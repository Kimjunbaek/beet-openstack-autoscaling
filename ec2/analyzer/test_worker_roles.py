"""EC2 policy/executor regression tests. SSH and cloud mutation are mocked."""
import copy
import io
import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import config
import decide
import executor

GIB = 1024 ** 3


def metrics(workers=()):
    return decide.Metrics(cpu_requests_now=.2, openstack_quota_used=.3, capacity={
        "observed_at": time.time(),
        "nodes": {name: {"cpu": 2, "memory": 4 * GIB, "used_memory": .5 * GIB, "ready": True}
                  for name in ("beet-k3s-cp", "beet-k3s-w1", *workers)},
        "demand": {"cpu": .5, "memory": 2 * GIB},
        "host": {"name": "beet-host1", "observed_at": time.time(), "free_vcpus": 8,
                 "free_memory": 9 * GIB, "free_disk": 100 * GIB, "cpu_used_ratio": .15}})


class RolesTests(unittest.TestCase):
    def test_existing_relocate_decision_is_preserved(self):
        m = metrics()
        m.steal_by_vm = {"beet-k3s-w1": .2}
        self.assertEqual(decide.decide(m, decide.State(), time.time()).action, decide.Action.RELOCATE)

    def test_missing_capacity_blocks_live_worker_order(self):
        m = decide.Metrics(cpu_requests_now=.9, host_mem_avail_gb=9, openstack_quota_used=.2,
                           requests_saturation={"earliest": 5, "expected": 6, "latest": 7})
        with patch.object(config, "LIVE_WORKER_SCALING", True):
            d = decide.decide(m, decide.State(), time.time())
        self.assertEqual(d.action, decide.Action.SATURATED)
        self.assertIsNone(d.request)
    def test_predictive_trigger_selects_w2(self):
        m = metrics()
        m.requests_saturation = {"earliest": 5}
        d = decide.decide(m, decide.State(), time.time())
        self.assertEqual(d.action, decide.Action.ADD_VM)
        self.assertEqual(d.target, "beet-k3s-w2")
        self.assertEqual(d.request["action"], "add")

    def test_w2_memory_pressure_selects_w3(self):
        m = metrics(["beet-k3s-w2"])
        m.capacity["nodes"]["beet-k3s-w2"]["used_memory"] = 3.8 * GIB
        d = decide.decide(m, decide.State(), time.time())
        self.assertEqual(d.target, "beet-k3s-w3")
        self.assertEqual(d.action, decide.Action.ADD_VM)

    def test_physical_ram_shortage_prevents_add(self):
        m = metrics()
        m.capacity["demand"]["cpu"] = 5
        m.capacity["host"]["free_memory"] = 5 * GIB
        self.assertEqual(decide.decide(m, decide.State(), time.time()).action, decide.Action.SATURATED)

    def test_missing_host_memory_no_small_vm_fallback(self):
        self.assertIsNone(decide.choose_flavor(decide.Metrics(), "CPU")[0])

    def test_scalein_w3_first_after_continuous_hold(self):
        m = metrics(["beet-k3s-w2", "beet-k3s-w3"])
        state = decide.State()
        start = time.time()
        first = decide.decide(m, state, start)
        self.assertEqual(first.action, decide.Action.NONE)
        for elapsed in range(30, 931, 30):
            m.capacity["observed_at"] = start + elapsed
            result = decide.decide(m, state, start + elapsed)
        self.assertEqual(result.action, decide.Action.REMOVE_VM)
        self.assertEqual(result.target, "beet-k3s-w3")

    def test_collection_gap_resets_low_hold(self):
        m = metrics(["beet-k3s-w2"])
        state = decide.State()
        start = time.time()
        decide.decide(m, state, start)
        m.capacity["observed_at"] = start + 950
        self.assertEqual(decide.decide(m, state, start + 950).action, decide.Action.NONE)
        self.assertEqual(state.low_load_since, start + 950)

    def test_executor_requires_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                executor.Executor(state_file=Path(directory)/"job.json").start_add(time.time())

    def test_remove_uses_cp_and_never_direct_cloud_or_kubernetes(self):
        nova, kube = Mock(), Mock()
        process = Mock(stdin=io.BytesIO())
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(executor.subprocess, "Popen", return_value=process) as popen:
                ex = executor.Executor(nova_factory=nova, k8s_factory=kube, state_file=Path(directory)/"job.json")
                r = {"action": "remove", "worker": "beet-k3s-w3", "demand": {}, "host": {}}
                ex.start_remove(time.time(), request=r)
                self.assertEqual(popen.call_args.args[0][-1], "step")
                self.assertEqual(ex.job.stage, "observe")
                ex._output.close()
                ex._lock.close()
        nova.assert_not_called()
        kube.assert_not_called()


if __name__ == "__main__":
    unittest.main()
