"""
Nightcord-Panopticon 后端：汇总青源 agent 上报的机器指标和 Nightcord-Status 的检测结果
用法：
    pip install -r requirements.txt
    cp config.example.yaml config.yaml
    python app.py
然后浏览器打开 http://127.0.0.1:1810
"""
import hmac
import json
import os
import queue
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context, session, redirect, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_sock import Sock
from simple_websocket import ConnectionClosed
from werkzeug.security import check_password_hash

import sso
import terminal
import terminal_ca
import webauthn_manager as wam
from nightcord_status_client import NightcordStatusClient
from notifier import Notifier, evaluate_status_alerts, format_duration, nightcord_target_key_prefix, plan_agent_alerts
from auth import BruteForceGuard, ForwardedProto, get_client_ip, trusted_proxies_from
from config_editor import redact_for_display, merge_submitted, appearance_of, clean_appearance, kaomoji_of, clean_kaomoji
from agent_deploy import deploy_agent, AgentDeployError
import agent_hosts
from config_store import load_yaml, save_yaml, update_yaml
import metrics_store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


class _SessionInterface(SecureCookieSessionInterface):
    # 经 HTTPS 访问（包括受信反代转来的，见 auth.ForwardedProto）时 session cookie 带 Secure，
    # 不会再从同一个域名的 http 地址发出去；WireGuard 内网直连的 http 照常能登录。
    # 访问的是 SSO 回调所在的 https 域名时也一律带上：反代漏配 trusted_proxies 认不出 https 时不至于漏
    def get_cookie_secure(self, app):
        if request.is_secure:
            return True
        settings = _sso_settings()
        return bool(settings) and settings["redirect_uri"].startswith("https://") \
            and request.host == urlsplit(settings["redirect_uri"]).netloc


app.session_interface = _SessionInterface()
sock = Sock(app)
metrics_store.init_db()

_brute_force_guard = BruteForceGuard()  # 默认 5 次失败锁 15 分钟，实际阈值以 config.yaml 为准（见 enforce_auth）

# 不需要登录就能访问的路由：登录页本身，以及登录相关的两个 API（否则没法登录）
# setup_page / settings_page 只回一段固定的转发脚本（见 _SETTINGS_FORWARD），不带任何数据；放行是为了
# 让旧书签的 #s=... 先在浏览器里转成看板地址，没登录的话再由看板被踢去登录页，# 后面那段一路带着。
PUBLIC_ENDPOINTS = {"login_page", "login_password", "logout", "webauthn_login_options", "webauthn_login_verify",
                    "setup_page", "settings_page", "sso_login", "sso_callback", "api_login_methods",
                    # 没登录也能看的第一面：看板页本身、只有名字和状态色的 Status（颜文字随它一起给）、磁贴的颜色
                    "index", "api_public_status", "api_get_appearance"}

# 登录后分两档（见 sso.py）：超级管理员什么都能做；一般管理员只能看——下面这些是一般管理员能进的，
# 其余（设置、密钥、部署 / 移除 agent、移除机器、本地 Passkey 管理）一律只给超管。
# 用白名单而不是黑名单：以后新加的接口默认只有超管能用，忘了归类也不会漏给一般管理员。
ADMIN_ENDPOINTS = {"index", "api_me", "api_status", "api_metrics_history", "api_metrics_version",
                   "api_get_appearance", "api_get_kaomoji",
                   # 网页终端：能开哪台由 SSO 的 ssh_hosts 决定，每次开都要重新验证（见 terminal_start）
                   "api_terminal_hosts", "api_terminal_history", "terminal_start", "terminal_ws"}
ROLES = (sso.ROLE_SUPERADMIN, sso.ROLE_ADMIN)
LOCAL_SESSION_SECONDS = 12 * 3600  # 本地应急登录的会话时长，到期重新登录
SSO_PENDING_MAX = 4  # 同一个浏览器里同时进行中的账户登录（多开标签页）最多留几个

# 远程数据源（拉取模式的 Nightcord-Status）的采集结果缓存。远程采集可能很慢（对面没响应要等到
# 超时），所以 /api/status 从不当场等它：直接用缓存，过期了就在后台刷新
# （同一时间只跑一个）。ts 记的是采集完成的时间。青源数据每次请求都从数据库现读，始终最新。
# fp：采集开始时那段配置的指纹。读缓存时跟当前配置比，对不上就当作没有缓存——指纹来自
#     配置文件本身，所以别的进程（Gunicorn 多 worker）改了配置也能认出来。
# seq：每次采集开始领一个递增序号，只有比已入缓存的更新的采集才能写入，先开始后结束的旧采集
#      （比如巡检和看板触发的刷新同时在跑）不会覆盖新结果。
_remote = {"data": None, "ts": 0, "fp": None, "refreshing": False, "seq": 0, "stored_seq": 0}
_remote_lock = threading.Lock()
CACHE_TTL = 10  # 秒

# 常驻一个 Notifier 实例，不要在 background_alert_loop 每轮里 new 一个新的——
# 那样它内部记的"这个问题已经通知过"的状态每 60 秒就被清空一次，等于永远发不完。
_notifier = Notifier({}, watch_generations=metrics_store.watch_generations)

ALERT_CHECK_INTERVAL = 60  # 秒，后台告警巡检间隔（与前端是否打开无关）
AGENT_STALE_SECONDS = 180  # 秒，青源 agent 超过这么久没上报就不当它在线（默认上报间隔 60s，留够 3 倍余量）
# 开机时间比上次记的晚这么多才算重启过。boot_time 是"现在 - 已运行时长"算出来的，NTP 校时会让它抖几秒
REBOOT_MIN_SHIFT_SECONDS = 120
METRICS_PRUNE_INTERVAL = 3600  # 秒，自建监控 agent 历史数据的清理巡检间隔
METRICS_REPORT_PATH = "/api/metrics/report"
AGENT_HEARTBEAT_PATH = "/api/agents/heartbeat"
STATUS_REPORT_PATH = "/api/status/report"
STATUS_PUSH_STALE_SECONDS = 180  # 2702 每 60s 推一次，超过 3 轮没推就当它失联
STATUS_PUSH_SOURCE = "nightcord-status"
STATUS_VALUES = ("ok", "degraded", "down", "unknown")
QINGYUAN_VERSION = "7.17-beta"  # 青源（自建系统指标监控架构）版本号，见 QINGYUAN.zh-CN.md


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError(
            f"未找到配置文件 {CONFIG_PATH}，请先复制 config.example.yaml 为 config.yaml"
        )
    return load_yaml(CONFIG_PATH)


def save_config(cfg):
    save_yaml(CONFIG_PATH, cfg)


def update_config(mutator):
    """Apply a read-modify-write configuration change under one process lock."""
    return update_yaml(CONFIG_PATH, mutator)


def ensure_session_secret():
    """
    session cookie 签名密钥；持久化在 config.yaml 里，
    这样多进程(gunicorn 多 worker)部署时大家读到的是同一个值，重启也不会互相把对方的 session 弄失效。
    config.yaml 还不存在（第一次运行、还没走完配置向导）时用一个临时的随机值顶着。
    """
    if not os.path.exists(CONFIG_PATH):
        return secrets.token_hex(32)

    def ensure(cfg):
        da = cfg.setdefault("dashboard_auth", {})
        if not da.get("session_secret"):
            da["session_secret"] = secrets.token_hex(32)
        return da["session_secret"]

    return update_config(ensure)


app.secret_key = ensure_session_secret()


def _current_trusted_proxies():
    # 跑在所有路由前面（包括 agent 上报这些机器接口）：配置缺失或 YAML 写坏了都只退回"只信本机"，
    # 不能让它把每个请求都变成 500
    try:
        return trusted_proxies_from(load_config())
    except Exception:
        return trusted_proxies_from({})


app.wsgi_app = ForwardedProto(app.wsgi_app, _current_trusted_proxies)


