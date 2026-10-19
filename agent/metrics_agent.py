"""
青源（Qingyuan）7.17-beta（内部开发代号 yukisakura）—— 自建系统指标监控 agent，
跑在各面板服务器上（不是 Panopticon 主机），定时采集本机 CPU/内存/磁盘/网络，以及 swap/负载/进程数/TCP 连接数/开机时间，
push 给 Panopticon 的 /api/metrics/report。整体架构说明见仓库根目录 QINGYUAN.zh-CN.md。

用法：
    pip install -r requirements.txt
    cp agent_config.example.yaml agent_config.yaml   # 填面板名/上报地址/共享密钥
    python metrics_agent.py
生产环境建议配 nightcord-metrics-agent.service（见同目录），systemd 常驻。
"""
import os
import socket
import time

import psutil
import requests
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "agent_config.yaml")
EXTERNAL_IP_REFRESH_SECONDS = 1800  # 公网 IP 很少变，不用每轮上报都查一次
EXTERNAL_IP_SERVICES = ("https://api.ipify.org", "https://ifconfig.me/ip")
# psutil 的 disk_partitions(all=False) 在容器化的机器上仍然可能混进这些虚拟/伪文件系统，
# 额外按 fstype 过滤一层，避免磁盘卡片里冒出一堆跟"真实容量"无关的挂载点
_PSEUDO_FSTYPES = {"tmpfs", "devtmpfs", "overlay", "squashfs", "aufs", "proc", "sysfs"}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError(
            f"未找到配置文件 {CONFIG_PATH}，请先复制 agent_config.example.yaml 为 agent_config.yaml 并填写"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_internal_ip():
    """本机在默认路由出口上的内网 IP。不需要真的发包，UDP connect 只是为了让内核选路。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def get_external_ip():
    """依次尝试几个公网 IP 查询服务，都失败就返回 None（不影响正常上报）。"""
    for url in EXTERNAL_IP_SERVICES:
        try:
            resp = requests.get(url, timeout=5)
            ip = resp.text.strip()
            if ip:
                return ip
        except Exception:
            continue
    return None


def collect_disk_detail():
    """
    枚举本机所有真实挂载点的用量，不再只看根分区——服务器常见的"数据盘挂在别处"
    （比如 /data、/mnt/backup）不该在磁盘卡片里彻底隐身。
    返回 [{"path","total","used","percent"}, ...]，按 percent 从高到低排，方便一眼看到最紧张的盘。
    """
    detail = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in _PSEUDO_FSTYPES:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue  # 挂载点存在但读不到用量（权限/离线网络盘之类），跳过不影响其它盘
        detail.append({
            "path": part.mountpoint,
            "total": usage.total,
            "used": usage.used,
            "percent": usage.percent,
        })
    detail.sort(key=lambda d: d["percent"], reverse=True)
    return detail


_PROC_TCP_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")
_TCP_ESTABLISHED_HEX = "01"  # /proc/net/tcp 第 4 列 st，01 = ESTABLISHED


def count_established_tcp():
    """
    只数已建立（ESTABLISHED）的 TCP 连接，监听中、TIME_WAIT 这些不算。
    Linux 上直接数 /proc/net/tcp{,6}：psutil.net_connections() 为了对上 pid 会扫遍所有进程的 fd，
    机器一忙每分钟一次就不便宜。别的系统退回 psutil；拿不到（权限等）返回 None，前端显示 "-"。
    """
    if os.path.exists(_PROC_TCP_TABLES[0]):
        total = 0
        for path in _PROC_TCP_TABLES:
            try:
                with open(path, "r", encoding="ascii") as f:
                    next(f, None)  # 表头
                    total += sum(1 for line in f if line.split()[3:4] == [_TCP_ESTABLISHED_HEX])
            except FileNotFoundError:
                continue  # 没开 IPv6 时没有 tcp6
            except OSError:
                return None
        return total
    try:
        return sum(1 for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_ESTABLISHED)
    except (psutil.AccessDenied, PermissionError, OSError):
        return None


def collect_system():
    """
    swap / 负载 / 进程数 / 连接数 / 开机时间。都是"拿不到就不报"：
    Panopticon 对缺失的字段存 NULL、显示 "-"，不会因为某一项失败让整次上报失败。
    swap_total 为 0 表示这台机器没开 swap，此时 swap 百分比报 None 而不是 0%。
    """
    out = {}
    try:
        sw = psutil.swap_memory()
        out["swap_total"] = sw.total
        out["swap"] = sw.percent if sw.total else None
    except Exception:
        pass
    try:
        out["load1"], out["load5"], out["load15"] = (round(v, 2) for v in os.getloadavg())
    except (AttributeError, OSError):
        pass  # Windows 没有 getloadavg
    out["cores"] = psutil.cpu_count(logical=True)
    try:
        out["procs"] = len(psutil.pids())
    except Exception:
        pass
    out["tcp_established"] = count_established_tcp()
    try:
        # 开机时间是绝对时间，Panopticon 拿它跟上一次比，变大了就是重启过
        out["boot_time"] = int(psutil.boot_time())
    except Exception:
        pass
    return out


def collect(prev_net, prev_ts):
    """采集一次快照。网络吞吐由本次/上次 net_io_counters 的差值算出 kbps。"""
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory().percent
    disk_detail = collect_disk_detail()
    # 顶层 disk 字段保持向后兼容（历史趋势图那条线、阈值告警都读它）：
    # 优先用根分区的百分比，万一根分区没采集到（理论上不该发生）就退而求其次用最紧张的那块盘
    root = next((d for d in disk_detail if d["path"] == "/"), None)
    disk = root["percent"] if root else (disk_detail[0]["percent"] if disk_detail else 0.0)
    net = psutil.net_io_counters()
    now = time.time()

    elapsed = max(now - prev_ts, 1e-6)
    net_in_kbps = (net.bytes_recv - prev_net.bytes_recv) / 1024 / elapsed
    net_out_kbps = (net.bytes_sent - prev_net.bytes_sent) / 1024 / elapsed

    sample = {
        "ts": int(now),
        "cpu": cpu,
        "mem": mem,
        "disk": disk,
        "disk_detail": disk_detail,
        "net_in_kbps": round(net_in_kbps, 2),
        "net_out_kbps": round(net_out_kbps, 2),
    }
    sample.update(collect_system())
    return sample, net, now


def main():
    cfg = load_config()
    panel_name = cfg["panel_name"]
    report_url = cfg["report_url"]
    shared_secret = cfg["shared_secret"]
    interval_seconds = cfg.get("interval_seconds", 60)
    report_ip = cfg.get("report_ip", True)  # 告警卡片要显示内/外网 IP；不想让 agent 对外发请求查公网 IP 可以关掉

    prev_net = psutil.net_io_counters()
    prev_ts = time.time()
    ip_internal = get_internal_ip() if report_ip else None
    ip_external = get_external_ip() if report_ip else None
    last_external_ip_check = time.time()

    def report_once():
        nonlocal prev_net, prev_ts, ip_internal, ip_external, last_external_ip_check
        sample, prev_net, prev_ts = collect(prev_net, prev_ts)
        sample["panel"] = panel_name
        if report_ip:
            if time.time() - last_external_ip_check >= EXTERNAL_IP_REFRESH_SECONDS or not ip_external:
                ip_external = get_external_ip()
                last_external_ip_check = time.time()
            if not ip_internal:
                ip_internal = get_internal_ip()
            sample["ip_internal"] = ip_internal
            sample["ip_external"] = ip_external
        resp = requests.post(
            report_url,
            json=sample,
            headers={"X-Metrics-Secret": shared_secret},
            timeout=8,
        )
        if resp.status_code != 200:
            print(f"[metrics_agent] 上报失败: HTTP {resp.status_code} {resp.text}")

    print(f"[metrics_agent] 启动，panel={panel_name}，每 {interval_seconds}s 上报一次到 {report_url}")
    try:
        # 一键安装那边想装完就能在主看板立刻看到卡片，所以启动后马上报一次，
        # 不用等第一个 interval_seconds（默认 60s）过去才有数据。
        report_once()
    except Exception as e:
        print(f"[metrics_agent] 首次上报出错: {e}")

    while True:
        time.sleep(interval_seconds)
        try:
            report_once()
        except Exception as e:
            # 网络抖动/Panopticon 临时不可达都不该让 agent 退出，下一轮再试
            print(f"[metrics_agent] 上报出错: {e}")


if __name__ == "__main__":
    main()
