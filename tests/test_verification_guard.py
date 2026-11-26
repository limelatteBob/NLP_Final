"""P0 safeguards: mocked review decisions, real engine state and immutable inputs."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evotask.config import Config
from evotask.engine import Engine, task_from_spec
from evotask.storage import Store, digest
from evotask import verification_guard as guard


class VerificationGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.engine = Engine(self.store, Config(provider='deepseek'), None)
        self.run = self.engine.new('时间允许 H:MM 或 HH:MM，相邻预约不冲突。', memory=False)
        self.run['requirements'] = [{'id': 'R1', 'text': self.run['goal'], 'priority': 'must'}]
        def task(id, role, path, deps):
            spec = {'id': id, 'title': '检查', 'objective': self.run['goal'], 'role': role,
                    'depends_on': deps, 'outputs': [{'path': path, 'title': path, 'kind': 'process', 'order': 1}],
                    'checks': [{'kind': 'exists', 'path': path, 'requirement_ids': ['R1']}]}
            result = task_from_spec(spec)
            result.update(owner=role, status='succeeded' if role == 'coder' else 'running')
            return result
        self.author = task('author', 'coder', 'app.py', [])
        self.qa = task('qa', 'verifier', 'test.py', ['author'])
        self.check = {'kind': 'python', 'path': 'test.py', 'targets': [{'task_id': 'author', 'path': 'app.py'}], 'requirement_ids': ['R1']}
        self.qa['checks'] = [self.check]
        self.run['tasks'] = [self.author, self.qa]
        root = self.store.workspace('coder', self.run['id'])
        (root/'app.py').write_text('def answer(): return True\n')
        artifact = self.store.archive(self.run, 'coder', 'app.py', 'author')
        artifact['verification'] = 'verified'
        self.author['artifact_ids'] = [artifact['id']]
        self.engine.refresh_inputs(self.run, self.qa)
        self.root = self.store.workspace('verifier', self.run['id'])
        self.path = self.root/'test.py'
        self.path.write_text('assert True\n')
        self.review = {'verdict': 'pass', 'reason': '符合需求', 'cases': [
            {'assertion_ids': ['A1'], 'requirement_ids': ['R1'], 'input': '09:00–09:30 与 09:30–10:00', 'expected': 'True',
             'asserted': 'True', 'reason': '相邻不冲突', 'consistent': True}]}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def review_test(self):
        guard.review_test(self.engine, self.run, self.qa, self.check, self.path, {'input': 'hash'})

    def failure(self):
        self.qa['last_verification'] = {'step': 1, 'path': 'test.py', 'result': {'success': False, 'output': 'ValueError: 09:00'}}
        return self.qa['last_verification']

    def decision(self, kind='business'):
        return {'failure_kind': kind, 'target_task': 'author', 'requirement_ids': ['R1'], 'reason': '实现拒绝合法时间',
                'case': {'input': '09:00', 'expected': '合法', 'actual': 'ValueError', 'asserted': '合法',
                         'requirement_quote': '时间允许 H:MM 或 HH:MM',
                         'implementation_matches_requirement': kind == 'test',
                         'test_matches_requirement': kind == 'business'}}

    def test_missing_script_precedes_archive_model_and_docker(self):
        self.path.unlink()
        with patch.object(self.store, 'archive') as archive, patch.object(self.engine, 'model') as model, patch.object(self.engine.tools.sandbox, 'verify') as docker:
            with self.assertRaises(guard.ExecutionPreflightError) as exc:
                self.engine.verify(self.run, self.qa, self.check)
            self.assertIn(self.qa['input_bindings'][0]['path'], str(exc.exception))
            archive.assert_not_called(); model.assert_not_called(); docker.assert_not_called()
        self.assertFalse(self.qa.get('revision_tests'))

    def test_syntax_and_repair_package_preflight(self):
        self.path.write_text('def bad(:\n')
        with self.assertRaisesRegex(guard.ExecutionPreflightError, 'line 1'):
            self.engine.tools.prepare(self.run, self.qa, 'python_run', {'path': 'test.py'})
        with self.assertRaisesRegex(guard.ExecutionPreflightError, 'no frozen repair package'):
            self.engine.tools.execute(self.run, self.qa, 'verify_revision', {})
        self.assertFalse(guard.readiness(self.engine.tools, self.run, self.qa)['verify_revision_ready'])

    def test_wrong_expectation_never_executes_or_freezes(self):
        bad = copy.deepcopy(self.review); bad.update(verdict='revise')
        bad['cases'][0].update(asserted='False', consistent=False)
        with patch.object(self.engine, 'model', return_value=bad), patch.object(self.engine.tools.sandbox, 'verify') as docker:
            with self.assertRaises(guard.TestExpectationError): self.engine.verify(self.run, self.qa, self.check)
            docker.assert_not_called()
        self.assertFalse(self.qa.get('revision_tests'))
        self.assertEqual(self.author['status'], 'succeeded')

    def test_review_cache_invalidates_for_script_rules_and_targets(self):
        with patch.object(self.engine, 'model', return_value=self.review) as model:
            self.review_test(); self.review_test(); self.assertEqual(model.call_count, 1)
            self.path.write_text('assert 1 == 1\n'); self.review_test(); self.assertEqual(model.call_count, 2)
            self.run['clarifications'].append({'answer': '补充要求'}); self.review_test(); self.assertEqual(model.call_count, 3)
            guard.review_test(self.engine, self.run, self.qa, self.check, self.path, {'input': 'new-hash'})
            self.assertEqual(model.call_count, 4)

    def test_passing_review_requires_consistent_covered_cases(self):
        for change in ('inconsistent', 'uncovered'):
            bad = copy.deepcopy(self.review)
            if change == 'inconsistent': bad['cases'][0]['consistent'] = False
            else:
                self.run['requirements'].append({'id': 'R2', 'text': 'new'})
                self.check['requirement_ids'].append('R2')
            with patch.object(self.engine, 'model', return_value=bad):
                with self.assertRaisesRegex(ValueError, 'Test expectation precheck|uncovered requirements'): self.review_test()

    def test_review_can_correct_wrong_diagnosis_and_invalidates_on_new_failure(self):
        evidence = self.failure()
        with patch.object(self.engine, 'model', return_value=self.decision()):
            actual = guard.review_diagnosis(self.engine, self.run, self.qa, {'failure_kind': 'test'}, {}, evidence)
        self.assertEqual(actual['failure_kind'], 'business')
        self.assertEqual(guard.reviewed_kind(self.run, self.qa), 'business')
        self.qa['last_verification']['result']['output'] = 'new failure'
        self.assertIsNone(guard.reviewed_kind(self.run, self.qa))

    def test_all_assertions_are_checked_in_bounded_batches(self):
        self.path.write_text('\n'.join('assert True' for _ in range(7)))
        def respond(run, purpose, context):
            result = copy.deepcopy(self.review)
            result['cases'][0]['assertion_ids'] = [a['id'] for a in context['assertion_batch']]
            return result
        with patch.object(self.engine, 'model', side_effect=respond) as model:
            self.review_test(); self.assertEqual(model.call_count, 2)
            self.assertEqual(model.call_args_list[1].args[2]['assertion_batch'][0]['id'], 'A7')
            self.review_test(); self.assertEqual(model.call_count, 2)

    def test_summary_cannot_skip_last_assertion_or_unittest_call(self):
        self.path.write_text('assert True\nself.assertEqual(2, 3)\n')
        with patch.object(self.engine, 'model', return_value=self.review):
            with self.assertRaisesRegex(guard.TestExpectationError, 'omitted assertions: A2'): self.review_test()

    def test_contradictory_or_invented_diagnosis_cannot_reopen_author(self):
        for change in ('flags', 'quote', 'owner', 'requirement'):
            result = self.decision()
            if change == 'flags': result['case']['implementation_matches_requirement'] = True
            if change == 'quote': result['case']['requirement_quote'] = 'not in actual requirements'
            if change == 'owner': result['target_task'] = 'unrelated'
            if change == 'requirement': result['requirement_ids'] = ['R99']
            with patch.object(self.engine, 'model', return_value=result):
                with self.assertRaises(ValueError): guard.review_diagnosis(self.engine, self.run, self.qa, {}, {}, self.failure())
            self.assertEqual(self.author['status'], 'succeeded')
            self.assertIsNone(guard.reviewed_kind(self.run, self.qa))

    def test_actions_cannot_bypass_diagnosis(self):
        self.failure()
        with patch.object(self.engine, 'diagnose_verification') as diagnose, patch.object(self.engine, 'request_revision') as repair:
            self.engine.action(self.run, self.qa, {'type': 'request_revision', 'target_task': 'author', 'reason': '业务错误', 'failure_kind': 'business', 'requirement_ids': ['R1']})
            diagnose.assert_called_once(); repair.assert_not_called()

    def test_wrong_frozen_test_still_requires_approval(self):
        self.failure(); self.engine.freeze_verification(self.run, self.qa)
        decision = self.decision('test')
        self.qa['reviewed_diagnosis'] = {'key': guard.diagnosis_key(self.run, self.qa), 'decision': decision}
        before = self.path.read_bytes()
        self.engine.propose_test_revision(self.run, self.qa, {'path': 'test.py', 'content': 'assert 1 == 1\n', 'reason': '测试预期错误', 'requirement_ids': ['R1']})
        self.assertEqual(self.run['pending']['kind'], 'test_revision')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.author['status'], 'succeeded')

    def test_legacy_and_frozen_tests_do_not_add_review_calls(self):
        self.run['execution_schema'] = 3
        with patch.object(self.engine, 'model') as model:
            self.review_test(); model.assert_not_called()
        self.run['execution_schema'] = 4
        self.engine.freeze_verification(self.run, self.qa)
        with patch.object(self.engine, 'model') as model:
            self.review_test(); model.assert_not_called()


if __name__ == '__main__': unittest.main()
