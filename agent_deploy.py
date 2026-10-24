"""
一键远程部署青源 agent（agent/metrics_agent.py）到面板服务器。

SSH 到目标机器：同步 agent 代码、建虚拟环境装依赖、写 agent_config.yaml、
注册 systemd 服务并启动。密码只在这一次连接里用，用完即从这个函数的调用栈里消失——
是否把密码加密落库交给调用方（见 agent_hosts.py），这个模块本身不做任何持久化。

主机指纹用的是"首次连接自动信任"（TOFU），跟大多数面板的一键装法一路子，
不是绝对安全（理论上第一次连接可能被中间人劫持），指纹会记进返回的 log 里，
多疑的话可以自己去目标机器上核对。
"""
import base64
import os
import socket
import time

import paramiko
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_SRC_DIR = os.path.join(BASE_DIR, "agent")
REMOTE_DIR_NAME = "nightcord-metrics-agent"
SERVICE_NAME = "nightcord-metrics-agent"


class AgentDeployError(Exception):
    """部署失败，message 可以直接原样展示给前端。"""


def deploy_agent(ip, port, ssh_user, password, report_url, shared_secret, log=None, panel_name_override=None,
                 pinned_host_key=None):
    """
    返回部署成功后使用的 panel_name（默认取自目标机器的 hostname，除非传了 panel_name_override）。
    失败抛 AgentDeployError。

    panel_name_override：重新部署时传上次用的 panel_name，让这台机器的名字不随 hostname 变。
    pinned_host_key：(类型, base64)，这台机器在网页终端清单里钉过主机指纹时传进来：对不上就在发送
    密码之前断开。没钉过的机器（第一次装）只能先信任第一次见到的指纹，并把它打印出来。
    """
    def emit(line):
        if log:
            log(line)

    ssh = paramiko.SSHClient()
    if pinned_host_key:
        key_type, key_b64 = pinned_host_key
        ssh.get_host_keys().add(ip if port == 22 else f"[{ip}]:{port}", key_type,
                                paramiko.PKey.from_type_string(key_type, base64.b64decode(key_b64)))
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    emit(f"连接 {ssh_user}@{ip}:{port} ...")
    try:
        ssh.connect(
            ip, port=port, username=ssh_user, password=password,
            timeout=15, banner_timeout=15, auth_timeout=15,
            look_for_keys=False, allow_agent=False,
        )
    except paramiko.AuthenticationException:
        raise AgentDeployError("SSH 账号或密码不对")
    except paramiko.BadHostKeyException:
        raise AgentDeployError("主机指纹跟网页终端清单里钉的对不上，没有发送密码。先查清楚是不是机器重装过")
    except (socket.timeout, socket.error, paramiko.SSHException) as e:
        raise AgentDeployError(f"连接失败：{e}")

    try:
        host_key = ssh.get_transport().get_remote_server_key()
        emit(f"已连接。主机指纹（{host_key.get_name()}）：{host_key.get_base64()[:44]}...")

        is_root = ssh_user == "root"

        home, hostname = _run(ssh, "echo $HOME && hostname", emit).strip().splitlines()[-2:]
        home, hostname = home.strip(), hostname.strip() or ip
        panel_name = panel_name_override or hostname
        remote_dir = f"{home}/{REMOTE_DIR_NAME}"
        if panel_name_override:
            emit(f"目标机器 hostname：{hostname}，但按指定绑定到面板「{panel_name}」，安装目录：{remote_dir}")
        else:
            emit(f"目标机器 hostname：{panel_name}，安装目录：{remote_dir}")

        _run(ssh, f"mkdir -p {remote_dir}", emit)

        emit("上传 agent 代码 ...")
        sftp = ssh.open_sftp()
        try:
            for fname in ("metrics_agent.py", "requirements.txt"):
                sftp.put(os.path.join(AGENT_SRC_DIR, fname), f"{remote_dir}/{fname}")

            agent_config = yaml.safe_dump(
                {
                    "panel_name": panel_name,
                    "report_url": report_url,
                    "shared_secret": shared_secret,
                    "interval_seconds": 60,
                },
                allow_unicode=True,
                sort_keys=False,
            )
            with sftp.file(f"{remote_dir}/agent_config.yaml", "w") as f:
                f.write(agent_config)
            sftp.chmod(f"{remote_dir}/agent_config.yaml", 0o600)
        finally:
            sftp.close()

        emit("创建虚拟环境并安装依赖（可能要一会儿）...")
        _ensure_venv(ssh, remote_dir, password, is_root, emit)
        _run(
            ssh,
            f"{remote_dir}/.venv/bin/pip install --quiet --disable-pip-version-check "
            f"-r {remote_dir}/requirements.txt",
            emit, timeout=300,
        )

        emit("注册 systemd 服务 ...")
        unit = _build_unit_file(remote_dir)
        _write_privileged_file(
            ssh, "/etc/systemd/system/nightcord-metrics-agent.service", unit, password, is_root, emit,
        )
        _run_privileged(ssh, "systemctl daemon-reload", password, is_root, emit)
        _run_privileged(ssh, f"systemctl enable {SERVICE_NAME}", password, is_root, emit)
        # 用 restart 而不是 "enable --now"：后者对已经在跑的服务是空操作，不会重启，
        # 重新部署时新写的 agent_config.yaml（比如改过的 report_url）永远不会被已存活的旧进程读到。
        # restart 对"服务本来就没起过"和"服务已经在跑"两种情况都能正确生效，行为一致。
        _run_privileged(ssh, f"systemctl restart {SERVICE_NAME}", password, is_root, emit)

        time.sleep(1.5)  # 给 systemd 一点时间把服务从 activating 拉到 active
        status = _run(ssh, f"systemctl is-active {SERVICE_NAME}", emit, check=False).strip()
        if status != "active":
            raise AgentDeployError(
                f"服务已安装但状态是「{status}」而不是 active，"
                f"去目标机器上 journalctl -u {SERVICE_NAME} -n 50 看看具体报错"
            )
        emit("青源 agent 已启动 ✓")
        return panel_name
    finally:
        ssh.close()


