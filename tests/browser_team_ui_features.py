"""Isolated browser acceptance; injected failure + real offline workflow, no paid calls."""
import json
import socket
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import uvicorn
from playwright.sync_api import sync_playwright, expect

from evotask.config import Config
from evotask.engine import Engine
from evotask.storage import Store
from evotask.teams import Teams
from evotask.retrieval import KnowledgeBase
from evotask.web.app import create_app
from tests.test_team_ui_features import DOCUMENT


def main():
    output = Path('test-results/team-ui-20261125'); output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='team-features-') as temp, socket.socket() as sock:
        state = Path(temp) / 'state'
        app = create_app(Config(provider='demo'), state)
        sock.bind(('127.0.0.1', 0)); url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True); thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline: raise RuntimeError('Test server startup timeout')
                time.sleep(.05)
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport={'width':1440, 'height':1000})
                page = context.new_page(); errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(url + '/#chat')
                page.get_by_role('button', name='新建团队', exact=True).click()
                page.get_by_role('button', name='上传 Markdown', exact=True).click()
                page.locator('#team-document').set_input_files({'name':'team.md', 'mimeType':'text/markdown', 'buffer':DOCUMENT.encode()})
                page.get_by_role('button', name='识别成员与职责').click()
                expect(page.locator('#team-import-status')).to_contain_text('已识别 4 位成员')
                expect(page.locator('[name="coordinator-name"]')).to_have_value('技术负责人')
                expect(page.locator('[name="verifier-name"]')).to_have_value('测试工程师')
                page.locator('[name="coordinator-name"]').fill('协作负责人')
                page.screenshot(path=str(output/'markdown-preview.png'))
                page.get_by_role('button', name='创建团队', exact=True).click()
                expect(page.locator('h1')).to_have_text('示例项目团队')
                team_id = page.url.split('/')[-1]
                sid = next(c['value'].split('.')[0] for c in context.cookies() if c['name']=='evotask_session')
                root = state / 'sessions' / sid / 'demo'
                with closing(Store(root)) as store:
                    KnowledgeBase(store).add(Path('evotask/web/data/similarity.md'))
                    run = Engine(store, Config(provider='demo'), None).new('演示：调研文本相似度，编写代码并独立验证', team_id=team_id, require_review=False)
                    run.update(status='failed', failure='浏览器验收人工注入失败', ended=time.time()); store.save(run)
                page.reload()
                card = page.locator(f'[data-team-run="{run["id"]}"]')
                expect(card).to_be_visible()
                with page.expect_response(lambda r:'/rerun?' in r.url and r.request.method=='POST') as response:
                    card.get_by_role('button', name='重新运行', exact=True).click()
                assert response.value.status == 202, response.value.text()
                current_id = response.value.json()['id']
                assert not page.locator('#modal').evaluate('(el)=>el.open')
                expect(card).to_have_count(0)
                current = page.locator(f'[data-team-run="{current_id}"]')
                expect(current).to_be_visible()
                expect(current.locator('.pill').first).to_contain_text('已完成', timeout=30000)
                expect(page.locator('#team-messages')).to_contain_text('分配任务')
                expect(page.locator('#team-messages')).to_contain_text('收到，开始处理')
                expect(page.locator('#team-messages')).to_contain_text('资料交接')
                assert page.locator('.team-chat-heading time[datetime]').count() > 5
                assert '→' in page.locator('#team-messages').inner_text()
                with closing(Store(root)) as store:
                    assert store.load(run['id'])['status']=='failed'
                    new = store.load(current_id)
                    assert new['status']=='succeeded' and len(new['artifacts']) > 0
                    store.bind_team(team_id)
                    for i in range(12):
                        store.message('group', 'researcher', f'补充资料 {i}：' + '这是一段用于验证聊天可读性与滚动位置的较长中文内容。' * 4)
                page.reload()
                expect(page.locator('#team-messages')).to_contain_text('补充资料 11')
                page.locator('#team-message').fill('保留这条未发送的草稿')
                feed = page.locator('#team-messages'); feed.evaluate('(el)=>el.scrollTop=0')
                with closing(Store(root)) as store:
                    store.bind_team(team_id); store.message('group', 'coder', '新进展：已经检查完毕。')
                expect(page.locator('#team-new-messages')).to_be_visible(timeout=10000)
                assert feed.evaluate('(el)=>el.scrollTop') < 20
                expect(page.locator('#team-message')).to_have_value('保留这条未发送的草稿')
                page.get_by_role('button', name='查看新消息 ↓').click()
                assert feed.evaluate('(el)=>el.scrollHeight-el.scrollTop-el.clientHeight') < 5
                for width, height, name in [(1440,1000,'desktop'), (820,1180,'tablet'), (390,844,'mobile')]:
                    page.set_viewport_size({'width':width,'height':height})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), name
                    widths = page.locator('.team-chat-body').evaluate_all('(els)=>els.map(el=>el.getBoundingClientRect().width)')
                    assert min(widths) > 150, (name,widths)
                    page.locator('#team-messages').evaluate('(el)=>el.scrollTop=350')
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(output/f'chat-{name}.png'), full_page=True, animations='disabled')
                page.set_viewport_size({'width':1440,'height':1000})
                page.goto(url + '/#task/' + run['id'])
                expect(page).to_have_url(url + '/#task/' + current_id)
                page.goto(url + '/#runs')
                expect(page.locator(f'[data-run="{run["id"]}"]')).to_have_count(0)
                expect(page.locator(f'[data-run="{current_id}"]')).to_have_count(1)
                page.goto(url + '/#chat')
                page.get_by_role('button', name='新建团队', exact=True).click()
                page.get_by_role('button', name='上传 Markdown', exact=True).click()
                page.locator('#team-document').set_input_files({'name':'invalid.md','mimeType':'text/markdown','buffer':b'No members here'})
                page.get_by_role('button', name='识别成员与职责').click()
                expect(page.locator('#team-import-status')).to_contain_text('未识别到成员')
                page.get_by_role('button', name='手动填写', exact=True).click()
                expect(page.locator('#team-import-panel')).to_be_hidden()
                assert not errors, errors
                browser.close()
                (output/'summary.json').write_text(json.dumps({'status':'passed','paid_model_calls':0,'failure':'explicitly injected browser fixture','workflow':'real offline execution completed with artifacts','checks':['Markdown upload + editable preview','parse error + manual fallback','one-click replacement + audit retained','actual assignment/acknowledgement/handoff','timestamp + recipient','stable scroll + draft','desktop/tablet/mobile widths','old URL routes to latest'],'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
                print('PASS: team UI features, offline workflow, responsive bubbles, Markdown preview, latest-attempt replacement')
        finally:
            server.should_exit=True; thread.join(timeout=15)


if __name__ == '__main__':
    main()
