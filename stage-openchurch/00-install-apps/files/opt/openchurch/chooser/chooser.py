#!/usr/bin/env python3
"""
Open Church OS first-boot chooser.

Serves one page on port 80 asking which app this Pi should run. When a
choice is made it:
  1. records the choice in root-owned /etc/openchurch/selected,
  2. enables that app's service (bound to localhost only),
  3. writes the nginx reverse-proxy config and hands ports 80/443 to nginx,
  4. disables itself. The unit's ConditionPathExists means it can never
     start again while the selection file exists.

Changing apps later is deliberate and root-only:  sudo openchurch switch
Standard library only, so it works before anything else is configured.
"""

import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

SELECTED = "/etc/openchurch/selected"
NGINX_TEMPLATE = "/etc/openchurch/nginx-site.template"
NGINX_SITE = "/etc/nginx/sites-available/openchurch"
NGINX_LINK = "/etc/nginx/sites-enabled/openchurch"
NGINX_DEFAULT = "/etc/nginx/sites-enabled/default"

APPS = {
    "one-church": {
        "service": "openchurch-one-church",
        "port": 8080,
        "title": "One Church",
        "desc": ("The full system: whole-congregation records, giving, attendance, "
                 "groups, AND a complete youth ministry module with check-in, "
                 "permission slips, and baptism follow-up. Church and Youth "
                 "dashboards. Pick this unless you have a reason not to."),
    },
    "congman": {
        "service": "openchurch-congman",
        "port": 8081,
        "title": "Congregation Manager",
        "desc": ("Congregation records only: members, attendance, giving, groups, "
                 "notes. No youth module. Good if youth ministry runs its own "
                 "separate system."),
    },
    "cym": {
        "service": "openchurch-cym",
        "port": 8082,
        "title": "Church Youth Manager",
        "desc": ("Youth ministry only: students, guardians, events, check-in, "
                 "permission slips. Light and focused. Good if the church office "
                 "already has other software and you just need the youth side."),
    },
}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Open Church OS — first-time setup</title>
<style>
body {{ font-family: Georgia, serif; background:#f7f6f2; color:#22252a; margin:0; padding:24px; }}
.wrap {{ max-width: 640px; margin: 0 auto; }}
h1 {{ font-size: 26px; margin-bottom: 4px; }}
.sub {{ color:#666; margin-top: 0; }}
.card {{ background:#fff; border:1px solid #ddd8cc; border-radius:8px; padding:16px 18px; margin:14px 0; }}
.card h2 {{ margin:0 0 6px; font-size:19px; }}
.card p {{ margin:0 0 12px; font-size:14.5px; line-height:1.45; }}
button {{ background:#22252a; color:#f7f6f2; border:0; border-radius:6px; padding:9px 16px;
         font-size:15px; cursor:pointer; }}
.note {{ font-size: 13px; color:#666; margin-top:20px; }}
</style></head><body><div class="wrap">
<h1>Welcome to Open Church OS</h1>
<p class="sub">One question, one time: what should this little computer do for your church?
This choice is recorded and this page won't be offered again.</p>
{cards}
<p class="note">Whichever you pick opens its own setup wizard next, where you'll name your
church and create the first login. Free and open source, no subscription, and your records
never leave this device. Switching apps later is possible but deliberate: it takes a person
at the machine, and each app keeps its own separate data. Optional remote access (Tailscale)
is included but OFF; an administrator can turn it on later from the device itself.</p>
</div></body></html>"""

CARD = """<div class="card"><h2>{title}</h2><p>{desc}</p>
<form method="post" action="/choose"><input type="hidden" name="app" value="{key}">
<button>Set up {title}</button></form></div>"""

DONE = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="12; url=https://{host}/">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Starting {title}…</title>
<style>body {{ font-family: Georgia, serif; background:#f7f6f2; color:#22252a;
padding:40px 24px; }} .wrap {{ max-width: 560px; margin: 0 auto; }}</style></head><body>
<div class="wrap">
<h1>{title} is starting…</h1>
<p>In a few seconds this page will jump to:</p>
<p style="font-size:20px"><strong><a href="https://{host}/">https://{host}/</a></strong></p>
<p><strong>Your browser will show a security warning the first time.</strong> That's
expected: the connection IS encrypted, but the certificate is self-signed because this
server lives on your church network, not the public internet, so no public authority can
vouch for it. Click "Advanced" and proceed — you only have to accept it once per device.</p>
<p style="color:#666;font-size:14px">Bookmark the address above — it's the app's home
from now on. This setup page has retired itself.</p>
</div></body></html>"""


def finalize(app):
    """Runs after the response is sent: record the choice, start the app,
    hand ports 80/443 to nginx, and retire the chooser for good."""
    time.sleep(1)
    os.makedirs("/etc/openchurch", exist_ok=True)
    with open(SELECTED, "w") as f:
        f.write(app["service"].replace("openchurch-", "") + "\n")
    os.chmod(SELECTED, 0o644)  # world-readable, root-writable

    with open(NGINX_TEMPLATE) as f:
        site = f.read().replace("__PORT__", str(app["port"]))
    with open(NGINX_SITE, "w") as f:
        f.write(site)
    if os.path.lexists(NGINX_DEFAULT):
        os.remove(NGINX_DEFAULT)
    if not os.path.lexists(NGINX_LINK):
        os.symlink(NGINX_SITE, NGINX_LINK)

    subprocess.run(["systemctl", "enable", app["service"]], check=True)
    subprocess.run(["systemctl", "start", app["service"]], check=True)
    subprocess.run(["systemctl", "disable", "openchurch-chooser"], check=False)
    subprocess.run(["systemctl", "enable", "nginx"], check=False)
    # Stopping the chooser frees port 80 for nginx. Done as a detached shell
    # so the stop doesn't kill this code before nginx is restarted.
    subprocess.Popen(["sh", "-c",
                      "systemctl stop openchurch-chooser; systemctl restart nginx"])


class Handler(BaseHTTPRequestHandler):
    def _send(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if os.path.exists(SELECTED):
            return self._send("Setup has already been completed on this device.", 410)
        cards = "".join(CARD.format(key=k, **v) for k, v in APPS.items())
        self._send(PAGE.format(cards=cards))

    def do_POST(self):
        if self.path != "/choose":
            return self._send("Not found", 404)
        if os.path.exists(SELECTED):
            return self._send("Setup has already been completed on this device.", 410)
        length = min(int(self.headers.get("Content-Length", 0)), 1000)
        form = parse_qs(self.rfile.read(length).decode())
        key = (form.get("app") or [""])[0]
        if key not in APPS:
            return self._send("Unknown choice — go back and pick one of the buttons.", 400)
        app = APPS[key]
        host = (self.headers.get("Host") or "openchurch.local").split(":")[0]
        self._send(DONE.format(host=host, title=app["title"]))
        threading.Thread(target=finalize, args=(app,), daemon=True).start()

    def log_message(self, *a):
        pass  # keep the journal quiet


if __name__ == "__main__":
    if os.path.exists(SELECTED):
        # Belt and braces: the unit's ConditionPathExists should prevent
        # this, but exit quietly if started by hand anyway.
        raise SystemExit(0)
    print("Open Church OS chooser on port 80")
    HTTPServer(("0.0.0.0", 80), Handler).serve_forever()
