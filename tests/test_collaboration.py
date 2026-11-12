from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from evotask.collaboration import normalize_action
from evotask.config import Config
from evotask.engine import Engine, task_from_spec
from evotask.retrieval import KnowledgeBase
from evotask.sandbox import DockerSandbox
from evotask.storage import Store, digest
from evotask.tools import Tools


class ReportProvider:
    """Deliberately omit a URL first, then repair the author's report, never its test."""
    def __init__(self, repair=True):
        self.repair = repair
        self.contexts = []

    def complete(self, purpose, context):
        if purpose == 'plan':
            return {'tasks': [
                {'id': 'research', 'title': 'research', 'objective': 'record sources', 'role': 'researcher', 'depends_on': [],
                 'checks': [{'kind': 'exists', 'path': 'sources.md'}]},
                {'id': 'write', 'title': 'report', 'objective': 'write report', 'role': 'coder', 'depends_on': ['research'],
                 'checks': [{'kind': 'contains', 'path': 'report.md', 'text': 'Market'}]},
                {'id': 'verify', 'title': 'verify', 'objective': 'Report must contain a real URL', 'role': 'verifier', 'depends_on': ['write'],
                 'checks': [{'kind': 'python', 'path': 'verify_report.py'}]},
            ]}
        if purpose == 'bid':
            return {'accept': True, 'confidence': 1.0 if context['agent'] == context['task']['role'] else 0.0, 'reason': 'fixture'}
        task = context['task']
        self.contexts.append((task['id'], context['collaboration']))
        if task['id'] == 'research':
            if task['steps'] == 1:
                return {'type': 'tool', 'name': 'write_file', 'args': {'path': 'sources.md', 'content': 'Relevant lead https://example.com/research'}}
            return {'type': 'finish', 'artifacts': ['sources.md'], 'summary': 'sources ready'}
        if task['id'] == 'write':
            if task['steps'] == 1:
                source = next(path for path in task['inputs'] if path.startswith('inputs/research/'))
                return {'type': 'tool', 'name': 'read_file', 'args': {'path': source}}
            if task['steps'] == 2:
                content = '# Market\n## Conclusions\nA hypothesis, not an established market fact.\n'
                if task.get('revision_feedback') and self.repair:
                    content += '## Sources\nhttps://example.com/research\n'
                return {'type': 'tool', 'name': 'write_file', 'args': {'path': 'report.md', 'content': content}}
            return {'type': 'finish', 'artifacts': ['report.md'], 'summary': 'report ready'}
        if task['steps'] == 1 and not task.get('revision_feedback'):
            path = next(p for p in task['inputs'] if p.startswith('inputs/write/'))
            script = ("from pathlib import Path\nimport re\n" + f"report_path = {path!r}\n"
                      "report = Path(report_path)\nassert report.exists()\ncontent = report.read_text(encoding='utf-8')\n"
                      "assert 'Market' in content\nassert 'Conclusions' in content\n"
                      "assert re.findall(r'https?://[^\\s]+', content), 'No URLs found in report'\nprint('passed')\n")
            return {'purpose': 'act', 'action': {'type': 'tool', 'name': 'write_file', 'args': {'path': 'verify_report.py', 'content': script}}}
        if task['steps'] == (1 if task.get('revision_feedback') else 2):
            return {'purpose': 'act', 'action': {'type': 'tool', 'name': 'python_run', 'args': {'path': 'verify_report.py'}}}
        return {'type': 'finish', 'artifacts': ['verify_report.py'], 'summary': 'unchanged assertions passed'}


