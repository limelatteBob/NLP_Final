"""Exercise actual demo collaboration and chat layout in Edge; no paid API calls."""
import argparse
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from evotask.storage import Store


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8001')
    parser.add_argument('--state-dir',type=Path,required=True)
    args=parser.parse_args()
    output=Path('test-results/conversations');output.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1100})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(args.url)
        expect(page.locator('#main h1')).to_be_visible()
        csrf=context.request.get(args.url+'/api/session').json()['csrf']
        response=context.request.post(args.url+'/api/runs',data={'review':False,'memory':False},headers={'X-EvoTask-CSRF':csrf})
        assert response.status==202,response.text()
        rid=response.json()['id']
        for _ in range(100):
            run=context.request.get(args.url+'/api/runs/'+rid).json()
            if not run['executing']:break
            page.wait_for_timeout(100)
        assert run['status']=='succeeded',run['status']
        sid=next(c['value'].split('.')[0] for c in context.cookies() if c['name']=='evotask_session')
        root=args.state_dir/'sessions'/sid/'demo'
        store=Store(root)
        store.message('group','human','浏览器测试注入：这是长中文文本布局检查。'+'请保持整段文字自然换行，不能每个字占一行。'*12)
        store.message('group','coordinator','浏览器测试注入：<script>window.chatInjected=true</script> '+'long_path_without_spaces_'*24)
        page.goto(args.url+'/#chat')
        expect(page.locator('[data-message-kind="assignment"]')).to_have_count(3)
        expect(page.locator('[data-message-kind="acknowledgement"]')).to_have_count(3)
        assignments=page.locator('[data-message-kind="assignment"]')
        expect(assignments.first).to_contain_text('Coordinator')
        expect(page.locator('[data-message-kind="acknowledgement"]').first).to_contain_text('收到')
        expect(page.locator('[data-message-kind="artifact_handoff"]').first).to_contain_text('已交接')
        assert page.locator('time[datetime]').count()>10
        assert not page.evaluate('Boolean(window.chatInjected)')
        first=assignments.first
        first.scroll_into_view_if_needed()
        assert first.locator('.message-bubble').bounding_box()['width']>250
        page.screenshot(path=str(output/'desktop-team.png'),full_page=True,animations='disabled')
        # New events arrive while the user is composing and reading older messages.
        page.locator('#chat-input').fill('尚未发送的中文草稿')
        page.locator('#chat-messages').evaluate('(el)=>el.scrollTop=0')
        store.message('group','researcher','浏览器测试注入：轮询新增消息，不应打断阅读。')
        expect(page.locator('#chat-messages')).to_contain_text('轮询新增消息',timeout=10000)
        expect(page.locator('#chat-input')).to_have_value('尚未发送的中文草稿')
        assert page.locator('#chat-messages').evaluate('(el)=>el.scrollTop')<30
        expect(page.locator('.chat-new-message')).to_be_visible()
        page.locator('.chat-new-message').click()
        assert page.locator('#chat-messages').evaluate('(el)=>el.scrollHeight-el.scrollTop-el.clientHeight')<3
        page.locator('#chat-input').fill('')
        page.locator('#chat-run-filter').select_option(rid)
        expect(page.locator('#chat-input')).to_be_disabled()
        expect(page.locator('#chat-messages')).not_to_contain_text('浏览器测试注入')
        expect(page.locator('[data-message-kind="assignment"]')).to_have_count(3)
        for width in (820,390):
            page.set_viewport_size({'width':width,'height':1000})
            page.locator('[data-message-kind="assignment"]').first.scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'),width
            assert page.locator('#chat-messages').evaluate('(el)=>el.scrollWidth<=el.clientWidth'),width
            assert page.locator('[data-message-kind="assignment"] .message-bubble').first.bounding_box()['width']>160,width
            page.screenshot(path=str(output/f'team-{width}.png'),full_page=True,animations='disabled')
        page.set_viewport_size({'width':1440,'height':1100})
        page.locator('[data-channel="coder"]').click()
        page.locator('#chat-input').fill('private-chat-regression')
        page.get_by_role('button',name='发送消息').click()
        expect(page.locator('#chat-messages')).to_contain_text('[DEMO',timeout=10000)
        expect(page.locator('time[datetime]')).to_have_count(2)
        page.locator('[data-channel="group"]').click()
        page.locator('#chat-run-filter').select_option('')
        expect(page.locator('#chat-messages')).not_to_contain_text('private-chat-regression')
        # Names are readable in API, preview and actual download, without archive IDs.
        artifact=run['artifact_groups']['deliverable'][0]
        assert 'art_' not in artifact['download_name']
        page.goto(args.url+'/#task/'+rid)
        page.locator('[data-artifact-group="deliverable"] .artifact-name').first.click()
        expect(page.locator('#modal-title')).to_have_text(artifact['download_name'])
        with page.expect_download() as download:page.get_by_role('link',name='下载文件',exact=True).click()
        assert download.value.suggested_filename==artifact['download_name']
        assert not errors,errors
        store.close();browser.close()
    print(json.dumps({'status':'passed','paid_model_calls':0,'checks':['actual-demo-assignment','acknowledgement','handoff','timestamps','readable-bubbles','draft-retained','scroll-retained','new-message-indicator','task-filter','private-isolation','xss','mobile-tablet','readable-downloads'],'page_errors':errors}))


if __name__=='__main__':main()
