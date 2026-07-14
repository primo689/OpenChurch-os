#!/usr/bin/env python3
"""
Church Youth Manager — a tiny self-hosted server for any church.

First run: open the app in a browser and it walks you through setup
(church name, ministry name, first admin account). No code editing needed.

All data lives in data.db (one file). Back it up by copying that file.
Admins manage user accounts from the More tab inside the app.
"""

import csv
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, request, session, send_from_directory, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

VERSION = "1.0.0"
PORT = int(os.environ.get("OPENCHURCH_PORT", 8080))
MIN_PASSWORD_LEN = 8
# Record IDs may only look like the ones the app generates — this blocks
# anyone from smuggling script tags or path tricks in through an ID.
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BODY = 200_000  # 200KB is far bigger than any legitimate record

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("OPENCHURCH_DATA_DIR") or BASE
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "data.db")
SECRET_PATH = os.path.join(DATA_DIR, ".secret")

# A secret key keeps people logged in across server restarts.
# chmod 600 = only the account running the server can read it.
if not os.path.exists(SECRET_PATH):
    with open(SECRET_PATH, "w") as f:
        f.write(secrets.token_hex(32))
os.chmod(SECRET_PATH, 0o600)
with open(SECRET_PATH) as f:
    SECRET_KEY = f.read().strip()

app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 90  # 90 days
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY

DATA_TABLES = ["students", "events", "volunteers", "attendance"]

