# 用青源(Qingyuan)监控ESXi

在Nightcord-Status接入Panopticon的基础上,再把ESXi的CPU/内存/磁盘细粒度指标接进来。
这份文档只讲这一件事:怎么不在ESXi本机装任何东西,把它的资源使用率喂给Panopticon已有的
青源(Qingyuan)接收端,得到跟其它自建agent一样的历史趋势图卡片。

## 为什么不能直接把 `agent/metrics_agent.py` 装到ESXi上

青源agent是给普通Linux写的,依赖`python3 -m venv`、`pip install`、`systemd`常驻。
ESXi是VMware自己的精简系统:

- 没有完整的第三方包管理,`pip install`在ESXi上不是受支持的操作
- 自定义的常驻服务在系统更新/重启后经常被清空,装了也留不住
- 往生产虚拟化层塞非官方组件,风险跟收益不成比例

结论:**不在ESXi上跑agent,而是从已经能连到ESXi的机器"代它上报"。**

## 架构

```
[FreePBX @ 10.10.0.4]
    │
    ├── status_daemon.py ──▶ status_cache.json ──▶ xml_service ──▶ Nightcord-Status
    │                                                    (Panopticon拉取,只看在线/离线)
    │
    └── esxi_metrics_reporter.py (新增,本文档的内容)
              │ pyVmomi 连 ESXi 查 CPU/内存/磁盘
              │ 每 60s 一次
              ▼
        POST /api/metrics/report (WireGuard内网)
              │
              ▼
        [Panopticon] ── metrics_store.py ── 历史趋势图卡片
```

FreePBX已经是status_daemon.py的宿主,而且已经在用pyVmomi跟ESXi打交道(查
`overall_status`/`power_state`/`connection_state`),这个新脚本复用同一个只读账号,
不需要新的网络路径,也不需要新建WireGuard peer——出口流量走的是同一张WG网,只是方向
反过来(FreePBX主动推给Panopticon,而不是Panopticon拉FreePBX)。

## 第一步:确认能从vSphere API拿到哪些字段

用pyVmomi连上ESXi主机后:

| 指标 | 取值方式 |
|---|---|
| CPU使用率 | `host.summary.quickStats.overallCpuUsage`(MHz) ÷ (`hardware.cpuInfo.numCpuCores` × `hardware.cpuInfo.hz` / 1e6) |
| 内存使用率 | `quickStats.overallMemoryUsage`(MB) ÷ (`hardware.memorySize` / 1024 / 1024) |
| 磁盘使用率 | 遍历 `host.datastore`,汇总各datastore的 `summary.capacity` 和 `summary.freeSpace`,算总使用率(具体挑哪些datastore看你的存储布局) |
| 网络吞吐 | 要走 `PerformanceManager` 查 `net.usage.average`,比前三项麻烦。可以先填0占位,不影响卡片显示,前端只是不画那条线 |

## 第二步:写独立的上报脚本

不要塞进`status_daemon.py`的主循环,避免两件事互相影响。新建
`esxi_metrics_reporter.py`,跟status_daemon.py放在同一台机器(FreePBX):

```python
import time
import requests
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl

REPORT_URL = "http://<Panopticon的WireGuard内网地址>:1810/api/metrics/report"
SHARED_SECRET = "跟Panopticon config.yaml里metrics_agent.shared_secret保持一致"
ESXI_HOSTS = {
    "esxi-1": {"host": "192.168.3.230", "user": "readonly_user", "password": "..."},
    "esxi-2": {"host": "192.168.3.240", "user": "readonly_user", "password": "..."},
}
INTERVAL_SECONDS = 60  # 必须小于Panopticon那边的AGENT_STALE_SECONDS(180s),否则会被判定离线


def collect_one(cfg):
    ctx = ssl._create_unverified_context()
    si = SmartConnect(host=cfg["host"], user=cfg["user"], pwd=cfg["password"], sslContext=ctx)
    try:
        host = si.content.rootFolder.childEntity[0].hostFolder.childEntity[0].host[0]
        qs = host.summary.quickStats
        hw = host.summary.hardware
        cpu_pct = qs.overallCpuUsage / (hw.numCpuCores * hw.cpuMhz) * 100
        mem_pct = qs.overallMemoryUsage / (hw.memorySize / 1024 / 1024) * 100

        cap = free = 0
        for ds in host.datastore:
            cap += ds.summary.capacity
            free += ds.summary.freeSpace
        disk_pct = (1 - free / cap) * 100 if cap else 0

        return cpu_pct, mem_pct, disk_pct
    finally:
        Disconnect(si)


def main():
    while True:
        for panel_name, cfg in ESXI_HOSTS.items():
            try:
                cpu, mem, disk = collect_one(cfg)
                requests.post(
                    REPORT_URL,
                    json={
                        "panel": panel_name,
                        "ts": int(time.time()),
                        "cpu": round(cpu, 1),
                        "mem": round(mem, 1),
                        "disk": round(disk, 1),
                        "net_in_kbps": 0,
                        "net_out_kbps": 0,
                    },
                    headers={"X-Metrics-Secret": SHARED_SECRET},
                    timeout=6,
                )
            except Exception as e:
                print(f"[esxi_metrics_reporter] {panel_name} 上报失败: {e}")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
```

账号密码不要明文写死在脚本里,建议参考`agent/agent_config.example.yaml`的做法,单独拆一个
`esxi_reporter_config.yaml`(不进仓库),脚本启动时读取。

## 第三步:装成systemd常驻

参照`agent/nightcord-metrics-agent.service`改一份,名字换成
`nightcord-esxi-reporter.service`,`ExecStart`指向这个新脚本。跟status_daemon.py是两个
独立的systemd unit,互不影响,哪个挂了都不会连累另一个。

## 第四步:Panopticon这边不用改代码

只需要确认`config.yaml`里:

```yaml
metrics_agent:
  enabled: true
  shared_secret: "..."   # 跟上面脚本里的SHARED_SECRET一致
```

如果之前已经为别的服务器开了青源接收端,这段应该已经打开,直接复用同一把密钥即可——
不需要每个数据源单独配一把。

## 第五步:验证

1. 跑一次脚本或等它自然触发一轮,curl一下`REPORT_URL`确认返回200而不是401/400
2. 打开Panopticon网页,应该会看到两张新卡片,标题分别是`esxi-1`、`esxi-2`
   (每个上报的panel名各自一张卡片)
3. 等几分钟后卡片上出现CPU/内存/磁盘的历史趋势图(1h/6h/24h/7d可切换)

这两张新卡片跟已有的Nightcord-Status卡片里"esxi-1: ok"那一行是互补关系,不是重复:
Nightcord-Status给的是粗粒度的"在不在线",这里给的是细粒度的"资源用了多少"。

## 已知限制

- `net_in_kbps`/`net_out_kbps`目前占位成0,要补全的话需要额外查`PerformanceManager`的
  性能计数器,比前三项复杂,可以后续再加
- ESXi的只读账号权限建议控制到最小(只给`Read-only`角色),这个脚本不需要任何写权限
- 如果ESXi主机有多个,`ESXI_HOSTS`字典按需增加即可,互不影响
