"""Personal knowledge CRUD and task selection in Edge. Model submission is intercepted."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    output = Path('test-results/web')
    output.mkdir(parents=True, exist_ok=True)
    errors, submitted = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        # Block actual task submission even if a later assertion/test fails.
        def intercept(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(status=422, content_type='application/json', body=json.dumps({'detail': '测试已捕获选库参数，未调用模型'}))
        page.route('**/api/runs?mode=live', intercept)
        page.goto(args.url + '/#knowledge')
        page.locator('#mode-select').select_option('live')
        expect(page.locator('#environment-name')).to_contain_text('真实模型')
        expect(page.locator('#active-base')).to_be_visible()
        page.get_by_role('button', name='新建知识库', exact=True).click()
        page.locator('#base-name').fill('TypeMate 市场研究')
        page.locator('#base-description').fill('产品定位、用户访谈与可追溯的市场资料。')
        page.get_by_role('button', name='保存知识库').click()
        expect(page.locator('#active-base option:checked')).to_have_text('TypeMate 市场研究')
        first = page.locator('#active-base').input_value()
        page.get_by_role('button', name='新建文档', exact=True).click()
        page.locator('#document-name').fill('market-notes.md')
        page.locator('#document-source').fill('https://example.com/market')
        page.locator('#document-description').fill('用户提供的研究材料；需核实统计口径。')
        page.locator('#document-content').fill('# Market notes\nalpha evidence')
        page.get_by_role('button', name='加入知识库').click()
        expect(page.locator('#document-list')).to_contain_text('market-notes.md')
        page.get_by_role('button', name='编辑 market-notes.md', exact=True).click()
        page.locator('#edit-document-content').fill('# Market notes\ndelta evidence')
        page.locator('#document-source').fill('https://example.com/revised')
        page.locator('#document-scope').select_option('researcher')
        # Hold the post-save refresh to verify the editor stays open until the
        # underlying page is ready; otherwise a freshly entered query is lost.
        page.evaluate('''() => {
            const original = window.fetch.bind(window);
            let hold = true;
            window.fetch = async (...args) => {
                const response = await original(...args);
                if (hold && String(args[0]).includes('/api/workspace')) {
                    hold = false;
                    document.body.setAttribute('data-test-refresh-pending', 'true');
                    await new Promise(resolve => { window.__releaseKnowledgeRefresh = resolve; });
                    document.body.removeAttribute('data-test-refresh-pending');
                }
                return response;
            };
        }''')
        page.get_by_role('button', name='保存资料').click()
        expect(page.locator('body')).to_have_attribute('data-test-refresh-pending', 'true')
        expect(page.locator('#modal')).to_be_visible()
        page.evaluate('window.__releaseKnowledgeRefresh()')
        expect(page.locator('#modal')).not_to_be_visible()
        page.locator('#knowledge-query').fill('delta')
        page.get_by_role('button', name='检索证据').click()
        expect(page.locator('.search-result')).to_contain_text('delta evidence')
        expect(page.locator('.search-result a')).to_have_attribute('href', 'https://example.com/revised')
        page.locator('#search-agent').select_option('coder')
        page.get_by_role('button', name='检索证据').click()
        expect(page.locator('#search-results')).to_contain_text('未找到匹配片段')
        page.locator('#search-agent').select_option('researcher')
        page.get_by_role('button', name='检索证据').click()
        expect(page.locator('.search-result')).to_be_visible()
        page.screenshot(path=str(output / 'desktop-personal-knowledge.png'), full_page=True, animations='disabled')
        page.get_by_role('button', name='编辑知识库', exact=True).click()
        page.locator('#base-name').fill('TypeMate 项目资料')
        page.get_by_role('button', name='保存知识库').click()
        expect(page.locator('#active-base option:checked')).to_have_text('TypeMate 项目资料')
        page.get_by_role('button', name='新建知识库', exact=True).click()
        page.locator('#base-name').fill('长期研究资料')
        page.get_by_role('button', name='保存知识库').click()
        expect(page.locator('#active-base option:checked')).to_have_text('长期研究资料')
        second = page.locator('#active-base').input_value()
        page.locator('#active-base').select_option(first)
        page.get_by_role('button', name='编辑 market-notes.md', exact=True).click()
        page.locator('#document-base').select_option(second)
        page.get_by_role('button', name='保存资料').click()
        expect(page.locator('#active-base')).to_have_value(second)
        expect(page.locator('#document-list')).to_contain_text('market-notes.md')
        page.locator('#active-base').select_option(first)
        expect(page.locator('#document-list')).not_to_contain_text('market-notes.md')
        page.get_by_role('button', name='删除知识库', exact=True).click()
        page.get_by_role('button', name='确认删除知识库', exact=True).click()
        expect(page.locator('#active-base option')).to_have_count(2)
        page.reload()
        page.locator('#active-base').select_option(second)
        expect(page.locator('#document-list')).to_contain_text('market-notes.md')
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(output / 'mobile-personal-knowledge.png'), full_page=True, animations='disabled')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.goto(args.url + '/#home')
        page.locator('.task-knowledge summary').click()
        for field in page.locator('.task-knowledge input').all():
            field.uncheck()
        page.locator(f'[data-base="{second}"]').check()
        page.locator('#goal').fill('根据选中的知识库整理研究资料。')
        page.screenshot(path=str(output / 'desktop-task-knowledge.png'), full_page=True, animations='disabled')
        page.get_by_role('button', name='启动任务', exact=False).click()
        expect(page.locator('#toast-region')).to_contain_text('未调用模型')
        assert len(submitted) == 1 and submitted[0]['knowledge_base_ids'] == [second], submitted
        page.goto(args.url + '/#knowledge')
        page.locator('#active-base').select_option(second)
        page.get_by_role('button', name='删除知识库', exact=True).click()
        expect(page.locator('#modal')).to_contain_text('1 份资料')
        expect(page.locator('.deletion-preview')).to_contain_text('market-notes.md')
        page.get_by_role('button', name='取消', exact=True).click()
        expect(page.locator('#document-list')).to_contain_text('market-notes.md')
        page.get_by_role('button', name='删除知识库', exact=True).click()
        page.screenshot(path=str(output / 'delete-knowledge-confirmation.png'), full_page=True, animations='disabled')
        page.get_by_role('button', name='确认删除知识库', exact=True).click()
        expect(page.locator('#active-base option')).to_have_count(1)
        expect(page.locator('#document-list')).not_to_contain_text('market-notes.md')
        page.goto(args.url + '/#home')
        expect(page.locator('.task-knowledge summary')).to_contain_text('0 个已选')
        expect(page.locator(f'[data-base="{second}"]')).to_have_count(0)
        page.goto(args.url + '/#knowledge')
        page.get_by_role('button', name='新建文档', exact=True).click()
        page.locator('#document-name').fill('temporary.md')
        page.locator('#document-content').fill('clearword evidence')
        page.get_by_role('button', name='加入知识库').click()
        expect(page.locator('#document-list')).to_contain_text('temporary.md')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.get_by_role('button', name='清空知识库', exact=True).click()
        expect(page.locator('#modal')).to_contain_text('默认知识库本身会保留')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(output / 'mobile-clear-knowledge.png'), full_page=True, animations='disabled')
        page.get_by_role('button', name='确认清空知识库', exact=True).click()
        expect(page.locator('#document-list')).not_to_contain_text('temporary.md')
        page.reload()
        expect(page.locator('#active-base option')).to_have_count(1)
        expect(page.locator('#document-list')).not_to_contain_text('temporary.md')
        assert not errors, errors
        print(json.dumps({'status': 'passed', 'checks': ['base-crud', 'document-edit', 'source-url', 'scope', 'move',
                            'reload', 'mobile', 'task-selection', 'delete-populated-base', 'cancel-delete',
                            'prune-deleted-selection', 'clear-default'], 'real_model_calls': 0, 'page_errors': errors}))
        browser.close()


if __name__ == '__main__':
    main()
