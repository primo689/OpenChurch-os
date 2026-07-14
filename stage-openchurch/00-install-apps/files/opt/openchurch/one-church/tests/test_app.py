#!/usr/bin/env python3
"""Functional walk-through of One Church using Flask's test client.

Run from the one-church folder:  python3 tests/test_app.py
WARNING: this wipes the data/ folder. Never run it on a live install.
"""
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.exists(os.path.join(ROOT, "data", "church.db")) and "--force" not in sys.argv:
    sys.exit("Refusing to run: data/church.db exists and this test wipes it.\n"
             "Move your database somewhere safe first, or pass --force if this\n"
             "really is a throwaway install.")
os.chdir(ROOT)
sys.path.insert(0, ROOT)
shutil.rmtree("data", ignore_errors=True)
os.makedirs("data/photos", exist_ok=True)
os.makedirs("backups", exist_ok=True)

import app as appmod
app = appmod.app
app.config["TESTING"] = True

c = app.test_client()
fails = []

def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        fails.append(name)

def csrf(cl, path):
    r = cl.get(path, follow_redirects=True)
    m = re.search(rb'name="csrf_token" value="([^"]+)"', r.data)
    return m.group(1).decode() if m else ""

def login(cl, user, pw="testpass123"):
    tok = csrf(cl, "/login")
    return cl.post("/login", data=dict(csrf_token=tok, username=user, password=pw),
                   follow_redirects=True)

def q(sql, *args):
    con = appmod.connect()
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows

def x(sql, *args):
    con = appmod.connect()
    con.execute(sql, args)
    con.commit()
    con.close()

# ---- setup
tok = csrf(c, "/setup")
c.post("/setup?step=1", data=dict(csrf_token=tok, church_name="First Christian Church",
       city="Springfield", admin_name="Pat Admin", username="padmin",
       password="testpass123", password2="testpass123"), follow_redirects=True)
x("INSERT INTO settings(key,value) VALUES('onboarded','1') "
  "ON CONFLICT(key) DO UPDATE SET value='1'")
r = login(c, "padmin")
check("admin logs in", b"Sign out" in r.data or r.status_code == 200)

# ---- mode dispatch: admin defaults to church dashboard
r = c.get("/", follow_redirects=True)
check("admin lands on church dashboard", b"/dashboard" in r.request.path.encode() or b"Active members" in r.data)
check("mode toggle rendered", b"mode-toggle" in r.data)

# ---- mode toggle to youth and back
r = c.get("/mode/youth", follow_redirects=True)
check("youth mode -> youth dashboard", b"Youth dashboard" in r.data)
check("youth-mode nav shows Students", b"Students" in r.data)
r = c.get("/mode/church", follow_redirects=True)
check("church mode back", b"Active members" in r.data or b"Dashboard" in r.data)

# ---- is_member: create member, toggle membership
tok = csrf(c, "/members/new")
c.post("/members/new", data=dict(csrf_token=tok, first_name="Alice", last_name="Smith",
       status="Active"), follow_redirects=True)
aid = q("SELECT id FROM members WHERE first_name='Alice'")[0]["id"]
tok = csrf(c, f"/members/{aid}")
r = c.post(f"/members/{aid}/membership", data=dict(csrf_token=tok), follow_redirects=True)
check("membership toggle marks member", q("SELECT is_member FROM members WHERE id=?", aid)[0][0] == 1)
check("membership date auto-set", bool(q("SELECT membership_date FROM members WHERE id=?", aid)[0][0]))

# ---- student lifecycle
tok = csrf(c, "/youth/new")
r = c.post("/youth/new", data={
    "csrf_token": tok, "first_name": "Katie", "last_name": "Miller", "grade": "8",
    "birthdate": "2012-03-14", "photo_consent": "yes", "allergies": "peanuts",
    "medical": "", "emergency_name": "Gran Miller", "emergency_phone": "573-555-0100",
    "g_name": ["Sarah Miller"], "g_relation": ["Mom"], "g_phone": ["573-555-0101"]},
    follow_redirects=True)
check("student created", b"Katie Miller" in r.data)
check("guardian shown", b"Sarah Miller" in r.data)
kid = q("SELECT id FROM members WHERE first_name='Katie'")[0]["id"]

# ---- ministries
tok = csrf(c, "/youth")
c.post("/youth/ministries", data=dict(csrf_token=tok, action="add", name="Kids",
       grade_from="K", grade_to="5"), follow_redirects=True)
