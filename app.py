"""
宝塔多面板聚合 Dashboard 后端
用法：
    pip install -r requirements.txt
    cp config.example.yaml config.yaml   # 填入各面板地址与 API 密钥
    python app.py
然后浏览器打开 http://127.0.0.1:1810
"""
import hmac
import json
import os
import queue
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context, session, redirect, url_for
from werkzeug.security import check_password_hash

import webauthn_manager as wam
from bt_client import BTClient
from nightcord_status_client import NightcordStatusClient
from notifier import Notifier, evaluate_alerts, plan_agent_alerts
from auth import BruteForceGuard, get_client_ip
from config_editor import redact_for_display, merge_submitted
from agent_deploy import deploy_agent, AgentDeployError
import agent_hosts
from config_store import load_yaml, save_yaml, update_yaml
import metrics_store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
metrics_store.init_db()

_brute_force_guard = BruteForceGuard()  # 默认 5 次失败锁 15 分钟，实际阈值以 config.yaml 为准（见 enforce_auth）

# 不需要登录就能访问的路由：登录页本身，以及登录相关的两个 API（否则没法登录）
PUBLIC_ENDPOINTS = {"login_page", "login_password", "logout", "webauthn_login_options", "webauthn_login_verify"}

# 简单内存缓存，避免每次刷新都重新请求所有面板
_cache = {"data": None, "ts": 0}
CACHE_TTL = 10  # 秒

# 常驻一个 Notifier 实例，不要在 background_alert_loop 每轮里 new 一个新的——
# 那样它内部记的"这个问题已经通知过"的状态每 60 秒就被清空一次，等于永远发不完。
_notifier = Notifier({}, watch_generations=metrics_store.watch_generations)

ALERT_CHECK_INTERVAL = 60  # 秒，后台告警巡检间隔（与前端是否打开无关）
AGENT_STALE_SECONDS = 180  # 秒，青源 agent 超过这么久没上报就不当它在线（默认上报间隔 60s，留够 3 倍余量）
METRICS_PRUNE_INTERVAL = 3600  # 秒，自建监控 agent 历史数据的清理巡检间隔
METRICS_REPORT_PATH = "/api/metrics/report"
AGENT_HEARTBEAT_PATH = "/api/agents/heartbeat"
QINGYUAN_VERSION = "7.17-beta"  # 青源（自建系统指标监控架构）版本号，见 QINGYUAN.zh-CN.md


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError(
            f"未找到配置文件 {CONFIG_PATH}，请先复制 config.example.yaml 为 config.yaml 并填写面板信息"
        )
    return load_yaml(CONFIG_PATH)


def load_panels():
    return load_config().get("panels", [])


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


@app.before_request
def enforce_auth():
    """
    Dashboard 按设计要长期暴露公网，所以给所有路由加一层登录门槛（密码 + 可选 Passkey），
    并按来源 IP 做暴力破解锁定。dashboard_auth.enabled 为 false（默认）时不做任何限制，
    适合只在 WireGuard 内网访问、不打算公网暴露的部署方式。
    """
    if request.path == METRICS_REPORT_PATH:
        return  # agent 机器对机器上报，鉴权走独立的共享密钥（见 api_metrics_report），不走登录态鉴权
    if request.path == AGENT_HEARTBEAT_PATH:
        return  # 2702 机器对机器拉取，鉴权走 status_api.token（见 api_agent_heartbeat）
    try:
        cfg = load_config()
    except RuntimeError:
        return  # 还没配置 config.yaml，让后续逻辑走正常的报错路径
    auth_cfg = cfg.get("dashboard_auth", {})
    if not auth_cfg.get("enabled"):
        return

    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint == "static":
        return
    if session.get("authed"):
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "未登录"}), 401
    return redirect(url_for("login_page"))


@app.route("/login", methods=["GET"])
def login_page():
    return send_from_directory(app.static_folder, "login.html")


