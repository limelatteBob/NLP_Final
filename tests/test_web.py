from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from evotask.web.app import create_app
except ImportError:
    TestClient = None

from evotask.config import Config
from evotask.providers import DemoProvider
from evotask.providers import ProviderUnavailable
from evotask.engine import Engine
from tests.test_action_protocol import SpeechProvider, legacy_normalize


@unittest.skipIf(TestClient is None, 'Install .[web,test-web] to test HTTP services')
class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evotask-web-test-')
        self.config = Config(provider='demo', model='fixture')
        self.app = create_app(self.config, Path(self.temp.name))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.bootstrap(self.client)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def bootstrap(self, client):
        session = client.get('/api/session').json()
        client.headers.update({'X-EvoTask-CSRF': session['csrf']})
        return session

    def start_run(self, **kwargs):
        response = self.client.post('/api/runs', json=kwargs)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['id']

    def wait_idle(self, run_id):
        for _ in range(120):
            run = self.client.get('/api/runs/' + run_id).json()
            if not run['executing']:
                return run
            time.sleep(.025)
        self.fail('Worker did not become idle')

    def test_complete_review_export_and_stale_approval(self):
        run_id = self.start_run(review=True)
        run = self.wait_idle(run_id)
        self.assertEqual(run['status'], 'waiting_human', run)
        self.assertEqual(len(run['tasks']), 3)
        self.assertGreater(len(run['artifacts']), 0)
        report = next(a for a in run['artifacts'] if a['path'] == 'report.md')
        body = self.client.get(f'/api/runs/{run_id}/artifacts/{report["id"]}').json()
        self.assertIn('DEMO', body['content'])
        self.assertIn('doc_', body['content'])
        downloaded = self.client.get(f'/api/runs/{run_id}/artifacts/{report["id"]}?download=true')
        self.assertEqual(downloaded.content.decode(), body['content'])
        self.assertIn('attachment;', downloaded.headers['content-disposition'])
        reply = {'request_id': run['pending']['id'], 'reply': 'yes'}
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/resume', json=reply).status_code, 202)
        done = self.wait_idle(run_id)
        self.assertEqual(done['status'], 'succeeded')
        self.assertEqual(done['completed_tasks'], 3)
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/resume', json=reply).status_code, 409)
        export = self.client.get(f'/api/runs/{run_id}/report')
        self.assertEqual(export.json()['run']['status'], 'succeeded')
        self.assertIn('events', export.json())
        self.assertGreater(len(self.client.get('/api/agents/coordinator/memory').json()), 0)

    def test_retry_action_format_failure_is_isolated_and_keeps_review(self):
        with patch('evotask.web.app.make_provider', return_value=SpeechProvider()), \
                patch.object(Engine, 'normalize_decision', legacy_normalize):
            run_id = self.start_run(goal='K-pop 项目汇报', review=False)
            failed = self.wait_idle(run_id)
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue(failed['can_retry_actions'])
        url = f'/api/runs/{run_id}/retry-actions'
        other = TestClient(self.app)
        self.bootstrap(other)
        self.assertEqual(other.post(url, json={}).status_code, 404)
        self.assertEqual(self.client.post(url, json={}, headers={'X-EvoTask-CSRF': 'bad'}).status_code, 403)
        self.assertEqual(self.client.post(url + '?mode=live', json={}).status_code, 404)
        with patch('evotask.web.app.make_provider', return_value=SpeechProvider()):
            self.assertEqual(self.client.post(url, json={}).status_code, 202)
            resumed = self.wait_idle(run_id)
        self.assertEqual(resumed['status'], 'waiting_human')
        self.assertEqual(resumed['pending']['task_id'], 't2')
        self.assertFalse(resumed['can_retry_actions'])
        self.assertEqual(resumed['tasks'][0], failed['tasks'][0])
        self.assertEqual(self.client.post(url, json={}).status_code, 409)
        self.assertTrue(any(a['path'] == 'speech_draft.md' for a in resumed['artifacts']))

    def test_sessions_modes_and_downloads_are_isolated(self):
        run_id = self.start_run(review=False)
        run = self.wait_idle(run_id)
        other = TestClient(self.app)
        self.bootstrap(other)
        self.assertEqual(other.get('/api/workspace').json()['runs'], [])
        self.assertEqual(other.get('/api/runs/' + run_id).status_code, 404)
        self.assertEqual(other.get(f'/api/runs/{run_id}/report').status_code, 404)
        self.assertEqual(other.get(f'/api/runs/{run_id}/artifacts/{run["artifacts"][0]["id"]}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/runs/{run_id}?mode=live').status_code, 404)
        restored = TestClient(self.app)
        restored.cookies.update(self.client.cookies)
        self.bootstrap(restored)
        self.assertEqual(restored.get('/api/runs/' + run_id).json()['status'], 'succeeded')
        other.close(); restored.close()

    def test_csrf_origin_host_and_validation(self):
        self.assertEqual(self.client.post('/api/runs', json={}, headers={'X-EvoTask-CSRF': 'wrong'}).status_code, 403)
        self.assertEqual(self.client.post('/api/runs', json={}, headers={'Origin': 'https://evil.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/api/session', headers={'Host': 'evil.invalid'}).status_code, 400)
        self.assertEqual(self.client.get('/api/workspace?mode=invalid').status_code, 422)
        self.assertEqual(self.client.post('/api/runs', json={'provider': 'other'}).status_code, 422)
        self.assertEqual(self.client.post('/api/knowledge', json={'name': '../secret.md', 'content': 'hello'}).status_code, 422)
        self.assertEqual(self.client.post('/api/knowledge', json={'name': 'x.pdf', 'content': 'hello'}).json()['status'], 'stored')
        self.assertEqual(self.client.post('/api/knowledge', json={'name': 'x.md', 'content': '汉' * 90000}).status_code, 201)
        self.assertEqual(self.client.post('/api/session/login', content=b' ' * 2_000_001,
                                         headers={'Content-Type': 'application/json'}).status_code, 413)
        self.assertEqual(self.client.post('/api/runs?mode=live', json={'goal': 'hello'}).status_code, 422)

    def test_rag_scope_version_search_and_remove(self):
        content = {'name': '边界.md', 'content': '私有 quasar。空文本应返回零相似度。', 'scope': 'researcher'}
        response = self.client.post('/api/knowledge', json=content)
        self.assertEqual(response.status_code, 201, response.text)
        doc = response.json()
        self.assertTrue(self.client.get('/api/knowledge/search?query=空文本&agent=researcher').json())
        self.assertFalse(self.client.get('/api/knowledge/search?query=quasar&agent=coder').json())
        content['content'] = '更新：私有 nebula'
        updated = self.client.post('/api/knowledge', json=content).json()
        self.assertEqual(doc['id'], updated['id'])
        self.assertNotEqual(doc['version'], updated['version'])
        self.assertEqual(self.client.request('DELETE', '/api/knowledge/' + doc['id'], json={}).status_code, 200)
        self.assertEqual(self.client.get('/api/knowledge').json(), [])
        self.assertFalse(self.client.get('/api/knowledge/search?query=nebula').json())

    def test_chat_private_and_group_history(self):
        response = self.client.post('/api/chat', json={'channel': 'coder', 'message': 'private test'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('DEMO', response.json()['body'])
        self.assertEqual(self.client.get('/api/chat?channel=group').json(), [])
        self.assertEqual(self.client.get('/api/chat?channel=verifier').json(), [])
        private = self.client.get('/api/chat?channel=coder').json()
        self.assertEqual([x['sender'] for x in private], ['human', 'coder'])

    def test_event_stream_and_static_boundary(self):
        run_id = self.start_run(review=False)
        self.wait_idle(run_id)
        stream = self.client.get(f'/api/runs/{run_id}/stream')
        self.assertEqual(stream.status_code, 200)
        self.assertIn('event: snapshot', stream.text)
        self.assertIn('event: idle', stream.text)
        snapshot = json.loads(stream.text.split('data: ')[1].split('\n\n')[0])
        self.assertGreater(snapshot['cursor'], 0)
        self.assertEqual(snapshot['run']['status'], 'succeeded')
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/assets/app.js').status_code, 200)
        self.assertEqual(self.client.get('/.env').status_code, 404)
        self.assertEqual(self.client.get('/assets/%2e%2e/%2e%2e/.env').status_code, 404)
        self.assertIn("script-src 'self'", self.client.get('/').headers['content-security-policy'])

    def test_busy_job_cancels_at_boundary_without_next_tool(self):
        entered, release = threading.Event(), threading.Event()

        class SlowProvider(DemoProvider):
            def complete(self, purpose, context):
                if purpose == 'plan':
                    entered.set()
                    release.wait(4)
                return super().complete(purpose, context)

        with patch('evotask.web.app.make_provider', return_value=SlowProvider()):
            run_id = self.start_run(review=False)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.client.post('/api/runs', json={}).status_code, 409)
            self.assertEqual(self.client.post('/api/knowledge', json={'name': 'x.md', 'content': 'hello'}).status_code, 409)
            self.assertEqual(self.client.post(f'/api/runs/{run_id}/cancel', json={}).status_code, 200)
            release.set()
            run = self.wait_idle(run_id)
        self.assertEqual(run['status'], 'cancelled')
        self.assertEqual(run['artifacts'], [])

    def test_waiting_run_can_be_cancelled_and_rejection_is_recorded(self):
        run_id = self.start_run(review=True)
        self.assertEqual(self.wait_idle(run_id)['status'], 'waiting_human')
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/cancel', json={}).status_code, 200)
        stopped = self.wait_idle(run_id)
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertIsNone(stopped['pending'])
        self.assertGreater(len(stopped['artifacts']), 0)
        run_id = self.start_run(review=True)
        run = self.wait_idle(run_id)
        self.client.post(f'/api/runs/{run_id}/resume', json={'request_id': run['pending']['id'], 'reply': 'no'})
        self.assertEqual(self.wait_idle(run_id)['status'], 'failed')

    def test_token_gate_does_not_echo_submitted_credentials(self):
        token = 'test-access-token-with-more-than-24-chars'
        protected = create_app(self.config, Path(self.temp.name) / 'protected', access_token=token)
        with TestClient(protected) as client:
            self.assertFalse(self.bootstrap(client)['authenticated'])
            self.assertEqual(client.get('/api/workspace').status_code, 401)
            response = client.post('/api/session/login', json={'token': token, 'unknown': token})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(token, response.text)
            self.assertEqual(client.post('/api/session/login', json={'token': 'incorrect'}).status_code, 401)
            self.assertEqual(client.post('/api/session/login', json={'token': token}).status_code, 200)
            self.assertEqual(client.get('/api/workspace').status_code, 200)
            self.assertIn('HttpOnly', client.post('/api/session/login', json={'token': token}).headers['set-cookie'])

    def test_review_tracks_terminal_tasks_in_unordered_plan(self):
        class ReorderedDemo(DemoProvider):
            def complete(self, purpose, context):
                value = super().complete(purpose, context)
                if purpose == 'plan':
                    value['tasks'].reverse()
                return value

        with patch('evotask.web.app.make_provider', return_value=ReorderedDemo()):
            run = self.wait_idle(self.start_run(review=True))
        self.assertEqual(run['status'], 'waiting_human')
        self.assertEqual(run['pending']['task_id'], 'report')
        self.assertEqual(next(t for t in run['tasks'] if t['id'] == 'research')['status'], 'succeeded')

    def test_api_account_failure_is_visible_and_not_retried(self):
        class Unavailable:
            calls = 0

            def complete(self, purpose, context):
                self.calls += 1
                raise ProviderUnavailable('Model API returned HTTP 402: insufficient account balance')

        provider = Unavailable()
        with patch('evotask.web.app.make_provider', return_value=provider):
            run = self.wait_idle(self.start_run(review=False))
        self.assertEqual(run['status'], 'failed')
        self.assertIn('HTTP 402', run['failure'])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(run['artifacts'], [])


if __name__ == '__main__':
    unittest.main()