tok = csrf(c, "/youth")
c.post("/youth/ministries", data=dict(csrf_token=tok, action="add", name="Youth",
       grade_from="6", grade_to="12"), follow_redirects=True)
mids = {m["name"]: m["id"] for m in q("SELECT * FROM ministries")}
check("ministries created", set(mids) == {"Kids", "Youth"})
r = c.get(f"/youth?ministry={mids['Youth']}")
check("Katie (gr 8) in Youth tab", b"Katie" in r.data)
r = c.get(f"/youth?ministry={mids['Kids']}")
check("Katie not in Kids tab", b"Katie" not in r.data)

# ---- baptism note + dashboard watchlist
tok = csrf(c, f"/members/{kid}")
c.post(f"/members/{kid}/notes", data=dict(csrf_token=tok, body="Asked about baptism",
       kind="baptism", visibility="leaders"), follow_redirects=True)
r = c.get("/youth/dashboard")
check("baptism watchlist shows Katie", b"Katie" in r.data and b"Baptism follow-up" in r.data)

# ---- youth event scoped to ministry + atomic toggle check-in
tok = csrf(c, "/youth/events")
c.post("/youth/events", data=dict(csrf_token=tok, name="Lock-in", date="2026-07-10",
       detail="", permission_required="on", repeat_weeks="0",
       ministry_id=str(mids["Youth"])), follow_redirects=True)
eid = q("SELECT id FROM youth_events WHERE name='Lock-in'")[0]["id"]

# toggle present via JSON (like the page's JS does)
tok = csrf(c, f"/youth/events/{eid}")
r = c.post(f"/youth/events/{eid}/toggle", data=json.dumps(
    {"member_id": kid, "field": "present", "csrf_token": tok}),
    content_type="application/json")
check("toggle present", r.status_code == 200 and r.get_json()["present"] is True)
r = c.post(f"/youth/events/{eid}/toggle", data=json.dumps(
    {"member_id": kid, "field": "permission", "csrf_token": tok}),
    content_type="application/json")
check("toggle slip", r.get_json()["permission"] is True)
r = c.post(f"/youth/events/{eid}/toggle", data=json.dumps(
    {"member_id": kid, "field": "present", "csrf_token": tok}),
    content_type="application/json")
check("untoggle clears row", r.get_json()["present"] is False
      and not q("SELECT * FROM youth_attendance WHERE event_id=?", eid))
r = c.post(f"/youth/events/{eid}/toggle", data=json.dumps(
    {"member_id": kid, "field": "present", "csrf_token": "wrong"}),
    content_type="application/json")
check("toggle rejects bad csrf", r.status_code == 400)
# put Katie back for later checks
c.post(f"/youth/events/{eid}/toggle", data=json.dumps(
    {"member_id": kid, "field": "present", "csrf_token": tok}),
    content_type="application/json")

# ---- visitor quick-add + 2nd visit nudge via toggle
tok = csrf(c, f"/youth/events/{eid}")
c.post(f"/youth/events/{eid}", data=dict(csrf_token=tok, action="visitor",
       first_name="New", last_name="Kid"), follow_redirects=True)
vid = q("SELECT id FROM members WHERE first_name='New'")[0]["id"]
tok2 = csrf(c, "/youth/events")
c.post("/youth/events", data=dict(csrf_token=tok2, name="Youth Night", date="2026-07-15",
       repeat_weeks="0", detail=""), follow_redirects=True)
eid2 = q("SELECT id FROM youth_events WHERE name='Youth Night'")[0]["id"]
r = c.post(f"/youth/events/{eid2}/toggle", data=json.dumps(
    {"member_id": vid, "field": "present", "csrf_token": tok}),
    content_type="application/json")
check("2nd-visit nudge in toggle reply", "2nd visit" in r.get_json().get("nudge", ""))

# ---- promotion: kindergartner regression + year guard
tok = csrf(c, "/youth/new")
c.post("/youth/new", data={"csrf_token": tok, "first_name": "Kinder", "last_name": "Kid",
       "grade": "K", "g_name": [""], "g_relation": [""], "g_phone": [""]},
       follow_redirects=True)
