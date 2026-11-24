"""Create an unrelated custom team through the UI; exercise both rounds with controlled responses."""
import json
import hashlib
import socket
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import uvicorn
from playwright.sync_api import expect, sync_playwright

from evotask.config import Config
from evotask.web.app import create_app
from tests.test_teams import MuseumProvider, ROLES


class ChoiceMuseumProvider(MuseumProvider):
    def complete(self, purpose, context):
        result = super().complete(purpose, context)
        if result.get('question'):
            result = dict(result, options=['陶瓷展（推荐）', '书画展'])
        return result


def fill_team(page, name, roles):
    page.get_by_role('button', name='新建团队', exact=True).click()
    page.locator('#team-name').fill(name)
    for slot, role in roles.items():
        page.locator(f'[name="{slot}-name"]').fill(role['name'])
        page.locator(f'[name="{slot}-instructions"]').fill(role['instructions'])
    page.get_by_role('button', name='创建团队', exact=True).click()
    expect(page.locator('h1')).to_have_text(name)


def main():
    with tempfile.TemporaryDirectory(prefix='evotask-team-browser-') as temp, socket.socket() as sock:
        app = create_app(Config(provider='demo', model='fixture'), Path(temp) / 'state')
        sock.bind(('127.0.0.1', 0)); url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline: raise RuntimeError('Server startup timed out')
                time.sleep(.05)
            with patch('evotask.web.app.make_provider', return_value=ChoiceMuseumProvider()), sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
                errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(url + '/#chat')
                fill_team(page, 'Museum accessibility team', ROLES)
                team_url = page.url
                page.locator('#team-message').fill('Discuss the visitor audience')
                page.get_by_role('button', name='发送消息', exact=True).click()
                expect(page.locator('#team-messages')).to_contain_text('discussion only')
                expect(page.locator('[data-team-run]')).to_have_count(0)
                page.locator('#team-channel').select_option('coder')
                expect(page.locator('#team-channel')).to_have_value('coder')
                expect(page.get_by_role('button', name='发起任务', exact=True)).to_be_disabled()
                page.locator('#team-message').fill('PRIVATE_DESIGN_NOTE')
                page.get_by_role('button', name='发送消息', exact=True).click()
                expect(page.locator('#team-messages')).to_contain_text('PRIVATE_DESIGN_NOTE')
                page.locator('#team-channel').select_option('researcher')
                expect(page.locator('#team-messages')).not_to_contain_text('PRIVATE_DESIGN_NOTE')
                page.locator('#team-channel').select_option('group')
                expect(page.get_by_role('button', name='发起任务', exact=True)).to_be_enabled()
                expect(page.locator('#team-messages')).not_to_contain_text('PRIVATE_DESIGN_NOTE')
                page.locator('#team-message').fill('Prepare the museum guide')
                page.get_by_role('button', name='发起任务', exact=True).click()
                for round_number in range(2):
                    expect(page.locator('[data-team-reply] textarea')).to_be_visible(timeout=15000)
                    run_id = page.locator('[data-team-reply]').get_attribute('data-team-reply')
                    page.reload()  # The persisted question must survive a refresh.
                    if round_number == 0:
                        page.goto(url + '/#task/' + run_id)
                        page.get_by_role('button', name='书画展', exact=True).click()
                        expect(page.get_by_role('button', name='确认通过', exact=True)).to_be_visible(timeout=15000)
                        page.goto(team_url)
                    else:
                        page.locator('[data-team-reply] textarea').fill('The ceramics exhibition')
                        page.get_by_role('button', name='回复并继续', exact=True).click()
                    expect(page.get_by_role('button', name='确认通过', exact=True)).to_be_visible(timeout=15000)
                    assert page.locator('[data-team-reply]').get_attribute('data-team-reply') == run_id
                    page.get_by_role('button', name='确认通过', exact=True).click()
                    try:
                        expect(page.locator(f'[data-team-run="{run_id}"] > .team-actions > .pill')).to_have_text('已完成', timeout=15000)
                    except AssertionError:
                        debug = Path('test-results/interaction'); debug.mkdir(parents=True, exist_ok=True)
                        (debug / 'browser-failure.json').write_text(json.dumps({
                            'run': page.request.get(url + '/api/runs/' + run_id).json(), 'errors': errors,
                            'visible': page.locator('body').inner_text()}, ensure_ascii=False, indent=2), encoding='utf-8')
                        raise
                    if round_number == 0:
                        page.get_by_role('button', name='基于这些文件发起新任务').click()
                        expect(page.locator('#modal')).to_contain_text('museum/data.json')
                        page.locator('#team-next-goal').fill('Extend the existing museum guide and increment visitor count')
                        page.get_by_role('button', name='创建并启动新任务').click()
                with page.expect_download() as download:
                    page.get_by_role('link', name='导出完整团队记录').click()
                exported = Path(temp) / 'audit.json'; download.value.save_as(exported)
                audit = json.loads(exported.read_text(encoding='utf-8'))
                assert len(audit['runs']) == 2 and all(r['run']['status'] == 'succeeded' for r in audit['runs'])
                assert audit['runs'][1]['run']['inherited_inputs']
                with page.expect_download() as download:
                    page.get_by_role('link', name='导出评测包 ZIP', exact=True).click()
                package = Path(temp) / 'evaluation.zip'; download.value.save_as(package)
                with zipfile.ZipFile(package) as archive:
                    manifest = json.loads(archive.read('manifest.json'))
                    assert len(manifest['runs']) == 2
                    assert 'The ceramics exhibition' in archive.read('dialogue.md').decode()
                    assert '书画展' in archive.read('dialogue.md').decode()
                    for item in manifest['files']:
                        assert hashlib.sha256(archive.read(item['path'])).hexdigest() == item['sha256']
                output = Path('test-results/mid-test'); output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'teams-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.screenshot(path=str(output / 'teams-mobile.png'), full_page=True)
                with page.expect_download() as download:
                    page.locator(f'[data-team-run="{run_id}"]').get_by_role('link', name='slides.html').click()
                html = Path(temp) / 'slides.html'; download.value.save_as(html)
                page.goto(html.as_uri())
                expect(page.locator('#slide')).to_have_text('Sources')
                page.locator('#next').click(); expect(page.locator('#slide')).to_have_text('Visitor implications')
                page.locator('#prev').click(); expect(page.locator('#slide')).to_have_text('Sources')
                assert not errors, errors
                browser.close()
            print('PASS: UI team creation, chat, questions/resume, inheritance, JSON/ZIP exports and hashes, responsive layout, downloaded HTML navigation')
        finally:
            server.should_exit = True; thread.join(timeout=15)


if __name__ == '__main__':
    main()
