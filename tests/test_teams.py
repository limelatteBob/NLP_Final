from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from evotask.engine import Engine, validate_plan
from evotask.storage import AGENTS, Store, digest
from evotask.teams import Teams
from tests.test_core import TestBase
from tests import test_web as web_tests

TestClient = web_tests.TestClient


ROLES = {a: {'name': n, 'instructions': f'You are {n}; prepare an accessible museum visit.'}
         for a, n in zip(AGENTS, ['Curator', 'Archivist', 'Guide designer', 'Access reviewer'])}
SLIDES = '''<!doctype html><html><meta charset="utf-8"><title>Museum</title>
<h1 id="slide">Sources</h1><button id="prev">Previous</button><button id="next">Next</button>
<script>let i=0;const pages=['Sources','Visitor implications','Access risks'];
function show(){document.querySelector('#slide').textContent=pages[i]}
document.querySelector('#next').onclick=()=>{i=Math.min(2,i+1);show()};
document.querySelector('#prev').onclick=()=>{i=Math.max(0,i-1);show()};</script></html>'''


class MuseumProvider:
    def __init__(self):
        self.contexts = []

    def complete(self, purpose, context):
        self.contexts.append(copy.deepcopy({'purpose': purpose, 'role': context.get('role_profile'),
            'team': context.get('team'), 'history': context.get('group_history')}))
        if purpose == 'chat':
            return {'reply': 'Museum discussion only; no task started.'}
        if purpose == 'plan':
            if not context['clarifications']:
                return {'question': 'Which exhibition should the guide cover?'}
            return {'tasks': [{'id': 'guide', 'role': 'researcher', 'title': 'Museum guide',
                'objective': 'Prepare guide and data', 'required_tools': ['python_run', 'web_search'],
                'depends_on': [], 'checks': [{'kind': 'exists', 'path': 'slides.html'},
                                            {'kind': 'contains', 'path': 'museum/data.json', 'text': 'visitors'}]}]}
        if purpose == 'bid':
            return {'accept': True, 'confidence': 1 if context['agent'] == 'researcher' else .1, 'reason': 'archival duty'}
        task = context['task']
        actions = [h for h in task['history'] if 'action' in h]
        inherited = 'inputs/previous/museum/data.json' in task['inputs']
        if inherited and not actions:
            return {'type': 'tool', 'name': 'read_file', 'args': {'path': 'inputs/previous/museum/data.json'}}
        step = len(actions) - int(inherited)
        if step == 0:
            count = json.loads(actions[0]['result']['content'])['visitors'] + 1 if inherited else 37
            return {'type': 'tool', 'name': 'write_file', 'args': {'path': 'museum/data.json', 'content': json.dumps({'visitors': count})}}
        if step == 1:
            return {'type': 'write_file', 'path': 'museum/app.py', 'content': 'def label():\n    return "Museum"\n'}
        if step == 2:
            return {'type': 'write_file', 'path': 'slides.html', 'content': SLIDES}
        return {'type': 'finish', 'summary': 'Guide ready for review', 'artifacts': ['museum/data.json', 'museum/app.py', 'slides.html']}


