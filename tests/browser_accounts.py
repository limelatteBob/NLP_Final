"""Exercise account UI with independent desktop/mobile browser cookie jars.

Uses no model calls. Run against an isolated local test server.
"""
import argparse
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8001')
    args = parser.parse_args()
    output = Path('test-results/web'); output.mkdir(parents=True, exist_ok=True)
    username = 'browser_' + uuid4().hex[:10]
    password = 'browser test password 123'
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        desktop = browser.new_context(viewport={'width': 1440, 'height': 1000})
        mobile = browser.new_context(viewport={'width': 390, 'height': 844})
        a, b = desktop.new_page(), mobile.new_page()
        for page in (a, b):
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(args.url + '/#knowledge')
            page.locator('#mode-select').select_option('live')
            expect(page.locator('#environment-name')).to_contain_text('真实模型')

        a.get_by_role('button', name='新建文档', exact=True).click()
        a.locator('#document-name').fill('cross-device-notes.md')
        a.locator('#document-source').fill('https://example.com/research')
        a.locator('#document-content').fill('# Research\nalpha evidence from guest')
        a.get_by_role('button', name='加入知识库').click()
        expect(a.locator('#document-list')).to_contain_text('cross-device-notes.md')
        a.get_by_role('button', name='账号与同步', exact=True).click()
        a.get_by_role('button', name='注册账号', exact=True).click()
        a.locator('#account-display').fill('林同学')
        a.locator('#account-username').fill(username)
        a.locator('#account-password').fill(password)
        a.locator('#account-confirm').fill(password)
        a.get_by_role('button', name='创建账号并登录').click()
        expect(a.locator('.account-summary')).to_contain_text('林同学', timeout=15000)
        expect(a.locator('.import-preview')).to_contain_text('1 份资料')
        a.get_by_role('button', name='导入到我的账号').click()
        expect(a.get_by_role('button', name='导入到我的账号')).to_be_disabled()
        a.get_by_role('button', name='返回工作空间').click()
        imported = a.locator('#active-base option').filter(has_text='访客导入').get_attribute('value')
        a.locator('#active-base').select_option(imported)
        expect(a.locator('#document-list')).to_contain_text('cross-device-notes.md')

        def login(page):
            if not page.locator('#account-username').is_visible():
                page.get_by_role('button', name='账号与同步', exact=True).click()
            page.locator('#account-username').fill(username)
            page.locator('#account-password').fill(password)
            page.get_by_role('button', name='登录并同步').click()
            expect(page.locator('.account-summary')).to_contain_text('林同学', timeout=15000)
            page.get_by_role('button', name='返回工作空间').click()

        login(b)
        b.locator('#active-base').select_option(imported)
        expect(b.locator('#document-list')).to_contain_text('cross-device-notes.md')
        assert b.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        with b.expect_download() as downloaded:
            b.get_by_role('link', name='下载原件 cross-device-notes.md').click()
        assert Path(downloaded.value.path()).read_text(encoding='utf-8') == '# Research\nalpha evidence from guest'

        # Edit on mobile, watch desktop polling pick up the server update.
        b.get_by_role('button', name='编辑 cross-device-notes.md', exact=True).click()
        b.locator('#document-description').fill('来自手机的更新')
        b.locator('#edit-document-content').fill('# Research\ndelta evidence synchronized')
        b.get_by_role('button', name='保存资料').click()
        expect(b.locator('#modal')).not_to_be_visible()
        expect(a.locator('#document-list')).to_contain_text('来自手机的更新', timeout=18000)
        a.locator('#knowledge-query').fill('delta')
        a.get_by_role('button', name='检索证据').click()
        expect(a.locator('.search-result')).to_contain_text('delta evidence synchronized')
        a.screenshot(path=str(output / 'desktop-account-sync.png'), full_page=True, animations='disabled')

        # A stale editor must not overwrite another device's edit.
        a.get_by_role('button', name='编辑 cross-device-notes.md', exact=True).click()
        b.get_by_role('button', name='编辑 cross-device-notes.md', exact=True).click()
        b.locator('#document-description').fill('并发更新已保留')
        b.get_by_role('button', name='保存资料').click()
        expect(b.locator('#modal')).not_to_be_visible()
        a.locator('#document-description').fill('stale editor')
        a.get_by_role('button', name='保存资料').click()
        expect(a.locator('#toast-region')).to_contain_text('其他页面或设备更新')
        expect(a.locator('#modal')).to_be_visible()
        a.get_by_role('button', name='关闭', exact=True).click()
        a.get_by_role('button', name='立即同步', exact=False).click()
        expect(a.locator('#document-list')).to_contain_text('并发更新已保留')

        a.get_by_role('button', name='账号与同步', exact=True).click()
        expect(a.locator('.account-device')).to_have_count(2)
        a.screenshot(path=str(output / 'desktop-account-panel.png'), full_page=True, animations='disabled')
        b.get_by_role('button', name='账号与同步', exact=True).click()
        b.screenshot(path=str(output / 'mobile-account-panel.png'), full_page=True, animations='disabled')
        assert b.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        b.get_by_role('button', name='返回工作空间').click()
        a.get_by_role('button', name='退出其他设备', exact=True).click()
        expect(a.locator('.account-device')).to_have_count(1)
        b.get_by_role('button', name='立即同步', exact=False).click()
        expect(b.locator('#account-username')).to_be_visible(timeout=15000)
        login(b)
        b.locator('#active-base').select_option(imported)

        # A password change rotates the current cookie and revokes the other device.
        a.get_by_text('修改密码', exact=True).click()
        a.locator('#current-password').fill(password)
        a.locator('#new-password').fill(password + 'new')
        a.locator('#confirm-password').fill(password + 'new')
        a.get_by_role('button', name='更新密码', exact=True).click()
        expect(a.locator('#toast-region')).to_contain_text('密码已更新', timeout=15000)
        password += 'new'
        b.reload()
        expect(b.locator('#account-username')).to_be_visible(timeout=15000)
        login(b)
        b.locator('#active-base').select_option(imported)
        tab = desktop.new_page()
        tab.on('pageerror', lambda error: errors.append(str(error)))
        tab.goto(args.url + '/#knowledge')
        tab.locator('#active-base').select_option(imported)
        expect(tab.locator('#document-list')).to_contain_text('cross-device-notes.md')
        a.get_by_role('button', name='退出此设备', exact=True).click()
        expect(a.get_by_role('button', name='账号与同步', exact=True)).to_contain_text('登录同步')
        expect(a.locator('#document-list')).not_to_contain_text('cross-device-notes.md')
        expect(tab.get_by_role('button', name='账号与同步', exact=True)).to_contain_text('登录同步')
        expect(tab.locator('#document-list')).not_to_contain_text('cross-device-notes.md')
        b.reload()
        b.locator('#active-base').select_option(imported)
        expect(b.locator('#document-list')).to_contain_text('并发更新已保留')
        assert not errors, errors
        desktop.close(); mobile.close(); browser.close()
    print('Account browser checks passed: registration, guest import, cross-context sync/download, conflict, devices, password, logout, mobile layout.')


if __name__ == '__main__':
    main()
