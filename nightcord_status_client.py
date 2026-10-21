"""
Nightcord-Status（自建监控服务，xml_status_service.py，端口 8702）的只读客户端。
一次探测覆盖若干个异构监控目标（ESXi 主机 / TCP 探测 / 第三方 statuspage 等）。
接口只监听 WireGuard 内网、目前无鉴权，纯 GET。
"""
import requests

DEFAULT_TIMEOUT = 8


class NightcordStatusError(Exception):
    pass


class NightcordStatusClient:
    def __init__(self, name: str, base_url: str, verify_ssl: bool = False):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl

    def _call(self):
        url = f"{self.base_url}/api/status.json"
        try:
            resp = requests.get(url, timeout=DEFAULT_TIMEOUT, verify=self.verify_ssl)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise NightcordStatusError(f"[{self.name}] 请求 {url} 失败: {e}")

    def collect_all(self):
        """
        汇总所有监控目标。status 字段（ok/degraded/down/unknown）由服务端直接给出，
        跟前端 labelMap/badgeMap 用的 key 一致，这里不做二次映射。
        """
        data = self._call()
        return {
            "name": self.name,
            "url": self.base_url,
            "online": True,
            "error": None,
            "kind": "nightcord-status",
            "targets": data.get("targets", []),
        }
