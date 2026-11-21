"""CP executes one order and never selects a flavor or starts a following step."""
import copy
import importlib.util
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
CONFIG = json.loads(Path(__file__).with_name("capacity-config.json").read_text())
GIB = 1024**3
FLAVORS = [{"name": "t3.medium", "vcpus": 2, "ram": 4096, "disk": 30},
           {"name": "t3.medium-6g", "vcpus": 2, "ram": 6144, "disk": 30}]
LIMITS = {"max_cores": 20, "used_cores": 6, "max_ram": 51200, "used_ram": 12288,
          "max_instances": 10, "used_instances": 3}

spec = importlib.util.spec_from_file_location("worker_exec", Path(__file__).with_name("worker-exec.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def order(step="join"):
    return {"version": 2, "operation_id": "a"*32, "command_id": "b"*32,
            "worker": "beet-k3s-w2", "step": step, "sent_at": time.time(), "flavor": "t3.medium-6g",
            "host": {"name": "beet-host1", "observed_at": time.time(), "free_memory": 9*GIB,
                     "free_vcpus": 10, "free_disk": 100*GIB, "cpu_used_ratio": .1}}


class ExecutionTests(unittest.TestCase):
    def make(self):
        e = m.StepExecutor.__new__(m.StepExecutor)
        e.cfg = copy.deepcopy(CONFIG)
        e.state = {}
        e.controller = Mock()
        e.cloud = e.controller.cloud
        e.persist = Mock()
        node = {"metadata": {"uid": "uid"}, "spec": {}}
        identity = {"server_id": "vm", "port_id": "port", "node_uid": "uid", "address": "192.0.2.183"}
        server = SimpleNamespace(id="vm", name="beet-k3s-w2", status="ACTIVE")
        port = SimpleNamespace(id="port")
        e.resources = Mock(return_value=(server, port, node, identity))
        e.playbook = Mock()
        return e

    def test_legacy_whole_operation_is_not_a_step(self):
        with self.assertRaises(ValueError):
            m.validate(order("scale"), CONFIG, time.time())

    def test_stale_order_is_rejected(self):
        r = order()
        r["sent_at"] -= 121
        with self.assertRaises(ValueError):
            m.validate(r, CONFIG, time.time())

    def test_baseline_worker_is_rejected(self):
        r = order()
        r["worker"] = "beet-k3s-w1"
        with self.assertRaises(ValueError):
            m.validate(r, CONFIG, time.time())

    def test_join_only_runs_join_not_verify_or_lb(self):
        e = self.make()
        r = order()
        e.run_one(r)
        e.playbook.assert_called_once_with(r)
        self.assertEqual(e.playbook.call_args.args[0]["step"], "join")
        e.controller.select_flavor.assert_not_called()
        e.controller.add.assert_not_called()
        e.controller.remove.assert_not_called()

    def test_cp_does_not_replace_ec2_selected_flavor(self):
        e = self.make()
        e.guard_create = Mock()
        r = order("create_vm")
        e.run_one(r)
        self.assertEqual(e.playbook.call_args.args[0]["flavor"], "t3.medium-6g")
        e.controller.select_flavor.assert_not_called()

    def test_admission_rejects_without_selecting_smaller_flavor(self):
        e = self.make()
        e.observe = Mock(return_value={"flavors": FLAVORS, "limits": LIMITS,
                                     "constraints": {"minimum_disk_gb": 30}})
        r = order("create_vm")
        r["host"]["free_memory"] = 5*GIB
        with self.assertRaises(RuntimeError):
            e.guard_create(r, None, SimpleNamespace(id="port"))
        e.controller.select_flavor.assert_not_called()
        e.playbook.assert_not_called()

    def test_create_requires_ec2_to_reserve_port_first(self):
        e = self.make()
        with self.assertRaises(RuntimeError):
            e.guard_create(order("create_vm"), None, None)

    def test_preview_never_mutates_or_calls_playbook(self):
        e = self.make()
        result = e.run_one(order(), preview=True)
        self.assertFalse(result["executed"])
        self.assertEqual(e.state, {})
        e.persist.assert_not_called()
        e.playbook.assert_not_called()

    def test_same_command_id_is_idempotent(self):
        e = self.make()
        r = order()
        self.assertEqual(e.run_one(r), e.run_one(r))
        e.playbook.assert_called_once()

    def test_command_id_cannot_change_step(self):
        e = self.make()
        r = order()
        e.run_one(r)
        r["step"] = "verify"
        with self.assertRaises(RuntimeError):
            e.run_one(r)

    def test_another_operation_cannot_preempt(self):
        e = self.make()
        e.state["active"] = {"operation_id": "c"*32, "worker": "beet-k3s-w2", "context": {}}
        with self.assertRaises(RuntimeError):
            e.run_one(order())
        e.resources.assert_not_called()

    def test_changed_node_uid_is_rejected(self):
        e = self.make()
        r = order()
        r["node_uid"] = "old-uid"
        with self.assertRaises(RuntimeError):
            e.run_one(r)
        e.playbook.assert_not_called()

    def test_vm_delete_requires_successful_relocation(self):
        e = self.make()
        with self.assertRaises(RuntimeError):
            e.run_one(order("delete_vm"))
        e.cloud.compute.delete_server.assert_not_called()

    def test_closed_operation_cannot_be_replayed(self):
        e = self.make()
        e.state["closed"] = ["a"*32]
        with self.assertRaises(RuntimeError):
            e.run_one(order())
        e.playbook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
