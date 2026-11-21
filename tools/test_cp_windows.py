"""Offline CP logic tests on Windows; does NOT validate Linux flock behavior."""
import sys
import types
import unittest
from pathlib import Path

if sys.platform != "win32":
    raise SystemExit("On Linux run: python -m unittest discover -s cp")

# The existing tests mock execution, SDK resources and persistence. They do not
# enter the production CLI locking path. Never import this shim in production.
sys.modules["fcntl"] = types.SimpleNamespace(
    LOCK_EX=1, LOCK_NB=2, flock=lambda *_: None
)
suite = unittest.defaultTestLoader.discover(
    str(Path(__file__).resolve().parents[1] / "cp"), pattern="test_*.py"
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
