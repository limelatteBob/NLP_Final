"""Real demo plus explicitly injected SSE/state fixtures; no paid model calls."""
import argparse
import copy
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from evotask.config import Config
from evotask.engine import Engine, task_from_spec
from evotask.storage import Store


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8001')
    parser.add_argument('--state-dir',type=Path,required=True)
    args=parser.parse_args()
    out=Path('test-results/network');out.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1100})
        context.add_init_script("window.__networkStreams=[]; const NativeSource=window.EventSource; window.EventSource=class extends NativeSource { constructor(...args) {super(...args);window.__networkStreams.push(this);} };")
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(args.url);expect(page.locator('#main h1')).to_be_visible()
        # Actual offline engine executes planning, delegation, assignment and file handoff.
        csrf=context.request.get(args.url+'/api/session').json()['csrf']
        response=context.request.post(args.url+'/api/runs',data={'review':False,'memory':False},headers={'X-EvoTask-CSRF':csrf})
        assert response.status==202,response.text()
        actual_id=response.json()['id']
        for _ in range(120):
            actual=context.request.get(args.url+'/api/runs/'+actual_id).json()
            if not actual['executing']:break
            page.wait_for_timeout(100)
        assert actual['status']=='succeeded'
        page.goto(args.url+'/#task/'+actual_id)
        host=page.locator('#collaboration-network')
        expect(host.locator('.network-node')).to_have_count(4)
        expect(host.locator('.network-summary')).to_have_text('任务已完成')
        assert host.locator('.network-edge-button').count()>=3
        assert host.locator('.network-node[data-status="running"]').count()==0
        host.screenshot(path=str(out/'actual-demo.png'),animations='disabled')
        # A separate record is labelled as an injected state fixture, never a model result.
        sid=next(c['value'].split('.')[0] for c in context.cookies() if c['name']=='evotask_session')
        store=Store(args.state_dir/'sessions'/sid/'demo')
        fixture=Engine(store,Config(provider='demo'),None).new('受控网络状态测试（人工注入，无模型调用）',memory=False)
        specs=[('t1','researcher','succeeded',[]),('t2','coder','running',['t1']),('t3','verifier','pending',['t2']),('t4','coder','pending',['t2'])]
        for tid,role,status,deps in specs:
            task=task_from_spec({'id':tid,'title':{'t1':'研究边界','t2':'实现代码','t3':'独立验证','t4':'补充说明'}[tid],
                                'objective':'受控测试：展示任务关系与状态。','role':role,'depends_on':deps,'checks':[{'kind':'exists','path':'result.md'}]})
            task.update(status=status,owner=role if tid!='t3' else None)
            fixture['tasks'].append(task)
        work=store.workspace('researcher',fixture['id']);(work/'notes.md').write_text('controlled evidence',encoding='utf-8')
        artifact=store.archive(fixture,'researcher','notes.md','t1');artifact['verification']='verified'
        fixture['tasks'][0]['artifact_ids']=[artifact['id']]
        path=store.handoff(fixture,'coder',artifact);fixture['tasks'][1]['inputs']=[path]
        fixture['status']='running';store.save(fixture)
        for tid,agent in [('t1','researcher'),('t2','coder')]:store.event(fixture['id'],agent,'task_assigned',{'task_id':tid})
        page.goto(args.url+'/#task/'+fixture['id'])
        host=page.locator('#collaboration-network')
        expect(host.locator('[data-network-agent="coder"] .network-status')).to_contain_text('执行已中断')
        base=context.request.get(args.url+'/api/runs/'+fixture['id']).json()
        seq=max(e['seq'] for e in store.events(fixture['id']))
        state=copy.deepcopy(base);state['executing']=True
        def push(run,events=None):
            page.evaluate("s=>window.__networkStreams.at(-1).dispatchEvent(new MessageEvent('snapshot',{data:JSON.stringify(s)}))",{'run':run,'events':events or []})
        push(state)
        coder=host.locator('[data-network-agent="coder"]')
        expect(coder).to_have_attribute('data-status','running')
        coder.click()
        expect(host.locator('.network-details')).to_contain_text('内部任务衔接')
        coder.focus()
        page.evaluate("window.__savedNode=document.querySelector('[data-network-agent=coder]');window.__savedAnimation=window.__savedNode.getAnimations()[0];")
        push(state)
        assert page.evaluate("window.__savedNode===document.querySelector('[data-network-agent=coder]') && window.__savedAnimation===window.__savedNode.getAnimations()[0] && document.activeElement===window.__savedNode")
        expect(coder).to_have_attribute('aria-expanded','true')
        # New event pulses once; repeated SSE and stale reconnect batches do not replay it.
        seq+=1
        fresh={'seq':seq,'run_id':fixture['id'],'agent':'coder','kind':'artifact_handoff','created':time.time(),'data':{'artifact_id':artifact['id'],'path':path}}
        push(state,[fresh])
        pulse=host.locator('g[data-network-edge="researcher:coder"] .relation-pulse')
        assert pulse.evaluate('(el)=>el.getAnimations().length')==1
        pulse.evaluate('(el)=>window.__savedPulse=el.getAnimations()[0]')
        push(state,[fresh])
        assert pulse.evaluate('(el)=>el.getAnimations()[0]===window.__savedPulse')
        page.wait_for_timeout(1400)
        assert pulse.evaluate('(el)=>el.getAnimations().length')==0
        stale=copy.deepcopy(fresh);stale.update(seq=seq+1,created=time.time()-60);seq+=1
        push(state,[stale]);assert pulse.evaluate('(el)=>el.getAnimations().length')==0
        page.evaluate("window.__networkStreams.at(-1).dispatchEvent(new Event('error'))")
        expect(host.locator('.network-summary')).to_contain_text('等待同步')
        assert coder.evaluate('(el)=>getComputedStyle(el).animationPlayState')=='paused'
        push(state,[stale]);expect(host.locator('.network-summary')).to_have_text('实时协作')
        # SVG arrows are keyboard-operable; relation cards contain actual source and time.
        group=host.locator('g[data-network-edge="researcher:coder"]')
        group.focus();page.keyboard.press('Enter')
        expect(host.locator('.network-details')).to_contain_text('notes.md')
        expect(host.locator('.network-details')).to_contain_text('任务依赖')
        expect(group).to_have_attribute('aria-expanded','true')
        push(state);expect(group).to_have_attribute('aria-expanded','true')
        coder.click();host.locator('[data-network-task="t2"]').first.click()
        expect(page.locator('.task-item[data-task="t2"]')).to_have_attribute('open','')
        host.locator('[data-action="memory"]').click()
        expect(page.locator('#modal')).to_be_visible()
        page.get_by_role('button',name='关闭',exact=True).click()
        # Selected planned relation follows the actual owner's assignment, rather than closing.
        host.locator('.network-edge-button[data-network-edge="coder:verifier"]').click()
        reassigned=copy.deepcopy(state);reassigned['tasks'][2].update(owner='researcher',status='running')
        push(reassigned)
        expect(host.locator('.network-detail-heading')).to_contain_text('Coder → Researcher')
        expect(host.locator('.network-edge-button[data-network-edge="coder:researcher"]')).to_have_attribute('aria-expanded','true')
        push(state)
        expect(host.locator('.network-detail-heading')).to_contain_text('Coder → Verifier')
        # All pause/stop states suppress work animation, child waits do not light up parents.
        waiting=copy.deepcopy(state);waiting['status']='waiting_human';waiting['executing']=False
        waiting['pending']={'id':'request_fixture','task_id':'t2','kind':'acceptance','question':'受控人工确认','created':time.time()}
        waiting['tasks'][1]['status']='waiting_human';push(waiting)
        expect(coder).to_have_attribute('data-status','waiting_human')
        assert host.locator('.network-node[data-status="running"]').count()==0
        child=copy.deepcopy(state);child['tasks'][1]['status']='waiting_child';child['tasks'][2].update(status='running',owner='verifier');push(child)
        expect(coder).to_have_attribute('data-status','waiting_child')
        expect(host.locator('[data-network-agent="verifier"]')).to_have_attribute('data-status','running')
        for status in ('failed','cancelled','succeeded'):
            stopped=copy.deepcopy(state);stopped.update(status=status,executing=False)
            push(stopped);assert host.locator('.network-node[data-status="running"]').count()==0
        push(state)
        page.emulate_media(reduced_motion='reduce')
        assert coder.evaluate('(el)=>getComputedStyle(el).animationName')=='none'
        seq+=1;fresh.update(seq=seq,created=time.time());push(state,[fresh])
        assert pulse.evaluate('(el)=>el.getAnimations().length')==0
        page.emulate_media(reduced_motion='no-preference')
        coder.click()
        # Direction and layout survive resize; long details do not widen the page.
        for width,height in [(1440,1100),(820,1050),(390,950)]:
            page.set_viewport_size({'width':width,'height':height})
            host.scroll_into_view_if_needed();page.wait_for_timeout(100)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            nodes=host.locator('.network-node').all()
            rects=[n.bounding_box() for n in nodes]
            network_box=host.bounding_box()
            assert network_box['width']>=280,(width,network_box)
            for r in rects:assert r['x']>=network_box['x'] and r['x']+r['width']<=network_box['x']+network_box['width'],(width,r,network_box)
            for i,a in enumerate(rects):
                for b in rects[i+1:]:assert a['x']+a['width']<=b['x'] or b['x']+b['width']<=a['x'] or a['y']+a['height']<=b['y'] or b['y']+b['height']<=a['y'],width
            assert host.locator('.relation-base').evaluate_all("els=>els.every(el=>el.getAttribute('marker-end') && !/NaN|Infinity/.test(el.getAttribute('d')))")
            if width==390:assert host.locator('.network-stage').evaluate("el=>getComputedStyle(el).getPropertyValue('--network-vertical').trim()")=='1'
            host.locator('.network-details').evaluate('(el)=>el.scrollTop=0')
            host.screenshot(path=str(out/f'network-{width}.png'),animations='disabled')
        fixture.update(status='cancelled',ended=time.time())
        for task in fixture['tasks']:
            if task['status'] not in {'succeeded','failed','cancelled'}:task['status']='cancelled'
        store.save(fixture)
        store.event(fixture['id'],'coordinator','validation_fixture_finished',{'model_calls':0})
        store.close()
        # Returning to history reconstructs its static graph; the homepage remains overview-only.
        page.goto(args.url+'/#task/'+actual_id)
        expect(page.locator('.network-summary')).to_have_text('任务已完成')
        assert page.locator('.relation-pulse').evaluate_all('els=>els.every(el=>el.getAnimations().length===0)')
        page.goto(args.url+'/#home')
        expect(page.locator('.launch-grid .team-visual')).to_have_count(1)
        assert page.locator('#collaboration-network').count()==0
        assert not errors,errors
        browser.close()
    print(json.dumps({'status':'passed','paid_model_calls':0,'checks':['actual-demo','real-owner-arrows','internal-task-links','node-dom-and-animation-retention','selection-retention','pulse-once','stale-event-no-replay','reconnection','keyboard-arrow','task-location','memory-entry','human-pause','child-wait','terminal-states','reduced-motion','desktop-tablet-mobile','static-home'],'page_errors':errors}))


if __name__=='__main__':main()
