**English** | [简体中文](README.zh-CN.md)

# nightcord-panopticon

> Nightcord Series · Self-hosted server monitoring dashboard

A lightweight, self-hosted global monitoring dashboard. Machine metrics come from **青源 (Qingyuan)**:
a self-hosted metrics agent you one-click-deploy (over SSH) from the settings page to any Linux box. It
reports per-mount disk usage, CPU/memory, network throughput, swap, load, process and connection counts,
and boot time; going offline, crossing a threshold, or rebooting sends a Feishu card / email. A separate
lightweight reporter can feed ESXi hosts into the same pipeline via vSphere API without installing
anything on the hypervisor itself. See [QINGYUAN.zh-CN.md](QINGYUAN.zh-CN.md) and
[ESXI_QINGYUAN_MONITORING.zh-CN.md](ESXI_QINGYUAN_MONITORING.zh-CN.md) (Chinese only, commands are
copy-pasteable) for the full picture.

It can also take in a self-hosted Nightcord-Status (2702) checker, shown as its own card, with one alert
per failing target.

## Architecture

```
Browser <-- HTTP --> Flask backend (app.py)
                                 │
                                 ├─ SQLite (metrics_store.py) <-- push reports -- Qingyuan agents (one per host)
                                 │                            <-- push --------- Nightcord-Status (optional)
                                 │
                                 └─ Feishu card / email alerts (notifier.py)
```

- `app.py`: Flask service that receives Qingyuan reports, exposes `/api/status`, runs the alert loop, and serves the frontend.
- `metrics_store.py`: Qingyuan history, the list of watched machines, and 2702 pushes, kept in SQLite (`metrics.db`).
- `static/index.html`: single-page dashboard, polls `/api/status` every 15s; overview tiles, a machine table, per-machine overview / disks / history charts, and settings on the same page.
- `agent/metrics_agent.py` + `agent_deploy.py`: the Qingyuan agent itself, and the one-click SSH installer driven from the settings page.
- `esxi_reporter/esxi_metrics_reporter.py`: standalone script (runs elsewhere, e.g. on a box that already talks to vSphere) that feeds ESXi host metrics into the same `/api/metrics/report` endpoint.
- `nightcord_status_client.py`: reads Nightcord-Status's `/api/status.json` in pull mode (push mode doesn't use it).
- `notifier.py`: threshold evaluation + edge-triggered Feishu/email alerts (fires once when a problem starts, not on every check cycle), with a recovery notice when it clears.
- `webauthn_manager.py` + `static/login.html`: session-based login with optional Passkey (WebAuthn) support alongside the password.
- `sso.py`: sign-in with a Palintropic account (OIDC), with the account's tier deciding superadmin or general admin.
- `terminal.py` + `terminal_ca.py`: the web terminal (WebSocket ⇄ SSH) with short-lived SSH certificates, one-time tickets and audit pushes.

General setup guide (not tied to a specific server) at [SETUP.md](SETUP.md); a worked example (WireGuard mesh, systemd, Nginx+HTTPS) at [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) (Chinese only, commands are still copy-pasteable).

## Usage

### 1. Install dependencies

```bash
cd nightcord-panopticon
pip install -r requirements.txt
```

### 2. Configure

Two options:

**Option A: the settings page (recommended)** — start the service (next step) and open
`http://127.0.0.1:1810`. On first run the settings page asks you to set a login password, alert
thresholds and Feishu/email notifications; everything is saved immediately. Come back any time; when
editing, leave secret fields blank to keep them unchanged. (The old `/setup` and `/settings` addresses
still forward there.)

