"""CP resource parsing and admission tests; no prediction or flavor selection lives here."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("execution_runtime", ROOT / "capacity-controller.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
CONFIG = json.loads((ROOT / "capacity-config.json").read_text())
GIB = 1024**3


class RuntimeTests(unittest.TestCase):
    def test_kubernetes_quantities(self):
        self.assertEqual(m.quantity("150m"), .15)
        self.assertEqual(m.quantity("2Gi"), 2*GIB)
        self.assertEqual(m.quantity("500000000n"), .5)

    def test_nonfinite_quantity_rejected(self):
        with self.assertRaises(ValueError):
            m.quantity("1e999")

    def test_init_sidecar_requests(self):
        pod = {"spec": {"containers": [{"resources": {"requests": {"cpu": "1"}}}],
                        "initContainers": [{"restartPolicy": "Always", "resources": {"requests": {"cpu": ".2"}}},
                                           {"resources": {"requests": {"cpu": "2"}}}],
                        "overhead": {"cpu": ".1"}}}
        self.assertAlmostEqual(m.pod_requests(pod)["cpu"], 2.3)

    def test_quota_admission(self):
        limits = {"max_cores": 8, "used_cores": 7, "max_ram": 8192, "used_ram": 4096,
                  "max_instances": 10, "used_instances": 4}
        self.assertFalse(m.fits_quota({"vcpus": 2, "ram": 4096}, limits))

    def test_host_admission(self):
        host = {"cpu_used_ratio": .1, "free_vcpus": 8, "free_memory": 9*GIB, "free_disk": 100*GIB}
        f = {"vcpus": 2, "ram": 6144, "disk": 30}
        self.assertTrue(m.host_fits(f, host, CONFIG))
        host["free_memory"] = 5*GIB
        self.assertFalse(m.host_fits(f, host, CONFIG))

    def test_cp_has_no_prediction_sizing_or_sequence_policy(self):
        for name in ("forecast", "decide", "choose_flavor", "execute_request"):
            self.assertFalse(hasattr(m, name), name)
        for name in ("select_flavor", "add", "remove", "recover", "finish_delete"):
            self.assertFalse(hasattr(m.Controller, name), name)


if __name__ == "__main__":
    unittest.main()