class InspectingSandbox:
    """Fixture result only; never execute generated Python on the host."""
    def __init__(self):
        self.script_hashes = []
    def execute(self, root, path):
        script = (root / path).read_text(encoding='utf-8')
        self.script_hashes.append(digest(script.encode()))
        source = re.search(r"report_path = '([^']+)'", script)[1]
        content = (root / source).read_text(encoding='utf-8')
        success = 'https://example.com/research' in content
        return {'success': success, 'exit_code': 0 if success else 1,
                'output': 'passed' if success else 'AssertionError: No URLs found in report', 'sandbox': 'test-fixture'}


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evotask-collaboration-')
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.config = Config(provider='demo', model='fixture', max_model_calls=100)
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def execute(self, repair=True, sandbox=None, review=False):
        provider, sandbox = ReportProvider(repair), sandbox or InspectingSandbox()
        engine = Engine(self.store, self.config, provider, sandbox=sandbox)
        run = engine.advance(engine.new('Produce a sourced market report', require_review=review))
        return engine, run, provider, sandbox

    def test_assertion_failure_returns_to_author_then_same_test_passes(self):
        engine, run, provider, sandbox = self.execute()
        self.assertEqual(run['status'], 'succeeded', run['failure'])
        self.assertEqual(run['revision_count'], 1)
        self.assertEqual(len(set(sandbox.script_hashes)), 1, 'must retain the failing test')
        reports = [a for a in run['artifacts'] if a['path'] == 'report.md']
        self.assertEqual(reports[0]['verification'], 'superseded')
        self.assertEqual(reports[-1]['verification'], 'verified')
        self.assertNotIn(b'https://', self.store.artifact_bytes(reports[0]))
        self.assertIn(b'https://', self.store.artifact_bytes(reports[-1]))
        writer = next(t for t in run['tasks'] if t['id'] == 'write')
        verifier = next(t for t in run['tasks'] if t['id'] == 'verify')
        self.assertNotEqual(writer['owner'], verifier['owner'])
        self.assertTrue(any(path.startswith('inputs/research/') for path in verifier['inputs']))
        self.assertIn('real URL', next(c for t, c in provider.contexts if t == 'write')['downstream_requirements'][0]['objective'])
        self.assertTrue(any(e['kind'] == 'artifact_refreshed' for e in self.store.events(run['id'])))
        self.assertTrue(any(m.get('superseded') and not m['verified'] for m in self.store.memory('coder')))
        with self.assertRaises(PermissionError):
            engine.tools.execute(run, verifier, 'write_file', {'path': 'verify_report.py', 'content': 'print("fake pass")'})

    def test_repeated_assertion_stops_after_two_revisions(self):
        _, run, _, sandbox = self.execute(repair=False)
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['revision_count'], 2)
        self.assertEqual(len(sandbox.script_hashes), 3)
        self.assertIn('Revision budget exhausted', run['tasks'][-1]['summary'])

    def test_review_happens_after_successful_reverification_and_survives_reload(self):
        engine, run, _, _ = self.execute(review=True)
        self.assertEqual(run['status'], 'waiting_human')
        self.assertEqual(run['pending']['task_id'], 'verify')
        pending = run['pending']['id']
        restored = self.store.load(run['id'])
        self.assertEqual(restored['revision_count'], 1)
        result = engine.resume(run['id'], pending, 'yes')
        self.assertEqual(result['status'], 'succeeded')

    def test_legacy_failed_verifier_can_be_reopened_without_replacing_its_test(self):
        engine = Engine(self.store, self.config, ReportProvider(False), sandbox=InspectingSandbox())
        run = engine.new('legacy failure')
        run['revision_count'] = 2  # Suppress the automatic path to reproduce an older terminal failure.
        engine.advance(run)
        self.assertEqual(run['status'], 'failed')
        for key in ('revision_count', 'revisions'):
            run.pop(key, None)
        for task in run['tasks']:
            for key in ('input_bindings', 'revision_tests', 'last_verification'):
                task.pop(key, None)
        self.store.save(run)
        restored = self.store.load(run['id'])
        new_engine = Engine(self.store, self.config, ReportProvider(), sandbox=InspectingSandbox())
        new_engine.retry_verification(restored)
        result = new_engine.advance(restored)
        self.assertEqual(result['status'], 'succeeded', result['failure'])
        self.assertEqual(result['revision_count'], 1)

    def test_frozen_test_tampering_and_unrelated_revision_target_rejected(self):
        engine, run, _, _ = self.execute()
        verifier = run['tasks'][-1]
        path = self.store.workspace(verifier['owner'], run['id']) / 'verify_report.py'
        path.write_text('print("all checks passed")')
        with self.assertRaisesRegex(ValueError, 'Frozen verification'):
            engine.finish(run, verifier, {'artifacts': ['verify_report.py']})
        with self.assertRaisesRegex(ValueError, 'specific successful direct dependency'):
            engine.request_revision(run, verifier, 'assertion failed', 'research')

    def test_source_check_accepts_local_citation_without_invented_url(self):
        source = self.root / 'internal.txt'
        source.write_text('internal product evidence', encoding='utf-8')
        kb = KnowledgeBase(self.store)
        kb.add(source)
        engine = Engine(self.store, self.config, None)
        run = engine.new('use internal knowledge')
        task = task_from_spec({'id': 'writer', 'role': 'coder', 'title': 'report', 'objective': 'cite local evidence',
                              'checks': [{'kind': 'sources', 'path': 'report.md', 'policy': 'any'}]})
        task['owner'] = 'coder'
        run['tasks'] = [task]
        engine.action(run, task, {'type': 'tool', 'name': 'retrieve_documents', 'args': {'query': 'internal'}})
        citation = task['allowed_citations'][0]
        engine.action(run, task, {'type': 'tool', 'name': 'write_file', 'args': {'path': 'report.md', 'content': f'Internal knowledge [{citation}]'}})
        engine.finish(run, task, {'artifacts': ['report.md']})
        self.assertEqual(task['status'], 'succeeded')
        task['checks'][0]['policy'] = 'url'
        with self.assertRaisesRegex(ValueError, 'needs observed sources'):
            engine.finish(run, task, {'artifacts': ['report.md']})

    def test_nested_action_normalized_but_conflicting_payload_not_guessed(self):
        action = {'type': 'tool', 'name': 'list_files', 'args': {}}
        self.assertEqual(normalize_action({'purpose': 'act', 'action': action}), action)
        ambiguous = {'type': 'fail', 'action': action}
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            normalize_action(ambiguous)
        with self.assertRaises(ValueError):
            normalize_action({'purpose': 'plan', 'action': action})

    @unittest.skipUnless(os.environ.get('EVOTASK_DOCKER_TESTS') == '1', 'Opt-in real Docker revision workflow')
    def test_real_docker_executes_unchanged_verifier_after_author_revision(self):
        _, run, _, _ = self.execute(sandbox=DockerSandbox(self.config))
        self.assertEqual(run['status'], 'succeeded', run['failure'])
        self.assertEqual(run['revision_count'], 1)


if __name__ == '__main__':
    unittest.main()
