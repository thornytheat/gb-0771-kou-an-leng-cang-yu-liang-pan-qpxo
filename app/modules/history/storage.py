from contextlib import closing
import json
from pathlib import Path
import sqlite3
from app.faults import DockFault
from app.seals import compact


class Receipts:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, stamp TEXT NOT NULL, raw TEXT NOT NULL, stages TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS receipt_lineage '
                       '(revision_id INTEGER PRIMARY KEY AUTOINCREMENT, parent TEXT NOT NULL, child TEXT NOT NULL, '
                       'correction_id TEXT NOT NULL, created_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS corrections '
                       '(id TEXT PRIMARY KEY, slot TEXT NOT NULL, revision TEXT NOT NULL, door TEXT NOT NULL, '
                       'status TEXT NOT NULL, applied_at TEXT NOT NULL, detail TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS correction_receipts '
                       '(correction_id TEXT NOT NULL, source_id TEXT NOT NULL, result_id TEXT NOT NULL, '
                       'sequence INTEGER NOT NULL, reused INTEGER NOT NULL, '
                       'PRIMARY KEY (correction_id, source_id))')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def read(self, key):
        with closing(self.connect()) as db:
            row = db.execute('SELECT stamp,raw,stages FROM receipts WHERE id=?', (key,)).fetchone()
        if row is None:
            raise DockFault('NO_RECEIPT', '冷库档案不存在', 404)
        return dict(zip(('manifest', 'files', 'stages'), map(json.loads, row)))

    def insert(self, key, stamp, files, stages):
        with closing(self.connect()) as db, db:
            cursor = db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)',
                                (key, compact(stamp), compact(files), compact(stages)))
            inserted = cursor.rowcount == 1
        return self.read(key), not inserted

    def list(self):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT stamp FROM receipts ORDER BY rowid DESC')]

    def lineage_heads(self):
        """Return the newest id of every correction chain plus all never-corrected receipts."""
        with closing(self.connect()) as db:
            superseded = {row[0] for row in db.execute('SELECT DISTINCT parent FROM receipt_lineage')}
            leaves = [row[0] for row in db.execute(
                'SELECT DISTINCT child FROM receipt_lineage '
                'WHERE child NOT IN (SELECT parent FROM receipt_lineage)')]
            untouched = [row[0] for row in db.execute(
                'SELECT id FROM receipts WHERE id NOT IN (SELECT parent FROM receipt_lineage) '
                'AND id NOT IN (SELECT child FROM receipt_lineage)')]
        return sorted(set(leaves + untouched)), superseded

    def chain(self, key):
        with closing(self.connect()) as db:
            root = key
            while True:
                parent = db.execute(
                    'SELECT parent FROM receipt_lineage WHERE child=? ORDER BY revision_id LIMIT 1', (root,)).fetchone()
                if parent is None:
                    break
                root = parent[0]
            chain = [root]
            current = root
            while True:
                child = db.execute(
                    'SELECT child FROM receipt_lineage WHERE parent=? ORDER BY revision_id LIMIT 1',
                    (current,)).fetchone()
                if child is None:
                    break
                current = child[0]
                chain.append(current)
            return chain

    def correction(self, correction_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT id,slot,revision,door,status,applied_at,detail FROM corrections WHERE id=?',
                             (correction_id,)).fetchone()
        if row is None:
            return None
        record = dict(zip(('id', 'slot', 'revision', 'door', 'status', 'applied_at', 'detail'), row))
        record['detail'] = json.loads(record['detail'])
        return record

    def list_corrections(self):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,slot,revision,door,status,applied_at,detail FROM corrections ORDER BY rowid DESC').fetchall()
        records = []
        for row in rows:
            record = dict(zip(('id', 'slot', 'revision', 'door', 'status', 'applied_at', 'detail'), row))
            record['detail'] = json.loads(record['detail'])
            records.append(record)
        return records

    def apply_correction(self, correction_id, change, receipts, now):
        """Persist a correction batch atomically.

        ``receipts`` is a list of (source_document, new_key, new_stamp, new_files, new_stages, reused)
        for every linked shift. Any failure rolls the whole batch back so old versions stay in force.
        Returns False when another writer already persisted this same correction.
        """
        with closing(self.connect()) as db:
            try:
                with db:
                    if db.execute('SELECT 1 FROM corrections WHERE id=?', (correction_id,)).fetchone():
                        return False
                    detail = []
                    for sequence, (document, key, stamp, files, stages, reused) in enumerate(receipts):
                        db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)',
                                   (key, compact(stamp), compact(files), compact(stages)))
                        if not reused:
                            db.execute('INSERT INTO receipt_lineage (parent,child,correction_id,created_at) VALUES (?,?,?,?)',
                                       (document['manifest']['id'], key, correction_id, now))
                        db.execute('INSERT INTO correction_receipts VALUES (?,?,?,?,?)',
                                   (correction_id, document['manifest']['id'], key, sequence, int(reused)))
                        detail.append(dict(source=document['manifest']['id'], result=key,
                                           reused=reused, window=stamp['window']))
                    db.execute('INSERT INTO corrections VALUES (?,?,?,?,?,?,?)',
                               (correction_id, change['slot'], change['revision'], change['door'],
                                'applied', now, compact(detail)))
            except sqlite3.IntegrityError:
                db.rollback()
                return False
        return True
