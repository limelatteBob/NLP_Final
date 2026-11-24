"""Real browser/API quality-feedback regression with explicit seeded approval fixtures; no model calls."""
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
from evotask.engine import Engine, task_from_spec
from evotask.storage import Store, AGENTS
from evotask.teams import Teams
from evotask.web.app import create_app
from tests.test_acceptance_p0 import spec


def main():
    output=Path('test-results/p0-workflow');output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[]
    with tempfile.TemporaryDirectory(prefix='p0-ui-') as temporary, socket.socket() as sock:
        state=Path(temporary)/'state';app=create_app(Config(provider='demo'),state)
        sock.bind(('127.0.0.1',0));url=f'http://127.0.0.1:{sock.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError('Server startup timeout')
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True,chromium_sandbox=True)
                context=browser.new_context(viewport={'width':1400,'height':1000})
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url+'/#chat');page.wait_for_selector('h1')
                sid=next(c['value'].split('.')[0] for c in context.cookies() if c['name']=='evotask_session')
                root=state/'sessions'/sid/'demo'
                with closing(Store(root)) as store:
                    team=Teams(store).create('验收返修界面测试',{a:{'name':a,'instructions':'独立执行并保留证据'} for a in AGENTS})
                # Execute the real resume/request_revision path, stopping before the next model turn.
                with patch.object(Engine,'advance',lambda self,run:run):
                    for surface in ('detail','team'):
                        for kind in ('acceptance','test_revision','check_revision'):
                            with closing(Store(root)) as store:
                                engine=Engine(store,Config(provider='demo'),None)
                                run=engine.new('修订审批功能夹具 '+surface+' '+kind,team_id=team['id'])
                                run['requirements']=[{'id':'R1','text':'持久化并阻止重复预订','priority':'must'}]
                                author=task_from_spec(spec());author.update(owner='coder',status='succeeded',title='预约实现')
                                qa=task_from_spec(spec('qa','verifier','test.py',['author'],[{'kind':'python','path':'test.py','targets':[{'task_id':'author','path':'app.py'}]}]))
                                qa.update(owner='verifier',status='running',title='独立验收');run['tasks']=[author,qa]
                                (store.workspace('coder',run['id'])/'app.py').write_bytes(b'def count(): return 3\n')
                                artifact=store.archive(run,'coder','app.py','author');artifact['verification']='verified';author['artifact_ids']=[artifact['id']]
                                engine.refresh_inputs(run,qa)
                                (store.workspace('verifier',run['id'])/'test.py').write_bytes(b'assert 1==1\n')
                                engine.wait(run,qa,kind,'检查真实业务规则，或提供具体修改意见。',{'type':'finish','artifacts':['test.py']},{'diff':'-旧预期\n+待审阅预期','artifacts':[]})
                                request_id=run['pending']['id']
                            page.goto(url+('/#task/'+run['id'] if surface=='detail' else '/#team/'+team['id']))
                            scope=page.locator('.review-card') if surface=='detail' else page.locator(f'[data-team-reply="{run["id"]}"]')
                            expect(scope).to_be_visible()
                            select=scope.locator('#quality-target' if surface=='detail' else '[name=quality-target]')
                            textarea=scope.locator('#quality-feedback' if surface=='detail' else '[name=quality-feedback]')
                            select.select_option('author');textarea.fill('实际取消后仍占用时段，请修复业务实现，保留正确测试。')
                            # One polling interval must not lose either the target or feedback draft.
                            if kind=='check_revision':page.wait_for_timeout(3500)
                            expect(select).to_have_value('author')
                            expect(textarea).to_have_value('实际取消后仍占用时段，请修复业务实现，保留正确测试。')
                            if kind=='check_revision':page.screenshot(path=str(output/(surface+'-feedback.png')),full_page=True)
                            with page.expect_response(lambda r:'/resume?' in r.url and r.request.method=='POST') as response:
                                scope.get_by_role('button',name='提出修改并继续',exact=True).click()
                            assert response.value.status==202,response.value.text()
                            deadline=time.monotonic()+10
                            while True:
                                with closing(Store(root)) as store:changed=store.load(run['id'])
                                if changed.get('revision_count')==1:break
                                if time.monotonic()>deadline:raise AssertionError(changed)
                                time.sleep(.05)
                            assert changed['pending'] is None
                            assert changed['revisions'][0]['target_task']=='author'
                            assert changed['revisions'][0]['origin']=='human_quality_feedback'
                            assert next(t for t in changed['tasks'] if t['id']=='author')['status']=='pending'
                            assert next(a for a in changed['artifacts'] if a['id']==artifact['id'])['verification']=='superseded'
                            csrf=context.request.get(url+'/api/session').json()['csrf']
                            old=context.request.post(url+'/api/runs/'+run['id']+'/resume?mode=demo',data={'request_id':request_id,'reply':'yes'},headers={'X-EvoTask-CSRF':csrf})
                            assert old.status==409,old.text()
                            checks.append(surface+'/'+kind+': exact target, draft preserved, real API revision, old approval rejected')
                assert not errors,errors
                browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=15)
    (output/'summary.json').write_text(json.dumps({'status':'passed','paid_model_calls':0,'fixture':'Seeded pending approvals; next model turn deliberately paused','checks':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: '+str(len(checks))+' browser/API quality-feedback paths')

if __name__=='__main__':main()
