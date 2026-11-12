from __future__ import annotations

import collections
import heapq
import json
import math
import re
from pathlib import Path

from .documents import file_digest
from .collaboration import valid_source_url
from .storage import AGENTS, Store, digest, uid


def terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    for phrase in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(phrase)
        words.extend(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return words


def text_chunks(source: Path):
    """Bounded fragments, including single-line files of arbitrary length."""
    line_no, start, buffer = 1, 1, ''
    with source.open(encoding='utf-8-sig') as stream:
        while fragment := stream.readline(1200):
            if buffer and len(buffer) + len(fragment) > 1200:
                yield {'text': buffer.rstrip('\n'), 'start': start, 'end': max(start, line_no - buffer.endswith('\n'))}
                buffer = ''
            if not buffer:
                start = line_no
            buffer += fragment
            if fragment.endswith('\n'):
                line_no += 1
        if buffer:
            yield {'text': buffer.rstrip('\n'), 'start': start, 'end': max(start, line_no - buffer.endswith('\n'))}


class KnowledgeBase:
    """Disk-backed BM25 index; scope filtered before statistics and scoring."""
    def __init__(self, store: Store):
        self.path = store.root / 'knowledge.json'
        self.db = store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS knowledge_documents(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL,
                scope TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_chunks(
                doc_id TEXT NOT NULL, position INTEGER NOT NULL, start INTEGER, end INTEGER,
                text TEXT NOT NULL, counts TEXT NOT NULL, term_count INTEGER NOT NULL,
                PRIMARY KEY(doc_id, position));
            CREATE TABLE IF NOT EXISTS knowledge_migrations(name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS knowledge_bases(id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_file_cleanup(id TEXT PRIMARY KEY, document TEXT NOT NULL);
        ''')
        if 'base_id' not in {row['name'] for row in self.db.execute('PRAGMA table_info(knowledge_documents)')}:
            self.db.execute("ALTER TABLE knowledge_documents ADD COLUMN base_id TEXT NOT NULL DEFAULT 'kb_default'")
        if not self.db.execute("SELECT 1 FROM knowledge_bases WHERE id='kb_default'").fetchone():
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO knowledge_bases VALUES('kb_default','我的知识库','个人资料与长期参考')")
        if self.path.exists() and not self.db.execute("SELECT 1 FROM knowledge_migrations WHERE name='json-v1'").fetchone():
            with self.db:
                inserted = self.db.execute("INSERT OR IGNORE INTO knowledge_migrations VALUES('json-v1')").rowcount
                if inserted:
                    for doc in json.loads(self.path.read_text(encoding='utf-8')):
                        self._replace(doc, iter(doc.get('chunks', [])))

    def _replace(self, doc: dict, chunks):
        self.db.execute('DELETE FROM knowledge_chunks WHERE doc_id=?', (doc['id'],))
        metadata = {k: v for k, v in doc.items() if k not in {'id', 'name', 'version', 'scope', 'chunks'}}
        self.db.execute('INSERT OR REPLACE INTO knowledge_documents(id,name,version,scope,metadata,base_id) VALUES(?,?,?,?,?,?)',
                        (doc['id'], doc['name'], doc['version'], doc['scope'], json.dumps(metadata, ensure_ascii=False), doc.get('base_id', 'kb_default')))
        for index, chunk in enumerate(chunks):
            counts = collections.Counter(terms(chunk['text']))
            self.db.execute('INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?)',
                            (doc['id'], index, chunk['start'], chunk['end'], chunk['text'],
                             json.dumps(counts, ensure_ascii=False), sum(counts.values())))

    def summaries(self) -> list[dict]:
        return [json.loads(row['metadata']) | {k: row[k] for k in ('id', 'name', 'version', 'scope', 'chunks', 'base_id')}
                for row in self.db.execute('''SELECT d.*, (SELECT COUNT(*) FROM knowledge_chunks c
                    WHERE c.doc_id=d.id) AS chunks FROM knowledge_documents d ORDER BY d.rowid''')]

    def documents(self) -> list[dict]:
        return [doc | {'chunks': [dict(row) for row in self.db.execute(
            'SELECT text,start,end FROM knowledge_chunks WHERE doc_id=? ORDER BY position', (doc['id'],))]}
                for doc in self.summaries()]

    def bases(self) -> list[dict]:
        return [dict(row) for row in self.db.execute('''SELECT b.*,COUNT(d.id) AS documents FROM knowledge_bases b
            LEFT JOIN knowledge_documents d ON d.base_id=b.id GROUP BY b.id ORDER BY b.rowid''')]

    def validate_bases(self, identifiers: list[str] | None) -> None:
        if identifiers is not None and (not isinstance(identifiers, list) or any(not isinstance(i, str) for i in identifiers)
                                      or len(set(identifiers)) != len(identifiers)
                                      or not set(identifiers) <= {b['id'] for b in self.bases()}):
            raise ValueError('Unknown or duplicate knowledge base in this workspace')

    def save_base(self, name: str, description: str = '', identifier: str | None = None) -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not isinstance(description, str) or len(description) > 1000:
            raise ValueError('Knowledge base needs a name (1–80 characters) and description (up to 1000)')
        if identifier:
            self.validate_bases([identifier])
        identifier = identifier or uid('kb')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO knowledge_bases VALUES(?,?,?)', (identifier, name.strip(), description.strip()))
        return next(b for b in self.bases() if b['id'] == identifier)

    def deletion_preview(self, identifier: str) -> dict:
        self.validate_bases([identifier])
        base = next(b for b in self.bases() if b['id'] == identifier)
        documents = [d for d in self.summaries() if d['base_id'] == identifier]
        revision = digest(json.dumps({'base': base, 'documents': sorted(documents, key=lambda d: d['id'])},
                                     ensure_ascii=False, sort_keys=True).encode())
        return base | {'revision': revision, 'is_default': identifier == 'kb_default',
                       'document_names': [d['name'] for d in documents[:10]]}

    def remove_base(self, identifier: str, *, delete_documents: bool = False) -> dict:
        self.validate_bases([identifier])
        if identifier == 'kb_default' and not delete_documents:
            raise ValueError('Default knowledge base can be renamed but cannot be deleted')
        count = self.db.execute('SELECT COUNT(*) FROM knowledge_documents WHERE base_id=?', (identifier,)).fetchone()[0]
        if count and not delete_documents:
            raise ValueError('Move or delete this knowledge base’s documents before deleting it')
        with self.db:
            # Persist cleanup work in the same transaction: a restart or locked file
            # must not leave an untracked downloadable copy after index deletion.
            for row in self.db.execute('SELECT * FROM knowledge_documents WHERE base_id=?', (identifier,)):
                doc = json.loads(row['metadata']) | {k: row[k] for k in ('id', 'name', 'scope', 'base_id')}
                self.db.execute('INSERT OR REPLACE INTO knowledge_file_cleanup VALUES(?,?)',
                                (row['id'], json.dumps(doc, ensure_ascii=False)))
            self.db.execute('DELETE FROM knowledge_chunks WHERE doc_id IN (SELECT id FROM knowledge_documents WHERE base_id=?)', (identifier,))
            self.db.execute('DELETE FROM knowledge_documents WHERE base_id=?', (identifier,))
            if identifier != 'kb_default':
                self.db.execute('DELETE FROM knowledge_bases WHERE id=?', (identifier,))
        return {'removed': identifier != 'kb_default', 'cleared': identifier == 'kb_default', 'deleted_documents': count}

    def update_document(self, identifier: str, base_id: str, scope: str, source_url: str, description: str) -> dict:
        self.validate_bases([base_id])
        if scope not in {'shared', *AGENTS} or len(description) > 2000:
            raise ValueError('Invalid document scope or description')
        source_url = valid_source_url(source_url)
        doc = next((d for d in self.summaries() if d['id'] == identifier), None)
        if not doc:
            raise ValueError('Unknown document')
        if any(d['id'] != identifier and d['name'] == doc['name'] and d['scope'] == scope and d['base_id'] == base_id for d in self.summaries()):
            raise ValueError('A document with this name and scope already exists in the destination')
        metadata = {k: v for k, v in doc.items() if k not in {'id','name','version','scope','chunks','base_id'}}
        metadata.update(source_url=source_url, description=description)
        with self.db:
            self.db.execute('UPDATE knowledge_documents SET base_id=?,scope=?,metadata=? WHERE id=?',
                            (base_id, scope, json.dumps(metadata, ensure_ascii=False), identifier))
        return next(d for d in self.summaries() if d['id'] == identifier)

    def add(self, source: Path, scope: str = 'shared', *, name: str | None = None,
            identity: str | None = None, version: str | None = None, metadata: dict | None = None,
            base_id: str = 'kb_default', document_id: str | None = None) -> dict:
        self.validate_bases([base_id])
        if scope not in {'shared', *AGENTS}:
            raise ValueError('Scope must be shared or an agent ID')
        if source.suffix.lower() not in {'.md', '.txt'}:
            raise ValueError('Knowledge documents must be normalized .md or .txt')
        if not source.is_file():
            raise ValueError('Knowledge document missing')
        doc = {'id': document_id or 'doc_' + digest((identity or str(source.resolve())).encode())[:12],
               'name': name or source.name, 'version': version or file_digest(source), 'scope': scope,
               **(metadata or {}), 'base_id': base_id}
        with self.db:
            existing = self.db.execute('SELECT version FROM knowledge_documents WHERE id=?', (doc['id'],)).fetchone()
            if existing and existing['version'] == doc['version'] and metadata is None:
                # Retain migrated chunk positions/citations when re-adding unchanged CLI/demo data.
                self.db.execute('UPDATE knowledge_documents SET name=?,scope=? WHERE id=?',
                                (doc['name'], scope, doc['id']))
            else:
                self._replace(doc, text_chunks(source))
        return {key: doc[key] for key in ('id', 'name', 'version', 'scope')} | (metadata or {})

    def remove(self, doc_id: str) -> None:
        with self.db:
            if not self.db.execute('DELETE FROM knowledge_documents WHERE id=?', (doc_id,)).rowcount:
                raise ValueError('Unknown document ID')
            self.db.execute('DELETE FROM knowledge_chunks WHERE doc_id=?', (doc_id,))

    def search(self, query: str, agent: str, limit: int = 4, base_ids: list[str] | None = None) -> list[dict]:
        if agent not in AGENTS:
            raise ValueError('Invalid requesting agent')
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError('Query must contain 1–2000 characters')
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError('Retrieval limit must be between 1 and 8')
        params = [agent]
        sql = '''SELECT c.*,d.name,d.version,d.base_id,d.metadata FROM knowledge_chunks c JOIN knowledge_documents d ON c.doc_id=d.id
                 WHERE d.scope IN ('shared',?)'''
        if base_ids is not None:
            if not base_ids:
                return []
            sql += ' AND d.base_id IN (' + ','.join('?' for _ in base_ids) + ')'
            params.extend(base_ids)
        sql += ' ORDER BY c.doc_id,c.position'
        query_terms = set(terms(query))
        n, length_total, frequency = 0, 0, collections.Counter()
        for row in self.db.execute(sql, params):
            counts = json.loads(row['counts'])
            n += 1
            length_total += row['term_count']
            frequency.update(term for term in query_terms if term in counts)
        if not n:
            return []
        average = length_total / n or 1
        best = []
        for row in self.db.execute(sql, params):
            counts, score = json.loads(row['counts']), 0.0
            for term in query_terms:
                count = counts.get(term, 0)
                idf = math.log(1 + (n - frequency[term] + .5) / (frequency[term] + .5))
                score += idf * count * 2.5 / (count + 1.5 * (.25 + .75 * row['term_count'] / average))
            if score > 0:
                citation = f"{row['doc_id']}:{row['version'][:8]}:{row['position']}"
                item = {'text': row['text'], 'start': row['start'], 'end': row['end'], 'source': row['name'],
                        'version': row['version'], 'citation': citation, 'score': round(score, 5),
                        'base_id': row['base_id'], 'source_url': json.loads(row['metadata']).get('source_url', '')}
                heapq.heappush(best, (item['score'], citation, item))
                if len(best) > limit:
                    heapq.heappop(best)
        return sorted((item for _, _, item in best), key=lambda item: (-item['score'], item['citation']))


def recall(store: Store, agent: str, query: str, limit: int = 3) -> list[dict]:
    requested = set(terms(query))
    ranked = []
    for memory in store.memory(agent):
        overlap = len(requested & set(terms(memory.get("trigger", "") + " " + memory.get("lesson", ""))))
        if overlap and memory.get("verified"):
            ranked.append((overlap, memory))
    return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit]]
