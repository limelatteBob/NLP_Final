"""Real local commit/push plumbing; replace only the GitHub transport destination."""
from dataclasses import replace
from unittest.mock import patch

from evotask.engine import Engine, task_from_spec
from evotask.providers import DemoProvider
from tests.test_core import TestBase


class GitWorkflowTests(TestBase):
    def test_publish_requires_approval_and_pushes_only_pinned_snapshot(self):
        config = replace(self.config, github_remote='https://github.com/example/evotask-test.git')
        engine = Engine(self.store, config, DemoProvider())
        run = engine.new('发布已确认的文档')
        task = task_from_spec({'id': 'publish', 'title': '发布', 'objective': '发布', 'role': 'coder',
                               'checks': [{'kind': 'exists', 'path': 'guide.md'}]})
        task.update(owner='coder', status='running')
        run['tasks'] = [task]
        run['status'] = 'running'
        target = self.store.workspace('coder', run['id']) / 'guide.md'
        target.write_text('经确认的中文说明', encoding='utf-8')
        action = {'type': 'tool', 'name': 'git_publish', 'args': {'paths': ['guide.md'], 'branch': 'evotask/test'}}
        publisher = engine.tools.git
        original = publisher._git
        with patch.object(publisher, '_git', wraps=original) as git:
            engine.action(run, task, action)
            git.assert_not_called()
        self.assertEqual(run['pending']['kind'], 'tool_approval')
        prepared = run['pending']['prepared']
        # A later workspace edit must not alter the version the human approved.
        target.write_text('尚未确认的新版本', encoding='utf-8')
        remote = self.root / 'local-remote.git'
        remote.mkdir()
        original(remote, 'init', '--bare')
        calls = []
        def local_transport(repo, *args, **kwargs):
            calls.append(args)
            mapped = tuple(str(remote) if a == config.github_remote else a for a in args)
            return original(repo, *mapped, **kwargs)
        with patch.object(publisher, '_git', side_effect=local_transport):
            engine.action(run, task, action, approved=True, prepared=prepared)
        self.assertTrue(any(args[0] == 'commit' for args in calls))
        self.assertTrue(any(args[0] == 'push' for args in calls))
        self.assertTrue(task['history'][-1]['result']['remote_verified'])
        content = original(remote, 'show', 'refs/heads/evotask/test:guide.md').stdout.strip()
        self.assertEqual(content, '经确认的中文说明')
        self.assertEqual(target.read_text(encoding='utf-8'), '尚未确认的新版本')