class TeamEngineTests(TestBase):
    def setUp(self):
        super().setUp()
        self.team = Teams(self.store).create('Museum accessibility', ROLES)
        self.provider = MuseumProvider()
        self.engine = Engine(self.store, self.config, self.provider)

    def complete(self, run):
        self.engine.advance(run)
        self.assertEqual(run['pending']['kind'], 'planning')
        run = self.engine.resume(run['id'], run['pending']['id'], 'The ceramics exhibition')
        self.assertEqual(run['pending']['kind'], 'acceptance')
        return self.engine.resume(run['id'], run['pending']['id'], 'yes')

    def test_custom_profiles_tools_review_and_cross_run_bytes(self):
        first = self.complete(self.engine.new('Create museum guide', team_id=self.team['id'], require_review=True))
        self.assertEqual(first['status'], 'succeeded')
        selected = [a['id'] for a in first['artifacts'] if a['verification'] == 'verified']
        before = {a['id']: self.store.artifact_bytes(a) for a in first['artifacts']}
        second = self.engine.new('Extend museum guide', team_id=self.team['id'], source_run_id=first['id'],
                                 artifact_ids=selected, require_review=True)
        self.assertEqual({m['path'] for m in second['inherited_inputs']}, {'museum/data.json', 'museum/app.py', 'slides.html'})
        for agent in AGENTS:
            root = self.store.workspace(agent, second['id'])
            self.assertEqual((root / 'inputs/previous/museum/data.json').read_bytes(), b'{"visitors": 37}')
        second = self.complete(second)
        artifact = next(a for a in second['artifacts'] if a['path'] == 'museum/data.json')
        self.assertEqual(self.store.artifact_bytes(artifact), b'{"visitors": 38}')
        self.assertEqual(before, {a['id']: self.store.artifact_bytes(a) for a in first['artifacts']})
        self.assertIn(first['id'], second['group_history']['previous_runs'])
        for c in self.provider.contexts:
            self.assertEqual(c['team']['name'], 'Museum accessibility')
            self.assertIn(c['role']['name'], [r['name'] for r in ROLES.values()])
        self.assertTrue(self.store.memory('researcher'))
        self.store.bind_team(None)
        self.assertFalse(self.store.memory('researcher'))
        self.assertEqual(self.store.messages('group'), [])

    def test_cross_team_handoff_and_path_version_conflict_rejected(self):
        first = self.complete(self.engine.new('Create guide', team_id=self.team['id'], require_review=True))
        other = Teams(self.store).create('Other museum', ROLES)
        with self.assertRaisesRegex(ValueError, 'same team'):
            self.engine.new('steal', team_id=other['id'], source_run_id=first['id'], artifact_ids=[first['artifacts'][0]['id']])
        duplicate = dict(first['artifacts'][0], id='art_000000000000')
        first['artifacts'].append(duplicate); self.store.save(first)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            Teams(self.store).inputs(self.team['id'], first['id'], [duplicate['id'], first['artifacts'][0]['id']])
        with self.assertRaises(ValueError):
            Teams(self.store).inputs(self.team['id'], first['id'], ['art_111111111111'])

    def test_rerun_keeps_role_and_input_snapshots_and_rejects_tampering(self):
        first = self.complete(self.engine.new('Create guide', team_id=self.team['id'], require_review=True))
        second = self.engine.new('Extend guide', team_id=self.team['id'], source_run_id=first['id'], artifact_ids=[first['artifacts'][0]['id']])
        second['status'] = 'failed'; self.store.save(second)
        new = self.engine.rerun(second)
        self.assertNotEqual(new['id'], second['id'])
        self.assertEqual(new['team_snapshot'], second['team_snapshot'])
        self.assertEqual(new['inherited_inputs'], second['inherited_inputs'])
        path = self.store.workspace('coordinator', second['id']) / second['initial_inputs'][0]
        path.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'snapshot has changed'):
            self.engine.rerun(second)

    def test_permissions_cannot_be_assigned_in_role_text_or_payload(self):
        invalid = copy.deepcopy(ROLES); invalid['coordinator']['tools'] = ['shell']
        with self.assertRaises(ValueError):
            Teams(self.store).create('Bad', invalid)
        spec = [{'id': 'x', 'role': 'researcher', 'title': 'x', 'objective': 'x',
                 'required_tools': ['web_search', 'python_run'], 'checks': [{'kind': 'exists', 'path': 'x.md'}]}]
        with self.assertRaises(ValueError):
            validate_plan(spec, 6)
        self.engine.new('custom', team_id=self.team['id'])
        validate_plan(spec, 6, self.engine.tools.capabilities)
        with self.assertRaises(PermissionError):
            self.engine.tools.validate('researcher', 'shell', {})

    def test_web_sources_preserve_bytes_and_still_reject_executable_files(self):
        run = self.engine.new('web project', team_id=self.team['id'])
        task = {'owner': 'coder'}
        for path, content in [('style.css', 'body {color: green;}\n'), ('script.js', 'const title = "展览";\n')]:
            self.engine.tools.execute(run, task, 'write_file', {'path': path, 'content': content})
            artifact = self.store.archive(run, 'coder', path, 'web')
            self.assertEqual(self.store.artifact_bytes(artifact), content.encode())
        with self.assertRaises(ValueError):
            self.engine.tools.execute(run, task, 'write_file', {'path': 'run.exe', 'content': 'forbidden'})

    def test_verifier_excludes_authors_of_transitive_inputs(self):
        run = self.engine.new('Review a guide derived from research', team_id=self.team['id'])
        run['tasks'] = validate_plan([
            {'id': 'research', 'role': 'researcher', 'title': 'Research', 'objective': 'Research',
             'checks': [{'kind': 'exists', 'path': 'notes.md'}]},
            {'id': 'guide', 'role': 'coder', 'title': 'Guide', 'objective': 'Guide', 'depends_on': ['research'],
             'checks': [{'kind': 'exists', 'path': 'guide.md'}]},
            {'id': 'review', 'role': 'verifier', 'title': 'Review', 'objective': 'Independent review', 'depends_on': ['guide'],
             'checks': [{'kind': 'exists', 'path': 'review.md'}]},
        ], 6, self.engine.tools.capabilities)
        run['tasks'][0].update(owner='researcher', status='succeeded')
        run['tasks'][1].update(owner='coder', status='succeeded')
        with patch.object(self.provider, 'complete', return_value={'accept': True, 'confidence': 1, 'reason': 'Qualified'}):
            self.engine.assign(run, run['tasks'][2])
        self.assertEqual({bid['agent'] for bid in run['tasks'][2]['bids']}, {'coordinator', 'verifier'})
        self.assertEqual(run['tasks'][2]['owner'], 'verifier')

    def test_custom_role_names_map_only_when_unambiguous(self):
        run = self.engine.new('roles', team_id=self.team['id'])
        self.assertEqual(self.engine.role_spec(run, {'role': 'Guide_designer'})['role'], 'coder')
        self.assertEqual(self.engine.role_spec(run, {'role': 'access-reviewer'})['role'], 'verifier')
        run['team_snapshot']['roles']['researcher']['name'] = 'Guide-designer'
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.engine.role_spec(run, {'role': 'Guide designer'})
        self.assertEqual(self.engine.role_spec(run, {'role': 'coder'})['role'], 'coder')

    def test_invalid_plans_receive_feedback_and_are_bounded(self):
        invalid = {'tasks': [{'id': 'x', 'role': 'nonexistent', 'title': 'x', 'objective': 'x',
                             'checks': [{'kind': 'exists', 'path': 'slides.html'}]}]}
        contexts = []
        def bad_plan(purpose, context):
            self.assertEqual(purpose, 'plan')
            contexts.append(copy.deepcopy(context))
            return invalid
        with patch.object(self.provider, 'complete', side_effect=bad_plan):
            run = self.engine.advance(self.engine.new('invalid plans', team_id=self.team['id']))
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['model_calls'], 3)
        self.assertEqual(run['steps'], 0)
        self.assertIn('validation_error', contexts[1])
        self.assertEqual(sum(e['kind'] == 'plan_received' for e in self.store.events(run['id'])), 3)

    def test_planner_can_correct_a_role_before_work_starts(self):
        original = self.provider.complete
        attempts = []
        def corrected(purpose, context):
            if purpose != 'plan': return original(purpose, context)
            attempts.append(context)
            role = 'unknown' if len(attempts) == 1 else 'Archivist'
            return {'tasks': [{'id': 'guide', 'role': role, 'title': 'Museum', 'objective': 'Museum guide',
                              'checks': [{'kind': 'exists', 'path': 'slides.html'}]}]}
        with patch.object(self.provider, 'complete', side_effect=corrected):
            run = self.engine.advance(self.engine.new('correct role', team_id=self.team['id']))
        self.assertEqual(run['status'], 'succeeded', run['failure'])
        self.assertEqual(run['tasks'][0]['role'], 'researcher')


