"""
Dashboard 自身的登录保护：按 IP 的暴力破解锁定，以及经反代访问时怎么认出真实来源。

设计取舍：Panopticon 可能经反代暴露公网，它本身又握着 agent 的共享密钥和记住的 SSH 密码，
所以哪怕是个人自用也不能裸奔——至少要有登录门槛，并且防止别人对着登录框无限重试密码。
"""
import threading
import time


class BruteForceGuard:
    def __init__(self, max_attempts: int = 5, lockout_seconds: int = 900):
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._fails = {}      # ip -> 失败次数
        self._lockouts = {}   # ip -> 解锁时间戳
        self._lock = threading.Lock()

    def is_locked(self, ip: str):
        with self._lock:
            unlock_ts = self._lockouts.get(ip)
            if unlock_ts is None:
                return False, 0
            remaining = unlock_ts - time.time()
            if remaining <= 0:
                # 锁定期已过，清空记录，重新开始计数
                self._lockouts.pop(ip, None)
                self._fails.pop(ip, None)
                return False, 0
            return True, int(remaining)

    def record_failure(self, ip: str):
        with self._lock:
            count = self._fails.get(ip, 0) + 1
            self._fails[ip] = count
            if count >= self.max_attempts:
                self._lockouts[ip] = time.time() + self.lockout_seconds

    def record_success(self, ip: str):
        with self._lock:
            self._fails.pop(ip, None)
            self._lockouts.pop(ip, None)


LOOPBACK_PROXIES = frozenset({"127.0.0.1", "::1"})


def trusted_proxies_from(cfg) -> frozenset:
    """
    反代地址：只有从这些地址来的请求，才信它带的 X-Forwarded-For / X-Forwarded-Proto。
    本机回环总是信；经别的机器转发（比如东京的 Caddy 走 WireGuard 转过来）时，把那台机器的
    地址写进 dashboard_auth.trusted_proxies。直连 1810 的请求自己带这些头不会被当真。
    """
    extra = ((cfg or {}).get("dashboard_auth") or {}).get("trusted_proxies") or []
    if not isinstance(extra, (list, tuple)):
        extra = [extra]
    return LOOPBACK_PROXIES | {str(x).strip() for x in extra if x is not None and str(x).strip()}


def _rightmost(header_value):
    values = [v.strip() for v in (header_value or "").split(",") if v.strip()]
    return values[-1] if values else None


class ForwardedProto:
    """
    WSGI 中间件：请求来自受信反代时，按 X-Forwarded-Proto 设置 wsgi.url_scheme。
    经 HTTPS 反代访问时，WebAuthn 校验的 origin（见 webauthn_manager.origin_from_request）
    才是浏览器看到的 https://，不然 Passkey 注册和登录都会对不上。
    trusted_proxies() 每次请求现取，配置改了不用重启。
    """

    def __init__(self, wsgi_app, trusted_proxies):
        self.wsgi_app = wsgi_app
        self.trusted_proxies = trusted_proxies

    def __call__(self, environ, start_response):
        proto = (_rightmost(environ.get("HTTP_X_FORWARDED_PROTO")) or "").lower()
        if proto in ("http", "https") and environ.get("REMOTE_ADDR") in self.trusted_proxies():
            environ["wsgi.url_scheme"] = proto
        return self.wsgi_app(environ, start_response)


def get_client_ip(request, trusted_proxies=LOOPBACK_PROXIES) -> str:
    """Return the client IP, accepting forwarding headers only from a trusted proxy.

    A public client can send arbitrary ``X-Forwarded-For`` headers.  When the
    app is behind a local reverse proxy, that proxy appends the actual peer IP
    to the right of the list, so use the rightmost entry rather than attacker
    controlled values on the left.
    """
    remote_addr = request.remote_addr or "unknown"
    if remote_addr in trusted_proxies:
        forwarded = _rightmost(request.headers.get("X-Forwarded-For"))
        if forwarded:
            return forwarded
    return remote_addr
