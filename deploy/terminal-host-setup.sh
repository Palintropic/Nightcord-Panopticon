#!/bin/bash
# One-time setup on each machine the web terminal can open: trust Panopticon's SSH CA for one account.
#
#   sudo bash terminal-host-setup.sh <account> "<CA public key line>"
#
# Get the CA line on the Panopticon host with: .venv/bin/python terminal_ca.py pubkey
#
# What it changes (and nothing else):
#   /etc/ssh/panopticon_ca.pub                          the CA public key
#   /etc/ssh/panopticon_principals/<account>            the only principal accepted for that account
#   /etc/ssh/sshd_config.d/60-panopticon-terminal.conf  TrustedUserCAKeys + AuthorizedPrincipalsFile
# Forwarding is not touched here: certificates carry only permit-pty, so terminal logins get no port,
# agent or X11 forwarding while the account's other logins keep working as before.
# Undo: delete those three paths and reload sshd.
set -euo pipefail

account="${1:?account}"
ca_line="${2:?CA public key line}"
[[ "$account" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "bad account name" >&2; exit 2; }
[[ "$ca_line" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || { echo "not an ssh-ed25519 public key line" >&2; exit 2; }
id "$account" >/dev/null

conf_dir=/etc/ssh/sshd_config.d
grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config \
  || { echo "/etc/ssh/sshd_config does not Include $conf_dir/*.conf; add the two lines by hand" >&2; exit 3; }
if grep -RqiE '^[[:space:]]*(TrustedUserCAKeys|AuthorizedPrincipalsFile)' /etc/ssh/sshd_config "$conf_dir" 2>/dev/null \
   && ! grep -qs 'panopticon' "$conf_dir/60-panopticon-terminal.conf"; then
  echo "TrustedUserCAKeys / AuthorizedPrincipalsFile already set elsewhere; merge by hand" >&2; exit 3
fi

install -d -m 0755 /etc/ssh/panopticon_principals
printf '%s\n' "$ca_line" > /etc/ssh/panopticon_ca.pub
printf '%s\n' "$account" > "/etc/ssh/panopticon_principals/$account"
chmod 0644 /etc/ssh/panopticon_ca.pub "/etc/ssh/panopticon_principals/$account"
cat > "$conf_dir/60-panopticon-terminal.conf" <<'CONF'
# Panopticon web terminal: logins with a certificate from its CA, for the principals listed per account.
TrustedUserCAKeys /etc/ssh/panopticon_ca.pub
AuthorizedPrincipalsFile /etc/ssh/panopticon_principals/%u
CONF
chmod 0644 "$conf_dir/60-panopticon-terminal.conf"

sshd -t
if command -v systemctl >/dev/null && systemctl is-active --quiet ssh 2>/dev/null; then
  systemctl reload ssh
elif command -v systemctl >/dev/null && systemctl is-active --quiet sshd 2>/dev/null; then
  systemctl reload sshd
else
  echo "config written and checked; reload sshd yourself"
fi
echo "host key for Panopticon's config (terminal.hosts.<id>.host_key):"
cat /etc/ssh/ssh_host_ed25519_key.pub