@app.before_request
def enforce_auth():
    """
    Dashboard 按设计要长期暴露公网，所以给所有路由加一层登录门槛（Palintropic 账户 / 密码 / Passkey），
    并按来源 IP 做暴力破解锁定。dashboard_auth.enabled 为 false（默认）且没开 SSO 时不做任何限制，
    适合只在 WireGuard 内网访问、不打算公网暴露的部署方式。
    """
    if request.path == METRICS_REPORT_PATH:
        return  # agent 机器对机器上报，鉴权走独立的共享密钥（见 api_metrics_report），不走登录态鉴权
    if request.path == AGENT_HEARTBEAT_PATH:
        return  # 2702 机器对机器拉取，鉴权走 status_api.token（见 api_agent_heartbeat）
    if request.path == STATUS_REPORT_PATH:
        return  # 2702 机器对机器推送，鉴权走 metrics_agent.shared_secret（见 api_status_report）
    try:
        cfg = load_config()
    except RuntimeError:
        return  # 还没配置 config.yaml，让后续逻辑走正常的报错路径
    if not cfg and request.endpoint != "static":
        # 文件在、内容却是空的（被清空、写坏）：不能当成首次运行把看板敞开
        return jsonify({"error": "config.yaml 是空的，先修好配置"}), 503
    if not _login_required(cfg):
        return

    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint == "static":
        return
    role = _session_role()
    if role is None:
        if request.path.startswith("/api/"):
            return jsonify({"error": "未登录"}), 401
        return redirect(url_for("login_page"))
    if role != sso.ROLE_SUPERADMIN and request.endpoint not in ADMIN_ENDPOINTS:
        if request.path.startswith("/api/"):
            return jsonify({"error": "只有超级管理员能用"}), 403
        return redirect(url_for("index"))


def _login_required(cfg):
    """
    开了 SSO 就一定要登录，不管 dashboard_auth.enabled 怎么写：不然 sso 段开着、登录验证忘了开时，
    看板对谁都敞开，连本地 Passkey 都能被路人注册上，再用它登录成超管。
    看的是 sso.enabled 本身，没填全也算——填了一半更不能当成没开。
    """
    return bool((cfg.get("dashboard_auth") or {}).get("enabled") or (cfg.get("sso") or {}).get("enabled"))


def _session_role():
    """当前会话的档位；没登录或者 SSO 会话到期了返回 None。"""
    role = session.get("role")
    if role not in ROLES:
        return None
    expires = session.get("expires")
    if expires is not None and time.time() >= expires:
        session.clear()  # 到期了重新登录；SSO 登录的档位那时重新查一次
        return None
    return role


def _sign_in_locally():
    """
    本地密码 / Passkey 登录，直接是超管：开了 SSO 时它是 SSO 挂了时的应急入口，只有站长自己有，
    会话限 LOCAL_SESSION_SECONDS；没开 SSO 时它就是平常的登录方式，照旧长期有效。
    """
    session.clear()
    session["role"] = sso.ROLE_SUPERADMIN
    session["via"] = "local"
    session["sid"] = secrets.token_urlsafe(16)
    if (load_config().get("sso") or {}).get("enabled"):
        session["expires"] = time.time() + LOCAL_SESSION_SECONDS
    session.permanent = True


@app.route("/login", methods=["GET"])
def login_page():
    return send_from_directory(app.static_folder, "login.html")


@app.route("/login", methods=["POST"])
def login_password():
    cfg = load_config()
    auth_cfg = cfg.get("dashboard_auth", {})
    ip = get_client_ip(request, trusted_proxies_from(cfg))
    locked, retry_after = _brute_force_guard.is_locked(ip)
    if locked:
        resp = jsonify({"error": f"失败次数过多，请 {retry_after} 秒后再试。"})
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    _brute_force_guard.max_attempts = auth_cfg.get("max_attempts", 5)
    _brute_force_guard.lockout_seconds = auth_cfg.get("lockout_seconds", 900)

    data = request.get_json(silent=True) or {}
    expected_hash = auth_cfg.get("password_hash", "")
    ok = bool(
        expected_hash
        and data.get("username") == auth_cfg.get("username", "")
        and check_password_hash(expected_hash, data.get("password", ""))
    )
    if not ok:
        _brute_force_guard.record_failure(ip)
        return jsonify({"error": "用户名或密码错误"}), 401

    _brute_force_guard.record_success(ip)
    _sign_in_locally()
    return jsonify({"ok": True})


@app.route("/logout", methods=["POST"])
def logout():
    id_token = session.get("id_token") if session.get("via") == "sso" else None
    session.clear()
    url = None
    if id_token:
        try:
            url = sso.logout_url(sso.settings_from(load_config()), id_token)
        except RuntimeError:
            url = None
    # 有 redirect 时前端接着去 SSO 那边也退出，不然下次点「用 Palintropic 账户登录」会被直接放行；
    # 是账户登录、却拿不到 SSO 退出地址时告诉前端，让人知道 Palintropic 那边还登着
    if url:
        return jsonify({"ok": True, "redirect": url})
    return jsonify({"ok": True, "sso_signed_out": False} if id_token else {"ok": True})


# 登录页回来时可以带上看板的 #位置（跟 login.html 的 returnHash 同一个格式），别的一律不收，
# 免得被拿去当开放跳转
_RETURN_HASH = re.compile(r"^#(p=\d+|s=\w+|m=[^#\s]{1,200})$")


def _sso_settings():
    try:
        return sso.settings_from(load_config())
    except RuntimeError:
        return None


@app.route("/api/login/methods")
def api_login_methods():
    """登录页用：要不要显示「用 Palintropic 账户登录」。"""
    return jsonify({"sso": _sso_settings() is not None})


@app.route("/auth/login")
def sso_login():
    settings = _sso_settings()
    if settings is None:
        return redirect(url_for("login_page"))
    nxt = request.args.get("next", "")
    # 回调地址只登记了一个（公网域名）。从别的地址（比如 WireGuard 内网 IP）打开的，先转到那个域名
    # 再出发，否则 state 存在这个域名的 cookie 里，回来时那边的 cookie 里没有，登录必然失败。
    # 只比主机名和端口：cookie 不分 http / https，反代没带对 X-Forwarded-Proto 时比协议会转个没完
    target_origin = sso.origin_of(settings["redirect_uri"])
    if request.host != urlsplit(target_origin).netloc:
        query = f"?{urlencode({'next': nxt})}" if nxt else ""
        return redirect(f"{target_origin}{url_for('sso_login')}{query}")
    try:
        url, pending = sso.begin(settings)
    except sso.SsoError as e:
        print(f"[sso] 发起登录失败: {e.detail}")
        return redirect(url_for("login_page", sso_error=e.code))
    pending["next"] = nxt if _RETURN_HASH.match(nxt) else ""
    # 按 state 各存一份，同一个浏览器开两个标签页登录不会互相顶掉；过期的和太多的丢掉
    pendings = _live_pendings()
    pendings[pending["state"]] = pending
    session["sso_pending"] = dict(sorted(pendings.items(), key=lambda kv: kv[1]["ts"])[-SSO_PENDING_MAX:])
    return redirect(url)


def _live_pendings():
    stored = session.get("sso_pending")
    if not isinstance(stored, dict):
        return {}
    now = time.time()
    live = {}
    for state, p in stored.items():
        try:
            if isinstance(p, dict) and now - float(p.get("ts") or 0) <= sso.PENDING_MAX_AGE:
                live[state] = p
        except (TypeError, ValueError):
            continue
    return live


@app.route("/auth/callback")
def sso_callback():
    pendings = _live_pendings()
    pending = pendings.pop(request.args.get("state") or "", None)  # 每个 state 只能用一次
    session["sso_pending"] = pendings
    settings = _sso_settings()
    if settings is None:
        return redirect(url_for("login_page"))
    step_up = isinstance(pending, dict) and pending.get("purpose") == "terminal"
    try:
        who = sso.finish(settings, pending, request.args)
    except sso.SsoError as e:
        print(f"[sso] {'开终端前的重新验证' if step_up else '登录'}被拒 ({e.code}): {e.detail}")
        if step_up:
            session["terminal_error"] = e.code
            return redirect(f"/#p={TERMINAL_PIVOT}")
        return redirect(url_for("login_page", sso_error=e.code))
    if step_up:
        return _finish_terminal_step_up(pending, who)
    session.clear()
    session.update({
        "role": who["role"], "via": "sso", "sub": who["sub"], "name": who["name"] or who["email"],
        "email": who["email"], "id_token": who["id_token"], "expires": time.time() + settings["session_seconds"],
        "sid": secrets.token_urlsafe(16),
    })
    session.permanent = True
    print(f"[sso] 登录: {who['sub']} ({who['email']}) 档位 {who['role']}")
    return redirect("/" + pending.get("next", ""))


# ---------- 网页终端（设计见 terminal.py 开头） ----------
TERMINAL_PIVOT = 3            # 看板上「终端」是第几个 Pivot（static/index.html 的 TOP_PIVOTS）
STEP_UP_MAX_AGE = 300
STEP_UP_SKEW = 30             # 秒：SSO 和这里的时钟差多少还认
_tickets = terminal.Tickets()
_terminals = terminal.Registry()
_auditor = terminal.Auditor()