kg_id = q("SELECT id FROM members WHERE first_name='Kinder'")[0]["id"]
tok = csrf(c, "/youth")
r = c.post("/youth/promote", data=dict(csrf_token=tok), follow_redirects=True)
check("promotion runs with auto-backup", b"backup was saved" in r.data)
check("K promotes to 1, not 2",
      q("SELECT grade FROM youth_profiles WHERE member_id=?", kg_id)[0][0] == "1")
check("grade 8 -> 9", q("SELECT grade FROM youth_profiles WHERE member_id=?", kid)[0][0] == "9")
r = c.post("/youth/promote", data=dict(csrf_token=tok), follow_redirects=True)
check("same-year rerun blocked", b"already ran this school year" in r.data
      and q("SELECT grade FROM youth_profiles WHERE member_id=?", kg_id)[0][0] == "1")
r = c.post("/youth/promote", data=dict(csrf_token=tok, confirm_again="on"),
           follow_redirects=True)
check("explicit confirm overrides guard",
      q("SELECT grade FROM youth_profiles WHERE member_id=?", kg_id)[0][0] == "2")
x("UPDATE youth_profiles SET grade='9' WHERE member_id=?", kid)

# ---- youth form preserves non-Visitor/Active status
x("UPDATE members SET status='Inactive' WHERE id=?", kid)
tok = csrf(c, f"/youth/{kid}/edit")
c.post(f"/youth/{kid}/edit", data={"csrf_token": tok, "first_name": "Katie",
       "last_name": "Miller", "grade": "9", "allergies": "peanuts",
       "g_name": ["Sarah Miller"], "g_relation": ["Mom"], "g_phone": ["573-555-0101"]},
       follow_redirects=True)
check("Inactive preserved by youth edit",
      q("SELECT status FROM members WHERE id=?", kid)[0][0] == "Inactive")
x("UPDATE members SET status='Active' WHERE id=?", kid)

# ---- care-gated export
r = c.get("/export/youth.csv")
check("default export omits medical", b"peanuts" not in r.data)
r = c.get("/export/youth.csv?care=1")
check("care export includes medical", b"peanuts" in r.data)

# ---- in-app SimpleChurch import with family links + groups
sc = ("User ID,Family ID,First Name,Preferred Name,Last Name,Birthday,Email,"
      "Home Phone,Cell Phone,Work Phone,Address,City,State,Zip Code,Baptism Date,"
      "Died On,Active,Membership,Anniversary,Family Relationship,Active Groups,Notes\n"
      "1,77,Robert,Bob,Vance,03/14/1975,bob@x.com,,555-1,,1 Elm,Town,MO,65801,,"
      ",Yes,01/02/2010,05/20/2000,Primary,Choir,long note\n"
      "2,77,Phyllis,,Vance,07/04/1978,,,555-2,,,,,,,,Yes,,,Spouse,,\n"
      "3,77,Junior,,Vance,01/01/2012,,,,,,,,,,,Yes,,,Child,,\n")
from io import BytesIO
tok = csrf(c, "/import")
r = c.post("/import", data={"csrf_token": tok, "file": (BytesIO(sc.encode()), "sc.csv")},
           content_type="multipart/form-data", follow_redirects=True)
check("SimpleChurch file recognized", b"SimpleChurch" in r.data)
bob = q("SELECT * FROM members WHERE first_name='Bob'")
check("preferred name wins", len(bob) == 1)
check("is_member set from Membership date", bob[0]["is_member"] == 1)
check("spouse link created", len(q("""SELECT * FROM relationships r
    JOIN members a ON a.id=r.member_id JOIN members b ON b.id=r.related_id
    WHERE a.first_name='Bob' AND b.first_name='Phyllis' AND r.relation='Spouse'""")) == 1)
check("child->parent link + inverse", len(q("""SELECT * FROM relationships r
    JOIN members a ON a.id=r.member_id WHERE a.first_name='Junior'
    AND r.relation='Parent'""")) == 2)
check("group recreated", len(q("SELECT * FROM grp WHERE name='Choir'")) == 1)
check("note imported", len(q("SELECT * FROM notes WHERE body='long note'")) == 1)

# ---- Attendance Taker: attendance only, redirected home
from werkzeug.security import generate_password_hash
x("INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
  "taker", generate_password_hash("testpass123"), "Taker T", "Attendance Taker")
ct = app.test_client()
login(ct, "taker")
r = ct.get("/", follow_redirects=True)
check("attendance taker lands on attendance", b"Attendance" in r.data)
check("attendance taker: no members access", ct.get("/members").status_code == 403)
check("attendance taker: no youth access", ct.get("/youth").status_code == 403)

