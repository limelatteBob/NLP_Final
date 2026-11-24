"""Regressions for joining the retry/chat work with custom-team support."""
import tempfile
import unittest
from pathlib import Path

from evotask.config import Config
from evotask.conversations import team_messages
from evotask.engine import Engine
from evotask.retries import create_retry
from evotask.storage import AGENTS, Store
from evotask.teams import Teams


class MergeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.config = Config(provider='demo', model='merge-fixture')
        self.engine = Engine(self.store, self.config, None)
        self.roles = {slot: {'name': slot, 'instructions': 'Test role'} for slot in AGENTS}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_retry_keeps_custom_team_and_original_html_snapshot(self):
        team = Teams(self.store).create('Team', self.roles)
        first = self.engine.new('First', team_id=team['id'])
        path = self.store.workspace('coder', first['id']) / 'slides.html'
        content = '<!doctype html><title>Original</title>'
        path.write_text(content, encoding='utf-8')
        artifact = self.store.archive(first, 'coder', 'slides.html', 'author')
        artifact['verification'] = 'verified'
        first['status'] = 'succeeded'
        self.store.save(first)
        first.pop('execution_schema', None)  # This test models an archived pre-contract record.
        self.store.save(first)
        source = self.engine.new('Extend', team_id=team['id'], source_run_id=first['id'], artifact_ids=[artifact['id']])
        source['status'] = 'failed'
        self.store.save(source)
        with self.store.lock():
            retry = create_retry(self.store, self.config, source, 'team-retry-key-0001')
        self.assertEqual(retry['team_snapshot'], source['team_snapshot'])
        self.assertEqual(retry['inherited_inputs'], source['inherited_inputs'])
        self.assertEqual(retry['initial_inputs'], source['initial_inputs'])
        self.assertEqual(retry['retry_of'], source['id'])
        for agent in AGENTS:
            copied = self.store.workspace(agent, retry['id']) / retry['initial_inputs'][0]
            self.assertEqual(copied.read_text(encoding='utf-8'), content)
        original = self.store.workspace('coordinator', source['id']) / source['initial_inputs'][0]
        original.write_text('tampered', encoding='utf-8')
        with self.store.lock(), self.assertRaisesRegex(ValueError, 'snapshot has changed'):
            create_retry(self.store, self.config, source, 'team-retry-key-0002')

    def test_chat_projection_filters_team_events_and_keeps_timestamps(self):
        default = self.engine.new('DEFAULT_ONLY')
        a = Teams(self.store).create('A', self.roles)
        b = Teams(self.store).create('B', self.roles)
        self.engine.new('A_ONLY', team_id=a['id'])
        self.engine.new('B_ONLY', team_id=b['id'])
        for team_id, expected in [(None, 'DEFAULT_ONLY'), (a['id'], 'A_ONLY'), (b['id'], 'B_ONLY')]:
            self.store.bind_team(team_id)
            messages = team_messages(self.store, team_id=team_id)
            self.assertEqual([m['body'] for m in messages], [expected])
            self.assertTrue(all(m['created'] for m in messages))


if __name__ == '__main__':
    unittest.main()
