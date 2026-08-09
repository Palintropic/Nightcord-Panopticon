"""
一键装 agent 时"记住这台服务器密码"选项的存储层。

密码不会明文落进 config.yaml——用本地生成的密钥（agent_hosts.key，不进仓库）加密后
才写进 config.yaml 的 agent_hosts 段。丢了密钥文件等于丢了所有记住的密码（不影响已经
装好、正在跑的 agent 本身，只是没法再用"重新安装"按钮，得重新手填一次密码）。
"""
import os
import time

from cryptography.fernet import Fernet, InvalidToken

from config_store import load_yaml, save_yaml, update_yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(BASE_DIR, "agent_hosts.key")


def _get_fernet():
    if not os.path.exists(KEY_PATH):
        try:
            fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass  # 并发第一次调用撞上了，读已经写好的那份就行
    with open(KEY_PATH, "rb") as f:
        return Fernet(f.read().strip())


def _host_id(ip, port):
    return f"{ip}:{port}"


def _load(config_path):
    return load_yaml(config_path)


def _dump(config_path, cfg):
    save_yaml(config_path, cfg)


def list_hosts(cfg):
    """给设置页展示用，脱敏（不含密码本身）。"""
    hosts = cfg.get("agent_hosts", []) or []
    return [
        {
            "id": _host_id(h["ip"], h["port"]),
            "ip": h["ip"],
            "port": h["port"],
            "ssh_user": h.get("ssh_user", "root"),
            "panel_name": h.get("panel_name", ""),
            "last_deployed_ts": h.get("last_deployed_ts"),
        }
        for h in hosts
    ]


def get_host_credentials(cfg, host_id):
    """给"重新安装"按钮用，解密出可以直接拿去连接的密码。找不到或解密失败返回 None。"""
    for h in cfg.get("agent_hosts", []) or []:
        if _host_id(h["ip"], h["port"]) == host_id:
            try:
                password = _get_fernet().decrypt(h["password_enc"].encode()).decode()
            except (InvalidToken, KeyError):
                return None
            return {"ip": h["ip"], "port": h["port"], "ssh_user": h.get("ssh_user", "root"), "password": password}
    return None


def save_host(config_path, ip, port, ssh_user, password, panel_name):
    encrypted_password = _get_fernet().encrypt(password.encode()).decode()

    def update(cfg):
        hosts = cfg.setdefault("agent_hosts", [])
        hid = _host_id(ip, port)
        hosts[:] = [h for h in hosts if _host_id(h["ip"], h["port"]) != hid]
        hosts.append({
            "ip": ip,
            "port": port,
            "ssh_user": ssh_user,
            "password_enc": encrypted_password,
            "panel_name": panel_name,
            "last_deployed_ts": int(time.time()),
        })

    update_yaml(config_path, update)


def delete_host(config_path, host_id):
    def update(cfg):
        hosts = cfg.get("agent_hosts", []) or []
        hosts[:] = [h for h in hosts if _host_id(h["ip"], h["port"]) != host_id]
        cfg["agent_hosts"] = hosts

    update_yaml(config_path, update)
