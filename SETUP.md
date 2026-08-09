**English** | [简体中文](SETUP.zh-CN.md)

# Setup Guide

A general guide for anyone deploying this project (not tied to any specific server or IP). If you
want to see a concrete worked example, check [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) (Chinese only) — it
walks through a real "Shanghai + Japan, two BT Panel servers" setup. This document is the generic
version.

## Contents

- [Requirements](#requirements)
- [Quick start (local trial)](#quick-start-local-trial)
- [Step 1: Enable the API on BT Panel](#step-1-enable-the-api-on-bt-panel)
- [Step 2: Configure your panel list](#step-2-configure-your-panel-list)
- [Step 3: Pick a security tier](#step-3-pick-a-security-tier)
- [Step 4: Alerting (optional)](#step-4-alerting-optional)
- [Production deployment](#production-deployment)
- [FAQ](#faq)

---

## Requirements

- Python 3.8+
- One or more servers running BT Panel (any recent version — field names may vary slightly by version, but the core endpoints are stable)
- Network reachability from the machine running this dashboard to each panel's admin port (same machine, LAN, or VPN all work)

## Quick start (local trial)

```bash
git clone https://github.com/Akiyama-Mizuki-44/Nightcord-Panopticon.git
cd Nightcord-Panopticon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:1810` in your browser. Since nothing is configured yet, it'll redirect you to `/setup`.

## Step 1: Enable the API on BT Panel

For every panel you want to connect:

1. Panel dashboard → Settings → API interface → turn it on, copy the API secret key.
2. On the same page, set the **IP whitelist** to the IP of the machine running this dashboard (not the panel's own IP).
   - Same machine as the panel → `127.0.0.1`.
   - Different machine → whatever IP the panel sees that machine as (public IP, or an internal VPN/WireGuard IP if you've set one up — see the security tiers below).
3. (Optional but recommended) move the panel's admin port off the default `8888` to cut down on opportunistic scanning.

## Step 2: Configure your panel list

Two options:

**Option A: the visual settings page (recommended)**
Start the service, open `http://<dashboard-address>:1810/setup`, fill in each panel's name/URL/API key, and save. It stays available afterward at `/settings` — come back any time to add panels, change alerting, or change the login password. When editing, leave secret fields blank to keep the existing value; saving never blanks out a stored secret.

**Option B: edit the YAML by hand**
```bash
cp config.example.yaml config.yaml
```
Fill in `panels` / `dashboard_auth` / `notifications` per the comments in the file. The login password can't be plaintext — generate a hash first with `python gen_password_hash.py "your password"` and paste that in.

Both options write to the same `config.yaml`, so you can mix and match — configure once through the UI, then hand-edit the YAML later for bulk changes (restart the process to pick them up).

## Step 3: Pick a security tier

The moment this dashboard is running, it holds credentials that can call every configured panel's API. Pick whichever tier matches your situation:

| Situation | Approach |
|---|---|
| Just for you, dashboard and panels are on the same LAN or machine | `dashboard_auth.enabled: false` is fine — network unreachability is your protection, no need for HTTPS/VPN |
| Dashboard needs to be public long-term, and you're okay with panels being public too | Turn on `dashboard_auth` (built-in login + brute-force lockout) + put Nginx + HTTPS in front, and lock each panel's IP whitelist down to the dashboard's fixed IP |
| Dashboard needs to be public, but panel admin ports should never be reachable at all | Same as above, plus a private overlay network (WireGuard, Tailscale, etc.) between the dashboard and each panel, with panel admin ports firewalled to that private subnet only. See the WireGuard section of [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) for a worked example |

Whichever tier you're on, if the dashboard will ever be reached from the public internet, `dashboard_auth.enabled` must be on:

```bash
python gen_password_hash.py "your password"
```
Paste the output into `config.yaml` (or just set the password directly from the `/settings` page — easier).

## Step 4: Alerting (optional)

Supports a Feishu custom-bot webhook and SMTP email, both configurable from `/settings` or the `notifications` section of `config.yaml`. Triggers on a panel going offline, or CPU/memory/disk usage crossing your configured threshold; each distinct alert has a cooldown to avoid spamming.

## Production deployment

Don't leave `python app.py` running in a foreground terminal long-term:

```bash
pip install gunicorn
gunicorn -w 2 -b 127.0.0.1:1810 app:app
```

A full example with systemd process management and Nginx + Let's Encrypt HTTPS is in step 3 of [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) (Chinese, but the commands are copy-pasteable).

> Note: Gunicorn only serves HTTP. If you want both Gunicorn and alerting, run `python worker.py` as a second service; it runs the periodic jobs without binding another HTTP port (also covered in DEPLOY.zh-CN.md).

## FAQ

**Getting a signature error / failed request from a panel?**
Usually a clock skew issue (the signing scheme uses a timestamp) — check that the dashboard's and the panel's system clocks are in sync (`timedatectl` / NTP).

**Whose IP goes in the panel's IP whitelist?**
The IP of the machine *making* the API request — i.e. wherever this dashboard runs — not the panel's own IP.

**Saved settings but nothing changed?**
Saving via `/settings` clears the in-memory cache, so the next refresh picks up the new config automatically — no restart needed. If you hand-edited `config.yaml` instead, that also doesn't need a restart, since `app.py` re-reads the file on every request.

**Forgot the dashboard login password?**
Edit `config.yaml` directly, set `dashboard_auth.enabled` to `false`, restart once, log into `/settings` (no password needed now), generate a new hash with `gen_password_hash.py`, paste it in, and flip `enabled` back to `true`.