# One process, so a plain lock is enough to make attendance toggles atomic.
ATT_LOCK = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    for t in DATA_TABLES:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {t} (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
             id TEXT PRIMARY KEY,
             username TEXT UNIQUE NOT NULL,
             name TEXT NOT NULL,
             role TEXT NOT NULL,
             pw_hash TEXT NOT NULL,
             session_epoch INTEGER NOT NULL DEFAULT 0)"""
    )
    # Upgrade path for databases created before session_epoch existed.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already there
    conn.commit()
    conn.close()


init_db()


# ---------- helpers ----------
def get_settings():
    conn = db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_settings(d):
    conn = db()
    for k, v in d.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, str(v)),
        )
    conn.commit()
    conn.close()


def user_count():
    conn = db()
    n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    conn.close()
    return n


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    conn = db()
    row = conn.execute(
        "SELECT id, username, name, role, session_epoch FROM users WHERE id=?", (uid,)
    ).fetchone()
    conn.close()
    # A password change bumps session_epoch, which kicks out every device
    # that logged in before the change — including a lost or stolen phone.
    if not row or session.get("epoch", 0) != row["session_epoch"]:
        return None
    u = dict(row)
    del u["session_epoch"]
    return u


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "not logged in"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({"error": "not logged in"}), 401
        if u["role"] != "admin":
            return jsonify({"error": "admins only"}), 403
        return f(*args, **kwargs)
    return wrapper


# ---------- pages ----------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- setup & auth ----------
@app.route("/api/setup-status")
def setup_status():
    s = get_settings()
    return jsonify({
        "needsSetup": user_count() == 0,
        "churchName": s.get("churchName", ""),
        "ministryName": s.get("ministryName", ""),
        "version": VERSION,
    })


VALID_GRADES = ["K"] + [str(i) for i in range(1, 13)]


def clean_ministries(raw):
    """Validate a ministries list: [{name, from, to}] -> stored form with ids.
    Returns None if invalid."""
    if not isinstance(raw, list) or not raw or len(raw) > 10:
        return None
    out = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            return None
        name = str(m.get("name", "")).strip()
        gfrom = str(m.get("from", "K"))
        gto = str(m.get("to", "12"))
        if not name or gfrom not in VALID_GRADES or gto not in VALID_GRADES:
            return None
        out.append({"id": m.get("id") or f"m{i+1}", "name": name,
                    "from": gfrom, "to": gto})
    return out


@app.route("/api/setup", methods=["POST"])
def setup():
    if user_count() > 0:
        return jsonify({"error": "already set up"}), 403
    b = request.get_json(silent=True) or {}
    church = str(b.get("churchName", "")).strip()
    name = str(b.get("adminName", "")).strip()
    username = str(b.get("username", "")).strip().lower()
    password = str(b.get("password", ""))
    ministries = clean_ministries(b.get("ministries") or
                                  [{"name": str(b.get("ministryName", "")).strip() or "Youth Ministry"}])
    if not (church and name and username and ministries and len(password) >= MIN_PASSWORD_LEN):
        return jsonify({"error": f"missing fields or password under {MIN_PASSWORD_LEN} characters"}), 400
    set_settings({"churchName": church,
                  "ministryName": ministries[0]["name"],
                  "ministries": json.dumps(ministries)})
    conn = db()
    conn.execute(
        "INSERT INTO users (id, username, name, role, pw_hash) VALUES (?, ?, ?, ?, ?)",
        (secrets.token_hex(8), username, name, "admin", generate_password_hash(password)),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# Guessing protection: 5 wrong passwords locks that username for 15 minutes.
LOGIN_FAILS = {}  # username -> {"count": int, "locked_until": datetime}
FAIL_LIMIT = 5
LOCK_MINUTES = 15


@app.route("/api/login", methods=["POST"])
def login():
    b = request.get_json(silent=True) or {}
    username = str(b.get("username", "")).strip().lower()
    password = str(b.get("password", ""))
    rec = LOGIN_FAILS.get(username)
    if rec and rec.get("locked_until") and datetime.now() < rec["locked_until"]:
        mins = int((rec["locked_until"] - datetime.now()).total_seconds() // 60) + 1
        return jsonify({"error": f"too many wrong attempts — try again in {mins} min"}), 429
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if row and check_password_hash(row["pw_hash"], password):
        LOGIN_FAILS.pop(username, None)
        session.permanent = True
        session["uid"] = row["id"]
        session["epoch"] = row["session_epoch"]
        return jsonify({"ok": True})
    rec = LOGIN_FAILS.setdefault(username, {"count": 0, "locked_until": None})
    rec["count"] += 1
    if rec["count"] >= FAIL_LIMIT:
        rec["locked_until"] = datetime.now() + timedelta(minutes=LOCK_MINUTES)
        rec["count"] = 0
    return jsonify({"error": "wrong username or password"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# ---------- data ----------
def get_table(name):
    conn = db()
    rows = conn.execute(f"SELECT id, data FROM {name}").fetchall()
    conn.close()
    return {r["id"]: json.loads(r["data"]) for r in rows}


@app.route("/api/all")
@login_required
def get_all():
    out = {t: get_table(t) for t in DATA_TABLES}
    s = get_settings()
    return jsonify({
        "students": list(out["students"].values()),
        "events": list(out["events"].values()),
        "volunteers": list(out["volunteers"].values()),
        "attendance": out["attendance"],
        "settings": {"churchName": s.get("churchName", ""),
                     "ministryName": s.get("ministryName", ""),
                     "lastPromotionYear": s.get("lastPromotionYear", ""),
                     "ministries": json.loads(s["ministries"]) if s.get("ministries")
                                   else [{"id": "m1",
                                          "name": s.get("ministryName", "Youth Ministry"),
                                          "from": "K", "to": "12"}]},
        "me": current_user(),
        "version": VERSION,
    })


@app.route("/api/<table>/<item_id>", methods=["PUT"])
@login_required
def put_item(table, item_id):
    if table not in DATA_TABLES:
        return jsonify({"error": "unknown table"}), 404
    if not ID_RE.match(item_id):
        return jsonify({"error": "bad id"}), 400
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "bad data"}), 400
    conn = db()
    conn.execute(
        f"INSERT INTO {table} (id, data) VALUES (?, ?) "
        f"ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (item_id, json.dumps(data)),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/<table>/<item_id>", methods=["DELETE"])
@login_required
def delete_item(table, item_id):
    if table not in DATA_TABLES:
        return jsonify({"error": "unknown table"}), 404
    if not ID_RE.match(item_id):
        return jsonify({"error": "bad id"}), 400
    conn = db()
    conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
    if table == "events":  # remove that event's attendance too
        conn.execute("DELETE FROM attendance WHERE id=?", (item_id,))
    if table == "students":
        # Scrub the student out of every attendance record so event
        # counts don't stay inflated by a kid who no longer exists.
        rows = conn.execute("SELECT id, data FROM attendance").fetchall()
        for r in rows:
            rec = json.loads(r["data"])
            changed = False
            for field in ("present", "permission"):
                if item_id in rec.get(field, {}):
                    del rec[field][item_id]
                    changed = True
            if changed:
                conn.execute("UPDATE attendance SET data=? WHERE id=?",
                             (json.dumps(rec), r["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/attendance/<event_id>/toggle", methods=["POST"])
@login_required
def toggle_attendance(event_id):
    """Flip one checkmark atomically. Two volunteers checking kids in on
    two phones can no longer overwrite each other's work."""
    if not ID_RE.match(event_id):
        return jsonify({"error": "bad id"}), 400
    b = request.get_json(silent=True) or {}
    field = b.get("field")
    sid = str(b.get("studentId", ""))
    if field not in ("present", "permission") or not ID_RE.match(sid):
        return jsonify({"error": "bad request"}), 400
    with ATT_LOCK:
        conn = db()
        row = conn.execute("SELECT data FROM attendance WHERE id=?", (event_id,)).fetchone()
        rec = json.loads(row["data"]) if row else {"present": {}, "permission": {}}
        rec.setdefault(field, {})
        rec[field][sid] = not rec[field].get(sid)
        conn.execute(
            "INSERT INTO attendance (id, data) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (event_id, json.dumps(rec)),
        )
        conn.commit()
        conn.close()
    return jsonify(rec)


