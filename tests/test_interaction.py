"""Regression tests for concise HITL, task-directed delivery and team private channels."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from evotask.conversations import team_messages
from evotask.engine import Engine, validate_plan
from evotask.interaction import clarification, task_locale
from evotask.providers import INSTRUCTIONS
from evotask.teams import Teams
from tests.test_core import TestBase, WRITE, FINISH
from tests.test_teams import ROLES
from tests import test_web as web_tests


class EnglishDeliverableProvider:
    def __init__(self):
        self.contexts = []

    def complete(self, purpose, context):
        self.contexts.append((purpose, copy.deepcopy(context)))
        if purpose == 'plan':
            if not context['clarifications']:
                return {'question': 'Who will read this introduction?', 'options': ['General users (recommended)', 'Developers']}
            return {'requirements':[{'id':'R1','text':'Project introduction','priority':'must'}], 'tasks': [{'id': 'write', 'role': 'coder', 'title': 'Write the introduction', 'objective': 'Write a brief English introduction',
                              'outputs':[{'path':'intro.md','title':'Project introduction','kind':'deliverable','order':1}],
                              'checks': [{'kind': 'contains', 'path': 'intro.md', 'text': 'Project Introduction','requirement_ids':['R1']}]}]}
        if purpose == 'bid':
            return {'accept': True, 'confidence': 1 if context['agent'] == 'coder' else 0, 'reason': 'Can write the introduction.'}
        if not context['task']['history']:
            return {'type': 'tool', 'name': 'write_file', 'args': {'path': 'intro.md', 'content': '# Project Introduction\nThis project helps readers organize their research.'}}
        return {'type': 'finish', 'artifacts': ['intro.md'], 'summary': 'The introduction is ready.'}


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
            {'question': '问' * 101},
            {'question': '选择哪种格式？', 'options': ['中文']},
            {'question': '选择哪种格式？', 'options': ['中文', '中文']},
            {'question': '选择哪种格式？', 'options': ['甲', '乙', '丙', '丁']},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                clarification(value)
        self.assertEqual(clarification({'question': '资料的来源网址是什么？'}), ('资料的来源网址是什么？', []))
        self.assertEqual(clarification({'question': 'Which audience?'}), ('Which audience?', []))

    def test_english_task_uses_english_for_clarification_events_and_delivery(self):
        provider = EnglishDeliverableProvider()
        engine = Engine(self.store, replace(self.config, provider='deepseek'), provider)
        team = Teams(self.store).create('English roles', ROLES)
        goal = 'Write an introduction to this project.'
        run = engine.advance(engine.new(goal, team_id=team['id'], require_review=True))
        self.assertEqual(run['status'], 'waiting_human')
        self.assertEqual(run['steps'], 0)
        self.assertEqual(run['pending']['options'], ['General users (recommended)', 'Developers'])
        pending = self.store.load(run['id'])['pending']
        self.assertEqual(pending, run['pending'])
        self.assertIn('General users (recommended)', self.store.messages('group')[-1]['body'])
        # Only the human's actual selection resumes work; recommendation does not approve anything.
        run = engine.resume(run['id'], pending['id'], pending['options'][0])
        self.assertEqual(run['clarifications'][0]['answer'], 'General users (recommended)')
        self.assertEqual(run['pending']['kind'], 'acceptance')
        self.assertIn('Accept this delivery?', run['pending']['question'])
        run = engine.resume(run['id'], run['pending']['id'], 'yes')
        self.assertEqual(run['status'], 'succeeded', run.get('failure'))
        self.assertIn('Project Introduction', self.store.artifact_bytes(run['artifacts'][0]).decode())
        feed = team_messages(self.store, team_id=team['id'])
        self.assertTrue(any(msg['body'].startswith('Please take on') for msg in feed))
        self.assertTrue(any(msg['body'].startswith('Task completed.') for msg in feed))
        for msg in feed:
            self.assertNotRegex(msg['body'], r'[\u3400-\u9fff]')
        for purpose, context in provider.contexts:
            self.assertEqual(context['task_goal'], goal)
            for slot, role in ROLES.items():
                self.assertEqual(context['team']['roles'][slot]['name'], role['name'])
                self.assertEqual(context['team']['roles'][slot]['instructions'], role['instructions'])
        with self.assertRaisesRegex(ValueError, 'matching pending'):
            engine.resume(run['id'], pending['id'], '技术人员')

    def test_bad_planner_question_is_repaired_before_user_sees_it(self):
        provider = EnglishDeliverableProvider()
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

    def test_finish_preserves_deliverables_in_requested_languages(self):
        engine = Engine(self.store, replace(self.config, provider='deepseek'), None)
        for name, language, content in [
            ('intro.md', '简体中文', '# 项目介绍\n使用 Python 整理资料。\n> Original source quotation.'),
            ('intro.txt', '法文', 'Bienvenue dans le guide de démarrage.'),
            ('slides.html', '英文', '<!-- Slide navigation --><h1>Project report</h1><button>Next</button>'),
            ('booking.py', '英文', '# Keep the original record ID.\nprint("Booking confirmed")'),
            ('guide.md', '中英双语', '# 安装指南 / Setup guide\n请先安装 Python。\nInstall Python first.'),
        ]:
            with self.subTest(name=name, language=language):
                run = engine.new(f'用{language}交付 {name}')
                run['tasks'] = validate_plan([{'id': 'write', 'title': '介绍', 'objective': '介绍', 'role': 'coder',
                                              'checks': [{'kind': 'contains', 'path': name, 'text': content}]}], 6)
                task = run['tasks'][0]
                task.update(owner='coder', status='running')
                target = self.store.workspace('coder', run['id']) / name
                target.write_bytes(content.encode('utf-8'))
                engine.action(run, task, {'type': 'finish', 'summary': '已完成介绍。', 'artifacts': [name]})
                self.assertEqual(task['status'], 'succeeded')
                self.assertEqual(self.store.artifact_bytes(run['artifacts'][0]), content.encode('utf-8'))

    def test_language_policy_distinguishes_interaction_from_delivery(self):
        instructions = ' '.join(INSTRUCTIONS.split())
        self.assertIn('language of task_goal', instructions)
        self.assertIn('code comments, docstrings, UI labels, user-facing strings', instructions)
        self.assertIn('Preserve proper names, URLs, code/API identifiers, data keys', instructions)
        self.assertEqual(task_locale('用 Python 实现测试工具'), 'zh')
        self.assertEqual(task_locale('Build the dashboard.\n> 来源：原始中文段落不翻译。'), 'en')


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

    def test_chat_keeps_english_reply_and_still_repairs_multiple_questions(self):
        # create_app captures config, so use a dedicated app with live policy and a controlled provider.
        from evotask.web.app import create_app
        from tests.test_web import TestClient
        from pathlib import Path
        live_app = create_app(replace(self.config, provider='deepseek'), Path(self.temp.name) / 'live-policy')
        with TestClient(live_app) as client, patch('evotask.web.app.make_provider') as factory:
            self.bootstrap(client)
            factory.return_value.complete.side_effect = [{'reply': 'Which task? Which files?'}, {'reply': 'Hello, what do you need?'}]
            response = client.post('/api/chat?mode=live', json={'channel': 'group', 'message': 'Reply in English.'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(factory.return_value.complete.call_count, 2)
            messages = client.get('/api/chat?mode=live').json()
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]['body'], 'Reply in English.')
            self.assertEqual(messages[1]['body'], 'Hello, what do you need?')
