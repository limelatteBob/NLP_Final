"""Opt-in real Docker checks. Set EVOTASK_DOCKER_TESTS=1 only after preparing the image."""
import os
import tempfile
import unittest
from pathlib import Path

from evotask.config import Config
from evotask.sandbox import DockerSandbox


@unittest.skipUnless(os.environ.get("EVOTASK_DOCKER_TESTS") == "1", "Real Docker checks require EVOTASK_DOCKER_TESTS=1 and a ready image")
class DockerIntegrationTests(unittest.TestCase):
    def test_execution_and_isolation(self):
        with tempfile.TemporaryDirectory(prefix="evotask-docker-") as folder:
            root = Path(folder)
            workspace = root / "agent_a"
            workspace.mkdir()
            (root / "agent_b_secret.txt").write_text("not mounted")
            (workspace / "inputs").mkdir()
            (workspace / "inputs" / "original.txt").write_text("readonly")
            code = """from pathlib import Path
import socket
assert not Path('/agent_b_secret.txt').exists()
assert not Path('/work/../agent_b_secret.txt').exists()
try:
    Path('/work/inputs/original.txt').write_text('changed')
    raise AssertionError('input mount should be readonly')
except OSError:
    pass
try:
    Path('/etc/evotask-write-test').write_text('forbidden')
    raise AssertionError('root should be readonly')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
    raise AssertionError('network should be disabled')
except OSError:
    pass
Path('result.txt').write_text('sandbox verified')
print('sandbox verified')
"""
            (workspace / "check.py").write_text(code)
            result = DockerSandbox(Config(tool_timeout=10)).execute(workspace, "check.py")
            self.assertTrue(result["success"], result)
            self.assertEqual((workspace / "result.txt").read_text(), "sandbox verified")
            self.assertEqual((workspace / "inputs" / "original.txt").read_text(), "readonly")

    def test_runaway_script_fails_with_deadline(self):
        with tempfile.TemporaryDirectory(prefix="evotask-docker-") as folder:
            root = Path(folder)
            (root / "loop.py").write_text("while True: pass\n")
            result = DockerSandbox(Config(tool_timeout=2)).execute(root, "loop.py")
            self.assertFalse(result["success"])
