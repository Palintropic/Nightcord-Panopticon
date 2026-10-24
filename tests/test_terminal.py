import base64
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

import metrics_store

_TMP = tempfile.TemporaryDirectory()
metrics_store.DB_PATH = os.path.join(_TMP.name, "metrics.db")

import app  # noqa: E402
import terminal  # noqa: E402
import terminal_ca  # noqa: E402
from test_sso import SITE, SsoTestCase  # noqa: E402


def server_key():
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
    return paramiko.Ed25519Key.from_private_key(io.StringIO(pem))


class CaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = terminal_ca.CA_PATH
        terminal_ca.CA_PATH = os.path.join(self.tmp.name, "terminal_ca")

    def tearDown(self):
        terminal_ca.CA_PATH = self.old
        self.tmp.cleanup()

    def test_certificate_carries_only_what_the_design_allows(self):
        key, serial = terminal_ca.issue("mizuki", "sub-1/tokyo/abcd", "10.10.0.1", now=1_000_000)
        cert = serialization.load_ssh_public_identity(key.public_blob.key_type.encode() + b" "
                                                      + base64.b64encode(key.public_blob.key_blob))
        self.assertEqual(cert.type, serialization.SSHCertificateType.USER)
        self.assertEqual(cert.valid_principals, [b"mizuki"])
        self.assertEqual(cert.key_id, b"sub-1/tokyo/abcd")
        self.assertEqual(cert.critical_options, {b"source-address": b"10.10.0.1"})
        self.assertEqual(cert.extensions, {b"permit-pty": b""})      # no forwarding, no user rc
        self.assertEqual(cert.valid_before - 1_000_000, terminal_ca.CERT_TTL)
        self.assertEqual(cert.serial, serial)
        cert.verify_cert_signature()
        ca_pub = serialization.load_ssh_public_key(terminal_ca.public_line().encode())
        self.assertEqual(cert.signature_key().public_bytes(serialization.Encoding.OpenSSH,
                                                           serialization.PublicFormat.OpenSSH),
                         ca_pub.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH))

    def test_ca_key_file_is_private_and_reused(self):
        first = terminal_ca.public_line()
        self.assertEqual(oct(os.stat(terminal_ca.CA_PATH).st_mode & 0o777), "0o600")
        self.assertEqual(terminal_ca.public_line(), first)


PINNED = "ssh-ed25519 " + server_key().get_base64()


class SettingsTests(unittest.TestCase):
    KEY = PINNED

    def test_bad_hosts_are_skipped(self):
        cfg = {"terminal": {"enabled": True, "source_address": "10.10.0.1", "hosts": {
            "tokyo": {"address": "10.10.0.3", "user": "mizuki", "host_key": self.KEY, "label": "东京"},
            "Bad Name": {"address": "10.10.0.4", "user": "mizuki", "host_key": self.KEY},
            "nokey": {"address": "10.10.0.4", "user": "mizuki", "host_key": "nope"},
            "rootish": {"address": "10.10.0.4", "user": "root; rm", "host_key": self.KEY},
            "noaddr": {"address": "example.com", "user": "mizuki", "host_key": self.KEY},
        }}}
        s = terminal.settings_from(cfg)
        self.assertEqual(list(s["hosts"]), ["tokyo"])
        self.assertEqual(s["hosts"]["tokyo"]["host_key_type"], "ssh-ed25519")

    def test_off_or_without_source_address(self):
        self.assertIsNone(terminal.settings_from({}))
        self.assertIsNone(terminal.settings_from({"terminal": {"enabled": True}}))


