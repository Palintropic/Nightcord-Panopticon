**English** | [简体中文](SETUP.zh-CN.md)

# Setup Guide

A general guide for anyone deploying this project (not tied to any specific server or IP). If you
want to see a concrete worked example, check [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) (Chinese only) — it
walks through a real Shanghai + Tokyo setup over WireGuard. This document is the generic version.

## Contents

- [Requirements](#requirements)
- [Quick start (local trial)](#quick-start-local-trial)
- [Step 1: Configure the dashboard](#step-1-configure-the-dashboard)
- [Step 2: Install the Qingyuan agent](#step-2-install-the-qingyuan-agent)
- [Step 3: Pick a security tier](#step-3-pick-a-security-tier)
- [Step 4: Alerting (optional)](#step-4-alerting-optional)
- [Production deployment](#production-deployment)
- [FAQ](#faq)

---

## Requirements

- Python 3.8+ for the dashboard
- Linux servers to monitor, with `python3` (including the `venv` module) and `systemd` for the one-click agent installer, and SSH access to them
- A network path from every monitored server to the dashboard's port 1810 (same machine, LAN, or a VPN such as WireGuard)

## Quick start (local trial)

```bash
git clone https://github.com/Akiyama-Mizuki-44/Nightcord-Panopticon.git
cd Nightcord-Panopticon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:1810` in your browser. Since nothing is configured yet, the settings page asks you to set things up first.

## Step 1: Configure the dashboard

Two options:

**Option A: the settings page (recommended)**
Start the service, open `http://<dashboard-address>:1810`, and fill in the login password, alert thresholds and notifications from Settings. Come back any time to change them. When editing, leave secret fields blank to keep the existing value; saving never blanks out a stored secret.

**Option B: edit the YAML by hand**
```bash
cp config.example.yaml config.yaml
```
Fill in `dashboard_auth` / `notifications` / `metrics_agent` per the comments in the file. The login password can't be plaintext — generate a hash first with `python gen_password_hash.py "your password"` and paste that in.

Both options write to the same `config.yaml`, so you can mix and match — configure once through the UI, then hand-edit the YAML later for bulk changes.

## Step 2: Install the Qingyuan agent

If the agent will run on a different machine from the dashboard, first set `metrics_agent.report_url` in `config.yaml` to the address that machine can reach the dashboard on (e.g. `http://10.10.0.1:1810` over WireGuard). Without it the installer guesses from the address you opened the settings page with, which is wrong when you came in through a tunnel or a reverse proxy.

Then Settings → 部署 (Deploy): enter the server's IP, SSH port, user and password, and install. The log streams while it runs. Tick "remember the password" to be able to redeploy it later with one click (needed to pick up agent updates). The machine appears on the dashboard after its first report, about a minute later.

Manual installation and the agent's own config are covered in [agent/README.zh-CN.md](agent/README.zh-CN.md); ESXi hosts are fed in by a separate reporter, see [ESXI_QINGYUAN_MONITORING.zh-CN.md](ESXI_QINGYUAN_MONITORING.zh-CN.md).

## Step 3: Pick a security tier

The dashboard stores SSH passwords you chose to remember and the shared secret agents report with. Pick whichever tier matches your situation:

| Situation | Approach |
|---|---|
| Just for you, dashboard and servers on the same LAN or machine | `dashboard_auth.enabled: false` is fine — network unreachability is your protection, no need for HTTPS/VPN |
| Servers in different places, dashboard reached only over a VPN | Agents report over a private overlay network (WireGuard, Tailscale, etc.); firewall port 1810 to that subnet only. See [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) for a WireGuard example |
| Dashboard needs to be public long-term | Same as above, plus `dashboard_auth` (built-in login + brute-force lockout) and Nginx + HTTPS in front. Don't proxy `/api/metrics/report`, `/api/status/report` or `/api/agents/heartbeat`: they skip the login and check only a secret / token |

Whichever tier you're on, if the dashboard will ever be reached from the public internet, `dashboard_auth.enabled` must be on:

```bash
python gen_password_hash.py "your password"
```
Paste the output into `config.yaml` (or just set the password from Settings → 登录 (Login) — easier).

## Step 4: Alerting (optional)

Supports a Feishu custom-bot webhook and SMTP email, both configurable from Settings or the `notifications` section of `config.yaml`. Triggers when a machine stops reporting for 3 minutes, when CPU/memory/disk crosses your configured threshold, and when a machine reboots; a recovery notice follows when the problem clears. Each problem is sent once when it starts, not on every check.

## Production deployment

Don't leave `python app.py` running in a foreground terminal long-term:

```bash
pip install gunicorn
gunicorn -w 2 -b 127.0.0.1:1810 app:app
```

A full example with systemd process management and Nginx + Let's Encrypt HTTPS is in step 3 of [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) (Chinese, but the commands are copy-pasteable).

> Note: Gunicorn only serves HTTP. If you want both Gunicorn and alerting, run `python worker.py` as a second service; it runs the periodic jobs without binding another HTTP port (also covered in DEPLOY.zh-CN.md).

## FAQ

**Deployed the agent but the machine never shows up?**
Most often `metrics_agent.report_url` points somewhere the server can't reach. On the server, `journalctl -u nightcord-metrics-agent -f` shows each failed report; a `401` means the shared secret doesn't match, a `400` a malformed report.

**Saved settings but nothing changed?**
Saving from the settings page clears the in-memory cache, so the next refresh picks up the new config automatically — no restart needed. If you hand-edited `config.yaml` instead, that also doesn't need a restart, since `app.py` re-reads the file on every request.

**Forgot the dashboard login password?**
Edit `config.yaml` directly, set `dashboard_auth.enabled` to `false`, restart once, open the settings page (no password needed now), generate a new hash with `gen_password_hash.py`, paste it in, and flip `enabled` back to `true`.
