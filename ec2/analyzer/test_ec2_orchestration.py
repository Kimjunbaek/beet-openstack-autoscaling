"""State-machine tests: CP mocks cannot choose the flavor or enqueue another action."""
import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import executor
from decide import select_worker_flavor

GIB = 1024**3


def observation(workers=()):
    nodes = {name: {"cpu": 2, "memory": 4*GIB, "status": "ready", "used_memory": .5*GIB}
             for name in ("beet-k3s-cp", "beet-k3s-w1", *workers)}
    return {"snapshot": {"nodes": nodes, "managed": [{"name": w} for w in workers],
                         "demand": {"cpu": .5, "memory": 2*GIB}, "pending": False,
                         "largest_pending": {"cpu": 0, "memory": 0}},
            "limits": {"max_cores": 20, "used_cores": 6, "max_ram": 51200, "used_ram": 12288,
                       "max_instances": 10, "used_instances": 3},
            "flavors": [{"name": "t3.medium", "vcpus": 2, "ram": 4096, "disk": 30},
                        {"name": "t3.medium-6g", "vcpus": 2, "ram": 6144, "disk": 30},
                        {"name": "t3.large", "vcpus": 2, "ram": 8192, "disk": 40}],
            "constraints": {"allowed_flavors": ["t3.medium", "t3.medium-6g", "t3.large"],
                            "minimum_disk_gb": 30, "node_cpu_reserve": .25, "node_memory_reserve": .75*GIB,
                            "target_utilization": .75, "physical_host": "beet-host1", "host_cpu_limit": .85,
                            "host_cpu_reserve": 1, "host_memory_reserve": 2*GIB, "host_disk_reserve": 10*GIB},
            "identity": {"server_id": "vm" if workers else None, "port_id": "port" if workers else None,
                         "node_uid": "uid" if workers else None}, "lb_enabled": True, "active_operation": None}


def request(kind="add", worker="beet-k3s-w2"):
    return {"action": kind, "worker": worker, "demand": {"cpu": 1, "memory": 9.75*GIB if kind == "add" else 2*GIB},
            "host": {"name": "beet-host1", "observed_at": time.time(), "free_memory": 9*GIB,
                     "free_vcpus": 10, "free_disk": 100*GIB, "cpu_used_ratio": .1}}


