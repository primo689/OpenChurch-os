# Open Church OS

A ready-to-flash operating system image for Raspberry Pi that turns a $75
computer into a church management server. Built on Raspberry Pi OS Lite.

Flash it with Raspberry Pi Imager, detailed instructions below. Plug the Pi into the church network, browse to
**http://openchurch.local** — and pick, just once, which app this Pi runs:

- **One Church** — the full system: congregation records, giving, attendance,
  groups, and a complete youth ministry module. Church and Youth dashboards.
- **Congregation Manager** — congregation records only, no specific youth management.
- **Church Youth Manager** — youth ministry only. Tracks all youth related data.

The chosen app starts immediately behind an HTTPS front end and opens its own
setup wizard. All three are free, open source, run entirely on the Pi, and
never send your congregation's records anywhere.

Open Church OS is an unofficial build on top of Raspberry Pi OS and is not
affiliated with or endorsed by Raspberry Pi Ltd.

## For churches: flashing the image

1. Download the latest `.img.xz` from the Releases page.
2. Open Raspberry Pi Imager, choose **"Use custom"** and pick the file.
3. Flash to a microSD card (32 GB+), put it in the Pi, power on.
4. Wait about two minutes, then browse to `http://openchurch.local` from any
   device on the same network. (If that name doesn't resolve, find the Pi's
   IP on your router and use that.)
5. Pick your app. You'll land on `https://openchurch.local/` and your browser
   will show a one-time security warning — see below. Run the app's setup
   wizard. Done.

**About the browser warning.** The connection is encrypted, but the
certificate is self-signed: this server lives on your church network, not the
public internet, so no public authority can vouch for the name. Click
"Advanced" and proceed; each device only asks once. Encrypted-with-a-warning
beats unencrypted, which is what most LAN tools give you.

**Do first-boot setup on a trusted network.** Until a choice is made, the
setup page answers to anyone on the network. Plug the Pi into the office
switch or a private network for its first boot, make the choice, and only
then put it wherever it will live. After the choice, the setup page is
permanently retired.

## How the system is put together

- Each app runs under its **own service account** (`oc-onechurch`,
  `oc-congman`, `oc-cym`) with its own database directory
  (`/var/lib/openchurch/<app>`, mode 0700), its own backups
  (`/var/backups/openchurch/<app>`), and its own environment file
  (`/etc/openchurch/<app>.env`).
- Each service is **sandboxed by systemd**: read-only view of the system,
  write access only to its own state directories, no privilege escalation.
  A compromised app cannot read the other apps' data.
- **Only the selected app is enabled.** The other two are installed but
  disabled — not hidden, disabled — and their service accounts can't be
  logged into.
- Apps bind to **localhost only**. nginx owns ports 80 and 443, terminates
  HTTPS, adds security headers, and proxies to the selected app.
- The first-boot choice is recorded in root-owned `/etc/openchurch/selected`.
  The chooser's systemd unit refuses to start while that file exists, so the
  "pick an app" page cannot reappear without root intervention.

## Administration

`sudo openchurch` on the Pi (via SSH or keyboard):

- `sudo openchurch status` — what's selected, what's running.
- `sudo openchurch switch <app>` — change which app runs. Root-only,
  interactive confirmation required. **Read the next section first.**
- `sudo openchurch chooser-reset` — wipe the selection and re-arm the
  first-boot page. For re-provisioning a device, not everyday use.

## Switching is not migration

These are different things, and the tooling treats them differently:

**Switching** (`sudo openchurch switch`) changes which app answers the web
address. It moves **no data**. Each app keeps its own separate records; the
app you switch to starts with whatever it had before, or empty. The old
app's data stays untouched on disk in case you switch back. The switch
command makes you read exactly this warning and type the app's name to
confirm.

**Migration** moves records between apps, and it's a deliberate workflow
with a backup, a compatibility check, a preview, and a way back. **The one
supported migration is Church Youth Manager into One Church:**

1. `sudo openchurch status` and take stock. Back up CYM's data:
   `sudo cp /var/lib/openchurch/cym/data.db /root/cym-backup.db`
2. Switch to One Church and run its setup wizard if it's new.
3. Preview the import (matches students by name AND birthdate, warns loudly
   about anything ambiguous):
   `cd /opt/openchurch/one-church && sudo -u oc-onechurch OPENCHURCH_DATA_DIR=/var/lib/openchurch/one-church python3 import_youth_manager.py --dry-run /var/lib/openchurch/cym/data.db`
4. Run it without `--dry-run`, then spot-check students in the app.
5. Rollback plan: One Church takes a backup you can restore from
   `/var/backups/openchurch/one-church`, and the untouched CYM data means
   switching back loses nothing.

Every other direction (One Church back to CYM, anything into Congregation
Manager) is **unsupported** — not "hidden," unsupported. Ask before
improvising.

## Optional remote access (Tailscale)

Tailscale is baked into the image but OFF. Turned on, it lets specific,
invited devices (the minister's laptop, the treasurer's PC) reach the app
from anywhere, encrypted end to end, with no ports opened to the internet
and no changes to the church router.

Be clear-eyed about the sentence it changes. Off: records are reachable
only from the building's network. On: records are reachable by exactly the
devices invited to your Tailscale network. Both are strong positions; they
are different promises, and whoever answers for the data should know which
one is being made.

To enable, at the Pi (SSH or keyboard):

```bash
sudo openchurch remote on
```

It asks for confirmation, then prints a QR code: scan it with your phone's
camera, sign in, done. (A plain login link is printed too if you'd rather
click.)
Use a church-owned account, not a personal one that leaves when a person
does. Manage which devices are invited at login.tailscale.com. Turn it off
any time with `sudo openchurch remote off`; check with
`sudo openchurch remote status`.

Note for remote users: the browser's certificate warning appears over
Tailscale too, same reason, same one-time acceptance per device.

## For an existing Raspberry Pi OS install

```bash
curl -fsSL https://raw.githubusercontent.com/primo689/openchurch-os/main/install.sh | sudo bash
```

Same architecture, no reflash.

## Building the image

Built by [pi-gen](https://github.com/RPi-Distro/pi-gen) (the official
Raspberry Pi OS build tool) with one custom stage — see `stage-openchurch/`.
GitHub Actions does the work:

- **Manual:** Actions tab -> "Build Open Church OS image" -> Run workflow.
  Image appears as an artifact (kept 14 days).
- **Release:** `git tag v1.0.0 && git push --tags` builds and attaches the
  image to a GitHub release. Builds take roughly 30-60 minutes.

## Updating

OS security updates: `sudo apt update && sudo apt full-upgrade`, like any Pi.
App updates: replace the app's folder under `/opt/openchurch/` (code only —
all data lives in `/var/lib/openchurch/`, which updates never touch), then
`sudo systemctl restart openchurch-<app>`. New image releases: update the
app folders in this repo, tag, and the image rebuilds itself.

## Repo layout

```
.github/workflows/build-image.yml    CI that builds the flashable image
stage-openchurch/                    pi-gen custom stage
  00-install-apps/
    00-packages                      apt packages baked into the image
    01-run.sh                        install script run during image build
    files/opt/openchurch/            the three apps + the chooser
    files/etc/openchurch/            env files, nginx template
    files/etc/systemd/system/        sandboxed service units
    files/usr/local/bin/openchurch   admin CLI
install.sh                           installer for existing Pi OS installs
```