@app.route("/auth/terminal", methods=["POST"])
def terminal_start():
    """
    开终端：先回 SSO 用 passkey 重新验证。用 POST（看板的表单提交）：SameSite=Lax 的会话 cookie 不会随
    别的网站发来的 POST 带过来，别人没法替你发起。本地应急登录开不了终端，它没有 SSO 可以重新验证。
    """
    try:
        cfg = load_config()
    except RuntimeError:
        cfg = {}
    settings, term = sso.settings_from(cfg), terminal.settings_from(cfg)
    host = request.form.get("host", "")
    error = None
    if settings is None or term is None or host not in term["hosts"]:
        error = "unknown_host"
    elif session.get("via") != "sso" or not session.get("sid") or not session.get("sub"):
        error = "local_session"
    if error:
        session["terminal_error"] = error
        return redirect(f"/#p={TERMINAL_PIVOT}", code=303)
    try:
        url, pending = sso.begin(settings, extra={"prompt": "login", "max_age": str(STEP_UP_MAX_AGE)})
    except sso.SsoError as e:
        print(f"[terminal] 发起重新验证失败: {e.detail}")
        session["terminal_error"] = e.code
        return redirect(f"/#p={TERMINAL_PIVOT}", code=303)
    pending.update(purpose="terminal", host=host, sid=session["sid"], sub=session["sub"], next="")
    pendings = _live_pendings()
    pendings[pending["state"]] = pending
    session["sso_pending"] = dict(sorted(pendings.items(), key=lambda kv: kv[1]["ts"])[-SSO_PENDING_MAX:])
    return redirect(url, code=303)


def _finish_terminal_step_up(pending, who):
    """重新验证回来：同一个看板会话、同一个人、刚刚用 passkey 验过、这台机器分给了他，才发票。"""
    error = None
    if session.get("sid") != pending.get("sid") or session.get("sub") != pending.get("sub") \
            or who["sub"] != pending.get("sub"):
        error = "mismatch"
    elif not who["amr"] or who["amr"][-1] != "passkey":
        # amr 是这个 SSO 会话里按先后累计的验证方式，以前用过 passkey 不算数，要看最后这一次
        error = "not_passkey"
    elif who["auth_time"] is None or who["auth_time"] < float(pending.get("ts") or 0) - STEP_UP_SKEW:
        error = "stale"
    elif pending.get("host") not in who["ssh_hosts"]:
        error = "host_denied"
    if error:
        print(f"[terminal] 重新验证不通过 ({error}): {who['sub']} → {pending.get('host')} amr={who['amr']}")
        session["terminal_error"] = error
        return redirect(f"/#p={TERMINAL_PIVOT}")
    session["role"] = who["role"]  # 档位以这张刚发的 token 为准
    session["terminal_ticket"] = _tickets.issue(session["sid"], who["sub"], who["email"], who["name"],
                                                pending["host"])
    return redirect(f"/#p={TERMINAL_PIVOT}")


@app.route("/api/terminal/hosts")
def api_terminal_hosts():
    """
    「终端」页：配置里有哪些机器、有没有一张刚验过还没用的票（有就自动连上）、上一次失败的原因。
    能不能开哪台在重新验证时才由 SSO 决定，这里不预判。
    """
    term = terminal.settings_from(load_config())
    error = session.pop("terminal_error", None)
    pending = _tickets.peek(session.get("terminal_ticket"), session.get("sid")) if term else None
    return jsonify({
        "enabled": term is not None,
        "can_open": session.get("via") == "sso",
        "hosts": [{"id": h["id"], "label": h["label"]} for h in (term or {}).get("hosts", {}).values()],
        "pending": pending,
        "error": error,
    })


@app.route("/api/terminal/history")
def api_terminal_history():
    """最近的终端记录：超管看所有人的，一般管理员只看自己的。"""
    if _session_role() == sso.ROLE_SUPERADMIN:
        rows = metrics_store.terminal_history()
    else:
        rows = metrics_store.terminal_history(sub=session.get("sub") or "")
    keys = ("id", "email", "host", "client_ip", "started_at", "ended_at", "bytes_in", "bytes_out", "end_reason")
    return jsonify({"sessions": [{k: r[k] for k in keys} for r in rows]})


WS_LINGER = 2  # 秒


def _ws_send_json(ws, **msg):
    try:
        ws.send(json.dumps(msg, ensure_ascii=False))
    except Exception:
        pass


def _ws_last_word(ws, **msg):
    """
    发最后一条消息（错误或结束），然后等浏览器自己关（最多 WS_LINGER 秒），把它发来的东西读掉。
    simple_websocket 关连接时直接 close 底层 socket，要是浏览器的消息还没读走，内核会回 RST，
    刚发出去的这条消息就可能到不了浏览器。
    """
    _ws_send_json(ws, **msg)
    deadline = time.monotonic() + WS_LINGER
    try:
        while time.monotonic() < deadline:
            ws.receive(timeout=max(0.05, deadline - time.monotonic()))
    except Exception:
        pass


@sock.route("/api/terminal/ws")
def terminal_ws(ws):
    """
    终端的 WebSocket。enforce_auth 已经挡掉了没登录的；这里再要求：同源（防跨站 WebSocket）、
    会话里那张票还在而且属于这个会话，用掉它，然后签证书、连目标机、开始转发。
    """
    origin = request.headers.get("Origin", "")
    if not origin or urlsplit(origin).netloc != request.host:
        _ws_last_word(ws, t="error", code="origin")
        return
    cfg = load_config()
    term = terminal.settings_from(cfg)
    entry = _tickets.take(session.get("terminal_ticket"), session.get("sid"))
    if term is None or entry is None or session.get("via") != "sso" or session.get("sub") != entry["sub"]:
        _ws_last_word(ws, t="error", code="no_ticket")
        return
    host = term["hosts"].get(entry["host"])
    if host is None:
        _ws_last_word(ws, t="error", code="unknown_host")
        return
    session_id = secrets.token_hex(8)
    refused = _terminals.acquire(session_id, entry["sub"])
    if refused:
        _ws_last_word(ws, t="error", code=refused)
        return
    try:
        _run_terminal(ws, cfg, term, host, entry, session_id)
    finally:
        _terminals.release(session_id)


def _run_terminal(ws, cfg, term, host, entry, session_id):
    try:
        first = json.loads(ws.receive(timeout=5) or "{}")
    except (ValueError, TypeError, ConnectionClosed):
        first = {}
    if not isinstance(first, dict):
        first = {}
    cols, rows = terminal.clamp_size(first.get("cols"), first.get("rows"))
    key, serial = terminal_ca.issue(host["user"], f"{entry['sub']}/{host['id']}/{session_id}",
                                    term["source_address"])
    client_ip = get_client_ip(request, trusted_proxies_from(cfg))
    started = time.time()
    base = {"session": session_id, "sub": entry["sub"], "email": entry["email"], "name": entry["name"],
            "host": host["id"], "label": host["label"], "address": host["address"], "client_ip": client_ip}
    # 先记、先推「打开」，再连：连接本身哪怕出事，审计里也已经有这一次
    metrics_store.terminal_started(session_id, entry["sub"], entry["email"], host["id"], client_ip, str(serial),
                                   started)
    _auditor.record({**base, "kind": "open", "at": started}, cfg)
    print(f"[terminal] 打开 {session_id}: {entry['email']} → {host['id']} 来自 {client_ip}")
    transport = chan = None
    reason, bytes_in, bytes_out = "exit", 0, 0
    try:
        transport, chan = terminal.connect(host, key, term["source_address"], cols, rows)
        _ws_send_json(ws, t="ready", host=host["label"])
        reason, bytes_in, bytes_out = terminal.bridge(ws, chan, ConnectionClosed)
    except terminal.HostKeyMismatch as e:
        print(f"[terminal] 主机指纹不对，没有连: {e}")
        reason = "host_key_mismatch"
    except Exception as e:
        print(f"[terminal] 连接 {host['id']} 失败: {e}")
        reason = "connect_failed"
    finally:
        for closable in (chan, transport):
            if closable is not None:
                closable.close()
        ended = time.time()
        metrics_store.terminal_ended(session_id, ended, bytes_in, bytes_out, reason)
        _auditor.record({**base, "kind": "close", "at": ended, "duration": ended - started,
                         "bytes_in": bytes_in, "bytes_out": bytes_out, "reason": reason}, cfg)
        print(f"[terminal] 关闭 {session_id}: {reason}")
        _ws_last_word(ws, t="closed", reason=reason, text=terminal.END_REASONS.get(reason, reason))


