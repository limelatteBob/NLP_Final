"""Fresh retry isolation and output identity/acceptance regressions."""
import copy
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from evotask.config import Config
from evotask.engine import Engine
from evotask.outputs import describe_artifacts, grouped_artifacts, validate_outputs, validate_output_contract
from evotask.retrieval import KnowledgeBase
from evotask.storage import Store
try:
    from fastapi.testclient import TestClient
    from evotask.web.app import create_app
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, 'Install .[web,test-web] for HTTP tests')
class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(provider='demo', model='current', max_model_calls=80)
        self.app = create_app(self.config, Path(self.temp.name))
        self.client = TestClient(self.app).__enter__()
        session = self.client.get('/api/session').json()
        self.client.headers['X-EvoTask-CSRF'] = session['csrf']
        self.client.get('/api/workspace')
        self.root = next(Path(self.temp.name).glob('sessions/*/demo'))

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def failed(self, reason='Injected execution failure', inputs=False):
        store = Store(self.root)
        files = []
        if inputs:
            source = Path(self.temp.name) / '用户_资料.md'
            source.write_text('original user evidence', encoding='utf-8')
            files.append(source)
        run = Engine(store, replace(self.config, max_model_calls=1), None).new(
            'Produce the demo report', files, memory=False, require_review=True, knowledge_base_ids=[])
        run.update(status='failed', failure=reason, model_calls=1, steps=99, ended=time.time(),
                   clarifications=[{'question':'audience?', 'answer':'beginners'}])
        store.save(run)
        store.close()
        return run

    def retry(self, run, key='request-0000000001'):
        return self.client.post('/api/runs/' + run['id'] + '/retry', json={'request_key':key})

    def idle(self, rid):
        for _ in range(240):
            run = self.client.get('/api/runs/' + rid).json()
            if not run['executing']:
                return run
            time.sleep(.025)
        self.fail('Worker did not stop')

    def test_reset_current_config_inputs_and_options_for_all_failure_types(self):
        for reason in ('planning failed', 'API unavailable', 'execution error', 'model budget exhausted'):
            with self.subTest(reason=reason):
                old = self.failed(reason, inputs=True)
                with patch.object(self.app.state.jobs, 'submit'):
                    response = self.retry(old)
                self.assertEqual(response.status_code, 202, response.text)
                child = response.json()
                store = Store(self.root)
                fresh = store.load(child['id'])
                parent = store.load(old['id'])
                self.assertEqual(fresh['retry_of'], old['id'])
                self.assertEqual(fresh['retry_root'], old['id'])
                self.assertEqual(fresh['config']['max_model_calls'], 80)
                self.assertEqual(fresh['config']['model'], 'deterministic-fixture')
                for key in ('goal','memory_enabled','require_review','knowledge_base_ids','clarifications','initial_inputs'):
                    self.assertEqual(fresh[key], old[key], key)
                for key in ('steps','model_calls','active_seconds'):
                    self.assertEqual(fresh[key], 0)
                self.assertEqual(fresh['tasks'], [])
                self.assertEqual(fresh['artifacts'], [])
                self.assertIsNone(fresh['pending'])
                self.assertEqual(parent['status'], 'failed')
                for key in ('tasks','artifacts','failure','model_calls'):
                    self.assertEqual(parent[key], old[key])
                for agent in ('coordinator','coder','researcher','verifier'):
                    self.assertEqual((store.workspace(agent,fresh['id']) / fresh['initial_inputs'][0]).read_text(), 'original user evidence')
                store.close()
                self.app.state.jobs.release(self.root)

    def test_idempotency_concurrent_tabs_and_persistent_replay(self):
        old = self.failed()
        with patch.object(self.app.state.jobs, 'submit') as submit:
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda n: self.retry(old, 'different-key-000'+str(n)), range(3)))
            self.assertEqual([r.status_code for r in results], [202]*3)
            ids = {r.json()['id'] for r in results}
            self.assertEqual(len(ids),1)
            self.assertEqual(submit.call_count,1)
        rid = ids.pop()
        self.app.state.jobs.release(self.root)
        store = Store(self.root)
        child = store.load(rid); child['status']='failed'; store.save(child)
        # A later repair of the source does not invalidate an already committed key.
        old['status']='succeeded'; store.save(old); store.close()
        # Recreate application: the key survives a service restart and terminal child.
        with TestClient(create_app(self.config,Path(self.temp.name))) as restarted:
            restarted.cookies.update(self.client.cookies)
            session = restarted.get('/api/session').json()
            result = restarted.post('/api/runs/'+old['id']+'/retry',json={'request_key':'different-key-0000'},headers={'X-EvoTask-CSRF':session['csrf']})
            self.assertEqual(result.json()['id'],rid)

    def test_security_and_nonfailed_source(self):
        old = self.failed(); url='/api/runs/'+old['id']+'/retry'
        body={'request_key':'security-key-0001'}
        self.assertEqual(self.client.post(url,json=body,headers={'X-EvoTask-CSRF':'bad'}).status_code,403)
        self.assertEqual(self.client.post(url+'?mode=live',json=body).status_code,404)
        other=TestClient(self.app)
        session=other.get('/api/session').json()
        self.assertEqual(other.post(url,json=body,headers={'X-EvoTask-CSRF':session['csrf']}).status_code,404)
        other.close()
        store=Store(self.root); old['status']='running';store.save(old);store.close()
        self.assertEqual(self.retry(old).status_code,409)

    def test_missing_input_deleted_kb_and_still_active_source(self):
        old=self.failed(inputs=True)
        store=Store(self.root)
        (store.workspace('coordinator',old['id'])/old['initial_inputs'][0]).unlink()
        response=self.retry(old)
        self.assertEqual(response.status_code,422,response.text)
        self.assertIn('输入资料缺失',response.text)
        self.assertFalse(self.app.state.jobs.info(self.root)['busy'])
        old['initial_inputs']=[];old['input_manifest']=[];old['knowledge_base_ids']=['kb_deleted'];store.save(old)
        self.assertEqual(self.retry(old).status_code,422)
        self.assertFalse(self.app.state.jobs.info(self.root)['busy'])
        self.app.state.jobs.reserve(self.root,old['id'])
        self.assertEqual(self.retry(old).status_code,409)
        self.app.state.jobs.release(self.root)
        self.assertEqual(len(store.runs()),1)
        store.close()

    def test_attachment_original_and_copy_failure_cleanup(self):
        old=self.failed(inputs=True)
        upload=self.client.post('/api/uploads?name=source.txt&purpose=task',content=b'original attachment')
        self.assertEqual(upload.status_code,201,upload.text)
        store=Store(self.root)
        old['uploaded_files']=[upload.json()];store.save(old)
        with patch.object(self.app.state.jobs,'submit'):
            response=self.retry(old)
        self.assertEqual(response.status_code,202,response.text)
        child=store.load(response.json()['id'])
        self.assertEqual(child['uploaded_files'],old['uploaded_files'])
        self.app.state.jobs.release(self.root)
        child['status']='failed';store.save(child)
        old=child
        raw=store.root/'uploads'/upload.json()['id']/'original.txt'
        raw.unlink()
        self.assertIn('附件原件缺失',self.retry(old,'missing-upload-001').text)
        self.assertFalse(self.app.state.jobs.info(self.root)['busy'])
        old['uploaded_files']=[];store.save(old)
        before={str(p) for p in (store.root/'agents').glob('*/work/run_*')}
        with patch('evotask.engine.shutil.copyfile',side_effect=OSError('injected copy failure')):
            with self.assertRaises(OSError):self.retry(old,'copy-failure-0001')
        self.assertFalse(self.app.state.jobs.info(self.root)['busy'])
        self.assertEqual(before,{str(p) for p in (store.root/'agents').glob('*/work/run_*')})
        self.assertEqual(len(store.runs()),2)
        store.close()

    def test_scheduler_and_worker_failures_release_slot(self):
        old=self.failed()
        with patch.object(self.app.state.jobs.pool,'submit',side_effect=RuntimeError('injected scheduler error')):
            response=self.retry(old)
        self.assertEqual(response.status_code,503,response.text)
        rid=response.json()['run_id']
        run=self.idle(rid)
        self.assertEqual(run['status'],'failed')
        self.assertFalse(self.app.state.jobs.info(self.root)['busy'])
        with patch('evotask.web.app.make_provider',side_effect=RuntimeError('injected worker error')):
            response=self.retry(old,'new-key-00000002')
            self.assertEqual(response.status_code,202,response.text)
            run=self.idle(response.json()['id'])
        self.assertEqual(run['status'],'failed')
        self.assertFalse(self.app.state.jobs.info(self.root)['busy'])

    def test_real_demo_retry_completes_review_and_preserves_source(self):
        old=self.failed()
        store=Store(self.root)
        KnowledgeBase(store).add(Path('evotask/web/data/similarity.md'))
        old['knowledge_base_ids']=None
        store.save(old);store.close()
        response=self.retry(old)
        self.assertEqual(response.status_code,202,response.text)
        rid=response.json()['id']; run=self.idle(rid)
        self.assertEqual(run['status'],'waiting_human',run.get('failure'))
        self.assertEqual(run['deliverable_count'],1)
        self.assertLess(run['deliverable_count'],run['artifact_count'])
        self.assertTrue(run['artifact_groups']['process'])
        a=run['artifact_groups']['deliverable'][0]
        self.assertEqual(a['status_label'],'待确认')
        preview=self.client.get(f'/api/runs/{rid}/artifacts/{a["id"]}').json()
        self.assertEqual(preview['download_name'],a['download_name'])
        self.assertIn('attachment;',self.client.get(f'/api/runs/{rid}/artifacts/{a["id"]}?download=true').headers['content-disposition'])
        reply=self.client.post('/api/runs/'+rid+'/resume',json={'request_id':run['pending']['id'],'reply':'yes'})
        self.assertEqual(reply.status_code,202)
        done=self.idle(rid)
        self.assertEqual(done['status'],'succeeded')
        self.assertEqual(self.client.get('/api/runs/'+old['id']).json()['status'],'failed')


