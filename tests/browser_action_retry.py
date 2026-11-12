"""Exercise failure recovery in Edge with a temporary server and no paid API calls."""
from __future__ import annotations

import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from evotask.config import Config
from evotask.engine import Engine
from evotask.web.app import create_app
from tests.test_action_protocol import SpeechProvider, legacy_normalize


def main():
    with tempfile.TemporaryDirectory(prefix='evotask-retry-browser-') as temp:
        app = create_app(Config(provider='demo', model='fixture'), Path(temp))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            url = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if time.monotonic() > deadline:
                        raise RuntimeError('Test server did not start')
                    time.sleep(.05)
                with patch('evotask.web.app.make_provider', return_value=SpeechProvider()), httpx.Client(base_url=url) as client:
                    session = client.get('/api/session').json()
                    client.headers['X-EvoTask-CSRF'] = session['csrf']
                    with patch.object(Engine, 'normalize_decision', legacy_normalize):
                        response = client.post('/api/runs', json={'goal': 'K-pop 项目汇报', 'review': False})
                        response.raise_for_status()
                        run_id = response.json()['id']
                        deadline = time.monotonic() + 10
                        while True:
                            run = client.get('/api/runs/' + run_id).json()
                            if not run['executing']:
                                break
                            if time.monotonic() > deadline:
                                raise RuntimeError('Fixture did not finish')
                            time.sleep(.05)
                    assert run['status'] == 'failed' and run['can_retry_actions']
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel='msedge', headless=True)
                        context = browser.new_context(viewport={'width': 1365, 'height': 900})
                        context.add_cookies([{'name': c.name, 'value': c.value, 'url': url} for c in client.cookies.jar])
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(url + '/#task/' + run_id)
                        page.get_by_role('button', name='继续失败任务', exact=True).click()
                        expect(page.locator('#modal')).to_contain_text('保留已完成的上游任务')
                        page.get_by_role('button', name='继续执行', exact=True).click()
                        expect(page.get_by_role('button', name='确认通过')).to_be_visible(timeout=15000)
                        page.get_by_role('button', name='speech_draft.md', exact=False).click()
                        expect(page.locator('#modal .markdown')).to_contain_text('K-pop')
                        page.get_by_role('button', name='关闭', exact=True).click()
                        output = Path('test-results/web')
                        output.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(output / 'action-retry-review.png'), full_page=True)
                        page.get_by_role('button', name='确认通过').click()
                        expect(page.locator('.detail-meta').first).to_contain_text('已完成', timeout=15000)
                        assert not errors, errors
                        browser.close()
                    print('PASS: failed task -> retry -> speech artifact -> human review -> completed; no API calls')
            finally:
                server.should_exit = True
                thread.join(timeout=15)


if __name__ == '__main__':
    main()
