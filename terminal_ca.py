"""
网页终端登目标机用的 SSH 短期证书。

Panopticon 持一把 CA 私钥（terminal_ca，跟 config.yaml 同目录，0600，不进仓库，也不跟
agent_hosts.key 共用）。每开一次终端现生成一把一次性钥匙，用 CA 签一张证书：
- 有效期 CERT_TTL（只管握手，连上之后不受影响）；
- principal 只有目标账户，source-address 只认 Panopticon 的 WireGuard 地址；
- 扩展只给 permit-pty：端口转发、agent 转发、X11、~/.ssh/rc 都不给，限制写在证书里，
  不用改目标机的全局 sshd 设置；
- key id 写「谁 / 哪台 / 会话号」，目标机自己的 auth.log 里也看得到是谁。
目标机只需信任这把 CA 的公钥（TrustedUserCAKeys + AuthorizedPrincipalsFile），机器上不留任何
长期钥匙；换一把 CA 就全部作废。

    python terminal_ca.py pubkey    # 打印 CA 公钥，配置目标机时用
"""
import io
import os
import secrets
import sys
import threading
import time

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(BASE_DIR, "terminal_ca")
CERT_TTL = 300          # 秒
CLOCK_SKEW = 60         # 秒，证书生效时间往前放一点，目标机时钟慢一点也能用
CA_COMMENT = "panopticon-terminal-ca"

_lock = threading.Lock()


def _load_or_create():
    with _lock:
        if not os.path.exists(CA_PATH):
            key = ed25519.Ed25519PrivateKey.generate()
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                    serialization.NoEncryption())
            try:
                fd = os.open(CA_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(pem)
            except FileExistsError:
                pass  # 另一个线程先写好了，读它那份
        with open(CA_PATH, "rb") as f:
            return serialization.load_ssh_private_key(f.read(), password=None)


def public_line():
    """CA 公钥，一行 OpenSSH 格式，放进目标机的 /etc/ssh/panopticon_ca.pub。"""
    raw = _load_or_create().public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                      serialization.PublicFormat.OpenSSH)
    return f"{raw.decode()} {CA_COMMENT}"


def issue(principal, key_id, source_address, now=None):
    """
    签一张证书，返回 (paramiko 钥匙（已挂上证书）, 证书序列号)。
    钥匙只在内存里，用完即弃。
    """
    now = int(time.time() if now is None else now)
    user_key = ed25519.Ed25519PrivateKey.generate()
    serial = secrets.randbits(63)
    cert = (
        serialization.SSHCertificateBuilder()
        .public_key(user_key.public_key())
        .serial(serial)
        .type(serialization.SSHCertificateType.USER)
        .key_id(key_id.encode())
        .valid_principals([principal.encode()])
        .valid_after(now - CLOCK_SKEW)
        .valid_before(now + CERT_TTL)
        .add_critical_option(b"source-address", source_address.encode())
        .add_extension(b"permit-pty", b"")
        .sign(_load_or_create())
    )
    pem = user_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                 serialization.NoEncryption()).decode()
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(pem))
    key.load_certificate(cert.public_bytes().decode())
    return key, serial


if __name__ == "__main__":
    if sys.argv[1:] == ["pubkey"]:
        print(public_line())
    else:
        sys.exit(__doc__)
