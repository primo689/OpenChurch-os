# Church Youth Manager

Free, self-hosted youth ministry software for small churches. Runs on a
$50 Raspberry Pi on your church wifi. No subscription, no cloud, no
company — your kids' information never leaves your building.

Tracks: students K–12, parents/guardians, allergies & medical needs,
emergency contacts, attendance, permission forms, visitors, baptism
conversations and baptism dates, follow-up notes, volunteers, birthdays,
attendance trends, and grade promotion. Multi-user with individual
accounts. Automatic nightly backups. Printable trip safety sheets.
CSV exports for the church office.

Built by a youth minister, for youth ministers. Provided free, as-is,
under the MIT license — use it, change it, share it. There's no support
line, but the setup guide below assumes zero technical background, and
the whole app is small enough that any local techie (or an AI assistant)
can help you with it.

**A note on responsibility:** this software holds children's medical and
contact information. Keeping that data safe — screened volunteers only,
private wifi, backups — is your church's responsibility. The guide's
security section tells you how.

---

A small self-hosted server for tracking students, guardians, allergies,
emergency contacts, attendance, permission forms, baptism conversations,
follow-up notes, and volunteers. Runs on a Raspberry Pi at your church.
Everyone on your team gets their own account, and setup happens in the
browser — no code editing, ever.

You can hand this same folder to any other church. The first time they
open it, it asks for their church name, ministry name, and first admin
account, and it becomes theirs.

There are only three files that matter:

- `server.py` — the server. You never need to edit it.
- `static/index.html` — the app people see in their browser.
- `data.db` — created automatically. **This file IS your data.** Back it up.

---

## One-time setup (about 20 minutes)

### 1. Get the Pi running

Any Raspberry Pi 3 or newer works. Install Raspberry Pi OS using the
official Raspberry Pi Imager (raspberrypi.com/software) — during imaging,
set a username/password and enter your church wifi so it connects on boot.

### 2. Copy this folder onto the Pi

Easiest way: put this whole `church-youth-manager` folder on a USB stick,
plug it into the Pi, and copy it to the home folder. You want it to end
up at `/home/pi/church-youth-manager` (if your Pi username isn't `pi`,
adjust the paths below to match).

### 3. Install Flask (the only thing it needs)

Open a Terminal on the Pi and run:

    sudo apt update
    sudo apt install -y python3-flask python3-waitress

### 4. Start it once to test

    cd /home/pi/church-youth-manager
    python3 server.py

Open a browser on the Pi and go to `http://localhost:8080`. You'll see
the **setup screen** — enter your church name, ministry name (this is
what shows at the top of the app, e.g. "The Basement" or "Ignite"), and create
your own admin account. That's the whole setup.

When it works, press Ctrl+C in the terminal to stop it — the next step
makes it run by itself.

### 5. Make it start automatically on boot

    sudo nano /etc/systemd/system/youth-tracker.service

Paste this in (adjust `pi` if your username differs):

    [Unit]
    Description=Church Youth Manager
    After=network.target

    [Service]
    User=pi
    WorkingDirectory=/home/pi/church-youth-manager
    ExecStart=/usr/bin/python3 /home/pi/church-youth-manager/server.py
    Restart=always

    [Install]
    WantedBy=multi-user.target

Save (Ctrl+O, Enter, Ctrl+X), then run:

    sudo systemctl enable --now youth-tracker

From now on the Pi runs the tracker whenever it has power. Unplug the
monitor and keyboard and stick the Pi on a shelf near the router.

### 6. Give the Pi a fixed address

Find the Pi's IP address:

    hostname -I

It'll say something like `192.168.1.50`. Log into your church router and
reserve that address for the Pi (usually called "DHCP reservation" or
"static lease") so it never changes. If you can't get into the router,
the address usually stays the same anyway — just know it *can* change
after a power outage.

### 7. Put it on everyone's phone

On any phone connected to the church wifi, open Safari or Chrome and go to:

    http://192.168.1.50:8080   (use YOUR Pi's address)

Sign in, then tap Share → **Add to Home Screen**. Now it looks and feels
like an app.

---

## Managing your team

Everyone signs in with their own username and password. From the **More**
tab, admins can:

- **Add a person** — give them a name, username, password, and a role.
- **Roles:** *Leader* is for everyday use (attendance, notes, students).
  *Admin* can also manage accounts, church settings, and restore backups.
  Most volunteers should be Leaders.
- **Reset a password** — edit their account and type a new one. Handy
  when someone forgets.
- **Remove an account** — when a volunteer moves on, remove them and
  they can't sign in anymore. This is the whole point of individual
  accounts: you never have to change a shared password and re-tell
  everyone.

The app won't let you remove your own account or the last admin, so you
can't lock yourself out.

Notes are signed automatically — when someone adds a follow-up or baptism
note, their name is on it, so you always know who talked to whom.

---

## Everyday use

- **Wednesday night:** open the app, tap the event, tap "Here" next to each
  kid as they walk in. If the event needs permission forms, tap "Form" when
  a kid hands one in. When you create a Wednesday event, use "Repeat
  weekly" to make the whole semester at once.
