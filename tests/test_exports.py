import copy
import io
import json
import time
import zipfile

from evotask.engine import Engine, task_from_spec
from evotask.exports import EvaluationExportError, evaluation_zip, team_audit, zip_chunks
from evotask.storage import digest
from evotask.teams import Teams
from tests.test_core import TestBase
from tests.test_teams import ROLES


class EvaluationBundleTests(TestBase):
    def setUp(self):
        super().setUp()
        self.team = Teams(self.store).create('博物馆团队', ROLES)
        self.engine = Engine(self.store, self.config, None)

    def archived_run(self):
        run = self.engine.new('Deliver an accessible museum guide', team_id=self.team['id'])
        task = task_from_spec({'id': 'guide', 'role': 'coder', 'title': 'Guide', 'objective': 'Build guide',
                              'checks': [{'kind': 'exists', 'path': 'museum/数据.json'}]})
        task.update(owner='coder', status='succeeded', history=[{'action': {'type': 'finish'},
                    'result': {'checks': [{'kind': 'exists', 'passed': True}], 'review_note': '人工仍未批准整题'}}])
        run['tasks'] = [task]
        path = self.store.workspace('coder', run['id']) / 'museum/数据.json'
        path.parent.mkdir(parents=True)
        contents = [b'{"visitors":37}\r\n', '{"visitors":38,"title":"陶器展"}\r\n'.encode()]
        for status, content in zip(('superseded', 'verified'), contents):
            path.write_bytes(content)
            self.store.archive(run, 'coder', 'museum/数据.json', 'guide')['verification'] = status
        task['artifact_ids'] = [run['artifacts'][-1]['id']]
        run.update(status='failed', failure='External review failed', ended=time.time())
        self.store.save(run)
        return run, contents

    def test_full_history_versions_and_bytes_without_other_channels(self):
        run, contents = self.archived_run()
        for i in range(120):
            self.store.message('group', 'human', f'历史消息 {i}\n```md\n原样保留\n```')
        self.store.message('private:coder', 'human', 'EXCLUDED_PRIVATE_MESSAGE')
        self.store.event(run['id'], 'verifier', 'attempt_failed', {'reason': '保持原始失败信息'})
        for i in range(31):
            later = self.engine.new(f'Follow-up {i}', team_id=self.team['id'])
            later.update(status='cancelled', ended=time.time()); self.store.save(later)
        other = Teams(self.store).create('Other', ROLES)
        self.engine.new('EXCLUDED_OTHER_TEAM', team_id=other['id'])
        self.store.bind_team(None)
        self.store.message('group', 'human', 'EXCLUDED_DEFAULT_GROUP')
        audit = team_audit(self.store, self.team['id'], 'demo')
        self.assertEqual(len(audit['runs']), 32)
        self.assertGreater(len(audit['messages']), 100)
        with evaluation_zip(self.store, audit) as stream, zipfile.ZipFile(stream) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            saved = json.loads(archive.read('audit.json'))
            self.assertEqual(saved, audit)
            self.assertEqual(set(archive.namelist()), {f['path'] for f in manifest['files']} | {'manifest.json'})
            for file in manifest['files']:
                data = archive.read(file['path'])
                self.assertEqual(digest(data), file['sha256'])
                self.assertEqual(len(data), file['bytes'])
            records = manifest['artifacts']
            self.assertEqual(len(records), 2)
            self.assertEqual([archive.read(a['archive_path']) for a in records], contents)
            self.assertNotEqual(records[0]['archive_path'], records[1]['archive_path'])
            self.assertEqual([a['current_task_output'] for a in records], [False, True])
            self.assertEqual([a['verification'] for a in records], ['superseded', 'verified'])
            self.assertTrue(all(a['run_status'] == 'failed' for a in records))
            self.assertFalse(manifest['coverage']['raw_model_api_transcript'])
            transcript = archive.read('dialogue.md').decode()
            for text in ('历史消息 0', '历史消息 119', '保持原始失败信息', '人工仍未批准整题', 'External review failed'):
                self.assertIn(text, transcript)
            self.assertIn('````text', transcript)
            self.assertNotIn('EXCLUDED_', transcript + archive.read('audit.json').decode())

    def test_empty_team_and_waiting_run_are_exportable(self):
        with evaluation_zip(self.store, team_audit(self.store, self.team['id'], 'demo')) as stream:
            with zipfile.ZipFile(stream) as archive:
                self.assertEqual(json.loads(archive.read('manifest.json'))['artifacts'], [])
        run = self.engine.new('Need clarification', team_id=self.team['id'])
        self.engine.wait(run, None, 'planning', 'Which gallery?')
        with evaluation_zip(self.store, team_audit(self.store, self.team['id'], 'demo')) as stream:
            with zipfile.ZipFile(stream) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['runs'][0]['status'], 'waiting_human')
                self.assertEqual(manifest['runs'][0]['pending']['question'], 'Which gallery?')

    def test_missing_or_corrupt_archive_fails_instead_of_omitting_output(self):
        run, _ = self.archived_run()
        audit = team_audit(self.store, self.team['id'], 'demo')
        artifact = run['artifacts'][0]
        blob = self.store.root / 'blobs' / artifact['sha256']
        blob.write_bytes(b'changed')
        with self.assertRaisesRegex(EvaluationExportError, artifact['id']):
            evaluation_zip(self.store, audit)
        blob.unlink()
        with self.assertRaisesRegex(EvaluationExportError, artifact['id']):
            evaluation_zip(self.store, audit)

    def test_unsafe_paths_and_metadata_are_rejected(self):
        self.archived_run()
        original = team_audit(self.store, self.team['id'], 'demo')
        for key, value in [('path', '../outside.txt'), ('path', 'C:/outside.txt'),
                           ('path', 'museum/../../outside.txt'), ('id', '../bad'), ('sha256', '../bad'), ('bytes', 1)]:
            with self.subTest(key=key, value=value):
                audit = copy.deepcopy(original)
                audit['runs'][0]['run']['artifacts'][0][key] = value
                with self.assertRaises(EvaluationExportError):
                    evaluation_zip(self.store, audit)

    def test_download_stream_closes_on_completion_or_disconnect(self):
        for interrupted in (False, True):
            stream = io.BytesIO(b'x' * 100000)
            chunks = zip_chunks(stream)
            if interrupted:
                self.assertTrue(next(chunks)); chunks.close()
            else:
                self.assertEqual(len(b''.join(chunks)), 100000)
            self.assertTrue(stream.closed)
