"""EC2 orchestrator: choose flavor, issue one CP command, inspect result, choose the next command."""
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

import config
from decide import select_worker_flavor

log = logging.getLogger("analyzer")
ADD_TIMEOUT_SEC = 40 * 60
STEP_TIMEOUT_SEC = 660
EXIT_BUSY = 75
EXIT_REJECTED = 64
ADD_STEPS = ["observe", "reserve_port", "create_vm", "join", "verify", "lb_add", "health", "complete"]
REMOVE_STEPS = ["observe", "lb_disable", "cordon", "drain", "verify_relocated",
                "lb_remove", "delete_vm", "delete_node", "delete_port", "health", "complete"]


def ssh_command(remote: str) -> List[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={config.CP_SSH_KNOWN_HOSTS}",
            "-o", "IdentitiesOnly=yes", "-i", config.CP_SSH_KEY, config.CP_SSH_TARGET, remote]


@dataclass
class Job:
    kind: str
    started: float
    server_id: Optional[str] = None
    stage: str = "observe"
    flavor: Optional[str] = None
    operation_id: str = ""
    worker: str = ""
    request: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    index: int = 0
    command_id: str = ""
    identity: dict = field(default_factory=dict)
    attempts: int = 0
    ready_at: float = 0
    step_started: float = 0
    blocked: bool = False
    rollback: bool = False
    failure: str = ""
    lb_was_enabled: bool = False
    drain_wait: float = 20


@dataclass
class Event:
    kind: str
    elapsed_sec: float
    detail: str = ""
    flavor: Optional[str] = None


