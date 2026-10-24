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
import os
import queue
import re
import secrets
import socket
import threading
import time
import unicodedata
from datetime import datetime

import paramiko
import requests

import metrics_store

# 这个进程的标记，记在每个终端会话上：重启时只替别的（已经不在的）进程补记结束，不碰自己正开着的
WORKER_ID = f"{os.getpid()}-{secrets.token_hex(4)}"

HOST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
LOGIN_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

TICKET_TTL = 60          # 秒，重新验证完到连上终端最多这么久
TICKETS_PER_SESSION = 3
MAX_PER_USER = 3
MAX_TOTAL = 8            # 每个终端占 gunicorn 一个线程，留够给看板本身
IDLE_SECONDS = 15 * 60   # 这么久没有键盘输入就断开
OUTPUT_BACKLOG = 64      # 发往浏览器的输出最多积压这么多块（每块最多 32 KB）；满了就让 SSH 那边等着
STALL_SECONDS = 30       # 积压满了、这么久一块都推不出去，才算浏览器不读了
DRAIN_SECONDS = 10       # 目标机已经说完、等积压发完最多这么久
MAX_SECONDS = 2 * 3600   # 单次最长
CONNECT_TIMEOUT = 10
OUTBOX_MAX_ATTEMPTS = 100  # 后台每分钟重试一次，推不出去的审计最多重试这么多次
PUSH_TIMEOUT = 6

END_REASONS = {
    "exit": "目标机上的 shell 退出了",
    "browser_closed": "浏览器关掉了",
    "idle": f"{IDLE_SECONDS // 60} 分钟没有输入",
    "time_limit": f"超过单次最长 {MAX_SECONDS // 3600} 小时",
    "slow_browser": f"浏览器 {STALL_SECONDS} 秒都没收输出",
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


def no_delay(sock):
    """
    关掉 Nagle：按键和回显都是几个字节的小包，开着 Nagle 时打字一快，后一个包要等前一个的 ACK
    （隔着 WireGuard 一个往返几十毫秒），手感一顿一顿的。设不上（不是 TCP、已经断了）就算了。
    """
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (AttributeError, OSError):
        pass


def connect(host, key, source_address, cols, rows, timeout=CONNECT_TIMEOUT):
    """
    连上目标机并开一个 pty shell，返回 (transport, channel)。
    socket 显式绑在 source_address 上：连 Panopticon 自己那台时也从 WireGuard 地址出发，跟证书里的
    source-address 对得上。只协商钉死的那种主机钥匙，指纹不对就断开、抛 HostKeyMismatch。
    """
    sock = socket.create_connection((host["address"], host["port"]), timeout=timeout,
                                    source_address=(source_address, 0))
    no_delay(sock)
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


def bridge(ws, chan, closed_errors, abort, clock=time.monotonic, idle=IDLE_SECONDS, limit=MAX_SECONDS,
           stall=STALL_SECONDS, drain=DRAIN_SECONDS):
    """
    在浏览器和目标机之间来回转发，直到一边结束。返回 (原因, 输入字节数, 输出字节数)。
    浏览器 → 这里：文本帧 JSON，{"t":"in","d":"..."} 是键盘输入，{"t":"resize","cols":..,"rows":..} 改窗口大小。
    这里 → 浏览器：二进制帧是终端输出。
    closed_errors：WebSocket 被对方关掉时 receive / send 抛的异常类型。
    abort：断开浏览器那条连接的函数。中途要结束时调它，卡在发送里的线程才能解开。

    三个线程：读目标机（pump）→ 积压队列 → 发给浏览器（deliver，转发期间唯一往 WebSocket 写的线程）；
    本线程读浏览器的输入。积压满了 pump 就等着，SSH 自己的流量控制会让目标机慢下来——大输出只会
    变慢，不会被截断；积压满着、stall 秒一块都推不出去，才当浏览器不读了。返回之前两个线程都已退出，
    之后调用方可以放心往 WebSocket 写最后一句。
    """
    counts = {"in": 0, "out": 0}
    stop = threading.Event()           # 要中途结束：两个线程都该收手
    talked_out = threading.Event()     # 目标机已经说完（EOF），积压发完 deliver 就退出
    outcome = {}
    backlog = queue.Queue(maxsize=OUTPUT_BACKLOG)

    def finish(reason):
        outcome.setdefault("reason", reason)
        stop.set()

    def pump():
        chan.settimeout(0.5)
        try:
            while not stop.is_set():
                try:
                    data = chan.recv(32768)
                except socket.timeout:
                    continue
                if not data:
                    outcome.setdefault("reason", "exit")
                    break
                waited_since = clock()
                while not stop.is_set():
                    try:
                        backlog.put(data, timeout=0.5)
                        break
                    except queue.Full:
                        if clock() - waited_since >= stall:
                            finish("slow_browser")
        except Exception:
            outcome.setdefault("reason", "exit")
        finally:
            talked_out.set()

    def deliver():
        try:
            while not stop.is_set():
                try:
                    data = backlog.get(timeout=0.5)
                except queue.Empty:
                    if talked_out.is_set():
                        break
                    continue
                ws.send(data)
                counts["out"] += len(data)
        except closed_errors:
            finish("browser_closed")
        except Exception:
            finish("browser_closed")

    reader = threading.Thread(target=pump, daemon=True)
    sender = threading.Thread(target=deliver, daemon=True)
    reader.start()
    sender.start()
    started = last_input = clock()
    drain_from = None
    try:
        while not stop.is_set():
            now = clock()
            if not sender.is_alive():
                break                                  # 说完了也发完了，或者浏览器那边断了
            if talked_out.is_set():
                drain_from = drain_from if drain_from is not None else now
                if now - drain_from >= drain:
                    finish("slow_browser")
                    break
            if now - last_input >= idle:
                finish("idle")
                break
            if now - started >= limit:
                finish("time_limit")
                break
            try:
                raw = ws.receive(timeout=0.5)
            except closed_errors:
                finish("browser_closed")
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
        stop.set()
        if sender.is_alive():
            sender.join(timeout=1)
        if sender.is_alive():          # 卡在发送里：断开浏览器那条连接，它才会出来
            try:
                abort()
            except Exception:
                pass
            sender.join(timeout=5)
        reader.join(timeout=5)
    return outcome.get("reason", "exit"), counts["in"], counts["out"]


# ---------- 审计 ----------

def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _plain(value):
    """
    放进推送正文的字段（名字是用户自己在 SSO 里填的）。推送本身已经是纯文本（飞书 plain_text、
    Slack 关掉 mrkdwn），这里再去掉不可见的格式字符（方向控制、零宽、行 / 段分隔）和换行，
    并把 :// 拆开，免得被自动识别成可点的链接。
    """
    text = str(value if value is not None else "?")
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in text if unicodedata.category(c) != "Cf")
    return text.replace("://", ": //")[:200]