- **Before a trip or lock-in:** open the event → **Safety sheet** → Print.
  One page with every kid's allergies, medical needs, and emergency
  contacts, for whichever adult is in the room.
- **Texting parents:** open the event → **Copy parent #s** — paste the
  numbers straight into a group text.
- **Keeping kids from slipping away:** the Students tab shows a
  "Haven't seen lately" alert for kids who came regularly and then missed
  three events in a row. That list is your follow-up list.
- **A kid brings a friend:** on the event's attendance screen, tap
  **+ Visitor** and type their name — they're added and marked here in one
  step. The app flags their second visit, because a second-time visitor is
  exactly who deserves a follow-up. When they become a regular, edit them
  and uncheck "visitor."
- **When someone gets baptized:** put the date on their record ("Baptized
  on"). It shows on their page and in the exports — the conversation log
  leads somewhere, and now the app remembers where.
- **Birthdays:** the More tab lists everyone with a birthday in the next
  30 days, so you catch them before Wednesday night, not after.
- **After a good conversation:** open the student, tap "Baptism convo,"
  write down where they're at. It's dated and signed automatically.
- **Promotion Sunday (last Sunday of May):** a banner appears on the
  Students tab when it's time. One tap moves everyone up a grade and
  graduates the seniors — they're archived with their full history kept.
- **When a kid moves away:** archive them instead of deleting. They
  disappear from lists but their story stays.
- **For elder meetings:** the More tab shows monthly attendance trends —
  real numbers for the conversation about the youth program.

---

## Maintenance (the honest version)

**Backups now happen automatically.** Every night (and every time it
starts up), the Pi copies `data.db` into a `backups/` folder next to it,
keeping the last 30 days. If the app ever breaks or the SD card dies with
the backups folder intact, restore by copying the newest
`backups/backup-DATE.db` over `data.db` and restarting.

**Still do an off-Pi backup monthly.** The automatic backups die with the
SD card if the whole card fails. Two ways, either works:

1. In the app: More tab → **Download backup**. Email the file to yourself.
2. On the Pi: copy the whole `backups/` folder to a USB stick. (Don't
   copy `data.db` itself while the server is running — the nightly
   backups in `backups/` are made with SQLite's safe-copy and are always
   consistent; the live file may not be.)

The More tab also has **Students CSV** and **Attendance CSV** buttons —
plain spreadsheets for the church office or elder reports.

Restoring from the in-app backup replaces ministry data but deliberately
never touches sign-in accounts — so a restore can't lock anyone out.
Restoring by copying `data.db` back restores everything, accounts too.

**If the app stops responding:** unplug the Pi, wait ten seconds, plug it
back in. It restarts itself. Ninety percent of problems end here.

**If it still doesn't work:** plug a monitor into the Pi and run:

    sudo systemctl status youth-tracker

The error it shows will tell you what's wrong — or paste it into any AI assistant and ask.

**Updates to the Pi itself** (a few times a year is plenty):

    sudo apt update && sudo apt upgrade -y
    sudo reboot

**Version:** the app shows its version number on the sign-in screen and
at the bottom of the More tab. When asking for help or comparing with
another church, that number is the first thing to check.

**Adding features:** paste `server.py` or `index.html` into an AI assistant, describe
what you want changed, and copy the new file back onto the Pi. Restart with
`sudo systemctl restart youth-tracker`. Your data is untouched — it lives
in `data.db`, not in the code.

---

## Giving this to another church

Copy the folder (WITHOUT `data.db` and `.secret` — those are yours) and
send it to them with this README. They follow the same steps, and the
setup screen makes it theirs: their church name, their ministry name,
their accounts. Nothing about your church travels with the code.

---

## What this is and isn't (security honesty)

This holds kids' names, medical info, and family phone numbers, so treat
it accordingly:

- It's only reachable on the **church wifi**. That's a feature — it's not
  exposed to the internet.
- Passwords are stored hashed (scrambled one-way), never in plain text,
  must be 8+ characters, and five wrong guesses locks that username for
  15 minutes.
- If someone loses a phone that was signed in, an admin resetting that
  person's password signs the lost phone out everywhere, immediately.
- Honest limitation: traffic on your wifi isn't encrypted (no HTTPS on a
  local network without real hassle). On a private church network that's
  an acceptable tradeoff; it's one more reason the Pi should never be on
  the guest wifi. Tailscale (below) encrypts everything if you add it.
- Honest limitation: there's no audit log — the app doesn't record who
  viewed or edited what. Signed notes cover the pastoral records, but if
  your church requires access logging, this tool doesn't do it.
- Give accounts only to screened volunteers — the same people you'd hand
  a paper roster.
- If the church wifi has a guest network, put the Pi on the **private**
  network, not the guest one.
- If you ever want to check attendance from home, don't open the Pi to the
  internet directly. Install **Tailscale** (tailscale.com) on the Pi and
  your phone — it's free for personal use and creates a private tunnel
  only your devices can use. Their website has a simple Raspberry Pi guide.
