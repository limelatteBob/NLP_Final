from __future__ import annotations

import os
import json
import sqlite3
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evotask.config import Config
from evotask.engine import Engine
from evotask.retrieval import KnowledgeBase
from evotask.storage import Store
from evotask.tools import Tools

try:
    from fastapi.testclient import TestClient
    from evotask.web.app import create_app
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, 'Install Web test dependencies')
class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evotask-bases-')
        self.root = Path(self.temp.name)
        self.app = create_app(Config(provider='deepseek', model='fixture', api_key_env='EVOTASK_TEST_KB_KEY'), self.root)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.headers['X-EvoTask-CSRF'] = self.client.get('/api/session').json()['csrf']
    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()
    def base(self, name, mode='demo'):
        result = self.client.post('/api/knowledge/bases?mode=' + mode, json={'name': name, 'description': 'project research'})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()
    def document(self, base, content, name='notes.md', scope='shared'):
        result = self.client.post('/api/knowledge', json={'name': name, 'content': content, 'base_id': base, 'scope': scope,
                                                       'source_url': 'https://example.com/research', 'description': 'source notes'})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()
    def edit(self, doc, base, **extra):
        return self.client.patch('/api/knowledge/' + doc['id'], json={'base_id': base, 'scope': 'shared',
                                'source_url': 'https://example.com/revised', 'description': 'updated', **extra})

    def test_named_bases_crud_and_isolation(self):
        base = self.base('TypeMate 市场研究')
        self.assertEqual(self.client.patch('/api/knowledge/bases/' + base['id'], json={'name': '市场知识', 'description': 'new'}).status_code, 200)
        self.assertEqual(len(self.client.get('/api/knowledge/bases?mode=live').json()), 1)
        other = TestClient(self.app)
        other.headers['X-EvoTask-CSRF'] = other.get('/api/session').json()['csrf']
        self.assertEqual(len(other.get('/api/knowledge/bases').json()), 1)
        self.assertEqual(other.patch('/api/knowledge/bases/' + base['id'], json={'name': 'stolen'}).status_code, 422)
        other.close()
        self.assertEqual(self.client.request('DELETE', '/api/knowledge/bases/' + base['id'], json={}).status_code, 200)
        self.assertEqual(self.client.request('DELETE', '/api/knowledge/bases/kb_default', json={}).status_code, 422)

    def test_same_name_separate_bases_scope_and_move_identity(self):
        base = self.base('project')['id']
        first = self.document('kb_default', 'alpha evidence', scope='researcher')
        second = self.document(base, 'beta evidence')
        self.assertNotEqual(first['id'], second['id'])
        self.assertFalse(self.client.get('/api/knowledge/search', params={'query': 'alpha', 'base_id': base}).json())
        self.assertFalse(self.client.get('/api/knowledge/search', params={'query': 'alpha', 'agent': 'coder'}).json())
        self.assertEqual(self.edit(first, base, scope='researcher').status_code, 200)
        # Importing into its former location must not overwrite the moved document.
        replacement = self.document('kb_default', 'gamma evidence', scope='researcher')
        self.assertNotEqual(first['id'], replacement['id'])
        self.assertEqual(len(self.client.get('/api/knowledge').json()), 3)
        self.assertTrue(self.client.get('/api/knowledge/search', params={'query': 'alpha', 'base_id': base}).json())

    def test_edit_body_updates_index_version_original_and_source(self):
        doc = self.document('kb_default', 'obsoleteword')
        response = self.edit(doc, 'kb_default', content='updatedword evidence')
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()
        self.assertEqual(updated['id'], doc['id'])
        self.assertNotEqual(updated['version'], doc['version'])
        self.assertFalse(self.client.get('/api/knowledge/search?query=obsoleteword').json())
        hit = self.client.get('/api/knowledge/search?query=updatedword').json()[0]
        self.assertEqual(hit['source_url'], 'https://example.com/revised')
        self.assertEqual(self.client.get('/api/uploads/' + updated['upload_id'] + '/download').content, b'updatedword evidence')
        self.assertEqual(self.client.get('/api/uploads/' + doc['upload_id'] + '/download').status_code, 404)
        self.assertEqual(self.client.get('/api/knowledge/' + doc['id']).json()['text'], 'updatedword evidence')
        self.assertEqual(self.edit(doc, 'kb_default', source_url='javascript:alert(1)').status_code, 422)

    def test_nonempty_base_cannot_be_deleted_or_conflicting_move_overwrite(self):
        base = self.base('research')['id']
        first = self.document(base, 'evidence')
        second = self.document('kb_default', 'another source')
        self.assertEqual(self.edit(second, base).status_code, 422)
        self.assertEqual(self.client.request('DELETE', '/api/knowledge/bases/' + base, json={}).status_code, 422)
        self.assertEqual(self.client.request('DELETE', '/api/knowledge/' + first['id'], json={}).status_code, 200)
        self.assertEqual(self.client.request('DELETE', '/api/knowledge/bases/' + base, json={}).status_code, 200)

    def delete_confirmed(self, base, preview=None):
        preview = preview or self.client.get('/api/knowledge/bases/' + base).json()
        return self.client.request('DELETE', '/api/knowledge/bases/' + base, json={
            'delete_documents': True, 'expected_revision': preview['revision']})

    def test_delete_populated_base_removes_index_and_files_without_affecting_other_data(self):
        base = self.base('discard')['id']
        first = self.document(base, 'deleteword evidence')
        second = self.document(base, 'privateword evidence', name='private.md', scope='researcher')
        keep = self.document('kb_default', 'keepword evidence')
        endpoint = '/api/knowledge/bases/' + base
        other = TestClient(self.app)
        other.headers['X-EvoTask-CSRF'] = other.get('/api/session').json()['csrf']
        preview = self.client.get(endpoint).json()
        self.assertEqual(preview['documents'], 2)
        self.assertEqual(other.request('DELETE', endpoint, json={'delete_documents': True, 'expected_revision': preview['revision']}).status_code, 404)
        other.close()
        self.assertEqual(self.client.request('DELETE', endpoint + '?mode=live', json={'delete_documents': True, 'expected_revision': preview['revision']}).status_code, 404)
        result = self.delete_confirmed(base)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {'removed': True, 'cleared': False, 'deleted_documents': 2, 'cleanup_pending': 0})
        for doc in (first, second):
            self.assertEqual(self.client.get('/api/knowledge/' + doc['id']).status_code, 404)
            self.assertEqual(self.client.get('/api/uploads/' + doc['upload_id'] + '/download').status_code, 404)
            self.assertFalse(list((self.root / 'sessions').glob('*/demo/uploads/' + doc['upload_id'])))
        self.assertEqual(self.client.get('/api/knowledge/search?query=deleteword').json(), [])
        self.assertEqual(self.client.get('/api/knowledge').json()[0]['id'], keep['id'])
        self.assertEqual(self.client.get('/api/uploads/' + keep['upload_id'] + '/download').content, b'keepword evidence')
        self.assertEqual(self.client.get(endpoint).status_code, 404)
        self.assertEqual(self.delete_confirmed(base, preview).status_code, 404)

    def test_default_clear_retains_library_and_prevents_legacy_reimport(self):
        self.client.get('/api/knowledge')
        root = next((self.root / 'sessions').iterdir()) / 'demo'
        legacy = [{'id': 'doc_123456789abc', 'name': 'old.txt', 'version': 'a' * 64, 'scope': 'shared',
                   'chunks': [{'text': 'legacyword', 'start': 1, 'end': 1}]}]
        (root / 'knowledge.json').write_text(json.dumps(legacy), encoding='utf-8')
        self.document('kb_default', 'currentword')
        result = self.delete_confirmed('kb_default')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['cleared'])
        self.assertEqual(result.json()['deleted_documents'], 2)
        self.assertEqual(self.client.get('/api/knowledge').json(), [])
        self.assertEqual(self.client.get('/api/knowledge/bases').json()[0]['id'], 'kb_default')
        self.document('kb_default', 'nextword')
        self.assertEqual(len(self.client.get('/api/knowledge').json()), 1)
        self.assertFalse(self.client.get('/api/knowledge/search?query=legacyword').json())

    def test_deletion_rejects_stale_preview_missing_confirmation_csrf_and_busy(self):
        base = self.base('concurrent')['id']
        doc = self.document(base, 'version1')
        endpoint = '/api/knowledge/bases/' + base
        preview = self.client.get(endpoint).json()
        self.assertEqual(self.client.request('DELETE', endpoint, json={'delete_documents': True}).status_code, 422)
        self.assertEqual(self.client.request('DELETE', endpoint, json={'delete_documents': True, 'expected_revision': '错' * 64}).status_code, 422)
        self.assertEqual(self.client.request('DELETE', endpoint, json={}, headers={'X-EvoTask-CSRF': 'wrong'}).status_code, 403)
        self.assertEqual(self.edit(doc, base, description='changed on another device').status_code, 200)
        self.assertEqual(self.delete_confirmed(base, preview).status_code, 409)
        self.assertEqual(self.client.get('/api/uploads/' + doc['upload_id'] + '/download').status_code, 200)
        target = next((self.root / 'sessions').iterdir()) / 'demo'
        self.app.state.jobs.reserve(target)
        try:
            self.assertEqual(self.delete_confirmed(base).status_code, 409)
        finally:
            self.app.state.jobs.release(target)
        self.assertEqual(self.delete_confirmed(base).status_code, 200)

    def test_failed_file_cleanup_is_revoked_and_retried(self):
        base = self.base('locked')['id']
        doc = self.document(base, 'lockedword')
        with patch('evotask.web.app.remove_upload', side_effect=PermissionError('file in use')):
            result = self.delete_confirmed(base)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['cleanup_pending'], 1)
        self.assertEqual(self.client.get('/api/uploads/' + doc['upload_id'] + '/download').status_code, 404)
        self.assertEqual(self.client.get('/api/knowledge').json(), [])
        folder = next((self.root / 'sessions').iterdir()) / 'demo' / 'uploads' / doc['upload_id']
        self.assertTrue(folder.exists())
        self.base('retry cleanup')
        self.assertFalse(folder.exists())

    def test_database_failure_rolls_back_deletion_and_keeps_files(self):
        base = self.base('atomic')['id']
        doc = self.document(base, 'rollbackword')
        path = next((self.root / 'sessions').iterdir()) / 'demo' / 'state.sqlite3'
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TRIGGER fail_base_delete BEFORE DELETE ON knowledge_bases BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.delete_confirmed(base)
        self.assertEqual(self.client.get('/api/uploads/' + doc['upload_id'] + '/download').status_code, 200)
        self.assertTrue(self.client.get('/api/knowledge/search?query=rollbackword').json())
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM knowledge_file_cleanup').fetchone()[0], 0)
            db.execute('DROP TRIGGER fail_base_delete')
        self.assertEqual(self.delete_confirmed(base).status_code, 200)

    def test_run_selection_enforced_by_tool_and_explicit_empty_disables_retrieval(self):
        store = Store(self.root / 'core')
        try:
            kb = KnowledgeBase(store)
            base = kb.save_base('other')['id']
            first, second = self.root / 'a.txt', self.root / 'b.txt'
            first.write_text('alpha evidence')
            second.write_text('beta evidence')
            kb.add(first)
            kb.add(second, base_id=base)
            engine = Engine(store, Config(provider='demo', model='fixture'), None)
            run = engine.new('select', knowledge_base_ids=[base])
            self.assertEqual(engine.tools.execute(run, {'owner': 'researcher'}, 'retrieve_documents', {'query': 'alpha'})['passages'], [])
            self.assertTrue(engine.tools.execute(run, {'owner': 'researcher'}, 'retrieve_documents', {'query': 'beta'})['passages'])
            run['knowledge_base_ids'] = []
            self.assertEqual(engine.tools.execute(run, {'owner': 'researcher'}, 'retrieve_documents', {'query': 'beta'})['passages'], [])
        finally:
            store.close()

    def test_http_task_selection_validated_and_persisted_without_model_calls(self):
        base = self.base('task research', 'live')['id']
        with patch.dict(os.environ, {'EVOTASK_TEST_KB_KEY': 'test-only'}), patch('evotask.web.app.Jobs.submit'):
            invalid = self.client.post('/api/runs?mode=live', json={'goal': 'research', 'knowledge_base_ids': ['kb_foreign']})
            self.assertEqual(invalid.status_code, 422)
            result = self.client.post('/api/runs?mode=live', json={'goal': 'research', 'knowledge_base_ids': [base]})
            self.assertEqual(result.status_code, 202, result.text)
            self.assertEqual(result.json()['knowledge_base_ids'], [base])

    def test_repair_api_authorizes_legacy_run_and_queues_original_config(self):
        from tests.test_collaboration import ReportProvider, InspectingSandbox
        self.client.get('/api/workspace')
        root = next((self.root / 'sessions').iterdir()) / 'demo'
        store = Store(root)
        try:
            cfg = Config(provider='demo', model='fixture')
            engine = Engine(store, cfg, ReportProvider(False), sandbox=InspectingSandbox())
            run = engine.new('legacy report')
            run['revision_count'] = 2
            engine.advance(run)
            run.pop('revision_count')
            store.save(run)
        finally:
            store.close()
        detail = self.client.get('/api/runs/' + run['id']).json()
        self.assertTrue(detail['can_repair'])
        endpoint = '/api/runs/' + run['id'] + '/repair'
        self.assertEqual(self.client.post(endpoint, json={}, headers={'X-EvoTask-CSRF': 'invalid'}).status_code, 403)
        other = TestClient(self.app)
        other.headers['X-EvoTask-CSRF'] = other.get('/api/session').json()['csrf']
        self.assertEqual(other.post(endpoint, json={}).status_code, 404)
        other.close()
        with patch('evotask.web.app.Jobs.submit') as submit:
            response = self.client.post(endpoint, json={})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(submit.call_args.args[2], cfg)
        repaired = self.client.get('/api/runs/' + run['id']).json()
        self.assertEqual(repaired['status'], 'running')
        self.assertEqual(len(repaired['revisions']), 1)


if __name__ == '__main__':
    unittest.main()
