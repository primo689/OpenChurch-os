#!/bin/bash -e
# Install the Open Church apps, the chooser, the admin CLI, and services.

cp -r files/opt "${ROOTFS_DIR}/"
cp -r files/etc "${ROOTFS_DIR}/"
cp -r files/usr "${ROOTFS_DIR}/"

on_chroot << CHROOT
# One service account PER APP. Isolation is the point: a compromised app
# runs as its own user and its unit's sandbox only allows writes to its
# own state directories, so it cannot casually reach the other apps' data.
for u in oc-onechurch oc-congman oc-cym; do
    if ! id "\$u" >/dev/null 2>&1; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "\$u"
    fi
done

# App code: root-owned and read-only to the apps. State lives elsewhere.
chown -R root:root /opt/openchurch
chmod -R a+rX,go-w /opt/openchurch
chmod +x /usr/local/bin/openchurch

# Per-app state and backup directories, private to each account (0700).
install -d -o oc-onechurch -g oc-onechurch -m 700 /var/lib/openchurch/one-church
install -d -o oc-congman   -g oc-congman   -m 700 /var/lib/openchurch/congman
install -d -o oc-cym       -g oc-cym       -m 700 /var/lib/openchurch/cym
install -d -o oc-onechurch -g oc-onechurch -m 700 /var/backups/openchurch/one-church
install -d -o oc-congman   -g oc-congman   -m 700 /var/backups/openchurch/congman

# Config is root's. Env files hold no secrets, but nobody else writes them.
chown -R root:root /etc/openchurch
chmod 755 /etc/openchurch
chmod 644 /etc/openchurch/*.env /etc/openchurch/nginx-site.template

# First boot: only the chooser answers. The apps and nginx stay disabled
# until a choice is recorded; the chooser's unit refuses to start once
# /etc/openchurch/selected exists.
systemctl enable openchurch-chooser
systemctl disable openchurch-one-church openchurch-congman openchurch-cym 2>/dev/null || true
systemctl disable nginx 2>/dev/null || true

# http://openchurch.local instead of hunting for an IP address.
systemctl enable avahi-daemon

# Tailscale: baked in, OFF by default. Enabling it is a deliberate root
# action on the device (sudo openchurch remote on), never a checkbox.
curl -fsSL https://pkgs.tailscale.com/stable/raspbian/bookworm.noarmor.gpg \
    > /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/raspbian/bookworm.tailscale-keyring.list \
    > /etc/apt/sources.list.d/tailscale.list
apt-get update
apt-get install -y tailscale
systemctl disable tailscaled 2>/dev/null || true
CHROOT
