"""
设置页 / 首次运行向导（OOBE）用的 config.yaml 读写逻辑。

设计原则：
- 敏感字段（飞书 webhook、邮箱密码、登录密码）从不回显明文给前端，
  GET 接口只返回"是否已设置"，前端表单里对应输入框留空提交 = 不修改。
- 只有真正填了新值才会覆盖旧值；开启的通知渠道必须把必填项填完整，
  否则整个保存请求失败并返回具体报错，不会把配置存成半残状态。
"""
from werkzeug.security import generate_password_hash

SECRET_PLACEHOLDER = "••••••••"


def redact_for_display(cfg: dict) -> dict:
    cfg = cfg or {}
    da = cfg.get("dashboard_auth", {}) or {}
    dashboard_auth = {
        "enabled": bool(da.get("enabled", False)),
        "username": da.get("username", ""),
        "has_password": bool(da.get("password_hash")),
        "max_attempts": da.get("max_attempts", 5),
        "lockout_seconds": da.get("lockout_seconds", 900),
    }

    notif = cfg.get("notifications", {}) or {}
    feishu = notif.get("feishu", {}) or {}
    email = notif.get("email", {}) or {}
    notifications = {
        "cooldown_seconds": notif.get("cooldown_seconds", 600),
        "thresholds": {
            "cpu": (notif.get("thresholds") or {}).get("cpu", 90),
            "mem": (notif.get("thresholds") or {}).get("mem", 90),
            "disk": (notif.get("thresholds") or {}).get("disk", 85),
        },
        "feishu": {
            "enabled": bool(feishu.get("enabled", False)),
            "webhook": SECRET_PLACEHOLDER if feishu.get("webhook") else "",
        },
        "email": {
            "enabled": bool(email.get("enabled", False)),
            "smtp_host": email.get("smtp_host", ""),
            "smtp_port": email.get("smtp_port", 465),
            "use_ssl": bool(email.get("use_ssl", True)),
            "use_tls": bool(email.get("use_tls", False)),
            "username": email.get("username", ""),
            "password": SECRET_PLACEHOLDER if email.get("password") else "",
            "from": email.get("from", ""),
            "to": email.get("to", []) or [],
        },
    }

    return {
        "dashboard_auth": dashboard_auth,
        "notifications": notifications,
        # 只读：设置页判断"是不是首次配置"用，这一段配置没有设置页 UI
        "nightcord_status_enabled": bool((cfg.get("nightcord_status") or {}).get("enabled")),
    }


def _clean_str(v):
    return (v or "").strip()


def merge_submitted(old_cfg: dict, submitted: dict):
    """返回 (merged_cfg, errors)。errors 非空时调用方不应保存。"""
    old_cfg = old_cfg or {}
    submitted = submitted or {}
    errors = []
    da_in = submitted.get("dashboard_auth", {}) or {}
    old_da = old_cfg.get("dashboard_auth", {}) or {}
    merged_da = {
        "enabled": bool(da_in.get("enabled", False)),
        "username": _clean_str(da_in.get("username")) or old_da.get("username", ""),
        "max_attempts": int(da_in.get("max_attempts") or old_da.get("max_attempts", 5) or 5),
        "lockout_seconds": int(da_in.get("lockout_seconds") or old_da.get("lockout_seconds", 900) or 900),
    }
    # session_secret / passkeys 不通过这个通用表单编辑（有各自专门的接口），
    # 原样带过去，不然保存一次设置就会把已登录的 session 和已注册的 Passkey 全清空。
    if old_da.get("session_secret"):
        merged_da["session_secret"] = old_da["session_secret"]
    if old_da.get("passkeys"):
        merged_da["passkeys"] = old_da["passkeys"]
    if "trusted_proxies" in old_da:
        merged_da["trusted_proxies"] = old_da["trusted_proxies"]  # 只能手改 config.yaml，见 auth.trusted_proxies_from
    new_password = _clean_str(da_in.get("new_password"))
    if new_password:
        merged_da["password_hash"] = generate_password_hash(new_password)
    elif old_da.get("password_hash"):
        merged_da["password_hash"] = old_da["password_hash"]
    elif merged_da["enabled"]:
        errors.append("已开启登录验证，但还没有设置密码")

    notif_in = submitted.get("notifications", {}) or {}
    old_notif = old_cfg.get("notifications", {}) or {}
    old_feishu = old_notif.get("feishu", {}) or {}
    old_email = old_notif.get("email", {}) or {}
    feishu_in = notif_in.get("feishu", {}) or {}
    email_in = notif_in.get("email", {}) or {}

    webhook = _clean_str(feishu_in.get("webhook"))
    if webhook in ("", SECRET_PLACEHOLDER):
        webhook = old_feishu.get("webhook", "")
    if feishu_in.get("enabled") and not webhook:
        errors.append("已开启飞书推送，但还没有填 Webhook 地址")

    email_password = _clean_str(email_in.get("password"))
    if email_password in ("", SECRET_PLACEHOLDER):
        email_password = old_email.get("password", "")
    if email_in.get("enabled") and not email_password:
        errors.append("已开启邮件推送，但还没有填密码/授权码")

    to_list = email_in.get("to")
    if isinstance(to_list, str):
        to_list = [x.strip() for x in to_list.split(",") if x.strip()]
    if not to_list:
        to_list = old_email.get("to", []) or []

    merged_notif = {
        "cooldown_seconds": int(notif_in.get("cooldown_seconds") or old_notif.get("cooldown_seconds", 600) or 600),
        "thresholds": {
            "cpu": int((notif_in.get("thresholds") or {}).get("cpu") or 90),
            "mem": int((notif_in.get("thresholds") or {}).get("mem") or 90),
            "disk": int((notif_in.get("thresholds") or {}).get("disk") or 85),
        },
        "feishu": {
            "enabled": bool(feishu_in.get("enabled", False)),
            "webhook": webhook,
        },
        "email": {
            "enabled": bool(email_in.get("enabled", False)),
            "smtp_host": _clean_str(email_in.get("smtp_host")) or old_email.get("smtp_host", ""),
            "smtp_port": int(email_in.get("smtp_port") or old_email.get("smtp_port", 465) or 465),
            "use_ssl": bool(email_in.get("use_ssl", old_email.get("use_ssl", True))),
            "use_tls": bool(email_in.get("use_tls", old_email.get("use_tls", False))),
            "username": _clean_str(email_in.get("username")) or old_email.get("username", ""),
            "password": email_password,
            "from": _clean_str(email_in.get("from")) or old_email.get("from", ""),
            "to": to_list,
        },
    }

    # 从 old_cfg 出发再覆盖显式处理过的顶层键，而不是整个重建：
    # 这样 nightcord_status / metrics_agent 这些没有设置页 UI 的顶层配置段
    # 不会在保存 /settings 表单时被静默抹掉。
    merged = dict(old_cfg)
    # 宝塔面板数据源已经去掉了，旧配置里留下的 panels（带着面板 API 密钥）保存时顺手清掉
    merged.pop("panels", None)
    merged.update({"dashboard_auth": merged_da, "notifications": merged_notif})
    return merged, errors


