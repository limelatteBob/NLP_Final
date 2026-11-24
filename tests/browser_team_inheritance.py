"""Browser -> real API/jobs/Engine -> selected input consumption, using a deterministic offline provider."""
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
from evotask.storage import Store, AGENTS, digest
from evotask.web.app import create_app
from tests.test_teams import MuseumProvider
from tests.test_team_ui_features import DOCUMENT


def main():
    output=Path('test-results/team-inheritance');output.mkdir(parents=True,exist_ok=True)
    config=Config(provider='demo');errors=[]
    with tempfile.TemporaryDirectory(prefix='team-inheritance-') as temporary, socket.socket() as sock:
        state=Path(temporary)/'state';app=create_app(config,state)
        sock.bind(('127.0.0.1',0));url=f'http://127.0.0.1:{sock.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError('Server startup timeout')
                time.sleep(.05)
            with patch('evotask.web.app.make_provider',side_effect=lambda cfg:MuseumProvider()), sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True,chromium_sandbox=True)
                try:
                    context=browser.new_context(viewport={'width':1440,'height':1000})
                    page=context.new_page();page.set_default_timeout(10000);page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(url+'/#chat')
                    page.get_by_role('button',name='新建团队',exact=True).click()
                    page.get_by_role('button',name='上传 Markdown',exact=True).click()
                    page.locator('#team-document').set_input_files({'name':'team.md','mimeType':'text/markdown','buffer':DOCUMENT.encode()})
                    page.get_by_role('button',name='识别成员与职责').click()
                    expect(page.locator('#team-import-status')).to_contain_text('已识别 4 位成员')
                    page.get_by_role('button',name='创建团队',exact=True).click()
                    expect(page.locator('h1')).to_have_text('示例项目团队')
                    team_id=page.url.split('/')[-1]
                    sid=next(c['value'].split('.')[0] for c in context.cookies() if c['name']=='evotask_session')
                    root=state/'sessions'/sid/'demo'

                    # Generate and accept the source using the real engine, not a fake successful response.
                    with closing(Store(root)) as store:
                        engine=Engine(store,config,MuseumProvider())
                        source=engine.advance(engine.new('Create museum guide',team_id=team_id,require_review=True))
                        source=engine.resume(source['id'],source['pending']['id'],'Ceramics')
                        source=engine.resume(source['id'],source['pending']['id'],'yes')
                        assert source['status']=='succeeded',source
                        current_ids={aid for task in source['tasks'] for aid in task['artifact_ids']}
                        selected=next(a for a in source['artifacts'] if a['id'] in current_ids and a['path']=='museum/data.json')
                        content=store.artifact_bytes(selected)
                        assert json.loads(content)=={'visitors':37}
                        # Explicit historical-version fixture: verified, but absent from the current task manifest.
                        source_path=store.workspace('researcher',source['id'])/selected['path']
                        source_path.write_bytes(b'{"visitors":99}')
                        old=store.archive(source,'researcher',selected['path'],'guide')
                        old['verification']='verified';source_path.write_bytes(content);store.save(source)
                        original_state=json.loads(json.dumps(source))
                        original_bytes={a['id']:store.artifact_bytes(a) for a in source['artifacts']}

                    page.reload()
                    page.evaluate("window.testToasts=[];new MutationObserver(()=>window.testToasts.push(document.querySelector('#toast-region').innerText)).observe(document.querySelector('#toast-region'),{childList:true,subtree:true})")
                    card=page.locator(f'[data-team-run="{source["id"]}"]')
                    card.get_by_role('button',name='基于这些文件发起新任务',exact=True).click()
                    form=page.locator('#team-inherit-form');expect(form).to_be_visible()
                    checked=set(form.locator('[name=artifact]:checked').evaluate_all('(els)=>els.map(el=>el.value)'))
                    assert checked==current_ids,(checked,current_ids)
                    expect(form.locator(f'[value="{old["id"]}"]')).not_to_be_checked()
                    goal='基于上一轮 museum/data.json 将 visitors 增加 1，保留原任务文件。'
                    form.locator('#team-next-goal').fill(goal)
                    expect(page).to_have_url(url+'/#team/'+team_id)
                    page.screenshot(path=str(output/'01-file-picker.png'),full_page=True)
                    for checkbox in form.locator('[name=artifact]').all():checkbox.uncheck()
                    expect(page).to_have_url(url+'/#team/'+team_id)
                    form.get_by_role('button',name='创建并启动新任务',exact=True).click()
                    expect(page.get_by_text('请至少选择一个归档文件。',exact=True)).to_be_visible()
                    expect(form).to_be_visible()
                    expect(form.locator('#team-next-goal')).to_have_value(goal)

                    # An obsolete version is rejected; the worker slot must be released for the valid request.
                    form.locator(f'[value="{old["id"]}"]').check()
                    with page.expect_response(lambda r:'/api/runs?' in r.url and r.request.method=='POST') as rejected:
                        form.get_by_role('button',name='创建并启动新任务',exact=True).click()
                    assert rejected.value.status==422,rejected.value.text()
                    expect(form.get_by_role('button',name='创建并启动新任务',exact=True)).to_be_enabled()
                    expect(form.locator('#team-next-goal')).to_have_value(goal)
                    assert len(context.request.get(url+'/api/teams/'+team_id+'?mode=demo').json()['runs'])==1
                    form.locator(f'[value="{old["id"]}"]').uncheck()
                    form.locator(f'[value="{selected["id"]}"]').check()
                    with page.expect_response(lambda r:'/api/runs?' in r.url and r.request.method=='POST') as created:
                        form.get_by_role('button',name='创建并启动新任务',exact=True).click()
                    assert created.value.status==202,created.value.text()
                    body=created.value.request.post_data_json
                    assert body['source_run_id']==source['id'] and body['artifact_ids']==[selected['id']],body
                    assert body['team_id']==team_id and body['goal']==goal
                    child_id=created.value.json()['id'];assert child_id!=source['id']
                    expect(page.locator('#modal')).not_to_be_visible()

                    def idle():
                        deadline=time.monotonic()+20
                        while True:
                            response=context.request.get(url+'/api/runs/'+child_id+'?mode=demo')
                            assert response.status==200,response.text()
                            data=response.json()
                            if not data['executing']:return data
                            if time.monotonic()>deadline:raise AssertionError(data)
                            time.sleep(.05)
                    child=idle();assert child['pending']['kind']=='planning',child
                    current=page.locator(f'[data-team-run="{child_id}"]')
                    expect(current).to_be_visible()
                    current.locator('textarea[name=reply]').fill('Ceramics')
                    with page.expect_response(lambda r:'/resume?' in r.url and r.request.method=='POST') as resumed:
                        current.get_by_role('button',name='回复并继续',exact=True).click()
                    assert resumed.value.status==202,resumed.value.text()
                    child=idle();assert child['pending']['kind']=='acceptance',child
                    expect(current.get_by_role('button',name='确认通过',exact=True)).to_be_visible()
                    page.screenshot(path=str(output/'02-new-task-review.png'),full_page=True)
                    with page.expect_response(lambda r:'/resume?' in r.url and r.request.method=='POST') as approved:
                        current.get_by_role('button',name='确认通过',exact=True).click()
                    assert approved.value.status==202,approved.value.text()
                    child=idle();assert child['status']=='succeeded',child
                    expect(current.locator('.pill').first).to_contain_text('已完成')
                    expect(page.locator('#toast-region .toast')).to_have_count(0,timeout=10000)
                    page.screenshot(path=str(output/'03-completed.png'),full_page=True)
                    with closing(Store(root)) as store:
                        result=store.load(child_id);inherited=result['inherited_inputs']
                        assert len(inherited)==1 and inherited[0]['artifact_id']==selected['id']
                        assert inherited[0]['source_run_id']==source['id'] and inherited[0]['sha256']==digest(content)
                        for agent in AGENTS:
                            directory=store.workspace(agent,child_id)/'inputs/previous'
                            assert [f.relative_to(directory).as_posix() for f in directory.rglob('*') if f.is_file()]==['museum/data.json']
                            assert (directory/'museum/data.json').read_bytes()==content
                        task=result['tasks'][0]
                        reads=[h for h in task['history'] if h.get('action',{}).get('name')=='read_file']
                        assert any(json.loads(h['result']['content'])=={'visitors':37} for h in reads),reads
                        artifact=next(a for a in result['artifacts'] if a['id'] in task['artifact_ids'] and a['path']=='museum/data.json')
                        assert json.loads(store.artifact_bytes(artifact))=={'visitors':38}
                        assert store.load(source['id'])==original_state
                        assert {a['id']:store.artifact_bytes(a) for a in source['artifacts']}==original_bytes
                        (output/'run.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                        (output/'events.json').write_text(json.dumps(store.events(child_id),ensure_ascii=False,indent=2),encoding='utf-8')
                    assert not errors,errors
                    summary={'status':'passed','paid_model_calls':0,'provider':'deterministic MuseumProvider fixture; real browser/API/jobs/Engine/storage',
                        'source_run_id':source['id'],'child_run_id':child_id,'selected_artifact':selected['id'],'input_visitors':37,'output_visitors':38,
                        'checks':['picker opens without JS error','current versions selected by default','empty selection blocked',
                                  'stale selection rejected and draft retained','valid request starts after rejection','only selected bytes and path inherited',
                                  'agent actually reads inherited data','clarification and approval through UI','child succeeds','source state and artifacts unchanged'],
                        'page_errors':errors}
                    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
                    print('PASS: browser selection -> real task startup -> inherited data read -> approval -> completion (37 -> 38; source unchanged)')
                except Exception:
                    if 'page' in locals():
                        print('UI error:',page.locator('#toast-region').inner_text())
                        print('Page errors:',errors)
                        print('Toast history:',page.evaluate('window.testToasts'))
                        if page.locator('#team-inherit-form').count():print('Form:',page.locator('#team-inherit-form').evaluate('(el)=>({valid:el.checkValidity(),selected:[...el.querySelectorAll("input:checked")].map(e=>e.value),goal:el.querySelector("textarea").value})'))
                        page.screenshot(path=str(output/'failure.png'),full_page=True)
                    raise
                finally:browser.close()
        finally:server.should_exit=True;thread.join(timeout=15)

if __name__=='__main__':main()