**Option B: edit the YAML by hand**
```bash
cp config.example.yaml config.yaml
```
Fill it in following the comments (`dashboard_auth`'s password needs a hash from `gen_password_hash.py`).

### 3. Run

```bash
python app.py
```

Open `http://127.0.0.1:1810` in your browser.

### 4. Install the Qingyuan agent on your servers

Settings → 部署 (Deploy): enter the target's IP, SSH user and password to install the agent as a systemd
service. When the agent runs on another machine, set `metrics_agent.report_url` in `config.yaml` to
Panopticon's internal address first; see "agent 怎么连回 Panopticon" in [QINGYUAN.zh-CN.md](QINGYUAN.zh-CN.md)
for why. A machine shows up on the dashboard after its first report.

## Security notes

- `config.yaml` holds plaintext secrets — keep it out of public repos (already excluded via `.gitignore`).
- Don't run Flask's dev server in production; use `gunicorn -c gunicorn.conf.py app:app` (one worker, threads) behind a reverse proxy instead.

### Recommended architecture: agents report over WireGuard

Panopticon and every server join the same WireGuard subnet (e.g. `10.10.0.0/24`). Agents report to
Panopticon's WireGuard address (`metrics_agent.report_url`, e.g. `http://10.10.0.1:1810`), so reports
never cross the public internet.

```
[Qingyuan agent, one per server] --WireGuard--> [Panopticon :1810] <--browser-- [you]
```

1. **Only allow 1810 from WireGuard**, on the server running Panopticon:
   ```bash
   ufw allow in on wg0 to any port 1810 proto tcp
   ```
   Don't open 1810 to the public: the report endpoint is guarded only by a shared secret, so anyone
   holding it could push fake data.
2. **How you reach the dashboard**: over WireGuard is simplest. For long-term public access, put Nginx +
   HTTPS in front and **turn on the login in the next section**. Don't proxy `/api/metrics/report`,
   `/api/status/report` or `/api/agents/heartbeat` to the public (they skip the login and check only a
   secret / token); keep them reachable from WireGuard only. If the proxy runs on another machine, list
   its address in `dashboard_auth.trusted_proxies`, or every visitor looks like the proxy and one wrong
   password locks everyone out.

### Login + brute-force protection on Panopticon itself

`app.py` gates every route behind a session-based login (password, with optional Passkey /
WebAuthn as a second way in — fingerprint, Face ID, security key), plus per-IP failure lockout.
Disabled by default (`dashboard_auth.enabled: false`). **Turn it on before exposing this
publicly**:

```bash
python gen_password_hash.py "your password"   # generates a hash — never put a plaintext password in config.yaml
```

Paste the output into `config.yaml`:

```yaml
dashboard_auth:
  enabled: true
  username: "mizuki"
  password_hash: "the hash from the previous step"
  max_attempts: 5        # lock an IP out after this many consecutive failures
  lockout_seconds: 900   # lockout duration in seconds, default 15 minutes
```

Behavior: signed out, the home page shows only the first Status page — machine names, online or not,
and a rough state colour (from `/api/public/status`, worked out on the server, with no numbers, IPs,
mount points, error text or history) — with no way into details and a 登录 button; other pages
redirect to `/login` and other API calls get `401`. Once one IP
accumulates `max_attempts` failures, every subsequent request from it — even with the correct
password — gets a `429` until the lockout window passes. Lockout state lives in memory and resets
when the dashboard restarts. Passkeys are registered from Settings → 登录 (Login) after your first
password login, and require HTTPS (`localhost`/`127.0.0.1` excluded) since WebAuthn won't work over plain
HTTP otherwise.

If you only ever access Panopticon from inside WireGuard and don't plan to expose it publicly, leave `enabled: false` and skip the login prompt entirely.

### Sign in with a Palintropic account (SSO)

The `sso` section of `config.yaml` adds Palintropic accounts (OIDC authorization code + PKCE, confidential
client). With it on, signing in is always required (whatever `dashboard_auth.enabled` says), the login page
leads with "用 Palintropic 账户登录", and the local password / Passkey become the fallback.

- The tier comes from the ID token's `roles` claim: `panopticon:superadmin` can do everything;
  `panopticon:admin` sees the dashboard, machine details and history, and gets `403` on settings, secrets,
  deploying / removing agents and removing machines. Accounts with neither cannot sign in.
- A missing `roles` claim refuses the sign-in (it means the SSO token hook did not run) rather than being
  read as "no tier".
- The ID token's signature (RS256 / ES256 / EdDSA only), `iss`, `aud`, `exp` and `nonce` are checked; no
  refresh token is requested and the access token is never used as proof of identity.
- After `session_minutes` (default 60) the dashboard goes back to SSO on its own, which looks the tier up
  again, so a revoked tier takes effect within that time. A live Palintropic session makes this silent.
- Signing out also signs out of Palintropic (needs `post_logout_redirect_uri` and SSO being reachable at the
  time; otherwise only the dashboard session ends, and the login page says so).
- The issuer must be https (localhost excepted) and every discovery address must share its origin, so the
  client secret is never sent anywhere else.
- Only one callback address is registered. Starting the account sign-in from another address (e.g. the
  WireGuard IP) first moves to the callback's host.
- The local password / Passkey sign in as superadmin (when SSO is unreachable only the site owner gets in);
  with SSO on, such a session ends after 12 hours.

Create the client on the Palintropic side with `bin/palid client`; `redirect_uri` must match what is
registered there exactly.

### Web terminal

The dashboard's 终端 page opens an SSH terminal to a machine in the browser; see the `terminal` section of
`config.example.yaml`.

- Only Palintropic sign-ins can open one (not the local emergency login), and **every opening** goes back
  to Palintropic to verify again, with a passkey this time (the last entry of the ID token's `amr`, and an
  `auth_time` no earlier than the request). The machines come from that fresh token's `ssh_hosts`, so a
  host taken away in SSO is refused at the next opening.
- That yields a one-time ticket (bound to the dashboard session, the account and the machine, 60 s) used up
  by the WebSocket handshake, which also checks Origin.
- Logins use a freshly signed short-lived certificate: valid 5 minutes, for the configured account only,
  only from Panopticon's WireGuard address, with `permit-pty` only (no port, agent or X11 forwarding). Each
  target needs `deploy/terminal-host-setup.sh` once and keeps no long-lived key. Host keys are pinned in
  the config; the one-click agent deploy also checks a pinned key before sending a password.
- Idle disconnect after 15 minutes, 2 hours at most, 3 per person and 8 in total at a time.
- Every opening and closing is pushed to Feishu and Slack (retried in the background, and sessions left
  open by a restart are closed as unknown). The local database can be edited from a terminal on that
  machine, so the pushed copy is the record. No screen or keystroke recording.
- WebSockets need `gunicorn -c gunicorn.conf.py app:app` in production.

## Ideas for extension

- Service / certificate checks: HTTP, Ping, TCPing and certificate expiry (check the overlap with Nightcord-Status first).
- Windows agent support: `agent/metrics_agent.py` already uses cross-platform `psutil`, but the one-click installer (`agent_deploy.py`) is SSH + systemd only, and the disk collector assumes POSIX mount points.
- Network throughput for the ESXi reporter: currently reports `0`, since it needs `PerformanceManager` counters instead of the simpler `quickStats` fields used for CPU/memory (see the "known limitations" section in [ESXI_QINGYUAN_MONITORING.zh-CN.md](ESXI_QINGYUAN_MONITORING.zh-CN.md)).

## License

MIT © Akiyama Mizuki · Nightcord Series
