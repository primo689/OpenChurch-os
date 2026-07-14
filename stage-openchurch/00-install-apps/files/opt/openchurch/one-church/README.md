# One Church

A self-hosted church management system that runs on a Raspberry Pi on your
church's local network. Whole-congregation records and a full youth ministry
module in one place: one database, one login, one record per person. No
subscription, no cloud, no internet required.

One Church is the merger of ChurchBook (congregation-manager) and Church
Youth Manager. Databases from either app upgrade automatically; see
"Coming from an older app" below.

## Two views, one system

The header has a Church/Youth toggle for anyone whose role includes youth
access. **Church view** is the lead-minister home: members, attendance
trends, birthdays, follow-up list, giving. **Youth view** is the youth-minister
home: roster by grade, baptism follow-up watchlist, recent visitors, upcoming
events, check-in. The toggle is a preference, not a wall - both views stay
open to both ministers, so either one can cover for the other without any
settings changes.

## What it does

- **Members** - contact details, statuses, photos, and a one-click formal
  membership flag (kept separate from "shows up", because they're different
  questions). Search, filters, printable directory data.
- **Youth module** - students are members with a youth profile: grade,
  guardians with tap-to-call numbers, emergency contact, allergies and
  medical needs, photo consent. Ministries split the roster by grade range
  (e.g. Kids K-5, Youth 6-12), each with its own tab, guardian text list,
  and check-in rosters.
- **Events & check-in** - one-off events and trips with per-student
  permission slips. Every tap saves instantly and atomically, so two leaders
  can check kids in from two phones at once. Walk-in visitor quick-add, and
  a nudge on a visitor's second visit - the right moment to follow up.
- **Baptism follow-up** - notes have kinds; a student with a "Baptism
  conversation" note carries a badge and sits on the youth dashboard
  watchlist until their baptism date is recorded.
- **Promotion Sunday** - one click moves everyone up a grade and graduates
  seniors. Takes a backup automatically first, and refuses to run twice in
  one school year without an explicit extra confirmation.
- **Attendance** - per event type with trend chart and a "not seen in 4
  weeks" list. An Attendance Taker login role lets Sunday volunteers take
  attendance without seeing member records, notes, or giving.
- **Giving** - gifts by fund, printable year-end statements, access limited
  by role.
- **Notes** - pastoral notes with author, kind, and two visibility levels.
- **Groups, family links, serving roles, audit log, CSV import/export,
  one-click backups.**
- **Security** - per-person logins, role permissions, CSRF protection,
  login throttling, idle timeout, and session epochs: changing or resetting
  a password signs out every other device that used the old one, including
  a lost phone.

## Coming from an older app

**From ChurchBook (congregation-manager), any version:** copy your `data/`
folder (or just `church.db`) into One Church's `data/` folder and start the
app. The database upgrades itself on first start. Take a backup copy first
anyway - it's one file.

**From an earlier One Church build:** same thing - drop the database in and
start the app.

**From Church Youth Manager:** start One Church once so the database exists,
then:

```bash
python3 import_youth_manager.py /path/to/your/data.db
```

Add `--dry-run` first to preview. Students, guardians, notes, events,
check-ins, permission slips, and volunteers all import; students are matched
to existing members by name AND birthdate, with loud warnings for anything
ambiguous.

**From SimpleChurch CRM:** export members under Utilities -> Export Data,
then upload the file as-is on One Church's Admin -> Import page. The format
is recognized automatically; family relationships are recreated (spouse and
parent/child links), groups come across, and notes are kept. The import
report tells you exactly what happened.

## Privacy and data handling

This system holds pastoral notes, giving records, and - in the youth module -
guardians' phone numbers and children's allergy and medical details. Treat the
whole thing like the filing cabinet it replaces: locked, and only opened by
people with a reason.

- **Network.** Traffic between a browser and the Pi is not encrypted. Run it
  on the staff network only - never guest Wi-Fi - and don't expose it to the
  internet. For encryption on the wire, put a reverse proxy like Caddy in
  front with a self-signed certificate; the app needs no changes for that.
