import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

def stamp():
    return datetime.now(timezone.utc).isoformat()

class Store:
    """SQLite 会话、消息、运行轨迹和结构化记忆；每次操作独立连接。"""
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT);
            CREATE TABLE IF NOT EXISTS memories(session_id TEXT,key TEXT,value TEXT,updated TEXT,PRIMARY KEY(session_id,key));
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session_id TEXT,status TEXT,input TEXT,result TEXT,error TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,session_id TEXT,payload TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,run_id TEXT,payload TEXT);
            """)
            # 上次进程异常终止时不会把遗留任务显示为持续运行。
            db.execute("UPDATE runs SET status='failed', error='服务重启，运行已中断，请重新提交' WHERE status='running'")
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db
    def session(self, title):
        sid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?)',(sid,title,stamp()))
        return sid
    def sessions(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM sessions ORDER BY created DESC')]
    def exists(self, sid):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM sessions WHERE id=?',(sid,)).fetchone() is not None
    def message(self,sid,role,content):
        with self.connect() as db:
            db.execute('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)',(sid,role,content))
    def history(self,sid,limit=8):
        with self.connect() as db:
            rows=db.execute('SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?',(sid,limit)).fetchall()
            return [dict(r) for r in reversed(rows)]
    def memory(self,sid):
        with self.connect() as db:
            return {r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM memories WHERE session_id=?',(sid,))}
    def remember(self,sid,key,value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO memories VALUES(?,?,?,?)',(sid,key,json.dumps(value,ensure_ascii=False),stamp()))
    def clear_memory(self,sid):
        with self.connect() as db:
            db.execute('DELETE FROM memories WHERE session_id=?',(sid,))
            db.execute('DELETE FROM messages WHERE session_id=?',(sid,))
    def create_run(self,sid,request):
        rid=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',(rid,sid,'running',json.dumps(request,ensure_ascii=False),None,None,stamp()))
        return rid
    def event(self,rid,payload):
        with self.connect() as db:
            db.execute('INSERT INTO events(run_id,payload) VALUES(?,?)',(rid,json.dumps(payload,ensure_ascii=False)))
    def finish(self,rid,result=None,error=None):
        with self.connect() as db:
            db.execute('UPDATE runs SET status=?,result=?,error=? WHERE id=?',('failed' if error else 'completed',json.dumps(result,ensure_ascii=False),error,rid))
    def run(self,rid):
        with self.connect() as db:
            r=db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not r:return None
            data=dict(r)
            for k in ['input','result']:
                data[k]=json.loads(data[k]) if data[k] else None
            data['events']=[json.loads(r['payload']) for r in db.execute('SELECT payload FROM events WHERE run_id=? ORDER BY id',(rid,))]
            return data
    def runs(self,sid):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,status,created,error FROM runs WHERE session_id=? ORDER BY created DESC',(sid,))]

    def save_plan(self,sid,payload):
        pid=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO plans VALUES(?,?,?,?)',(pid,sid,json.dumps(payload,ensure_ascii=False),'pending'))
        return pid
    def plan(self,pid):
        with self.connect() as db:
            row=db.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()
            if not row:return None
            return {**dict(row),'payload':json.loads(row['payload'])}
    def claim_plan(self,pid):
        with self.connect() as db:
            return db.execute("UPDATE plans SET status='confirmed' WHERE id=? AND status='pending'",(pid,)).rowcount==1

    def pending_plans(self,sid):
        with self.connect() as db:
            rows=db.execute("SELECT * FROM plans WHERE session_id=? AND status='pending'",(sid,)).fetchall()
            return [{'id':r['id'],**json.loads(r['payload'])} for r in rows]