class OutputTests(unittest.TestCase):
    def artifact(self, id, task='t1', path='report.md', sha='a', verification='verified', **kwargs):
        return dict(id=id,task_id=task,path=path,sha256=sha,verification=verification,bytes=100,owner='coder',**kwargs)

    def fixture(self):
        return {'tasks':[{'id':'t1','title':'作者','outputs':[{'path':'report.md','title':'最终报告','kind':'deliverable','order':2,'summary':'结果'}], 'artifact_ids':['art_good']},
                         {'id':'t2','title':'研究','outputs':[{'path':'report.md','title':'研究笔记','kind':'process','order':1}]}],
                'status':'succeeded','artifact_schema':2,'pending':None,'artifacts':[
                    self.artifact('art_old',sha='old',verification='superseded'),
                    self.artifact('art_good'), self.artifact('art_repeat'),
                    self.artifact('art_notes',task='t2',sha='notes'),
                    self.artifact('art_fail',sha='broken',verification='unverified',origin='failure_snapshot')]}

    def test_same_names_distinct_tasks_duplicates_and_failed_snapshot(self):
        source=self.fixture(); original=copy.deepcopy(source)
        records=describe_artifacts(source); groups=grouped_artifacts(records)
        self.assertEqual(source,original)
        self.assertEqual([a['id'] for a in groups['deliverable']],['art_good'])
        self.assertEqual([a['id'] for a in groups['history']],['art_old'])
        self.assertEqual([a['id'] for a in groups['process']],['art_notes','art_fail'])
        self.assertEqual(len({a['download_name'] for a in records}),len(records))
        self.assertEqual(records[1]['version'],2)
        self.assertEqual(records[2]['duplicate_of'],'art_good')
        self.assertFalse(records[-1]['is_current'])
        self.assertIn('失败过程',records[-1]['status_label'])

    def test_old_unknown_times_and_event_recovery(self):
        run=self.fixture();run.pop('artifact_schema')
        for task in run['tasks']:task.pop('outputs')
        groups=grouped_artifacts(describe_artifacts(run))
        self.assertEqual(len(groups['unknown']),2)
        self.assertIsNone(groups['unknown'][0]['created'])
        event={'kind':'task_succeeded','created':123,'data':{'artifacts':['art_good']}}
        self.assertEqual(describe_artifacts(run,[event])[1]['created'],123)

    def test_order_status_safe_names_and_explicit_test_delivery(self):
        run=self.fixture()
        run['tasks'][1]['outputs'][0].update(kind='deliverable',title='中文<>:/非法'+'长'*110)
        run['status']='failed'
        records=describe_artifacts(run); groups=grouped_artifacts(records)
        self.assertEqual([a['task_id'] for a in groups['deliverable']],['t2','t1'])
        for a in groups['deliverable']:
            self.assertIn('整项任务未完成',a['status_label'])
            self.assertLess(len(a['download_name']),180)
            self.assertFalse(any(c in a['download_name'] for c in '<>:/\\|?*'))
            self.assertTrue(a['download_name'].endswith('.md'))
        run['tasks'][1]['outputs'][0]['path']='test.py'
        run['artifacts'][3]['path']='test.py'
        self.assertEqual(len(grouped_artifacts(describe_artifacts(run))['deliverable']),2)

    def test_live_plan_requires_explicit_intent_and_deliverable(self):
        for tasks in ([], [{}], [{'outputs':None}], [{'outputs':[]}],
                      [{'outputs':[dict(path='notes.md',title='Notes',kind='process',order=1)]}]):
            with self.assertRaises(ValueError):validate_output_contract(tasks)
        validate_output_contract([{'outputs':[dict(path='report.md',title='Report',kind='deliverable',order=1)]}])

    def test_contract_rejects_invalid_paths_types_duplicates(self):
        valid=dict(path='report.md',title='Report',kind='deliverable',order=1)
        self.assertEqual(validate_outputs([valid])[0]['path'],'report.md')
        for update in ({'path':'../x'}, {'path':'inputs/a.md'},{'kind':'final'},{'order':True},{'title':''}):
            with self.assertRaises(ValueError):validate_outputs([valid|update])
        with self.assertRaises(ValueError):validate_outputs([valid,valid])
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));engine=Engine(store,Config(provider='demo'),None)
            run=engine.new('test')
            task={'id':'t1','owner':'coder','outputs':[valid]}
            with self.assertRaisesRegex(ValueError,'every declared deliverable'):
                engine.finish(run,task,{'artifacts':['notes.md']})
            store.close()