# ---------- settings (admin) ----------
@app.route("/api/settings", methods=["PUT"])
@admin_required
def update_settings():
    b = request.get_json(silent=True) or {}
    church = str(b.get("churchName", "")).strip()
    if not church:
        return jsonify({"error": "church name required"}), 400
    extra = {}
    if "lastPromotionYear" in b:
        extra["lastPromotionYear"] = str(b["lastPromotionYear"])
    if "ministries" in b:
        ministries = clean_ministries(b["ministries"])
        if ministries is None:
            return jsonify({"error": "each ministry needs a name and valid grade range"}), 400
        extra["ministries"] = json.dumps(ministries)
        extra["ministryName"] = ministries[0]["name"]
    elif b.get("ministryName"):
        extra["ministryName"] = str(b["ministryName"]).strip()
    set_settings({"churchName": church, **extra})
    return jsonify({"ok": True})


# ---------- user management (admin) ----------
@app.route("/api/users")
@admin_required
def list_users():
    conn = db()
    rows = conn.execute("SELECT id, username, name, role FROM users ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/users", methods=["POST"])
@admin_required
def create_user():
    b = request.get_json(silent=True) or {}
    name = str(b.get("name", "")).strip()
    username = str(b.get("username", "")).strip().lower()
    password = str(b.get("password", ""))
    role = b.get("role", "leader")
    if role not in ("admin", "leader"):
        role = "leader"
    if not (name and username and len(password) >= MIN_PASSWORD_LEN):
        return jsonify({"error": f"name, username, and a password of {MIN_PASSWORD_LEN}+ characters are required"}), 400
    conn = db()
    try:
        conn.execute(
            "INSERT INTO users (id, username, name, role, pw_hash) VALUES (?, ?, ?, ?, ?)",
            (secrets.token_hex(8), username, name, role, generate_password_hash(password)),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "that username is already taken"}), 400
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/users/<user_id>", methods=["PUT"])
@admin_required
def update_user(user_id):
    b = request.get_json(silent=True) or {}
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "no such user"}), 404
    name = str(b.get("name", row["name"])).strip() or row["name"]
    role = b.get("role", row["role"])
    if role not in ("admin", "leader"):
        role = row["role"]
    # Never let the last admin demote themselves out of admin.
    if row["role"] == "admin" and role != "admin":
        admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]
        if admins <= 1:
            conn.close()
            return jsonify({"error": "there must be at least one admin"}), 400
    password = b.get("password")
    if password:
        if len(str(password)) < MIN_PASSWORD_LEN:
            conn.close()
            return jsonify({"error": f"password must be {MIN_PASSWORD_LEN}+ characters"}), 400
        # New password + epoch bump = every previously-signed-in device
        # (including a lost phone) has to sign in again.
        new_epoch = row["session_epoch"] + 1
        conn.execute("UPDATE users SET pw_hash=?, session_epoch=? WHERE id=?",
                     (generate_password_hash(str(password)), new_epoch, user_id))
        me = current_user()
        if me and me["id"] == user_id:
            session["epoch"] = new_epoch  # don't kick out the person making the change
    conn.execute("UPDATE users SET name=?, role=? WHERE id=?", (name, role, user_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/users/<user_id>", methods=["DELETE"])
@admin_required
def delete_user(user_id):
    me = current_user()
    if me["id"] == user_id:
        return jsonify({"error": "you can't remove your own account"}), 400
    conn = db()
    row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "no such user"}), 404
    if row["role"] == "admin":
        admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]
        if admins <= 1:
            conn.close()
            return jsonify({"error": "there must be at least one admin"}), 400
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- backup ----------
@app.route("/api/export")
@admin_required
def export_all():
    out = {t: get_table(t) for t in DATA_TABLES}
    s = get_settings()
    payload = {
        "settings": {"churchName": s.get("churchName", ""),
                     "ministryName": s.get("ministryName", ""),
                     "ministries": json.loads(s["ministries"]) if s.get("ministries") else None},
        "students": list(out["students"].values()),
        "events": list(out["events"].values()),
        "volunteers": list(out["volunteers"].values()),
        "attendance": out["attendance"],
    }
    resp = app.response_class(json.dumps(payload, indent=2), mimetype="application/json")
    resp.headers["Content-Disposition"] = "attachment; filename=youth-backup.json"
    return resp


