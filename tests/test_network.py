"""Run pure graph projection regressions with Node when available."""
import shutil
import subprocess
import unittest
from pathlib import Path


class NetworkTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for frontend model tests')
    def test_projection_and_states(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(['node', 'tests/test_network.mjs'], cwd=root, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
