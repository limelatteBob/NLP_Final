"""Regression tests for concise HITL, Chinese delivery and team private channels."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from evotask.engine import Engine, validate_plan
from evotask.interaction import clarification, chinese_prose, validate_deliverable
from evotask.providers import INSTRUCTIONS
from evotask.teams import Teams
from tests.test_core import TestBase, WRITE, FINISH
from tests.test_teams import ROLES
from tests import test_web as web_tests


class ChineseProvider:
    def __init__(self):
        self.contexts = []

    def complete(self, purpose, context):
        self.contexts.append((purpose, copy.deepcopy(context)))
        if purpose == 'plan':
            if not context['clarifications']:
                return {'question': '这份介绍面向哪类读者？', 'options': ['普通用户（推荐）', '技术人员']}
            return {'tasks': [{'id': 'write', 'role': 'coder', 'title': '撰写介绍', 'objective': '编写简短的中文介绍',
                              'checks': [{'kind': 'contains', 'path': 'intro.md', 'text': '项目介绍'}]}]}
        if purpose == 'bid':
            return {'accept': True, 'confidence': 1 if context['agent'] == 'coder' else 0, 'reason': '具备文档编写能力。'}
        if not context['task']['history']:
            return {'type': 'tool', 'name': 'write_file', 'args': {'path': 'intro.md', 'content': '# 项目介绍\n帮助普通用户完成资料整理。'}}
        return {'type': 'finish', 'artifacts': ['intro.md'], 'summary': '已完成中文项目介绍。'}


class InteractionTests(TestBase):
    def test_answer_is_shared_with_later_tasks_and_not_requested_again(self):
        engine = self.engine([{'type': 'ask', 'question': '需要哪种格式？'}, WRITE, FINISH])
        run = engine.advance(engine.new('test shared answer'))
        run = engine.resume(run['id'], run['pending']['id'], 'Markdown')
        self.assertEqual(run['status'], 'succeeded')
        self.assertEqual(run['clarifications'][0]['answer'], 'Markdown')
        self.assertEqual(run['clarifications'][0]['task_id'], 'work')
        with self.assertRaisesRegex(ValueError, '已得到回答'):
            engine.wait(run, None, 'planning', '需要哪种格式？')

    def test_short_single_question_and_choices_contract(self):
        for value in [
            {'question': '目标是什么？受众是谁？'}, {'question': '请说明：\n1. 时间\n2. 范围'},
            {'question': '问' * 101}, {'question': 'Which audience?'},
            {'question': '选择哪种格式？', 'options': ['中文']},
            {'question': '选择哪种格式？', 'options': ['中文', '中文']},
            {'question': '选择哪种格式？', 'options': ['甲', '乙', '丙', '丁']},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                clarification(value)
        self.assertEqual(clarification({'question': '资料的来源网址是什么？'}), ('资料的来源网址是什么？', []))

    def test_foreign_role_and_goal_still_get_chinese_and_explicit_answer(self):
        provider = ChineseProvider()
        engine = Engine(self.store, replace(self.config, provider='deepseek'), provider)
        team = Teams(self.store).create('English roles', ROLES)
        run = engine.advance(engine.new('Write an English introduction', team_id=team['id']))
        self.assertEqual(run['status'], 'waiting_human')
        self.assertEqual(run['steps'], 0)
        self.assertEqual(run['pending']['options'], ['普通用户（推荐）', '技术人员'])
        pending = self.store.load(run['id'])['pending']
        self.assertEqual(pending, run['pending'])
        self.assertIn('普通用户（推荐）', self.store.messages('group')[-1]['body'])
        # Only the human's actual selection resumes work; recommendation does not approve anything.
        run = engine.resume(run['id'], pending['id'], pending['options'][0])
        self.assertEqual(run['status'], 'succeeded', run.get('failure'))
        self.assertEqual(run['clarifications'][0]['answer'], '普通用户（推荐）')
        self.assertIn('项目介绍', self.store.artifact_bytes(run['artifacts'][0]).decode())
        for purpose, context in provider.contexts:
            self.assertIn('Simplified Chinese', context['interaction_policy'])
            for slot, role in ROLES.items():
                self.assertEqual(context['team']['roles'][slot]['name'], role['name'])
                self.assertEqual(context['team']['roles'][slot]['instructions'], role['instructions'])
        with self.assertRaisesRegex(ValueError, 'matching pending'):
            engine.resume(run['id'], pending['id'], '技术人员')

    def test_bad_planner_question_is_repaired_before_user_sees_it(self):
        provider = ChineseProvider()
        original = provider.complete
        def respond(purpose, context):
            if purpose == 'plan' and not context.get('validation_error'):
                return {'question': 'What is your budget? What is your deadline?'}
            return original(purpose, context)
        with patch.object(provider, 'complete', side_effect=respond):
            engine = Engine(self.store, replace(self.config, provider='deepseek'), provider)
            run = engine.advance(engine.new('Write a report'))
        self.assertEqual(run['model_calls'], 2)
        self.assertEqual(run['status'], 'waiting_human')
        self.assertNotIn('What', run['pending']['question'])
        self.assertEqual(sum(e['kind'] == 'plan_rejected' for e in self.store.events(run['id'])), 1)

    def test_foreign_deliverable_cannot_be_submitted_but_can_be_repaired(self):
        engine = Engine(self.store, replace(self.config, provider='deepseek'), ChineseProvider())
        run = engine.new('Introduction')
        run['tasks'] = validate_plan([{'id': 'write', 'title': '介绍', 'objective': '介绍', 'role': 'coder',
                                      'checks': [{'kind': 'exists', 'path': 'intro.md'}]}], 6)
        task = run['tasks'][0]
        task.update(owner='coder', status='running')
        root = self.store.workspace('coder', run['id'])
        target = root / 'intro.md'
        target.write_text('# Introduction\nThis project helps users manage their research.', encoding='utf-8')
        action = {'type': 'finish', 'summary': '已完成介绍。', 'artifacts': ['intro.md']}
        with self.assertRaisesRegex(ValueError, '简体中文'):
            engine.action(run, task, action)
        self.assertEqual(run['artifacts'], [])
        target.write_text('# 项目介绍\n使用 Python 整理资料，参见 https://example.com。', encoding='utf-8')
        engine.action(run, task, action)
        self.assertEqual(task['status'], 'succeeded')

    def test_html_code_urls_and_source_quotes(self):
        target = self.root / 'slides.html'
        target.write_text('<script>const label="中文";</script><h1>Project report</h1>', encoding='utf-8')
        with self.assertRaises(ValueError):
            validate_deliverable(target)
        target.write_text('<style>.title {color:red}</style><h1>项目汇报</h1><p>支持 Python 与 RAG。</p>', encoding='utf-8')
        validate_deliverable(target)
        chinese_prose('## 说明\n以下为来源原文：\n> Original source quotation.\n```python\nprint("hello")\n```', '说明')
        code = self.root / 'test.py'
        code.write_text('assert 1 + 1 == 2', encoding='utf-8')
        validate_deliverable(code)
        self.assertIn('overrides role descriptions and input language', INSTRUCTIONS)
        self.assertNotIn('answer to the human in their language', INSTRUCTIONS)


# Reuse the HTTP harness without inheriting and re-running its entire test suite.
@unittest.skipIf(web_tests.TestClient is None, 'Install Web test dependencies')
class InteractionWebTests(TestBase):
    setUp = web_tests.WebTests.setUp
    tearDown = web_tests.WebTests.tearDown
    bootstrap = web_tests.WebTests.bootstrap

    def test_team_private_history_never_enters_group_or_other_agent_context(self):
        team = self.client.post('/api/teams', json={'name': '团队', 'roles': ROLES}).json()
        contexts = []
        def reply(purpose, context):
            contexts.append(context)
            return {'reply': '已收到你的消息。'}
        with patch('evotask.web.app.make_provider') as factory:
            factory.return_value.complete.side_effect = reply
            for channel, message in [('coder', 'PRIVATE_CODER_SECRET'), ('researcher', '研究问题'), ('group', '共同讨论')]:
                response = self.client.post('/api/chat', json={'team_id': team['id'], 'channel': channel, 'message': message})
                self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('PRIVATE_CODER_SECRET', str(contexts[0]['history']))
        self.assertNotIn('PRIVATE_CODER_SECRET', str(contexts[1:]))
        for channel in ('group', 'coder', 'researcher'):
            data = self.client.get(f"/api/teams/{team['id']}?channel={channel}").json()
            self.assertEqual(data['channel'], channel)
            self.assertEqual('PRIVATE_CODER_SECRET' in str(data['messages']), channel == 'coder')

    def test_chat_repairs_foreign_reply_once_and_preserves_human_input(self):
        # create_app captures config, so use a dedicated app with live policy and a controlled provider.
        from evotask.web.app import create_app
        from tests.test_web import TestClient
        from pathlib import Path
        live_app = create_app(replace(self.config, provider='deepseek'), Path(self.temp.name) / 'live-policy')
        with TestClient(live_app) as client, patch('evotask.web.app.make_provider') as factory:
            self.bootstrap(client)
            factory.return_value.complete.side_effect = [{'reply': 'Hello, what do you need?'}, {'reply': '你好，需要我协助哪项任务？'}]
            response = client.post('/api/chat?mode=live', json={'channel': 'group', 'message': 'Reply in English.'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(factory.return_value.complete.call_count, 2)
            messages = client.get('/api/chat?mode=live').json()
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]['body'], 'Reply in English.')
            self.assertIn('你好', messages[1]['body'])
