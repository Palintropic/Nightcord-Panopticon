"""
网页终端：机器清单、一次性票、并发上限、SSH 连接、浏览器和目标机之间的数据转发，以及审计推送。
路由在 app.py（/auth/terminal、/api/terminal/*）。

一条连接：浏览器 ⇄ WebSocket ⇄ Panopticon ⇄ SSH（只走 WireGuard）⇄ 目标机。
- 开终端前回 SSO 用 passkey 重新验证，验过发一张一次性票（绑定看板会话、账户、机器，60 秒有效），
  WebSocket 握手时原子地用掉；
- 登目标机用现签的短期证书（terminal_ca.py），主机指纹钉死在配置里，对不上就断；
- 空闲、单次时长、每人和全局的并发都有上限；
- 每次开 / 关都记一笔，并推到飞书和 Slack。本机数据库里的记录能被开到上海的终端改掉，
  所以外部推送才是正本：推不出去的进 audit_outbox，后台重试。
"""
import base64
import ipaddress
import json
import queue
import re
import secrets
import socket
import threading
import time
from datetime import datetime

import paramiko
import requests

import metrics_store

HOST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
LOGIN_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

TICKET_TTL = 60          # 秒，重新验证完到连上终端最多这么久
TICKETS_PER_SESSION = 3
MAX_PER_USER = 3
MAX_TOTAL = 8            # 每个终端占 gunicorn 一个线程，留够给看板本身
IDLE_SECONDS = 15 * 60   # 这么久没有键盘输入就断开
OUTPUT_BACKLOG = 256     # 发往浏览器的输出最多积压这么多块（每块最多 32 KB），浏览器读得太慢就断开
MAX_SECONDS = 2 * 3600   # 单次最长
CONNECT_TIMEOUT = 10
OUTBOX_MAX_ATTEMPTS = 100  # 后台每分钟重试一次，推不出去的审计最多重试这么多次
PUSH_TIMEOUT = 6

END_REASONS = {
    "exit": "目标机上的 shell 退出了",
    "browser_closed": "浏览器关掉了",
    "idle": f"{IDLE_SECONDS // 60} 分钟没有输入",
    "time_limit": f"超过单次最长 {MAX_SECONDS // 3600} 小时",
    "slow_browser": "浏览器收得太慢，输出积压太多",
    "host_key_mismatch": "主机指纹对不上，没有连",
    "connect_failed": "连不上目标机",
    "unknown": "Panopticon 重启时还开着，结束时间不确定",
}


# ---------- 配置 ----------

def _host_key(value):
    """'ssh-ed25519 AAAA…[ 注释]' → (类型, base64)。格式不对返回 None。"""
    parts = str(value or "").split()
    if len(parts) < 2 or parts[0] not in ("ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"):
        return None
    try:
        base64.b64decode(parts[1], validate=True)
    except ValueError:
        return None
    return parts[0], parts[1]


def settings_from(cfg):
    """
    config.yaml 的 terminal 段。没开返回 None。写错的机器跳过（打一行日志），不让整个终端功能挂掉。
      terminal:
        enabled: true
        source_address: 10.10.0.1        # Panopticon 自己的 WireGuard 地址，证书只认从这里来
        slack_webhook: https://hooks.slack.com/...
        hosts:
          tokyo: {label: 东京, address: 10.10.0.3, user: mizuki, host_key: "ssh-ed25519 AAAA..."}
    """
    t = (cfg or {}).get("terminal") or {}
    if not t.get("enabled"):
        return None
    try:
        source = str(ipaddress.ip_address(str(t.get("source_address") or "").strip()))
    except ValueError:
        print("[terminal] source_address 没填或格式不对，终端不可用")
        return None
    hosts = {}
    for host_id, h in (t.get("hosts") or {}).items():
        host_id = str(host_id)
        if not HOST_ID.match(host_id) or not isinstance(h, dict):
            print(f"[terminal] 跳过机器 {host_id!r}：名字或格式不对")
            continue
        key = _host_key(h.get("host_key"))
        user = str(h.get("user") or "")
        try:
            address = str(ipaddress.ip_address(str(h.get("address") or "").strip()))
            port = int(h.get("port") or 22)
        except (TypeError, ValueError):
            address, port = None, 0
        if not key or not LOGIN_USER.match(user) or not address or not 0 < port < 65536:
            print(f"[terminal] 跳过机器 {host_id!r}：地址、账户或主机指纹不对")
            continue
        hosts[host_id] = {
            "id": host_id, "label": str(h.get("label") or host_id)[:64], "address": address, "port": port,
            "user": user, "host_key_type": key[0], "host_key": key[1],
        }
    return {"source_address": source, "hosts": hosts, "slack_webhook": str(t.get("slack_webhook") or "").strip()}