@unittest.skipIf(TestClient is None, 'Install Web test dependencies')
class TeamWebTests(unittest.TestCase):
    setUp = web_tests.WebTests.setUp
    tearDown = web_tests.WebTests.tearDown
    bootstrap = web_tests.WebTests.bootstrap
    wait_idle = web_tests.WebTests.wait_idle

    def test_group_chat_task_resume_download_export_and_isolation(self):
        response = self.client.post('/api/teams', json={'name': 'Museum', 'roles': ROLES})
        self.assertEqual(response.status_code, 201, response.text)
        team = response.json()
        other = self.client.post('/api/teams', json={'name': 'Separate', 'roles': ROLES}).json()
        provider = MuseumProvider()
        with patch('evotask.web.app.make_provider', return_value=provider):
            self.assertEqual(self.client.post('/api/chat', json={'team_id': team['id'], 'message': 'Discuss museum'}).status_code, 200)
            self.assertEqual(self.client.get('/api/teams/' + team['id']).json()['runs'], [])
            run = self.client.post('/api/runs', json={'team_id': team['id'], 'goal': 'Museum guide'}).json()
            waiting = self.wait_idle(run['id'])
            reply = {'request_id': waiting['pending']['id'], 'reply': 'Ceramics', 'team_id': other['id']}
            self.assertEqual(self.client.post(f"/api/runs/{run['id']}/resume", json=reply).status_code, 404)
            reply['team_id'] = team['id']
            self.assertEqual(self.client.post(f"/api/runs/{run['id']}/resume", json=reply).status_code, 202)
            waiting = self.wait_idle(run['id'])
            self.assertEqual(waiting['pending']['kind'], 'acceptance')
            self.client.post(f"/api/runs/{run['id']}/resume", json={'team_id': team['id'], 'request_id': waiting['pending']['id'], 'reply': 'yes'})
            done = self.wait_idle(run['id'])
        artifact = next(a for a in done['artifacts'] if a['path'] == 'slides.html')
        download = self.client.get(f"/api/runs/{run['id']}/artifacts/{artifact['id']}?download=true")
        self.assertEqual(download.content, SLIDES.encode())
        self.assertIn('attachment', download.headers['content-disposition'])
        export = self.client.get(f"/api/teams/{team['id']}/export").json()
        self.assertEqual(len(export['runs']), 1)
        self.assertTrue(export['messages'])
        self.assertTrue(export['memory']['researcher'])
        bundle_url = f"/api/teams/{team['id']}/evaluation.zip"
        response = self.client.get(bundle_url)
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else '')
        self.assertEqual(response.headers['content-type'], 'application/zip')
        self.assertEqual(int(response.headers['content-length']), len(response.content))
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(len(manifest['artifacts']), len(done['artifacts']))
            for item in manifest['artifacts']:
                self.assertEqual(digest(archive.read(item['archive_path'])), item['sha256'])
            self.assertIn('Ceramics', archive.read('dialogue.md').decode())
        from evotask.exports import EvaluationExportError
        with patch('evotask.web.app.evaluation_zip', side_effect=EvaluationExportError('Archive integrity failure')):
            error = self.client.get(bundle_url)
            self.assertEqual(error.status_code, 409)
            self.assertIn('Archive integrity failure', error.json()['detail'])
        isolated = self.client.get('/api/teams/' + other['id']).json()
        self.assertEqual(isolated['messages'], [])
        self.assertEqual(isolated['runs'], [])
        self.assertFalse(any(isolated['memory'].values()))
        stranger = TestClient(self.app); self.bootstrap(stranger)
        self.assertEqual(stranger.get('/api/teams/' + team['id']).status_code, 404)
        self.assertEqual(self.client.get('/api/teams/' + team['id'] + '?mode=live').status_code, 404)
        self.assertEqual(stranger.get(bundle_url).status_code, 404)
        self.assertEqual(self.client.get(bundle_url + '?mode=live').status_code, 404)


if __name__ == '__main__':
    unittest.main()
