"""P0 evidence, repair and durable context contracts (no paid model calls)."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from evotask.acceptance import validate_contract, requirements, read_review
from evotask.config import Config
from evotask.engine import Engine, validate_plan, task_from_spec
from evotask.sandbox import DockerSandbox
from evotask.storage import Store, digest, AGENTS
from evotask.teams import Teams

def output(path): return {'path':path,'title':path,'kind':'deliverable','order':1}
def spec(id='author', role='coder', path='app.py', deps=None, checks=None):
    return {'id':id,'role':role,'title':'功能实现','objective':'按需求实现并验证','depends_on':deps or [],
            'outputs':[output(path)],'checks':checks or [{'kind':'exists','path':path,'requirement_ids':['R1']}]}

class ContractTests(unittest.TestCase):
    def test_wrong_local_path_rejected_and_upstream_resolved(self):
        specs=[spec(),spec('qa','verifier','review.json',['author'],[{'kind':'exists','path':'app.py'}])]
        with self.assertRaisesRegex(ValueError,'not its declared output'): validate_contract(validate_plan(specs,8))
        specs[-1]['checks'][0]['source_task']='author'
        validate_contract(validate_plan(specs,8))
    def test_missing_coverage_and_python_targets(self):
        tasks=validate_plan([spec()],8)
        with self.assertRaisesRegex(ValueError,'missing'): validate_contract(tasks,requirements([{'id':'R2','text':'持久化'}]))
        tasks=validate_plan([spec(),spec('qa','verifier','test.py',['author'],[{'kind':'python','path':'test.py'}])],8)
        with self.assertRaisesRegex(ValueError,'explicit upstream targets'): validate_contract(tasks)
    def test_structured_review_cannot_pass_empty_or_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'review.json'
            for value in ({'verdict':'pass','findings':[],'requirements':[]},
                          {'verdict':'blocked','findings':[],'requirements':[{'id':'R1','status':'blocked','evidence':[]}]}):
                p.write_text(json.dumps(value))
                with self.assertRaises(ValueError): read_review(p)

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.store=Store(self.root); self.engine=Engine(self.store,Config(provider='demo'),None)
        self.run=self.engine.new('测试验收与独立返修')
        self.run['requirements']=[{'id':'R1','text':'2+2 必须为4','priority':'must'}]
        self.author=task_from_spec(spec()); self.author.update(owner='coder',status='succeeded')
        self.qa=task_from_spec(spec('qa','verifier','test.py',['author'],[{'kind':'python','path':'test.py'}])); self.qa.update(owner='verifier',status='running')
        self.run['tasks']=[self.author,self.qa]
        root=self.store.workspace('coder',self.run['id']); (root/'app.py').write_text('def add(a,b): return a+b\n')
        a=self.store.archive(self.run,'coder','app.py','author'); a['verification']='verified';self.author['artifact_ids']=[a['id']]
        self.engine.refresh_inputs(self.run,self.qa)
        self.qroot=self.store.workspace('verifier',self.run['id']); (self.qroot/'test.py').write_text('assert 2+2==5\n')
    def tearDown(self): self.store.close(); self.temp.cleanup()
    def test_ambiguous_assertion_requires_diagnosis(self):
        with self.assertRaisesRegex(ValueError,'Diagnose first'):
            self.engine.action(self.run,self.qa,{'type':'request_revision','reason':'2+2应为5','target_task':'author'})
        self.assertEqual(self.author['status'],'succeeded');self.assertEqual(self.run['revision_count'],0)
    def test_reverse_package_and_preflight_hashes(self):
        path=self.qa['inputs'][0]
        self.qa['last_verification']={'path':'test.py','result':{'success':False,'verification':{'manifest':{path:digest((self.qroot/path).read_bytes())}}}}
        feedback=self.engine.request_revision(self.run,self.qa,'Observed failure','author')
        root=self.store.workspace('coder',self.run['id'])
        self.assertTrue(feedback['test_inputs']);self.assertTrue(feedback['reproduction_inputs'])
        self.assertEqual((root/feedback['test_inputs'][0]['path']).read_bytes(),(self.qroot/'test.py').read_bytes())
        self.assertEqual((root/feedback['reproduction_inputs'][0]['path']).read_bytes(),(root/'app.py').read_bytes())
        with self.assertRaises(PermissionError):
            self.engine.tools.execute(self.run,self.author,'write_file',{'path':feedback['test_inputs'][0]['path'],'content':'changed'})
    def test_unrelated_success_does_not_reset_failure_fingerprint(self):
        for i in range(3):
            self.engine.fail_attempt(self.run,self.qa,{'type':'finish'},ValueError('Acceptance failed: test.py missing required text X'))
            if i<2:self.engine.action(self.run,self.qa,{'type':'write_file','path':'notes.md','content':str(i)})
        self.assertEqual(self.qa['status'],'failed')
        self.assertEqual(self.qa['failure_diagnosis']['occurrences'],3)
    def test_changed_browser_version_starts_a_new_failure_fingerprint(self):
        for version in ('old', 'old', 'new'):
            self.qa['last_verification'] = {'step':self.qa['steps'], 'path':'page.html',
                'result':{'output':'expected 0, observed 3', 'verification':{'manifest':{'page.html':version}}}}
            self.engine.fail_attempt(self.run,self.qa,{'type':'finish'},ValueError('Browser check failed'))
            self.engine.action(self.run,self.qa,{'type':'write_file','path':'notes.md','content':'unrelated'})
        self.assertNotEqual(self.qa['status'],'failed')
        self.assertEqual(self.qa['failure_diagnosis']['occurrences'],1)
        self.assertEqual(sorted(self.qa['failure_fingerprints'].values()),[1,2])
    def test_frozen_test_correction_needs_exact_approval(self):
        self.engine.freeze_verification(self.run,self.qa)
        self.engine.propose_test_revision(self.run,self.qa,{'path':'test.py','content':'assert 2+2==4\n','reason':'原预期与 R1 冲突','requirement_ids':['R1']})
        pending=copy.deepcopy(self.run['pending'])
        self.assertIn('-assert 2+2==5',pending['prepared']['diff'])
        self.assertEqual((self.qroot/'test.py').read_text(),'assert 2+2==5\n')
        def verified(run,task,check):
            result={'success':True,'output':'fixture: approval flow only'}
            task['last_verification']={'path':check['path'],'result':result};return result
        with patch.object(self.engine,'advance',side_effect=lambda r:r), patch.object(self.engine,'verify',side_effect=verified):
            result=self.engine.resume(self.run['id'],pending['id'],'yes')
        self.assertEqual((self.qroot/'test.py').read_text(),'assert 2+2==4\n')
        self.assertEqual(result['tasks'][-1]['revision_tests'][0]['sha256'],digest(b'assert 2+2==4\n'))
        with self.assertRaisesRegex(ValueError,'matching pending'):self.engine.resume(self.run['id'],pending['id'],'yes')
    def test_changed_test_rejects_stale_approval(self):
        self.engine.freeze_verification(self.run,self.qa)
        self.engine.propose_test_revision(self.run,self.qa,{'path':'test.py','content':'assert 2+2==4','reason':'wrong expectation','requirement_ids':['R1']})
        (self.qroot/'test.py').write_text('changed')
        with self.assertRaisesRegex(ValueError,'stale approval'):self.engine.resume(self.run['id'],self.run['pending']['id'],'yes')
    def test_quality_feedback_reopens_without_old_approval(self):
        self.engine.wait(self.run,self.qa,'acceptance','是否接受？',{'type':'finish','artifacts':['test.py']},{'artifacts':[]})
        request=self.run['pending']['id']
        with patch.object(self.engine,'advance',side_effect=lambda r:r): result=self.engine.resume(self.run['id'],request,'revise:还未满足 SQLite 持久化')
        self.assertIsNone(result['pending']);self.assertEqual(result['status'],'running')
        self.assertIn('SQLite',result['clarifications'][-1]['answer'])
        with self.assertRaises(ValueError):self.engine.resume(self.run['id'],request,'yes')
    def test_durable_human_rules_outlive_chat_window_and_isolate_team(self):
        teams=Teams(self.store); roles={a:{'name':a,'instructions':'职责'} for a in AGENTS}
        team=teams.create('团队',roles); other=teams.create('另一团队',roles)
        self.store.bind_team(team['id']);self.store.message('group','human','三间会议室与 SQLite')
        for i in range(60):self.store.message('group','coder','工作进度'+str(i))
        for i in range(25):self.store.message('group','human','需求'+str(i))
        self.store.message('group','human','yes')
        first=teams.decisions(team['id']);self.assertIn('SQLite',first['decisions'][0]['body'])
        self.assertEqual(first['total'],26);self.assertEqual(first['next_offset'],10)
        self.assertEqual(teams.decisions(other['id'])['total'],0)
        self.assertIsInstance(json.loads(teams.history(team['id'])['previous_runs']),list)

@unittest.skipUnless(os.environ.get('EVOTASK_DOCKER_TESTS')=='1','Explicit real Docker tests')
class EvidenceDockerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='evotask-p0-');self.root=Path(self.temp.name)
        self.sandbox=DockerSandbox(Config(tool_timeout=10))
    def tearDown(self):self.temp.cleanup()
    def check(self,code):
        (self.root/'check.py').write_text(code,encoding='utf-8');return self.sandbox.verify(self.root,'check.py')
    def test_uncalled_functions_and_empty_cannot_pass(self):
        for code in ('', 'def test_first():\n    assert True\ndef test_second():\n    assert False\n'):
            result=self.check(code);self.assertFalse(result['success'],result);self.assertEqual(result['verification']['executed'],0)
    def test_assertions_and_failure(self):
        result=self.check('assert 2+2==4\nassert len([1])==1\n');self.assertTrue(result['success'],result);self.assertEqual(result['verification']['assertions'],2)
        result=self.check('assert 2+2==5\n');self.assertFalse(result['success']);self.assertEqual(result['verification']['assertions'],1)
    def test_unittest_failure_even_without_sys_exit(self):
        result=self.check('import unittest\nclass Tests(unittest.TestCase):\n def test_math(self): self.assertEqual(2+2,5)\nunittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))\n')
        self.assertFalse(result['success'],result);self.assertEqual(result['verification']['failed'],1)
    def test_skipped_only_is_not_a_pass(self):
        result=self.check("import unittest\nclass Tests(unittest.TestCase):\n @unittest.skip('unavailable')\n def test_x(self): assert False\nunittest.main()\n")
        self.assertFalse(result['success'],result);self.assertEqual(result['verification']['executed'],0)
    def test_actual_input_hash_and_changed_file(self):
        (self.root/'inputs').mkdir(); (self.root/'inputs/data.json').write_text('{"value":4}')
        result=self.check("import json\nfrom pathlib import Path\nassert json.loads(Path('inputs/data.json').read_text())['value']==4\n")
        self.assertTrue(result['success'],result);self.assertEqual(result['verification']['manifest']['inputs/data.json'],digest(b'{"value":4}'))
        (self.root/'data.txt').write_text('old')
        result=self.check("from pathlib import Path\nassert Path('data.txt').read_text()=='old'\nPath('data.txt').write_text('new')\n")
        self.assertFalse(result['success'],result);self.assertIn('data.txt',result['verification']['changed'])
    def test_ordinary_python_script_still_runs(self):
        (self.root/'script.py').write_text('print("ordinary output")')
        self.assertTrue(self.sandbox.execute(self.root,'script.py')['success'])