@app.route("/login", methods=["POST"])
def login_password():
    cfg = load_config()
    auth_cfg = cfg.get("dashboard_auth", {})
    ip = get_client_ip(request)
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
    session.clear()
    session["authed"] = True
    session.permanent = True
    return jsonify({"ok": True})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


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
    ip = get_client_ip(request)
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
    ip = get_client_ip(request)
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
    session.clear()
    session["authed"] = True
    session.permanent = True
    return jsonify({"ok": True})


def fetch_all():
    panels = load_panels()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(panels))) as pool:
        futures = {
            pool.submit(
                BTClient(p["name"], p["url"], p["api_key"], p.get("verify_ssl", False)).collect_all
            ): p
            for p in panels
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                p = futures[fut]
                results.append({"name": p.get("name"), "url": p.get("url"), "online": False, "error": str(e)})
    # 保持配置顺序
    order = {p["name"]: i for i, p in enumerate(panels)}
    results.sort(key=lambda r: order.get(r.get("name"), 999))

    ns_cfg = load_config().get("nightcord_status", {})
    if ns_cfg.get("enabled"):
        try:
            ns_result = NightcordStatusClient(
                ns_cfg.get("name", "Nightcord-Status"),
                ns_cfg["url"],
                ns_cfg.get("verify_ssl", False),
            ).collect_all()
        except Exception as e:
            ns_result = {"name": ns_cfg.get("name", "Nightcord-Status"), "url": ns_cfg.get("url"),
                         "online": False, "error": str(e), "kind": "nightcord-status", "targets": []}
        results.append(ns_result)

    _merge_qingyuan(results)
    return results


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
            "disk_detail": metrics_store.get_disk_detail(panel),
            "ip_internal": ip.get("ip_internal"),
            "ip_external": ip.get("ip_external"),
        })
    return states


def _merge_qingyuan(results):
    """
    把青源（自建 agent）最新上报的数据叠加进 fetch_all() 的结果里：
    - 面板名跟某个宝塔面板对得上：把青源数据挂在同一张卡片上（同名机器装了两边），
      前端展示时青源数据优先（本机 agent 直采，比宝塔面板 API 转发的更实时更准）。
    - 名字对不上但 IP 对得上：兜底按 IP 合并——宝塔面板名字是手打的、青源 panel_name
      默认取自远端 hostname，两套命名本来就没有必然关系，光靠名字很容易漏判（同一台机器
      拆成两张卡片）。一键部署时选了"绑定到已有面板"就不会走到这个兜底分支，这里主要是
      给部署在先的旧 agent、或者没走绑定流程的场景兜底。
    - 名字、IP 都对不上：说明这台机器只装了青源，没有对应的宝塔面板，单独给它造一张卡片，
      否则它在主看板上永远不可见（只能去设置页的"已部署 agent"列表里找）。
    离线的机器也照样出卡片（灰掉、显示最后一次的数据），不会因为不上报了就从看板上消失；
    只有在设置页"移除"之后才不再显示。
    """
    by_name = {r.get("name"): r for r in results}
    by_ip = {}
    for r in results:
        host = urlparse(r.get("url") or "").hostname
        if host:
            by_ip.setdefault(host, r)

    now = time.time()
    for state in _agent_states(now):
        panel_name = state["panel"]
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
            "ts": sample.get("ts"),
            "ip_internal": state["ip_internal"],
            "ip_external": state["ip_external"],
            "online": state["online"],
            "seen_at": state["seen_at"],
            "error": offline_error,
        }
        existing = (
            by_name.get(panel_name)
            or by_ip.get(state["ip_internal"])
            or by_ip.get(state["ip_external"])
        )
        if existing is not None:
            existing["qingyuan"] = qingyuan
        else:
            results.append({
                "name": panel_name,
                "url": None,
                "online": state["online"],
                "error": offline_error,
                "kind": "qingyuan-only",
                "qingyuan": qingyuan,
            })


