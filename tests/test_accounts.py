from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from evotask.config import Config
from evotask.storage import Store, atomic_json
try:
    from fastapi.testclient import TestClient
    from evotask.web.app import create_app
    from evotask.web.accounts import ACCOUNT_COOKIE, token_digest, password_hash, password_matches
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, 'Install .[web,test-web]')
class AccountTests(unittest.TestCase):
    password = 'correct test password 123'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evotask-accounts-')
        self.root = Path(self.temp.name)
        self.app = create_app(Config(provider='demo', model='fixture'), self.root)
        self.client = self.new_client()
        self.client.__enter__()
        self.clients = [self.client]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for client in self.clients[1:]:
            client.close()
        self.temp.cleanup()

    def bootstrap(self, client):
        session = client.get('/api/session').json()
        client.headers['X-EvoTask-CSRF'] = session['csrf']
        return session

    def new_client(self, **kwargs):
        client = TestClient(self.app, **kwargs)
        self.bootstrap(client)
        if hasattr(self, 'clients'):
            self.clients.append(client)
        return client

    def register(self, client=None, name='alice'):
        client = client or self.client
        response = client.post('/api/account/register', json={'username': name, 'password': self.password, 'display_name': '研究者'})
        self.assertEqual(response.status_code, 201, response.text)
        client.headers['X-EvoTask-CSRF'] = response.json()['csrf']
        return response.json()['user']

    def login(self, client, name='alice', password=None):
        response = client.post('/api/account/login', json={'username': name, 'password': password or self.password})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers['X-EvoTask-CSRF'] = response.json()['csrf']
        return response

    def add_document(self, client=None, mode='live', **kwargs):
        response = (client or self.client).post('/api/knowledge?mode=' + mode, json={
            'name': 'notes.md', 'content': '# Evidence\nalpha source', 'source_url': 'https://example.com/evidence', **kwargs})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_cross_browser_data_download_chat_modes_and_isolation(self):
        user = self.register()
        doc = self.add_document()
        self.client.post('/api/chat', json={'channel': 'group', 'message': 'hello'})
        other = self.new_client()
        self.login(other, 'ALICE')
        self.assertEqual(self.bootstrap(other)['user']['id'], user['id'])
        docs = other.get('/api/knowledge?mode=live').json()
        self.assertEqual(docs[0]['id'], doc['id'])
        downloaded = other.get(f"/api/uploads/{docs[0]['upload_id']}/download?mode=live")
        self.assertEqual(downloaded.content.decode(), '# Evidence\nalpha source')
        self.assertEqual(len(other.get('/api/chat').json()), 2)
        self.assertEqual(other.get('/api/knowledge').json(), [])
        outsider = self.new_client()
        self.register(outsider, 'bob')
        self.assertEqual(outsider.get('/api/knowledge?mode=live').json(), [])
        self.assertEqual(outsider.get(f"/api/uploads/{docs[0]['upload_id']}/download?mode=live").status_code, 404)
        self.assertEqual(outsider.get(f"/api/knowledge/{doc['id']}?mode=live").status_code, 404)
        self.assertEqual(outsider.get('/api/chat').json(), [])

    def test_tasks_share_account_and_writer_reservation(self):
        user = self.register()
        other = self.new_client(); self.login(other)
        created = self.client.post('/api/runs', json={'review': True})
        self.assertEqual(created.status_code, 202, created.text)
        run_id = created.json()['id']
        for _ in range(200):
            run = other.get('/api/runs/' + run_id).json()
            if not run['executing']:
                break
            time.sleep(.02)
        self.assertEqual(run['status'], 'waiting_human')
        outsider = self.new_client()
        self.assertEqual(outsider.get('/api/runs/' + run_id).status_code, 404)
        target = self.root / 'accounts' / user['id'] / 'demo'
        self.app.state.jobs.reserve(target)
        try:
            self.assertEqual(other.post('/api/knowledge/bases', json={'name': 'blocked'}).status_code, 409)
        finally:
            self.app.state.jobs.release(target)
        reply = other.post('/api/runs/' + run_id + '/resume', json={'request_id': run['pending']['id'], 'reply': 'yes'})
        self.assertEqual(reply.status_code, 202, reply.text)

    def test_library_clear_syncs_devices_and_preserves_other_account(self):
        self.register()
        removed = self.add_document()
        other = self.new_client(); self.login(other)
        outsider = self.new_client(); self.register(outsider, 'bob')
        kept = self.add_document(outsider, content='bob private notes')
        endpoint = '/api/knowledge/bases/kb_default?mode=live'
        preview = other.get(endpoint).json()
        result = other.request('DELETE', endpoint, json={'delete_documents': True, 'expected_revision': preview['revision']})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.get('/api/knowledge?mode=live').json(), [])
        self.assertEqual(self.client.get('/api/uploads/' + removed['upload_id'] + '/download?mode=live').status_code, 404)
        self.assertEqual(outsider.get('/api/knowledge?mode=live').json()[0]['id'], kept['id'])
        self.assertEqual(outsider.get('/api/uploads/' + kept['upload_id'] + '/download?mode=live').content, b'bob private notes')

    def test_guest_import_preserves_sources_bytes_scopes_and_is_idempotent(self):
        guest_doc = self.add_document(scope='researcher')
        guest_cookie = self.client.cookies.get('evotask_session')
        self.register()
        # Import must merge into an existing account without overwriting same-named data.
        own_doc = self.add_document(content='account original')
        preview = self.client.get('/api/account/guest-knowledge?mode=live').json()
        self.assertEqual(preview['documents'], 1)
        imported = self.client.post('/api/account/import-guest?mode=live', json={})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()['documents'], 1)
        self.assertFalse(self.client.post('/api/account/import-guest?mode=live', json={}).json()['imported'])
        docs = self.client.get('/api/knowledge?mode=live').json()
        self.assertEqual(len(docs), 2)
        copy = next(d for d in docs if d['id'] != own_doc['id'])
        self.assertNotEqual(copy['id'], guest_doc['id'])
        self.assertEqual(copy['source_url'], 'https://example.com/evidence')
        self.assertEqual(copy['scope'], 'researcher')
        self.assertEqual(copy['version'], guest_doc['version'])
        self.assertEqual(self.client.get(f"/api/uploads/{copy['upload_id']}/download?mode=live").text, '# Evidence\nalpha source')
        result = self.client.get('/api/knowledge/search', params={'mode': 'live', 'query': 'alpha', 'agent': 'researcher', 'base_id': copy['base_id']}).json()
        self.assertEqual(result[0]['source_url'], copy['source_url'])
        self.assertEqual(self.client.get('/api/knowledge/search', params={'mode': 'live', 'query': 'alpha', 'agent': 'coder', 'base_id': copy['base_id']}).json(), [])
        guest = self.new_client()
        guest.cookies.set('evotask_session', guest_cookie, domain='testserver.local', path='/')
        self.bootstrap(guest)
        self.assertEqual(guest.get('/api/knowledge?mode=live').json()[0]['id'], guest_doc['id'])
        self.assertFalse(self.client.get('/api/account/guest-knowledge').json()['already_imported'])

    def test_import_failure_rolls_back_and_can_retry(self):
        self.add_document()
        user = self.register()
        with patch('evotask.web.knowledge_import.shutil.copyfile', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                self.client.post('/api/account/import-guest?mode=live', json={})
        self.assertEqual(self.client.get('/api/knowledge?mode=live').json(), [])
        uploads = self.root / 'accounts' / user['id'] / 'live' / 'uploads'
        self.assertEqual(list(uploads.iterdir()), [])
        self.assertFalse(self.client.get('/api/account/guest-knowledge?mode=live').json()['already_imported'])
        self.assertTrue(self.client.post('/api/account/import-guest?mode=live', json={}).json()['imported'])

    def test_import_preserves_legacy_chunk_positions_and_unknown_binary(self):
        sid = self.client.cookies.get('evotask_session').split('.')[0]
        store = Store(self.root / 'sessions' / sid / 'live')
        atomic_json(store.root / 'knowledge.json', [{'id': 'doc_123456789abc', 'name': 'legacy.md',
            'version': 'a' * 64, 'scope': 'researcher', 'chunks': [{'text': 'quasar legacy', 'start': 7, 'end': 8}]}])
        store.close()
        binary = bytes(range(256))
        uploaded = self.client.post('/api/uploads?name=opaque.dat&purpose=knowledge&mode=live',
                                    content=binary, headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.register()
        result = self.client.post('/api/account/import-guest?mode=live', json={})
        self.assertEqual(result.json()['documents'], 2)
        docs = self.client.get('/api/knowledge?mode=live').json()
        opaque = next(d for d in docs if d['name'] == 'opaque.dat')
        self.assertEqual(opaque['status'], 'stored')
        self.assertEqual(self.client.get('/api/uploads/' + opaque['upload_id'] + '/download?mode=live').content, binary)
        legacy = next(d for d in docs if d['name'] == 'legacy.md')
        hits = self.client.get('/api/knowledge/search?mode=live&query=quasar&agent=researcher').json()
        self.assertEqual((hits[0]['start'], hits[0]['end']), (7, 8))
        self.assertEqual(hits[0]['citation'], legacy['id'] + ':aaaaaaaa:0')

    def test_login_cannot_issue_old_password_session_during_password_change(self):
        user = self.register()
        other = self.new_client()
        replacement = password_hash(self.password + 'new')
        def concurrent_change(password, encoded):
            valid = password_matches(password, encoded)
            with self.app.state.accounts.connection() as db:
                db.execute('UPDATE users SET password_hash=? WHERE id=?', (replacement, user['id']))
            return valid
        with patch('evotask.web.accounts.password_matches', side_effect=concurrent_change):
            response = other.post('/api/account/login', json={'username': 'alice', 'password': self.password})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.client.get('/api/account/devices').json()), 1)

    def test_stale_csrf_and_cross_origin_are_rejected(self):
        csrf = self.client.headers['X-EvoTask-CSRF']
        self.register()
        self.assertNotEqual(csrf, self.client.headers['X-EvoTask-CSRF'])
        for method, path in [('post', '/api/account/logout'), ('get', '/api/workspace')]:
            response = getattr(self.client, method)(path, headers={'X-EvoTask-CSRF': csrf}, **({'json': {}} if method == 'post' else {}))
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post('/api/account/logout', json={}, headers={'Origin': 'https://evil.invalid'}).status_code, 403)
        self.assertEqual(self.client.post('/api/account/register', json={'username': 'second', 'password': self.password}).status_code, 409)

    def test_logout_revokes_replay_and_rotates_guest(self):
        self.add_document()
        guest_sid = self.client.cookies.get('evotask_session')
        self.register(); self.add_document(content='private account data')
        replay = self.new_client(); replay.cookies.update(self.client.cookies); self.bootstrap(replay)
        other = self.new_client(); self.login(other)
        self.assertEqual(self.client.post('/api/account/logout', json={}).status_code, 200)
        self.assertIsNone(self.bootstrap(self.client)['user'])
        self.assertNotEqual(guest_sid, self.client.cookies.get('evotask_session'))
        self.assertEqual(self.client.get('/api/knowledge?mode=live').json(), [])
        self.assertEqual(replay.get('/api/knowledge?mode=live').status_code, 401)
        self.assertEqual(len(other.get('/api/knowledge?mode=live').json()), 1)
        self.login(self.client)
        self.assertEqual(len(self.client.get('/api/knowledge?mode=live').json()), 1)

    def test_other_device_revocation_and_password_change(self):
        self.register()
        other = self.new_client(); self.login(other)
        self.assertEqual(len(self.client.get('/api/account/devices').json()), 2)
        self.assertEqual(self.client.post('/api/account/logout-others', json={}).json()['revoked'], 1)
        self.assertEqual(other.get('/api/workspace').status_code, 401)
        self.assertTrue(self.bootstrap(other)['account_expired'])
        self.login(other)
        replay = self.new_client(); replay.cookies.update(self.client.cookies); self.bootstrap(replay)
        wrong = self.client.post('/api/account/password', json={'current_password': 'wrong', 'new_password': self.password + 'new'})
        self.assertEqual(wrong.status_code, 401)
        changed = self.client.post('/api/account/password', json={'current_password': self.password, 'new_password': self.password + 'new'})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.client.headers['X-EvoTask-CSRF'] = changed.json()['csrf']
        self.assertEqual(other.get('/api/workspace').status_code, 401)
        self.assertEqual(replay.get('/api/workspace').status_code, 401)
        self.bootstrap(other)
        failed = other.post('/api/account/login', json={'username': 'alice', 'password': self.password})
        self.assertEqual(failed.status_code, 401)
        self.login(other, password=self.password + 'new')
        self.assertEqual(len(self.client.get('/api/account/devices').json()), 2)

    def test_expiration_fail_closed_and_cookie_security(self):
        user = self.register()
        token = self.client.cookies.get(ACCOUNT_COOKIE)
        with self.app.state.accounts.connection() as db:
            db.execute('UPDATE account_sessions SET expires=? WHERE digest=?', (int(time.time()) - 1, token_digest(token)))
        self.assertEqual(self.client.get('/api/workspace').status_code, 401)
        session = self.bootstrap(self.client)
        self.assertTrue(session['account_expired']); self.assertIsNone(session['user'])
        secure = self.new_client(base_url='https://testserver')
        response = self.login(secure)
        cookie = response.headers['set-cookie'].lower()
        for expected in ['httponly', 'samesite=strict', 'secure']:
            self.assertIn(expected, cookie)
        with self.app.state.accounts.connection() as db:
            row = db.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
            self.assertNotIn(self.password, json.dumps(dict(row)))
            hashes = [r[0] for r in db.execute('SELECT digest FROM account_sessions')]
            self.assertNotIn(secure.cookies.get(ACCOUNT_COOKIE), hashes)

    def test_registration_validation_unique_names_password_salts_and_throttling(self):
        for body in [{'username': '../bad', 'password': self.password}, {'username': 'alice', 'password': 'short'}]:
            response = self.client.post('/api/account/register', json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(body['password'], response.text)
        self.register(name='ALIce')
        other = self.new_client()
        duplicate = other.post('/api/account/register', json={'username': 'alice', 'password': self.password})
        self.assertEqual(duplicate.status_code, 409)
        self.register(other, 'bob')
        with self.app.state.accounts.connection() as db:
            hashes = [r[0] for r in db.execute('SELECT password_hash FROM users')]
            self.assertEqual(len(set(hashes)), 2)
        attacker = self.new_client()
        for _ in range(10):
            last = attacker.post('/api/account/login', json={'username': 'ghost', 'password': 'wrong'})
        self.assertEqual(last.status_code, 401)
        response = attacker.post('/api/account/login', json={'username': 'ghost', 'password': 'wrong'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '60')

    def test_accounts_survive_restart_and_access_gate_cannot_be_bypassed(self):
        self.register(); self.add_document()
        restarted = create_app(Config(provider='demo'), self.root)
        with TestClient(restarted) as client:
            self.bootstrap(client); self.login(client)
            self.assertEqual(len(client.get('/api/knowledge?mode=live').json()), 1)
        guarded = create_app(Config(provider='demo'), self.root, access_token='access-gate-token-24-characters')
        with TestClient(guarded) as client:
            session = self.bootstrap(client)
            self.assertFalse(session['authenticated'])
            self.assertEqual(client.post('/api/account/login', json={'username': 'alice', 'password': self.password}).status_code, 401)
            self.assertEqual(client.post('/api/session/login', json={'token': 'access-gate-token-24-characters'}).status_code, 200)
            self.bootstrap(client); self.login(client)
            self.assertEqual(len(client.get('/api/knowledge?mode=live').json()), 1)

    def test_concurrent_edit_detects_changed_metadata_and_preserves_first_write(self):
        self.register(); doc = self.add_document()
        other = self.new_client(); self.login(other)
        path = '/api/knowledge/' + doc['id'] + '?mode=live'
        initial = self.client.get(path).json()
        body = {'base_id': initial['base_id'], 'scope': 'researcher', 'source_url': 'https://example.com/updated',
                'description': 'first edit', 'expected_revision': initial['revision']}
        self.assertEqual(other.patch(path, json=body).status_code, 200)
        body['content'] = 'stale replacement'
        stale = self.client.patch(path, json=body)
        self.assertEqual(stale.status_code, 409, stale.text)
        latest = self.client.get(path).json()
        self.assertEqual(latest['description'], 'first edit')
        self.assertEqual(latest['text'], '# Evidence\nalpha source')


if __name__ == '__main__':
    unittest.main()
