"""Bounded local text/Markdown sources, atomic SQLite updates and FTS5 snapshots.

Uploads are never paths. Draft sources affect inference only after normal APPLY.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

MAX_FILE = 128 * 1024
MAX_TOTAL = 2 * 1024 * 1024
MAX_SOURCES = 32
MAX_CHUNKS = 3000
NAME = "knowledge.sqlite3"


class KnowledgeError(ValueError): pass


def safe_path(path):
    for p in (path, *path.absolute().parents):
        try: info = p.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise KnowledgeError("Knowledge storage path is unavailable")


def display_name(name):
    if (not isinstance(name, str) or not 1 <= len(name) <= 120 or name in {".", ".."}
            or any(c in name for c in '/\\:') or any(ord(c) < 32 for c in name)):
        raise KnowledgeError("Invalid display filename")
    return name


def chunks(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text or '\x00' in text: raise KnowledgeError("Empty or invalid UTF-8 text")
    result, start = [], 0
    while start < len(text):
        end = min(start + 1000, len(text))
        if end < len(text):
            split = text.rfind(' ', start + 600, end)
            if split > start: end = split
        part = text[start:end].strip()
        if part: result.append(part)
        if end == len(text): break
        start = end - 100
    return result


class KnowledgeSnapshot:
    def __init__(self, revision, values):
        self.revision, self.values = revision, tuple(values)
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE VIRTUAL TABLE passages USING fts5(identity UNINDEXED, text)')
        self.db.executemany('INSERT INTO passages VALUES (?,?)', self.values)

    def retrieve(self, text, top_k):
        tokens = list(dict.fromkeys(re.findall(r'[^\W_]{2,40}', text.casefold())))[:32]
        if not tokens: return ()
        query = ' OR '.join('"' + t + '"' for t in tokens)
        return tuple(dict(id=r[0], text=r[1]) for r in self.db.execute(
            'SELECT identity,text FROM passages WHERE passages MATCH ? ORDER BY rank,identity LIMIT ?', (query, top_k)))

    def close(self): self.db.close()


class KnowledgeStore:
    def __init__(self, state_dir): self.path = Path(state_dir) / NAME

    @contextmanager
    def database(self, *, write=False):
        safe_path(self.path)
        if not write and not self.path.exists():
            yield None
            return
        if write: self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path if write else self.path.resolve().as_uri()+'?mode=ro',
            uri=not write, isolation_level=None, timeout=.1)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=FULL')
                db.execute('CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, bytes INTEGER NOT NULL, enabled INTEGER NOT NULL)')
                db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(source UNINDEXED, identity UNINDEXED, text)')
            yield db
        finally: db.close()

    def view(self):
        with self.database() as db:
            sources = [dict(r) for r in db.execute('SELECT s.*, (SELECT count(*) FROM passages p WHERE p.source=s.id) AS chunks FROM sources s ORDER BY s.id')] if db else []
            values = [(r['identity'], r['text']) for r in db.execute('SELECT identity,text FROM passages WHERE source IN (SELECT id FROM sources WHERE enabled=1) ORDER BY identity')] if db else []
        revision = sha256(json.dumps([sources, values], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return dict(revision=revision, sources=sources, formats=['txt', 'md'], total_bytes=sum(s['bytes'] for s in sources)), values

    def snapshot(self):
        view, values = self.view()
        return KnowledgeSnapshot(view['revision'], values)

    def add(self, name, raw):
        name = display_name(name)
        if Path(name).suffix.lower() not in {'.txt', '.md'}: raise KnowledgeError("Supported formats: TXT, MD")
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_FILE: raise KnowledgeError("File exceeds 128 KiB")
        try: parts = chunks(raw.decode('utf-8-sig'))
        except UnicodeError: raise KnowledgeError("UTF-8 text required") from None
        identity = uuid4().hex
        with self.database(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                count, size = db.execute('SELECT count(*),coalesce(sum(bytes),0) FROM sources').fetchone()
                if count >= MAX_SOURCES or size + len(raw) > MAX_TOTAL or db.execute('SELECT count(*) FROM passages').fetchone()[0]+len(parts) > MAX_CHUNKS:
                    raise KnowledgeError("Knowledge capacity reached (32 sources / 2 MiB / 3000 chunks)")
                db.execute('INSERT INTO sources VALUES (?,?,?,1)', (identity, name, len(raw)))
                db.executemany('INSERT INTO passages VALUES (?,?,?)', ((identity, identity+'_'+str(i), part) for i,part in enumerate(parts)))
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
        return identity

    def update(self, identity, *, enabled=None, delete=False):
        if not re.fullmatch(r'[a-f0-9]{32}', identity): raise KnowledgeError("Invalid source")
        with self.database(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if not db.execute('SELECT 1 FROM sources WHERE id=?',(identity,)).fetchone(): raise KnowledgeError("Source unavailable")
                if delete:
                    db.execute('DELETE FROM passages WHERE source=?',(identity,)); db.execute('DELETE FROM sources WHERE id=?',(identity,))
                else:
                    if type(enabled) is not bool: raise KnowledgeError("Explicit enabled value required")
                    db.execute('UPDATE sources SET enabled=? WHERE id=?',(int(enabled),identity))
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
