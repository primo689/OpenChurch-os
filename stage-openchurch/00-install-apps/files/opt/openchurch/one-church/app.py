#!/usr/bin/env python3
"""
One Church - a self-hosted church membership system for a Raspberry Pi.
Runs on the local network only. No internet required.

Start with:  python3 app.py
Then visit:  http://<pi-ip-address>:8080
"""

import calendar
import csv
import glob
import io
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("OPENCHURCH_DATA_DIR") or os.path.join(BASE_DIR, "data")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
BACKUP_DIR = os.environ.get("OPENCHURCH_BACKUP_DIR") or os.path.join(BASE_DIR, "backups")
DB_PATH = os.path.join(DATA_DIR, "church.db")
SECRET_PATH = os.path.join(DATA_DIR, "secret_key")

IDLE_TIMEOUT_MINUTES = 120      # signed out after this much inactivity
LOGIN_MAX_FAILS = 5             # failed attempts allowed...
LOGIN_WINDOW_MINUTES = 15       # ...within this window, per username

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PHOTO_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB uploads
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"    # blocks cross-site request forgery
app.config["SESSION_COOKIE_HTTPONLY"] = True

# Persistent secret key so logins survive restarts
if os.path.exists(SECRET_PATH):
    os.chmod(SECRET_PATH, 0o600)   # lock down keys created by older versions
    app.secret_key = open(SECRET_PATH, "rb").read()
else:
    app.secret_key = os.urandom(32)
    with open(SECRET_PATH, "wb") as f:
        f.write(app.secret_key)
    os.chmod(SECRET_PATH, 0o600)   # only the account running the app can read it

# ---------------------------------------------------------------- roles

LEADERSHIP_ROLES = ["Admin", "Minister", "Youth Minister", "Elder", "Deacon", "Youth Leader"]

# Roles a login can have. Attendance Taker is a login-only role: it can take
# attendance and nothing else, so Sunday volunteers see names and checkboxes
# but no member records, notes, reports, or giving.
LOGIN_ROLES = LEADERSHIP_ROLES + ["Attendance Taker"]

# Base permissions per login role. Access to giving is configured separately
# on the Admin page (see current_perms) so a church can restrict it to a
# treasurer-style short list.
PERMS = {
    "Admin":          {"members.view", "members.edit", "attendance", "reports",
                       "notes.leaders", "notes.leadership",
                       "groups.edit", "admin", "users", "audit", "data", "youth"},
    "Minister":       {"members.view", "members.edit", "attendance", "reports",
                       "notes.leaders", "notes.leadership",
                       "groups.edit", "admin", "audit", "data", "youth"},
    "Elder":          {"members.view", "members.edit", "attendance", "reports",
                       "notes.leaders", "notes.leadership", "groups.edit", "youth"},
    "Youth Minister": {"members.view", "members.edit", "attendance", "reports",
                       "notes.leaders", "groups.edit", "youth"},
    "Deacon":         {"members.view", "attendance", "reports", "notes.leaders"},
    "Youth Leader":   {"members.view", "attendance", "notes.leaders", "youth"},
    "Attendance Taker": {"attendance"},
}

GRADES = ["K"] + [str(i) for i in range(1, 13)]
NOTE_KINDS = {"general": "General", "followup": "Follow-up", "baptism": "Baptism conversation"}
# Roles whose home screen is the youth dashboard rather than the church one.
YOUTH_FIRST_ROLES = {"Youth Minister", "Youth Leader"}

DEFAULT_GIVING_ROLES = "Admin,Minister,Elder"

STATUSES = ["Active", "Visitor", "Inactive", "Moved", "Deceased"]

RELATIONS = ["Parent", "Child", "Grandparent", "Grandchild", "Spouse", "Sibling"]
INVERSE = {"Parent": "Child", "Child": "Parent", "Grandparent": "Grandchild",
           "Grandchild": "Grandparent", "Spouse": "Spouse", "Sibling": "Sibling"}

MEMBER_FIELDS = ["first_name", "last_name", "status", "is_member", "leadership_role", "birthdate",
                 "membership_date", "baptism_date", "anniversary", "phone", "email", "address"]

# ---------------------------------------------------------------- database

