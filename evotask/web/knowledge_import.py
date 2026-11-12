"""Copy a browser's guest libraries into an account without replacing its data."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..retrieval import KnowledgeBase
from ..storage import Store, safe_path, uid


def import_key(sid: str) -> str:
    return 'guest_import:' + sid


def already_imported(target: Store, sid: str) -> bool:
    KnowledgeBase(target)
    return bool(target.db.execute('SELECT 1 FROM knowledge_migrations WHERE name=?', (import_key(sid),)).fetchone())


def copy_knowledge(source: Store, target: Store, sid: str) -> dict:
    """Call with both workspace locks held. Index + marker commit together.

    Fresh IDs prevent collisions. Copy chunks verbatim (including positions and
    versions), plus raw and normalized uploads, with bounded memory. A failed
    copy rolls back database writes and removes only files made by this call.
    Guest originals and prior task citations are never modified.
    """
    source_kb = KnowledgeBase(source)
    if already_imported(target, sid):
        return {'imported': False, 'documents': 0, 'bases': 0}
    documents = source_kb.summaries()
    bases = [b for b in source_kb.bases() if b['id'] != 'kb_default' or b['documents']]
    if not bases:
        return {'imported': False, 'documents': 0, 'bases': 0}
    base_map = {b['id']: uid('kb') for b in bases}
    files, folders = [], []
    try:
        with target.db:
            for base in bases:
                target.db.execute('INSERT INTO knowledge_bases VALUES(?,?,?)',
                                  (base_map[base['id']], (base['name'][:72] + ' · 访客导入'), base['description']))
            for doc in documents:
                doc_id, base_id = uid('doc'), base_map[doc['base_id']]
                metadata = {k: v for k, v in doc.items() if k not in {'id', 'name', 'version', 'scope', 'base_id', 'chunks'}}
                if doc.get('upload_id'):
                    upload_id = uid('upload')
                    relative = 'uploads/' + doc['upload_id']
                    dst = safe_path(target.root, 'uploads/' + upload_id)
                    dst.mkdir(parents=True, exist_ok=False)
                    folders.append(dst)
                    # Only this upload's known direct files are eligible; no directory traversal/copytree.
                    original = 'original' + Path(doc['name']).suffix
                    for name in (original, 'text.txt'):
                        source_file = safe_path(source.root, relative + '/' + name)
                        if name == original or source_file.exists():
                            copied = safe_path(target.root, 'uploads/' + upload_id + '/' + name)
                            files.append(copied)
                            shutil.copyfile(source_file, copied)
                    info = json.loads(safe_path(source.root, relative + '/metadata.json').read_text(encoding='utf-8'))
                    info.update(id=upload_id, document_id=doc_id, base_id=base_id, scope=doc['scope'],
                                source_url=doc.get('source_url', ''), description=doc.get('description', ''))
                    files.append(dst / 'metadata.json')
                    (dst / 'metadata.json').write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
                    metadata['upload_id'] = upload_id
                target.db.execute('INSERT INTO knowledge_documents(id,name,version,scope,metadata,base_id) VALUES(?,?,?,?,?,?)',
                                  (doc_id, doc['name'], doc['version'], doc['scope'], json.dumps(metadata, ensure_ascii=False), base_id))
                cursor = source.db.execute('SELECT * FROM knowledge_chunks WHERE doc_id=? ORDER BY position', (doc['id'],))
                for row in cursor:
                    target.db.execute('INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?)',
                                      (doc_id, row['position'], row['start'], row['end'], row['text'], row['counts'], row['term_count']))
            target.db.execute('INSERT INTO knowledge_migrations VALUES(?)', (import_key(sid),))
    except BaseException:
        for file in files:
            file.unlink(missing_ok=True)
        for folder in reversed(folders):
            folder.rmdir()
        raise
    return {'imported': True, 'documents': len(documents), 'bases': len(bases)}