- **Backups contain everything.** A backup file is a full, unencrypted copy
  of the database. A USB backup drive deserves a locked drawer, not a desk
  cubby. Know where every copy is.
- **Exports.** The youth CSV leaves out allergies, medical needs, and
  emergency contacts unless you choose "Export with care details." Delete
  that file when you're done with it. Both exports are audit-logged.
- **Retention.** Archived students keep their full record. Decide a rule
  (for example: clear care details two years after graduation), write it
  down, and follow it.
- **Access.** Give each volunteer their own login so the audit log means
  something, use the Attendance Taker role for Sunday helpers, and
  deactivate logins when people step away. Resetting a password signs the
  old sessions out everywhere.

## Install on a Raspberry Pi

Tested on Raspberry Pi OS. From a terminal on the Pi:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv
# Copy the one-church folder to the Pi, e.g. /home/pi/one-church, then:
cd /home/pi/one-church
python3 -m venv venv
venv/bin/pip install flask waitress
venv/bin/python app.py
```

Open a browser on any computer or phone on the church network and go to
`http://<the-pi's-ip-address>:8080`. You'll be walked through setup:
church name, your admin account, and your leadership team.

To find the Pi's IP address, run `hostname -I` on the Pi. Give the Pi a
static IP in your router settings so the address never changes.

## Run it automatically at boot

Create a service so it starts whenever the Pi powers on:

```bash
sudo tee /etc/systemd/system/one-church.service << 'EOF'
[Unit]
Description=One Church
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/one-church
ExecStart=/home/pi/one-church/venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now one-church
```

## Backups (do not skip this)

SD cards fail. The database is a single file: `data/church.db`.

1. In the app, Admin → "Back up now" copies it into the `backups/` folder.
2. Schedule a nightly backup **and copy it to a USB drive**:

```bash
# Plug in a USB drive; it usually mounts at /media/pi/<drive-name>
crontab -e
# add this line (adjust the drive path):
15 2 * * * /home/pi/one-church/backup.sh /media/pi/USBDRIVE
```

`backup.sh` keeps the last 30 nightly copies and prunes older ones.

Restoring is just replacing `data/church.db` with a backup file while the
service is stopped (`sudo systemctl stop one-church`).

## Security and privacy — read this before going live

**Network.** This is built for a trusted local network only. Never port-forward it
to the internet; if you ever need remote access, put it behind Tailscale. Traffic on
the local network is plain HTTP, so **run the Pi on the church's private/staff Wi-Fi,
not on a guest network** whose password is on the wall.

**What's protected in the app:**
- Passwords are hashed; anyone can change their own from the Account page.
  Five failed logins locks a username out for 15 minutes, and failures are audited.
- Sessions sign out after 2 hours of inactivity — important for shared office computers.
- All forms carry anti-forgery tokens, and cross-site requests are rejected.
- The audit log records logins (including failures), field-level member edits,
  deletions, gifts, exports, and backups.
- Access to giving is configurable on the Admin page. **Decide with your leadership
  who should see individual giving before entering any gifts** — many churches limit
  it to a treasurer so pastoral relationships stay separate from money knowledge.

**What's on you:**
- This database will hold minors' names, birthdates, addresses, photos, and pastoral
  notes. Treat every copy of it accordingly.
- Backups are unencrypted copies of everything. A backup USB drive belongs in a
  locked drawer, not a desk cup. Old drives should be wiped, not tossed.
- Agree as a leadership team who writes youth notes and who reads them. The
  "Leadership only" visibility level exists for exactly that conversation.
- Keep the Pi itself somewhere physically secure — whoever holds the Pi holds the data.

## Files

```
app.py          all application code
templates/      HTML pages
static/         stylesheet (no internet fonts or scripts — works fully offline)
data/church.db  the database (created on first run)
data/photos/    member photos
backups/        database backups
backup.sh       nightly backup script for cron
```