@app.route("/api/me")
def api_me():
    """看板用来决定显示哪些东西。不要求登录时谁都是超管（要求登录时没登录的到不了这里）。"""
    role = _session_role() or sso.ROLE_SUPERADMIN
    return jsonify({"role": role, "via": session.get("via", ""), "name": session.get("name", "")})


@app.route("/api/webauthn/register/options", methods=["GET"])
def webauthn_register_options():
    cfg = load_config()
    auth_cfg = cfg.get("dashboard_auth", {})
    username = auth_cfg.get("username") or "admin"
    rp_id = wam.rp_id_from_request(request)
    challenge_b64, options_json = wam.registration_options(rp_id, username, auth_cfg.get("passkeys") or [])
    session["webauthn_challenge"] = challenge_b64
    session["webauthn_rp_id"] = rp_id
    return Response(options_json, mimetype="application/json")


@app.route("/api/webauthn/register/verify", methods=["POST"])
def webauthn_register_verify():
    body = request.get_json(silent=True) or {}
    credential = body.get("credential")
    name = (body.get("name") or "").strip() or "未命名 Passkey"
    challenge_b64 = session.pop("webauthn_challenge", None)
    rp_id = session.pop("webauthn_rp_id", None)
    if not credential or not challenge_b64 or not rp_id:
        return jsonify({"error": "注册会话已过期，请重试"}), 400

    credential_json = credential if isinstance(credential, str) else json.dumps(credential)
    try:
        record = wam.verify_registration(credential_json, challenge_b64, rp_id, wam.origin_from_request(request))
    except Exception as e:
        return jsonify({"error": f"验证失败：{e}"}), 400

    def add_passkey(cfg):
        da = cfg.setdefault("dashboard_auth", {})
        da.setdefault("passkeys", []).append({**record, "name": name, "created_at": time.time()})

    update_config(add_passkey)
    return jsonify({"ok": True})


@app.route("/api/webauthn/passkeys", methods=["GET"])
def webauthn_list_passkeys():
    cfg = load_config()
    passkeys = cfg.get("dashboard_auth", {}).get("passkeys") or []
    return jsonify([
        {"credential_id": p["credential_id"], "name": p.get("name", ""), "created_at": p.get("created_at")}
        for p in passkeys
    ])


@app.route("/api/webauthn/passkeys/<credential_id>", methods=["DELETE"])
def webauthn_delete_passkey(credential_id):
    def delete_passkey(cfg):
        da = cfg.setdefault("dashboard_auth", {})
        passkeys = da.get("passkeys") or []
        remaining = [p for p in passkeys if p.get("credential_id") != credential_id]
        da["passkeys"] = remaining
        return len(remaining) != len(passkeys)

    if not update_config(delete_passkey):
        return jsonify({"error": "未找到该 Passkey"}), 404
    return jsonify({"ok": True})


@app.route("/api/webauthn/login/options", methods=["POST"])
def webauthn_login_options():
    cfg = load_config()
    auth_cfg = cfg.get("dashboard_auth", {})
    ip = get_client_ip(request, trusted_proxies_from(cfg))
    locked, retry_after = _brute_force_guard.is_locked(ip)
    if locked:
        resp = jsonify({"error": f"失败次数过多，请 {retry_after} 秒后再试。"})
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    rp_id = wam.rp_id_from_request(request)
    challenge_b64, options_json = wam.authentication_options(rp_id, auth_cfg.get("passkeys") or [])
    session["webauthn_challenge"] = challenge_b64
    session["webauthn_rp_id"] = rp_id
    return Response(options_json, mimetype="application/json")


@app.route("/api/webauthn/login/verify", methods=["POST"])
def webauthn_login_verify():
    cfg = load_config()
    auth_cfg = cfg.get("dashboard_auth", {})
    ip = get_client_ip(request, trusted_proxies_from(cfg))
    locked, retry_after = _brute_force_guard.is_locked(ip)
    if locked:
        resp = jsonify({"error": f"失败次数过多，请 {retry_after} 秒后再试。"})
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    _brute_force_guard.max_attempts = auth_cfg.get("max_attempts", 5)
    _brute_force_guard.lockout_seconds = auth_cfg.get("lockout_seconds", 900)

    body = request.get_json(silent=True) or {}
    credential = body.get("credential")
    challenge_b64 = session.pop("webauthn_challenge", None)
    rp_id = session.pop("webauthn_rp_id", None)
    if not credential or not challenge_b64 or not rp_id:
        _brute_force_guard.record_failure(ip)
        return jsonify({"error": "登录会话已过期，请重试"}), 400

    credential_json = credential if isinstance(credential, str) else json.dumps(credential)
    stored = wam.find_passkey(auth_cfg.get("passkeys") or [], wam.extract_credential_id(credential))
    if not stored:
        _brute_force_guard.record_failure(ip)
        return jsonify({"error": "未知的 Passkey"}), 401

    try:
        new_sign_count = wam.verify_authentication(
            credential_json, challenge_b64, rp_id, wam.origin_from_request(request), stored
        )
    except Exception:
        _brute_force_guard.record_failure(ip)
        return jsonify({"error": "验证失败"}), 401

    credential_id = stored["credential_id"]

    def update_sign_count(current_cfg):
        current = wam.find_passkey(current_cfg.get("dashboard_auth", {}).get("passkeys") or [], credential_id)
        if current:
            current["sign_count"] = new_sign_count

    update_config(update_sign_count)
    _brute_force_guard.record_success(ip)
    _sign_in_locally()
    return jsonify({"ok": True})


def fetch_all():
    """采集远程数据源（顺便更新缓存）并加上青源数据。后台巡检用；看板请求走 api_status 的非阻塞路径。"""
    ticket = _begin_remote_fetch()
    remote = fetch_remote(ticket["cfg"])
    _store_remote(remote, ticket)
    return _with_qingyuan(remote)


def _with_qingyuan(remote):
    results = _qingyuan_cards(time.time()) + list(remote)  # 拷一份列表，不往缓存本身上加卡片
    try:
        cfg = load_config()
    except RuntimeError:
        cfg = {}
    push_card = _nightcord_push_card(cfg, time.time())
    if push_card is not None:
        results.append(push_card)
    return results


def _nightcord_push_mode(cfg):
    ns_cfg = cfg.get("nightcord_status") or {}
    return bool(ns_cfg.get("enabled")) and ns_cfg.get("source") == "push"


def _nightcord_push_card(cfg, now):
    """
    推送模式下 Nightcord-Status 的卡片，跟青源数据一样现读（存在 SQLite 里，多个 worker 共用），
    不进远程缓存。形状跟拉取模式的 NightcordStatusClient.collect_all() 一致，前端和告警都不用分两套。
    """
    if not _nightcord_push_mode(cfg):
        return None
    name = (cfg.get("nightcord_status") or {}).get("name", "Nightcord-Status")
    card = {"name": name, "url": None, "online": True, "error": None,
            "kind": "nightcord-status", "targets": [], "seen_at": None}
    pushed = metrics_store.get_status_push(STATUS_PUSH_SOURCE)
    if pushed is None:
        card.update(online=False, error="还没收到过 2702 的推送")
        return card
    received_at, body = pushed
    card["seen_at"] = received_at
    if now - received_at > STATUS_PUSH_STALE_SECONDS:
        card.update(online=False, error=(
            f"2702 超过 {STATUS_PUSH_STALE_SECONDS // 60} 分钟没有推送，最后一次在 "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(received_at))}"
        ))
        return card
    card["targets"] = body.get("targets", [])
    return card


def _remote_fingerprint(cfg=None):
    """决定远程采集结果的那段配置（Nightcord-Status）的指纹。"""
    if cfg is None:
        try:
            cfg = load_config()
        except RuntimeError:
            cfg = {}
    return json.dumps(cfg.get("nightcord_status") or {}, sort_keys=True, ensure_ascii=False)


def _begin_remote_fetch():
    """采集开始时调用：读一次配置快照，指纹和接下来的采集都只用这一份（见 fetch_remote 的 cfg 参数），
    并领一个序号。配置若在采集中途改了，这份结果的指纹就是旧的，读的时候会被当作过期。"""
    try:
        cfg = load_config()
    except RuntimeError:
        cfg = {}
    with _remote_lock:
        _remote["seq"] += 1
        return {"seq": _remote["seq"], "fp": _remote_fingerprint(cfg), "cfg": cfg}


