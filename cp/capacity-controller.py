#!/usr/bin/env python3
"""CP execution helpers only. Prediction, sizing and orchestration live on EC2.

The old filename is retained for compatibility. Execute worker-exec.py with one EC2 order.
"""
import json
import math
import re
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import openstack

ROOT = Path(__file__).resolve().parent
TAG = "k3s-worker-v1"

def quantity(value):
    match = re.fullmatch(r"([0-9.]+)([eE][+-]?[0-9]+|[a-zA-Z]*)", str(value))
    if not match:
        raise ValueError(f"Unsupported Kubernetes quantity: {value}")
    number, suffix = match.groups()
    factors = {"": 1, "n": 1e-9, "u": 1e-6, "m": .001,
               "k": 1000, "K": 1000, "M": 1e6, "G": 1e9, "T": 1e12,
               "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    factor = 10 ** int(suffix[1:]) if suffix.startswith(("e", "E")) else factors[suffix]
    result = float(Decimal(number) * Decimal(str(factor)))
    if not math.isfinite(result) or result < 0:
        raise ValueError("Resource quantities must be finite and nonnegative")
    return result

def pod_requests(pod):
    """Kubernetes effective requests, including restartable init sidecars."""
    spec = pod["spec"]
    effective = {}
    for resource in ("cpu", "memory"):
        def request(container):
            return quantity(container.get("resources", {}).get("requests", {}).get(resource, "0"))
        regular = sum(request(c) for c in spec.get("containers", []))
        sidecars, initialization = 0, 0
        for container in spec.get("initContainers", []):
            value = request(container)
            if container.get("restartPolicy") == "Always":
                sidecars += value
                initialization = max(initialization, sidecars)
            else:
                initialization = max(initialization, sidecars + value)
        value = max(regular + sidecars, initialization)
        if resource in spec.get("resources", {}).get("requests", {}):
            value = quantity(spec["resources"]["requests"][resource])
        effective[resource] = value + quantity(spec.get("overhead", {}).get(resource, "0"))
    return effective

def fits_quota(flavor, limits):
    for resource, demand in (("cores", flavor["vcpus"]), ("ram", flavor["ram"]), ("instances", 1)):
        maximum = limits[f"max_{resource}"]
        if maximum >= 0 and limits[f"used_{resource}"] + demand > maximum:
            return False
    return True

def command(args, timeout=30):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-1800:]}")
    return result.stdout

def kubectl(*args):
    return command(["sudo", "-n", "k3s", "kubectl", *args])

def kube_json(*args):
    return json.loads(kubectl(*args, "-o", "json"))

def save(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)

def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("Expected a finite nonnegative number")
    return value

def host_fits(flavor, host, config):
    """Conservative single-host admission: actual RAM/disk plus Nova inventory, without overcommit."""
    return (host["cpu_used_ratio"] < config["host_cpu_limit"]
            and flavor["vcpus"] + config["host_cpu_reserve"] <= host["free_vcpus"]
            and flavor["ram"] * 1024**2 + config["host_memory_reserve"] <= host["free_memory"]
            and flavor["disk"] * 1024**3 + config["host_disk_reserve"] <= host["free_disk"])

def read_config():
    config = json.loads((ROOT / "capacity-config.json").read_text())
    if config["workers"] != ["beet-k3s-w2", "beet-k3s-w3"]:
        raise ValueError("This executor is restricted to w2 and w3")
    for key in ("node_cpu_reserve", "node_memory_reserve", "host_cpu_reserve",
                "host_memory_reserve", "host_disk_reserve", "drain_timeout_seconds"):
        if config[key] <= 0:
            raise ValueError("Execution guard must be positive: " + key)
    return config