@app.route("/api/import", methods=["POST"])
@admin_required
def import_all():
    """Restores ministry data. User accounts are deliberately NOT part of
    backups, so restoring can never lock anyone out."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "bad backup"}), 400
    conn = db()
    for t in DATA_TABLES:
        conn.execute(f"DELETE FROM {t}")
    for s in data.get("students", []):
        conn.execute("INSERT INTO students (id, data) VALUES (?, ?)", (s["id"], json.dumps(s)))
    for e in data.get("events", []):
        conn.execute("INSERT INTO events (id, data) VALUES (?, ?)", (e["id"], json.dumps(e)))
    for v in data.get("volunteers", []):
        conn.execute("INSERT INTO volunteers (id, data) VALUES (?, ?)", (v["id"], json.dumps(v)))
    for eid, rec in (data.get("attendance") or {}).items():
        conn.execute("INSERT INTO attendance (id, data) VALUES (?, ?)", (eid, json.dumps(rec)))
    conn.commit()
    conn.close()
    if isinstance(data.get("settings"), dict) and data["settings"].get("churchName"):
        restored = {
            "churchName": data["settings"]["churchName"],
            "ministryName": data["settings"].get("ministryName", "Youth Ministry"),
        }
        ministries = clean_ministries(data["settings"].get("ministries") or [])
        if ministries:
            restored["ministries"] = json.dumps(ministries)
        set_settings(restored)
    return jsonify({"ok": True})


# ---------- automatic nightly backups ----------
BACKUP_DIR = os.path.join(BASE, "backups")
KEEP_BACKUPS = 30


def do_backup():
    """Safely copy data.db into backups/, keeping the last 30 days."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = os.path.join(BACKUP_DIR, f"backup-{date.today().isoformat()}.db")
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)  # sqlite's own safe-copy, fine even mid-write
        src.close()
        dst.close()
        old = sorted(f for f in os.listdir(BACKUP_DIR)
                     if f.startswith("backup-") and f.endswith(".db"))
        for f in old[:-KEEP_BACKUPS]:
            os.remove(os.path.join(BACKUP_DIR, f))
    except Exception as e:
        print("Backup failed:", e)


def backup_loop():
    while True:
        do_backup()
        time.sleep(60 * 60 * 24)  # once a day


# ---------- CSV export ----------
def csv_response(rows, filename):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    resp = app.response_class(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


@app.route("/api/export/students.csv")
@admin_required
def export_students_csv():
    students = get_table("students").values()
    rows = [["Name", "Grade", "Birthdate", "Allergies", "Medical",
             "Guardians", "Emergency contact", "Photo consent",
             "Baptized on", "Visitor", "Archived"]]
    for s in students:
        guardians = "; ".join(
            f"{g.get('name','')} ({g.get('relation','')}) {g.get('phone','')}".strip()
            for g in s.get("guardians", []))
        emergency = f"{s.get('emergencyName','')} {s.get('emergencyPhone','')}".strip()
        rows.append([s.get("name",""), s.get("grade",""), s.get("birthdate",""),
                     s.get("allergies",""), s.get("medical",""), guardians,
                     emergency, s.get("photoConsent",""), s.get("baptizedDate",""),
                     "yes" if s.get("visitor") else "",
                     "yes" if s.get("archived") else ""])
    return csv_response(rows, "students.csv")


@app.route("/api/export/attendance.csv")
@admin_required
def export_attendance_csv():
    students = get_table("students")
    events = sorted(get_table("events").values(), key=lambda e: e.get("date", ""))
    attendance = get_table("attendance")
    rows = [["Date", "Event", "Student", "Present", "Permission form"]]
    for e in events:
        rec = attendance.get(e["id"], {})
        for sid, s in students.items():
            present = bool(rec.get("present", {}).get(sid))
            form = bool(rec.get("permission", {}).get(sid))
            if present or form:
                rows.append([e.get("date",""), e.get("name",""), s.get("name",""),
                             "yes" if present else "", "yes" if form else ""])
    return csv_response(rows, "attendance.csv")


if __name__ == "__main__":
    threading.Thread(target=backup_loop, daemon=True).start()
    print(f"Church Youth Manager v{VERSION} running. On this device: http://localhost:{PORT}")
    print(f"From phones on the same wifi, use this machine's IP, e.g. http://192.168.1.50:{PORT}")
    print(f"Nightly backups: {BACKUP_DIR} (keeps last {KEEP_BACKUPS})")
    try:
        from waitress import serve
        serve(app, host=os.environ.get("OPENCHURCH_BIND", "0.0.0.0"), port=PORT, threads=8)
    except ImportError:
        # Works fine without waitress; installing it (sudo apt install
        # python3-waitress) gives a sturdier server for daily use.
        app.run(host=os.environ.get("OPENCHURCH_BIND", "0.0.0.0"), port=PORT, threaded=True)