def _store_remote(data, ticket):
    with _remote_lock:
        if ticket["seq"] < _remote["stored_seq"]:
            return  # 有更晚开始的采集已经写进来了，这份更旧
        _remote["stored_seq"] = ticket["seq"]
        _remote["data"] = data
        _remote["ts"] = time.time()
        _remote["fp"] = ticket["fp"]


def _invalidate_remote():
    """同进程里刚保存过配置：马上丢掉缓存。跨进程靠读缓存时的指纹比对。"""
    with _remote_lock:
        _remote["data"] = None


def _refresh_remote_async():
    """后台刷新远程缓存；已经有一个在跑就什么都不做。"""
    with _remote_lock:
        if _remote["refreshing"]:
            return
        _remote["refreshing"] = True

    def run():
        try:
            ticket = _begin_remote_fetch()
            _store_remote(fetch_remote(ticket["cfg"]), ticket)
        except Exception as e:
            print(f"[remote refresh] 出错: {e}")
        finally:
            with _remote_lock:
                _remote["refreshing"] = False

    threading.Thread(target=run, daemon=True).start()


def fetch_remote(cfg):
    """拉取模式下 Nightcord-Status 的原始采集结果（不含青源数据），只按传进来的配置快照采集。"""
    ns_cfg = cfg.get("nightcord_status") or {}
    if not ns_cfg.get("enabled") or _nightcord_push_mode(cfg):
        return []
    try:
        return [NightcordStatusClient(
            ns_cfg.get("name", "Nightcord-Status"),
            ns_cfg["url"],
            ns_cfg.get("verify_ssl", False),
        ).collect_all()]
    except Exception as e:
        return [{"name": ns_cfg.get("name", "Nightcord-Status"), "url": ns_cfg.get("url"),
                 "online": False, "error": str(e), "kind": "nightcord-status", "targets": []}]


def _agent_states(now):
    """所有"应该在线"的青源机器的当前状态，看板卡片、告警巡检、2702 心跳接口共用这一份判定。"""
    states = []
    for row in metrics_store.list_known_panels():
        panel = row["panel"]
        ip = metrics_store.get_ip(panel)
        states.append({
            "panel": panel,
            "seen_at": row["seen_at"],
            "generation": row["generation"],
            "online": now - row["seen_at"] <= AGENT_STALE_SECONDS,
            "sample": metrics_store.get_latest(panel),
            "system": metrics_store.get_system(panel),
            "disk_detail": metrics_store.get_disk_detail(panel),
            "ip_internal": ip.get("ip_internal"),
            "ip_external": ip.get("ip_external"),
        })
    return states


def _qingyuan_cards(now):
    """
    每台青源机器一张卡片。离线的机器也照样出卡片（灰掉、显示最后一次的数据），
    不会因为不上报了就从看板上消失；只有在设置页"移除"之后才不再显示。
    """
    cards = []
    for state in _agent_states(now):
        sample = state["sample"] or {}
        offline_error = None if state["online"] else (
            f"青源 agent 超过 {AGENT_STALE_SECONDS // 60} 分钟没有上报，最后一次上报在 "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state['seen_at']))}"
        )
        qingyuan = {
            "cpu": sample.get("cpu"),
            "mem": sample.get("mem"),
            "disk": sample.get("disk"),
            "disk_detail": state["disk_detail"],
            "swap": sample.get("swap"),
            "swap_total": state["system"]["swap_total"],
            "load1": sample.get("load1"),
            "load5": sample.get("load5"),
            "load15": sample.get("load15"),
            "cores": state["system"]["cores"],
            "procs": sample.get("procs"),
            "tcp_established": sample.get("tcp_established"),
            "boot_time": state["system"]["boot_time"],
            "ts": sample.get("ts"),
            "ip_internal": state["ip_internal"],
            "ip_external": state["ip_external"],
            "online": state["online"],
            "seen_at": state["seen_at"],
            "error": offline_error,
        }
        cards.append({
            "name": state["panel"],
            "url": None,
            "online": state["online"],
            "error": offline_error,
            "kind": "qingyuan",
            "qingyuan": qingyuan,
        })
    return cards


def _thresholds():
    """前端磁贴变红用的阈值，跟告警巡检读同一份配置，看板上的红色和收到的告警永远一致。"""
    try:
        th = (load_config().get("notifications") or {}).get("thresholds") or {}
    except RuntimeError:
        th = {}  # 配置文件缺失：状态接口照常返回（缓存数据 + 默认阈值），不因为阈值变成 500
    return {"cpu": th.get("cpu", 90), "mem": th.get("mem", 90), "disk": th.get("disk", 85)}


@app.route("/api/status")
def api_status():
    """
    永远立刻返回：远程数据源用缓存（过期就后台刷新），青源数据现读。第一次还没有远程缓存时，
    先只返回青源机器，remote_pending=True，远程部分下一次刷新就有了。
    """
    try:
        cfg = load_config()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({**_status_snapshot(cfg), "thresholds": _thresholds()})


def _status_snapshot(cfg, max_age=CACHE_TTL):
    has_remote = bool((cfg.get("nightcord_status") or {}).get("enabled")) and not _nightcord_push_mode(cfg)
    now = time.time()
    with _remote_lock:
        remote, remote_ts, remote_fp = _remote["data"], _remote["ts"], _remote["fp"]
    if remote is not None and remote_fp != _remote_fingerprint(cfg):
        remote = None  # 缓存是按改之前的配置采的（可能是别的进程改的配置）
    if remote is None or now - remote_ts >= max_age:
        _refresh_remote_async()
    return {
        "panels": _with_qingyuan(remote or []),
        "ts": now,
        # 远程数据源（拉取模式的 Nightcord-Status）那部分的采集完成时间；前端据此提示数据是否陈旧
        "remote_ts": remote_ts if remote is not None and has_remote else None,
        "remote_pending": remote is None and has_remote,
    }


# 没登录时看到的第一面（Status）：只有机器名、在线与否和粗略的状态色。状态在服务器上算好再给，
# 数字、IP、挂载点、离线原因原文、2702 的各个检测目标都不出门。
# 判定跟 static/index.html 的 stateOf / noMetrics 一致，改一边要同步另一边。
PUBLIC_CAUTION_PERCENT = 70  # 即 index.html 的 CAUTION_BAND


def _level(value, threshold):
    if value is None:
        return ""
    if value >= threshold:
        return "warn"
    if value >= PUBLIC_CAUTION_PERCENT:
        return "caution"
    return ""


def _public_card(p, th):
    """state：ok / caution / warn；nodata：机器离线或者一个数都没报（磁贴灰掉）。"""
    if p.get("kind") == "nightcord-status":
        online = p.get("online") is not False
        statuses = {t.get("status") for t in p.get("targets") or [] if isinstance(t, dict)}
        state = ("warn" if not online or "down" in statuses
                 else "caution" if statuses & {"degraded", "unknown"} else "ok")
        return {"name": p.get("name") or "", "kind": "nightcord-status", "online": online,
                "state": state, "nodata": False}
    q = p.get("qingyuan") or {}
    online = p.get("online") is not False and q.get("online") is not False
    percents = [d.get("percent") for d in q.get("disk_detail") or []
                if isinstance(d, dict) and isinstance(d.get("percent"), (int, float))]
    values = {"cpu": q.get("cpu"), "mem": q.get("mem"), "disk": max(percents) if percents else q.get("disk")}
    levels = {_level(values[k], th[k]) for k in values}
    state = "warn" if not online or "warn" in levels else "caution" if "caution" in levels else "ok"
    return {"name": p.get("name") or "", "kind": "qingyuan", "online": online, "state": state,
            "nodata": not online or all(v is None for v in values.values())}


# 匿名请求谁都能发，不能拿它驱动采集或者每次都扫一遍数据库：整个结果缓存几秒；拉取模式的远程数据
# 缺了或者超过一分钟才让它刷新一次（同一时间只跑一个），匿名流量最多每分钟带出一次采集，跟后台巡检
# 同一个量级。不能完全不刷新：远程缓存在进程内存里，多 worker 部署时巡检进程采到的结果别的进程看不到。
PUBLIC_CACHE_SECONDS = 5
PUBLIC_REMOTE_MAX_AGE = 60
_public_cache = {"at": 0.0, "body": None}
_public_lock = threading.Lock()


