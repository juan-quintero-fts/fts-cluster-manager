import datetime
import os
import re
import sqlite3

DB='/app/data/audit.db'

ANSI_ESCAPE_RE = re.compile(
    r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x1b\x07]*(?:\x07|\x1b\\))'
)
CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
SECRET_RE = re.compile(r'(?i)(password|passwd|pwd|mongosh_password|mysql_pwd)\s*(?:=|:|\s)\s*([^\s,;]+)')


def sanitize_detail(value):
    """Quita colores ANSI y controles, conservando saltos de línea y tabulaciones."""
    text = str(value or '')
    text = CONTROL_CHAR_RE.sub('', ANSI_ESCAPE_RE.sub('', text))
    return SECRET_RE.sub(lambda m: f'{m.group(1)}=[REDACTED]', text)

def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with sqlite3.connect(DB) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, host TEXT, action TEXT, ok INTEGER, detail TEXT)''')

def log(actor, host, action, ok, detail=''):
    init_db()
    clean_detail = sanitize_detail(detail)[:4000]
    with sqlite3.connect(DB) as c:
        c.execute('INSERT INTO audit(ts,actor,host,action,ok,detail) VALUES(?,?,?,?,?,?)', (datetime.datetime.now().isoformat(timespec='seconds'), actor, host, action, 1 if ok else 0, clean_detail))

def recent(limit=100):
    init_db()
    with sqlite3.connect(DB) as c:
        c.row_factory=sqlite3.Row
        rows = c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [
        {**dict(row), 'detail': sanitize_detail(row['detail'])}
        for row in rows
    ]
