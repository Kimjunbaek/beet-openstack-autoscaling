#!/usr/bin/env python3
"""CP execution boundary: execute exactly one EC2-selected step; never choose a next step."""
import argparse
import fcntl
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("worker_runtime", ROOT / "capacity-controller.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
STEPS = {"observe", "reserve_port", "create_vm", "join", "verify", "lb_add", "lb_disable",
         "cordon", "drain", "verify_relocated", "lb_remove", "delete_vm", "delete_node",
         "delete_port", "uncordon", "lb_enable", "health", "complete"}


def validate(r, cfg, now):
    if (not isinstance(r, dict) or r.get("version") != 2 or r.get("step") not in STEPS
            or r.get("worker") not in cfg["workers"]):
        raise ValueError("Invalid execution order")
    for field in ("operation_id", "command_id"):
        if not re.fullmatch(r"[a-f0-9]{32}", r.get(field, "")):
            raise ValueError("Invalid command identity")
    if not -10 <= now - runtime.finite_number(r.get("sent_at")) <= 120:
        raise ValueError("Expired execution order")


class StepExecutor:
    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state
        self.controller = runtime.Controller(cfg, {})
        self.cloud = self.controller.cloud

    def persist(self):
        runtime.save(ROOT / "worker-step-state.json", self.state)

    def resources(self, name):
        servers = [s for s in self.cloud.compute.servers() if s.name == name]
        ports = [p for p in self.cloud.network.ports(name=f"{name}-port") if p.name == f"{name}-port"]
        if len(servers) > 1 or len(ports) > 1:
            raise RuntimeError("Ambiguous worker resources")
        server, port = next(iter(servers), None), next(iter(ports), None)
        if server and server.metadata.get("beet_worker_automation") != runtime.TAG:
            raise RuntimeError("Unowned VM")
        if port and port.description != runtime.TAG:
            raise RuntimeError("Unowned port")
        if port and port.device_id and (not server or port.device_id != server.id):
            raise RuntimeError("Unexpected port attachment")
        raw = runtime.kubectl("get", "node", name, "--ignore-not-found", "-o", "json")
        node = json.loads(raw or "null")
        address = None
        if port:
            ips = [ip["ip_address"] for ip in port.fixed_ips if "." in ip["ip_address"]]
            if len(ips) != 1:
                raise RuntimeError("Expected one IPv4 address")
            address = ips[0]
        if node and address and address not in [a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP"]:
            raise RuntimeError("VM and Kubernetes addresses differ")
        return server, port, node, {"server_id": server.id if server else None,
                                    "port_id": port.id if port else None,
                                    "node_uid": node["metadata"]["uid"] if node else None,
                                    "address": address, "vm_status": server.status if server else None}

    def observe(self):
        c = self.controller
        snapshot = c.snapshot(time.time())
        snapshot.pop("pods", None)
        limits = self.cloud.compute.get_limits().absolute
        image = self.cloud.image.find_image(self.cfg["worker_image"], ignore_missing=False)
        return {"snapshot": snapshot,
                "flavors": [{"id": f.id, "name": f.name, "vcpus": f.vcpus, "ram": f.ram, "disk": f.disk}
                            for f in self.cloud.compute.flavors()],
                "limits": {"max_cores": limits.max_total_cores, "used_cores": limits.total_cores_used,
                           "max_ram": limits.max_total_ram_size, "used_ram": limits.total_ram_used,
                           "max_instances": limits.max_total_instances, "used_instances": limits.total_instances_used},
                "constraints": {**{k: self.cfg[k] for k in ("allowed_flavors", "minimum_disk_gb", "node_cpu_reserve",
                                  "node_memory_reserve", "physical_host", "host_cpu_limit",
                                  "host_cpu_reserve", "host_memory_reserve", "host_disk_reserve")},
                                "minimum_disk_gb": max(self.cfg["minimum_disk_gb"], image.min_disk or 0)},
                "active_operation": self.state.get("active")}

    def playbook(self, r):
        variables = {"worker_name": r["worker"], "execution_step": r["step"],
                     "automation_operation_id": r["operation_id"],
                     "worker_flavor": r.get("flavor", ""),
                     "worker_network": self.cfg["worker_network"], "worker_image": self.cfg["worker_image"],
                     "worker_security_group": self.cfg["worker_security_group"],
                     "octavia_pool_id": self.cfg["octavia_pool_id"], "octavia_vip": self.cfg["octavia_vip"],
                     "service_host": self.cfg["service_host"], "k3s_control_address": self.cfg["control_address"],
                     "worker_verification_passed": r["step"] == "lb_add"}
        subprocess.run([self.cfg["ansible_playbook"], "-i", str(ROOT / "inventory.ini"),
                        str(ROOT / "provision.yml"), "--tags", r["step"], "-e", json.dumps(variables)],
                       cwd=ROOT, check=True, timeout=600)

    def guard_create(self, r, server, port):
        if not port:
            raise RuntimeError("EC2 must request reserve_port before create_vm")
        if r.get("flavor") not in self.cfg["allowed_flavors"]:
            raise ValueError("Flavor is not allowed")
        if server:
            if server.metadata.get("beet_operation_id") != r["operation_id"]:
                raise RuntimeError("An existing VM belongs to a different creation operation")
            return
        host = r.get("host", {})
        if (host.get("name") != self.cfg["physical_host"]
                or not -10 <= time.time() - runtime.finite_number(host.get("observed_at")) <= 120):
            raise RuntimeError("Fresh physical telemetry required")
        for key in ("free_vcpus", "free_memory", "free_disk", "cpu_used_ratio"):
            runtime.finite_number(host.get(key))
        data = self.observe()
        choices = [f for f in data["flavors"] if f["name"] == r["flavor"]]
        if (len(choices) != 1 or choices[0]["disk"] < data["constraints"]["minimum_disk_gb"]
                or not runtime.fits_quota(choices[0], data["limits"])
                or not runtime.host_fits(choices[0], host, self.cfg)):
            raise RuntimeError("EC2-selected flavor failed admission; no alternative is selected on CP")

    def run_one(self, r, preview=False):
        validate(r, self.cfg, time.time())
        step, name = r["step"], r["worker"]
        if step == "observe":
            result = self.observe()
            _, _, _, identity = self.resources(name)
            result["identity"] = identity
            member = self.controller.member(self.controller.pool(), identity["address"]) if identity["address"] else None
            result["lb_enabled"] = member.is_admin_state_up if member else None
            return result
        if step == "health":
            self.controller.healthy_http()
            return {"http": 200}
        active = self.state.get("active")
        if active and (active["operation_id"] != r["operation_id"] or active["worker"] != name):
            raise RuntimeError("Another EC2 operation owns the execution lock")
        if r["operation_id"] in self.state.get("closed", []):
            if step == "complete":
                return {"completed": True}
            raise RuntimeError("Operation already closed")
        if active and r["command_id"] in active.get("results", {}):
            cached = active["results"][r["command_id"]]
            if cached["step"] != step:
                raise RuntimeError("Command ID reused for a different step")
            return cached["data"]
        server, port, node, identity = self.resources(name)
        context = active.get("context", {}) if active else {}
        for key in ("server_id", "port_id", "node_uid"):
            expected = context.get(key) or r.get(key)
            if expected and identity[key] and expected != identity[key]:
                raise RuntimeError(f"Resource identity changed: {key}")
        if step == "create_vm":
            self.guard_create(r, server, port)
        if preview:
            return {"validated_step": step, "worker": name, "identity": identity, "executed": False}
        if not active:
            active = {"operation_id": r["operation_id"], "worker": name, "results": {}, "context": dict(identity)}
            self.state["active"] = active
        context = active["context"]
        active["running_step"] = step
        self.persist()
        c = self.controller
        result = {}
        if step == "reserve_port":
            if server or node:
                raise RuntimeError("Worker name already exists")
            if not port:
                network = self.cloud.network.find_network(self.cfg["worker_network"], ignore_missing=False)
                group = self.cloud.network.find_security_group(self.cfg["worker_security_group"], ignore_missing=False)
                self.cloud.network.create_port(name=f"{name}-port", network_id=network.id,
                                               description=runtime.TAG, security_group_ids=[group.id])
        elif step in ("create_vm", "join", "verify", "lb_add"):
            if step != "create_vm" and (not server or server.status != "ACTIVE" or not port):
                raise RuntimeError("An active owned VM is required")
            if step == "lb_add" and (not node or not context.get("verified")
                                     or node["metadata"]["uid"] != context.get("node_uid")):
                raise RuntimeError("EC2 must request successful verification before lb_add")
            self.playbook(r)
            if step == "verify":
                context["verified"] = True
        elif step in ("lb_disable", "lb_enable", "lb_remove"):
            if not identity["address"]:
                raise RuntimeError("Worker address is missing")
            pool = c.pool()
            member = c.member(pool, identity["address"])
            if member:
                result["member_was_enabled"] = member.is_admin_state_up
                if step == "lb_remove":
                    self.cloud.load_balancer.delete_member(member, pool)
                    deadline = time.monotonic() + 120
                    while self.cloud.load_balancer.find_member(member.id, pool, ignore_missing=True):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Member deletion timeout")
                        time.sleep(3)
                else:
                    enabled = step == "lb_enable"
                    self.cloud.load_balancer.update_member(member, pool, is_admin_state_up=enabled)
                    c.wait_member(pool, member.id, enabled)
            elif step == "lb_enable":
                raise RuntimeError("Cannot enable a missing member")
        elif step in ("cordon", "uncordon", "drain", "verify_relocated"):
            if not node or not server:
                raise RuntimeError("An owned VM and node are required")
            if step == "drain":
                pods = runtime.kube_json("get", "pods", "-A")["items"]
                context["owners"] = list(c.workloads(pods, name))
                self.persist()
                runtime.command(["sudo", "-n", "k3s", "kubectl", "drain", name, "--ignore-daemonsets",
                                 f"--timeout={self.cfg['drain_timeout_seconds']}s"],
                                timeout=self.cfg["drain_timeout_seconds"] + 15)
                context["drained"] = True
            elif step == "verify_relocated":
                if not context.get("drained"):
                    raise RuntimeError("Drain verification requires a successful drain")
                c.wait_workloads(context.get("owners", []), name)
                c.healthy_http()
                result["snapshot"] = c.snapshot(time.time())
                result["snapshot"].pop("pods", None)
                context["relocated"] = True
            else:
                runtime.kubectl(step, name)
        elif step == "delete_vm":
            if not context.get("relocated"):
                raise RuntimeError("Successful relocation verification is required")
            c.healthy_http()
            if server:
                if not node or not node["spec"].get("unschedulable"):
                    raise RuntimeError("Worker must remain cordoned")
                c.workloads(runtime.kube_json("get", "pods", "-A")["items"], name)
                remaining = [p for p in runtime.kube_json("get", "pods", "-A")["items"]
                             if p["spec"].get("nodeName") == name and p["status"].get("phase") not in ("Succeeded", "Failed")
                             and not any(o["kind"] == "DaemonSet" for o in p["metadata"].get("ownerReferences", []))
                             and "kubernetes.io/config.mirror" not in p["metadata"].get("annotations", {})]
                if remaining:
                    raise RuntimeError("Workloads returned to the node; deletion refused")
                self.cloud.compute.delete_server(server)
                self.cloud.compute.wait_for_delete(server, wait=180)
        elif step == "delete_node":
            if server:
                raise RuntimeError("EC2 must request VM deletion first")
            if node:
                if not context.get("node_uid"):
                    raise RuntimeError("No recorded node identity")
                runtime.kubectl("delete", "node", name, "--ignore-not-found", "--timeout=30s")
        elif step == "delete_port":
            if server or node or (port and port.device_id):
                raise RuntimeError("VM/node/port attachment still exists")
            if port:
                self.cloud.network.delete_port(port)
        elif step == "complete":
            self.state["closed"] = (self.state.get("closed", []) + [r["operation_id"]])[-50:]
            self.state.pop("active", None)
            self.persist()
            return {"completed": True}
        else:
            raise ValueError("Unsupported execution step")
        _, _, _, identity = self.resources(name)
        context.update({k: v for k, v in identity.items() if v is not None})
        active["context"] = context
        result.update(identity)
        active["results"][r["command_id"]] = {"step": step, "data": result}
        active.pop("running_step", None)
        self.persist()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.read(16385)
    if len(raw) > 16384:
        raise ValueError("Order is too large")
    r = json.loads(raw)
    with (ROOT / "provision.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(75)
        state_file = ROOT / "worker-step-state.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        result = StepExecutor(runtime.read_config(), state).run_one(r, args.preview)
        print(json.dumps({"ok": True, "step": r["step"], "operation_id": r["operation_id"], "data": result}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[-1200:]}))
        raise SystemExit(1)