def _public_kaomoji(cfg, names):
    """只给看得到的这些机器的颜文字分配，以及它们用到的角色的表情；移除了的机器、没用上的角色都不给。"""
    k = kaomoji_of(cfg)
    assign = {m: c for m, c in k["assign"].items() if m in names}
    return {"assign": assign, "faces": {c: f for c, f in k["faces"].items() if c in assign.values()}}


@app.route("/api/public/status")
def api_public_status():
    now = time.time()
    with _public_lock:
        if _public_cache["body"] is not None and now - _public_cache["at"] < PUBLIC_CACHE_SECONDS:
            return jsonify(_public_cache["body"])
    # 锁外重算：读配置和数据库慢的时候，别的匿名请求不用排队等它（过期那一刻可能有几个一起算，无妨）
    try:
        cfg = load_config()
    except RuntimeError:
        return jsonify({"error": "看板还没配置好"}), 400
    snapshot = _status_snapshot(cfg, max_age=PUBLIC_REMOTE_MAX_AGE)
    th = _thresholds()
    cards = [_public_card(p, th) for p in snapshot["panels"]]
    body = {
        "panels": cards,
        "kaomoji": _public_kaomoji(cfg, {c["name"] for c in cards}),
        # 还有数据没到（拉取模式第一次采集中）：前端说「还没准备好」，而不是「还没有机器」
        "pending": snapshot["remote_pending"],
    }
    with _public_lock:
        _public_cache.update(at=now, body=body)
    return jsonify(body)


def _number(value, low, high, cast=float):
    """上报里的可选数值：类型不对、不是有限数、超出合理范围的一律当没报（存 NULL），不让整次上报失败。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = cast(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if v != v or not low <= v <= high:  # v != v 筛掉 NaN
        return None
    return v


def _optional_metrics(body, now):
    """新版 agent 才带的字段，见 agent/metrics_agent.py 的 collect_system。"""
    extra = {
        "swap": _number(body.get("swap"), 0, 100),
        "load1": _number(body.get("load1"), 0, 1e6),
        "load5": _number(body.get("load5"), 0, 1e6),
        "load15": _number(body.get("load15"), 0, 1e6),
        "procs": _number(body.get("procs"), 0, 10_000_000, int),
        "tcp_established": _number(body.get("tcp_established"), 0, 100_000_000, int),
    }
    system = {
        # 2000-01-01 之前或者比现在还晚一天的开机时间都是乱值
        "boot_time": _number(body.get("boot_time"), 946684800, now + 86400, int),
        "cores": _number(body.get("cores"), 1, 100_000, int),
        "swap_total": _number(body.get("swap_total"), 0, 2**62, int),
    }
    return extra, system


def _notify_reboot(panel, previous_boot, boot_time, ip_internal, ip_external):
    """开机时间往后跳了：机器重启过。重启本身已经结束了，所以是一条普通通知，不算告警、没有恢复。"""
    try:
        notif_cfg = load_config().get("notifications", {})
    except RuntimeError:
        return
    fmt = "%Y-%m-%d %H:%M:%S"
    alert = {
        "server": panel,
        "alert_type": "机器重启通知",
        "content": f"开机时间从 {time.strftime(fmt, time.localtime(previous_boot))} "
                   f"变成了 {time.strftime(fmt, time.localtime(boot_time))}，这台机器重启过。"
                   f"现在已经运行约 {format_duration(max(0, time.time() - boot_time))}。",
        "ip_internal": ip_internal,
        "ip_external": ip_external,
    }
    # 发飞书 / 邮件可能要好几秒，别让 agent 的上报请求等着
    threading.Thread(target=Notifier(notif_cfg).send_event, args=(alert,), daemon=True).start()


@app.route(METRICS_REPORT_PATH, methods=["POST"])
def api_metrics_report():
    """自建监控 agent（agent/metrics_agent.py）的上报入口，机器对机器调用。
    鉴权用共享密钥而不是 dashboard_auth 的登录态（见 enforce_auth 里的例外）。
    """
    cfg = load_config().get("metrics_agent", {})
    if not cfg.get("enabled"):
        return jsonify({"error": "metrics_agent 未启用"}), 404

    expected = cfg.get("shared_secret", "")
    got = request.headers.get("X-Metrics-Secret", "")
    if not expected or not hmac.compare_digest(expected, got):
        return jsonify({"error": "共享密钥不匹配"}), 401

    body = request.get_json(silent=True) or {}
    panel = body.get("panel")
    try:
        ts = int(body["ts"])
        cpu = float(body["cpu"])
        mem = float(body["mem"])
        disk = float(body["disk"])
        net_in_kbps = float(body["net_in_kbps"])
        net_out_kbps = float(body["net_out_kbps"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "缺少字段或字段类型不对，需要 panel/ts/cpu/mem/disk/net_in_kbps/net_out_kbps"}), 400
    if not panel:
        return jsonify({"error": "缺少 panel"}), 400

    now = time.time()
    extra, system = _optional_metrics(body, now)
    metrics_store.insert_sample(panel, ts, cpu, mem, disk, net_in_kbps, net_out_kbps, extra)
    metrics_store.mark_seen(panel, now)
    metrics_store.upsert_ip(panel, body.get("ip_internal"), body.get("ip_external"))
    previous_boot = metrics_store.update_system(panel, system["boot_time"], system["cores"], system["swap_total"])
    if (system["boot_time"] is not None and previous_boot is not None
            and system["boot_time"] - previous_boot > REBOOT_MIN_SHIFT_SECONDS):
        ip = metrics_store.get_ip(panel)
        _notify_reboot(panel, previous_boot, system["boot_time"], ip.get("ip_internal"), ip.get("ip_external"))

    disk_detail = body.get("disk_detail")
    if isinstance(disk_detail, list):
        # 老版本 agent 不会带这个字段，新版本带了才存；随便什么畸形数据都别写进库里
        clean = []
        for d in disk_detail:
            try:
                clean.append({
                    "path": str(d["path"]), "total": float(d["total"]),
                    "used": float(d["used"]), "percent": float(d["percent"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        metrics_store.upsert_disk_detail(panel, clean)

    return jsonify({"ok": True})


@app.route("/api/metrics/version")
def api_metrics_version():
    return jsonify({"name": "青源", "version": QINGYUAN_VERSION})


@app.route("/api/metrics/history")
def api_metrics_history():
    """给前端历史趋势图用的读接口，跟其它看板数据一样走 dashboard_auth。"""
    panel = request.args.get("panel", "")
    if not panel:
        return jsonify({"error": "缺少 panel 参数"}), 400
    hours = request.args.get("hours", 24, type=float) or 24
    since_ts = int(time.time() - hours * 3600)
    return jsonify({"panel": panel, "samples": metrics_store.query_history(panel, since_ts)})


@app.route(AGENT_HEARTBEAT_PATH)
def api_agent_heartbeat():
    """
    给 2702（Nightcord-Status）的 qingyuan 检测用的只读接口，机器对机器调用：
    每台机器最后一次上报的时间和在线判定。只给判断"挂没挂"需要的字段，不给 IP、磁盘这些细节。
    鉴权用 status_api.token（只读用途，跟 agent 上报的 shared_secret 分开，泄露了也写不进数据）。
    """
    cfg = load_config().get("status_api", {})
    if not cfg.get("enabled"):
        return jsonify({"error": "status_api 未启用"}), 404

    expected = cfg.get("token", "")
    got = request.headers.get("X-Status-Token", "")
    if not expected or not hmac.compare_digest(expected, got):
        return jsonify({"error": "token 不匹配"}), 401

    now = time.time()
    agents = [
        {"name": s["panel"], "last_seen": s["seen_at"], "online": s["online"]}
        for s in _agent_states(now)
    ]
    return jsonify({"now": int(now), "stale_seconds": AGENT_STALE_SECONDS, "agents": agents})


def _clean_pushed_target(item):
    if not isinstance(item, dict) or not item.get("id"):
        return None
    status = item.get("status")
    last_error = item.get("last_error")
    detail = item.get("detail")
    return {
        "id": str(item["id"]),
        "display_name": str(item.get("display_name") or item["id"]),
        "type": str(item.get("type") or ""),
        "status": status if status in STATUS_VALUES else "unknown",
        "last_error": str(last_error) if last_error else None,
        "detail": detail if isinstance(detail, dict) else {},
    }


@app.route(STATUS_REPORT_PATH, methods=["POST"])
def api_status_report():
    """
    2702（Nightcord-Status）每轮采集完把全部目标状态推过来，机器对机器调用。
    FreePBX 主动连上海，家里防火墙不用为 Panopticon 开口子（拉取模式要连 FreePBX 的 8702）。
    鉴权用 metrics_agent.shared_secret：这是写数据的入口，跟青源上报同一类；
    status_api.token 只用于只读的心跳接口，不拿来写。
    """
    cfg = load_config()
    if not _nightcord_push_mode(cfg):
        return jsonify({"error": "nightcord_status 推送模式未启用"}), 404

    expected = (cfg.get("metrics_agent") or {}).get("shared_secret", "")
    got = request.headers.get("X-Metrics-Secret", "")
    if not expected or not hmac.compare_digest(expected, got):
        return jsonify({"error": "共享密钥不匹配"}), 401

    body = request.get_json(silent=True) or {}
    raw_targets = body.get("targets")
    if not isinstance(raw_targets, list) or len(raw_targets) > 200:
        return jsonify({"error": "targets 必须是列表（最多 200 个）"}), 400
    targets = [t for t in (_clean_pushed_target(item) for item in raw_targets) if t]
    metrics_store.save_status_push(STATUS_PUSH_SOURCE, time.time(), {
        "last_updated": str(body.get("last_updated") or ""),
        "targets": targets,
    })
    return jsonify({"ok": True, "targets": len(targets)})


@app.route("/api/agents")
def api_agents():
    """设置页"青源机器"列表：所有还在盯的机器和在线状态。"""
    now = time.time()
    return jsonify({"agents": [
        {"name": s["panel"], "last_seen": s["seen_at"], "online": s["online"]}
        for s in _agent_states(now)
    ]})


@app.route("/api/agents/<path:name>/retire", methods=["POST"])
def api_agent_retire(name):
    """把机器移出监控名单（卸了 agent / 机器退役）。之后它要是又上报了，会自动回到名单里。"""
    if not metrics_store.retire_panel(name, time.time()):
        return jsonify({"error": "没有这台机器"}), 404
    # 不在这里清告警的内存状态：移除让监控代次 +1，旧告警在下一次发送 / 恢复检查时按代次静默作废。
    # 在这里按机器名清，可能误删移除之后才发出的新一代告警（见 Notifier.notify 的代次说明）
    return jsonify({"ok": True})  # 青源数据每次现读，看板下次刷新就不再显示这张卡


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# 设置已经并进看板：设置列表是 /#p=5，各页是 /#s=<页名>。/setup 和 /settings 留着转过去，
# 旧书签和文档里的地址照样能用。旧地址可能带着 #s=notify 这种页名，# 后面的部分浏览器不发给
# 服务器，HTTP 跳转也会被 Location 里的 # 盖掉，所以回一小段脚本在浏览器里转：认识的页名照旧
# 打开（以前的「外观」现在叫 theme），其它一律去设置列表，首次配置的提示也在那里。
_SETTINGS_FORWARD = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>设置 · Panopticon</title>
<script>
(function(){
  var pages = {appearance:'theme', theme:'theme', kaomoji:'kaomoji', notify:'notify', agents:'agents',
               deploy:'deploy', login:'login'};
  var m = location.hash.match(/^#s=(\\w+)$/);
  var key = m && Object.prototype.hasOwnProperty.call(pages, m[1]) ? pages[m[1]] : null;
  location.replace('/' + (key ? '#s=' + key : '#p=5'));
})();
</script></head>
<body><a href="/#p=5">打开设置</a></body></html>
"""