class Executor:
    def __init__(self, nova_factory=None, k8s_factory=None, node_ready=None,
                 ssh: Callable[[str], List[str]] = ssh_command, state_file=None, capacity_reader=None):
        # nova_factory/k8s_factory remain accepted for compatibility; this class never calls them.
        self._ssh = ssh
        self._capacity_reader = capacity_reader
        self._state_file = Path(state_file or config.WORKER_JOB_FILE)
        self._lock = None
        self._process = None
        self._output = None
        self.job = None
        self.last_lead_sec = None
        self.last_action_at = 0
        if self._state_file.exists():
            saved = json.loads(self._state_file.read_text())
            self.last_lead_sec = saved.get("last_lead_sec")
            self.last_action_at = saved.get("last_action_at", 0)
            if saved.get("job"):
                self._claim()
                self.job = Job(**saved["job"])
                log.warning("EC2 worker operation restored: %s %s", self.job.worker, self.job.stage)

    @property
    def busy(self):
        return self.job is not None

    def _claim(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self._state_file.with_suffix(".lock").open("a")
        if os.name == "posix":
            import fcntl
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _save(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_file.with_suffix(".tmp")
        with temporary.open("w") as stream:
            os.chmod(temporary, 0o600)
            json.dump({"version": 2, "job": asdict(self.job) if self.job else None,
                       "last_lead_sec": self.last_lead_sec, "last_action_at": self.last_action_at}, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self._state_file)

    def _start(self, kind, now, request):
        if self.busy:
            raise RuntimeError("An EC2 worker operation is already active")
        if (not request or request.get("action") != kind
                or request.get("worker") not in config.MANAGED_WORKERS
                or "demand" not in request or "host" not in request):
            raise ValueError("A validated EC2 scaling observation is required")
        self._claim()
        self.job = Job(kind, now, operation_id=uuid.uuid4().hex, worker=request["worker"],
                       request=request, steps=list(ADD_STEPS if kind == "add" else REMOVE_STEPS))
        self.last_action_at = now
        self._save()
        self._launch(now)

    def start_add(self, now, flavor=None, request=None):
        # Flavor is always chosen on EC2 after the inventory observation, never by CP.
        self._start("add", now, request)

    def start_remove(self, now, request=None):
        self._start("remove", now, request)

    def _host(self):
        reader = self._capacity_reader
        if reader is None:
            import prom
            reader = lambda: prom.capacity_context([])
        return reader()["host"]

    def _launch(self, now):
        j = self.job
        j.stage = j.steps[j.index]
        j.command_id = j.command_id or uuid.uuid4().hex
        j.step_started = j.step_started or now
        r = {"version": 2, "operation_id": j.operation_id, "command_id": j.command_id,
             "step": j.stage, "worker": j.worker, "sent_at": time.time(),
             **j.identity}
        if j.stage == "create_vm":
            r.update(flavor=j.flavor, host=self._host())
        elif j.flavor:
            r["flavor"] = j.flavor
        self._save()
        self._output = tempfile.TemporaryFile()
        self._process = subprocess.Popen(self._ssh("step"), stdin=subprocess.PIPE,
                                         stdout=self._output, stderr=subprocess.STDOUT)
        self._process.stdin.write(json.dumps(r).encode())
        self._process.stdin.close()
        log.info("EC2 command: %s worker=%s flavor=%s operation=%s", j.stage, j.worker, j.flavor, j.operation_id)

    def _read_result(self):
        self._output.seek(0)
        lines = self._output.read().decode(errors="replace").splitlines()
        self._output.close()
        self._output = None
        for line in reversed(lines):
            try:
                result = json.loads(line)
                if isinstance(result, dict) and isinstance(result.get("ok"), bool):
                    return result
            except ValueError:
                pass
        return {"ok": False, "error": " / ".join(lines[-3:])[-1000:] or "No structured CP response"}

    def _inspect(self, data):
        j = self.job
        if j.stage == "observe":
            active = data.get("active_operation")
            if active and active["operation_id"] != j.operation_id:
                raise RuntimeError("An unfinished CP operation must be reconciled")
            s = data["snapshot"]
            nodes = s["nodes"]
            managed = {w["name"] for w in s["managed"]}
            if any(n["status"] != "ready" for n in nodes.values()):
                raise RuntimeError("A node is unavailable")
            j.identity.update({k: v for k, v in data["identity"].items()
                               if k in ("server_id", "port_id", "node_uid") and v is not None})
            if j.kind == "add":
                available = [w for w in config.MANAGED_WORKERS if w not in nodes and w not in managed]
                if not available or j.worker != available[0]:
                    raise RuntimeError("EC2 add target no longer matches the worker order")
                j.request["host"] = self._host()
                j.flavor = select_worker_flavor(data, j.request)
                log.info("EC2 selected flavor: %s for %s", j.flavor, j.worker)
            else:
                ordered = [w for w in config.MANAGED_WORKERS if w in managed]
                if not ordered or j.worker != ordered[-1]:
                    raise RuntimeError("EC2 must remove the last managed worker first")
                self._remaining_headroom(s)
                j.lb_was_enabled = data.get("lb_enabled") is True
            j.drain_wait = config.WORKER_CONNECTION_DRAIN_SEC
        elif j.stage == "verify_relocated":
            self._remaining_headroom(data["snapshot"])
        for key in ("server_id", "port_id", "node_uid"):
            if data.get(key):
                j.identity[key] = data[key]

    def _remaining_headroom(self, snapshot):
        j = self.job
        others = [n for name, n in snapshot["nodes"].items() if name != j.worker]
        demand = {r: max(snapshot["demand"][r], j.request["demand"][r]) for r in ("cpu", "memory")}
        if (snapshot["pending"] or any(n.get("memory_pressure") for n in snapshot["nodes"].values())
                or any(demand[r] > sum(n[r] for n in others) * .70 for r in demand)):
            raise RuntimeError("EC2 refuses removal: remaining nodes lack CPU/RAM headroom")

    def _failed(self, now, reason):
        j = self.job
        j.failure = reason
        # EC2 explicitly requests rollback only before destructive member/VM deletion.
        if (j.kind == "remove" and not j.rollback
                and j.stage in ("lb_disable", "cordon", "drain", "verify_relocated")):
            j.rollback = True
            j.steps = ["uncordon"] + (["lb_enable"] if j.lb_was_enabled else []) + ["health", "complete"]
            j.index = 0
            j.command_id = ""
            j.attempts = 0
            j.step_started = 0
            j.ready_at = now
            self._save()
            log.error("EC2 chose rollback: %s", reason)
            return None
        j.blocked = True
        self._save()
        return Event(f"{j.kind}_failed", now - j.started,
                     f"{j.worker} step={j.stage}; paused for reconciliation: {reason}", j.flavor)

    def tick(self, now):
        j = self.job
        if not j or j.blocked:
            return None
        try:
            if self._process is None:
                if now >= j.ready_at:
                    self._launch(now)
                return None
            status = self._process.poll()
            if status is None:
                if now - j.step_started <= STEP_TIMEOUT_SEC and now - j.started <= ADD_TIMEOUT_SEC:
                    return None
                self._process.kill()
                self._process.wait()
                status = 124
            response = self._read_result()
            self._process = None
            if status == EXIT_BUSY:
                if now - j.started > ADD_TIMEOUT_SEC:
                    j.blocked = True
                    j.failure = "CP execution lock exceeded the EC2 operation deadline"
                    self._save()
                    return Event(f"{j.kind}_failed", now - j.started, j.failure, j.flavor)
                j.ready_at = now + 30
                self._save()
                return None
            if status != 0 or not response.get("ok"):
                j.attempts += 1
                if j.attempts < 3 and now - j.started <= ADD_TIMEOUT_SEC:
                    j.ready_at = now + 30
                    j.step_started = 0
                    self._save()
                    return None
                return self._failed(now, response.get("error", f"SSH exited {status}"))
            if (response.get("operation_id") != j.operation_id or response.get("step") != j.stage):
                return self._failed(now, "CP response identity does not match the EC2 order")
            self._inspect(response["data"])
            log.info("EC2 verified step result: %s worker=%s", j.stage, j.worker)
            previous = j.stage
            j.index += 1
            j.command_id = ""
            j.attempts = 0
            j.step_started = 0
            if j.index == len(j.steps):
                elapsed = now - j.started
                if j.kind == "add" and not j.rollback:
                    self.last_lead_sec = elapsed
                event = Event(f"{j.kind}_{'failed' if j.rollback else 'done'}", elapsed,
                              f"{j.worker}; " + (f"EC2 rollback finished: {j.failure}" if j.rollback else "EC2 completed all ordered steps"),
                              j.flavor)
                self.job = None
                self.last_action_at = now
                self._save()
                if self._lock:
                    self._lock.close()
                    self._lock = None
                return event
            j.ready_at = now + (j.drain_wait if previous == "cordon" else 0)
            self._save()
            return None
        except Exception as exc:
            log.exception("EC2 worker flow stopped")
            return self._failed(now, str(exc))
