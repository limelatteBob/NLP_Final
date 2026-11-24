import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from evotask.team_import import parse_team_document
from evotask.retries import visible_runs
from evotask.storage import Store
from evotask.teams import Teams
from tests import test_retry_outputs as retry_tests
from tests.test_teams import ROLES

DOCUMENT = '''# 示例项目团队
## 技术负责人
协调任务，与用户讨论需求。
## 研究员
收集证据并提供资料。
## 软件开发者
实现业务代码。
## 测试工程师
独立测试并反馈问题。
'''


class DocumentTests(unittest.TestCase):
    def test_headings_table_fences_and_role_mapping(self):
        parsed = parse_team_document('团队.md', DOCUMENT)
        self.assertEqual(len(parsed['members']), 4)
        self.assertEqual(parsed['assignments']['coordinator'], 0)
        self.assertEqual(parsed['assignments']['verifier'], 3)
        table = '# 团队\n| 角色 | 职责 |\n| --- | --- |\n| 作者 | 写代码<br>修复问题 |\n| 测试 | 独立检查 |'
        result = parse_team_document('team.markdown', table)
        self.assertEqual(result['members'][0]['instructions'], '写代码\n修复问题')
        self.assertIn('2 位成员', ' '.join(result['warnings']))
        self.assertEqual(sum(v is None for v in result['assignments'].values()), 2)
        fenced = '# Team\n## Technical Lead (lead)\n```text\nKeep this instruction.\n## not a member\n```\n## QA\nTest it.'
        result = parse_team_document('roles.md', fenced)
        self.assertEqual(len(result['members']), 2)
        self.assertIn('## not a member', result['members'][0]['instructions'])
        nested = '# 团队\n## 成员\n### 研究员\n#### 职责\n研究资料。\n### 测试员\n检查结果。'
        self.assertEqual(len(parse_team_document('r.md', nested)['members']), 2)

    def test_teacher_documents(self):
        documents = list(Path('Mid_Test').glob('*/01_role_initialization.md'))
        if not documents:
            self.skipTest('Local teacher dataset is not tracked')
        for document in documents:
            with self.subTest(document=document.parent.name):
                result = parse_team_document(document.name, document.read_text(encoding='utf-8'))
                self.assertEqual(len(result['members']), 4)
                self.assertEqual(len(set(result['assignments'].values())), 4)

    def test_invalid_ambiguous_and_oversized_documents(self):
        for filename, text in [('x.exe', DOCUMENT), ('x.md', 'no roles'), ('x.md', '字' * 65536),
                               ('x.md', '## A\none\n## A\ntwo'), ('x.md', '## A\n' + 'x' * 5001)]:
            with self.subTest(filename=filename, text=text[:20]):
                with self.assertRaises(ValueError):
                    parse_team_document(filename, text)
        many = '# Team\n' + '\n'.join(f'## Member {n}\nDuty {n}.' for n in range(6))
        result = parse_team_document('six.md', many)
        self.assertEqual(len(result['members']), 6)
        self.assertIn('6 位成员', ' '.join(result['warnings']))

    def test_legacy_chain_order_and_team_isolation(self):
        runs = [dict(id='a', created=1, team_id='t'), dict(id='b', created=2),
                dict(id='c', created=3, team_id='t', rerun_of='a'),
                dict(id='d', created=4, team_id='t', retry_of='c'),
                dict(id='foreign', created=5, team_id='other', retry_of='b')]
        self.assertEqual([r['id'] for r in visible_runs(runs)], ['foreign', 'b', 'd'])


