[English](README.md) | **简体中文**

# nightcord-panopticon

> Nightcord 系列 · 自建服务器监控看板

自建的轻量级全局监控面板。机器指标来自 **青源（Qingyuan）**：一个自建监控 agent，在设置页一键
SSH 部署到任意 Linux 机器，上报分挂载点磁盘用量、CPU/内存、网络吞吐、swap、负载、进程数、连接数和
开机时间；离线、超阈值、重启都会推飞书卡片消息 / 邮件。另外还有一个独立的轻量上报脚本，能通过
vSphere API 把 ESXi 宿主机的指标接进同一套流水线，不用在 ESXi 本机装任何东西。完整介绍见
[QINGYUAN.zh-CN.md](QINGYUAN.zh-CN.md) 和 [ESXI_QINGYUAN_MONITORING.zh-CN.md](ESXI_QINGYUAN_MONITORING.zh-CN.md)。

也能接入自建的 Nightcord-Status（2702）检测服务，把它的检测目标显示成一张单独的卡片，故障目标逐个告警。

## 架构

```
浏览器 <-- HTTP --> Flask 后端 (app.py)
                            │
                            ├─ SQLite (metrics_store.py) <-- 上报 -- 青源 agent（每台机器一个）
                            │                            <-- 推送 -- Nightcord-Status（可选）
                            │
                            └─ 飞书卡片 / 邮件告警 (notifier.py)
```

- `app.py`：Flask 服务，接收青源上报、暴露 `/api/status`，跑告警巡检，并托管前端页面。
- `metrics_store.py`：青源上报的历史数据、机器名单和 2702 推送，存在 SQLite（`metrics.db`）里。
- `static/index.html`：单页 Dashboard，每 15 秒轮询一次 `/api/status`，展示总览磁贴、机器表格、每台机器的
  概览/磁盘/历史趋势图，设置也在同一个页面里。
- `agent/metrics_agent.py` + `agent_deploy.py`：青源 agent 本体，以及设置页驱动的一键 SSH 安装器。
- `esxi_reporter/esxi_metrics_reporter.py`：独立脚本（跑在别的机器上，比如已经能连 vSphere 的那台），把 ESXi 宿主机指标喂进同一个 `/api/metrics/report` 接口。
- `nightcord_status_client.py`：拉取模式下读取 Nightcord-Status 的 `/api/status.json`（推送模式不用它）。
- `notifier.py`：阈值判断 + 边缘触发的飞书/邮件告警（问题刚发生时发一次，不会每轮巡检都重发），解除时发恢复通知。
- `webauthn_manager.py` + `static/login.html`：session 登录，密码之外还支持 Passkey（WebAuthn）。

通用配置指南（不绑定具体服务器）见 [SETUP.zh-CN.md](SETUP.zh-CN.md)；一份具体案例（含 WireGuard 组网、systemd、Nginx+HTTPS）见 [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md)。

## 使用步骤

### 1. 安装依赖

```bash
cd nightcord-panopticon
pip install -r requirements.txt
```

### 2. 配置

两种方式任选一种：

**方式 A：可视化设置页（推荐）**——直接启动服务（见下一步），浏览器打开 `http://127.0.0.1:1810`，
第一次打开时设置页会提示你先配好登录密码、告警阈值、飞书/邮箱推送，保存后立即生效。之后随时可以回来改，
改的时候密钥类字段留空 = 不修改。（旧地址 `/setup`、`/settings` 仍然会转到设置页。）

**方式 B：手动编辑 YAML**
```bash
cp config.example.yaml config.yaml
```
按文件里的注释填写（`dashboard_auth` 的密码要用 `gen_password_hash.py` 生成哈希）。

### 3. 启动

```bash
python app.py
```

浏览器打开 `http://127.0.0.1:1810` 即可看到看板。

### 4. 给服务器装青源 agent

设置 → 部署：填目标服务器的 IP、SSH 账号和密码，一键安装并注册成 systemd 服务。agent 跑在别的机器上时，
先在 `config.yaml` 里把 `metrics_agent.report_url` 填成 Panopticon 在内网里的地址，原因见
[QINGYUAN.zh-CN.md](QINGYUAN.zh-CN.md) 的「agent 怎么连回 Panopticon」。机器第一次上报后就会出现在看板上。