def describe(event):
    """一条审计：标题和正文各行。推到飞书和 Slack 的都是这些。"""
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
    # 正文用 plain_text，不是 markdown：用户填的名字不会被当成 @、链接或格式
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "body": {"direction": "vertical",
                     "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)}}]},
        },
    }


def _slack_text(title, lines):
    # mrkdwn 关掉，再按 Slack 的要求转义 & < >：不会有 @channel、链接或格式
    text = "\n".join([f"【{title}】"] + lines)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


CLAIM_SECONDS = 60   # 推一条审计时占住它这么久，别的进程不会同时推同一条


class Auditor:
    """
    审计先和对应的会话记录在同一个事务里落进 audit_outbox（metrics_store.terminal_record_open /
    terminal_record_close），再推出去；推不出去的后台重试。投递是「至少一次」：推成功了但还没来得及
    标记就崩溃的话，重启后会再推一遍。
    """

    def __init__(self, post=None):
        self._post = post or requests.post

    def open(self, cfg, session_id, sub, email, host, client_ip, cert_serial, event):
        """记打开。本地记录失败直接抛——没有审计就不开终端。"""
        row_id = metrics_store.terminal_record_open(session_id, WORKER_ID, sub, email, host, client_ip,
                                                    cert_serial, event["at"], event)
        if row_id:
            self.deliver_pending(cfg, only=row_id)

    def close(self, cfg, event):
        """记结束。已经被别的进程记过的不再记，返回 False。"""
        row_id = metrics_store.terminal_record_close(event["session"], event["at"], event.get("bytes_in", 0),
                                                     event.get("bytes_out", 0), event.get("reason"), event)
        if row_id:
            self.deliver_pending(cfg, only=row_id)
        return row_id is not None

    def deliver_pending(self, cfg, only=None, now=None):
        for row in metrics_store.outbox_pending(OUTBOX_MAX_ATTEMPTS):
            if only is not None and row["id"] != only:
                continue
            if not metrics_store.outbox_claim(row["id"], time.time() if now is None else now, CLAIM_SECONDS):
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
            resp = self._post(webhook, json={"text": _slack_text(title, lines), "mrkdwn": False},
                              timeout=PUSH_TIMEOUT)
            return resp.status_code == 200
        except Exception as e:
            print(f"[terminal] 审计推 Slack 失败: {e}")
            return False


def close_unfinished(auditor, cfg, now=None):
    """
    替已经不在的进程补记它们留下的没结束的会话（原因 unknown）。本进程自己的不碰；调用方要等旧进程
    肯定退出了（gunicorn 的 graceful_timeout 之后）再调，不然会把旧进程还开着的终端提前记成 unknown。
    一条出错不影响别的。
    """
    now = time.time() if now is None else now
    for s in metrics_store.terminal_unfinished(exclude_worker=WORKER_ID):
        try:
            auditor.close(cfg, {
                "kind": "close", "at": now, "session": s["id"], "sub": s["sub"], "email": s["email"],
                "host": s["host"], "label": s["host"], "address": "", "client_ip": s["client_ip"],
                "duration": now - s["started_at"], "bytes_in": s["bytes_in"], "bytes_out": s["bytes_out"],
                "reason": "unknown",
            })
        except Exception as e:
            print(f"[terminal] 补记会话 {s['id']} 的结束失败: {e}")
