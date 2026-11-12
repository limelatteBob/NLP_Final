from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from evotask.config import Config
from evotask.engine import Engine
from evotask.retrieval import KnowledgeBase, text_chunks
from evotask.storage import Store, atomic_json
from evotask.tools import Tools

try:
    from fastapi.testclient import TestClient
    from evotask.web.app import create_app
    import pypdf
    import defusedxml
    import openpyxl
except ImportError:
    TestClient = None


def text_pdf(text='quasar research evidence'):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f'BT /F1 12 Tf 40 300 Td ({text}) Tj ET'.encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@unittest.skipIf(TestClient is None, 'Install .[web,test-web] for document upload tests')
class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='evotask-doc-test-')
        self.root = Path(self.temp.name)
        self.app = create_app(Config(provider='demo', model='fixture'), self.root / 'web')
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.headers['X-EvoTask-CSRF'] = self.client.get('/api/session').json()['csrf']

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def upload(self, name, content, **params):
        response = self.client.post('/api/uploads', params={'name': name, **params}, content=content,
                                    headers={'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_binary_pdf_extracts_real_text_and_downloads_original(self):
        original = text_pdf()
        result = self.upload('论文.pdf', original, scope='researcher')
        self.assertEqual(result['status'], 'indexed', result)
        hits = self.client.get('/api/knowledge/search', params={'query': 'quasar', 'agent': 'researcher'}).json()
        self.assertTrue(hits)
        self.assertEqual(hits[0]['source'], '论文.pdf')
        self.assertIn('第 1 页', hits[0]['text'])
        self.assertFalse(self.client.get('/api/knowledge/search?query=quasar&agent=coder').json())
        response = self.client.get(f"/api/uploads/{result['id']}/download")
        self.assertEqual(response.content, original)
        other = TestClient(self.app)
        other.get('/api/session')
        self.assertEqual(other.get(f"/api/uploads/{result['id']}/download").status_code, 404)
        other.close()

    def test_word_slides_sheets_and_text_formats(self):
        xml = '<root><p><t>nebula 文档证据</t></p></root>'
        for name, member in [('plan.docx', 'word/document.xml'), ('slides.pptx', 'ppt/slides/slide1.xml'), ('note.odt', 'content.xml')]:
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr(member, xml)
            result = self.upload(name, output.getvalue())
            self.assertEqual(result['status'], 'indexed', result)
        workbook = openpyxl.Workbook()
        workbook.active.append(['nebula', '工作表证据'])
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        self.assertEqual(self.upload('sheet.xlsx', output.getvalue())['status'], 'indexed')
        for name, value in [('records.csv', 'nebula,value\n资料,1'), ('unknown.custom', 'nebula 文本'),
                            ('note.rtf', r'{\rtf1\ansi nebula evidence}'), ('note.json', '{"nebula": "资料"}')]:
            self.assertEqual(self.upload(name, value.encode())['status'], 'indexed')
        hits = self.client.get('/api/knowledge/search?query=nebula').json()
        self.assertTrue(hits)

    def test_large_stream_and_long_line_are_not_rejected(self):
        # More than both former 256 KB and 2 MB limits, sent as many chunks.
        block = ('quasar passage with evidence.\n' * 2400).encode()
        result = self.upload('large.txt', iter([block] * 50))
        self.assertGreater(result['bytes'], 2_000_000)
        self.assertEqual(result['status'], 'indexed')
        self.assertTrue(self.client.get('/api/knowledge/search?query=quasar').json())
        long = ('单行资料 ' * 70000).encode()
        result = self.upload('line.log', long)
        self.assertEqual(result['status'], 'indexed')
        self.assertTrue(self.client.get('/api/knowledge/search?query=资料').json())

    def test_unreadable_and_scanned_files_are_saved_without_fake_index(self):
        for name, content in [('image.png', b'\x89PNG\x00\x00\xff'), ('archive.zip', b'PK\x00\x00'),
                              ('old.doc', b'\xd0\xcf\x11\xe0'), ('empty.txt', b'')]:
            result = self.upload(name, content)
            self.assertEqual(result['status'], 'stored', result)
            doc = next(d for d in self.client.get('/api/knowledge').json() if d['name'] == name)
            self.assertEqual(doc['chunks'], 0)
            self.assertEqual(self.client.get(f"/api/uploads/{result['id']}/download").content, content)
        writer, output = pypdf.PdfWriter(), io.BytesIO()
        writer.add_blank_page(width=100, height=100)
        writer.write(output)
        self.assertEqual(self.upload('scanned.pdf', output.getvalue())['status'], 'stored')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('word/document.xml', '<root><p/></root>')
        self.assertEqual(self.upload('empty.docx', output.getvalue())['status'], 'stored')
        workbook, output = openpyxl.Workbook(), io.BytesIO()
        workbook.save(output)
        workbook.close()
        self.assertEqual(self.upload('empty.xlsx', output.getvalue())['status'], 'stored')

    def test_utf16_html_and_upload_auth(self):
        self.assertEqual(self.upload('中文.txt', '中文编码 quasar'.encode('utf-16'))['status'], 'indexed')
        self.upload('page.html', b'<p>visible evidence</p><script>hiddenword</script>')
        self.assertTrue(self.client.get('/api/knowledge/search?query=visible').json())
        self.assertFalse(self.client.get('/api/knowledge/search?query=hiddenword').json())
        self.assertEqual(self.client.post('/api/uploads?name=../escape.pdf', content=b'x').status_code, 422)
        self.assertEqual(self.client.post('/api/uploads?name=x.pdf', content=b'x', headers={'X-EvoTask-CSRF': 'bad'}).status_code, 403)
        self.assertEqual(self.client.post('/api/uploads?name=x.pdf', content=b'x', headers={'Origin': 'https://evil.invalid'}).status_code, 403)

    def test_reupload_replaces_index_and_removes_obsolete_raw_copy(self):
        before = self.upload('same.csv', b'quasar first version')
        after = self.upload('same.csv', b'nebula second version')
        self.assertEqual(before['document_id'], after['document_id'])
        self.assertNotEqual(before['sha256'], after['sha256'])
        self.assertFalse(self.client.get('/api/knowledge/search?query=quasar').json())
        self.assertEqual(self.client.get(f"/api/uploads/{before['id']}/download").status_code, 404)
        self.assertEqual(self.client.request('DELETE', '/api/knowledge/' + after['document_id'], json={}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/uploads/{after['id']}/download").status_code, 404)

    def test_large_task_attachment_reaches_engine_and_is_pageable(self):
        content = '中文证据\n' * 70000 + 'END_MARKER'
        result = self.upload('large-input.md', content.encode(), purpose='task', mode='live')
        with StoreContext(self.root / 'engine') as store:
            source = self.root / 'engine-input.txt'
            source.write_text(content, encoding='utf-8')
            cfg = Config(provider='demo', model='fixture')
            run = Engine(store, cfg, None).new('read all input', [source])
            tool = Tools(store, cfg)
            task = {'owner': 'researcher'}
            first = tool.execute(run, task, 'read_file', {'path': run['initial_inputs'][0]})
            self.assertTrue(first['truncated'])
            second = tool.execute(run, task, 'read_file', {'path': run['initial_inputs'][0], 'offset': first['next_offset']})
            self.assertEqual(first['content'] + second['content'], content[:32000])
            last = tool.execute(run, task, 'read_file', {'path': run['initial_inputs'][0], 'offset': len(content) - 10})
            self.assertEqual(last['content'], 'END_MARKER')
            self.assertFalse(last['truncated'])
        self.assertEqual(result['status'], 'ready')

    def test_task_api_uses_normalized_attachments_and_enforces_ownership(self):
        cfg = Config(provider='deepseek', model='test-fixture', api_key_env='EVOTASK_TEST_UPLOAD_KEY')
        app = create_app(cfg, self.root / 'task-api')
        # Submission is isolated from execution: no provider or paid model is called.
        with patch.dict(os.environ, {'EVOTASK_TEST_UPLOAD_KEY': 'test-only'}), TestClient(app) as client:
            client.headers['X-EvoTask-CSRF'] = client.get('/api/session').json()['csrf']
            uploaded = []
            for name, content in [('paper.pdf', text_pdf()), ('large.csv', b'quasar,value\n' * 210000),
                                  ('scan.png', b'\x89PNG\x00\xff')]:
                response = client.post('/api/uploads', params={'name': name, 'purpose': 'task', 'mode': 'live'}, content=content)
                self.assertEqual(response.status_code, 201)
                uploaded.append(response.json())
            other = TestClient(app)
            other.headers['X-EvoTask-CSRF'] = other.get('/api/session').json()['csrf']
            self.assertEqual(other.post('/api/runs?mode=live', json={'goal': 'research', 'upload_ids': [uploaded[0]['id']]}).status_code, 404)
            other.close()
            with patch('evotask.web.app.Jobs.submit') as submit:
                response = client.post('/api/runs?mode=live', json={'goal': 'research', 'upload_ids': [v['id'] for v in uploaded]})
                self.assertEqual(response.status_code, 202, response.text)
                submit.assert_called_once()
            run = response.json()
            self.assertEqual(len(run['initial_inputs']), 3)
            self.assertIn('paper.pdf', run['initial_inputs'][0])
            detail = client.get('/api/runs/' + run['id'] + '?mode=live').json()
            self.assertEqual([v['name'] for v in detail['uploaded_files']], ['paper.pdf', 'large.csv', 'scan.png'])
            root = submit.call_args.args[0]
            with StoreContext(root) as store:
                workspace = store.workspace('researcher', run['id'])
                self.assertIn('quasar', (workspace / run['initial_inputs'][0]).read_text(encoding='utf-8'))
                self.assertGreater((workspace / run['initial_inputs'][1]).stat().st_size, 2_000_000)
                self.assertIn('未读取正文', (workspace / run['initial_inputs'][2]).read_text(encoding='utf-8'))

    def test_failed_upload_releases_workspace_and_removes_incomplete_files(self):
        with patch('evotask.web.app.ingest_upload', side_effect=ValueError('simulated extraction failure')):
            response = self.client.post('/api/uploads?name=bad.txt', content=b'evidence')
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/api/knowledge').json(), [])
        self.assertEqual(list((self.root / 'web').glob('sessions/*/demo/uploads/*')), [])
        self.assertEqual(self.upload('working.txt', b'quasar')['status'], 'indexed')

    def test_json_index_migrates_once_preserving_citations_and_scope(self):
        with StoreContext(self.root / 'migration') as store:
            legacy = {'id': 'doc_123456789abc', 'name': 'legacy.txt', 'version': 'a' * 64, 'scope': 'researcher',
                      'chunks': [{'text': 'quasar legacy evidence', 'start': 7, 'end': 8}]}
            atomic_json(store.root / 'knowledge.json', [legacy])
            kb = KnowledgeBase(store)
            hit = kb.search('quasar', 'researcher')[0]
            self.assertEqual(hit['citation'], 'doc_123456789abc:aaaaaaaa:0')
            self.assertEqual(hit['start'], 7)
            self.assertEqual(kb.search('quasar', 'coder'), [])
            kb.remove(legacy['id'])
            self.assertEqual(KnowledgeBase(store).summaries(), [])


class StoreContext:
    def __init__(self, root):
        self.store = Store(root)
    def __enter__(self):
        return self.store
    def __exit__(self, *args):
        self.store.close()


if __name__ == '__main__':
    unittest.main()