@app.route("/setup")
def setup_page():
    return Response(_SETTINGS_FORWARD, mimetype="text/html")


@app.route("/settings")
def settings_page():
    return Response(_SETTINGS_FORWARD, mimetype="text/html")


@app.route("/api/config", methods=["GET"])
def api_get_config():
    try:
        cfg = load_config()
    except RuntimeError:
        cfg = {}
    return jsonify(redact_for_display(cfg))


@app.route("/api/config", methods=["POST"])
def api_save_config():
    submitted = request.get_json(silent=True) or {}

    def merge_and_save(old_cfg):
        merged, errors = merge_submitted(old_cfg, submitted)
        if not errors:
            old_cfg.clear()
            old_cfg.update(merged)
        return errors

    errors = update_config(merge_and_save)
    if errors:
        return jsonify({"errors": errors}), 400
    _invalidate_remote()  # 配置变了，远程缓存作废，下次 /api/status 后台重新采集
    return jsonify({"ok": True})


@app.route("/api/appearance", methods=["GET"])
def api_get_appearance():
    try:
        cfg = load_config()
    except RuntimeError:
        cfg = {}
    return jsonify(appearance_of(cfg))


@app.route("/api/appearance", methods=["POST"])
def api_save_appearance():
    """设置 → 主题：选了马上保存，只动 appearance 这一段，不碰别的配置。"""
    colours, errors = clean_appearance(request.get_json(silent=True) or {})
    if errors:
        return jsonify({"errors": errors}), 400

    def put(cfg):
        cfg["appearance"] = colours

    update_config(put)
    return jsonify(colours)


@app.route("/api/kaomoji", methods=["GET"])
def api_get_kaomoji():
    try:
        cfg = load_config()
    except RuntimeError:
        cfg = {}
    return jsonify(kaomoji_of(cfg))


@app.route("/api/kaomoji", methods=["POST"])
def api_save_kaomoji():
    """设置 → 颜文字：整份提交，只动 kaomoji 这一段。"""
    kaomoji, errors = clean_kaomoji(request.get_json(silent=True) or {})
    if errors:
        return jsonify({"errors": errors}), 400

    def put(cfg):
        cfg["kaomoji"] = kaomoji

    update_config(put)
    return jsonify(kaomoji)


@app.route("/api/notifications/test", methods=["POST"])
def api_notifications_test():
    """设置页"发送测试通知"按钮：用已保存的 config.yaml 里的飞书配置发一条模拟告警卡片，
    用来确认 Webhook 是不是真的通（不是只看 HTTP 状态码，飞书那边很多错误也返回 200）。
    """
    try:
        cfg = load_config()
    except RuntimeError:
        return jsonify({"ok": False, "error": "还没有 config.yaml，先在设置页保存通知配置"}), 400
    notifier = Notifier(cfg.get("notifications", {}))
    ok, message = notifier.send_feishu_test()
    return jsonify({"ok": ok, "message": message})


def _ensure_metrics_agent_secret(cfg):
    """一键部署 agent 时如果 metrics_agent 这段还没配过，顺手把它打开并生成一个共享密钥，
    省得用户还得回去手改 config.yaml。返回是否改动了 cfg（改了才需要落盘）。
    """
    ma = cfg.setdefault("metrics_agent", {})
    changed = False
    if not ma.get("enabled"):
        ma["enabled"] = True
        changed = True
    if not ma.get("shared_secret"):
        ma["shared_secret"] = secrets.token_hex(32)
        changed = True
    if "retention_days" not in ma:
        ma["retention_days"] = 30
        changed = True
    return changed