# ---- Youth Leader: youth-first landing + no giving
x("INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
  "leader", generate_password_hash("testpass123"), "Leader Lou", "Youth Leader")
cl = app.test_client()
login(cl, "leader")
r = cl.get("/", follow_redirects=True)
check("youth leader lands on youth dashboard", b"Youth dashboard" in r.data)
check("youth leader blocked from giving", cl.get("/giving").status_code == 403)

# ---- session epoch: admin password reset kicks the leader out
tok = csrf(c, "/admin")
lid = q("SELECT id FROM users WHERE username='leader'")[0]["id"]
c.post("/admin", data=dict(csrf_token=tok, action="user_password", id=str(lid),
       password="newpass12345"), follow_redirects=True)
r = cl.get("/youth", follow_redirects=True)
check("password reset signs out other device", b"Sign in" in r.data or b"signed out" in r.data)
login(cl, "leader", "newpass12345")
check("new password works", cl.get("/youth").status_code == 200)

# ---- last-admin guards
r = c.post("/admin", data=dict(csrf_token=tok, action="user_edit", id=str(q(
    "SELECT id FROM users WHERE username='padmin'")[0]["id"]), username="padmin",
    display_name="Pat", role="Deacon"), follow_redirects=True)
check("can't change own role", b"can't change your own role" in r.data.lower()
      or q("SELECT role FROM users WHERE username='padmin'")[0][0] == "Admin")

# ---- upgrade paths: three database lineages all land on v3
import sqlite3 as s3
def lineage_db(builder):
    shutil.rmtree("data", ignore_errors=True)
    os.makedirs("data/photos")
    con = s3.connect("data/church.db")
    builder(con)
    con.commit(); con.close()
    appmod.init_db()
    con = s3.connect("data/church.db")
    con.row_factory = s3.Row
    cols_m = {r[1] for r in con.execute("PRAGMA table_info(members)")}
    cols_n = {r[1] for r in con.execute("PRAGMA table_info(notes)")}
    cols_u = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ver = con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]
    con.close()
    return ("is_member" in cols_m and "kind" in cols_n and "session_epoch" in cols_u
            and "ministries" in tables and "youth_profiles" in tables and ver == "3")

def churchbook_v1(con):
    con.executescript("""
      CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
        role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE members (id INTEGER PRIMARY KEY, first_name TEXT NOT NULL,
        last_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Active',
        leadership_role TEXT DEFAULT '', birthdate TEXT, membership_date TEXT,
        baptism_date TEXT, anniversary TEXT, phone TEXT DEFAULT '',
        email TEXT DEFAULT '', address TEXT DEFAULT '', photo TEXT DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE notes (id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL,
        author TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'leaders',
        body TEXT NOT NULL, created_at TEXT NOT NULL);
      INSERT INTO settings VALUES('schema_version','1');
      INSERT INTO members(first_name,last_name,membership_date,created_at,updated_at)
        VALUES('Old','Timer','2001-01-01','x','x');
    """)

check("ChurchBook v1 upgrades to v3", lineage_db(churchbook_v1))
con = s3.connect("data/church.db")
check("v1 is_member backfilled from membership_date",
      con.execute("SELECT is_member FROM members WHERE first_name='Old'").fetchone()[0] == 1)
con.close()

def onechurch_v2(con):
    churchbook_v1(con)
    con.executescript("""
      ALTER TABLE notes ADD COLUMN kind TEXT NOT NULL DEFAULT 'general';
      CREATE TABLE youth_profiles (member_id INTEGER PRIMARY KEY,
        grade TEXT NOT NULL DEFAULT '?', photo_consent TEXT NOT NULL DEFAULT '',
        allergies TEXT NOT NULL DEFAULT '', medical TEXT NOT NULL DEFAULT '',
        emergency_name TEXT NOT NULL DEFAULT '', emergency_phone TEXT NOT NULL DEFAULT '',
        graduated INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE youth_events (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        date TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
        permission_required INTEGER NOT NULL DEFAULT 0);
      UPDATE settings SET value='2' WHERE key='schema_version';
    """)

check("earlier One Church v2 upgrades to v3", lineage_db(onechurch_v2))

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL TESTS PASSED")
sys.exit(1 if fails else 0)