class Controller:
    def __init__(self, config, state):
        self.config, self.state = config, state
        self.cloud = openstack.connect(cloud=config["os_cloud"], api_timeout=15)
        self.lb = self.cloud.load_balancer

    def pool(self):
        pool = self.lb.get_pool(self.config["octavia_pool_id"])
        if pool.protocol != "HTTP" or not pool.health_monitor_id:
            raise RuntimeError("Expected the existing HTTP pool with a health monitor")
        return pool

    def healthy_http(self, address=None):
        url = f"http://{address or self.config['octavia_vip']}/status.php"
        request = urllib.request.Request(url, headers={"Host": self.config["service_host"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=8) as response:
            if response.status != 200 or not json.load(response).get("installed"):
                raise RuntimeError("Nextcloud status check failed")

    def snapshot(self, now):
        config = self.config
        raw_nodes = kube_json("get", "nodes")["items"]
        pods = kube_json("get", "pods", "-A")["items"]
        metrics = json.loads(kubectl("get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes"))["items"]
        usage = {}
        for metric in metrics:
            timestamp = datetime.fromisoformat(metric["timestamp"].replace("Z", "+00:00")).timestamp()
            if now - timestamp > 120 or timestamp - now > 30:
                raise RuntimeError("Node metrics are stale")
            usage[metric["metadata"]["name"]] = metric["usage"]
        nodes = {}
        for node in raw_nodes:
            name = node["metadata"]["name"]
            if name not in config["baseline_nodes"] + config["workers"]:
                raise RuntimeError(f"Unexpected node {name}; capacity policy needs review")
            if name not in usage:
                raise RuntimeError(f"No current node metrics for {name}")
            conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
            taints = node["spec"].get("taints", [])
            ready = (conditions.get("Ready") == "True"
                     and all(conditions.get(c) != "True" for c in ("DiskPressure", "PIDPressure"))
                     and not node["spec"].get("unschedulable")
                     and not any(t["effect"] in ("NoSchedule", "NoExecute")
                                 and t["key"] != "node.kubernetes.io/memory-pressure" for t in taints))
            addresses = [a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP"]
            nodes[name] = {
                "status": "ready" if ready else "unavailable",
                "cpu": quantity(node["status"]["allocatable"]["cpu"]),
                "memory": quantity(node["status"]["allocatable"]["memory"]),
                "used_cpu": quantity(usage[name]["cpu"]), "used_memory": quantity(usage[name]["memory"]),
                "requested_memory": 0, "addresses": addresses,
                "uid": node["metadata"]["uid"],
                "memory_pressure": conditions.get("MemoryPressure") == "True",
            }
        if not all(name in nodes for name in config["baseline_nodes"]):
            raise RuntimeError("A baseline node is missing")
        total = {"cpu": 0, "memory": 0}
        largest_pending = {"cpu": 0, "memory": 0}
        pending = False
        for pod in pods:
            if pod["status"].get("phase") in ("Succeeded", "Failed"):
                continue
            requests = pod_requests(pod)
            for resource in total:
                total[resource] += requests[resource]
            name = pod["spec"].get("nodeName")
            if name in nodes:
                nodes[name]["requested_memory"] += requests["memory"]
            # A Pending image download or unbound PVC is not evidence that more nodes help.
            unscheduled = any(c["type"] == "PodScheduled" and c["status"] == "False"
                              and c.get("reason") == "Unschedulable"
                              and ("Insufficient cpu" in c.get("message", "")
                                   or "Insufficient memory" in c.get("message", ""))
                              for c in pod["status"].get("conditions", []))
            if not name and unscheduled:
                pending = True
                for resource in total:
                    largest_pending[resource] = max(largest_pending[resource], requests[resource])
        servers = [s for s in self.cloud.compute.servers() if s.name in config["workers"]]
        if len({s.name for s in servers}) != len(servers):
            raise RuntimeError("Ambiguous worker names")
        for server in servers:
            if server.metadata.get("beet_worker_automation") != TAG:
                raise RuntimeError(f"{server.name} is not owned by this automation")
        for name in config["workers"]:
            if name in nodes and not any(s.name == name for s in servers):
                raise RuntimeError(f"Stale Kubernetes record for {name}")
        demand = {"cpu": max(total["cpu"], sum(n["used_cpu"] for n in nodes.values())),
                  "memory": max(total["memory"], sum(n["used_memory"] for n in nodes.values()))}
        return {"nodes": nodes, "pods": pods, "demand": demand, "pending": pending,
                "largest_pending": largest_pending,
                "managed": [{"name": s.name, "id": s.id, "status": s.status} for s in servers]}


    def member(self, pool, address):
        matches = [m for m in self.lb.members(pool) if m.address == address and m.protocol_port == 80]
        if len(matches) > 1:
            raise RuntimeError("Ambiguous Octavia members")
        return matches[0] if matches else None

    def wait_member(self, pool, member_id, enabled):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            member = self.lb.get_member(member_id, pool)
            if member.provisioning_status == "ERROR":
                raise RuntimeError("Octavia member update failed")
            if member.provisioning_status == "ACTIVE" and member.is_admin_state_up == enabled:
                return
            time.sleep(3)
        raise RuntimeError("Octavia member update timed out")

    def workloads(self, pods, name):
        owners = set()
        for pod in pods:
            if pod["spec"].get("nodeName") != name or pod["status"].get("phase") in ("Succeeded", "Failed"):
                continue
            if "kubernetes.io/config.mirror" in pod["metadata"].get("annotations", {}):
                continue
            references = [o for o in pod["metadata"].get("ownerReferences", []) if o.get("controller")]
            if references and references[0]["kind"] == "DaemonSet":
                continue
            if not references or references[0]["kind"] not in ("ReplicaSet", "StatefulSet"):
                raise RuntimeError("Drain requires replicated Deployment or StatefulSet workloads")
            namespace = pod["metadata"]["namespace"]
            for volume in pod["spec"].get("volumes", []):
                if "hostPath" in volume or "emptyDir" in volume:
                    raise RuntimeError("A workload has node-local data; migrate it before scale-in")
                if "persistentVolumeClaim" in volume:
                    claim = kube_json("-n", namespace, "get", "pvc", volume["persistentVolumeClaim"]["claimName"])
                    pv = kube_json("get", "pv", claim["spec"]["volumeName"])
                    if ("local" in pv["spec"] or "hostPath" in pv["spec"]
                            or pv["spec"].get("nodeAffinity")):
                        raise RuntimeError("A workload has storage placement constraints")
            owner = references[0]
            if owner["kind"] == "ReplicaSet":
                replica = kube_json("-n", namespace, "get", "replicaset", owner["name"])
                parents = [o for o in replica["metadata"].get("ownerReferences", []) if o.get("controller")]
                if parents and parents[0]["kind"] == "Deployment":
                    owner = parents[0]
            owners.add((namespace, owner["kind"].lower(), owner["name"]))
        return owners

    def wait_workloads(self, owners, name):
        deadline = time.monotonic() + self.config["drain_timeout_seconds"]
        while time.monotonic() < deadline:
            ready = True
            for namespace, kind, workload in owners:
                obj = kube_json("-n", namespace, "get", kind, workload)
                desired = obj["spec"].get("replicas", 1)
                status = obj.get("status", {})
                ready &= (status.get("observedGeneration", 0) >= obj["metadata"]["generation"]
                          and status.get("readyReplicas", 0) >= desired)
            pods = kube_json("get", "pods", "-A")["items"]
            for pod in pods:
                if pod["status"].get("phase") in ("Succeeded", "Failed"):
                    continue
                owners_ref = pod["metadata"].get("ownerReferences", [])
                if any(o["kind"] == "DaemonSet" for o in owners_ref):
                    continue
                if pod["spec"].get("nodeName") == name:
                    ready = False
                if not pod["spec"].get("nodeName") or pod["status"].get("phase") == "Pending":
                    ready = False
            if ready:
                return
            time.sleep(5)
        raise RuntimeError("Evicted workloads did not become Ready on the remaining nodes")

if __name__ == "__main__":
    raise SystemExit("CP autonomous controller retired. EC2 must issue one step to worker-exec.py.")
