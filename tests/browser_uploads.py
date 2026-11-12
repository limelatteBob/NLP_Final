"""Browser regression for binary, batch and large uploads; no model calls.

Run a local server, then `python -m tests.browser_uploads --url http://127.0.0.1:8000`.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from playwright.sync_api import sync_playwright, expect

from tests.test_documents import text_pdf


def fixtures():
    files = [('研究资料.pdf', text_pdf('nebula research evidence'))]
    word, output = Document(), io.BytesIO()
    word.add_paragraph('nebula Word 文档证据')
    word.save(output)
    files.append(('研究笔记.docx', output.getvalue()))
    slides, output = Presentation(), io.BytesIO()
    slide = slides.slides.add_slide(slides.slide_layouts[0])
    slide.shapes.title.text = 'nebula 演示证据'
    slides.save(output)
    files.append(('报告.pptx', output.getvalue()))
    workbook, output = Workbook(), io.BytesIO()
    workbook.active.append(['nebula', '表格证据'])
    workbook.save(output)
    workbook.close()
    files.append(('数据.xlsx', output.getvalue()))
    files.append(('大型语料.csv', b'nebula,value,evidence\n' * 300000))
    files.append(('未知格式.bin', b'\x00\xff\x12binary original'))
    files.append(('无扩展名资料', 'nebula 自定义文本资料'.encode()))
    return [{'name': name, 'mimeType': 'application/octet-stream', 'buffer': content} for name, content in files]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    output = Path('test-results/web')
    output.mkdir(parents=True, exist_ok=True)
    files, errors = fixtures(), []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1050})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url + '/#knowledge')
        expect(page.locator('#knowledge-file')).to_have_attribute('multiple', '')
        assert page.locator('#knowledge-file').get_attribute('accept') is None
        page.locator('#knowledge-file').set_input_files(files)
        expect(page.locator('.upload-file-list > div')).to_have_count(7)
        page.get_by_role('button', name='加入知识库').click()
        expect(page.locator('#modal')).not_to_be_visible(timeout=60000)
        expect(page.locator('.document-row')).to_have_count(7)
        for file in files:
            row = page.locator('.document-row').filter(has_text=file['name'])
            expect(row).to_contain_text('仅保存原件' if file['name'].endswith('.bin') else '已索引')
        large_size = f"{len(files[4]['buffer']) / 1024**2:.1f} MB"
        expect(page.locator('.document-row').filter(has_text='大型语料.csv')).to_contain_text(large_size)
        page.locator('#knowledge-query').fill('nebula')
        page.get_by_role('button', name='检索证据').click()
        expect(page.locator('.search-result').first).to_be_visible()
        expect(page.locator('#search-results')).not_to_contain_text('未知格式.bin')
        # The bytes offered by download are exactly the uploaded bytes, for parsed and unparsed files.
        for file in (files[0], files[4], files[5]):
            with page.expect_download() as pending:
                page.get_by_role('link', name='下载原件 ' + file['name'], exact=True).click()
            download = pending.value
            assert download.suggested_filename == file['name']
            assert hashlib.sha256(Path(download.path()).read_bytes()).digest() == hashlib.sha256(file['buffer']).digest()
        page.screenshot(path=str(output / 'desktop-upload-formats.png'), full_page=True, animations='disabled')
        page.reload()
        expect(page.locator('.document-row')).to_have_count(7)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile document overflow'
        page.screenshot(path=str(output / 'mobile-upload-formats.png'), full_page=True, animations='disabled')
        page.set_viewport_size({'width': 1440, 'height': 1050})
        page.goto(args.url + '/#home')
        page.locator('#mode-select').select_option('live')
        expect(page.locator('#goal')).not_to_have_attribute('readonly', '')
        assert page.locator('#task-files').get_attribute('accept') is None
        page.locator('#task-files').set_input_files(files)
        expect(page.locator('.attachment-chip')).to_have_count(7)
        expect(page.locator('#attachments')).to_contain_text(large_size)
        page.screenshot(path=str(output / 'desktop-large-attachments.png'), full_page=True, animations='disabled')
        # Stop here: live attachment execution is tested with mocked submission in test_documents.py.
        assert not errors, errors
        print(json.dumps({'status': 'passed', 'files': len(files), 'largest_bytes': len(files[4]['buffer']),
                          'checks': ['real-office-pdf', 'batch-upload', 'large-upload', 'raw-download-integrity',
                                     'unsupported-status', 'rag', 'reload', 'mobile-layout', 'task-attachment-selection'],
                          'page_errors': errors}, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
