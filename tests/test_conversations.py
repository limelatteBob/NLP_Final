import json
import tempfile
import unittest
from pathlib import Path

from evotask.config import Config
from evotask.conversations import team_messages
from evotask.engine import Engine
from evotask.outputs import describe_artifacts
from evotask.providers import DemoProvider
from evotask.retrieval import KnowledgeBase
from evotask.storage import Store

try:
    from fastapi.testclient import TestClient
    from evotask.web.app import create_app
except ImportError:
    TestClient = None


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.config = Config(provider='demo', model='fixture')
        KnowledgeBase(self.store).add(Path('evotask/web/data/similarity.md'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def completed(self, goal='协作测试'):
        engine = Engine(self.store, self.config, DemoProvider())
        return engine.advance(engine.new(goal, memory=False))

    def test_actual_assignment_acknowledgement_handoff_and_completion(self):
        run = self.completed()
        self.assertEqual(run['status'], 'succeeded')
        raw_before = self.store.messages('group', limit=100)
        feed = team_messages(self.store)
        assignments = [m for m in feed if m['kind'] == 'assignment']
        self.assertEqual(len(assignments), len(run['tasks']))
        for assignment in assignments:
            self.assertEqual(assignment['sender'], 'coordinator')
            self.assertIn(assignment['recipient'], ('coder', 'researcher', 'verifier', 'coordinator'))
            ack = feed[feed.index(assignment) + 1]
            self.assertEqual(ack['kind'], 'acknowledgement')
            self.assertEqual(ack['sender'], assignment['recipient'])
            self.assertIn('收到', ack['body'])
            self.assertEqual(ack['created'], assignment['created'])
            self.assertEqual(ack['source'], 'execution')
        self.assertTrue(any(m['kind'] == 'delegated' for m in feed))
        self.assertTrue(any(m['kind'] == 'artifact_handoff' and m['recipient'] for m in feed))
        self.assertEqual(len([m for m in feed if m['kind'] == 'task_succeeded']), len(run['tasks']))
        self.assertEqual(len([m for m in feed if m['body'] == run['goal']]), 1)
        self.assertFalse(any(m['source'] == 'chat' for m in feed))
        self.assertEqual([m['created'] for m in feed], sorted(m['created'] for m in feed))
        self.assertEqual(len({m['id'] for m in feed}), len(feed))
        self.assertEqual(raw_before, self.store.messages('group', limit=100))

    def test_private_messages_run_filter_and_raw_conversation_preserved(self):
        first = self.completed('第一次协作')
        second = self.completed('第二次协作')
        self.store.message('private:coder', 'human', 'private-secret')
        self.store.message('group', 'human', '普通用户消息')
        self.store.message('group', 'coordinator', '普通模型回复')
        full = team_messages(self.store)
        self.assertFalse(any('private-secret' in m['body'] for m in full))
        self.assertEqual([m['body'] for m in full[-2:]], ['普通用户消息','普通模型回复'])
        filtered = team_messages(self.store, first['id'])
        self.assertTrue(filtered)
        self.assertEqual({m['run_id'] for m in filtered}, {first['id']})
        self.assertFalse(any(second['goal'] in m['body'] for m in filtered))
        self.assertEqual(len(team_messages(self.store, limit=3)), 3)
        with self.assertRaises(PermissionError):
            self.store.messages('private:coder', 'researcher', metadata=True)
        meta = self.store.messages('private:coder', metadata=True)
        self.assertIsInstance(meta[0]['created'], float)
        self.assertNotIn('created', self.store.messages('private:coder')[0])

    def test_legacy_summary_recovered_without_changing_events(self):
        run = self.completed()
        for event in self.store.events(run['id']):
            if event['kind'] == 'task_succeeded':
                event['data'].pop('summary', None)
                with self.store.db:
                    self.store.db.execute('UPDATE events SET data=? WHERE seq=?', (json.dumps(event['data']), event['seq']))
        events_before = self.store.events(run['id'])
        feed = team_messages(self.store)
        self.assertTrue(any('已整理研究材料' in m['body'] for m in feed))
        self.assertEqual(events_before, self.store.events(run['id']))

    def test_readable_names_keep_unique_versions_without_ids(self):
        run = self.completed()
        # Deliberately use identical titles, including Windows case-insensitive collisions.
        for task in run['tasks']:
            for output in task['outputs']:
                output.update(title='共享报告.md', kind='process')
        records = describe_artifacts(run)
        for a in records:
            self.assertNotIn('art_', a['download_name'])
            self.assertNotIn(a['id'], a['display_name'])
            self.assertEqual(a['display_name'], a['download_name'])
            self.assertFalse(a['download_name'].endswith('.md.md'))
            self.assertLess(len(a['download_name']), 60)
        self.assertEqual(len({a['download_name'].casefold() for a in records}), len(records))
        duplicate = dict(run['artifacts'][0], id='art_duplicate')
        run['artifacts'].append(duplicate)
        names = [a['download_name'] for a in describe_artifacts(run)]
        self.assertEqual(len(names), len(set(names)))


@unittest.skipIf(TestClient is None, 'Install .[web,test-web]')
class ConversationAPITests(unittest.TestCase):
    def test_time_and_scope(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(Config(provider='demo'), Path(folder))) as client:
            csrf = client.get('/api/session').json()['csrf']
            client.headers['X-EvoTask-CSRF'] = csrf
            response = client.post('/api/chat', json={'channel':'coder','message':'hello'})
            self.assertEqual(response.status_code,200)
            private = client.get('/api/chat?channel=coder').json()
            self.assertTrue(all(isinstance(m['created'],float) for m in private))
            self.assertEqual(client.get('/api/chat').json(),[])
            # Seed only this browser's run, without scheduling or paid model calls.
            root = next(Path(folder).glob('sessions/*/demo'))
            store = Store(root)
            run = Engine(store,Config(provider='demo'),None).new('scope fixture')
            store.close()
            url = '/api/chat?run_id='+run['id']
            self.assertEqual(client.get(url).status_code,200)
            self.assertEqual(client.get(url+'&mode=live').status_code,404)
            self.assertEqual(client.get(url+'&channel=coder').status_code,422)
            with TestClient(client.app) as other:
                other.get('/api/session')
                self.assertEqual(other.get(url).status_code,404)