# 设置 → 主题：曲线、正常磁贴、底部状态栏各自的颜色，存在 config.yaml 的 appearance 段，
# 所有设备共用。没设置的部分用 25 时紫（跟 uwp.css 里的 --SystemAccent 一致）。
APPEARANCE_PARTS = ("curve", "tile", "bar")
DEFAULT_ACCENT = "#884499"


def _clean_hex(v):
    """'#AbC123' / 'abc123' → '#abc123'；其它一律 None。"""
    if not isinstance(v, str):
        return None
    s = v.strip().lstrip("#")
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        return None
    return "#" + s.lower()


def appearance_of(cfg: dict) -> dict:
    stored = (cfg or {}).get("appearance") or {}
    return {part: _clean_hex(stored.get(part)) or DEFAULT_ACCENT for part in APPEARANCE_PARTS}


def clean_appearance(submitted: dict):
    """返回 (colours, errors)。三项都要给，格式是 #rrggbb。"""
    colours, errors = {}, []
    names = {"curve": "曲线", "tile": "磁贴", "bar": "状态栏"}
    for part in APPEARANCE_PARTS:
        hex_value = _clean_hex((submitted or {}).get(part))
        if hex_value is None:
            errors.append(f"{names[part]}颜色要写成 #rrggbb")
        else:
            colours[part] = hex_value
    return colours, errors


# 设置 → 颜文字：哪台机器由哪个角色看（assign: 机器名 → 角色名），以及改过的表情
# （faces: 角色名 → [正常, 接近阈值, 离线或出事]，空字符串 = 用默认）。角色表本身在前端，
# 这里只管格式和长度，不认识的角色名前端会退回自动分配。
KAOMOJI_FACE_MAX = 24
_KAOMOJI_NAME_MAX = 16
_KAOMOJI_MACHINE_MAX = 128
_KAOMOJI_MAX_ENTRIES = 200


def _plain_text(v, limit):
    """去掉首尾空白；不是字符串、太长或带控制字符的返回 None。"""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if len(s) > limit or any(ord(c) < 32 or ord(c) == 127 for c in s):
        return None
    return s


def kaomoji_of(cfg: dict) -> dict:
    stored = (cfg or {}).get("kaomoji") or {}
    cleaned, _ = clean_kaomoji(stored if isinstance(stored, dict) else {})
    return cleaned


def clean_kaomoji(submitted: dict):
    """返回 (kaomoji, errors)。坏掉的条目报错，不静默丢掉。"""
    errors = []
    assign_in = (submitted or {}).get("assign") or {}
    faces_in = (submitted or {}).get("faces") or {}
    if not isinstance(assign_in, dict) or not isinstance(faces_in, dict):
        return {"assign": {}, "faces": {}}, ["格式不对"]
    if len(assign_in) > _KAOMOJI_MAX_ENTRIES or len(faces_in) > _KAOMOJI_MAX_ENTRIES:
        return {"assign": {}, "faces": {}}, ["条目太多"]

    assign = {}
    for machine, chara in assign_in.items():
        m = _plain_text(machine, _KAOMOJI_MACHINE_MAX)
        c = _plain_text(chara, _KAOMOJI_NAME_MAX)
        if not m or not c:
            errors.append(f"机器【{str(machine)[:32]}】的角色不对")
            continue
        assign[m] = c

    faces = {}
    for chara, triple in faces_in.items():
        c = _plain_text(chara, _KAOMOJI_NAME_MAX)
        if not c or not isinstance(triple, list) or len(triple) != 3:
            errors.append(f"【{str(chara)[:16]}】的表情要正好三个")
            continue
        cleaned = [_plain_text(f, KAOMOJI_FACE_MAX) for f in triple]
        if any(f is None for f in cleaned):
            errors.append(f"【{c}】的表情最多 {KAOMOJI_FACE_MAX} 个字，也不能换行")
            continue
        if any(cleaned):
            faces[c] = cleaned
    return {"assign": assign, "faces": faces}, errors
