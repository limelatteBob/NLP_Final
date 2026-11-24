import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from evotask.acceptance import validate_contract,html_headings,ReviewBlocked
from evotask.engine import Engine,task_from_spec,validate_plan
from evotask.config import Config
from evotask.storage import Store,digest
from evotask.check_revision import propose,apply
from tests.test_acceptance_p0 import spec

class WorkflowTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name));self.engine=Engine(self.store,Config(provider='demo'),None)
  self.run=self.engine.new('独立验收');self.run['requirements']=[{'id':'R1','text':'返回3','priority':'must'}]
  self.author=task_from_spec(spec());self.author.update(owner='coder',status='succeeded')
  self.qa=task_from_spec(spec('qa','verifier','test.py',['author'],[{'kind':'python','path':'test.py','targets':[{'task_id':'author','path':'app.py'}],'requirement_ids':['R1']}]))
  self.qa.update(owner='verifier',status='running');self.run['tasks']=[self.author,self.qa]
  self.aroot=self.store.workspace('coder',self.run['id']);self.qroot=self.store.workspace('verifier',self.run['id'])
  (self.aroot/'app.py').write_bytes(b'def count(): return 4\n');artifact=self.store.archive(self.run,'coder','app.py','author')
  artifact['verification']='verified';self.author['artifact_ids']=[artifact['id']];self.engine.refresh_inputs(self.run,self.qa)
  self.source=self.qa['inputs'][0];(self.qroot/'test.py').write_bytes(b'assert 2+2==5\n')
 def tearDown(self):self.store.close();self.temp.cleanup()
 def test_human_revision_origin_present_in_first_saved_record_and_event(self):
  self.engine.wait(self.run,self.qa,'acceptance','请确认',{'type':'finish','artifacts':['test.py']},{'artifacts':[]})
  snapshots=[];original_save=self.store.save
  def observe_save(run):
   original_save(run)
   if run.get('revisions'):
    snapshots.append(json.loads(json.dumps(run['revisions'][0])))
  with patch.object(self.store,'save',side_effect=observe_save), patch.object(self.engine,'advance',side_effect=lambda r:r):
   result=self.engine.resume(self.run['id'],self.run['pending']['id'],'revise:author|返回值必须为3，请保留原测试。')
  self.assertTrue(snapshots)
  for snapshot in snapshots:
   self.assertEqual(snapshot.get('origin'),'human_quality_feedback')
  events=[e for e in self.store.events(self.run['id']) if e['kind']=='revision_requested']
  self.assertTrue(events)
  self.assertEqual(events[-1]['data']['origin'],'human_quality_feedback')
  self.assertEqual(result['revision_count'],1)
 def test_binary_archive_plan_rejected_before_execution(self):
  with self.assertRaisesRegex(ValueError,'SQLite .db is runtime only'):validate_contract(validate_plan([spec(path='db.db')],8))
 def test_html_headings_are_structural_and_case_insensitive(self):
  self.assertEqual(html_headings('<h2>Cita<span>tions</span></h2><h3>  Key   Risks </h3>'),{'citations','key risks'})
 def test_plan_provider_contract_error_can_be_corrected(self):
  engine=Engine(self.store,Config(provider='deepseek',model='fixture'),Mock())
  engine.provider.complete.side_effect=[ValueError('malformed plan'),{'requirements':[{'id':'R1','text':'输出文件'}],'tasks':[spec()]}]
  run=engine.new('中文需求')
  tasks=engine.plan_tasks(run,[],time.monotonic())
  self.assertEqual(len(tasks),1);self.assertEqual(run['model_calls'],2)
 def test_changed_input_snapshot_cannot_be_verified_as_archived_version(self):
  self.engine.tools.sandbox=Mock();(self.qroot/self.source).write_bytes(b'def count(): return 3\n')
  with self.assertRaisesRegex(ValueError,'Authorized input snapshot changed'):self.engine.verify(self.run,self.qa,self.qa['checks'][0])
  self.engine.tools.sandbox.verify.assert_not_called()
 def test_finish_rejects_changed_input_before_browser_or_review(self):
  self.qa['outputs']=[];self.qa['checks']=[{'kind':'exists','path':'report.md'}]
  (self.qroot/'report.md').write_bytes(b'Observed report')
  (self.qroot/self.source).write_bytes(b'def count(): return 3\n')
  with self.assertRaisesRegex(ValueError,'Authorized input snapshot changed'):
   self.engine.finish(self.run,self.qa,{'artifacts':['report.md']})
  self.assertNotEqual(self.qa['status'],'succeeded')
 def test_negative_structured_report_is_never_published(self):
  self.qa['outputs']=[];self.qa['checks']=[{'kind':'exists','path':'review.json'}]
  for verdict in ('fail','blocked'):
   (self.qroot/'review.json').write_text(json.dumps({'verdict':verdict,'findings':[],'requirements':[{'id':'R1','status':verdict,'evidence':[]}]}))
   with self.assertRaises(ValueError):self.engine.finish(self.run,self.qa,{'artifacts':['review.json']})
   self.assertNotEqual(self.qa['status'],'succeeded');self.assertEqual(self.run['artifacts'][-1]['verification'],'invalid')
 def test_diagnosed_wrong_test_directly_prepares_approval(self):
  self.engine.freeze_verification(self.run,self.qa)
  self.qa['last_verification']={'step':0,'path':'test.py','result':{'success':False,'output':'AssertionError: 2+2==5','verification':{'executed':1,'manifest':{self.source:digest((self.qroot/self.source).read_bytes())}}}}
  self.engine.provider=Mock();self.engine.provider.complete.side_effect=[{'failure_kind':'test','reason':'错误期望','requirement_ids':['R1']},
   {'type':'revise_test','path':'test.py','content':'assert 2+2==4\n','reason':'保留正确算术预期','requirement_ids':['R1']}]
  self.engine.diagnose_verification(self.run,self.qa)
  self.assertEqual(self.run['pending']['kind'],'test_revision');self.assertEqual(self.run['revision_count'],0)
  self.assertEqual((self.qroot/'test.py').read_bytes(),b'assert 2+2==5\n')
 def test_repeated_revision_preserves_each_rounds_failed_input(self):
  def observed():self.qa['last_verification']={'path':'test.py','result':{'verification':{'manifest':{self.source:digest((self.qroot/self.source).read_bytes())}}}}
  observed();first=self.engine.request_revision(self.run,self.qa,'first failure','author')
  (self.aroot/'app.py').write_bytes(b'def count(): return 5\n');a=self.store.archive(self.run,'coder','app.py','author');a['verification']='verified'
  self.author.update(status='succeeded',artifact_ids=[a['id']]);self.engine.refresh_inputs(self.run,self.qa);observed()
  second=self.engine.request_revision(self.run,self.qa,'second failure','author')
  self.assertNotEqual(first['reproduction_inputs'][0]['path'],second['reproduction_inputs'][0]['path'])
  self.assertEqual((self.aroot/first['reproduction_inputs'][0]['path']).read_bytes(),b'def count(): return 4\n')
  self.assertEqual((self.aroot/second['reproduction_inputs'][0]['path']).read_bytes(),b'def count(): return 5\n')
 def test_browser_check_correction_needs_exact_approval_and_keeps_coverage(self):
  old={'kind':'browser','path':'page.html','steps':[{'action':'text','selector':'h1','value':'wrong'}],'requirement_ids':['R1']}
  self.qa['checks']=[old];new=dict(old,steps=[{'action':'text_exact','selector':'h1','value':'Actual'}])
  action={'check_index':0,'check':new,'reason':'选择器和预期与实际需求不符'}
  propose(self.engine,self.run,self.qa,action);pending=self.run['pending']
  self.assertEqual(self.qa['checks'][0],old);apply(self.engine,self.run,self.qa,pending,'yes');self.assertEqual(self.qa['checks'][0],new)
  with self.assertRaisesRegex(ValueError,'stale approval'):apply(self.engine,self.run,self.qa,pending,'yes')
  with self.assertRaisesRegex(ValueError,'coverage'):propose(self.engine,self.run,self.qa,dict(action,check=dict(new,requirement_ids=[])))
 @unittest.skipUnless(os.environ.get('EVOTASK_DOCKER_TESTS')=='1','Real Docker replay')
 def test_author_replays_exact_test_against_current_source(self):
  code=f"import importlib.util\ns=importlib.util.spec_from_file_location('app',{self.source!r})\nm=importlib.util.module_from_spec(s);s.loader.exec_module(m)\nassert m.count()==3\n"
  (self.qroot/'test.py').write_bytes(code.encode());self.engine.request_revision(self.run,self.qa,'count returns4','author')
  (self.aroot/'app.py').write_bytes(b'def count(): return 3\n')
  result=self.engine.tools.execute(self.run,self.author,'verify_revision',{})
  self.assertTrue(result['success'],result);self.assertEqual((self.qroot/self.source).read_bytes(),b'def count(): return 4\n')
  self.assertEqual((self.qroot/'test.py').read_bytes(),code.encode())
