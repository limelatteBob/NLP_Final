from __future__ import annotations

import copy
from unittest.mock import patch

from evotask.collaboration import ActionProtocolError, normalize_action
from evotask.engine import Engine
from tests.test_core import ScriptProvider, TestBase


class SpeechProvider:
    """Reproduce the recorded K-pop write_file decision without an API call."""
    def complete(self, purpose, context):
        if purpose == 'plan':
            return {'tasks': [
                {'id': 't1', 'title': '提取要点', 'objective': '提取开发计划', 'role': 'coordinator',
                 'depends_on': [], 'checks': [{'kind': 'exists', 'path': 'notes.md'}]},
                {'id': 't2', 'title': '撰写演讲稿', 'objective': '基于要点撰写 K-pop 演讲稿',
                 'role': 'coordinator', 'depends_on': ['t1'], 'checks': [
                     {'kind': 'exists', 'path': 'speech_draft.md'},
                     {'kind': 'contains', 'path': 'speech_draft.md', 'text': 'K-pop'},
                     {'kind': 'human', 'reason': '检查事实与口头表达'}]},
                {'id': 't3', 'title': '交付', 'objective': '完成交付记录', 'role': 'coordinator',
                 'depends_on': ['t2'], 'checks': [{'kind': 'exists', 'path': 'delivery.md'}]},
            ]}
        if purpose == 'bid':
            return {'accept': True, 'confidence': 1 if context['agent'] == 'coordinator' else 0, 'reason': 'fixture'}
        task = context['task']
        path = {'t1': 'notes.md', 't2': 'speech_draft.md', 't3': 'delivery.md'}[task['id']]
        written = any(h.get('action', {}).get('name') == 'write_file'
                      and h.get('result', {}).get('success', True) for h in task['history'])
        if written:
            return {'type': 'finish', 'summary': 'ready for review', 'artifacts': [path]}
        if task['id'] == 't2':
            return {'type': 'write_file', 'path': path, 'content': '# K-pop 项目汇报\n测试稿：请人工审阅。'}
        return {'type': 'tool', 'name': 'write_file', 'args': {'path': path, 'content': 'K-pop 开发计划（测试资料）'}}


def legacy_normalize(engine, run, task, action):
    if action.get('type') == 'write_file':
        raise ValueError('Invalid agent action type')
    return action