class FakeCP:
    def __init__(self, data):
        self.data = data
        self.orders = []
        self.fail = set()

    def popen(self, args, stdin, stdout, stderr):
        owner = self
        process = Mock()
        process.poll.return_value = 0
        class Input(io.BytesIO):
            def close(self):
                r = json.loads(self.getvalue())
                owner.orders.append(r)
                if r["step"] in owner.fail:
                    reply = {"ok": False, "error": "controlled step failure"}
                    process.poll.return_value = 1
                else:
                    data = copy.deepcopy(owner.data) if r["step"] == "observe" else {}
                    if r["step"] == "verify_relocated":
                        data["snapshot"] = owner.data["snapshot"]
                    if r["step"] == "reserve_port":
                        data["port_id"] = "port"
                    if r["step"] == "create_vm":
                        data.update(server_id="vm", port_id="port")
                    if r["step"] in ("join", "verify"):
                        data.update(server_id="vm", port_id="port", node_uid="uid")
                    reply = {"ok": True, "operation_id": r["operation_id"], "step": r["step"], "data": data}
                stdout.write((json.dumps(reply)+"\n").encode())
                stdout.flush()
                super().close()
        process.stdin = Input()
        return process


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/"job.json"

    def tearDown(self):
        self.directory.cleanup()

    def make(self):
        return executor.Executor(state_file=self.path, capacity_reader=lambda: {"host": request()["host"]})

    def drive(self, ex, start):
        for index in range(1, 100):
            event = ex.tick(start + index*30)
            if event:
                return event
        self.fail("No terminal event")

    def test_ec2_selects_6gb_for_predicted_5gb_shortfall(self):
        self.assertEqual(select_worker_flavor(observation(), request()), "t3.medium-6g")

    def test_ec2_refuses_flavor_when_physical_ram_missing(self):
        r = request()
        r["host"]["free_memory"] = 5*GIB
        with self.assertRaises(ValueError):
            select_worker_flavor(observation(), r)

    def test_ec2_refuses_flavor_when_project_quota_full(self):
        data = observation()
        data["limits"]["used_ram"] = 51200
        with self.assertRaises(ValueError):
            select_worker_flavor(data, request())

    def test_add_commands_and_flavor_are_chosen_on_ec2(self):
        cp = FakeCP(observation())
        with patch.object(executor.subprocess, "Popen", side_effect=cp.popen):
            ex = self.make()
            start = time.time()
            ex.start_add(start, request=request())
            event = self.drive(ex, start)
        self.assertEqual([r["step"] for r in cp.orders], executor.ADD_STEPS)
        create = next(r for r in cp.orders if r["step"] == "create_vm")
        self.assertEqual(create["flavor"], "t3.medium-6g")
        self.assertEqual(event.kind, "add_done")
        self.assertEqual(event.flavor, "t3.medium-6g")
        self.assertFalse(ex.busy)

    def test_remove_commands_are_separate_and_ec2_orders_them(self):
        cp = FakeCP(observation(["beet-k3s-w2", "beet-k3s-w3"]))
        with patch.object(executor.subprocess, "Popen", side_effect=cp.popen):
            ex = self.make()
            start = time.time()
            ex.start_remove(start, request=request("remove", "beet-k3s-w3"))
            event = self.drive(ex, start)
        self.assertEqual([r["step"] for r in cp.orders], executor.REMOVE_STEPS)
        self.assertEqual(event.kind, "remove_done")

    def test_failed_drain_rolls_back_from_ec2_and_never_deletes_vm(self):
        cp = FakeCP(observation(["beet-k3s-w2"]))
        cp.fail.add("drain")
        with patch.object(executor.subprocess, "Popen", side_effect=cp.popen):
            ex = self.make()
            start = time.time()
            ex.start_remove(start, request=request("remove"))
            event = self.drive(ex, start)
        steps = [r["step"] for r in cp.orders]
        self.assertNotIn("delete_vm", steps)
        self.assertEqual(steps[-4:], ["uncordon", "lb_enable", "health", "complete"])
        self.assertEqual(event.kind, "remove_failed")

    def test_failed_join_pauses_without_automatically_deleting_vm(self):
        cp = FakeCP(observation())
        cp.fail.add("join")
        with patch.object(executor.subprocess, "Popen", side_effect=cp.popen):
            ex = self.make()
            start = time.time()
            ex.start_add(start, request=request())
            event = self.drive(ex, start)
        self.assertEqual(event.kind, "add_failed")
        self.assertTrue(ex.job.blocked)
        self.assertNotIn("delete_vm", [r["step"] for r in cp.orders])
        ex._lock.close()

    def test_retry_reuses_command_id(self):
        cp = FakeCP(observation())
        cp.fail.add("reserve_port")
        with patch.object(executor.subprocess, "Popen", side_effect=cp.popen):
            ex = self.make()
            start = time.time()
            ex.start_add(start, request=request())
            self.drive(ex, start)
        ids = [r["command_id"] for r in cp.orders if r["step"] == "reserve_port"]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 1)
        ex._lock.close()

    def test_restart_resumes_saved_command_identity(self):
        cp = FakeCP(observation())
        with patch.object(executor.subprocess, "Popen", side_effect=cp.popen):
            ex = self.make()
            start = time.time()
            ex.start_add(start, request=request())
            command_id = ex.job.command_id
            ex._output.close()
            ex._lock.close()
            resumed = self.make()
            resumed.tick(start+1)
            self.assertEqual(cp.orders[-1]["command_id"], command_id)
            resumed._output.close()
            resumed._lock.close()


if __name__ == "__main__":
    unittest.main()
