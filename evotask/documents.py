"""Local document extraction. Originals are never executed or sent to a service."""
from __future__ import annotations

import codecs
import hashlib
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator


class UnreadableDocument(ValueError):
    pass


def file_digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def text_encoding(source: Path) -> str:
    with source.open('rb') as stream:
        sample = stream.read(65536)
    for marker, encoding in ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
                             (codecs.BOM_UTF8, 'utf-8-sig'), (codecs.BOM_UTF16_LE, 'utf-16'),
                             (codecs.BOM_UTF16_BE, 'utf-16')):
        if sample.startswith(marker):
            return encoding
    if b'\x00' in sample or (sample and sum(c < 32 and c not in (9, 10, 12, 13) for c in sample) / len(sample) > .02):
        raise UnreadableDocument('此文件为二进制格式，当前无法提取正文；原件已保留。')
    try:
        codecs.getincrementaldecoder('utf-8')().decode(sample, final=False)
        return 'utf-8'
    except UnicodeDecodeError:
        from charset_normalizer import from_bytes
        detected = from_bytes(sample).best()
        if detected and detected.encoding:
            return detected.encoding
    raise UnreadableDocument('无法识别文本编码；原件已保留，可转换为 UTF-8 后重新上传。')


class HTMLText(HTMLParser):
    def __init__(self, output):
        super().__init__(convert_charrefs=True)
        self.output, self.hidden = output, 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if not self.hidden and tag in {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3'}:
            self.output.write('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {'p', 'div', 'li', 'tr'}:
            self.output.write('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.output.write(data)


def office_parts(source: Path, suffix: str) -> Iterator[str]:
    """Stream OOXML/OpenDocument paragraphs; never unzip to a directory."""
    from defusedxml.ElementTree import iterparse
    with zipfile.ZipFile(source) as archive:
        if suffix in {'.docx', '.docm'}:
            names = [n for n in archive.namelist() if re.fullmatch(r'word/(document|header\d+|footer\d+|footnotes|endnotes)\.xml', n)]
            names.sort(key=lambda n: (n != 'word/document.xml', n))
        elif suffix in {'.pptx', '.pptm'}:
            names = sorted((n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)),
                           key=lambda n: int(re.search(r'(\d+)\.xml', n)[1]))
        else:
            names = ['content.xml']
        if not names:
            raise UnreadableDocument('文档中未找到正文结构；原件已保留。')
        for name in names:
            heading = '\n[' + name + ']\n'
            with archive.open(name) as member:
                for _, element in iterparse(member, events=('end',)):
                    tag = element.tag.rsplit('}', 1)[-1]
                    if tag in {'p', 'h'}:
                        text = ''.join(element.itertext()).strip()
                        if text:
                            if heading:
                                yield heading
                                heading = ''
                            yield text + '\n'
                        element.clear()


def extract_document(source: Path, name: str, destination: Path) -> dict:
    """Write normalized UTF-8 text without a fixed upload/text byte ceiling.

    Parsing failure is distinct from successful indexing. Keep the original
    available for download even for encrypted, scanned or unsupported files.
    """
    suffix = Path(name).suffix.lower()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open('w', encoding='utf-8', newline='\n') as output:
            if suffix == '.pdf':
                from pypdf import PdfReader
                with source.open('rb') as stream:
                    if b'%PDF-' not in stream.read(1024):
                        raise UnreadableDocument('文件不包含有效的 PDF 标识，未提取正文；原件已保留。')
                reader = PdfReader(source)
                if reader.is_encrypted and not reader.decrypt(''):
                    raise UnreadableDocument('PDF 需要密码，未提取正文；请上传解密后的副本。')
                for index, page in enumerate(reader.pages, 1):
                    text = page.extract_text() or ''
                    if text.strip():
                        output.write(f'\n[第 {index} 页]\n{text}\n')
            elif suffix in {'.docx', '.docm', '.pptx', '.pptm', '.odt', '.odp', '.ods'}:
                for text in office_parts(source, suffix):
                    output.write(text)
            elif suffix in {'.xlsx', '.xlsm', '.xltx', '.xltm'}:
                from openpyxl import load_workbook
                workbook = load_workbook(source, read_only=True, data_only=False, keep_links=False)
                try:
                    for sheet in workbook:
                        heading = f'\n[工作表：{sheet.title}]\n'
                        for row in sheet.iter_rows(values_only=True):
                            text = '\t'.join('' if cell is None else str(cell) for cell in row)
                            if text.strip():
                                if heading:
                                    output.write(heading)
                                    heading = ''
                                output.write(text + '\n')
                finally:
                    workbook.close()
            elif suffix in {'.doc', '.xls', '.ppt'}:
                raise UnreadableDocument('旧版 Office 二进制文件已保存；请另存为 DOCX / XLSX / PPTX 以提取正文。')
            else:
                encoding = text_encoding(source)
                with source.open('r', encoding=encoding) as stream:
                    if suffix in {'.html', '.htm', '.xhtml'}:
                        parser = HTMLText(output)
                        while chunk := stream.read(65536):
                            parser.feed(chunk)
                        parser.close()
                    elif suffix == '.rtf':
                        from striprtf.striprtf import rtf_to_text
                        output.write(rtf_to_text(stream.read()))
                    else:
                        while chunk := stream.read(65536):
                            output.write(chunk)
        if not destination.stat().st_size:
            raise UnreadableDocument('未提取到正文；扫描 PDF / 图片需要先进行 OCR，原件已保留。')
        return {'status': 'ready', 'detail': '已提取正文，可供检索和任务使用。',
                'text_bytes': destination.stat().st_size}
    except UnreadableDocument as exc:
        detail = str(exc)
    except ImportError:
        detail = '服务端缺少文档解析依赖；原件已保留，请安装项目的 web 依赖。'
    except Exception:
        detail = '文档解析失败，可能是格式不匹配、损坏或加密；原件已保留。'
    destination.unlink(missing_ok=True)
    return {'status': 'stored', 'detail': detail, 'text_bytes': 0}
