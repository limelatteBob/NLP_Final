from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from evotask.config import Config
from evotask.engine import Engine, task_from_spec, validate_plan
from evotask.evaluation import evaluate
from evotask.gittools import GitPublisher, github_target
from evotask.providers import APIProvider, DemoProvider, ProviderUnavailable
from evotask.retrieval import KnowledgeBase, recall
from evotask.sandbox import DockerSandbox
from evotask.storage import Store, safe_path
from evotask.tools import Tools


ROOT = Path(__file__).resolve().parent.parent


class ScriptProvider:
    def __init__(self, actions, checks=None, role="coder"):
        self.actions, self.role = actions, role
        self.checks = checks or [{"kind": "contains", "path": "result.md", "text": "ready"}]

    def complete(self, purpose, context):
        if purpose == "plan":
            return {"tasks": [{"id": "work", "title": "test work", "objective": "write verified result",
                               "role": self.role, "required_tools": [], "depends_on": [], "checks": self.checks}]}
        if purpose == "bid":
            return {"accept": True, "confidence": 1.0 if context["agent"] == self.role else 0.0, "reason": "test fixture"}
        history = context["task"]["history"]
        index = sum("action" in event for event in history)
        return self.actions[min(index, len(self.actions) - 1)]


WRITE = {"type": "tool", "name": "write_file", "args": {"path": "result.md", "content": "ready"}}
FINISH = {"type": "finish", "summary": "done", "artifacts": ["result.md"]}


class TestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="evotask-test-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.config = Config(provider="demo", model="fixture", max_model_calls=80)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def engine(self, actions=None, checks=None, **kwargs):
        return Engine(self.store, self.config, ScriptProvider(actions or [WRITE, FINISH], checks), **kwargs)


