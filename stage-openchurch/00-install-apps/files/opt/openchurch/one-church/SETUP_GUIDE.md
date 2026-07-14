# One Church Setup Guide

From a blank Raspberry Pi to a running system. Plan on about an hour, most of it
waiting on downloads. No steps are optional except where marked.

---

## What you need

- Raspberry Pi 3, 4, or 5 (any RAM size works for a church this size)
- MicroSD card, 16 GB or larger, name-brand (cheap cards are how you lose data)
- Power supply for the Pi
- USB flash drive for backups
- Ethernet cable (recommended) or the church Wi-Fi password — **staff Wi-Fi, not guest**
- A computer with an SD card slot for the first step
- The `one-church.zip` file

---

## Step 1 — Put the operating system on the SD card

On your computer:

1. Download **Raspberry Pi Imager** from raspberrypi.com/software and install it.
2. Insert the microSD card.
3. Open Imager and choose:
   - **Device:** your Pi model
   - **OS:** Raspberry Pi OS Lite (64-bit) — under "Raspberry Pi OS (other)".
     Lite has no desktop; you don't need one and it runs lighter.
   - **Storage:** the SD card
4. Click **Next**, then **Edit Settings** when it asks. Set:
   - hostname: `congregation`
   - Username: `pi` and a password you'll remember
   - Wi-Fi network and password (skip if using ethernet)
   - Under the Services tab: **enable SSH** with password authentication
5. Write the card. Takes a few minutes.

## Step 2 — First boot

1. Put the SD card in the Pi, connect ethernet if using it, plug in power.
2. Wait two minutes.
3. From your computer, open a terminal (Mac: Terminal app; Windows: PowerShell) and run:

```
ssh pi@congregation.local
```

Type the password you set. If `congregation.local` doesn't resolve, log into your
router's admin page, find the device list, and use the Pi's IP address instead:
`ssh pi@192.168.x.x`.

## Step 3 — Give the Pi a permanent address

The Pi's address must never change, or bookmarks and phones will lose it.

Log into your router's admin page, find **DHCP reservations** (sometimes called
"static leases" or "always use this IP"), and reserve the Pi's current IP for its
MAC address. Every router does this a little differently; search your router model
plus "DHCP reservation" if you can't find it.

Write the IP address down. That's the address everyone will use.

## Step 4 — Install One Church

Still in the SSH session, run these one at a time:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv unzip sqlite3
```

Copy the zip to the Pi. From a **second** terminal on your computer, in the folder
holding the zip:

```bash
scp one-church.zip pi@congregation.local:/home/pi/
```

Back in the SSH session:

```bash
cd /home/pi
unzip one-church.zip
cd one-church
python3 -m venv venv
venv/bin/pip install flask waitress
```

> **Before Step 5:** the service file below assumes the username `pi` and the
> app at `/home/pi/one-church`. If your username is different, replace `pi`
> everywhere it appears. And keep the app somewhere WITHOUT spaces in the path
> (`/home/yourname/one-church`, not `~/Documents/My Church Stuff/`) - systemd
> fails with a CHDIR error on paths with spaces, and Python venvs break if you
> move them after creation (delete `venv` and recreate it if you relocate).

## Step 5 — Make it start on boot and stay running

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

Check it's alive:

```bash
systemctl status one-church
```

You want to see "active (running)". If the Pi ever locks up, pulling the power
and plugging it back in brings One Church up on its own.

## Step 6 — Run the onboarding

On any computer or phone on the church network, browse to:

```
http://<the-pi's-ip-address>:8080
```

(One Church runs on port **8080** so it never collides with
Church Youth Manager on 8080 — both can live on the same Pi.)

The setup wizard will walk you through:

1. **Church name and city** — the city appears on printed giving statements.
2. **Your admin account** — pick a real password, 8+ characters. This account
   can do everything, including manage other logins.
3. **Leadership** — add each minister, elder, deacon, and leader by name and role.
   Check "also create a login" for anyone who'll actually use the system, and set
   them a starter password. Tell them to change it from their **Account** page
   the first time they sign in.

Click **Finish setup** and you're on the dashboard.

## Step 7 — Set up backups (do not skip)

Plug the USB drive into the Pi. Find its name:

```bash
ls /media/pi/
```

Then schedule the nightly backup:

```bash
crontab -e
```

Choose nano if it asks, then add this line at the bottom (replace USBDRIVE with
the actual name from the step above):

```
15 2 * * * /home/pi/one-church/backup.sh /media/pi/USBDRIVE
```

Save (Ctrl+O, Enter) and exit (Ctrl+X). Every night at 2:15 the database is copied
to the Pi and to the USB drive, keeping the last 30 of each.

Test it right now instead of trusting it:

```bash
/home/pi/one-church/backup.sh /media/pi/USBDRIVE
ls /media/pi/USBDRIVE
```

You should see a `church-<date>.db` file. **That drive holds names, addresses,
kids' birthdates, notes, and giving. It lives in a locked drawer.**

## Step 8 — Load your people

Two ways:

- **From SimpleChurch:** export your members to CSV, then in One Church go to
  Admin → Import members. Column names it understands are listed on that page.
  Dates need to be YYYY-MM-DD format — fix them in a spreadsheet first if needed.
  Import skips duplicate names, so running it twice won't double everyone.
- **By hand:** Members → Add member. Slower, but you'll clean data as you go.

After importing, spot-check ten people. Then link families from each member's page
(add "Parent" one direction; the reverse appears automatically) and add photos as
you get them.

---

## Before go-live: three decisions for leadership

Settle these with Cody and the elders before real data goes in, not after:

1. **Who sees giving.** Admin → "Who can see giving." Default is Admin, Minister,
   and Elder. Many churches narrow it to one treasurer. This is a policy decision,
   not a technical one.
2. **Who writes and reads youth notes.** "Leadership only" notes are visible to
   Admin, Minister, and Elders. Everyone with a login can see "All leaders" notes.
   Agree on what belongs at which level.
3. **Where the Pi and the backup drive physically live.** Locked office, locked
   drawer. Whoever holds the hardware holds the data.

## Everyday use

- **Attendance:** Attendance → pick event and date → check names → Save. Walk-in?
  Use the quick-add box at the bottom. Print the sheet with the button that appears
  after saving.
- **Follow-up:** Reports shows the trend chart and everyone not seen in N weeks,
  with phone numbers ready.
- **Year-end statements:** each member's page has a statement button per year,
  print-ready, from January onward.

## If something breaks

- Page won't load: `sudo systemctl restart one-church`, wait ten seconds, retry.
- Still down: `journalctl -u one-church -n 50` shows the last 50 log lines —
  send those to whoever is helping you.
- Restoring a backup: `sudo systemctl stop one-church`, copy the backup file over
  `/home/pi/one-church/data/church.db`, then `sudo systemctl start one-church`.
- Locked out entirely (lost admin password): from SSH, delete
  `/home/pi/one-church/data/church.db` **only if you're restoring from a backup
  taken before the problem** — deleting it without a backup erases everything.