## 安全提示

- `config.yaml` 里是明文密钥，注意权限控制，不要提交到公开代码仓库（本仓库已通过 `.gitignore` 排除）。
- 生产环境不要用 Flask 自带的开发服务器，可用 `gunicorn app:app` 或反向代理。

### 推荐架构：agent 走 WireGuard 上报

Panopticon 和各台服务器加入同一个 WireGuard 私有网段（如 `10.10.0.0/24`），agent 往 Panopticon 的
WireGuard 内网地址上报（`metrics_agent.report_url`，例如 `http://10.10.0.1:1810`），上报流量不经过公网。

```
[青源 agent，每台服务器一个] --WireGuard--> [Panopticon :1810] <--浏览器-- [你]
```

1. **只让 WireGuard 网段访问 1810**：在跑 Panopticon 的服务器上
   ```bash
   ufw allow in on wg0 to any port 1810 proto tcp
   ```
   不要对公网放行 1810：上报接口只靠共享密钥鉴权，公网直连意味着拿到密钥的人能往里灌假数据。
2. **你自己怎么看看板**：连上 WireGuard 直接访问内网地址最省事；要长期公网访问的话，前面放 Nginx + HTTPS
   反代，并且**一定要打开下一节的登录验证**。反代时别把 `/api/metrics/report`、`/api/status/report`、
   `/api/agents/heartbeat` 转出去（它们不走登录，只认密钥 / token），让它们只在 WireGuard 里可达。
   反代在别的机器上时，把那台机器的地址写进 `dashboard_auth.trusted_proxies`，不然所有访问者都会被当成
   反代那一台，一个人输错密码就会锁住所有人。

### Panopticon 自身的登录验证 + 防暴力破解

`app.py` 给所有路由挡了一层 session 登录（密码，外加可选的 Passkey / WebAuthn 作为第二种登录方式——
指纹、Face ID、安全密钥），加上按来源 IP 的失败次数锁定，默认关闭（`dashboard_auth.enabled: false`），
**只要打算公网暴露就必须打开**：

```bash
python gen_password_hash.py "你的密码"   # 生成哈希，不要在 config.yaml 里写明文密码
```

把输出粘到 `config.yaml`：

```yaml
dashboard_auth:
  enabled: true
  username: "mizuki"
  password_hash: "上一步生成的哈希"
  max_attempts: 5        # 同一 IP 连续失败这么多次后锁定
  lockout_seconds: 900   # 锁定时长（秒），默认 15 分钟
```

行为：没登录或用户名密码不对 → 跳转 `/login`（API 请求则是 `401`）；同一 IP 累计失败次数达到
`max_attempts` → 后续请求（哪怕密码是对的）都会被 `429` 拒绝，直到锁定期过去。锁定状态存在内存里，
重启 Dashboard 会重置。Passkey 要先用密码登录一次，再去 设置 → 登录 里添加，且需要 HTTPS 访问
（`localhost`/`127.0.0.1` 除外）——WebAuthn 在普通 HTTP 下用不了。

如果你只打算在 WireGuard 内网访问 Panopticon（不公网暴露），`enabled` 保持 `false` 即可，省掉每次登录的麻烦。

## 可扩展方向

- 服务 / 证书探测：HTTP、Ping、TCPing 和证书到期（先看跟 Nightcord-Status 重叠多少）。
- Windows agent 支持：`agent/metrics_agent.py` 用的 `psutil` 本身跨平台，但一键安装器（`agent_deploy.py`）是 SSH + systemd 那一套，磁盘采集也假设了 POSIX 挂载点，都还没适配 Windows。
- ESXi 上报脚本的网络吞吐：现在固定占位成 0，要补全需要查 `PerformanceManager` 性能计数器，比 CPU/内存用的 `quickStats` 麻烦不少（见 [ESXI_QINGYUAN_MONITORING.zh-CN.md](ESXI_QINGYUAN_MONITORING.zh-CN.md) 的"已知限制"）。

## License

MIT © Akiyama Mizuki · Nightcord Series
