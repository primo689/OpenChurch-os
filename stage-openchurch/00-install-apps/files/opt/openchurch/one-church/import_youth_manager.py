#!/usr/bin/env python3
"""
Import a Church Youth Manager database into One Church.

Usage:
    python3 import_youth_manager.py /path/to/data.db

Run this from the one-church folder AFTER starting the app at least once
(so data/church.db exists and the schema is in place). Safe to preview:
run with --dry-run to see what would be imported without writing anything.

What comes across:
  students    -> members + youth profiles + guardians (+ their notes)
  events      -> youth events
  attendance  -> youth event check-ins, including permission slips
  volunteers  -> members with the "Youth Volunteer" serving role

Students are matched to existing members by exact first+last name, so if
you already entered a kid in One Church their record is reused, not doubled.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DEST_PATH = os.path.join(BASE, "data", "church.db")


def rows(con, table):
    out = []
    for r in con.execute(f"SELECT id, data FROM {table}").fetchall():
        d = json.loads(r["data"])
        d["_id"] = r["id"]
        out.append(d)
    return out


def split_name(name):
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], "(last name?)"
    return " ".join(parts[:-1]), parts[-1]


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    if len(args) != 1:
        print(__doc__)
        sys.exit(1)
    src_path = args[0]
    if not os.path.exists(src_path):
        sys.exit(f"Source not found: {src_path}")
    if not os.path.exists(DEST_PATH):
        sys.exit("data/church.db not found. Start One Church once first (python3 app.py), "
                 "then run this importer.")

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(DEST_PATH)
    dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA foreign_keys = ON")

    now = datetime.now().isoformat(timespec="seconds")
    existing = {(m["first_name"].lower(), m["last_name"].lower()): m["id"]
                for m in dst.execute("SELECT id, first_name, last_name FROM members").fetchall()}

    students = rows(src, "students")
    events = rows(src, "events")
    volunteers = rows(src, "volunteers")
    att = {r["id"]: json.loads(r["data"])
           for r in src.execute("SELECT id, data FROM attendance").fetchall()}

    print(f"Found: {len(students)} students, {len(events)} events, "
          f"{len(volunteers)} volunteers, {len(att)} attendance records.")
    if dry:
        print("Dry run - nothing written.")
        return

    id_map = {}   # old student id -> member id
    matched = 0
    warned = []

    for s in students:
        first, last = split_name(s.get("name"))
        if not first:
            continue
        key = (first.lower(), last.lower())
        s_bd = (s.get("birthdate") or "").strip()
        if key in existing:
            mid = existing[key]
            # Same name is not proof of same person (Jr., twins, father/son).
            # If both records have a birthdate and they disagree, keep them
            # separate rather than silently merging two different people.
            m_bd = (dst.execute("SELECT birthdate FROM members WHERE id=?",
                                (mid,)).fetchone()["birthdate"] or "").strip()
            if s_bd and m_bd and s_bd != m_bd:
                warned.append(f"  {first} {last}: birthdates disagree "
                              f"({s_bd} vs {m_bd}) - imported as a SEPARATE person. "
                              "Merge by hand if they really are the same.")
                status = "Visitor" if s.get("visitor") else "Active"
                cur = dst.execute(
                    "INSERT INTO members(first_name,last_name,status,birthdate,baptism_date,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (first, last, status, s_bd, s.get("baptizedDate", "") or "", now, now))
                mid = cur.lastrowid
            else:
                matched += 1
                if not (s_bd and m_bd):
                    warned.append(f"  {first} {last}: matched to an existing member by "
                                  "name only (no birthdate to compare) - spot-check them.")
        else:
            status = "Visitor" if s.get("visitor") else "Active"
            cur = dst.execute(
                "INSERT INTO members(first_name,last_name,status,birthdate,baptism_date,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (first, last, status, s.get("birthdate", "") or "",
                 s.get("baptizedDate", "") or "", now, now))
            mid = cur.lastrowid
            existing[key] = mid
        id_map[s["_id"]] = mid

        consent = s.get("photoConsent", "")
        dst.execute(
            """INSERT INTO youth_profiles(member_id,grade,photo_consent,allergies,medical,
               emergency_name,emergency_phone,graduated,archived) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(member_id) DO UPDATE SET grade=excluded.grade""",
            (mid, s.get("grade", "?") or "?", consent if consent in ("yes", "no") else "",
             s.get("allergies", "") or "", s.get("medical", "") or "",
             s.get("emergencyName", "") or "", s.get("emergencyPhone", "") or "",
             1 if s.get("graduated") else 0, 1 if s.get("archived") else 0))

        for g in s.get("guardians") or []:
            if (g.get("name") or "").strip():
                dst.execute("INSERT INTO guardians(member_id,name,relation,phone) VALUES(?,?,?,?)",
                            (mid, g["name"].strip(), g.get("relation", "") or "",
                             g.get("phone", "") or ""))

        for n in s.get("notes") or []:
            kind = n.get("type", "general")
            if kind not in ("followup", "baptism"):
                kind = "general"
            dst.execute(
                "INSERT INTO notes(member_id,author,visibility,kind,body,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (mid, n.get("by", "imported") or "imported", "leaders", kind,
                 n.get("text", "") or "", n.get("date", now[:10]) or now[:10]))

    ev_map = {}
    for e in events:
        cur = dst.execute(
            "INSERT INTO youth_events(name,date,detail,permission_required) VALUES(?,?,?,?)",
            (e.get("name", "Event") or "Event", e.get("date", "") or "",
             e.get("detail", "") or e.get("notes", "") or "",
             1 if e.get("permission") or e.get("permissionRequired") else 0))
        ev_map[e["_id"]] = cur.lastrowid

    checkins = 0
    for old_eid, rec in att.items():
        if old_eid not in ev_map:
            continue
        present = rec.get("present") or {}
        slips = rec.get("permission") or {}
        for old_sid, is_present in present.items():
            if is_present and old_sid in id_map:
                dst.execute(
                    "INSERT OR IGNORE INTO youth_attendance(event_id,member_id,permission) "
                    "VALUES(?,?,?)",
                    (ev_map[old_eid], id_map[old_sid], 1 if slips.get(old_sid) else 0))
                checkins += 1

    role_id = None
    if volunteers:
        dst.execute("INSERT OR IGNORE INTO custom_roles(name) VALUES('Youth Volunteer')")
        role_id = dst.execute("SELECT id FROM custom_roles WHERE name='Youth Volunteer'"
                              ).fetchone()["id"]
    for v in volunteers:
        first, last = split_name(v.get("name"))
        if not first:
            continue
        key = (first.lower(), last.lower())
        if key in existing:
            mid = existing[key]
        else:
            cur = dst.execute(
                "INSERT INTO members(first_name,last_name,status,phone,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)", (first, last, "Active", v.get("phone", "") or "", now, now))
            mid = cur.lastrowid
            existing[key] = mid
        dst.execute("INSERT OR IGNORE INTO member_roles(member_id,role_id) VALUES(?,?)",
                    (mid, role_id))

    dst.execute("INSERT INTO audit(ts,username,action,detail) VALUES(?,?,?,?)",
                (now.replace("T", " "), "importer", "import.youth_manager",
                 f"{len(students)} students ({matched} matched existing members), "
                 f"{len(events)} events, {checkins} check-ins, {len(volunteers)} volunteers"))
    dst.commit()
    print(f"Done. {len(students)} students imported ({matched} matched to existing members), "
          f"{len(events)} events, {checkins} check-ins, {len(volunteers)} volunteers.")
    if warned:
        print()
        print("CHECK THESE BY HAND:")
        for w in warned:
            print(w)
    print("Anything odd will show in the app - spot-check a few students before trusting it.")


if __name__ == "__main__":
    main()
