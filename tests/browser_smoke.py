"""Real browser smoke test. Start `python -m evotask serve` first.

Uses the installed Microsoft Edge, creates an isolated browser context, and
never selects live mode or calls a paid model. Screenshots: test-results/web/.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--channel', default='msedge')
    args = parser.parse_args()
    output = Path('test-results/web')
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel, headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1050}, device_scale_factor=1)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url)
        expect(page.get_by_role('heading', name='一个想法，一支 AI 团队。')).to_be_visible()
        page.screenshot(path=str(output / 'desktop-home.png'), full_page=True, animations='disabled')
        page.get_by_role('button', name='启动协作演示').click()
        expect(page.get_by_role('button', name='确认通过')).to_be_visible(timeout=15000)
        run_url = page.url
        page.reload()
        expect(page.get_by_role('button', name='确认通过')).to_be_visible()
        page.locator('.artifact-name').filter(has_text='文本相似度报告').click()
        expect(page.locator('#modal .markdown')).to_contain_text('DEMO')
        expect(page.locator('#modal .markdown')).to_contain_text('doc_')
        with page.expect_download() as download:
            page.get_by_role('link', name='下载文件').click()
        assert download.value.suggested_filename.endswith('.md')
        assert '文本相似度报告' in download.value.suggested_filename
        page.get_by_role('button', name='关闭', exact=True).click()
        page.screenshot(path=str(output / 'desktop-review.png'), full_page=True, animations='disabled')
        page.get_by_role('button', name='确认通过').click()
        expect(page.locator('.detail-meta').first).to_contain_text('已完成', timeout=15000)
        page.screenshot(path=str(output / 'desktop-completed.png'), full_page=True, animations='disabled')
        page.get_by_role('link', name='知识库', exact=True).click()
        page.locator('#knowledge-file').set_input_files({
            'name': 'browser-evidence.md', 'mimeType': 'text/markdown',
            'buffer': '# Private nebula\nA private nebula source.\n<script>window.evoInjected=true</script>'.encode(),
        })
        page.locator('#document-scope').select_option('researcher')
        page.get_by_role('button', name='加入知识库').click()
        expect(page.locator('#document-list')).to_contain_text('browser-evidence.md')
        page.locator('#knowledge-query').fill('nebula')
        page.get_by_role('button', name='检索证据').click()
        expect(page.locator('#search-results')).to_contain_text('browser-evidence.md')
        assert not page.evaluate('Boolean(window.evoInjected)')
        page.screenshot(path=str(output / 'desktop-knowledge.png'), full_page=True, animations='disabled')
        page.locator('#search-agent').select_option('coder')
        page.get_by_role('button', name='检索证据').click()
        expect(page.locator('#search-results')).to_contain_text('未找到匹配片段')
        page.get_by_role('button', name='移除 browser-evidence.md').click()
        page.get_by_role('button', name='确认移除').click()
        expect(page.locator('#document-list')).not_to_contain_text('browser-evidence.md')
        page.get_by_role('link', name='Agent 对话', exact=True).click()
        page.locator('[data-channel="coder"]').click()
        page.locator('#chat-input').fill('请解释一下代码的验收流程。')
        page.get_by_role('button', name='发送消息').click()
        expect(page.locator('#chat-messages')).to_contain_text('[DEMO')
        page.screenshot(path=str(output / 'desktop-chat.png'), full_page=True, animations='disabled')
        page.locator('[data-channel="verifier"]').click()
        expect(page.locator('#chat-messages')).not_to_contain_text('请解释一下代码的验收流程。')
        page.keyboard.press('Control+k')
        page.locator('#command-input').fill('任务记录')
        page.locator('#command-input').press('Enter')
        expect(page.get_by_role('heading', name='每一步，都有迹可循。')).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 844})
        for route in ['home', 'runs', 'knowledge', 'chat']:
            page.goto(args.url + '/#' + route)
            expect(page.locator('#main h1')).to_be_visible()
            expect(page.locator('.loading-line')).to_have_count(0)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Overflow: ' + route
            page.screenshot(path=str(output / f'mobile-{route}.png'), full_page=True, animations='disabled')
        page.goto(run_url)
        expect(page.locator('.detail-meta').first).to_contain_text('已完成')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Task overflow'
        page.screenshot(path=str(output / 'mobile-task.png'), full_page=True, animations='disabled')
        page.get_by_role('button', name='展开导航').click()
        expect(page.locator('.sidebar')).to_have_class('sidebar is-open')
        page.get_by_role('link', name='工作台', exact=True).click()
        expect(page.get_by_role('heading', name='一个想法，一支 AI 团队。')).to_be_visible()
        assert not errors, errors
        print(json.dumps({'browser': args.channel, 'status': 'passed', 'screenshots': str(output),
                          'checks': ['demo', 'reload', 'approval', 'download', 'rag-scope', 'xss',
                                     'document-remove', 'private-chat', 'command-palette', 'mobile-layout'],
                          'page_errors': errors}, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
