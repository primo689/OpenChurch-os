# Congregation Manager

A self-hosted church membership system that runs on a Raspberry Pi on your church's
local network. No subscription, no cloud, no internet required.

## What it does

- **Members** — name, status, birthdate, membership date, baptism date, anniversary,
  phone, email, address, photo. Search by name, email, phone, or address.
- **Family links** — parent, child, grandparent, grandchild, spouse, sibling.
  Add one direction and the reverse is linked automatically.
- **Serving roles** — custom tags like Security or Attendance Taker, filterable.
- **Attendance** — per event type (Sunday worship, Sunday School, youth, etc.),
  with a trend chart and a "not seen in N weeks" follow-up list.
- **Giving** — record gifts by fund, with printable year-end statements.
- **Notes** — pastoral notes with two visibility levels. "Leadership only" notes are
  hidden from Deacons, Youth Ministers, and Youth Leaders.
- **Groups** — classes and teams, with a copy-paste BCC email list.
- **Logins & roles** — Admin, Minister, Elder, Youth Minister, Deacon, Youth Leader,
  each with different access. Giving is limited to Admin, Minister, and Elder.
- **Audit log, CSV import/export, one-click backups.**

## Install on a Raspberry Pi

Tested on Raspberry Pi OS. From a terminal on the Pi:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv
# Copy the congregation-manager folder to the Pi, e.g. /home/pi/congregation-manager, then:
cd /home/pi/congregation-manager
python3 -m venv venv
venv/bin/pip install flask waitress
venv/bin/python app.py
```

Open a browser on any computer or phone on the church network and go to
`http://<the-pi's-ip-address>:8081`. You'll be walked through setup:
church name, your admin account, and your leadership team.

To find the Pi's IP address, run `hostname -I` on the Pi. Give the Pi a
static IP in your router settings so the address never changes.

## Run it automatically at boot

Create a service so it starts whenever the Pi powers on:

```bash
sudo tee /etc/systemd/system/congregation-manager.service << 'EOF'
[Unit]
Description=Congregation Manager
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/congregation-manager
ExecStart=/home/pi/congregation-manager/venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now congregation-manager
```

## Backups (do not skip this)

SD cards fail. The database is a single file: `data/church.db`.

1. In the app, Admin → "Back up now" copies it into the `backups/` folder.
2. Schedule a nightly backup **and copy it to a USB drive**:

```bash
# Plug in a USB drive; it usually mounts at /media/pi/<drive-name>
crontab -e
# add this line (adjust the drive path):
15 2 * * * /home/pi/congregation-manager/backup.sh /media/pi/USBDRIVE
```

`backup.sh` keeps the last 30 nightly copies and prunes older ones.

Restoring is just replacing `data/church.db` with a backup file while the
service is stopped (`sudo systemctl stop congregation-manager`).

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