def _deploy_and_report(ip, port, ssh_user, password, remember, panel_name_override=None):
    """
    一键装 agent 的公共逻辑，POST /api/agent/deploy 和 .../redeploy 都走这里。
    装一次动辄一两分钟（建虚拟环境、装依赖），干等着容易让人怀疑是不是卡死了，
    所以用 NDJSON 流式返回：deploy_agent() 每 emit 一行日志就立刻推给前端，
    而不是攒到最后一次性甩一大段。SSH 部署本身在后台线程里跑，主线程负责把
    队列里的日志行 yield 出去，deploy_agent() 结束后队列收到 "done" 哨兵才收尾。
    """
    if not os.path.exists(CONFIG_PATH):
        return jsonify({"error": "还没有 config.yaml，先在设置页保存一次配置"}), 400

    def ensure_metrics_agent_secret(cfg):
        _ensure_metrics_agent_secret(cfg)
        return dict(cfg.get("metrics_agent", {}))

    ma_cfg = update_config(ensure_metrics_agent_secret)
    report_url_override = (ma_cfg.get("report_url") or "").rstrip("/")
    if report_url_override:
        report_url = report_url_override + METRICS_REPORT_PATH
    else:
        # 没配 report_url 就退而求其次，猜"管理员这一刻访问设置页用的地址"就是 agent 该上报的地址——
        # 这个猜测在通过 SSH 隧道／127.0.0.1 访问设置页、但部署目标是别的机器时是错的：
        # agent 会拿到一个只在"本机自己"才有意义的地址，上报永远静默失败，dashboard 上永远不会出现这张卡片。
        report_url = f"{request.scheme}://{request.host}{METRICS_REPORT_PATH}"
    shared_secret = ma_cfg["shared_secret"]

    q = queue.Queue()
    result = {}
    request_host = request.host.split(":")[0]
    is_risky_guess = (
        not report_url_override
        and request_host in ("127.0.0.1", "localhost")
        and ip not in ("127.0.0.1", "localhost")
    )

    def worker():
        try:
            if is_risky_guess:
                q.put(("log",
                    f"⚠️ 你现在是通过 127.0.0.1 访问设置页的（大概率走了 SSH 隧道），"
                    f"但部署目标 {ip} 不是本机——agent 会被塞进一个只在它自己身上才有意义的上报地址"
                    f"（{report_url}），装完大概率永远收不到数据，dashboard 也不会出现这张卡片。"
                    f"建议先在 config.yaml 的 metrics_agent.report_url 里手动填一个 agent 那边"
                    f"能访问到的 Panopticon 地址（比如 WireGuard 内网地址），再重新部署。"
                ))
            pins = {(h["address"], h["port"]): (h["host_key_type"], h["host_key"])
                    for h in ((terminal.settings_from(load_config()) or {}).get("hosts") or {}).values()}
            panel_name = deploy_agent(
                ip, port, ssh_user, password, report_url, shared_secret,
                log=lambda line: q.put(("log", line)),
                panel_name_override=panel_name_override,
                pinned_host_key=pins.get((ip, port)),
            )
            result["ok"], result["panel_name"] = True, panel_name
        except AgentDeployError as e:
            result["ok"], result["error"] = False, str(e)
        except Exception as e:
            result["ok"], result["error"] = False, f"未预期的错误：{e}"
        finally:
            q.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            kind, payload = q.get()
            if kind == "log":
                yield json.dumps({"type": "log", "line": payload}, ensure_ascii=False) + "\n"
                continue
            if result.get("ok"):
                if remember:
                    agent_hosts.save_host(CONFIG_PATH, ip, port, ssh_user, password, result["panel_name"])
                yield json.dumps(
                    {"type": "done", "ok": True, "panel_name": result["panel_name"]}, ensure_ascii=False
                ) + "\n"
            else:
                yield json.dumps(
                    {"type": "done", "ok": False, "error": result.get("error", "未知错误")}, ensure_ascii=False
                ) + "\n"
            return

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


@app.route("/api/agent/deploy", methods=["POST"])
def api_agent_deploy():
    """在设置页填 IP/端口/SSH 账号密码，一键 SSH 上去装青源 agent 并注册 systemd。"""
    body = request.get_json(silent=True) or {}
    ip = (body.get("ip") or "").strip()
    try:
        port = int(body.get("port") or 22)
    except (TypeError, ValueError):
        return jsonify({"error": "端口必须是数字"}), 400
    ssh_user = (body.get("ssh_user") or "root").strip()
    password = body.get("password") or ""
    remember = bool(body.get("remember"))
    if not ip or not password:
        return jsonify({"error": "服务器 IP 和 SSH 密码不能为空"}), 400

    return _deploy_and_report(ip, port, ssh_user, password, remember)


@app.route("/api/agent/hosts", methods=["GET"])
def api_agent_hosts():
    try:
        cfg = load_config()
    except RuntimeError:
        cfg = {}
    return jsonify({"hosts": agent_hosts.list_hosts(cfg)})


@app.route("/api/agent/hosts/<path:host_id>/redeploy", methods=["POST"])
def api_agent_redeploy(host_id):
    """用之前勾选过"记住密码"的那台服务器的凭据重新装一遍，不用再填密码。"""
    try:
        cfg = load_config()
    except RuntimeError:
        return jsonify({"error": "还没有 config.yaml"}), 400
    cred = agent_hosts.get_host_credentials(cfg, host_id)
    if not cred:
        return jsonify({"error": "没找到这台已保存的服务器（可能当时没勾选记住密码，或密钥文件变了）"}), 404
    # 沿用上次部署时实际用的 panel_name，不重新探测 hostname——不然改过 hostname 的机器重新部署后
    # 会换个名字上报，在看板上变成一台新机器，旧名字那张卡片留成离线。
    saved = next((h for h in agent_hosts.list_hosts(cfg) if h["id"] == host_id), None)
    panel_name_override = saved["panel_name"] if saved and saved.get("panel_name") else None
    return _deploy_and_report(
        cred["ip"], cred["port"], cred["ssh_user"], cred["password"], True,
        panel_name_override=panel_name_override,
    )


@app.route("/api/agent/hosts/<path:host_id>", methods=["DELETE"])
def api_agent_delete_host(host_id):
    agent_hosts.delete_host(CONFIG_PATH, host_id)
    return jsonify({"ok": True})


def _alert_round():
    """一轮告警巡检：拉取所有数据、判断阈值、发送告警和恢复通知，并顺带刷新缓存。"""
    cfg = load_config()
    data = fetch_all()  # 也会刷新远程缓存

    notif_cfg = cfg.get("notifications", {})
    thresholds = notif_cfg.get("thresholds", {})
    _notifier.update_config(notif_cfg)
    active_keys = set()
    for panel_result in data:
        if panel_result.get("kind") != "nightcord-status":
            continue  # 青源机器由下面的 plan_agent_alerts 统一判断
        for alert in evaluate_status_alerts(panel_result):
            active_keys.add(alert["key"])
            _notifier.notify(alert)
        if panel_result.get("online") is False:
            # 看不到 2702 的数据不等于它报过的故障都好了：保持原状，不发"已恢复"
            active_keys.update(_notifier.active_keys_with_prefix(
                nightcord_target_key_prefix(panel_result.get("name", ""))))

    # 青源（自建 agent）上报的机器：在线的按最新样本比阈值，
    # 离线的报离线（全部同时离线时合并成一条）。卸载掉的机器在设置页"移除"后就不再盯。
    now = time.time()
    agent_alerts, keep_keys = plan_agent_alerts(
        _agent_states(now), thresholds, now, AGENT_STALE_SECONDS, _notifier.active_keys_for,
    )
    active_keys |= keep_keys
    # 期间有人点了"移除"也没关系：Notifier 在每次发送那一刻现查名单（见 Notifier.watch_generations）
    for alert in agent_alerts:
        active_keys.add(alert["key"])
        _notifier.notify(alert)

    # 这轮没再出现的 key 就是问题解决了，从"已通知"名单里摘掉，以后再犯还能收到通知
    _notifier.resolve(active_keys)


def background_alert_loop():
    """后台巡检线程：每 ALERT_CHECK_INTERVAL 秒跑一轮 _alert_round。"""
    while True:
        try:
            _alert_round()
        except Exception as e:
            print(f"[background_alert_loop] 出错: {e}")
        time.sleep(ALERT_CHECK_INTERVAL)


def background_metrics_prune_loop():
    """后台线程：定期清理超过保留窗口的自建监控历史数据，防止 metrics.db 无限增长。"""
    while True:
        try:
            cfg = load_config().get("metrics_agent", {})
            retention_days = cfg.get("retention_days", 30)
            cutoff_ts = int(time.time() - retention_days * 86400)
            metrics_store.prune_older_than(cutoff_ts)
        except Exception as e:
            print(f"[background_metrics_prune_loop] 出错: {e}")
        time.sleep(METRICS_PRUNE_INTERVAL)


AUDIT_RETRY_INTERVAL = 60  # 秒


def background_audit_loop():
    """后台线程：上次没推出去的终端审计，每分钟重试一次。"""
    try:
        terminal.close_unfinished(_auditor, load_config())
    except Exception as e:
        print(f"[background_audit_loop] 补记未结束的终端出错: {e}")
    while True:
        try:
            _auditor.deliver_pending(load_config())
        except Exception as e:
            print(f"[background_audit_loop] 出错: {e}")
        time.sleep(AUDIT_RETRY_INTERVAL)


def start_background_workers():
    """巡检、历史清理、审计重试三个后台线程。生产用 gunicorn 时由 gunicorn.conf.py 在 worker 里调。"""
    t = threading.Thread(target=background_alert_loop, daemon=True)
    t.start()
    t2 = threading.Thread(target=background_metrics_prune_loop, daemon=True)
    t2.start()
    threading.Thread(target=background_audit_loop, daemon=True).start()


if __name__ == "__main__":
    start_background_workers()
    debug = os.environ.get("FLASK_DEBUG") == "1"
    port = int(os.environ.get("PORT", 1810))
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False, threaded=True)