class TeamFeatureAPITests(unittest.TestCase):
    setUp = retry_tests.RetryTests.setUp
    tearDown = retry_tests.RetryTests.tearDown
    failed = retry_tests.RetryTests.failed
    retry = retry_tests.RetryTests.retry

    def test_preview_csrf_no_side_effect_and_creation(self):
        payload = {'filename': '团队.md', 'content': DOCUMENT}
        response = self.client.post('/api/teams/preview', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/teams').json(), [])
        self.assertEqual(self.client.post('/api/teams/preview', json=payload, headers={'X-EvoTask-CSRF':'bad'}).status_code, 403)
        self.assertEqual(self.client.post('/api/teams/preview', json={'filename':'x.txt','content':DOCUMENT}).status_code, 422)
        data = response.json()
        roles = {slot:data['members'][index] for slot,index in data['assignments'].items()}
        result = self.client.post('/api/teams', json={'name':data['name'], 'roles':roles})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(len(result.json()['roles']), 4)

    def test_replacement_stable_position_and_original_audit(self):
        first = self.failed(inputs=True)
        other = self.failed('unrelated task')
        with patch.object(self.app.state.jobs, 'submit'):
            result = self.retry(first).json()
            duplicate = self.retry(first, 'second-tab-0000001').json()
        self.assertEqual(result['id'], duplicate['id'])
        self.app.state.jobs.release(self.root)
        listing = self.client.get('/api/workspace').json()['runs']
        self.assertEqual([r['id'] for r in listing], [other['id'], result['id']])
        audit = self.client.get('/api/runs/' + first['id']).json()
        self.assertEqual(audit['status'], 'failed')
        self.assertEqual(audit['replaced_by'], result['id'])
        self.assertFalse(audit['can_retry'])
        with closing(Store(self.root)) as store:
            child = store.load(result['id']); child['status'] = 'failed'; store.save(child)
        with patch.object(self.app.state.jobs, 'submit'):
            next_attempt = self.retry(child, 'next-attempt-00001').json()
            stale = self.retry(first, 'third-tab-00000001').json()
        self.app.state.jobs.release(self.root)
        self.assertEqual(stale['id'], next_attempt['id'])
        self.assertEqual(next_attempt['attempt_number'], 3)
        self.assertEqual(len(self.client.get('/api/workspace').json()['runs']), 2)
        with closing(Store(self.root)) as store:
            self.assertEqual(len(store.runs()), 4)
            self.assertTrue((store.workspace('coordinator', first['id']) / first['initial_inputs'][0]).exists())

    def test_team_rerun_and_actual_conversation_events(self):
        old = self.failed()
        with closing(Store(self.root)) as store:
            team = Teams(store).create('Team', ROLES)
            old.update(team_id=team['id'], team_snapshot=team, status='cancelled')
            store.save(old); store.bind_team(team['id'])
            store.message('group', 'human', 'Team only')
            store.event(old['id'], 'researcher', 'task_assigned', {'task_id':'t1','title':'查找资料','objective':'查找真实证据'})
            store.event(old['id'], 'verifier', 'revision_requested', {'task_id':'t1','reason':'检查不通过'})
        detail = self.client.get('/api/teams/' + team['id']).json()
        self.assertTrue(any(m.get('kind') == 'assignment' and m['recipient']=='researcher' for m in detail['messages']))
        self.assertTrue(any('收到' in m['body'] for m in detail['messages']))
        self.assertTrue(all(m.get('created') for m in detail['messages']))
        self.assertNotIn('Team only', str(self.client.get('/api/chat').json()))
        with patch.object(self.app.state.jobs, 'submit') as submit:
            response = self.client.post('/api/runs/' + old['id'] + '/rerun', json={})
            self.assertEqual(response.status_code, 202, response.text)
            second = self.client.post('/api/runs/' + old['id'] + '/rerun', json={})
            self.assertEqual(response.json()['id'], second.json()['id'])
            self.assertEqual(submit.call_count, 1)
        self.app.state.jobs.release(self.root)
        detail = self.client.get('/api/teams/' + team['id']).json()
        self.assertEqual([r['id'] for r in detail['runs']], [response.json()['id']])
        audit = self.client.get('/api/teams/' + team['id'] + '/export').json()
        self.assertEqual(len(audit['runs']), 2)

    def test_scheduling_failure_replaces_once_and_releases_slot(self):
        source = self.failed()
        with patch.object(self.app.state.jobs.pool, 'submit', side_effect=RuntimeError('Injected scheduling failure')):
            response = self.retry(source)
        self.assertEqual(response.status_code, 503)
        runs = self.client.get('/api/workspace').json()['runs']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['status'], 'failed')
        self.assertNotEqual(runs[0]['id'], source['id'])
        self.assertFalse(self.app.state.jobs.info(self.root)['busy'])


if __name__ == '__main__':
    unittest.main()
