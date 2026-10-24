#!/bin/bash
# One-time setup on each machine the web terminal can open: trust Panopticon's SSH CA for one account.
#
#   sudo bash terminal-host-setup.sh <account> "<CA public key line>" [--replace-ca]
#
# Get the CA line on the Panopticon host with: .venv/bin/python terminal_ca.py pubkey
#
# What it changes (and nothing else):
#   /etc/ssh/panopticon_ca.pub                          the CA public key
#   /etc/ssh/panopticon_principals/<account>            the only principal accepted for that account
#   /etc/ssh/sshd_config.d/60-panopticon-terminal.conf  TrustedUserCAKeys + AuthorizedPrincipalsFile
# Forwarding is not touched here: certificates carry only permit-pty, so terminal logins get no port,
# agent or X11 forwarding while the account's other logins keep working as before.
#
# Safe to run again with the same CA. A different CA is refused unless --replace-ca is given: switching
# takes effect at once, and certificates from the old CA stop working (there is no grace period).
# Existing files are backed up first; if any step fails (a write, `sshd -t`, the reload), everything is
# put back, and sshd is reloaded onto the old files if it had already been reloaded.
# Undo: delete the three paths above and reload sshd.
set -euo pipefail

account="${1:?account}"
ca_line="${2:?CA public key line}"
replace_ca="${3:-}"
[[ "$account" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "bad account name" >&2; exit 2; }
[[ "$ca_line" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || { echo "not an ssh-ed25519 public key line" >&2; exit 2; }
[[ -z "$replace_ca" || "$replace_ca" == "--replace-ca" ]] || { echo "unknown option: $replace_ca" >&2; exit 2; }
id "$account" >/dev/null

conf_dir=/etc/ssh/sshd_config.d
ca_file=/etc/ssh/panopticon_ca.pub
principals_dir=/etc/ssh/panopticon_principals
principals_file="$principals_dir/$account"
conf_file="$conf_dir/60-panopticon-terminal.conf"

grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config \
  || { echo "/etc/ssh/sshd_config does not Include $conf_dir/*.conf; add the two lines by hand" >&2; exit 3; }
if grep -RqiE '^[[:space:]]*(TrustedUserCAKeys|AuthorizedPrincipalsFile)' /etc/ssh/sshd_config "$conf_dir" 2>/dev/null \
   && ! grep -qs 'panopticon' "$conf_file"; then
  echo "TrustedUserCAKeys / AuthorizedPrincipalsFile already set elsewhere; merge by hand" >&2; exit 3
fi
new_key=$(cut -d' ' -f1-2 <<<"$ca_line")
if [[ -f "$ca_file" && "$(cut -d' ' -f1-2 "$ca_file")" != "$new_key" && "$replace_ca" != "--replace-ca" ]]; then
  echo "$ca_file already holds a different CA; pass --replace-ca to switch (old certificates stop working)" >&2
  exit 4
fi

# Back up whatever exists, so any failure (a write, `sshd -t`, the reload) puts things back exactly.
backup=$(mktemp -d /etc/ssh/.panopticon-backup.XXXXXX)
created_principals_dir=0
[[ -d "$principals_dir" ]] || created_principals_dir=1
for f in "$ca_file" "$principals_file" "$conf_file"; do
  [[ -e "$f" ]] && cp -a "$f" "$backup/$(basename "$f")"
done
committed=0
rollback() {
  [[ $committed == 1 ]] && { rm -rf "$backup"; return; }
  echo "setup failed; restoring the previous files" >&2
  for f in "$ca_file" "$principals_file" "$conf_file"; do
    if [[ -e "$backup/$(basename "$f")" ]]; then cp -a "$backup/$(basename "$f")" "$f"; else rm -f "$f"; fi
  done
  [[ $created_principals_dir == 1 ]] && rmdir "$principals_dir" 2>/dev/null
  rm -f /etc/ssh/.panopticon.* "$conf_dir"/.panopticon.* "$principals_dir"/.panopticon.* 2>/dev/null
  rm -rf "$backup"
  if [[ $reloaded == 1 ]]; then reload_sshd || true; fi   # put the running sshd back on the old files
}
reloaded=0
trap rollback EXIT

write() {  # write <path> <content>: via a temporary file in the same directory, then rename
  local tmp
  tmp=$(mktemp "$(dirname "$1")/.panopticon.XXXXXX")
  printf '%s\n' "$2" > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$1"
}

reload_sshd() {
  if command -v systemctl >/dev/null && systemctl is-active --quiet ssh 2>/dev/null; then
    systemctl reload ssh
  elif command -v systemctl >/dev/null && systemctl is-active --quiet sshd 2>/dev/null; then
    systemctl reload sshd
  else
    echo "config written and checked; reload sshd yourself"
  fi
}

install -d -m 0755 "$principals_dir"
write "$ca_file" "$ca_line"
write "$principals_file" "$account"
write "$conf_file" "# Panopticon web terminal: logins with a certificate from its CA, for the principals listed per account.
TrustedUserCAKeys $ca_file
AuthorizedPrincipalsFile $principals_dir/%u"

sshd -t
reloaded=1
reload_sshd
committed=1

echo "host key for Panopticon's config (terminal.hosts.<id>.host_key):"
cat /etc/ssh/ssh_host_ed25519_key.pub
