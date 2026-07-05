#!/usr/bin/env python3
"""Employee Manager — multi-instance management system.
First run: choose to CONFIGURE a server or JOIN an existing one.
"""

import json, os, sqlite3, hashlib, threading, time, webbrowser, sys, uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from functools import lru_cache

DATA_DIR = os.path.join(os.path.expanduser("~"), ".employee_manager")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DB_FILE = os.path.join(DATA_DIR, "data.db")

config = {}
if os.path.exists(CONFIG_FILE):
    try:
        config = json.loads(open(CONFIG_FILE).read())
    except: pass

MODE = config.get("mode", "setup")  # setup | server | client

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT DEFAULT '',
            role TEXT DEFAULT 'employee',
            phone TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            joined TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            descr TEXT DEFAULT '',
            assignee_id INTEGER,
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            due TEXT DEFAULT '',
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER NOT NULL,
            to_type TEXT NOT NULL,
            to_id INTEGER,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            time TEXT DEFAULT (datetime('now')),
            read INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT DEFAULT 'info',
            text TEXT NOT NULL,
            target TEXT DEFAULT 'all',
            from_name TEXT DEFAULT '',
            time TEXT DEFAULT (datetime('now')),
            read INTEGER DEFAULT 0,
            urgent INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg TEXT NOT NULL,
            time TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS allowed_users (
            username TEXT PRIMARY KEY,
            added_by INTEGER,
            added_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()

def add_log(msg):
    conn = get_db()
    conn.execute("INSERT INTO logs (msg) VALUES (?)", (msg,))
    conn.commit()
    conn.close()

def hash_pass(p):
    return hashlib.sha256(p.encode()).hexdigest()

# ---------- HTTP Server ----------

class Handler(BaseHTTPRequestHandler):

    def _send(self, data, status=200, ctype="text/html; charset=utf-8"):
        if isinstance(data, str):
            data = data.encode()
        elif isinstance(data, dict):
            data = json.dumps(data).encode()
            ctype = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html):
        self._send(html)

    def _json(self, data, status=200):
        self._send(data, status)

    def _error(self, msg, status=400):
        self._json({"ok": False, "error": msg}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode() if length else ""

    def _parse_path(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def log_message(self, fmt, *args):
        pass  # quiet

    def do_OPTIONS(self):
        self._send("", 204)

    def do_GET(self):
        path, qs = self._parse_path()
        MODE = config.get("mode", "setup")

        if MODE == "setup":
            if path == "/":
                self._html(setup_page())
            elif path == "/api/check":
                self._json({"mode": "setup"})
            else:
                self._error("not found", 404)
            return

        if path == "/":
            self._html(app_page())
        elif path == "/api/check":
            self._json({"mode": MODE, "server_name": config.get("server_name", "")})
        elif path == "/api/me":
            self._auth_req(lambda u: self._json({"ok": True, "user": u}))
        elif path == "/api/users":
            self._auth_req(lambda u: self._list_users(u))
        elif path == "/api/tasks":
            self._auth_req(lambda u: self._list_tasks(u))
        elif path == "/api/messages":
            self._auth_req(lambda u: self._list_messages(u))
        elif path == "/api/notifications":
            self._auth_req(lambda u: self._list_notifications(u))
        elif path == "/api/stats":
            self._auth_req(lambda u: self._stats(u))
        elif path == "/api/logs":
            self._auth_req(lambda u: self._get_logs(u))
        elif path == "/health":
            self._json({"ok": True, "mode": MODE})
        else:
            self._error("not found", 404)

    def do_POST(self):
        path, qs = self._parse_path()
        body = self._body()
        MODE = config.get("mode", "setup")

        if path == "/api/setup":
            self._handle_setup(body)
            return

        if MODE == "server":
            if path == "/api/register":
                self._handle_register(body)
            elif path == "/api/login":
                self._handle_login(body)
            elif path == "/api/tasks/create":
                self._auth_req(lambda u: self._create_task(u, body))
            elif path == "/api/tasks/update":
                self._auth_req(lambda u: self._update_task(u, body))
            elif path == "/api/tasks/delete":
                self._auth_req(lambda u: self._delete_task(u, body))
            elif path == "/api/messages/send":
                self._auth_req(lambda u: self._send_message(u, body))
            elif path == "/api/messages/read":
                self._auth_req(lambda u: self._read_message(u, body))
            elif path == "/api/notifications/mark":
                self._auth_req(lambda u: self._mark_notification(u, body))
            elif path == "/api/notifications/clear":
                self._auth_req(lambda u: self._clear_notifications(u, body))
            elif path == "/api/notifications/markall":
                self._auth_req(lambda u: self._mark_all_notifications(u, body))
            elif path == "/api/users/create":
                self._auth_req(lambda u: self._create_user(u, body))
            elif path == "/api/users/update":
                self._auth_req(lambda u: self._update_user(u, body))
            elif path == "/api/emergency":
                self._auth_req(lambda u: self._send_emergency(u, body))
            elif path == "/api/export":
                self._auth_req(lambda u: self._export_data(u))
            else:
                self._error("not found", 404)
        elif MODE == "client":
            # Proxy to remote server
            self._proxy_to_server(path, body)
        else:
            self._error("not found", 404)

    # ---------- Auth ----------

    def _get_user(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            conn = get_db()
            row = conn.execute("SELECT * FROM users WHERE password=? AND active=1", (hash_pass(token),)).fetchone()
            conn.close()
            if row: return dict(row)
        return None

    def _auth_req(self, cb):
        u = self._get_user()
        if not u:
            return self._json({"ok": False, "error": "unauthorized"}, 401)
        try:
            cb(u)
        except Exception as e:
            self._error(str(e))

    # ---------- Setup ----------

    def _handle_setup(self, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        mode = j.get("mode", "server")
        if mode == "server":
            admin_user = j.get("admin_user", "").strip()
            admin_pass = j.get("admin_pass", "").strip()
            admin_name = j.get("admin_name", "").strip()
            server_name = j.get("server_name", "Employee Server")
            if not admin_user or not admin_pass:
                return self._error("admin credentials required")
            init_db()
            conn = get_db()
            conn.execute("INSERT OR IGNORE INTO users (username,password,name,role) VALUES (?,?,?,?)",
                         (admin_user, hash_pass(admin_pass), admin_name or admin_user, "super_admin"))
            # Add allowed users from setup
            for u in j.get("users", []):
                uname = u.get("username", "").strip()
                role = u.get("role", "employee")
                pwd = u.get("password", "").strip()
                nm = u.get("name", "").strip()
                if uname and pwd:
                    conn.execute("INSERT OR IGNORE INTO users (username,password,name,role) VALUES (?,?,?,?)",
                                 (uname, hash_pass(pwd), nm or uname, role))
                if uname:
                    conn.execute("INSERT OR IGNORE INTO allowed_users (username) VALUES (?)", (uname,))
            conn.commit()
            conn.close()
            config.clear()
            config.update({
                "mode": "server",
                "server_name": server_name,
                "admin_user": admin_user,
                "port": int(j.get("port", 8765))
            })
            json.dump(config, open(CONFIG_FILE, "w"))
            add_log(f"Server '{server_name}' configured by {admin_name or admin_user}")
            self._json({"ok": True, "mode": "server", "port": config["port"]})
        elif mode == "client":
            remote_host = j.get("remote_host", "").strip()
            remote_port = int(j.get("remote_port", 8765))
            my_user = j.get("my_user", "").strip()
            my_pass = j.get("my_pass", "").strip()
            if not remote_host or not my_user or not my_pass:
                return self._error("host, username and password required")
            config.clear()
            config.update({
                "mode": "client",
                "remote_host": remote_host,
                "remote_port": remote_port,
                "my_user": my_user,
                "my_pass": my_pass,
                "port": int(j.get("port", 8765))
            })
            json.dump(config, open(CONFIG_FILE, "w"))
            self._json({"ok": True, "mode": "client"})
        else:
            self._error("invalid mode")

    # ---------- Client proxy ----------

    def _proxy_to_server(self, path, body):
        import urllib.request
        remote = f"http://{config['remote_host']}:{config['remote_port']}"
        try:
            req = urllib.request.Request(f"{remote}{path}",
                data=body.encode() if body else None,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {config.get('my_pass','')}"
                })
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read().decode()
            self._send(data, resp.status)
        except urllib.request.URLError as e:
            self._error(f"server unreachable: {e.reason}", 502)
        except Exception as e:
            self._error(str(e), 502)

    # ---------- Handlers for server mode ----------

    def _handle_register(self, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        uname = j.get("username", "").strip()
        pwd = j.get("password", "").strip()
        name = j.get("name", "").strip()
        if not uname or not pwd:
            return self._error("username and password required")
        conn = get_db()
        allowed = conn.execute("SELECT username FROM allowed_users WHERE username=?", (uname,)).fetchone()
        if not allowed:
            conn.close()
            return self._error("not in allowed users list", 403)
        existing = conn.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
        if existing:
            conn.close()
            return self._error("username already taken", 409)
        conn.execute("INSERT INTO users (username,password,name,role) VALUES (?,?,?,?)",
                     (uname, hash_pass(pwd), name or uname, "employee"))
        conn.commit()
        conn.close()
        add_log(f"User {uname} registered")
        self._json({"ok": True})

    def _handle_login(self, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        uname = j.get("username", "").strip()
        pwd = j.get("password", "").strip()
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username=? AND password=? AND active=1",
                          (uname, hash_pass(pwd))).fetchone()
        conn.close()
        if row:
            self._json({"ok": True, "user": dict(row)})
        else:
            self._json({"ok": False, "error": "invalid credentials"}, 401)

    def _list_users(self, u):
        conn = get_db()
        rows = conn.execute("SELECT id,username,name,email,role,phone,active,joined FROM users ORDER BY id").fetchall()
        conn.close()
        self._json([dict(r) for r in rows])

    def _create_user(self, u, body):
        if u["role"] not in ("super_admin", "admin"):
            return self._error("forbidden", 403)
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        uname = j.get("username", "").strip()
        pwd = j.get("password", "").strip()
        name = j.get("name", "").strip()
        role = j.get("role", "employee")
        if not uname or not pwd:
            return self._error("username and password required")
        conn = get_db()
        try:
            conn.execute("INSERT INTO users (username,password,name,role) VALUES (?,?,?,?)",
                        (uname, hash_pass(pwd), name or uname, role))
            conn.execute("INSERT OR IGNORE INTO allowed_users (username) VALUES (?)", (uname,))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return self._error("username exists", 409)
        conn.close()
        add_log(f"{u['name']} created user {uname}")
        self._json({"ok": True})

    def _update_user(self, u, body):
        if u["role"] not in ("super_admin", "admin"):
            return self._error("forbidden", 403)
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        uid = j.get("id")
        conn = get_db()
        fields = []
        vals = []
        for k in ("name","email","role","phone","active"):
            if k in j:
                fields.append(f"{k}=?")
                vals.append(j[k])
        if j.get("password",""):
            fields.append("password=?")
            vals.append(hash_pass(j["password"]))
        if fields:
            vals.append(uid)
            conn.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?", vals)
            conn.commit()
        conn.close()
        add_log(f"{u['name']} updated user #{uid}")
        self._json({"ok": True})

    def _list_tasks(self, u):
        conn = get_db()
        if u["role"] in ("super_admin", "admin"):
            rows = conn.execute("SELECT t.*,u.name as assignee_name FROM tasks t LEFT JOIN users u ON t.assignee_id=u.id ORDER BY t.id DESC").fetchall()
        else:
            rows = conn.execute("SELECT t.*,u.name as assignee_name FROM tasks t LEFT JOIN users u ON t.assignee_id=u.id WHERE t.assignee_id=? OR t.created_by=? ORDER BY t.id DESC",
                              (u["id"], u["id"])).fetchall()
        conn.close()
        self._json([dict(r) for r in rows])

    def _create_task(self, u, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        conn = get_db()
        conn.execute("INSERT INTO tasks (title,descr,assignee_id,priority,due,created_by) VALUES (?,?,?,?,?,?)",
                    (j["title"], j.get("descr",""), j.get("assignee_id"), j.get("priority","medium"), j.get("due",""), u["id"]))
        conn.commit()
        conn.close()
        add_log(f"{u['name']} created task: {j['title']}")
        self._json({"ok": True})

    def _update_task(self, u, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        tid = j.get("id")
        conn = get_db()
        fields = []
        vals = []
        for k in ("title","descr","assignee_id","priority","status","due"):
            if k in j:
                fields.append(f"{k}=?")
                vals.append(j[k])
        if fields:
            vals.append(tid)
            conn.execute(f"UPDATE tasks SET {','.join(fields)} WHERE id=?", vals)
            conn.commit()
        conn.close()
        self._json({"ok": True})

    def _delete_task(self, u, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        if u["role"] not in ("super_admin", "admin"):
            return self._error("forbidden", 403)
        conn = get_db()
        conn.execute("DELETE FROM tasks WHERE id=?", (j.get("id"),))
        conn.commit()
        conn.close()
        self._json({"ok": True})

    def _list_messages(self, u):
        conn = get_db()
        rows = conn.execute("""
            SELECT m.*, u.name as from_name FROM messages m
            LEFT JOIN users u ON m.from_id=u.id
            WHERE m.to_type='all' OR m.to_id=? OR m.from_id=?
            ORDER BY m.time DESC
        """, (u["id"], u["id"])).fetchall()
        conn.close()
        self._json([dict(r) for r in rows])

    def _send_message(self, u, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        conn = get_db()
        conn.execute("INSERT INTO messages (from_id,to_type,to_id,subject,body) VALUES (?,?,?,?,?)",
                    (u["id"], j.get("to_type","user"), j.get("to_id"), j.get("subject",""), j.get("body","")))
        conn.commit()
        conn.close()
        add_log(f"{u['name']} sent message: {j.get('subject','')}")
        self._json({"ok": True})

    def _read_message(self, u, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        conn = get_db()
        conn.execute("UPDATE messages SET read=1 WHERE id=? AND (to_id=? OR to_type='all')",
                    (j.get("id"), u["id"]))
        conn.commit()
        conn.close()
        self._json({"ok": True})

    def _list_notifications(self, u):
        conn = get_db()
        rows = conn.execute("""
            SELECT * FROM notifications
            WHERE target='all' OR target=?
            ORDER BY time DESC LIMIT 50
        """, (u["username"],)).fetchall()
        conn.close()
        self._json([dict(r) for r in rows])

    def _mark_notification(self, u, body):
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        conn = get_db()
        conn.execute("UPDATE notifications SET read=1 WHERE id=?", (j.get("id"),))
        conn.commit()
        conn.close()
        self._json({"ok": True})

    def _clear_notifications(self, u, body):
        conn = get_db()
        conn.execute("DELETE FROM notifications WHERE target='all' OR target=?", (u["username"],))
        conn.commit()
        conn.close()
        self._json({"ok": True})

    def _mark_all_notifications(self, u, body):
        conn = get_db()
        conn.execute("UPDATE notifications SET read=1 WHERE target='all' OR target=?", (u["username"],))
        conn.commit()
        conn.close()
        self._json({"ok": True})

    def _send_emergency(self, u, body):
        if u["role"] not in ("super_admin", "admin"):
            return self._error("forbidden", 403)
        try:
            j = json.loads(body)
        except:
            return self._error("invalid json")
        conn = get_db()
        conn.execute("INSERT INTO notifications (type,text,target,from_name,urgent) VALUES ('emergency',?,?,?,1)",
                    (j.get("text",""), j.get("target","all"), u["name"]))
        conn.commit()
        conn.close()
        add_log(f"⚠ EMERGENCY from {u['name']}: {j.get('text','')}")
        self._json({"ok": True})

    def _stats(self, u):
        conn = get_db()
        users = conn.execute("SELECT COUNT(*) as c FROM users WHERE active=1").fetchone()["c"]
        tasks = conn.execute("SELECT COUNT(*) as c FROM tasks").fetchone()["c"]
        msgs = conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
        notifs = conn.execute("SELECT COUNT(*) as c FROM notifications WHERE read=0 AND (target='all' OR target=?)",
                             (u["username"],)).fetchone()["c"]
        conn.close()
        self._json({"users": users, "tasks": tasks, "messages": msgs, "unread_notifs": notifs})

    def _get_logs(self, u):
        if u["role"] not in ("super_admin", "admin"):
            return self._error("forbidden", 403)
        conn = get_db()
        rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 50").fetchall()
        conn.close()
        self._json([dict(r) for r in rows])

    def _export_data(self, u):
        if u["role"] not in ("super_admin", "admin"):
            return self._error("forbidden", 403)
        conn = get_db()
        data = {
            "users": [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()],
            "tasks": [dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()],
            "messages": [dict(r) for r in conn.execute("SELECT * FROM messages").fetchall()],
            "notifications": [dict(r) for r in conn.execute("SELECT * FROM notifications").fetchall()],
        }
        conn.close()
        self._json(data)

# ---------- HTML Pages ----------

def setup_page():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Employee Manager · Setup</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#a0a5b1;color:#1a1a1a;font-family:'IBM Plex Mono',monospace;font-size:13px;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.container{max-width:520px;width:100%}
.card{background:#b9bfd0;border:1px solid rgba(0,0,0,.08);padding:32px}
.card h1{font-size:11px;letter-spacing:6px;text-transform:uppercase;font-weight:400;color:rgba(0,0,0,.5);margin-bottom:4px}
.card .sub{font-size:8px;letter-spacing:3px;color:rgba(0,0,0,.25);text-transform:uppercase;margin-bottom:24px}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:rgba(0,0,0,.5);margin-bottom:4px}
.form-group input,.form-group select{width:100%;padding:9px 12px;background:#c5cbdb;border:1px solid rgba(0,0,0,.08);color:#1a1a1a;font-size:12px;font-family:'IBM Plex Mono',monospace}
.form-group input:focus{border-color:rgba(0,0,0,.2)}
.btn{width:100%;padding:10px;background:#1a1a1a;color:#a0a5b1;border:none;cursor:pointer;font-size:10px;letter-spacing:3px;text-transform:uppercase;font-family:'IBM Plex Mono',monospace;transition:.2s}
.btn:hover{opacity:.85}
.btn-outline{background:transparent;color:#1a1a1a;border:1px solid rgba(0,0,0,.15)}
.tabs{display:flex;gap:4px;margin-bottom:20px}
.tabs button{flex:1;padding:9px;background:transparent;border:1px solid rgba(0,0,0,.08);font-size:9px;letter-spacing:2px;text-transform:uppercase;cursor:pointer;color:rgba(0,0,0,.4);font-family:'IBM Plex Mono',monospace;transition:.15s}
.tabs button.active{background:#c5cbdb;color:#1a1a1a;border-color:rgba(0,0,0,.12)}
.info{font-size:9px;color:rgba(0,0,0,.3);margin-top:12px;letter-spacing:1px;text-align:center}
.hidden{display:none}
.user-row{display:flex;gap:6px;margin-bottom:6px;align-items:center}
.user-row input{flex:1;padding:6px 8px;background:#c5cbdb;border:1px solid rgba(0,0,0,.08);font-size:11px;font-family:'IBM Plex Mono',monospace}
.user-row select{padding:6px;background:#c5cbdb;border:1px solid rgba(0,0,0,.08);font-size:10px;font-family:'IBM Plex Mono',monospace}
.user-row .rm{cursor:pointer;color:rgba(0,0,0,.3);font-size:14px;padding:0 4px}
.add-row{font-size:9px;color:rgba(0,0,0,.3);cursor:pointer;letter-spacing:2px;text-transform:uppercase;margin-top:6px;display:inline-block}
.add-row:hover{color:rgba(0,0,0,.6)}
.error{color:#b84a4a;font-size:10px;margin-top:8px;display:none;text-align:center}
</style>
</head>
<body>
<div class="container">
<div class="card">
  <h1>⬡ EMPLOYEE MANAGER</h1>
  <div class="sub">first-time setup</div>
  <div class="tabs">
    <button class="active" onclick="showTab('server')" id="tab-server">configure server</button>
    <button onclick="showTab('client')" id="tab-client">join server</button>
  </div>
  <div id="page-server">
    <div class="form-group"><label>server name</label><input id="srv-name" value="My Server"></div>
    <div class="form-group"><label>your name</label><input id="srv-admin-name" placeholder="Admin"></div>
    <div class="form-group"><label>admin username</label><input id="srv-admin-user" value="admin"></div>
    <div class="form-group"><label>admin password</label><input id="srv-admin-pass" type="password"></div>
    <div class="form-group"><label>port</label><input id="srv-port" value="8765"></div>
    <div class="form-group"><label>initial users</label>
      <div id="user-list">
        <div class="user-row">
          <input placeholder="username" class="u-user">
          <input placeholder="name" class="u-name">
          <select class="u-role"><option value="employee">employee</option><option value="manager">manager</option><option value="admin">admin</option><option value="super_admin">super_admin</option></select>
          <input type="password" placeholder="password" class="u-pass" style="max-width:100px">
          <span class="rm" onclick="this.parentElement.remove()">×</span>
        </div>
      </div>
      <span class="add-row" onclick="addUserRow()">+ add user</span>
    </div>
    <div class="error" id="err-server"></div>
    <button class="btn" onclick="setupServer()" style="margin-top:12px">configure server</button>
  </div>
  <div id="page-client" class="hidden">
    <div class="form-group"><label>server host (IP or domain)</label><input id="cli-host" placeholder="192.168.1.100"></div>
    <div class="form-group"><label>server port</label><input id="cli-port" value="8765"></div>
    <div class="form-group"><label>your username</label><input id="cli-user" placeholder="username"></div>
    <div class="form-group"><label>your password</label><input id="cli-pass" type="password"></div>
    <div class="error" id="err-client"></div>
    <button class="btn" onclick="setupClient()" style="margin-top:12px">join server</button>
  </div>
</div>
<div class="info">Each employee runs their own instance. One configures the server, others join.</div>
</div>
<script>
function showTab(t){
  document.getElementById('tab-server').classList.toggle('active',t==='server')
  document.getElementById('tab-client').classList.toggle('active',t==='client')
  document.getElementById('page-server').classList.toggle('hidden',t!=='server')
  document.getElementById('page-client').classList.toggle('hidden',t!=='client')
}
function addUserRow(){
  const d=document.getElementById('user-list')
  const r=document.createElement('div');r.className='user-row'
  r.innerHTML='<input placeholder="username" class="u-user"><input placeholder="name" class="u-name"><select class="u-role"><option value="employee">employee</option><option value="manager">manager</option><option value="admin">admin</option><option value="super_admin">super_admin</option></select><input type="password" placeholder="password" class="u-pass" style="max-width:100px"><span class="rm" onclick="this.parentElement.remove()">×</span>'
  d.appendChild(r)
}
function collectUsers(){
  const rows=document.querySelectorAll('#user-list .user-row'),users=[]
  rows.forEach(r=>{
    const u=r.querySelector('.u-user')?.value.trim()
    const n=r.querySelector('.u-name')?.value.trim()
    const p=r.querySelector('.u-pass')?.value
    const role=r.querySelector('.u-role')?.value
    if(u&&p) users.push({username:u,name:n||u,password:p,role})
  })
  return users
}
async function setupServer(){
  const body=JSON.stringify({
    mode:'server',
    server_name:document.getElementById('srv-name').value.trim()||'My Server',
    admin_user:document.getElementById('srv-admin-user').value.trim(),
    admin_pass:document.getElementById('srv-admin-pass').value,
    admin_name:document.getElementById('srv-admin-name').value.trim(),
    port:parseInt(document.getElementById('srv-port').value)||8765,
    users:collectUsers()
  })
  const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},body})
  const j=await r.json()
  if(j.ok){window.location.reload()}
  else{document.getElementById('err-server').textContent=j.error;document.getElementById('err-server').style.display='block'}
}
async function setupClient(){
  const body=JSON.stringify({
    mode:'client',
    remote_host:document.getElementById('cli-host').value.trim(),
    remote_port:parseInt(document.getElementById('cli-port').value)||8765,
    my_user:document.getElementById('cli-user').value.trim(),
    my_pass:document.getElementById('cli-pass').value
  })
  const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},body})
  const j=await r.json()
  if(j.ok){window.location.reload()}
  else{document.getElementById('err-client').textContent=j.error;document.getElementById('err-client').style.display='block'}
}
</script>
</body>
</html>"""

def app_page():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Employee Manager</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#a0a5b1;--surface:#b9bfd0;--surface2:#c5cbdb;--surface3:#d1d6e3;--border:rgba(0,0,0,.08);--text:#1a1a1a;--text2:rgba(0,0,0,.5);--text3:rgba(0,0,0,.25);--accent:#4a6a8a;--red:#b84a4a;--green:#4a8a6a;--amber:#8a7a4a;--font:'IBM Plex Mono',monospace}
html,body{width:100%;height:100%;background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;overflow:hidden}
a{color:var(--text);text-decoration:none}
input,textarea,select,button{font-family:var(--font);font-size:12px;outline:none}
#login{position:fixed;top:0;left:0;width:100%;height:100%;display:flex;justify-content:center;align-items:center;background:var(--bg);z-index:100;transition:opacity .4s}
#login.h{opacity:0;pointer-events:none}
#login-box{background:var(--surface2);padding:40px;border:1px solid var(--border);text-align:center;max-width:340px;width:90%}
#login-box h1{font-size:11px;letter-spacing:6px;text-transform:uppercase;font-weight:400;color:var(--text2);margin-bottom:4px}
#login-box .sub{font-size:8px;letter-spacing:3px;color:var(--text3);text-transform:uppercase;margin-bottom:24px}
#login-box input{display:block;width:100%;padding:10px 12px;background:var(--surface3);border:1px solid var(--border);color:var(--text);margin-bottom:8px;font-size:12px}
#login-box input:focus{border-color:rgba(0,0,0,.2)}
#login-box button{width:100%;padding:10px;background:var(--text);color:var(--bg);border:none;cursor:pointer;letter-spacing:3px;text-transform:uppercase;font-size:10px;transition:.2s}
#login-box button:hover{opacity:.85}
#login-box .error{color:var(--red);font-size:10px;margin-top:8px;display:none}
#app{display:none;width:100%;height:100%}
#app.s{display:flex}
#sidebar{width:200px;min-width:200px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
#sidebar .brand{padding:14px 16px;border-bottom:1px solid var(--border);font-size:8px;letter-spacing:4px;text-transform:uppercase;color:var(--text2)}
#sidebar .brand span{color:var(--text)}
#sidebar .user-info{padding:10px 16px;border-bottom:1px solid var(--border);font-size:10px}
#sidebar .user-info .name{font-weight:500}
#sidebar .user-info .role{font-size:8px;letter-spacing:2px;text-transform:uppercase;color:var(--text2);margin-top:2px}
#sidebar nav{padding:6px 0;flex:1;overflow-y:auto}
#sidebar nav a{display:block;padding:8px 16px;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--text2);transition:.15s;cursor:pointer;border-left:2px solid transparent}
#sidebar nav a:hover{background:rgba(0,0,0,.03);color:var(--text)}
#sidebar nav a.active{background:rgba(0,0,0,.04);color:var(--text);border-left-color:var(--text)}
#sidebar nav a .badge{float:right;background:var(--red);color:#fff;font-size:6px;padding:1px 5px;border-radius:2px;display:none}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#topbar{display:flex;align-items:center;justify-content:space-between;padding:10px 18px;border-bottom:1px solid var(--border);background:var(--surface);min-height:40px}
#topbar .page-title{font-size:9px;letter-spacing:4px;text-transform:uppercase;font-weight:500}
#topbar .actions{display:flex;gap:4px}
#topbar .actions button{background:var(--surface3);border:1px solid var(--border);padding:4px 10px;cursor:pointer;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:var(--text);transition:.15s;font-family:var(--font)}
#topbar .actions button:hover{background:var(--text);color:var(--bg)}
#topbar .actions .emergency{background:var(--red);color:#fff;border-color:var(--red)}
#content{flex:1;overflow-y:auto;padding:16px}
#emergency-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:var(--red);z-index:200;justify-content:center;align-items:center;text-align:center;flex-direction:column;color:#fff;cursor:pointer}
#emergency-overlay.s{display:flex}
#emergency-overlay h1{font-size:clamp(32px,8vw,80px);letter-spacing:12px;text-transform:uppercase;font-weight:600}
#emergency-overlay .msg{font-size:clamp(14px,3vw,32px);letter-spacing:4px;margin-top:16px;opacity:.8}
#emergency-overlay .from{font-size:12px;letter-spacing:3px;margin-top:24px;opacity:.4;text-transform:uppercase}
#emergency-overlay .dismiss{position:fixed;bottom:40px;font-size:10px;letter-spacing:4px;text-transform:uppercase;opacity:.3;transition:.3s}
.card{background:var(--surface2);border:1px solid var(--border);padding:14px;margin-bottom:10px}
.card-title{font-size:8px;letter-spacing:3px;text-transform:uppercase;color:var(--text2);margin-bottom:6px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:16px}
.stat{background:var(--surface2);border:1px solid var(--border);padding:14px;text-align:center}
.stat .num{font-size:24px;font-weight:500;letter-spacing:2px}
.stat .label{font-size:7px;letter-spacing:3px;text-transform:uppercase;color:var(--text2);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:11px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--border)}
th{font-size:7px;letter-spacing:3px;text-transform:uppercase;color:var(--text2);font-weight:400}
tr:hover{background:rgba(0,0,0,.02)}
.form-group{margin-bottom:10px}
.form-group label{display:block;font-size:7px;letter-spacing:2px;text-transform:uppercase;color:var(--text2);margin-bottom:3px}
.form-group input,.form-group textarea,.form-group select{width:100%;padding:7px 9px;background:var(--surface3);border:1px solid var(--border);color:var(--text);font-size:11px;font-family:var(--font)}
.form-group textarea{resize:vertical;min-height:50px}
.btn{display:inline-block;padding:5px 12px;background:var(--text);color:var(--bg);border:none;cursor:pointer;font-size:8px;letter-spacing:2px;text-transform:uppercase;transition:.15s;font-family:var(--font)}
.btn:hover{opacity:.85}
.btn-sm{padding:3px 8px;font-size:7px}
.btn-outline{background:transparent;color:var(--text);border:1px solid var(--border)}
.btn-outline:hover{background:var(--text);color:var(--bg)}
.btn-danger{background:var(--red);color:#fff}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.3);z-index:50;justify-content:center;align-items:center}
.modal.s{display:flex}
.modal-box{background:var(--surface2);border:1px solid var(--border);padding:20px;max-width:480px;width:90%;max-height:80vh;overflow-y:auto}
.modal-box h2{font-size:9px;letter-spacing:4px;text-transform:uppercase;font-weight:500;margin-bottom:12px}
.notif-item{padding:8px 10px;border-bottom:1px solid var(--border);font-size:10px;cursor:pointer}
.notif-item:hover{background:rgba(0,0,0,.02)}
.notif-item .t{font-size:8px;letter-spacing:2px;text-transform:uppercase;color:var(--text2)}
.notif-item .d{font-size:10px;margin:2px 0}
.notif-item .time{font-size:7px;color:var(--text3)}
.notif-item.urgent{border-left:2px solid var(--red)}
.msg-item{padding:10px;border-bottom:1px solid var(--border);font-size:10px;cursor:pointer}
.msg-item:hover{background:rgba(0,0,0,.02)}
.msg-item .from{font-weight:500;font-size:9px}
.msg-item .subj{font-size:10px;margin:2px 0}
.msg-item .preview{font-size:8px;color:var(--text2)}
.msg-item .time{font-size:7px;color:var(--text3);float:right}
.tabs{display:flex;gap:2px;margin-bottom:12px}
.tabs button{background:var(--surface3);border:1px solid var(--border);border-bottom:none;padding:6px 14px;font-size:8px;letter-spacing:2px;text-transform:uppercase;cursor:pointer;color:var(--text2);font-family:var(--font)}
.tabs button.active{background:var(--surface2);color:var(--text);border-bottom:1px solid var(--surface2)}
.tip{font-size:7px;color:var(--text3);padding:10px 0;text-align:center;letter-spacing:1px}
::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-thumb{background:rgba(0,0,0,.1)}
@media(max-width:640px){#sidebar{display:none}#sidebar.s{display:flex;position:fixed;top:0;left:0;height:100%;z-index:60}.grid-2{grid-template-columns:1fr}}
</style>
</head>
<body>
<div id="login">
  <div id="login-box">
    <h1>⬡ EMPLOYEE</h1>
    <div class="sub">manager</div>
    <input type="text" id="login-user" placeholder="username" autocomplete="off">
    <input type="password" id="login-pass" placeholder="password">
    <button id="login-btn">enter</button>
    <div class="error" id="login-error">invalid credentials</div>
  </div>
</div>
<div id="emergency-overlay" onclick="dismissEmergency()">
  <h1 id="emergency-title">⚠</h1>
  <div class="msg" id="emergency-msg"></div>
  <div class="from" id="emergency-from"></div>
  <div class="dismiss">click anywhere to dismiss</div>
</div>
<div id="app">
  <div id="sidebar">
    <div class="brand"><span>⬡</span> EMPLOYEE MANAGER</div>
    <div class="user-info"><div class="name" id="s-user"></div><div class="role" id="s-role"></div></div>
    <nav id="nav">
      <a class="active" data-page="dashboard">dashboard</a>
      <a data-page="tasks">tasks <span class="badge" id="task-badge"></span></a>
      <a data-page="messages">messages</a>
      <a data-page="notifications">notifications</a>
      <a data-page="employees">employees</a>
      <a data-page="admin" id="nav-admin" style="display:none">admin</a>
      <a data-page="logout" style="margin-top:auto;color:var(--text3)">logout</a>
    </nav>
  </div>
  <div id="main">
    <div id="topbar">
      <div class="page-title" id="page-title">dashboard</div>
      <div class="actions">
        <button onclick="showNewTask()" id="btn-new-task" style="display:none">+ task</button>
        <button onclick="showNewMsg()" id="btn-new-msg" style="display:none">+ message</button>
        <button onclick="showEmergency()" id="btn-emergency" class="emergency" style="display:none">⚠ emergency</button>
      </div>
    </div>
    <div id="content"></div>
  </div>
</div>
<div class="modal" id="modal"><div class="modal-box" id="modal-content"></div></div>
<script>
const MODE='"""+MODE+"""',SERVER_NAME='"""+config.get("server_name","Employee Manager")+"""'
let session=null,currentPage='dashboard'

function api(path,body){
  const opts={headers:{'Content-Type':'application/json'}}
  if(session) opts.headers['Authorization']='Bearer '+session.password
  if(body) opts.method='POST',opts.body=JSON.stringify(body)
  return fetch(path,opts).then(r=>r.json())
}

async function login(username,password){
  const j=await api('/api/login',{username,password})
  if(j.ok){
    session=j.user
    renderApp()
    return true
  }
  return false
}

function logout(){
  session=null
  document.getElementById('login').classList.remove('h')
  document.getElementById('app').classList.remove('s')
}

function renderApp(){
  document.getElementById('login').classList.add('h')
  document.getElementById('app').classList.add('s')
  document.getElementById('s-user').textContent=session.name
  const rn={super_admin:'super admin',admin:'admin',manager:'manager',employee:'employee'}
  document.getElementById('s-role').textContent=rn[session.role]||session.role
  const isSuper=session.role==='super_admin',isAdmin=isSuper||session.role==='admin'
  document.getElementById('nav-admin').style.display=isSuper?'block':'none'
  document.getElementById('btn-new-task').style.display=(isAdmin||session.role==='manager')?'inline-block':'none'
  document.getElementById('btn-new-msg').style.display='inline-block'
  document.getElementById('btn-emergency').style.display=isAdmin?'inline-block':'none'
  showPage('dashboard')
}

document.getElementById('nav').addEventListener('click',e=>{
  const a=e.target.closest('a')
  if(!a) return
  const page=a.dataset.page
  if(page==='logout'){logout();return}
  document.querySelectorAll('#nav a').forEach(x=>x.classList.remove('active'))
  a.classList.add('active')
  showPage(page)
})

function showPage(page){
  currentPage=page
  const titles={dashboard:'dashboard',tasks:'tasks',messages:'messages',notifications:'notifications',employees:'employees',admin:'admin'}
  document.getElementById('page-title').textContent=titles[page]||page
  const el=document.getElementById('content');el.innerHTML=''
  if(pages[page]) pages[page](el)
}

const pages={}

pages.dashboard=async(el)=>{
  const st=await api('/api/stats')
  el.innerHTML='<div class="stats"><div class="stat"><div class="num">'+(st.tasks||0)+'</div><div class="label">tasks</div></div><div class="stat"><div class="num">'+(st.unread_notifs||0)+'</div><div class="label">notifications</div></div><div class="stat"><div class="num">'+(st.users||0)+'</div><div class="label">employees</div></div><div class="stat"><div class="num">'+(st.messages||0)+'</div><div class="label">messages</div></div></div><div class="grid-2"><div class="card"><div class="card-title">notifications</div><div class="notif-list" id="dash-n"></div></div><div class="card"><div class="card-title">quick actions</div><div style="display:flex;gap:6px;flex-wrap:wrap;padding:4px 0"><button class="btn btn-sm" onclick="showNewTask()">+ task</button><button class="btn btn-sm btn-outline" onclick="showNewMsg()">+ message</button>'+(session.role==='super_admin'||session.role==='admin'?'<button class="btn btn-sm btn-danger" onclick="showEmergency()">⚠ emergency</button>':'')+'</div></div></div>'
  const n=await api('/api/notifications')
  const nl=document.getElementById('dash-n')
  if(n.length===0)nl.innerHTML='<div style="padding:12px;text-align:center;color:var(--text3);font-size:9px">none</div>'
  else nl.innerHTML=n.slice(0,5).map(x=>'<div class="notif-item'+(x.urgent?' urgent':'')+'" onclick="markNotif('+x.id+')"><div class="t">'+(x.type||'info')+(x.urgent?' ⚠':'')+(!x.read?' ●':'')+'</div><div class="d">'+x.text+'</div><div class="time">'+x.time.slice(0,16)+'</div></div>').join('')
}

pages.tasks=async(el)=>{
  const isAdmin=session.role==='super_admin'||session.role==='admin'
  const tasks=await api('/api/tasks')
  el.innerHTML='<div style="margin-bottom:10px"><select id="tf" style="background:var(--surface3);border:1px solid var(--border);padding:5px 8px;color:var(--text);font-size:9px;font-family:var(--font)" onchange="showPage(\'tasks\')"><option value="all">all</option><option value="pending">pending</option><option value="in_progress">in progress</option><option value="done">done</option><option value="mine">mine</option></select></div><table><tr><th>title</th><th>assignee</th><th>priority</th><th>status</th><th>due</th><th></th></tr></table><div id="tl"></div>'
  const f=document.getElementById('tf')?.value||'all'
  let list=[...tasks]
  if(f==='pending')list=list.filter(t=>t.status==='pending')
  else if(f==='in_progress')list=list.filter(t=>t.status==='in_progress')
  else if(f==='done')list=list.filter(t=>t.status==='done')
  else if(f==='mine')list=list.filter(t=>t.assignee_id===session.id)
  const html=list.map(t=>'<tr><td>'+t.title+'</td><td style="font-size:9px;color:var(--text2)">'+(t.assignee_name||'—')+'</td><td style="font-size:8px;color:'+(t.priority==='high'?'var(--red)':'var(--text2)')+'">'+t.priority+'</td><td><span style="font-size:8px;color:'+(t.status==='done'?'var(--green)':t.status==='in_progress'?'var(--accent)':'var(--amber)')+'">'+(t.status||'pending')+'</span></td><td style="font-size:9px;color:var(--text2)">'+(t.due||'—')+'</td><td><button class="btn btn-sm btn-outline" onclick="editTask('+t.id+')">edit</button>'+(isAdmin?' <button class="btn btn-sm btn-danger" onclick="delTask('+t.id+')">del</button>':'')+'</td></tr>').join('')
  document.querySelector('#tasks table')?document.querySelector('#tasks table').innerHTML='<tr><th>title</th><th>assignee</th><th>priority</th><th>status</th><th>due</th><th></th></tr>'+html:(document.getElementById('tl').innerHTML=html||'<div style="padding:30px;text-align:center;color:var(--text3);font-size:9px">no tasks</div>')
}

pages.messages=async(el)=>{
  const msgs=await api('/api/messages')
  el.innerHTML='<div style="margin-bottom:10px;display:flex;gap:6px"><select id="mf" style="background:var(--surface3);border:1px solid var(--border);padding:5px 8px;color:var(--text);font-size:9px;font-family:var(--font)" onchange="showPage(\'messages\')"><option value="all">all</option><option value="inbox">inbox</option><option value="sent">sent</option><option value="unread">unread</option></select><button class="btn btn-sm" onclick="showNewMsg()">+ compose</button></div><div class="msg-list">'+(msgs.length?msgs.map(m=>'<div class="msg-item" onclick="viewMsg('+m.id+')"><span class="time">'+(m.time||'').slice(0,16)+'</span><div class="from">'+(m.from_name||'system')+(m.to_type!=='all'&&m.to_id===session.id&&!m.read?' ●':'')+'</div><div class="subj">'+m.subject+'</div><div class="preview">'+(m.body||'').slice(0,60)+'</div></div>').join(''):'<div style="padding:20px;text-align:center;color:var(--text3);font-size:9px">no messages</div>')+'</div>'
}

pages.notifications=async(el)=>{
  const notifs=await api('/api/notifications')
  el.innerHTML='<div style="margin-bottom:10px;display:flex;gap:6px"><button class="btn btn-sm btn-outline" onclick="api(\'/api/notifications/markall\',{}).then(()=>showPage(\'notifications\'))">mark all read</button><button class="btn btn-sm btn-outline" onclick="api(\'/api/notifications/clear\',{}).then(()=>showPage(\'notifications\'))">clear</button></div><div class="notif-list">'+(notifs.length?notifs.map(x=>'<div class="notif-item'+(x.urgent?' urgent':'')+'" onclick="markNotif('+x.id+')"><div class="t">'+(x.type||'info')+(x.urgent?' ⚠':'')+(!x.read?' ●':'')+'</div><div class="d">'+x.text+'</div><div class="time">'+x.time.slice(0,16)+(x.from_name?' · '+x.from_name:'')+'</div></div>').join(''):'<div style="padding:20px;text-align:center;color:var(--text3);font-size:9px">no notifications</div>')+'</div>'
}

pages.employees=async(el)=>{
  const isAdmin=session.role==='super_admin'||session.role==='admin'
  const emps=await api('/api/users')
  el.innerHTML='<div style="margin-bottom:10px;display:flex;gap:6px">'+(isAdmin?'<button class="btn btn-sm" onclick="showNewUser()">+ employee</button>':'')+'</div><table><tr><th>name</th><th>username</th><th>role</th><th>status</th>'+(isAdmin?'<th></th>':'')+'</tr>'+(emps||[]).map(e=>'<tr><td>'+e.name+'</td><td style="font-size:9px;color:var(--text2)">'+e.username+'</td><td style="font-size:8px;text-transform:uppercase">'+(e.role||'employee')+'</td><td><span style="font-size:7px;color:'+(e.active?'var(--green)':'var(--red)')+'">'+(e.active?'active':'inactive')+'</span></td>'+(isAdmin?'<td><button class="btn btn-sm btn-outline" onclick="editUser('+e.id+')">edit</button></td>':'')+'</tr>').join('')+'</table>'
}

pages.admin=async(el)=>{
  if(session.role!=='super_admin'){el.innerHTML='<div style="padding:30px;text-align:center;color:var(--text3)">access denied</div>';return}
  const st=await api('/api/stats'),logs=await api('/api/logs')
  el.innerHTML='<div class="stats"><div class="stat"><div class="num">'+(st.users||0)+'</div><div class="label">users</div></div><div class="stat"><div class="num">'+(st.tasks||0)+'</div><div class="label">tasks</div></div><div class="stat"><div class="num">'+(st.messages||0)+'</div><div class="label">messages</div></div><div class="stat"><div class="num">'+(st.unread_notifs||0)+'</div><div class="label">unread</div></div></div><div class="card"><div class="card-title">server</div><div style="font-size:9px;color:var(--text2);margin-bottom:6px">'+SERVER_NAME+' · '+MODE+' mode</div><div style="display:flex;gap:6px"><button class="btn btn-sm btn-outline" onclick="api(\'/api/export\').then(d=>{const a=document.createElement(\'a\');a.href=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:\'application/json\'}));a.download=\'employee-manager-export.json\';a.click()})">export JSON</button></div></div><div class="card"><div class="card-title">logs</div><div style="font-size:8px;color:var(--text2);line-height:1.8">'+((logs||[]).map(l=>'<div>'+l.msg+'</div>').join('')||'<div style="color:var(--text3)">no logs</div>')+'</div></div>'
}

function markNotif(id){api('/api/notifications/mark',{id}).then(()=>showPage(currentPage))}

function showNewTask(){showModal('<h2>new task</h2><div class="form-group"><label>title</label><input id="t-title"></div><div class="form-group"><label>description</label><textarea id="t-desc" rows="3"></textarea></div><div class="grid-2"><div class="form-group"><label>priority</label><select id="t-prio"><option value="low">low</option><option value="medium" selected>medium</option><option value="high">high</option></select></div><div class="form-group"><label>due</label><input id="t-due" placeholder="YYYY-MM-DD"></div></div><div style="display:flex;gap:6px;margin-top:12px"><button class="btn" onclick="createTask()">create</button><button class="btn btn-outline" onclick="hideModal()">cancel</button></div>')}
async function createTask(){const t=document.getElementById('t-title').value.trim();if(!t)return;await api('/api/tasks/create',{title:t,descr:document.getElementById('t-desc').value.trim(),priority:document.getElementById('t-prio').value,due:document.getElementById('t-due').value});hideModal();showPage(currentPage)}
async function editTask(id){const tasks=await api('/api/tasks');const t=tasks.find(x=>x.id===id);if(!t)return;showModal('<h2>edit task</h2><div class="form-group"><label>title</label><input id="t-title" value="'+t.title+'"></div><div class="form-group"><label>description</label><textarea id="t-desc" rows="3">'+(t.descr||'')+'</textarea></div><div class="grid-2"><div class="form-group"><label>priority</label><select id="t-prio"><option value="low"'+(t.priority==='low'?' selected':'')+'>low</option><option value="medium"'+(t.priority==='medium'?' selected':'')+'>medium</option><option value="high"'+(t.priority==='high'?' selected':'')+'>high</option></select></div><div class="form-group"><label>status</label><select id="t-stat"><option value="pending"'+(t.status==='pending'?' selected':'')+'>pending</option><option value="in_progress"'+(t.status==='in_progress'?' selected':'')+'>in progress</option><option value="done"'+(t.status==='done'?' selected':'')+'>done</option></select></div></div><div class="form-group"><label>due</label><input id="t-due" value="'+(t.due||'')+'" placeholder="YYYY-MM-DD"></div><div style="display:flex;gap:6px;margin-top:12px"><button class="btn" onclick="saveTask('+id+')">save</button><button class="btn btn-outline" onclick="hideModal()">cancel</button></div>')}
async function saveTask(id){await api('/api/tasks/update',{id,title:document.getElementById('t-title').value.trim(),descr:document.getElementById('t-desc').value.trim(),priority:document.getElementById('t-prio').value,status:document.getElementById('t-stat').value,due:document.getElementById('t-due').value});hideModal();showPage(currentPage)}
async function delTask(id){if(!confirm('delete?'))return;await api('/api/tasks/delete',{id});showPage(currentPage)}

function showNewMsg(){showModal('<h2>new message</h2><div class="form-group"><label>subject</label><input id="m-subj"></div><div class="form-group"><label>message</label><textarea id="m-body" rows="4"></textarea></div><div style="display:flex;gap:6px;margin-top:12px"><button class="btn" onclick="sendMsg()">send</button><button class="btn btn-outline" onclick="hideModal()">cancel</button></div>')}
async function sendMsg(){const s=document.getElementById('m-subj').value.trim(),b=document.getElementById('m-body').value.trim();if(!s||!b)return;await api('/api/messages/send',{to_type:'all',subject:s,body:b});hideModal();showPage(currentPage)}
async function viewMsg(id){const msgs=await api('/api/messages');const m=msgs.find(x=>x.id===id);if(!m)return;await api('/api/messages/read',{id});showModal('<h2>'+m.subject+'</h2><div style="font-size:8px;color:var(--text2);margin-bottom:10px;letter-spacing:1px">from: '+(m.from_name||'system')+' · '+(m.time||'').slice(0,16)+'</div><div style="font-size:11px;line-height:1.6;white-space:pre-wrap">'+m.body+'</div><div style="margin-top:12px"><button class="btn btn-sm btn-outline" onclick="hideModal()">close</button></div>')}

function showEmergency(){showModal('<h2>⚠ emergency</h2><div class="form-group"><label>message</label><textarea id="em-msg" rows="4"></textarea></div><div style="display:flex;gap:6px;margin-top:12px"><button class="btn btn-danger" onclick="sendEmergency()">⚠ broadcast</button><button class="btn btn-outline" onclick="hideModal()">cancel</button></div>')}
async function sendEmergency(){const msg=document.getElementById('em-msg').value.trim();if(!msg)return;await api('/api/emergency',{text:msg});hideModal();showPage(currentPage)}

function showNewUser(){showModal('<h2>add employee</h2><div class="form-group"><label>username</label><input id="u-user"></div><div class="form-group"><label>name</label><input id="u-name"></div><div class="form-group"><label>password</label><input id="u-pass" type="password"></div><div class="form-group"><label>role</label><select id="u-role"><option value="employee">employee</option><option value="manager">manager</option><option value="admin">admin</option><option value="super_admin">super_admin</option></select></div><div style="display:flex;gap:6px;margin-top:12px"><button class="btn" onclick="createUser()">create</button><button class="btn btn-outline" onclick="hideModal()">cancel</button></div>')}
async function createUser(){const u=document.getElementById('u-user').value.trim();if(!u)return;await api('/api/users/create',{username:u,name:document.getElementById('u-name').value.trim()||u,password:document.getElementById('u-pass').value,role:document.getElementById('u-role').value});hideModal();showPage(currentPage)}
async function editUser(id){const emps=await api('/api/users');const e=emps.find(x=>x.id===id);if(!e)return;showModal('<h2>edit employee</h2><div class="form-group"><label>name</label><input id="u-name" value="'+e.name+'"></div><div class="form-group"><label>email</label><input id="u-email" value="'+(e.email||'')+'"></div><div class="form-group"><label>role</label><select id="u-role"><option value="employee"'+(e.role==='employee'?' selected':'')+'>employee</option><option value="manager"'+(e.role==='manager'?' selected':'')+'>manager</option><option value="admin"'+(e.role==='admin'?' selected':'')+'>admin</option><option value="super_admin"'+(e.role==='super_admin'?' selected':'')+'>super_admin</option></select></div><div class="form-group"><label>new password (leave blank to keep)</label><input id="u-pass" type="password"></div><div class="form-group"><label>active</label><select id="u-active"><option value="1"'+(e.active?' selected':'')+'>active</option><option value="0"'+(!e.active?' selected':'')+'>inactive</option></select></div><div style="display:flex;gap:6px;margin-top:12px"><button class="btn" onclick="saveUser('+id+')">save</button><button class="btn btn-outline" onclick="hideModal()">cancel</button></div>')}
async function saveUser(id){await api('/api/users/update',{id,name:document.getElementById('u-name').value.trim(),email:document.getElementById('u-email').value,role:document.getElementById('u-role').value,password:document.getElementById('u-pass').value,active:parseInt(document.getElementById('u-active').value)});hideModal();showPage(currentPage)}

function hideModal(){document.getElementById('modal').classList.remove('s')}
function showModal(html){document.getElementById('modal-content').innerHTML=html;document.getElementById('modal').classList.add('s')}
document.getElementById('modal').addEventListener('click',e=>{if(e.target===document.getElementById('modal'))hideModal()})

document.getElementById('login-btn').addEventListener('click',async()=>{
  const ok=await login(document.getElementById('login-user').value.trim(),document.getElementById('login-pass').value)
  document.getElementById('login-error').style.display=ok?'none':'block'
})
document.getElementById('login-user').addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('login-pass').focus()})
document.getElementById('login-pass').addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('login-btn').click()})
document.getElementById('page-title').addEventListener('click',()=>document.getElementById('sidebar').classList.toggle('s'))

// Poll emergency
setInterval(async()=>{
  if(!session)return
  const n=await api('/api/notifications')
  const em=n.find(x=>x.urgent&&!x.read)
  if(em){
    document.getElementById('emergency-title').textContent='⚠ EMERGENCY'
    document.getElementById('emergency-msg').textContent=em.text
    document.getElementById('emergency-from').textContent='from: '+(em.from_name||'system')
    document.getElementById('emergency-overlay').classList.add('s')
  }
},5000)
function dismissEmergency(){document.getElementById('emergency-overlay').classList.remove('s')}
</script>
</body>
</html>"""

# ---------- Main ----------

def main():
    if MODE == "setup":
        port = 8765
        print("\n  ⬡ EMPLOYEE MANAGER — Setup")
        print(f"  Open http://localhost:{port} in your browser\n")
    elif MODE == "server":
        port = config.get("port", 8765)
        print(f"\n  ⬡ EMPLOYEE MANAGER — Server mode")
        print(f"  Local:   http://localhost:{port}")
        print(f"  Network: http://{get_local_ip()}:{port}")
        print(f"  Server:  {config.get('server_name','Employee Manager')}\n")
        init_db()
    elif MODE == "client":
        port = config.get("port", 8765)
        print(f"\n  ⬡ EMPLOYEE MANAGER — Client mode")
        print(f"  Local:   http://localhost:{port}")
        print(f"  Server:  {config.get('remote_host','?')}:{config.get('remote_port','?')}\n")

    server = HTTPServer(("0.0.0.0", port), Handler)
    webbrowser.open(f"http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye\n")

def get_local_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except:
        return "127.0.0.1"
    finally:
        s.close()

if __name__ == "__main__":
    main()