class TicketTests(unittest.TestCase):
    def test_a_ticket_is_used_once_by_its_own_session(self):
        t = terminal.Tickets()
        token = t.issue("sid-a", "sub", "e", "n", "tokyo", now=100)
        self.assertIsNone(t.take(token, "sid-b", now=101))     # another session: refused, and it is gone
        token = t.issue("sid-a", "sub", "e", "n", "tokyo", now=100)
        self.assertEqual(t.peek(token, "sid-a", now=101), "tokyo")
        self.assertEqual(t.take(token, "sid-a", now=101)["host"], "tokyo")
        self.assertIsNone(t.take(token, "sid-a", now=101))

    def test_tickets_expire(self):
        t = terminal.Tickets()
        token = t.issue("sid", "sub", "e", "n", "tokyo", now=100)
        self.assertIsNone(t.take(token, "sid", now=100 + terminal.TICKET_TTL))

    def test_concurrent_takes_get_one_winner(self):
        t = terminal.Tickets()
        token = t.issue("sid", "sub", "e", "n", "tokyo")
        results = []
        threads = [threading.Thread(target=lambda: results.append(t.take(token, "sid"))) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(sum(1 for r in results if r), 1)

    def test_a_session_holds_at_most_three(self):
        t = terminal.Tickets()
        tokens = [t.issue("sid", "sub", "e", "n", "tokyo", now=100 + i) for i in range(5)]
        self.assertEqual([t.peek(x, "sid", now=110) is not None for x in tokens], [False, False, True, True, True])


class RegistryTests(unittest.TestCase):
    def test_limits(self):
        r = terminal.Registry(per_user=2, total=3)
        self.assertIsNone(r.acquire("1", "a"))
        self.assertIsNone(r.acquire("2", "a"))
        self.assertEqual(r.acquire("3", "a"), "too_many_mine")
        self.assertIsNone(r.acquire("3", "b"))
        self.assertEqual(r.acquire("4", "c"), "too_many_total")
        r.release("1")
        self.assertIsNone(r.acquire("4", "c"))


# ---------- a real SSH conversation against an in-process paramiko server ----------

class EchoServer(paramiko.ServerInterface):
    def __init__(self):
        self.users = []
        self.pty = None
        self.event = threading.Event()

    def check_auth_publickey(self, username, key):
        self.users.append((username, key.public_blob.key_type if key.public_blob else key.get_name()))
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_password(self, username, password):
        self.users.append((username, "password"))
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey,password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
        self.pty = (term.decode() if isinstance(term, bytes) else term, width, height)
        return True

    def check_channel_shell_request(self, channel):
        self.event.set()
        return True

    def check_channel_window_change_request(self, channel, width, height, pw, ph):
        self.pty = (self.pty[0], width, height)
        return True


def start_server(host_key):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    iface = EchoServer()
    state = {}

    def serve():
        conn, peer = listener.accept()
        state["peer"] = peer
        t = paramiko.Transport(conn)
        t.add_server_key(host_key)
        t.start_server(server=iface)
        chan = t.accept(10)
        if chan is None:
            return
        iface.event.wait(5)
        chan.send(b"hello\r\n")
        while True:
            data = chan.recv(1024)
            if not data:
                break
            chan.send(b"echo:" + data)
            if b"exit\n" in data:   # keystrokes may arrive together in one read
                break
        chan.close()
        t.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener, iface, state


class FakeWs:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def receive(self, timeout=None):
        if self.incoming:
            item = self.incoming.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        time.sleep(min(timeout or 0, 0.05))
        return None


class Closed(Exception):
    pass


class SshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = terminal_ca.CA_PATH
        terminal_ca.CA_PATH = os.path.join(self.tmp.name, "terminal_ca")
        self.key = server_key()

    def tearDown(self):
        terminal_ca.CA_PATH = self.old
        self.tmp.cleanup()

    def host(self, port, key=None):
        key = key or self.key
        return {"id": "t", "label": "t", "address": "127.0.0.1", "port": port, "user": "mizuki",
                "host_key_type": key.get_name(), "host_key": key.get_base64()}

    def test_pinned_key_connects_with_the_certificate_from_the_wireguard_address(self):
        listener, iface, state = start_server(self.key)
        cert_key, _ = terminal_ca.issue("mizuki", "x", "127.0.0.1")
        transport, chan = terminal.connect(self.host(listener.getsockname()[1]), cert_key, "127.0.0.1", 100, 30)
        try:
            self.assertEqual(iface.users, [("mizuki", "ssh-ed25519-cert-v01@openssh.com")])
            self.assertEqual(iface.pty, ("xterm-256color", 100, 30))
            self.assertEqual(state["peer"][0], "127.0.0.1")   # bound to the configured source address
            ws = FakeWs([json.dumps({"t": "resize", "cols": 120, "rows": 40}),
                         json.dumps({"t": "in", "d": "ls\n"}), json.dumps({"t": "in", "d": "exit\n"})])
            reason, bytes_in, bytes_out = terminal.bridge(ws, chan, Closed, abort=lambda: None)
        finally:
            chan.close()
            transport.close()
        self.assertEqual(reason, "exit")
        self.assertEqual(bytes_in, len("ls\n") + len("exit\n"))
        output = b"".join(ws.sent)
        self.assertIn(b"hello", output)
        self.assertIn(b"echo:ls", output)
        self.assertEqual(bytes_out, len(output))
        self.assertEqual(iface.pty[1:], (120, 40))

    def test_a_different_host_key_is_refused_before_signing_in(self):
        listener, iface, _ = start_server(self.key)
        cert_key, _ = terminal_ca.issue("mizuki", "x", "127.0.0.1")
        with self.assertRaises(terminal.HostKeyMismatch):
            terminal.connect(self.host(listener.getsockname()[1], key=server_key()), cert_key, "127.0.0.1", 80, 24)
        self.assertEqual(iface.users, [])

    def test_agent_deploy_sends_no_password_to_a_host_whose_pinned_key_differs(self):
        import agent_deploy
        listener, iface, _ = start_server(self.key)
        other = server_key()
        with self.assertRaises(agent_deploy.AgentDeployError) as ctx:
            agent_deploy.deploy_agent("127.0.0.1", listener.getsockname()[1], "mizuki", "secret", "http://x", "s",
                                      pinned_host_key=(other.get_name(), other.get_base64()))
        self.assertIn("主机指纹", str(ctx.exception))
        self.assertEqual(iface.users, [])

    def test_a_browser_that_stops_reading_is_cut_loose_and_no_thread_stays(self):
        class Chan(ScriptedChan):
            def recv(self, n):
                return b"x" * 1024   # the target never stops talking

        released = threading.Event()

        class StuckWs(FakeWs):
            def send(self, data):
                if not released.wait(60):   # the browser never reads, until the connection is cut
                    return
                raise Closed()

        before = threading.active_count()
        started = time.monotonic()
        reason, _, _ = terminal.bridge(StuckWs([]), Chan(), Closed, abort=released.set, stall=1)
        self.assertEqual(reason, "slow_browser")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(threading.active_count(), before)   # pump and deliver both exited

    def test_a_large_burst_slows_down_instead_of_being_cut(self):
        # 3 MB at once, more than the backlog holds, sent by a browser that is slow but keeps reading.
        chunks = [b"y" * 32768] * 96
        ws = SlowWs([], delay=0.005)
        reason, _, bytes_out = terminal.bridge(ws, ScriptedChan(chunks), Closed, abort=lambda: None, stall=2)
        self.assertEqual(reason, "exit")
        self.assertEqual(bytes_out, 96 * 32768)
        self.assertEqual(b"".join(ws.sent), b"".join(chunks))

    def test_output_is_fully_sent_and_counted_before_bridge_returns(self):
        # 绘名's case: four chunks, 0.6 s each to send, then EOF.
        ws = SlowWs([], delay=0.6)
        reason, _, bytes_out = terminal.bridge(ws, ScriptedChan([b"z" * 100] * 4), Closed, abort=lambda: None)
        sent_at_return = len(ws.sent)
        time.sleep(0.8)
        self.assertEqual((reason, bytes_out, sent_at_return, len(ws.sent)), ("exit", 400, 4, 4))

    def test_bridge_stops_when_idle_or_the_browser_leaves(self):
        clock = iter(range(0, 10_000, 600)).__next__
        reason, _, _ = terminal.bridge(FakeWs([]), QuietChan(), Closed, abort=lambda: None, clock=clock,
                                       idle=900, limit=7200)
        self.assertEqual(reason, "idle")
        reason, _, _ = terminal.bridge(FakeWs([Closed()]), QuietChan(), Closed, abort=lambda: None)
        self.assertEqual(reason, "browser_closed")


class QuietChan:
    def settimeout(self, t):
        pass

    def recv(self, n):
        time.sleep(0.05)
        raise socket.timeout()

    def sendall(self, data):
        pass

    def resize_pty(self, *a):
        pass


class ScriptedChan(QuietChan):
    """Hands out the given chunks, then EOF."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""


class SlowWs(FakeWs):
    def __init__(self, incoming, delay):
        super().__init__(incoming)
        self.delay = delay

    def send(self, data):
        time.sleep(self.delay)
        super().send(data)


# ---------- audit ----------

class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class AuditTests(unittest.TestCase):
    CFG = {"notifications": {"feishu": {"enabled": True, "webhook": "https://feishu.example/hook"}},
           "terminal": {"enabled": True, "source_address": "10.10.0.1", "slack_webhook": "https://slack.example/hook"}}
    EVENT = {"kind": "open", "at": 1_000_000, "session": "abcd", "sub": "s", "email": "a@example.com", "name": "Airi",
             "host": "tokyo", "label": "东京", "address": "10.10.0.3", "client_ip": "203.0.113.5"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        metrics_store.DB_PATH = os.path.join(self.tmp.name, "metrics.db")
        metrics_store.init_db()
        self.calls = []
        self.feishu_ok = True
        self.slack_ok = True

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if "feishu" in url:
            return Resp(200, {"code": 0 if self.feishu_ok else 9499})
        return Resp(200 if self.slack_ok else 500, {})

    def open(self, auditor, event=None, session="abcd"):
        event = {**(event or self.EVENT), "session": session}
        auditor.open(self.CFG, session, "s", "a@example.com", "tokyo", "203.0.113.5", "1", event)

    def close(self, auditor, session="abcd", at=1_000_600):
        return auditor.close(self.CFG, {**self.EVENT, "kind": "close", "session": session, "at": at,
                                        "duration": 600, "bytes_in": 3, "bytes_out": 5, "reason": "exit"})

    def test_both_channels_get_the_event_as_plain_text(self):
        self.open(terminal.Auditor(post=self.post))
        (feishu_url, card), (slack_url, slack) = self.calls
        self.assertEqual((feishu_url, slack_url), ("https://feishu.example/hook", "https://slack.example/hook"))
        self.assertEqual(card["card"]["body"]["elements"][0]["text"]["tag"], "plain_text")
        self.assertIs(slack["mrkdwn"], False)
        self.assertIn("a@example.com", slack["text"])
        self.assertEqual(metrics_store.outbox_pending(terminal.OUTBOX_MAX_ATTEMPTS), [])

    def test_a_failed_channel_is_retried_alone(self):
        self.slack_ok = False
        auditor = terminal.Auditor(post=self.post)
        self.open(auditor)
        self.assertEqual(len(metrics_store.outbox_pending(terminal.OUTBOX_MAX_ATTEMPTS)), 1)
        self.calls.clear()
        self.slack_ok = True
        auditor.deliver_pending(self.CFG)
        self.assertEqual([u for u, _ in self.calls], ["https://slack.example/hook"])   # feishu already done
        self.assertEqual(metrics_store.outbox_pending(terminal.OUTBOX_MAX_ATTEMPTS), [])

    def test_a_row_another_process_is_pushing_is_left_alone(self):
        self.slack_ok = False
        auditor = terminal.Auditor(post=self.post)
        self.open(auditor)
        row = metrics_store.outbox_pending(terminal.OUTBOX_MAX_ATTEMPTS)[0]
        self.assertTrue(metrics_store.outbox_claim(row["id"], time.time(), 60))   # the other process
        self.calls.clear()
        auditor.deliver_pending(self.CFG)
        self.assertEqual(self.calls, [])

    def test_open_and_close_are_recorded_once_and_with_their_session(self):
        auditor = terminal.Auditor(post=self.post)
        self.open(auditor)
        with self.assertRaises(Exception):          # the same session again: refused, not a second audit
            self.open(auditor)
        self.assertTrue(self.close(auditor))
        self.assertFalse(self.close(auditor))        # already closed: nothing recorded or pushed
        self.assertEqual(len(self.calls), 4)         # open + close, two channels each
        self.assertEqual(metrics_store.terminal_history()[0]["end_reason"], "exit")

    def test_names_cannot_inject_markup_or_hide_text(self):
        event = {**self.EVENT, "name": "<at id=all></at>\u202eevil\u200b", "label": "a\nb\u2028c",
                 "client_ip": "https://evil.example"}
        title, lines = terminal.describe(event)
        text = "\n".join(lines)
        for bad in ("\u202e", "\u200b", "\u2028", "a\nb", "https://"):
            self.assertNotIn(bad, text)
        slack = terminal._slack_text(title, lines)
        self.assertNotIn("<at", slack)
        self.assertIn("&lt;at id=all&gt;", slack)

    def test_done_records_are_pruned_later(self):
        self.open(terminal.Auditor(post=self.post))
        metrics_store.outbox_prune(self.EVENT["at"] + 1)
        with metrics_store._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_outbox").fetchone()[0], 0)

    def test_sessions_left_by_another_process_are_closed_as_unknown_but_not_our_own(self):
        auditor = terminal.Auditor(post=self.post)
        metrics_store.terminal_record_open("theirs", "old-worker", "s", "a@example.com", "tokyo", "203.0.113.5",
                                           "1", 1_000_000, {**self.EVENT, "session": "theirs"})
        self.open(auditor, session="ours")
        terminal.close_unfinished(auditor, self.CFG, now=1_000_600)
        open_ids = [s["id"] for s in metrics_store.terminal_unfinished()]
        self.assertEqual(open_ids, ["ours"])
        self.assertEqual(next(s for s in metrics_store.terminal_history() if s["id"] == "theirs")["end_reason"],
                         "unknown")
        self.assertIn("结束时间不确定", self.calls[-1][1]["text"])

    def test_an_old_outbox_table_is_migrated(self):
        with metrics_store._connect() as conn:
            conn.executescript("""
                DROP TABLE audit_outbox; DROP TABLE terminal_sessions;
                CREATE TABLE terminal_sessions (id TEXT PRIMARY KEY, sub TEXT NOT NULL, email TEXT, host TEXT NOT NULL,
                  client_ip TEXT, cert_serial TEXT, started_at INTEGER NOT NULL, ended_at INTEGER,
                  bytes_in INTEGER NOT NULL DEFAULT 0, bytes_out INTEGER NOT NULL DEFAULT 0, end_reason TEXT);
                CREATE TABLE audit_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
                  event_json TEXT NOT NULL, feishu_done INTEGER NOT NULL DEFAULT 0,
                  slack_done INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
                INSERT INTO audit_outbox (created_at, event_json) VALUES (1, '{}'), (2, '{}');
            """)
        metrics_store.init_db()
        self.open(terminal.Auditor(post=self.post))
        with metrics_store._connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT event_id FROM audit_outbox ORDER BY id")]
        self.assertEqual(ids, ["legacy:1", "legacy:2", "abcd:open"])


# ---------- the step-up round trip through SSO ----------

TERMINAL_CFG = {"enabled": True, "source_address": "10.10.0.1", "hosts": {
    "tokyo": {"label": "东京", "address": "10.10.0.3", "user": "mizuki",
              "host_key": PINNED}}}


class StepUpTests(SsoTestCase):
    def config(self, **sso_overrides):
        return {**super().config(**sso_overrides), "terminal": TERMINAL_CFG}

    def setUp(self):
        super().setUp()
        self.sign_in()   # the ordinary sign-in: password, no ssh_hosts needed
        self.idp.extra = {"roles": ["panopticon:admin"], "ssh_hosts": ["tokyo"], "amr": ["passkey"],
                          "auth_time": int(time.time())}

    def step_up(self, host="tokyo", origin=SITE):
        headers = {"Origin": origin} if origin else {}
        resp = self.client.post("/auth/terminal", base_url=SITE, data={"host": host}, headers=headers)
        if resp.status_code != 303 or not resp.headers["Location"].startswith("https://account.example/"):
            return resp, None
        q = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}
        self.idp.nonce = q["nonce"]
        return resp, q

    def finish(self, q):
        return self.get(f"/auth/callback?code=abc&state={q['state']}")

    def hosts(self):
        return self.get("/api/terminal/hosts").get_json()

    def test_step_up_asks_for_a_fresh_sign_in_and_issues_a_ticket(self):
        _, q = self.step_up()
        self.assertEqual((q["prompt"], q["max_age"]), ("login", "300"))
        resp = self.finish(q)
        self.assertEqual(resp.headers["Location"], "/#p=3")
        info = self.hosts()
        self.assertEqual(info["pending"], "tokyo")
        self.assertIsNone(info["error"])
        # The dashboard session itself is unchanged apart from the fresher tier.
        self.assertEqual(self.get("/api/me").get_json(), {"role": "admin", "via": "sso", "name": "Airi"})

    def refused(self, code):
        info = self.hosts()
        self.assertEqual(info["error"], code)
        self.assertIsNone(info["pending"])

    def test_password_is_not_enough(self):
        self.idp.extra["amr"] = ["password"]
        _, q = self.step_up()
        self.finish(q)
        self.refused("not_passkey")

    def test_a_passkey_earlier_in_the_sso_session_does_not_count(self):
        # Kratos lists every method used in its session, in order; only the last one is this step-up.
        self.idp.extra["amr"] = ["password", "passkey", "password"]
        _, q = self.step_up()
        self.finish(q)
        self.refused("not_passkey")

    def test_a_sign_in_time_in_the_future_does_not_count(self):
        self.idp.extra["auth_time"] = int(time.time()) + 600
        _, q = self.step_up()
        self.finish(q)
        self.refused("stale")

    def test_opening_from_another_site_is_refused(self):
        resp, q = self.step_up(origin="https://evil.example")
        self.assertIsNone(q)
        self.refused("origin")

    def test_without_origin_the_referer_must_be_the_dashboard(self):
        resp = self.client.post("/auth/terminal", base_url=SITE, data={"host": "tokyo"},
                                headers={"Referer": SITE + "/#p=3"})
        self.assertTrue(resp.headers["Location"].startswith("https://account.example/"))
        resp = self.client.post("/auth/terminal", base_url=SITE, data={"host": "tokyo"})
        self.assertEqual(resp.headers["Location"], "/#p=3")
        self.refused("origin")

    def test_an_earlier_sign_in_does_not_count(self):
        self.idp.extra["auth_time"] = int(time.time()) - 600
        _, q = self.step_up()
        self.finish(q)
        self.refused("stale")

    def test_host_must_be_granted(self):
        self.idp.extra["ssh_hosts"] = ["shanghai"]
        _, q = self.step_up()
        self.finish(q)
        self.refused("host_denied")

    def test_someone_else_signing_in_is_refused(self):
        _, q = self.step_up()
        self.idp.override = {"sub": "someone-else"}
        self.finish(q)
        self.refused("mismatch")

    def test_unknown_host_never_reaches_sso(self):
        resp, q = self.step_up("nowhere")
        self.assertIsNone(q)
        self.assertEqual(resp.headers["Location"], "/#p=3")
        self.refused("unknown_host")

    def test_local_emergency_sign_in_cannot_open_terminals(self):
        self.client.post("/logout", base_url=SITE)
        self.client.post("/login", base_url=SITE, json={"username": "mizuki", "password": "pw"})
        resp, q = self.step_up()
        self.assertIsNone(q)
        self.refused("local_session")

    def test_a_cancelled_step_up_reports_and_keeps_the_session(self):
        _, q = self.step_up()
        self.get(f"/auth/callback?error=access_denied&state={q['state']}")
        self.refused("cancelled")
        self.assertEqual(self.get("/api/status").status_code, 200)

    def test_history_is_own_for_admins(self):
        for sid, sub in (("a1", "user-1"), ("b1", "other")):
            metrics_store.terminal_record_open(sid, "w", sub, f"{sub}@example.com", "tokyo", "1.2.3.4", "1",
                                               time.time(), {"session": sid, "kind": "open"})
        _, q = self.step_up()
        self.finish(q)   # session role is now admin
        ids = [s["id"] for s in self.get("/api/terminal/history").get_json()["sessions"]]
        self.assertEqual(ids, ["a1"])


if __name__ == "__main__":
    unittest.main()
