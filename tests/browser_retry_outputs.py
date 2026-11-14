"""Browser regression with explicitly injected fixtures; never calls a paid model.
Run against a local test server, passing its --state-dir. Only creates a new browser session.
"""
import argparse
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from evotask.config import Config
from evotask.engine import Engine, task_from_spec
from evotask.retrieval import KnowledgeBase
from evotask.storage import Store


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8001')
    parser.add_argument('--state-dir',type=Path,required=True)
    args=parser.parse_args()
    output=Path('test-results/feature-browser');output.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1050})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(args.url);expect(page.locator('#main h1')).to_be_visible()
        sid=next(c['value'].split('.')[0] for c in context.cookies() if c['name']=='evotask_session')
        root=args.state_dir/'sessions'/sid/'demo'
        store=Store(root);KnowledgeBase(store).add(Path('evotask/web/data/similarity.md'))
        run=Engine(store,Config(provider='demo'),None).new('Browser retry fixture',memory=False,require_review=False)
        for task_id,kind,title in [('t1','deliverable','最终报告'),('t2','process','研究过程')]:
            task=task_from_spec({'id':task_id,'title':title,'objective':'fixture','role':'coder','checks':[{'kind':'exists','path':'report.md'}],
                                'outputs':[{'path':'report.md','title':title,'kind':kind,'order':1,'summary':title}]})
            task.update(owner='coder',status='succeeded');run['tasks'].append(task)
            path=store.workspace('coder',run['id'])/'report.md'
            path.write_text('older '+task_id,encoding='utf-8')
            old=store.archive(run,'coder','report.md',task_id);old['verification']='superseded'
            path.write_text('current '+task_id,encoding='utf-8')
            current=store.archive(run,'coder','report.md',task_id);current['verification']='verified'
            task['artifact_ids']=[current['id']]
            store.archive(run,'coder','report.md',task_id,origin='failure_snapshot')
        # Undeclared file is process data; three visible current files, two share report.md.
        (store.workspace('coder',run['id'])/'notes.md').write_text('process note',encoding='utf-8')
        store.archive(run,'coder','notes.md','t2',origin='failure_snapshot')
        run.update(status='failed',ended=time.time(),failure='人工注入的浏览器测试失败')
        store.save(run);store.close()
        page.goto(args.url+'/#task/'+run['id'])
        expect(page.locator('[data-artifact-group="deliverable"] .artifact-item')).to_have_count(1)
        expect(page.locator('[data-artifact-group="process"] .artifact-item')).to_have_count(2)
        for group in ('process','history'):
            assert not page.locator(f'[data-artifact-section="{group}"]').evaluate('(el)=>el.open')
        expect(page.locator('[data-artifact-group="deliverable"]')).to_contain_text('部分结果')
        page.locator('[data-artifact-section="history"] > summary').click()
        expect(page.locator('[data-artifact-section="history"] .artifact-item')).to_have_count(2)
        page.locator('[data-artifact-group="deliverable"] .artifact-name').click()
        title=page.locator('#modal-title').inner_text()
        with page.expect_download() as download:
            page.get_by_role('link',name='下载文件',exact=True).click()
        assert download.value.suggested_filename==title
        page.get_by_role('button',name='关闭',exact=True).click()
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        page.screenshot(path=str(output/'mobile-files.png'),full_page=True,animations='disabled')
        page.goto(args.url+'/#runs')
        button=page.locator(f'[data-retry-id="{run["id"]}"]')
        expect(button).to_be_visible()
        assert page.get_by_text('创建新任务，从头执行；真实模型将消耗新的 API 预算。',exact=True).count()==0
        with page.expect_response(lambda r:r.request.method=='POST' and '/retry?' in r.url) as response:
            button.click()
        assert response.value.status==202
        new_id=response.value.json()['id']
        assert new_id!=run['id']
        assert not page.locator('#modal').evaluate('(el)=>el.open')
        expect(page).to_have_url(args.url+'/#task/'+new_id)
        expect(page.locator('.detail-meta').first).to_contain_text('已完成',timeout=20000)
        expect(page.get_by_role('link',name='查看原失败任务')).to_be_visible()
        page.screenshot(path=str(output/'retry-completed.png'),full_page=True,animations='disabled')
        page.get_by_role('link',name='查看原失败任务').click()
        expect(page.get_by_role('button',name='重新运行',exact=True)).to_be_visible()
        expect(page.get_by_role('link',name='查看重新运行的任务 '+new_id[-6:])).to_be_visible()
        assert not errors,errors
        browser.close()
    print(json.dumps({'status':'passed','checks':['list-one-click-retry','no-confirmation','linked-runs','duplicate-snapshots','same-filenames','preview-download-name','collapsed-history-process','mobile'],'page_errors':errors}))


if __name__=='__main__':main()
