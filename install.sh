#!/bin/bash -e
# Open Church installer for an EXISTING Raspberry Pi OS install.
# (If you're starting fresh, flashing the Open Church OS image is easier.)
#
#   curl -fsSL https://raw.githubusercontent.com/YOURUSER/openchurch-os/main/install.sh | sudo bash
#
# Installs all three apps with per-app service accounts and sandboxed
# services, then starts the chooser on port 80. After you pick an app,
# nginx takes over ports 80/443 with HTTPS.

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: curl -fsSL ... | sudo bash"
    exit 1
fi

REPO="${OPENCHURCH_REPO:-https://github.com/YOURUSER/openchurch-os}"
SRC=/tmp/openchurch-src
F="$SRC/stage-openchurch/00-install-apps/files"

echo "Installing packages..."
apt-get update -qq
apt-get install -y -qq git python3-flask python3-waitress avahi-daemon nginx-light ssl-cert curl ca-certificates

echo "Adding Tailscale (installed but OFF until 'sudo openchurch remote on')..."
curl -fsSL https://pkgs.tailscale.com/stable/raspbian/bookworm.noarmor.gpg \
    > /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/raspbian/bookworm.tailscale-keyring.list \
    > /etc/apt/sources.list.d/tailscale.list
apt-get update -qq
apt-get install -y -qq tailscale
systemctl disable --now tailscaled 2>/dev/null || true

echo "Fetching apps..."
rm -rf "$SRC"
git clone --depth 1 "$REPO" "$SRC"

echo "Installing..."
cp -r "$F/opt/openchurch" /opt/
cp "$F"/etc/systemd/system/*.service /etc/systemd/system/
mkdir -p /etc/openchurch
cp "$F"/etc/openchurch/* /etc/openchurch/
cp "$F/usr/local/bin/openchurch" /usr/local/bin/
chmod +x /usr/local/bin/openchurch

for u in oc-onechurch oc-congman oc-cym; do
    id "$u" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$u"
done
chown -R root:root /opt/openchurch
chmod -R a+rX,go-w /opt/openchurch
install -d -o oc-onechurch -g oc-onechurch -m 700 /var/lib/openchurch/one-church
install -d -o oc-congman   -g oc-congman   -m 700 /var/lib/openchurch/congman
install -d -o oc-cym       -g oc-cym       -m 700 /var/lib/openchurch/cym
install -d -o oc-onechurch -g oc-onechurch -m 700 /var/backups/openchurch/one-church
install -d -o oc-congman   -g oc-congman   -m 700 /var/backups/openchurch/congman
chown -R root:root /etc/openchurch
chmod 755 /etc/openchurch
chmod 644 /etc/openchurch/*.env /etc/openchurch/nginx-site.template

systemctl daemon-reload
systemctl disable nginx 2>/dev/null || true
systemctl stop nginx 2>/dev/null || true
systemctl enable --now openchurch-chooser

rm -rf "$SRC"
IP=$(hostname -I | awk '{print $1}')
echo
echo "Done. From any device on this network, open:  http://${IP}/"
echo "and pick which app this Pi should run. Admin tool: sudo openchurch"