class ActionProtocolTests(TestBase):
    def test_flat_and_nested_tool_decisions_preserve_text(self):
        text = '# K-pop\n中文与 "引号"\n'
        canonical = {'type': 'tool', 'name': 'write_file', 'args': {'path': 'speech_draft.md', 'content': text}}
        for raw in [canonical, {'type': 'write_file', **canonical['args']},
                    {'type': 'write_file', 'args': canonical['args']},
                    {'purpose': 'act', 'action': {'type': 'write_file', **canonical['args']}}]:
            original = copy.deepcopy(raw)
            self.assertEqual(normalize_action(raw), canonical)
            self.assertEqual(raw, original)

    def test_ambiguous_unknown_and_malformed_actions_are_rejected(self):
        for raw in [[], {}, {'type': []}, {'type': 'shell', 'command': 'echo nope'},
                    {'type': 'write_file', 'path': 'x.md'},
                    {'type': 'write_file', 'args': {'path': 'x.md', 'content': 'x'}, 'path': 'y.md'},
                    {'type': 'write_file', 'path': 'x.md', 'content': 'x', 'name': 'delete_file'},
                    {'type': 'tool', 'name': 'list_files', 'args': {}, 'action': {'type': 'fail'}}]:
            with self.subTest(raw=raw), self.assertRaisesRegex(ActionProtocolError, 'Return exactly one'):
                normalize_action(raw)

    def test_speech_written_and_human_review_is_still_required(self):
        engine = Engine(self.store, self.config, SpeechProvider())
        run = engine.advance(engine.new('K-pop 项目汇报'))
        self.assertEqual(run['status'], 'waiting_human', run['failure'])
        self.assertEqual(run['pending']['task_id'], 't2')
        self.assertEqual(run['tasks'][1]['error_streak'], 0)
        path = self.store.workspace('coordinator', run['id']) / 'speech_draft.md'
        self.assertIn('K-pop', path.read_text(encoding='utf-8'))
        self.assertTrue(any(e['kind'] == 'action_normalized' for e in self.store.events(run['id'])))
        self.assertEqual(engine.resume(run['id'], run['pending']['id'], 'yes')['status'], 'succeeded')

    def failed_speech(self):
        engine = Engine(self.store, self.config, SpeechProvider())
        with patch.object(Engine, 'normalize_decision', legacy_normalize):
            run = engine.advance(engine.new('K-pop 项目汇报'))
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['tasks'][1]['error_streak'], 3)
        return engine, self.store.load(run['id'])

    def test_legacy_retry_preserves_upstream_history_checks_and_budgets(self):
        engine, run = self.failed_speech()
        upstream = copy.deepcopy(run['tasks'][0])
        history = copy.deepcopy(run['tasks'][1]['history'])
        checks = copy.deepcopy(run['tasks'][1]['checks'])
        counters = {k: run[k] for k in ('steps', 'model_calls', 'active_seconds')}
        task_steps = run['tasks'][1]['steps']
        engine.retry_actions(run)
        self.assertEqual(run['tasks'][0], upstream)
        self.assertEqual(run['tasks'][1]['history'][:-1], history)
        self.assertEqual(run['tasks'][1]['steps'], task_steps)
        self.assertEqual(run['tasks'][1]['checks'], checks)
        self.assertEqual(run['tasks'][2]['status'], 'pending')
        self.assertEqual({k: run[k] for k in counters}, counters)
        with self.assertRaises(ValueError):
            engine.retry_actions(run)
        engine.advance(run)
        self.assertEqual(run['status'], 'waiting_human', run['failure'])
        self.assertEqual(run['tasks'][0], upstream)
        self.assertEqual(engine.resume(run['id'], run['pending']['id'], 'yes')['status'], 'succeeded')

    def test_retry_refuses_budget_exhaustion_unsafe_replies_and_unrelated_failure(self):
        engine, run = self.failed_speech()
        for changes in [{'model_calls': self.config.max_model_calls}, {'steps': self.config.max_steps},
                        {'active_seconds': self.config.active_timeout}, {'action_retry_count': 2},
                        {'inflight': {'task_id': 't2'}}, {'failure': 'API unavailable'}, {'status': 'cancelled'}]:
            candidate = copy.deepcopy(run); candidate.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                engine.retry_actions(candidate)
        candidate = copy.deepcopy(run)
        candidate['tasks'][1]['history'][-1]['action'] = {'type': 'python_run', 'path': 'x.py'}
        self.assertEqual(engine.retryable_action_tasks(candidate), [])  # Coordinator has no Python permission.
        candidate['tasks'][1]['history'][-1]['result']['observed_error'] = 'HTTP failure'
        self.assertEqual(engine.retryable_action_tasks(candidate), [])

    def test_shorthand_retains_permissions_paths_and_deletion_approval(self):
        engine, run = self.failed_speech()
        task = run['tasks'][1]
        for action, error in [({'type': 'python_run', 'path': 'x.py'}, PermissionError),
                              ({'type': 'write_file', 'path': '../x.md', 'content': 'no'}, ValueError)]:
            with self.subTest(action=action), self.assertRaises(error):
                engine.action(run, task, action)
        engine.action(run, task, {'type': 'write_file', 'path': 'draft.md', 'content': 'keep'})
        engine.action(run, task, {'type': 'delete_file', 'path': 'draft.md'})
        self.assertEqual(run['pending']['kind'], 'tool_approval')
        self.assertTrue((self.store.workspace(task['owner'], run['id']) / 'draft.md').exists())

    def test_format_errors_are_bounded_and_keep_raw_reply_with_guidance(self):
        raw = {'type': 'unsupported_action'}
        engine = Engine(self.store, self.config, ScriptProvider([raw]))
        run = engine.advance(engine.new('test invalid replies'))
        self.assertEqual(run['status'], 'failed')
        history = run['tasks'][0]['history']
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-1]['action'], raw)
        self.assertIn('"type":"tool"', history[-1]['result']['observed_error'])
