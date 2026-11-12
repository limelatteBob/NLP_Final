import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evotask.config import Config
from evotask.environment import read_env
from evotask.providers import APIProvider, ProviderUnavailable
from unittest.mock import MagicMock
import urllib.error


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="evotask-env-")
        self.root = Path(self.temp.name)
        self.env = self.root / ".env"

    def tearDown(self):
        self.temp.cleanup()

    def test_bom_quotes_comments_and_literal_shell_text(self):
        self.env.write_text('\ufeff# sample\nexport API_KEY="abc#123" # comment\nMODEL=sample # tail\nLITERAL=$(never-run)\n', encoding="utf-8")
        values = read_env(self.env)
        self.assertEqual(values["API_KEY"], "abc#123")
        self.assertEqual(values["MODEL"], "sample")
        self.assertEqual(values["LITERAL"], "$(never-run)")

    def test_legacy_variables_work_without_persisting_secret(self):
        self.env.write_text('API_KEY=private-value\nBASE_URL=https://api.deepseek.com\nMODEL=course-model\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config.load(env_path=self.env)
            self.assertEqual(cfg.api_key_env, "API_KEY")
            self.assertEqual(os.environ[cfg.api_key_env], "private-value")
            self.assertEqual(cfg.model, "course-model")
            self.assertNotIn("private-value", json.dumps(cfg.snapshot()))

    def test_process_overrides_dotenv_and_explicit_json_overrides_both(self):
        self.env.write_text('EVOTASK_MODEL=file-model\nDEEPSEEK_API_KEY=file-secret\nEVOTASK_API_TIMEOUT=100\n', encoding="utf-8")
        config = self.root / "config.json"
        config.write_text('{"model":"explicit-model"}', encoding="utf-8")
        with patch.dict(os.environ, {"MODEL": "process-model", "DEEPSEEK_API_KEY": "process-secret"}, clear=True):
            cfg = Config.load(env_path=self.env)
            self.assertEqual(cfg.model, "process-model")
            self.assertEqual(cfg.api_timeout, 100)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "process-secret")
            self.assertEqual(Config.load(str(config), self.env).model, "explicit-model")

    def test_unrelated_environment_variables_not_injected(self):
        self.env.write_text('PATH=unexpected\nEVOTASK_MODEL=course-model\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            Config.load(env_path=self.env)
            self.assertNotIn("PATH", os.environ)
            self.assertNotIn("EVOTASK_MODEL", os.environ)

    def test_overridden_invalid_numeric_value_does_not_break_precedence(self):
        self.env.write_text('EVOTASK_API_TIMEOUT=invalid\n', encoding='utf-8')
        with patch.dict(os.environ, {'EVOTASK_API_TIMEOUT': '30'}, clear=True):
            self.assertEqual(Config.load(env_path=self.env).api_timeout, 30)

    def test_custom_secret_name_and_hkgai_defaults(self):
        self.env.write_text('EVOTASK_PROVIDER=hkgai\nEVOTASK_BASE_URL=https://course.example.hk\nHKGAI_API_KEY=hkgai-test\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config.load(env_path=self.env)
            self.assertEqual(cfg.api_key_env, "HKGAI_API_KEY")
            self.assertEqual(os.environ[cfg.api_key_env], "hkgai-test")
        self.env.write_text('EVOTASK_API_KEY_ENV=COURSE_KEY\nCOURSE_KEY=custom-test\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config.load(env_path=self.env)
            self.assertEqual(os.environ[cfg.api_key_env], "custom-test")

    def test_invalid_file_and_numeric_errors_hide_values(self):
        for content in ('API_KEY=secret\nAPI_KEY=secret2', 'API_KEY="secret', 'EVOTASK_API_TIMEOUT=secret'):
            self.env.write_text(content, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError) as caught:
                Config.load(env_path=self.env)
            self.assertNotIn("secret", str(caught.exception))

    def test_no_env_and_missing_file_are_supported(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Config.load(env_path=None), Config())
            self.assertEqual(Config.load(env_path=self.env), Config())

    def test_insufficient_balance_stops_with_actionable_sanitized_error(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError('https://api.deepseek.com', 402, 'private body', {}, None)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}, clear=True), patch(
            'evotask.providers.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(ProviderUnavailable) as caught:
                APIProvider(Config(model='course-model')).complete('chat', {})
        self.assertIn('insufficient account balance', str(caught.exception))
        self.assertNotIn('private body', str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