# ---------- 一次性票 ----------

class Tickets:
    """
    重新验证通过后发的票，放在服务器内存里（只有一个 worker 进程）。票号存在看板会话 cookie 里，
    WebSocket 握手时按票号取出并立刻删除：同一张票只能用一次，换个会话也用不了。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def _purge(self, now):
        for token in [t for t, e in self._items.items() if e["expires"] <= now]:
            del self._items[token]

    def issue(self, sid, sub, email, name, host, now=None):
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._purge(now)
            mine = sorted((e["expires"], t) for t, e in self._items.items() if e["sid"] == sid)
            for _, old in mine[:max(0, len(mine) - TICKETS_PER_SESSION + 1)]:
                del self._items[old]
            self._items[token] = {"sid": sid, "sub": sub, "email": email, "name": name, "host": host,
                                  "expires": now + TICKET_TTL}
        return token

    def peek(self, token, sid, now=None):
        """还没用掉的票是给哪台机器的（前端回来时据此自动连上），不消耗它。"""
        now = time.time() if now is None else now
        with self._lock:
            entry = self._items.get(token or "")
            if entry and entry["sid"] == sid and entry["expires"] > now:
                return entry["host"]
        return None

    def take(self, token, sid, now=None):
        now = time.time() if now is None else now
        with self._lock:
            entry = self._items.pop(token or "", None)
        if not entry or not sid or entry["sid"] != sid or entry["expires"] <= now:
            return None
        return entry


# ---------- 并发上限 ----------

class Registry:
    def __init__(self, per_user=MAX_PER_USER, total=MAX_TOTAL):
        self.per_user = per_user
        self.total = total
        self._lock = threading.Lock()
        self._open = {}   # 会话号 -> sub

    def acquire(self, session_id, sub):
        with self._lock:
            if len(self._open) >= self.total:
                return "too_many_total"
            if sum(1 for s in self._open.values() if s == sub) >= self.per_user:
                return "too_many_mine"
            self._open[session_id] = sub
            return None

    def release(self, session_id):
        with self._lock:
            self._open.pop(session_id, None)


# ---------- SSH ----------

class HostKeyMismatch(Exception):
    pass


def connect(host, key, source_address, cols, rows, timeout=CONNECT_TIMEOUT):
    """
    连上目标机并开一个 pty shell，返回 (transport, channel)。
    socket 显式绑在 source_address 上：连 Panopticon 自己那台时也从 WireGuard 地址出发，跟证书里的
    source-address 对得上。只协商钉死的那种主机钥匙，指纹不对就断开、抛 HostKeyMismatch。
    """
    sock = socket.create_connection((host["address"], host["port"]), timeout=timeout,
                                    source_address=(source_address, 0))
    try:
        transport = paramiko.Transport(sock)
    except Exception:
        sock.close()
        raise
    try:
        if host["host_key_type"] == "ssh-rsa":
            transport.get_security_options().key_types = ("rsa-sha2-512", "rsa-sha2-256")
        else:
            transport.get_security_options().key_types = (host["host_key_type"],)
        transport.start_client(timeout=timeout)
        got = transport.get_remote_server_key()
        got_type = "ssh-rsa" if got.get_name().startswith("rsa-sha2") else got.get_name()
        if got_type != host["host_key_type"] or got.get_base64() != host["host_key"]:
            raise HostKeyMismatch(f"{host['id']}: got {got.get_name()} {got.get_base64()[:16]}…")
        transport.auth_publickey(host["user"], key)
        chan = transport.open_session(timeout=timeout)
        chan.get_pty(term="xterm-256color", width=cols, height=rows)
        chan.invoke_shell()
        return transport, chan
    except Exception:
        transport.close()
        raise


def clamp_size(cols, rows):
    def num(v, low, high, default):
        try:
            return max(low, min(high, int(v)))
        except (TypeError, ValueError):
            return default
    return num(cols, 10, 500, 80), num(rows, 4, 200, 24)


def bridge(ws, chan, closed_errors, clock=time.monotonic, idle=IDLE_SECONDS, limit=MAX_SECONDS):
    """
    在浏览器和目标机之间来回转发，直到一边结束。返回 (原因, 输入字节数, 输出字节数)。
    浏览器 → 这里：文本帧 JSON，{"t":"in","d":"..."} 是键盘输入，{"t":"resize","cols":..,"rows":..} 改窗口大小。
    这里 → 浏览器：二进制帧是终端输出。
    closed_errors：WebSocket 被对方关掉时 receive 抛的异常类型。
    """
    counts = {"in": 0, "out": 0}
    done = threading.Event()
    outcome = {}
    backlog = queue.Queue(maxsize=OUTPUT_BACKLOG)

    # 目标机 → 积压队列 → 浏览器，分成两个线程：浏览器不读时卡住的只是发送那个线程，
    # 读目标机的这边发现队列满了就结束会话（关掉之后发送线程也随 WebSocket 一起解开）。
    def pump():
        chan.settimeout(0.5)
        try:
            while not done.is_set():
                try:
                    data = chan.recv(32768)
                except socket.timeout:
                    continue
                if not data:
                    outcome.setdefault("reason", "exit")
                    break
                try:
                    backlog.put_nowait(data)
                except queue.Full:
                    outcome.setdefault("reason", "slow_browser")
                    break
        except Exception:
            outcome.setdefault("reason", "exit")
        finally:
            done.set()

    def deliver():
        try:
            while not (done.is_set() and backlog.empty()):
                try:
                    data = backlog.get(timeout=0.5)
                except queue.Empty:
                    continue
                ws.send(data)
                counts["out"] += len(data)
        except closed_errors:
            outcome.setdefault("reason", "browser_closed")
        except Exception:
            outcome.setdefault("reason", "browser_closed")
        finally:
            done.set()

    reader = threading.Thread(target=pump, daemon=True)
    sender = threading.Thread(target=deliver, daemon=True)
    reader.start()
    sender.start()
    started = last_input = clock()
    try:
        while not done.is_set():
            now = clock()
            if now - last_input >= idle:
                outcome.setdefault("reason", "idle")
                break
            if now - started >= limit:
                outcome.setdefault("reason", "time_limit")
                break
            try:
                raw = ws.receive(timeout=1)
            except closed_errors:
                outcome.setdefault("reason", "browser_closed")
                break
            if raw is None:
                continue
            try:
                msg = json.loads(raw) if isinstance(raw, str) else {}
            except ValueError:
                msg = {}
            if not isinstance(msg, dict):
                continue
            if msg.get("t") == "in" and isinstance(msg.get("d"), str):
                data = msg["d"].encode("utf-8")
                chan.sendall(data)
                counts["in"] += len(data)
                last_input = clock()
            elif msg.get("t") == "resize":
                chan.resize_pty(*clamp_size(msg.get("cols"), msg.get("rows")))
    finally:
        done.set()
        reader.join(timeout=2)
        sender.join(timeout=2)
    return outcome.get("reason", "exit"), counts["in"], counts["out"]


# ---------- 审计 ----------

def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


_PLAIN = str.maketrans({"<": "＜", ">": "＞", "[": "［", "]": "］", "*": "＊", "`": "＇", "\n": " ", "\r": " "})


def _plain(value):
    """
    放进推送正文的字段（名字是用户自己在 SSO 里填的）：去掉飞书 / Slack 会当成标记的字符，
    免得有人把名字改成 <at id=all></at> 或者一段链接。
    """
    return str(value if value is not None else "?").translate(_PLAIN)[:200]


def describe(event):
    """一条审计的正文：推到飞书和 Slack 的都是这段。"""
    who = f"{_plain(event.get('name'))}（{_plain(event.get('email') or event.get('sub'))}）"
    where = f"{_plain(event.get('label'))}（{_plain(event.get('host'))}，{_plain(event.get('address'))}）"
    lines = [
        f"时间：{_when(event['at'])}",
        f"谁：{who}",
        f"哪台：{where}",
        f"从：{_plain(event.get('client_ip'))}",
        f"会话：{_plain(event['session'])}",
    ]
    if event["kind"] == "close":
        minutes = max(0, int(event.get("duration") or 0)) // 60
        lines += [
            f"时长：{minutes} 分钟",
            f"输入 / 输出：{event.get('bytes_in', 0)} / {event.get('bytes_out', 0)} 字节",
            f"原因：{_plain(END_REASONS.get(event.get('reason'), event.get('reason')))}",
        ]
    title = "终端已打开" if event["kind"] == "open" else "终端已关闭"
    return title, lines


def _feishu_card(title, lines, template):
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "body": {"direction": "vertical",
                     "elements": [{"tag": "markdown", "content": "\n".join(f">{line}" for line in lines)}]},
        },
    }


class Auditor:
    def __init__(self, post=None):
        self._post = post or requests.post
        self._lock = threading.Lock()  # 同一条记录别被两个线程同时推两遍

    def record(self, event, cfg):
        """
        记一条审计，马上试着推出去；推不出去的留在 outbox 里等后台重试。同一个会话的「打开」「关闭」
        各只记一次（会话号 + 种类唯一），重复记录直接忽略。投递是「至少一次」：推成功了但还没来得及
        标记就崩溃的话，重启后会再推一遍。
        """
        row_id = metrics_store.outbox_add(f"{event['session']}:{event['kind']}", event, event["at"])
        if row_id:
            self.deliver_pending(cfg, only=row_id)

    def deliver_pending(self, cfg, only=None):
        with self._lock:
            for row in metrics_store.outbox_pending(OUTBOX_MAX_ATTEMPTS):
                if only is not None and row["id"] != only:
                    continue
                feishu = row["feishu_done"] or self._feishu(row["event"], cfg)
                slack = row["slack_done"] or self._slack(row["event"], cfg)
                metrics_store.outbox_mark(row["id"], feishu, slack)

    # 没配置的渠道当作已经推过（没有地方可推）；配置了但推失败的返回 False，下次再试
    def _feishu(self, event, cfg):
        fs = ((cfg or {}).get("notifications") or {}).get("feishu") or {}
        if not fs.get("enabled") or not fs.get("webhook"):
            return True
        title, lines = describe(event)
        try:
            resp = self._post(fs["webhook"], json=_feishu_card(title, lines, "purple"), timeout=PUSH_TIMEOUT)
            body = resp.json()
            return resp.status_code == 200 and body.get("code", body.get("StatusCode", -1)) == 0
        except Exception as e:
            print(f"[terminal] 审计推飞书失败: {e}")
            return False

    def _slack(self, event, cfg):
        webhook = (settings_from(cfg) or {}).get("slack_webhook") or ""
        if not webhook:
            return True
        title, lines = describe(event)
        try:
            resp = self._post(webhook, json={"text": f"*{title}*\n" + "\n".join(lines)}, timeout=PUSH_TIMEOUT)
            return resp.status_code == 200
        except Exception as e:
            print(f"[terminal] 审计推 Slack 失败: {e}")
            return False


def close_unfinished(auditor, cfg, now=None):
    """启动时调：上次进程退出时还开着的终端补记结束（原因 unknown），并推一条审计。"""
    now = time.time() if now is None else now
    for s in metrics_store.terminal_unfinished():
        if not metrics_store.terminal_ended(s["id"], now, s["bytes_in"], s["bytes_out"], "unknown"):
            continue  # 那边刚好自己记了结束，以它为准
        auditor.record({
            "kind": "close", "at": now, "session": s["id"], "sub": s["sub"], "email": s["email"],
            "host": s["host"], "label": s["host"], "address": "", "client_ip": s["client_ip"],
            "duration": now - s["started_at"], "bytes_in": s["bytes_in"], "bytes_out": s["bytes_out"],
            "reason": "unknown",
        }, cfg)