@app.route("/api/status")
def api_status():
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return jsonify({"panels": _cache["data"], "cached": True, "ts": _cache["ts"]})
    try:
        data = fetch_all()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    _cache["data"] = data
    _cache["ts"] = now
    return jsonify({"panels": data, "cached": False, "ts": now})


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

    metrics_store.insert_sample(panel, ts, cpu, mem, disk, net_in_kbps, net_out_kbps)
    metrics_store.mark_seen(panel, time.time())
    metrics_store.upsert_ip(panel, body.get("ip_internal"), body.get("ip_external"))

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
    """给前端历史趋势图用的读接口，跟其它面板数据一样走 dashboard_auth。"""
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
    _cache["data"] = None  # 看板下次刷新就不再显示这张卡
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/setup")
def setup_page():
    # /setup 和 /settings 是同一个页面：面板列表为空时它就是"首次配置向导"，
    # 已经配置过之后就是普通的设置页，逻辑都在前端 JS 里根据 GET /api/config 的返回判断。
    return send_from_directory(app.static_folder, "settings.html")


@app.route("/settings")
def settings_page():
    return send_from_directory(app.static_folder, "settings.html")


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
    _cache["data"] = None  # 配置变了，强制下次 /api/status 重新拉取而不是用旧缓存
    return jsonify({"ok": True})


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
        return jsonify({"error": "还没有 config.yaml，先在设置页保存至少一台面板"}), 400

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
    shared_secret = cfg["metrics_agent"]["shared_secret"]

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
            panel_name = deploy_agent(
                ip, port, ssh_user, password, report_url, shared_secret,
                log=lambda line: q.put(("log", line)),
                panel_name_override=panel_name_override,
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
                _cache["data"] = None  # 新 agent 上线后下一次 /api/status 应该重新拉取一次
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

    bind_panel = (body.get("bind_panel") or "").strip() or None
    if bind_panel:
        # 这台机器如果同时也是某个已配置的宝塔面板，让青源上报强制用那个面板的 name，
        # 不然宝塔名字（手打的）和青源 panel_name（默认取自远端 hostname）大概率对不上，
        # 同一台机器会在 dashboard 上拆成两张卡片。
        try:
            cfg = load_config()
        except RuntimeError:
            return jsonify({"error": "还没有 config.yaml"}), 400
        known_names = {p.get("name") for p in cfg.get("panels", [])}
        if bind_panel not in known_names:
            return jsonify({"error": f"没找到名为「{bind_panel}」的宝塔面板"}), 400

    return _deploy_and_report(ip, port, ssh_user, password, remember, panel_name_override=bind_panel)


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
    # 沿用上次部署时实际用的 panel_name（不管当初是自动读的 hostname 还是手动绑定的宝塔面板名），
    # 不重新探测 hostname——不然身份会在每次重新部署之间漂移，之前靠名字/IP 对上的卡片又会拆开。
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


def background_alert_loop():
    """后台巡检线程：定期拉取所有面板数据、判断阈值、发送告警，并顺带刷新缓存。"""
    while True:
        try:
            cfg = load_config()
            data = fetch_all()
            _cache["data"] = data
            _cache["ts"] = time.time()

            notif_cfg = cfg.get("notifications", {})
            thresholds = notif_cfg.get("thresholds", {})
            _notifier.update_config(notif_cfg)
            active_keys = set()
            for panel_result in data:
                if panel_result.get("kind") == "qingyuan-only":
                    continue  # 只装了青源的机器由下面的 plan_agent_alerts 统一判断，这里再报就重复了
                for alert in evaluate_alerts(panel_result, thresholds):
                    active_keys.add(alert["key"])
                    _notifier.notify(alert)

            # 青源（自建 agent）上报的机器不一定跟宝塔面板重名，要单独判断：在线的按最新样本比阈值，
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


def start_background_workers():
    """Start both periodic jobs for the single-process development server."""
    t = threading.Thread(target=background_alert_loop, daemon=True)
    t.start()
    t2 = threading.Thread(target=background_metrics_prune_loop, daemon=True)
    t2.start()


if __name__ == "__main__":
    start_background_workers()
    debug = os.environ.get("FLASK_DEBUG") == "1"
    port = int(os.environ.get("PORT", 1810))
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False, threaded=True)