# Schema version 3. Databases from ChurchBook (v1 or v2) and from earlier
# One Church builds all upgrade automatically on startup: new tables are
# CREATE IF NOT EXISTS, and new columns are added by ensure_column() below,
# which checks what's actually in the database instead of trusting a number.
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
  role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  session_epoch INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS members (
  id INTEGER PRIMARY KEY,
  first_name TEXT NOT NULL, last_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Active',
  leadership_role TEXT DEFAULT '',
  is_member INTEGER NOT NULL DEFAULT 0,
  birthdate TEXT, membership_date TEXT, baptism_date TEXT, anniversary TEXT,
  phone TEXT DEFAULT '', email TEXT DEFAULT '', address TEXT DEFAULT '',
  photo TEXT DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_roles (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS member_roles (
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  role_id INTEGER NOT NULL REFERENCES custom_roles(id) ON DELETE CASCADE,
  UNIQUE(member_id, role_id)
);

CREATE TABLE IF NOT EXISTS relationships (
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  related_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  relation TEXT NOT NULL,
  UNIQUE(member_id, related_id, relation)
);

CREATE TABLE IF NOT EXISTS event_types (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attendance (
  id INTEGER PRIMARY KEY,
  event_type_id INTEGER NOT NULL REFERENCES event_types(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  UNIQUE(event_type_id, date, member_id)
);

CREATE TABLE IF NOT EXISTS funds (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS gifts (
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  fund_id INTEGER NOT NULL REFERENCES funds(id),
  date TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  method TEXT DEFAULT '', note TEXT DEFAULT '', recorded_by TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  author TEXT NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'leaders',   -- 'leaders' or 'leadership'
  kind TEXT NOT NULL DEFAULT 'general',         -- 'general', 'followup', 'baptism'
  body TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grp (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS group_members (
  group_id INTEGER NOT NULL REFERENCES grp(id) ON DELETE CASCADE,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  UNIQUE(group_id, member_id)
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  username TEXT NOT NULL, action TEXT NOT NULL, detail TEXT DEFAULT ''
);

-- ---- youth module (from Church Youth Manager) ----
-- A youth profile turns an ordinary member into a student. Everything else
-- about them (name, birthdate, baptism date, attendance, notes) lives on
-- the member record, so nothing is tracked twice.

CREATE TABLE IF NOT EXISTS youth_profiles (
  member_id INTEGER PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
  grade TEXT NOT NULL DEFAULT '?',              -- 'K', '1'..'12', or '?'
  photo_consent TEXT NOT NULL DEFAULT '',       -- '', 'yes', 'no'
  allergies TEXT NOT NULL DEFAULT '',
  medical TEXT NOT NULL DEFAULT '',
  emergency_name TEXT NOT NULL DEFAULT '',
  emergency_phone TEXT NOT NULL DEFAULT '',
  graduated INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guardians (
  id INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  name TEXT NOT NULL, relation TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT ''
);

-- Ministries split the roster by grade range, e.g. Kids K-5 and Youth 6-12.
CREATE TABLE IF NOT EXISTS ministries (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  grade_from TEXT NOT NULL DEFAULT 'K',
  grade_to TEXT NOT NULL DEFAULT '12'
);

CREATE TABLE IF NOT EXISTS youth_events (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL, date TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  permission_required INTEGER NOT NULL DEFAULT 0,
  ministry_id INTEGER REFERENCES ministries(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS youth_attendance (
  event_id INTEGER NOT NULL REFERENCES youth_events(id) ON DELETE CASCADE,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  permission INTEGER NOT NULL DEFAULT 0,        -- permission slip turned in
  UNIQUE(event_id, member_id)
);
"""

def ensure_column(con, table, col, decl, backfill=None):
    """Add a column if the table doesn't have it yet. Safe to run every
    start. Used instead of numbered migrations because databases arrive
    here from three different lineages (ChurchBook v1, ChurchBook v2,
    earlier One Church) whose version numbers don't mean the same thing."""
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        if backfill:
            con.execute(backfill)


def upgrade_columns(con):
    ensure_column(con, "members", "is_member", "INTEGER NOT NULL DEFAULT 0",
                  "UPDATE members SET is_member = 1 "
                  "WHERE membership_date IS NOT NULL AND membership_date != ''")
    ensure_column(con, "notes", "kind", "TEXT NOT NULL DEFAULT 'general'")
    ensure_column(con, "users", "session_epoch", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(con, "youth_events", "ministry_id",
                  "INTEGER REFERENCES ministries(id) ON DELETE SET NULL")

DEFAULT_EVENTS = ["Sunday Morning Worship", "Sunday School", "Sunday Youth Group", "Wednesday Night Youth"]
DEFAULT_FUNDS = ["General", "Missions", "Building"]


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 10000")
    return con


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    con = connect()
    con.execute("PRAGMA journal_mode = WAL")  # lets reads and writes overlap safely
    con.executescript(SCHEMA)
    upgrade_columns(con)
    con.execute("INSERT INTO settings(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
    con.commit()
    con.close()


init_db()


def get_setting(key, default=""):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db().execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db().commit()


def audit(action, detail=""):
    user = session.get("display_name", "system")
    db().execute("INSERT INTO audit(ts,username,action,detail) VALUES(?,?,?,?)",
                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user, action, detail))
    db().commit()


# ---------------------------------------------------------------- auth & CSRF

def current_perms():
    role = session.get("role", "")
    p = set(PERMS.get(role, set()))
    giving_roles = [r.strip() for r in
                    get_setting("giving_roles", DEFAULT_GIVING_ROLES).split(",")]
    if role == "Admin" or role in giving_roles:
        p.add("giving")
    return p


def require(perm):
    """Decorator: user must be logged in and hold this permission."""
    def deco(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            if not get_setting("onboarded"):
                return redirect(url_for("setup"))
            if "user_id" not in session:
                return redirect(url_for("login", next=request.path))
            if perm and perm not in current_perms():
                abort(403)
            return fn(*a, **kw)
        return wrapped
    return deco


@app.before_request
def guard():
    # Every visitor gets a CSRF token tied to their session.
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)

    # Reject POSTs that don't carry the token, or that come from another site.
    # Form posts carry it as a field; the check-in toggle sends JSON instead.
    if request.method == "POST":
        sent = request.form.get("csrf_token")
        if sent is None and request.is_json:
            sent = (request.get_json(silent=True) or {}).get("csrf_token")
        if sent != session["csrf"]:
            abort(400, "Form expired or invalid. Go back, reload the page, and try again.")
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            host = re.sub(r"^https?://", "", origin).split("/")[0]
            if host != request.host:
                abort(400, "Cross-site request blocked.")

    # Session epoch: a password change bumps the user's epoch, which signs
    # out every device that logged in before the change (lost phones included).
    if "user_id" in session and request.endpoint not in ("static",):
        u = db().execute("SELECT session_epoch, active FROM users WHERE id=?",
                         (session["user_id"],)).fetchone()
        if not u or not u["active"] or session.get("epoch", 0) != u["session_epoch"]:
            session.clear()
            flash("You've been signed out. Sign in again.")
            return redirect(url_for("login", next=request.path))

    # Idle timeout: sign out sessions that have been quiet too long.
    if "user_id" in session:
        last = session.get("last_seen", 0)
        if time.time() - last > IDLE_TIMEOUT_MINUTES * 60:
            session.pop("user_id", None)
            session.pop("display_name", None)
            session.pop("role", None)
            if request.endpoint not in ("login", "static", "setup"):
                flash("You were signed out after a period of inactivity.")
                return redirect(url_for("login", next=request.path))
        session["last_seen"] = time.time()


@app.context_processor
def inject_globals():
    return {
        "church_name": get_setting("church_name", "One Church"),
        "perms": current_perms(),
        "me": session.get("display_name"),
        "my_role": session.get("role"),
        "today": date.today().isoformat(),
        "csrf": lambda: Markup(
            f'<input type="hidden" name="csrf_token" value="{session.get("csrf","")}">'),
    }


@app.template_filter("money")
def money(cents):
    return f"${cents/100:,.2f}"


@app.template_filter("nicedate")
def nicedate(iso):
    if not iso:
        return "—"
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
        return f"{dt.strftime('%b')} {dt.day}, {dt.year}"   # works on Linux and Windows
    except ValueError:
        return iso


def parse_date(s):
    """Return ISO date string or empty string."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def upcoming(md, days=14):
    """Does month-day 'md' (like '02-29') fall in the next `days` days?
    Feb 29 birthdays count on Feb 28 in non-leap years.
    Returns days-from-today if upcoming, else None."""
    if not md:
        return None
    for i in range(days + 1):
        d = date.today() + timedelta(days=i)
        if d.strftime("%m-%d") == md:
            return i
        if (md == "02-29" and d.month == 2 and d.day == 28
                and not calendar.isleap(d.year)):
            return i
    return None


# ================================================================ ONBOARDING

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if get_setting("onboarded"):
        return redirect(url_for("home"))
    step = request.args.get("step", "1")

    if request.method == "POST" and step == "1":
        name = request.form.get("church_name", "").strip()
        city = request.form.get("city", "").strip()
        admin_name = request.form.get("admin_name", "").strip()
        username = request.form.get("username", "").strip().lower()
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if not (name and admin_name and username and pw):
            flash("All fields except city are required.")
        elif pw != pw2:
            flash("Passwords do not match.")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters.")
        else:
            set_setting("church_name", name)
            set_setting("city", city)
            db().execute("INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
                         (username, generate_password_hash(pw), admin_name, "Admin"))
            for ev in DEFAULT_EVENTS:
                db().execute("INSERT OR IGNORE INTO event_types(name) VALUES(?)", (ev,))
            for f in DEFAULT_FUNDS:
                db().execute("INSERT OR IGNORE INTO funds(name) VALUES(?)", (f,))
            db().commit()
            row = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            session.update(user_id=row["id"], display_name=row["display_name"],
                           role=row["role"], last_seen=time.time(),
                           epoch=row["session_epoch"],
                           mode="youth" if row["role"] in YOUTH_FIRST_ROLES else "church")
            return redirect(url_for("setup", step="2"))
        return render_template("setup.html", step="1", form=request.form)

    if step == "2":
        if "user_id" not in session:
            return redirect(url_for("setup"))
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                first = request.form.get("first_name", "").strip()
                last = request.form.get("last_name", "").strip()
                role = request.form.get("role", "")
                if first and last and role in LEADERSHIP_ROLES:
                    now = datetime.now().isoformat(timespec="seconds")
                    db().execute(
                        "INSERT INTO members(first_name,last_name,status,leadership_role,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?)", (first, last, "Active", role, now, now))
                    if request.form.get("make_login"):
                        u = request.form.get("login_username", "").strip().lower()
                        p = request.form.get("login_password", "")
                        if u and len(p) >= 8:
                            try:
                                db().execute(
                                    "INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
                                    (u, generate_password_hash(p), f"{first} {last}", role))
                            except sqlite3.IntegrityError:
                                flash(f"Username '{u}' is already taken; member added without a login.")
                        else:
                            flash("Login needs a username and an 8+ character password; member added without a login.")
                    db().commit()
                else:
                    flash("First name, last name, and role are required.")
            elif action == "finish":
                set_setting("onboarded", "1")
                audit("setup", "Initial setup completed")
                flash("Setup complete. Welcome!")
                return redirect(url_for("home"))
        leaders = db().execute(
            "SELECT * FROM members WHERE leadership_role != '' ORDER BY last_name").fetchall()
        return render_template("setup.html", step="2", leaders=leaders,
                               leadership_roles=LEADERSHIP_ROLES)

    return render_template("setup.html", step="1", form={})


# ================================================================ AUTH

_login_fails = {}   # username -> [timestamps of recent failures]


def _throttled(username):
    now = time.time()
    window = LOGIN_WINDOW_MINUTES * 60
    fails = [t for t in _login_fails.get(username, []) if now - t < window]
    _login_fails[username] = fails
    return len(fails) >= LOGIN_MAX_FAILS


@app.route("/login", methods=["GET", "POST"])
def login():
    if not get_setting("onboarded"):
        return redirect(url_for("setup"))
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower()
        p = request.form.get("password", "")
        if _throttled(u):
            audit("login.throttled", u)
            flash(f"Too many failed attempts. Wait {LOGIN_WINDOW_MINUTES} minutes and try again.")
            return render_template("login.html")
        row = db().execute("SELECT * FROM users WHERE username=? AND active=1", (u,)).fetchone()
        if row and check_password_hash(row["password_hash"], p):
            _login_fails.pop(u, None)
            session.update(user_id=row["id"], display_name=row["display_name"],
                           role=row["role"], last_seen=time.time(),
                           epoch=row["session_epoch"],
                           mode="youth" if row["role"] in YOUTH_FIRST_ROLES else "church")
            audit("login")
            nxt = request.args.get("next", "")
            # Only follow same-site paths; ignore anything pointing off-site.
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("home")
            return redirect(nxt)
        _login_fails.setdefault(u, []).append(time.time())
        audit("login.failed", u)
        flash("Wrong username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    audit("logout")
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@require("")
def account():
    if request.method == "POST":
        row = db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        cur = request.form.get("current", "")
        new = request.form.get("new", "")
        new2 = request.form.get("new2", "")
        if not check_password_hash(row["password_hash"], cur):
            flash("Your current password is wrong.")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.")
        elif new != new2:
            flash("New passwords do not match.")
        else:
            new_epoch = row["session_epoch"] + 1
            db().execute("UPDATE users SET password_hash=?, session_epoch=? WHERE id=?",
                         (generate_password_hash(new), new_epoch, row["id"]))
            db().commit()
            session["epoch"] = new_epoch   # every OTHER signed-in device gets kicked out
            audit("user.password_change", "own password (other devices signed out)")
            flash("Password changed.")
            return redirect(url_for("home"))
    return render_template("account.html")


# ================================================================ DASHBOARD

@app.route("/")
@require("")
def home():
    """Front door. Sends each login to the right place: attendance-only
    volunteers to attendance, youth-mode people to the youth dashboard,
    everyone else to the church dashboard. The toggle in the header flips
    mode for anyone who holds both permissions."""
    p = current_perms()
    if "members.view" not in p and "youth" not in p:
        return redirect(url_for("attendance"))
    if session.get("mode") == "youth" and "youth" in p:
        return redirect(url_for("youth_dashboard"))
    if "members.view" not in p:
        return redirect(url_for("youth_dashboard"))
    return redirect(url_for("dashboard"))


@app.route("/mode/<which>")
@require("")
def set_mode(which):
    """Header toggle between church and youth views. Both dashboards stay
    open to anyone with the permission for them, whatever mode they're in -
    mode is a preference, not a wall, so either minister can cover for the
    other without touching settings."""
    if which in ("church", "youth"):
        session["mode"] = which
    return redirect(url_for("home"))


@app.route("/dashboard")
@require("members.view")
def dashboard():
    session["mode"] = "church"
    d = db()
    active = d.execute("SELECT COUNT(*) c FROM members WHERE status='Active'").fetchone()["c"]
    n_members = d.execute("SELECT COUNT(*) c FROM members WHERE is_member=1 AND status='Active'").fetchone()["c"]
    visitors = d.execute("SELECT COUNT(*) c FROM members WHERE status='Visitor'").fetchone()["c"]

    recent = d.execute("""
        SELECT e.name, a.date, COUNT(*) c FROM attendance a
        JOIN event_types e ON e.id = a.event_type_id
        WHERE a.date = (SELECT MAX(date) FROM attendance WHERE event_type_id = a.event_type_id)
        GROUP BY a.event_type_id ORDER BY a.date DESC""").fetchall()

    upcoming_bdays, upcoming_annv = [], []
    for m in d.execute("SELECT * FROM members WHERE status IN ('Active','Visitor')").fetchall():
        i = upcoming(m["birthdate"][5:] if m["birthdate"] else "")
        if i is not None:
            upcoming_bdays.append((i, m))
        i = upcoming(m["anniversary"][5:] if m["anniversary"] else "")
        if i is not None:
            upcoming_annv.append((i, m))
    upcoming_bdays = [m for _, m in sorted(upcoming_bdays, key=lambda x: x[0])]
    upcoming_annv = [m for _, m in sorted(upcoming_annv, key=lambda x: x[0])]

    cutoff = (date.today() - timedelta(days=28)).isoformat()
    lapsed = d.execute("""
        SELECT m.id, m.first_name, m.last_name, MAX(a.date) last_seen
        FROM members m LEFT JOIN attendance a ON a.member_id = m.id
        WHERE m.status = 'Active'
        GROUP BY m.id HAVING last_seen IS NULL OR last_seen < ?
        ORDER BY last_seen""", (cutoff,)).fetchall()

    return render_template("dashboard.html", active=active, visitors=visitors,
                           n_members=n_members,
                           recent=recent, bdays=upcoming_bdays, annv=upcoming_annv,
                           lapsed=lapsed)


# ================================================================ MEMBERS

def member_query(q="", status="", role_id="", membership=""):
    sql = """SELECT DISTINCT m.* FROM members m
             LEFT JOIN member_roles mr ON mr.member_id = m.id WHERE 1=1"""
    args = []
    if q:
        like = f"%{q}%"
        sql += """ AND (m.first_name LIKE ? OR m.last_name LIKE ?
                   OR (m.first_name || ' ' || m.last_name) LIKE ?
                   OR m.email LIKE ? OR m.phone LIKE ? OR m.address LIKE ?)"""
        args += [like] * 6
    if status:
        sql += " AND m.status = ?"
        args.append(status)
    if role_id:
        sql += " AND mr.role_id = ?"
        args.append(role_id)
    if membership == "members":
        sql += " AND m.is_member = 1"
    elif membership == "nonmembers":
        sql += " AND m.is_member = 0"
    sql += " ORDER BY m.last_name, m.first_name"
    return db().execute(sql, args).fetchall()


@app.route("/members")
@require("members.view")
def members():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    role_id = request.args.get("role", "")
    membership = request.args.get("membership", "")
    rows = member_query(q, status, role_id, membership)
    roles = db().execute("SELECT * FROM custom_roles ORDER BY name").fetchall()
    return render_template("members_list.html", rows=rows, q=q, status=status,
                           role_id=role_id, statuses=STATUSES, roles=roles,
                           membership=membership)


def member_form_save(mid=None):
    """Validate and save. Returns member id on success, None on validation error."""
    f = request.form
    first = f.get("first_name", "").strip()
    last = f.get("last_name", "").strip()
    if not (first and last):
        flash("First and last name are required.")
        return None
    vals = dict(
        first_name=first, last_name=last,
        status=f.get("status") if f.get("status") in STATUSES else "Active",
        leadership_role=f.get("leadership_role", "") if f.get("leadership_role", "") in LEADERSHIP_ROLES + [""] else "",
        birthdate=parse_date(f.get("birthdate")),
        membership_date=parse_date(f.get("membership_date")),
        baptism_date=parse_date(f.get("baptism_date")),
        anniversary=parse_date(f.get("anniversary")),
        phone=f.get("phone", "").strip(), email=f.get("email", "").strip(),
        address=f.get("address", "").strip(),
        is_member=1 if f.get("is_member") else 0,
    )
    now = datetime.now().isoformat(timespec="seconds")
    if mid is None:
        cur = db().execute(
            """INSERT INTO members(first_name,last_name,status,is_member,leadership_role,birthdate,
               membership_date,baptism_date,anniversary,phone,email,address,created_at,updated_at)
               VALUES(:first_name,:last_name,:status,:is_member,:leadership_role,:birthdate,
               :membership_date,:baptism_date,:anniversary,:phone,:email,:address,:now,:now)""",
            {**vals, "now": now})
        mid = cur.lastrowid
        audit("member.create", f"{first} {last} (#{mid})")
    else:
        old = db().execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
        changes = [f"{k}: '{old[k] or ''}' -> '{v}'" for k, v in vals.items()
                   if (old[k] or "") != v]
        db().execute(
            """UPDATE members SET first_name=:first_name,last_name=:last_name,status=:status,
               is_member=:is_member,
               leadership_role=:leadership_role,birthdate=:birthdate,membership_date=:membership_date,
               baptism_date=:baptism_date,anniversary=:anniversary,phone=:phone,email=:email,
               address=:address,updated_at=:now WHERE id=:mid""",
            {**vals, "now": now, "mid": mid})
        audit("member.edit", f"{first} {last} (#{mid}): " +
              ("; ".join(changes) if changes else "no field changes"))

    # Photo: remove any older photo files for this member so nothing is orphaned.
    photo = request.files.get("photo")
    if photo and photo.filename:
        ext = os.path.splitext(secure_filename(photo.filename))[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            for old_file in glob.glob(os.path.join(PHOTO_DIR, f"member_{mid}.*")):
                os.remove(old_file)
            fname = f"member_{mid}{ext}"
            photo.save(os.path.join(PHOTO_DIR, fname))
            db().execute("UPDATE members SET photo=? WHERE id=?", (fname, mid))
        else:
            flash("Photo must be a .jpg, .png, or .webp file.")
    db().commit()
    return mid


@app.route("/members/new", methods=["GET", "POST"])
@require("members.edit")
def member_new():
    if request.method == "POST":
        mid = member_form_save()
        if mid:
            return redirect(url_for("member_view", mid=mid))
        # Validation failed: re-show the form with everything they typed.
        return render_template("member_form.html", m=None, v=request.form,
                               statuses=STATUSES, leadership_roles=LEADERSHIP_ROLES)
    return render_template("member_form.html", m=None, v={}, statuses=STATUSES,
                           leadership_roles=LEADERSHIP_ROLES)


@app.route("/members/<int:mid>/edit", methods=["GET", "POST"])
@require("members.edit")
def member_edit(mid):
    m = db().execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not m:
        abort(404)
    if request.method == "POST":
        if member_form_save(mid):
            return redirect(url_for("member_view", mid=mid))
        return render_template("member_form.html", m=m, v=request.form,
                               statuses=STATUSES, leadership_roles=LEADERSHIP_ROLES)
    return render_template("member_form.html", m=m, v=dict(m), statuses=STATUSES,
                           leadership_roles=LEADERSHIP_ROLES)


@app.route("/members/<int:mid>")
@require("members.view")
def member_view(mid):
    d = db()
    m = d.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not m:
        abort(404)

    fam = d.execute("""
        SELECT r.id rid, r.relation, x.id, x.first_name, x.last_name FROM relationships r
        JOIN members x ON x.id = r.related_id WHERE r.member_id = ?
        ORDER BY r.relation""", (mid,)).fetchall()

    my_roles = d.execute("""SELECT cr.* FROM custom_roles cr
        JOIN member_roles mr ON mr.role_id = cr.id WHERE mr.member_id=? ORDER BY cr.name""",
        (mid,)).fetchall()
    all_roles = d.execute("SELECT * FROM custom_roles ORDER BY name").fetchall()

    vis = ["leaders", "leadership"] if "notes.leadership" in current_perms() else ["leaders"]
    notes = d.execute(
        f"SELECT * FROM notes WHERE member_id=? AND visibility IN ({','.join('?'*len(vis))}) "
        "ORDER BY created_at DESC", (mid, *vis)).fetchall()

    recent_att = d.execute("""SELECT a.date, e.name FROM attendance a
        JOIN event_types e ON e.id=a.event_type_id
        WHERE a.member_id=? ORDER BY a.date DESC LIMIT 10""", (mid,)).fetchall()

    gift_years = []
    if "giving" in current_perms():
        gift_years = [r["y"] for r in d.execute(
            "SELECT DISTINCT substr(date,1,4) y FROM gifts WHERE member_id=? ORDER BY y DESC",
            (mid,)).fetchall()]

    groups = d.execute("""SELECT g.* FROM grp g JOIN group_members gm ON gm.group_id=g.id
        WHERE gm.member_id=? ORDER BY g.name""", (mid,)).fetchall()

    others = d.execute("SELECT id, first_name, last_name FROM members WHERE id != ? "
                       "ORDER BY last_name, first_name", (mid,)).fetchall()

    yp, gdns, recent_youth = None, [], []
    if "youth" in current_perms():
        yp = d.execute("SELECT * FROM youth_profiles WHERE member_id=?", (mid,)).fetchone()
        if yp:
            gdns = d.execute("SELECT * FROM guardians WHERE member_id=? ORDER BY id", (mid,)).fetchall()
            recent_youth = d.execute("""SELECT e.date, e.name FROM youth_attendance ya
                JOIN youth_events e ON e.id=ya.event_id
                WHERE ya.member_id=? ORDER BY e.date DESC LIMIT 10""", (mid,)).fetchall()

    return render_template("member_view.html", m=m, fam=fam, my_roles=my_roles,
                           all_roles=all_roles, notes=notes, recent_att=recent_att,
                           gift_years=gift_years, groups=groups, others=others,
                           relations=RELATIONS, yp=yp, gdns=gdns,
                           recent_youth=recent_youth, note_kinds=NOTE_KINDS)


@app.route("/members/<int:mid>/delete", methods=["POST"])
@require("users")
def member_delete(mid):
    m = db().execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not m:
        abort(404)
    # Foreign keys cascade: roles, attendance, notes, gifts, groups, relationships.
    db().execute("DELETE FROM members WHERE id=?", (mid,))
    db().commit()
    for old_file in glob.glob(os.path.join(PHOTO_DIR, f"member_{mid}.*")):
        os.remove(old_file)
    audit("member.delete", f"{m['first_name']} {m['last_name']} (#{mid})")
    flash("Member and their records were deleted.")
    return redirect(url_for("members"))


@app.route("/members/<int:mid>/membership", methods=["POST"])
@require("members.edit")
def member_membership(mid):
    m = db().execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not m:
        abort(404)
    if m["is_member"]:
        db().execute("UPDATE members SET is_member=0 WHERE id=?", (mid,))
        audit("member.membership", f"{m['first_name']} {m['last_name']} (#{mid}) -> not a member")
        flash("Marked as not a member. Their membership date was kept for the record.")
    else:
        if not m["membership_date"]:
            db().execute("UPDATE members SET is_member=1, membership_date=? WHERE id=?",
                         (date.today().isoformat(), mid))
            flash("Marked as a member; membership date set to today. Edit it if the real date differs.")
        else:
            db().execute("UPDATE members SET is_member=1 WHERE id=?", (mid,))
            flash("Marked as a member.")
        audit("member.membership", f"{m['first_name']} {m['last_name']} (#{mid}) -> member")
    db().commit()
    return redirect(url_for("member_view", mid=mid))


@app.route("/photos/<path:fname>")
@require("members.view")
def photo(fname):
    fname = secure_filename(fname)
    path = os.path.join(PHOTO_DIR, fname)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


# ----- relationships

@app.route("/members/<int:mid>/relationships", methods=["POST"])
@require("members.edit")
def relationship_add(mid):
    rel = request.form.get("relation")
    other = request.form.get("related_id", type=int)
    if rel in RELATIONS and other and other != mid:
        try:
            db().execute("INSERT INTO relationships(member_id,related_id,relation) VALUES(?,?,?)",
                         (mid, other, rel))
            db().execute("INSERT OR IGNORE INTO relationships(member_id,related_id,relation) VALUES(?,?,?)",
                         (other, mid, INVERSE[rel]))
            db().commit()
            audit("relationship.add", f"member #{mid} -> {rel} #{other}")
        except sqlite3.IntegrityError:
            flash("That relationship already exists.")
    return redirect(url_for("member_view", mid=mid))


@app.route("/relationships/<int:rid>/delete", methods=["POST"])
@require("members.edit")
def relationship_delete(rid):
    r = db().execute("SELECT * FROM relationships WHERE id=?", (rid,)).fetchone()
    if r:
        db().execute("DELETE FROM relationships WHERE id=?", (rid,))
        db().execute("DELETE FROM relationships WHERE member_id=? AND related_id=? AND relation=?",
                     (r["related_id"], r["member_id"], INVERSE[r["relation"]]))
        db().commit()
        audit("relationship.delete", f"#{r['member_id']} x #{r['related_id']}")
    return redirect(request.referrer or url_for("members"))


# ----- custom roles on a member

@app.route("/members/<int:mid>/roles", methods=["POST"])
@require("members.edit")
def member_role_set(mid):
    action = request.form.get("action")
    if action == "add":
        rid = request.form.get("role_id", type=int)
        newname = request.form.get("new_role", "").strip()
        if newname:
            db().execute("INSERT OR IGNORE INTO custom_roles(name) VALUES(?)", (newname,))
            rid = db().execute("SELECT id FROM custom_roles WHERE name=?", (newname,)).fetchone()["id"]
        if rid:
            db().execute("INSERT OR IGNORE INTO member_roles(member_id,role_id) VALUES(?,?)", (mid, rid))
            audit("member.role", f"member #{mid} role #{rid} added")
    elif action == "remove":
        rid = request.form.get("role_id", type=int)
        db().execute("DELETE FROM member_roles WHERE member_id=? AND role_id=?", (mid, rid))
        audit("member.role", f"member #{mid} role #{rid} removed")
    db().commit()
    return redirect(url_for("member_view", mid=mid))


# ----- notes

@app.route("/members/<int:mid>/notes", methods=["POST"])
@require("notes.leaders")
def note_add(mid):
    body = request.form.get("body", "").strip()
    vis = request.form.get("visibility", "leaders")
    kind = request.form.get("kind", "general")
    if kind not in NOTE_KINDS:
        kind = "general"
    if vis == "leadership" and "notes.leadership" not in current_perms():
        vis = "leaders"
    if body:
        db().execute("INSERT INTO notes(member_id,author,visibility,kind,body,created_at) VALUES(?,?,?,?,?,?)",
                     (mid, session["display_name"], vis, kind, body,
                      datetime.now().strftime("%Y-%m-%d %H:%M")))
        db().commit()
        audit("note.add", f"member #{mid} ({vis}, {kind})")
    return redirect(url_for("member_view", mid=mid))


@app.route("/notes/<int:nid>/delete", methods=["POST"])
@require("notes.leadership")
def note_delete(nid):
    db().execute("DELETE FROM notes WHERE id=?", (nid,))
    db().commit()
    audit("note.delete", f"note #{nid}")
    return redirect(request.referrer or url_for("members"))


# ================================================================ ATTENDANCE

@app.route("/attendance", methods=["GET", "POST"])
@require("attendance")
def attendance():
    d = db()
    events = d.execute("SELECT * FROM event_types WHERE active=1 ORDER BY name").fetchall()
    eid = request.values.get("event", type=int)
    day = parse_date(request.values.get("date")) or date.today().isoformat()

    if request.method == "POST" and eid:
        checked = set(request.form.getlist("present", type=int))
        existing = d.execute("SELECT COUNT(*) c FROM attendance WHERE event_type_id=? AND date=?",
                             (eid, day)).fetchone()["c"]
        # Guard: don't let an accidental empty save wipe a recorded date.
        if not checked and existing and not request.form.get("confirm_clear"):
            flash(f"This would erase all {existing} saved records for this date. "
                  "If that's what you want, check the confirmation box and save again.")
            return redirect(url_for("attendance", event=eid, date=day, guard=1))
        d.execute("DELETE FROM attendance WHERE event_type_id=? AND date=?", (eid, day))
        d.executemany("INSERT INTO attendance(event_type_id,date,member_id) VALUES(?,?,?)",
                      [(eid, day, m) for m in checked])
        d.commit()
        ev = d.execute("SELECT name FROM event_types WHERE id=?", (eid,)).fetchone()
        audit("attendance.save", f"{ev['name']} {day}: {len(checked)} present")
        flash(f"Saved: {len(checked)} present at {ev['name']} on {nicedate(day)}.")
        return redirect(url_for("attendance", event=eid, date=day))

    roster, present = [], set()
    if eid:
        roster = d.execute("""SELECT * FROM members WHERE status IN ('Active','Visitor')
                              ORDER BY last_name, first_name""").fetchall()
        present = {r["member_id"] for r in d.execute(
            "SELECT member_id FROM attendance WHERE event_type_id=? AND date=?", (eid, day)).fetchall()}
    return render_template("attendance.html", events=events, eid=eid, day=day,
                           roster=roster, present=present,
                           guard=request.args.get("guard"))


@app.route("/attendance/visitor", methods=["POST"])
@require("attendance")
def attendance_visitor():
    """Quick-add a walk-in: creates a Visitor member and marks them present."""
    eid = request.args.get("event", type=int)
    day = parse_date(request.args.get("date")) or date.today().isoformat()
    first = request.form.get("first_name", "").strip()
    last = request.form.get("last_name", "").strip()
    if first and last and eid:
        now = datetime.now().isoformat(timespec="seconds")
        cur = db().execute(
            "INSERT INTO members(first_name,last_name,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?)", (first, last, "Visitor", now, now))
        db().execute("INSERT INTO attendance(event_type_id,date,member_id) VALUES(?,?,?)",
                     (eid, day, cur.lastrowid))
        db().commit()
        audit("member.create", f"{first} {last} (#{cur.lastrowid}) via attendance quick-add")
        flash(f"{first} {last} added as a Visitor and marked present. "
              "Fill in their details from the Members page when you can.")
    else:
        flash("First and last name are required.")
    return redirect(url_for("attendance", event=eid, date=day))


@app.route("/attendance/sheet")
@require("attendance")
def attendance_sheet():
    """Printable record of who attended an event on a date."""
    d = db()
    eid = request.args.get("event", type=int)
    day = parse_date(request.args.get("date"))
    ev = d.execute("SELECT * FROM event_types WHERE id=?", (eid,)).fetchone()
    if not (ev and day):
        abort(404)
    rows = d.execute("""SELECT m.first_name, m.last_name, m.status FROM attendance a
        JOIN members m ON m.id=a.member_id
        WHERE a.event_type_id=? AND a.date=?
        ORDER BY m.last_name, m.first_name""", (eid, day)).fetchall()
    return render_template("attendance_sheet.html", ev=ev, day=day, rows=rows,
                           city=get_setting("city"),
                           printed_by=session["display_name"],
                           printed_at=datetime.now().strftime("%Y-%m-%d %H:%M"))


@app.route("/reports")
@require("reports")
def reports():
    d = db()
    events = d.execute("SELECT * FROM event_types ORDER BY name").fetchall()
    eid = request.args.get("event", type=int) or (events[0]["id"] if events else None)
    weeks = request.args.get("weeks", 4, type=int)

    trend = []
    if eid:
        trend = d.execute("""SELECT date, COUNT(*) c FROM attendance
            WHERE event_type_id=? GROUP BY date ORDER BY date DESC LIMIT 13""",
            (eid,)).fetchall()[::-1]
    peak = max([t["c"] for t in trend], default=1)

    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
    lapsed = d.execute("""
        SELECT m.id, m.first_name, m.last_name, m.phone, m.email, MAX(a.date) last_seen
        FROM members m LEFT JOIN attendance a ON a.member_id = m.id
        WHERE m.status='Active'
        GROUP BY m.id HAVING last_seen IS NULL OR last_seen < ?
        ORDER BY last_seen""", (cutoff,)).fetchall()

    return render_template("reports.html", events=events, eid=eid, trend=trend,
                           peak=peak, weeks=weeks, lapsed=lapsed)


# ================================================================ GIVING

@app.route("/giving", methods=["GET", "POST"])
@require("giving")
def giving():
    d = db()
    if request.method == "POST":
        mid = request.form.get("member_id", type=int)
        fid = request.form.get("fund_id", type=int)
        day = parse_date(request.form.get("date")) or date.today().isoformat()
        try:
            amount = round(float(request.form.get("amount", "0")) * 100)
        except ValueError:
            amount = 0
        method = request.form.get("method", "").strip()
        note = request.form.get("note", "").strip()
        if mid and fid and amount > 0:
            d.execute("""INSERT INTO gifts(member_id,fund_id,date,amount_cents,method,note,recorded_by)
                         VALUES(?,?,?,?,?,?,?)""",
                      (mid, fid, day, amount, method, note, session["display_name"]))
            d.commit()
            audit("gift.add", f"member #{mid} {money(amount)} on {day}")
            flash(f"Recorded {money(amount)}.")
        else:
            flash("Giver, fund, and an amount above zero are required.")
        return redirect(url_for("giving", year=day[:4]))

    year = request.args.get("year", str(date.today().year))
    giver = request.args.get("giver", type=int)
    where = "substr(g.date,1,4)=?"
    args = [year]
    if giver:
        where += " AND g.member_id=?"
        args.append(giver)
    rows = d.execute(f"""SELECT g.*, m.first_name, m.last_name, f.name fund
        FROM gifts g JOIN members m ON m.id=g.member_id JOIN funds f ON f.id=g.fund_id
        WHERE {where} ORDER BY g.date DESC, g.id DESC LIMIT 300""", args).fetchall()
    count = d.execute(f"SELECT COUNT(*) c FROM gifts g WHERE {where}", args).fetchone()["c"]
    total = d.execute(f"SELECT COALESCE(SUM(g.amount_cents),0) t FROM gifts g WHERE {where}",
                      args).fetchone()["t"]
    years = [r["y"] for r in d.execute(
        "SELECT DISTINCT substr(date,1,4) y FROM gifts ORDER BY y DESC").fetchall()] or [year]
    members_all = d.execute("SELECT id, first_name, last_name FROM members "
                            "ORDER BY last_name, first_name").fetchall()
    funds = d.execute("SELECT * FROM funds ORDER BY name").fetchall()
    return render_template("giving.html", rows=rows, total=total, year=year,
                           years=years, members_all=members_all, funds=funds,
                           count=count, giver=giver)


@app.route("/gifts/<int:gid>/delete", methods=["POST"])
@require("giving")
def gift_delete(gid):
    g_ = db().execute("SELECT * FROM gifts WHERE id=?", (gid,)).fetchone()
    if g_:
        db().execute("DELETE FROM gifts WHERE id=?", (gid,))
        db().commit()
        audit("gift.delete", f"gift #{gid}: member #{g_['member_id']} "
              f"{money(g_['amount_cents'])} on {g_['date']}")
    return redirect(request.referrer or url_for("giving"))


@app.route("/giving/statement/<int:mid>")
@require("giving")
def statement(mid):
    d = db()
    m = d.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if not m:
        abort(404)
    year = request.args.get("year", str(date.today().year))
    gifts = d.execute("""SELECT g.*, f.name fund FROM gifts g JOIN funds f ON f.id=g.fund_id
        WHERE g.member_id=? AND substr(g.date,1,4)=? ORDER BY g.date""", (mid, year)).fetchall()
    total = sum(x["amount_cents"] for x in gifts)
    by_fund = d.execute("""SELECT f.name, SUM(g.amount_cents) t FROM gifts g
        JOIN funds f ON f.id=g.fund_id WHERE g.member_id=? AND substr(g.date,1,4)=?
        GROUP BY f.name ORDER BY f.name""", (mid, year)).fetchall()
    audit("gift.statement", f"member #{mid} year {year}")
    return render_template("statement.html", m=m, gifts=gifts, total=total,
                           by_fund=by_fund, year=year,
                           city=get_setting("city"))


# ================================================================ GROUPS

@app.route("/groups", methods=["GET", "POST"])
@require("members.view")
def groups():
    d = db()
    if request.method == "POST":
        if "groups.edit" not in current_perms():
            abort(403)
        name = request.form.get("name", "").strip()
        desc = request.form.get("description", "").strip()
        if name:
            try:
                d.execute("INSERT INTO grp(name,description) VALUES(?,?)", (name, desc))
                d.commit()
                audit("group.create", name)
            except sqlite3.IntegrityError:
                flash("A group with that name already exists.")
        return redirect(url_for("groups"))
    rows = d.execute("""SELECT g.*, COUNT(gm.member_id) n FROM grp g
        LEFT JOIN group_members gm ON gm.group_id=g.id
        GROUP BY g.id ORDER BY g.name""").fetchall()
    return render_template("groups.html", rows=rows)


@app.route("/groups/<int:gid>", methods=["GET", "POST"])
@require("members.view")
def group_view(gid):
    d = db()
    grp = d.execute("SELECT * FROM grp WHERE id=?", (gid,)).fetchone()
    if not grp:
        abort(404)
    if request.method == "POST":
        if "groups.edit" not in current_perms():
            abort(403)
        action = request.form.get("action")
        if action == "add":
            mid = request.form.get("member_id", type=int)
            if mid:
                d.execute("INSERT OR IGNORE INTO group_members(group_id,member_id) VALUES(?,?)", (gid, mid))
        elif action == "remove":
            d.execute("DELETE FROM group_members WHERE group_id=? AND member_id=?",
                      (gid, request.form.get("member_id", type=int)))
        elif action == "delete_group":
            d.execute("DELETE FROM grp WHERE id=?", (gid,))
            d.commit()
            audit("group.delete", grp["name"])
            return redirect(url_for("groups"))
        d.commit()
        return redirect(url_for("group_view", gid=gid))
    membs = d.execute("""SELECT m.* FROM members m JOIN group_members gm ON gm.member_id=m.id
        WHERE gm.group_id=? ORDER BY m.last_name, m.first_name""", (gid,)).fetchall()
    others = d.execute("""SELECT id, first_name, last_name FROM members
        WHERE id NOT IN (SELECT member_id FROM group_members WHERE group_id=?)
        ORDER BY last_name, first_name""", (gid,)).fetchall()
    emails = ", ".join(m["email"] for m in membs if m["email"])
    return render_template("group_view.html", grp=grp, membs=membs, others=others, emails=emails)


# ================================================================ ADMIN

def other_active_admins(uid):
    return db().execute(
        "SELECT COUNT(*) c FROM users WHERE role='Admin' AND active=1 AND id != ?",
        (uid,)).fetchone()["c"]


@app.route("/admin", methods=["GET", "POST"])
@require("admin")
def admin():
    d = db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "church":
            set_setting("church_name", request.form.get("church_name", "").strip() or "One Church")
            set_setting("city", request.form.get("city", "").strip())
            audit("settings.edit")
            flash("Church details saved.")
        elif action == "giving_roles":
            chosen = [r for r in LEADERSHIP_ROLES
                      if request.form.get(f"g_{r}") and r != "Admin"]
            set_setting("giving_roles", ",".join(["Admin"] + chosen))
            audit("settings.giving_roles", ", ".join(["Admin"] + chosen))
            flash("Giving access updated.")
        elif action == "event_add":
            name = request.form.get("name", "").strip()
            if name:
                d.execute("INSERT OR IGNORE INTO event_types(name) VALUES(?)", (name,))
                d.commit()
                audit("event_type.add", name)
        elif action == "event_toggle":
            eid = request.form.get("id", type=int)
            d.execute("UPDATE event_types SET active = 1-active WHERE id=?", (eid,))
            d.commit()
        elif action == "fund_add":
            name = request.form.get("name", "").strip()
            if name:
                d.execute("INSERT OR IGNORE INTO funds(name) VALUES(?)", (name,))
                d.commit()
                audit("fund.add", name)
        elif action == "role_add":
            name = request.form.get("name", "").strip()
            if name:
                d.execute("INSERT OR IGNORE INTO custom_roles(name) VALUES(?)", (name,))
                d.commit()
                audit("custom_role.add", name)
        elif action == "role_delete":
            rid = request.form.get("id", type=int)
            d.execute("DELETE FROM custom_roles WHERE id=?", (rid,))
            d.commit()
            audit("custom_role.delete", f"#{rid}")
        elif action == "user_add" and "users" in current_perms():
            u = request.form.get("username", "").strip().lower()
            p = request.form.get("password", "")
            name = request.form.get("display_name", "").strip()
            role = request.form.get("role", "")
            if u and name and role in LOGIN_ROLES and len(p) >= 8:
                try:
                    d.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
                              (u, generate_password_hash(p), name, role))
                    d.commit()
                    audit("user.add", f"{u} ({role})")
                    flash(f"Login created for {name}. Ask them to change the password "
                          "from their Account page after first sign-in.")
                except sqlite3.IntegrityError:
                    flash("That username is taken.")
            else:
                flash("Username, name, role, and an 8+ character password are required.")
        elif action == "user_toggle" and "users" in current_perms():
            uid = request.form.get("id", type=int)
            target = d.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not target or uid == session["user_id"]:
                pass
            elif target["active"] and target["role"] == "Admin" and other_active_admins(uid) == 0:
                flash("That's the only active Admin login. Make another Admin first.")
            else:
                d.execute("UPDATE users SET active = 1-active WHERE id=?", (uid,))
                d.commit()
                audit("user.toggle", f"{target['username']} (#{uid})")
        elif action == "user_edit" and "users" in current_perms():
            uid = request.form.get("id", type=int)
            target = d.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            newu = request.form.get("username", "").strip().lower()
            newname = request.form.get("display_name", "").strip()
            newrole = request.form.get("role", "")
            if not target or not newu or not newname or newrole not in LOGIN_ROLES:
                flash("Username, name, and a valid role are required.")
            elif uid == session["user_id"] and newrole != target["role"]:
                flash("You can't change your own role. Another Admin has to do that.")
            elif (target["role"] == "Admin" and newrole != "Admin"
                  and target["active"] and other_active_admins(uid) == 0):
                flash("That's the only active Admin login. Make another Admin first.")
            else:
                try:
                    d.execute("UPDATE users SET username=?, display_name=?, role=? WHERE id=?",
                              (newu, newname, newrole, uid))
                    d.commit()
                    audit("user.edit", f"#{uid}: {target['username']}/{target['display_name']}/"
                          f"{target['role']} -> {newu}/{newname}/{newrole}")
                    if uid == session["user_id"]:
                        session["display_name"] = newname
                    flash(f"Login updated for {newname}.")
                except sqlite3.IntegrityError:
                    flash("That username is taken.")
        elif action == "user_delete" and "users" in current_perms():
            uid = request.form.get("id", type=int)
            target = d.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not target:
                pass
            elif uid == session["user_id"]:
                flash("You can't delete your own login while signed in with it.")
            elif (target["role"] == "Admin" and target["active"]
                  and other_active_admins(uid) == 0):
                flash("That's the only active Admin login. Make another Admin first.")
            else:
                d.execute("DELETE FROM users WHERE id=?", (uid,))
                d.commit()
                audit("user.delete", f"{target['username']} ({target['role']}, #{uid})")
                flash(f"Login '{target['username']}' deleted. "
                      "Their past actions stay in the audit log.")
        elif action == "user_password" and "users" in current_perms():
            uid = request.form.get("id", type=int)
            p = request.form.get("password", "")
            if len(p) >= 8:
                d.execute("UPDATE users SET password_hash=?, session_epoch=session_epoch+1 "
                          "WHERE id=?", (generate_password_hash(p), uid))
                d.commit()
                if uid == session["user_id"]:
                    session["epoch"] = d.execute(
                        "SELECT session_epoch FROM users WHERE id=?", (uid,)).fetchone()[0]
                audit("user.password_reset", f"user #{uid} (their devices signed out)")
                flash("Password updated. Any device signed in with the old password is now signed out.")
            else:
                flash("Password must be at least 8 characters.")
        elif action == "backup":
            flash(f"Backup saved: {make_backup()}")
        return redirect(url_for("admin"))

    users = d.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    events = d.execute("SELECT * FROM event_types ORDER BY name").fetchall()
    funds = d.execute("SELECT * FROM funds ORDER BY name").fetchall()
    roles = d.execute("""SELECT cr.*, COUNT(mr.member_id) n FROM custom_roles cr
        LEFT JOIN member_roles mr ON mr.role_id=cr.id GROUP BY cr.id ORDER BY cr.name""").fetchall()
    backups = sorted(os.listdir(BACKUP_DIR), reverse=True)[:10]
    giving_roles = get_setting("giving_roles", DEFAULT_GIVING_ROLES).split(",")
    return render_template("admin.html", users=users, events=events, funds=funds,
                           roles=roles, backups=backups,
                           leadership_roles=LEADERSHIP_ROLES,
                           login_roles=LOGIN_ROLES,
                           giving_roles=giving_roles,
                           city=get_setting("city"))


@app.route("/audit")
@require("audit")
def audit_log():
    rows = db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500").fetchall()
    return render_template("audit.html", rows=rows)


# ----- CSV import / export

@app.route("/export/members.csv")
@require("data")
def export_members():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(MEMBER_FIELDS + ["custom_roles"])
    for m in db().execute("SELECT * FROM members ORDER BY last_name, first_name").fetchall():
        roles = [r["name"] for r in db().execute(
            """SELECT cr.name FROM custom_roles cr JOIN member_roles mr
               ON mr.role_id=cr.id WHERE mr.member_id=?""", (m["id"],)).fetchall()]
        w.writerow([m[f] for f in MEMBER_FIELDS] + ["; ".join(roles)])
    audit("export.members")
    return send_file(io.BytesIO(out.getvalue().encode()), mimetype="text/csv",
                     as_attachment=True, download_name="members.csv")


def parse_us_date(s):
    """Accepts MM/DD/YYYY (SimpleChurch style) or YYYY-MM-DD. Returns ISO or ''."""
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def convert_simplechurch(reader):
    """Translate SimpleChurch export rows into our member fields, keeping the
    family and group information so the importer can link households and
    recreate groups. Returns a list of dicts."""
    out = []
    for row in reader:
        r = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        first = r.get("Preferred Name") or r.get("First Name", "")
        last = r.get("Last Name", "")
        if not (first and last):
            continue
        if r.get("Died On"):
            status = "Deceased"
        elif r.get("Active", "").lower() in ("no", "false", "0"):
            status = "Inactive"
        else:
            status = "Active"
        addr_bits = [r.get("Address", ""), r.get("City", ""),
                     " ".join(x for x in (r.get("State", ""), r.get("Zip Code", "")) if x)]
        out.append({
            "first_name": first,
            "last_name": last,
            "status": status,
            "leadership_role": "",
            "birthdate": parse_us_date(r.get("Birthday")),
            "membership_date": parse_us_date(r.get("Membership")),
            "is_member": 1 if (parse_us_date(r.get("Membership"))
                               or r.get("Membership", "").strip().lower() in ("member", "yes", "active")) else 0,
            "baptism_date": parse_us_date(r.get("Baptism Date")),
            "anniversary": parse_us_date(r.get("Anniversary")),
            "phone": r.get("Cell Phone") or r.get("Home Phone") or r.get("Work Phone", ""),
            "email": r.get("Email", ""),
            "address": ", ".join(b for b in addr_bits if b),
            "custom_roles": "",
            "family_id": r.get("Family ID", ""),
            "family_rel": r.get("Family Relationship", ""),
            "groups": [g.strip() for g in r.get("Active Groups", "").split(",") if g.strip()],
            "note": r.get("Notes", ""),
        })
    return out


def rows_generic(reader):
    """Normalize rows from our own export format (or anything close to it)."""
    out = []
    for row in reader:
        r = {(k or "").strip().lower().replace(" ", "_"): (v or "").strip()
             for k, v in row.items()}
        if not (r.get("first_name") and r.get("last_name")):
            continue
        out.append({
            "first_name": r["first_name"],
            "last_name": r["last_name"],
            "status": r.get("status") if r.get("status") in STATUSES else "Active",
            "leadership_role": r.get("leadership_role", "") if r.get("leadership_role", "") in LEADERSHIP_ROLES else "",
            "birthdate": parse_date(r.get("birthdate")),
            "membership_date": parse_date(r.get("membership_date")),
            "is_member": 1 if (r.get("is_member", "").lower() in ("1", "yes", "true", "y")
                               or parse_date(r.get("membership_date"))) else 0,
            "baptism_date": parse_date(r.get("baptism_date")),
            "anniversary": parse_date(r.get("anniversary")),
            "phone": r.get("phone", ""), "email": r.get("email", ""),
            "address": r.get("address", ""),
            "custom_roles": r.get("custom_roles", ""),
            "family_id": "", "family_rel": "", "groups": [], "note": "",
        })
    return out


def _link(a, b, relation):
    """Create relationship a->b plus its inverse, skipping duplicates."""
    db().execute("INSERT OR IGNORE INTO relationships(member_id,related_id,relation) VALUES(?,?,?)",
                 (a, b, relation))
    db().execute("INSERT OR IGNORE INTO relationships(member_id,related_id,relation) VALUES(?,?,?)",
                 (b, a, INVERSE[relation]))


@app.route("/import", methods=["GET", "POST"])
@require("data")
def import_members():
    report = None
    if request.method == "POST":
        f = request.files.get("file")
        allow_dupes = bool(request.form.get("allow_duplicates"))
        if not f or not f.filename:
            flash("Choose a CSV file first.")
        else:
            try:
                text = f.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text))
                headers = set(reader.fieldnames or [])
                is_sc = {"User ID", "Family ID", "Family Relationship"} <= headers
                rows = convert_simplechurch(reader) if is_sc else rows_generic(reader)

                lookup = {(m["first_name"].lower(), m["last_name"].lower()): m["id"]
                          for m in db().execute("SELECT id,first_name,last_name FROM members").fetchall()}
                added, matched = 0, 0
                families = {}   # family_id -> {"parents": [mids], "children": [mids]}
                now = datetime.now().isoformat(timespec="seconds")

                for r in rows:
                    key = (r["first_name"].lower(), r["last_name"].lower())
                    if key in lookup and not allow_dupes:
                        mid = lookup[key]   # existing person: reuse for links/groups
                        matched += 1
                    else:
                        cur = db().execute(
                            """INSERT INTO members(first_name,last_name,status,is_member,leadership_role,birthdate,
                               membership_date,baptism_date,anniversary,phone,email,address,
                               created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (r["first_name"], r["last_name"], r["status"], r["is_member"], r["leadership_role"],
                             r["birthdate"], r["membership_date"], r["baptism_date"], r["anniversary"],
                             r["phone"], r["email"], r["address"], now, now))
                        mid = cur.lastrowid
                        lookup[key] = mid
                        added += 1
                        if r["note"]:
                            db().execute(
                                "INSERT INTO notes(member_id,author,visibility,body,created_at) "
                                "VALUES(?,?,?,?,?)",
                                (mid, "SimpleChurch import", "leaders", r["note"],
                                 datetime.now().strftime("%Y-%m-%d %H:%M")))

                    for rname in re.split(r"[;,]", r["custom_roles"]):
                        rname = rname.strip()
                        if rname:
                            db().execute("INSERT OR IGNORE INTO custom_roles(name) VALUES(?)", (rname,))
                            rid = db().execute("SELECT id FROM custom_roles WHERE name=?",
                                               (rname,)).fetchone()["id"]
                            db().execute("INSERT OR IGNORE INTO member_roles(member_id,role_id) VALUES(?,?)",
                                         (mid, rid))

                    for gname in r["groups"]:
                        db().execute("INSERT OR IGNORE INTO grp(name,description) VALUES(?, 'From SimpleChurch')",
                                     (gname,))
                        gid = db().execute("SELECT id FROM grp WHERE name=?", (gname,)).fetchone()["id"]
                        db().execute("INSERT OR IGNORE INTO group_members(group_id,member_id) VALUES(?,?)",
                                     (gid, mid))

                    if r["family_id"]:
                        fam = families.setdefault(r["family_id"], {"parents": [], "children": []})
                        rel = r["family_rel"].lower()
                        if rel in ("primary", "spouse"):
                            fam["parents"].append(mid)
                        elif rel == "child":
                            fam["children"].append(mid)

                links = 0
                for fam in families.values():
                    p = fam["parents"]
                    if len(p) == 2:
                        _link(p[0], p[1], "Spouse")
                        links += 1
                    for child in fam["children"]:
                        for parent in p:
                            _link(child, parent, "Parent")
                            links += 1

                db().commit()
                fmt = "SimpleChurch" if is_sc else "generic CSV"
                audit("import.members", f"{fmt}: {added} added, {matched} matched, {links} family links")
                report = (f"Recognized a {fmt} file. Added {added} new members, matched {matched} "
                          f"existing ones, and created {links} family links"
                          + (f" plus their groups." if is_sc else "."))
            except Exception as e:
                db().rollback()
                flash(f"Import failed: {e}")
    return render_template("import.html", report=report, fields=MEMBER_FIELDS)


# ================================================================ YOUTH

ATT_LOCK = threading.Lock()   # makes per-checkbox check-in saves atomic


def make_backup(reason=""):
    """Copy the live database into backups/. Returns the filename."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"church-{stamp}.db")
    src = sqlite3.connect(DB_PATH)
    out = sqlite3.connect(dest)
    src.backup(out)
    out.close(); src.close()
    audit("backup", dest + (f" ({reason})" if reason else ""))
    return os.path.basename(dest)


def grade_sort(g):
    return 0 if g == "K" else (99 if g == "?" else int(g))


@app.template_filter("gradelabel")
def grade_label(g):
    return "Kindergarten" if g == "K" else ("Grade not set" if g == "?" else f"Grade {g}")


def ministry_list():
    return db().execute("SELECT * FROM ministries ORDER BY grade_from='K' DESC, "
                        "CAST(grade_from AS INTEGER), name").fetchall()


def grades_for(ministry):
    """The grades covered by a ministry row, in order."""
    order = ["K"] + [str(i) for i in range(1, 13)]
    a, b = order.index(ministry["grade_from"]), order.index(ministry["grade_to"])
    return set(order[min(a, b):max(a, b) + 1])


def youth_roster(include_archived=False, ministry=None):
    """Students with their member record, ordered by grade then name.
    Students with grade '?' show up in every ministry until sorted."""
    rows = db().execute("""SELECT m.*, yp.grade, yp.photo_consent, yp.allergies,
        yp.medical, yp.emergency_name, yp.emergency_phone, yp.graduated, yp.archived
        FROM youth_profiles yp JOIN members m ON m.id = yp.member_id""").fetchall()
    rows = [r for r in rows if include_archived or not r["archived"]]
    if ministry is not None:
        keep = grades_for(ministry)
        rows = [r for r in rows if r["grade"] in keep or r["grade"] == "?"]
    return sorted(rows, key=lambda r: (grade_sort(r["grade"]), r["last_name"], r["first_name"]))


def youth_visits(mid):
    """Gatherings this student has been marked present at, across youth
    events and regular attendance."""
    a = db().execute("SELECT COUNT(*) c FROM youth_attendance WHERE member_id=?", (mid,)).fetchone()["c"]
    b = db().execute("SELECT COUNT(*) c FROM attendance WHERE member_id=?", (mid,)).fetchone()["c"]
    return a + b


def school_year(d=None):
    """School years turn over Aug 1: July 2026 is still the 2025 year."""
    d = d or date.today()
    return d.year if d.month >= 8 else d.year - 1


@app.route("/youth/dashboard")
@require("youth")
def youth_dashboard():
    d = db()
    session["mode"] = "youth"
    roster = youth_roster()
    ministries = ministry_list()
    counts = [(m, sum(1 for r in roster if r["grade"] in grades_for(m) or r["grade"] == "?"))
              for m in ministries]

    # Baptism follow-up: students with a baptism conversation noted but no
    # baptism date on the member record yet.
    convo_ids = {n["member_id"] for n in d.execute(
        "SELECT DISTINCT member_id FROM notes WHERE kind='baptism'").fetchall()}
    baptism_watch = [r for r in roster if r["id"] in convo_ids and not r["baptism_date"]]

    # Visitors seen recently: warm, worth a follow-up.
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    recent_ids = {r["member_id"] for r in d.execute("""SELECT DISTINCT ya.member_id
        FROM youth_attendance ya JOIN youth_events e ON e.id=ya.event_id
        WHERE e.date >= ?""", (cutoff,)).fetchall()}
    recent_ids |= {r["member_id"] for r in d.execute(
        "SELECT DISTINCT member_id FROM attendance WHERE date >= ?", (cutoff,)).fetchall()}
    visitor_watch = [r for r in roster if r["status"] == "Visitor" and r["id"] in recent_ids]

    upcoming_events = d.execute("""SELECT e.*, m.name ministry FROM youth_events e
        LEFT JOIN ministries m ON m.id=e.ministry_id
        WHERE e.date >= ? ORDER BY e.date LIMIT 8""",
        (date.today().isoformat(),)).fetchall()

    last_checkins = d.execute("""SELECT e.name, e.date, COUNT(ya.member_id) c
        FROM youth_events e JOIN youth_attendance ya ON ya.event_id=e.id
        WHERE e.date <= ? GROUP BY e.id ORDER BY e.date DESC LIMIT 5""",
        (date.today().isoformat(),)).fetchall()

    bdays = []
    for r in roster:
        i = upcoming(r["birthdate"][5:] if r["birthdate"] else "")
        if i is not None:
            bdays.append((i, r))
    bdays = [r for _, r in sorted(bdays, key=lambda x: x[0])]

    return render_template("youth_dashboard.html", n_students=len(roster),
                           counts=counts, baptism_watch=baptism_watch,
                           visitor_watch=visitor_watch, upcoming_events=upcoming_events,
                           last_checkins=last_checkins, bdays=bdays)


@app.route("/youth", methods=["GET", "POST"])
@require("youth")
def youth():
    d = db()
    session["mode"] = "youth"
    if request.method == "POST" and request.form.get("action") == "attach":
        mid = request.form.get("member_id", type=int)
        if mid and d.execute("SELECT 1 FROM members WHERE id=?", (mid,)).fetchone():
            d.execute("INSERT OR IGNORE INTO youth_profiles(member_id) VALUES(?)", (mid,))
            d.commit()
            audit("youth.attach", f"member #{mid}")
            return redirect(url_for("youth_edit", mid=mid))
        return redirect(url_for("youth"))

    q = request.args.get("q", "").strip().lower()
    show_archived = request.args.get("archived") == "1"
    ministries = ministry_list()
    min_id = request.args.get("ministry", type=int)
    ministry = next((m for m in ministries if m["id"] == min_id), None)
    rows = youth_roster(include_archived=show_archived, ministry=ministry)

    if q:
        matched = []
        for r in rows:
            name = f"{r['first_name']} {r['last_name']}".lower()
            gnames = " ".join(g["name"].lower() for g in d.execute(
                "SELECT name FROM guardians WHERE member_id=?", (r["id"],)).fetchall())
            if q in name or q in gnames:
                matched.append(r)
        rows = matched

    convo_ids = {n["member_id"] for n in d.execute(
        "SELECT DISTINCT member_id FROM notes WHERE kind='baptism'").fetchall()}

    by_grade = []
    for r in rows:
        if by_grade and by_grade[-1][0] == r["grade"]:
            by_grade[-1][1].append(r)
        else:
            by_grade.append((r["grade"], [r]))

    archived_count = d.execute(
        "SELECT COUNT(*) c FROM youth_profiles WHERE archived=1").fetchone()["c"]
    seniors = d.execute(
        "SELECT COUNT(*) c FROM youth_profiles WHERE grade='12' AND archived=0").fetchone()["c"]
    moving = d.execute(
        "SELECT COUNT(*) c FROM youth_profiles WHERE archived=0 AND grade!='?'").fetchone()["c"]

    phones = []
    scope = youth_roster(ministry=ministry)
    if scope:
        marks = ",".join("?" * len(scope))
        for g_ in d.execute(f"SELECT DISTINCT phone FROM guardians WHERE phone != '' "
                            f"AND member_id IN ({marks}) ORDER BY phone",
                            [r["id"] for r in scope]).fetchall():
            phones.append(g_["phone"])

    unattached = d.execute("""SELECT id, first_name, last_name FROM members
        WHERE id NOT IN (SELECT member_id FROM youth_profiles)
        ORDER BY last_name, first_name""").fetchall()

    promoted_this_year = get_setting("last_promotion_year") == str(school_year())

    return render_template("youth.html", by_grade=by_grade, q=q,
                           show_archived=show_archived, archived_count=archived_count,
                           convo_ids=convo_ids, phones=phones, unattached=unattached,
                           seniors=seniors, moving=moving, ministries=ministries,
                           ministry=ministry, promoted_this_year=promoted_this_year)


@app.route("/youth/ministries", methods=["POST"])
@require("youth")
def youth_ministries():
    d = db()
    action = request.form.get("action")
    if action == "add":
        name = request.form.get("name", "").strip()
        gf = request.form.get("grade_from") if request.form.get("grade_from") in GRADES else "K"
        gt = request.form.get("grade_to") if request.form.get("grade_to") in GRADES else "12"
        if name:
            try:
                d.execute("INSERT INTO ministries(name,grade_from,grade_to) VALUES(?,?,?)",
                          (name, gf, gt))
                d.commit()
                audit("ministry.add", f"{name} ({gf}-{gt})")
            except sqlite3.IntegrityError:
                flash("A ministry with that name already exists.")
        else:
            flash("The ministry needs a name.")
    elif action == "delete":
        mid = request.form.get("id", type=int)
        m = d.execute("SELECT * FROM ministries WHERE id=?", (mid,)).fetchone()
        if m:
            d.execute("DELETE FROM ministries WHERE id=?", (mid,))
            d.commit()
            audit("ministry.delete", m["name"])
            flash(f"{m['name']} removed. Students and events are untouched; "
                  "events that pointed at it just lost the label.")
    return redirect(url_for("youth"))


def youth_form_save(mid=None):
    """Create or update a student: member record + youth profile + guardians.
    Returns member id on success, None on validation error."""
    f = request.form
    first = f.get("first_name", "").strip()
    last = f.get("last_name", "").strip()
    if not (first and last):
        flash("First and last name are required.")
        return None
    grade = f.get("grade") if f.get("grade") in GRADES + ["?"] else "?"
    bd = parse_date(f.get("birthdate"))
    consent = f.get("photo_consent") if f.get("photo_consent") in ("yes", "no") else ""
    now = datetime.now().isoformat(timespec="seconds")

    d = db()
    if mid is None:
        status = "Visitor" if f.get("visitor") else "Active"
        cur = d.execute(
            "INSERT INTO members(first_name,last_name,status,birthdate,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)", (first, last, status, bd, now, now))
        mid = cur.lastrowid
        audit("youth.create", f"{first} {last} (#{mid})")
    else:
        # The visitor checkbox only toggles between Visitor and Active.
        # Any other status (Inactive, Moved, Deceased) is left alone -
        # change those from the member edit page, on purpose.
        old = d.execute("SELECT status FROM members WHERE id=?", (mid,)).fetchone()["status"]
        if f.get("visitor"):
            status = "Visitor"
        elif old == "Visitor":
            status = "Active"
        else:
            status = old
        d.execute("UPDATE members SET first_name=?,last_name=?,status=?,birthdate=?,updated_at=? "
                  "WHERE id=?", (first, last, status, bd, now, mid))
        audit("youth.edit", f"{first} {last} (#{mid})")

    d.execute("""INSERT INTO youth_profiles(member_id,grade,photo_consent,allergies,medical,
                 emergency_name,emergency_phone) VALUES(?,?,?,?,?,?,?)
                 ON CONFLICT(member_id) DO UPDATE SET grade=excluded.grade,
                 photo_consent=excluded.photo_consent, allergies=excluded.allergies,
                 medical=excluded.medical, emergency_name=excluded.emergency_name,
                 emergency_phone=excluded.emergency_phone""",
              (mid, grade, consent, f.get("allergies", "").strip(), f.get("medical", "").strip(),
               f.get("emergency_name", "").strip(), f.get("emergency_phone", "").strip()))

    d.execute("DELETE FROM guardians WHERE member_id=?", (mid,))
    for name, rel, phone in zip(f.getlist("g_name"), f.getlist("g_relation"), f.getlist("g_phone")):
        if name.strip():
            d.execute("INSERT INTO guardians(member_id,name,relation,phone) VALUES(?,?,?,?)",
                      (mid, name.strip(), rel.strip(), phone.strip()))
    d.commit()
    return mid


@app.route("/youth/new", methods=["GET", "POST"])
@require("youth")
def youth_new():
    if request.method == "POST":
        mid = youth_form_save()
        if mid:
            return redirect(url_for("member_view", mid=mid))
    return render_template("youth_form.html", m=None, yp=None, gdns=[],
                           v=request.form, grades=GRADES)


@app.route("/youth/<int:mid>/edit", methods=["GET", "POST"])
@require("youth")
def youth_edit(mid):
    d = db()
    m = d.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    yp = d.execute("SELECT * FROM youth_profiles WHERE member_id=?", (mid,)).fetchone()
    if not (m and yp):
        abort(404)
    if request.method == "POST":
        if youth_form_save(mid):
            return redirect(url_for("member_view", mid=mid))
        return render_template("youth_form.html", m=m, yp=yp, gdns=[],
                               v=request.form, grades=GRADES)
    gdns = d.execute("SELECT * FROM guardians WHERE member_id=? ORDER BY id", (mid,)).fetchall()
    return render_template("youth_form.html", m=m, yp=yp, gdns=gdns, v={}, grades=GRADES)


@app.route("/youth/<int:mid>/archive", methods=["POST"])
@require("youth")
def youth_archive(mid):
    yp = db().execute("SELECT * FROM youth_profiles WHERE member_id=?", (mid,)).fetchone()
    if not yp:
        abort(404)
    db().execute("UPDATE youth_profiles SET archived = 1-archived WHERE member_id=?", (mid,))
    db().commit()
    audit("youth.archive", f"member #{mid} -> {'restored' if yp['archived'] else 'archived'}")
    flash("Restored to the active list." if yp["archived"] else "Archived - history kept.")
    return redirect(request.referrer or url_for("youth"))


@app.route("/youth/promote", methods=["POST"])
@require("youth")
def youth_promote():
    """Promotion Sunday: everyone moves up a grade; seniors graduate.
    A backup is taken automatically first, and running it twice in the
    same school year takes an extra confirmation - the most expensive
    mistake this page can make is a double promotion."""
    if (get_setting("last_promotion_year") == str(school_year())
            and not request.form.get("confirm_again")):
        flash("Promotion Sunday already ran this school year. If you really "
              "mean to run it again, tick the confirmation box first.")
        return redirect(url_for("youth", again=1))

    backup_name = make_backup("before Promotion Sunday")
    d = db()
    grads = d.execute("SELECT member_id FROM youth_profiles WHERE grade='12' AND archived=0").fetchall()
    for r in grads:
        d.execute("UPDATE youth_profiles SET graduated=1, archived=1 WHERE member_id=?",
                  (r["member_id"],))
    # Walk grades from the top down, and move K up LAST - otherwise the
    # fresh first-graders get caught by the 1->2 update and skip a grade.
    for g_ in range(11, 0, -1):
        d.execute("UPDATE youth_profiles SET grade=? WHERE grade=? AND archived=0",
                  (str(g_ + 1), str(g_)))
    d.execute("UPDATE youth_profiles SET grade='1' WHERE grade='K' AND archived=0")
    d.execute("INSERT INTO settings(key,value) VALUES('last_promotion_year',?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(school_year()),))
    d.commit()
    audit("youth.promote", f"{len(grads)} graduated")
    flash(f"Promotion Sunday done: everyone moved up a grade"
          + (f" and {len(grads)} senior{'s' if len(grads) != 1 else ''} graduated (archived, history kept)."
             if grads else ".")
          + f" A backup was saved first ({backup_name}) in case anything looks wrong.")
    return redirect(url_for("youth"))


@app.route("/youth/events", methods=["GET", "POST"])
@require("youth")
def youth_events():
    d = db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        day = parse_date(request.form.get("date"))
        detail = request.form.get("detail", "").strip()
        perm = 1 if request.form.get("permission_required") else 0
        min_id = request.form.get("ministry_id", type=int) or None
        if min_id and not d.execute("SELECT 1 FROM ministries WHERE id=?", (min_id,)).fetchone():
            min_id = None
        repeat = min(request.form.get("repeat_weeks", 0, type=int) or 0, 52)
        if name and day:
            base = datetime.strptime(day, "%Y-%m-%d").date()
            for i in range(0, repeat + 1):
                d.execute("INSERT INTO youth_events(name,date,detail,permission_required,ministry_id) "
                          "VALUES(?,?,?,?,?)",
                          (name, (base + timedelta(weeks=i)).isoformat(), detail, perm, min_id))
            d.commit()
            audit("youth_event.create", f"{name} {day}"
                  + (f" (+{repeat} weekly repeats)" if repeat else ""))
            flash(f"Created {repeat + 1} events." if repeat else "Event created.")
        else:
            flash("Event name and date are required.")
        return redirect(url_for("youth_events"))

    events = d.execute("""SELECT e.*, COUNT(ya.member_id) n, m.name ministry
        FROM youth_events e
        LEFT JOIN youth_attendance ya ON ya.event_id=e.id
        LEFT JOIN ministries m ON m.id=e.ministry_id
        GROUP BY e.id ORDER BY e.date DESC, e.id DESC LIMIT 100""").fetchall()
    return render_template("youth_events.html", events=events, ministries=ministry_list())


@app.route("/youth/events/<int:eid>", methods=["GET", "POST"])
@require("youth")
def youth_event_view(eid):
    d = db()
    ev = d.execute("""SELECT e.*, m.name ministry FROM youth_events e
        LEFT JOIN ministries m ON m.id=e.ministry_id WHERE e.id=?""", (eid,)).fetchone()
    if not ev:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            d.execute("DELETE FROM youth_events WHERE id=?", (eid,))
            d.commit()
            audit("youth_event.delete", f"{ev['name']} {ev['date']}")
            flash("Event and its check-ins were deleted.")
            return redirect(url_for("youth_events"))

        if action == "visitor":
            first = request.form.get("first_name", "").strip()
            last = request.form.get("last_name", "").strip()
            if first and last:
                now = datetime.now().isoformat(timespec="seconds")
                cur = d.execute(
                    "INSERT INTO members(first_name,last_name,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?)", (first, last, "Visitor", now, now))
                d.execute("INSERT INTO youth_profiles(member_id) VALUES(?)", (cur.lastrowid,))
                d.execute("INSERT INTO youth_attendance(event_id,member_id) VALUES(?,?)",
                          (eid, cur.lastrowid))
                d.commit()
                audit("youth.create", f"{first} {last} (#{cur.lastrowid}) via event quick-add")
                flash(f"{first} {last} added as a visitor and marked present. "
                      "Fill in guardians and details from their profile when you can.")
            else:
                flash("First and last name are required.")
            return redirect(url_for("youth_event_view", eid=eid))

    ministry = None
    if ev["ministry_id"]:
        ministry = d.execute("SELECT * FROM ministries WHERE id=?", (ev["ministry_id"],)).fetchone()
    roster = youth_roster(ministry=ministry)
    present = {r["member_id"] for r in d.execute(
        "SELECT member_id FROM youth_attendance WHERE event_id=?", (eid,)).fetchall()}
    slips = {r["member_id"] for r in d.execute(
        "SELECT member_id FROM youth_attendance WHERE event_id=? AND permission=1", (eid,)).fetchall()}
    return render_template("youth_event_view.html", ev=ev, roster=roster,
                           present=present, slips=slips)


@app.route("/youth/events/<int:eid>/toggle", methods=["POST"])
@require("youth")
def youth_event_toggle(eid):
    """Flip one checkbox atomically and save immediately. Two leaders
    checking kids in on two phones can no longer overwrite each other."""
    d = db()
    if not d.execute("SELECT 1 FROM youth_events WHERE id=?", (eid,)).fetchone():
        abort(404)
    b = request.get_json(silent=True) or {}
    mid = b.get("member_id")
    field = b.get("field")
    if (field not in ("present", "permission") or not isinstance(mid, int)
            or not d.execute("SELECT 1 FROM youth_profiles WHERE member_id=?", (mid,)).fetchone()):
        abort(400, "bad request")

    with ATT_LOCK:
        row = d.execute("SELECT * FROM youth_attendance WHERE event_id=? AND member_id=?",
                        (eid, mid)).fetchone()
        if field == "present":
            if row:
                d.execute("DELETE FROM youth_attendance WHERE event_id=? AND member_id=?",
                          (eid, mid))
                now_present, now_slip = False, False
            else:
                d.execute("INSERT INTO youth_attendance(event_id,member_id) VALUES(?,?)",
                          (eid, mid))
                now_present, now_slip = True, False
        else:  # permission slip; being handed a slip implies the kid is here
            if row:
                new_p = 0 if row["permission"] else 1
                d.execute("UPDATE youth_attendance SET permission=? WHERE event_id=? AND member_id=?",
                          (new_p, eid, mid))
                now_present, now_slip = True, bool(new_p)
            else:
                d.execute("INSERT INTO youth_attendance(event_id,member_id,permission) VALUES(?,?,1)",
                          (eid, mid))
                now_present, now_slip = True, True
        d.commit()

    nudge = ""
    m = d.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
    if now_present and m["status"] == "Visitor" and youth_visits(mid) == 2:
        nudge = (f"{m['first_name']} {m['last_name']}'s 2nd visit - "
                 "great time for a follow-up note or a call home.")
    return {"present": now_present, "permission": now_slip, "nudge": nudge}


@app.route("/export/youth.csv")
@require("youth")
def export_youth():
    """Roster export. Allergies, medical needs, and emergency contacts are
    left out unless ?care=1 is asked for - most uses of this file (sign-up
    sheets, mail merges) don't need a spreadsheet of kids' medical details
    floating around."""
    care = request.args.get("care") == "1"
    out = io.StringIO()
    w = csv.writer(out)
    cols = ["first_name", "last_name", "grade", "status", "birthdate", "baptism_date",
            "photo_consent", "guardians", "archived", "graduated"]
    if care:
        cols[7:7] = ["allergies", "medical", "emergency_name", "emergency_phone"]
    w.writerow(cols)
    for r in youth_roster(include_archived=True):
        gdns = "; ".join(f"{g_['name']} ({g_['relation']}) {g_['phone']}".strip()
                         for g_ in db().execute(
                             "SELECT * FROM guardians WHERE member_id=?", (r["id"],)).fetchall())
        row = [r["first_name"], r["last_name"], r["grade"], r["status"], r["birthdate"],
               r["baptism_date"], r["photo_consent"]]
        if care:
            row += [r["allergies"], r["medical"], r["emergency_name"], r["emergency_phone"]]
        row += [gdns, r["archived"], r["graduated"]]
        w.writerow(row)
    audit("export.youth.care" if care else "export.youth")
    return send_file(io.BytesIO(out.getvalue().encode()), mimetype="text/csv",
                     as_attachment=True,
                     download_name="youth-with-care-details.csv" if care else "youth.csv")


# ================================================================

@app.errorhandler(400)
def badrequest(e):
    return render_template("error.html", code=400, message=e.description), 400


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403,
                           message="Your role doesn't have access to that page."), 403


@app.errorhandler(404)
def notfound(e):
    return render_template("error.html", code=404, message="That page doesn't exist."), 404


if __name__ == "__main__":
    try:
        from waitress import serve
        print("Serving with waitress on http://0.0.0.0:8080")
        serve(app, host=os.environ.get("OPENCHURCH_BIND", "0.0.0.0"), port=int(os.environ.get("OPENCHURCH_PORT", 8080)))
    except ImportError:
        print("waitress not installed; using Flask's built-in server.")
        app.run(host=os.environ.get("OPENCHURCH_BIND", "0.0.0.0"), port=int(os.environ.get("OPENCHURCH_PORT", 8080)), debug=False)