class StorageTests(TestBase):
    def test_reject_escapes_and_windows_special_paths(self):
        for path in ("../secret", "/etc/passwd", "C:\\secrets.txt", "a/../../b", "a:stream", ".git/config",
                     "CON.txt", "x/aux", "a./b", "a//b", "x\x00.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_path(self.root, path)

    def test_symlink_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("private")
        try:
            (self.root / "link.txt").symlink_to(outside)
        except OSError:
            self.skipTest("OS does not permit symlinks in this test environment")
        with self.assertRaises(ValueError):
            safe_path(self.root, "link.txt")

    def test_private_message_visibility(self):
        self.store.message("private:researcher", "human", "private note")
        self.assertEqual(len(self.store.messages("private:researcher", "researcher")), 1)
        with self.assertRaises(PermissionError):
            self.store.messages("private:researcher", "coder")
        self.assertEqual(self.store.messages("group", "coder"), [])

    def test_memory_is_private_and_retrieved_only_when_verified(self):
        self.store.remember("researcher", {"id": "one", "trigger": "cosine", "lesson": "handle empty", "verified": True})
        self.store.remember("researcher", {"id": "two", "trigger": "cosine", "lesson": "unverified", "verified": False})
        self.assertEqual([m["id"] for m in recall(self.store, "researcher", "cosine")], ["one"])
        self.assertEqual(recall(self.store, "coder", "cosine"), [])

    def test_store_lock_prevents_second_writer(self):
        other = Store(self.store.root)
        try:
            with self.store.lock():
                with self.assertRaises(RuntimeError):
                    with other.lock():
                        pass
        finally:
            other.close()


class RetrievalTests(TestBase):
    def test_scope_no_hit_update_and_delete(self):
        path = self.root / "notes.txt"
        path.write_text("quasar uniquely private", encoding="utf-8")
        kb = KnowledgeBase(self.store)
        old = kb.add(path, "researcher")
        self.assertTrue(kb.search("quasar", "researcher"))
        self.assertFalse(kb.search("quasar", "coder"))
        self.assertFalse(kb.search("zebras", "researcher"))
        path.write_text("nebula revised", encoding="utf-8")
        new = kb.add(path, "researcher")
        self.assertEqual(old["id"], new["id"])
        self.assertNotEqual(old["version"], new["version"])
        self.assertFalse(kb.search("quasar", "researcher"))
        kb.remove(new["id"])
        self.assertFalse(kb.search("nebula", "researcher"))

    def test_chinese_retrieval_and_plaintext_constraint(self):
        path = self.root / "notes.md"
        path.write_text("空文本应该返回零值。\n余弦相似度需要处理边界。", encoding="utf-8")
        kb = KnowledgeBase(self.store)
        kb.add(path)
        self.assertTrue(kb.search("空文本", "coder"))
        with self.assertRaises(ValueError):
            kb.add(self.root / "document.pdf")


class PlanTests(unittest.TestCase):
    def spec(self, identifier, dependencies):
        return {"id": identifier, "title": "task", "objective": "work", "role": "coder",
                "depends_on": dependencies, "checks": [{"kind": "exists", "path": "output.md"}]}

    def test_cycle_and_unknown_dependencies_rejected(self):
        for specs in ([self.spec("a", ["b"]), self.spec("b", ["a"])], [self.spec("a", ["missing"])],
                      [self.spec("a", []), self.spec("a", [])]):
            with self.assertRaises(ValueError):
                validate_plan(specs, 10)

    def test_python_check_requires_execution_capability(self):
        spec = self.spec("a", [])
        spec["checks"] = [{"kind": "python", "path": "test_code.py"}]
        self.assertIn("python_run", task_from_spec(spec)["required_tools"])


class EngineTests(TestBase):
    def test_demo_actual_delegation_handoff_and_memory(self):
        KnowledgeBase(self.store).add(ROOT / "examples/knowledge/similarity.md")
        engine = Engine(self.store, self.config, DemoProvider())
        run = engine.advance(engine.new("cosine similarity report"))
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(len(run["tasks"]), 3)
        child = next(t for t in run["tasks"] if t["parent"])
        parent = next(t for t in run["tasks"] if t["id"] == child["parent"])
        self.assertNotEqual(child["owner"], parent["owner"])
        self.assertTrue(parent["inputs"])
        self.assertTrue(all(self.store.memory(a) for a in ("researcher", "coder", "verifier", "coordinator")))
        again = engine.advance(engine.new("cosine similarity report"))
        self.assertTrue(any(t["memory_hits"] for t in again["tasks"]))

    def test_no_memory_ablation_does_not_read_or_write(self):
        self.store.remember("coder", {"id": "seed", "trigger": "write verified result", "lesson": "history", "verified": True})
        engine = self.engine()
        run = engine.advance(engine.new("write result", memory=False))
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["tasks"][0]["memory_hits"], [])
        self.assertEqual(len(self.store.memory("coder")), 1)

    def test_acceptance_approval_survives_reload_and_versions_are_verified(self):
        checks = [{"kind": "contains", "path": "result.md", "text": "ready"}, {"kind": "human", "reason": "accept?"}]
        engine = self.engine(checks=checks)
        run = engine.advance(engine.new("write result"))
        self.assertEqual(run["status"], "waiting_human")
        pending_id = run["pending"]["id"]
        resumed = self.engine(checks=checks).resume(run["id"], pending_id, "yes")
        self.assertEqual(resumed["status"], "succeeded")
        self.assertTrue(all(a["verification"] == "verified" for a in resumed["artifacts"]))
        with self.assertRaises(ValueError):
            engine.resume(run["id"], pending_id, "yes")

    def test_clarification_resume_continues_after_answer(self):
        actions = [{"type": "ask", "question": "Which format?"}, WRITE, FINISH]
        engine = self.engine(actions)
        run = engine.advance(engine.new("write result"))
        self.assertEqual(run["status"], "waiting_human")
        run = engine.resume(run["id"], run["pending"]["id"], "Markdown")
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["tasks"][0]["history"][0]["result"]["human_reply"], "Markdown")

    def test_delete_approval_pins_version_and_archives_original(self):
        actions = [WRITE, {"type": "tool", "name": "delete_file", "args": {"path": "result.md"}}, WRITE, FINISH]
        engine = self.engine(actions)
        run = engine.advance(engine.new("delete and recreate"))
        self.assertTrue(self.store.workspace("coder", run["id"]).joinpath("result.md").exists())
        run = engine.resume(run["id"], run["pending"]["id"], "yes")
        self.assertEqual(run["status"], "succeeded")
        self.assertGreaterEqual(len(run["artifacts"]), 2)
        self.assertEqual(self.store.artifact_bytes(run["artifacts"][0]), b"ready")

    def test_rejecting_approval_prevents_delete(self):
        engine = self.engine([WRITE, {"type": "tool", "name": "delete_file", "args": {"path": "result.md"}}])
        run = engine.advance(engine.new("delete file"))
        run = engine.resume(run["id"], run["pending"]["id"], "no")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.store.workspace("coder", run["id"]).joinpath("result.md").read_text(), "ready")

    def test_approval_does_not_apply_to_changed_file(self):
        engine = self.engine([WRITE, {"type": "tool", "name": "delete_file", "args": {"path": "result.md"}}, FINISH])
        run = engine.advance(engine.new("delete file"))
        path = self.store.workspace("coder", run["id"]) / "result.md"
        path.write_text("ready changed", encoding="utf-8")
        run = engine.resume(run["id"], run["pending"]["id"], "yes")
        self.assertTrue(path.exists())
        failures = [e for e in self.store.events(run["id"]) if e["kind"] == "attempt_failed"]
        self.assertIn("exact file version", failures[0]["data"]["observed_error"])

    def test_failed_acceptance_archives_partial_output(self):
        wrong = {"type": "tool", "name": "write_file", "args": {"path": "result.md", "content": "not complete"}}
        engine = self.engine([wrong, FINISH])
        run = engine.advance(engine.new("write ready"))
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["artifacts"])
        self.assertTrue(any(a["verification"] == "invalid" for a in run["artifacts"]))
        self.assertTrue((self.store.root / "runs" / run["id"] / "failure.md").exists())
        self.assertEqual(engine.metrics(run)["subtask_completion_rate"], 0)

    def test_interrupted_tool_not_replayed(self):
        engine = self.engine()
        run = engine.new("write result")
        run["tasks"] = validate_plan(ScriptProvider([]).complete("plan", {})["tasks"], 10)
        run["status"] = "running"
        task = run["tasks"][0]
        task["owner"], task["status"] = "coder", "running"
        run["inflight"] = {"task_id": "work", "action": {"type": "tool", "name": "git_publish"}}
        self.store.save(run)
        run = engine.advance(self.store.load(run["id"]))
        self.assertEqual(run["status"], "failed")
        self.assertIn("Not replayed", run["tasks"][0]["summary"])
        self.assertEqual(run["model_calls"], 0)

    def test_model_budget_is_enforced(self):
        self.config = replace(self.config, max_model_calls=1)
        engine = self.engine()
        run = engine.advance(engine.new("budget"))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["model_calls"], 1)

    def test_account_error_stops_whole_run_without_repeated_bids(self):
        engine = self.engine()
        plan = engine.provider.complete("plan", {})
        with patch.object(engine.provider, "complete", side_effect=[plan, ProviderUnavailable("HTTP 402")]) as model:
            run = engine.advance(engine.new("account unavailable"))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(run["model_calls"], 2)

    def test_keyboard_interrupt_preserves_inflight_journal(self):
        engine = self.engine()
        run = engine.new("interrupt test")
        with patch.object(engine.tools, "execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.advance(run)
        restored = self.store.load(run["id"])
        self.assertIsNotNone(restored["inflight"])
        engine.advance(restored)
        self.assertEqual(restored["status"], "failed")

    def test_invented_retrieval_citation_is_rejected(self):
        invented = {"type": "tool", "name": "write_file", "args": {
            "path": "result.md", "content": "ready [doc_aaaaaaaaaaaa:bbbbbbbb:0]"}}
        engine = self.engine([invented, FINISH])
        run = engine.advance(engine.new("check source"))
        self.assertEqual(run["status"], "failed")
        self.assertIn("not retrieved", run["tasks"][0]["summary"])

    def test_resume_rejects_changed_config(self):
        engine = self.engine([{"type": "ask", "question": "clarify"}, WRITE, FINISH])
        run = engine.advance(engine.new("result"))
        changed = Engine(self.store, replace(self.config, max_tasks=99), ScriptProvider([WRITE, FINISH]))
        with self.assertRaises(ValueError):
            changed.resume(run["id"], run["pending"]["id"], "answer")


class ToolTests(TestBase):
    def test_crud_and_tool_permissions(self):
        engine = self.engine()
        run = engine.new("file ops")
        task = {"id": "work", "owner": "coder"}
        tools = Tools(self.store, self.config)
        tools.execute(run, task, "write_file", {"path": "x.txt", "content": "one"})
        tools.execute(run, task, "update_file", {"path": "x.txt", "old": "one", "new": "two"})
        self.assertEqual(tools.execute(run, task, "read_file", {"path": "x.txt"})["content"], "two")
        with self.assertRaises(PermissionError):
            tools.execute(run, task, "delete_file", {"path": "x.txt"})
        with self.assertRaises(PermissionError):
            tools.execute(run, {"id": "research", "owner": "researcher"}, "python_run", {"path": "x.py"})
        with self.assertRaises(PermissionError):
            tools.execute(run, task, "write_file", {"path": "inputs/other.txt", "content": "overwrite"})

    def test_sandbox_unavailable_never_executes_on_host(self):
        path = self.root / "code.py"
        path.write_text("raise Exception('must not run')")
        sandbox = DockerSandbox(self.config)
        with patch.object(sandbox, "doctor", return_value={"available": False, "reason": "not running"}), patch("subprocess.Popen") as popen:
            with self.assertRaises(RuntimeError):
                sandbox.execute(self.root, "code.py")
            popen.assert_not_called()

    def test_container_command_limits_and_only_own_workspace(self):
        sandbox = DockerSandbox(self.config)
        (self.root / "inputs").mkdir()
        command = sandbox.command(self.root, "code.py", "evotask-test")
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--pull=never", "--pids-limit=64"):
            self.assertIn(flag, command)
        self.assertFalse(any("docker.sock" in x for x in command))
        self.assertTrue(any("target=/work/inputs,readonly" in x for x in command))

    def test_git_target_restrictions(self):
        github_target("https://github.com/example/project.git", "evotask/test")
        for remote, branch in (("https://token@github.com/example/project", "evotask/x"),
                               ("file:///somewhere", "evotask/x"),
                               ("https://github.com/example/project", "main")):
            with self.assertRaises(ValueError):
                github_target(remote, branch)

    def test_real_local_git_commit_uses_only_controller_snapshot(self):
        # No remote or external message. Verify actual Git plumbing in a temporary repository.
        publisher = GitPublisher(self.store, self.config)
        repo = self.root / "git-fixture"
        repo.mkdir()
        try:
            publisher._git(repo, "init")
        except FileNotFoundError:
            self.skipTest("git not installed")
        (repo / "report.md").write_text("validated snapshot", encoding="utf-8")
        publisher._git(repo, "add", "--", "report.md")
        publisher._git(repo, "commit", "-m", "test snapshot")
        sha = publisher._git(repo, "rev-parse", "HEAD").stdout.strip()
        self.assertRegex(sha, r"^[0-9a-f]{40,64}$")


class ProviderAndEvaluationTests(TestBase):
    def test_forbidden_provider_and_endpoint(self):
        with self.assertRaises(ValueError):
            Config(provider="openai").validate()
        with self.assertRaises(ValueError):
            Config(base_url="https://api.openai.com").validate()
        with self.assertRaises(ValueError):
            Config(provider="hkgai", base_url="https://api.anthropic.com").validate()

    def test_missing_key_does_not_silently_switch_to_demo(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            APIProvider(Config(model="course-model"))

    def test_api_request_and_structured_response_without_real_network(self):
        cfg = Config(model="course-model")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"choices": [{"finish_reason": "stop",
            "message": {"content": '{"reply":"hello"}'}}]}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-secret"}), patch(
            "evotask.providers.urllib.request.build_opener", return_value=opener):
            result = APIProvider(cfg).complete("chat", {"agent": "researcher"})
        self.assertEqual(result, {"reply": "hello"})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "course-model")
        context = json.loads(body['messages'][1]['content'])['context']
        self.assertEqual(context['response_token_limit'], body['max_tokens'])
        self.assertNotIn("test-secret", request.data.decode())

    def test_truncated_response_is_not_accepted_as_an_action(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'choices': [{'finish_reason': 'length',
            'message': {'content': '{"type":"finish","artifacts":["partial.html"]}'}}]}).encode()
        opener = MagicMock(); opener.open.return_value = response
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-secret'}), patch(
                'evotask.providers.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, 'no action executed'):
                APIProvider(Config(model='course-model')).complete('act', {})

    def test_api_errors_do_not_expose_secret_response_body(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.deepseek.com", 401, "secret-detail", {}, None)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-secret"}), patch(
            "evotask.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(RuntimeError) as raised:
                APIProvider(Config(model="course-model")).complete("chat", {})
        self.assertIn("401", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_batch_demo_has_external_checks_and_explicit_label(self):
        report = evaluate(self.root / "eval", ROOT / "benchmarks/demo.jsonl", self.config, progress=lambda _: None)
        result = json.loads(report.with_name("results.json").read_text(encoding="utf-8"))
        self.assertEqual(result["summary"]["mode"], "demo")
        self.assertEqual(result["summary"]["task_completion_rate"], 1)
        self.assertTrue(all(c["passed"] for c in result["cases"][0]["checks"]))


if __name__ == "__main__":
    unittest.main()