def _build_unit_file(remote_dir):
    return (
        "[Unit]\n"
        "Description=Nightcord Panopticon self-hosted metrics agent\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={remote_dir}\n"
        f"ExecStart={remote_dir}/.venv/bin/python {remote_dir}/metrics_agent.py\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _run(ssh, cmd, emit, timeout=30, check=True):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    exit_code = stdout.channel.recv_exit_status()
    if check and exit_code != 0:
        # 很多工具（比如 venv 模块报"缺 ensurepip"）把诊断信息写到 stdout 而不是 stderr，
        # 只看 stderr 经常啥都捞不着，日志停在"命令失败"就没下文了——两边都拿，stderr 优先。
        detail = (err.strip() or out.strip())[:400]
        emit(f"  ! 命令失败（exit {exit_code}）：{cmd[:80]}")
        if detail:
            emit(f"    {detail}")
        raise AgentDeployError(f"远端命令执行失败：{cmd[:60]}（exit {exit_code}）\n{detail[:300]}")
    return out


def _ensure_venv(ssh, remote_dir, password, is_root, emit):
    """
    建虚拟环境最常见的坑：Debian/Ubuntu 系统 python3 自带但 venv 模块依赖的 ensurepip
    在独立的 python3-venv 包里，没装的话 `python3 -m venv` 直接 exit 1。
    能自动装就自动装、装完重试一次，装不了（非 apt 系统 / 没权限）再把原始报错抛出去，
    好过卡在一句"命令失败"让人自己上服务器排查。
    """
    venv_cmd = f"python3 -m venv {remote_dir}/.venv"
    try:
        _run(ssh, venv_cmd, emit, timeout=120)
        return
    except AgentDeployError as e:
        msg = str(e)
        if not any(s in msg for s in ("ensurepip", "python3-venv", "No module named venv")):
            raise

    emit("检测到系统缺少 python3-venv（Debian/Ubuntu 常见），尝试自动安装 ...")
    try:
        # 非 root 账号走 sudo -S 时，sudo 只包住紧跟在后面的那一个命令，
        # 用 bash -c 把 update && install 揉成一条命令，两步才能一起被 sudo 到。
        _run_privileged(
            ssh, "bash -c 'apt-get update -qq && apt-get install -y python3-venv'",
            password, is_root, emit, timeout=180,
        )
    except AgentDeployError as e:
        raise AgentDeployError(
            "缺少 python3-venv 且自动安装失败（可能不是 apt 系发行版，或者这个账号没有 sudo 权限）。"
            f"去目标机器上手动执行 `sudo apt install python3-venv` 后重试一键安装。\n{e}"
        )
    emit("python3-venv 安装完成，重新创建虚拟环境 ...")
    _run(ssh, venv_cmd, emit, timeout=120)


def _run_privileged(ssh, cmd, password, is_root, emit, timeout=30):
    """root 账号直接跑；非 root 账号走 sudo -S，用同一个 SSH 密码当 sudo 密码。"""
    if is_root:
        _run(ssh, cmd, emit, timeout=timeout)
        return
    full_cmd = f"sudo -S -p '' {cmd}"
    stdin, stdout, stderr = ssh.exec_command(full_cmd, timeout=timeout)
    stdin.write(password + "\n")
    stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        emit(f"  ! sudo 命令失败（exit {exit_code}）：{cmd[:80]}")
        raise AgentDeployError(f"sudo 执行失败（可能是这个账号的 sudo 密码跟 SSH 密码不一致）：{cmd[:60]}\n{err.strip()[:300]}")
    return out


def _write_privileged_file(ssh, remote_path, content, password, is_root, emit):
    if is_root:
        sftp = ssh.open_sftp()
        try:
            with sftp.file(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()
        return
    full_cmd = f"sudo -S -p '' tee {remote_path} > /dev/null"
    stdin, stdout, stderr = ssh.exec_command(full_cmd)
    stdin.write(password + "\n")
    stdin.write(content)
    stdin.channel.shutdown_write()
    err = stderr.read().decode("utf-8", "replace")
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        emit(f"  ! 写入 {remote_path} 失败（exit {exit_code}）")
        raise AgentDeployError(f"写入 {remote_path} 失败：{err.strip()[:300]}")
