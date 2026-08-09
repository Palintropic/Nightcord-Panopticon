"""
Dashboard 自身的登录保护：HTTP Basic Auth + 按 IP 的暴力破解锁定。

设计取舍：Panopticon 现在要长期暴露公网，它本身又握着能连进 WireGuard 内网、
调用所有面板 API 的凭证，所以哪怕是个人自用也不能裸奔——至少要有登录门槛，
并且防止别人对着登录框无限重试密码。
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


def get_client_ip(request, trusted_proxies=frozenset({"127.0.0.1", "::1"})) -> str:
    """Return the client IP, accepting forwarding headers only from a trusted proxy.

    A public client can send arbitrary ``X-Forwarded-For`` headers.  When the
    app is behind a local reverse proxy, that proxy appends the actual peer IP
    to the right of the list, so use the rightmost entry rather than attacker
    controlled values on the left.
    """
    remote_addr = request.remote_addr or "unknown"
    if remote_addr in trusted_proxies:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            forwarded = [value.strip() for value in xff.split(",") if value.strip()]
            if forwarded:
                return forwarded[-1]
    return remote_addr
