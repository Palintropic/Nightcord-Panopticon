# 青源（Qingyuan）

自建系统指标监控架构的名字，覆盖 `agent/metrics_agent.py`（面板服务器上的采集/上报端）
和 Panopticon 侧的 `metrics_store.py` + `app.py` 里的 `/api/metrics/*` 接口这一整条链路。
跟 BT 面板 API 那条链路（`bt_client.py`）并列、互不影响——业务数据继续问 BT，系统级指标走青源。

- **当前版本**：`7.17-beta`
- **内部开发代号**：`yukisakura`（仅内部记录用，不出现在面向用户的文档/界面里）
- **状态**：beta——接口、存储 schema、前端历史趋势图都已经过本机端到端验证（鉴权、字段校验、历史读写、
  清理、图表渲染都测过），也已经在真实生产环境的多台服务器上跑通（同名机器自动合并卡片、跨 WireGuard
  上报、ufw 放行等都验证过），仍标 beta 是因为还没经过长时间稳定性观察。

## 组成

| 文件 | 作用 |
|---|---|
| `agent/metrics_agent.py` | 跑在各面板服务器，采集 CPU/内存/磁盘/网络，定时 push |
| `agent/agent_config.example.yaml` | agent 的配置模板（手动部署时用，见 `agent/README.zh-CN.md`） |
| `agent/nightcord-metrics-agent.service` | agent 的 systemd 部署示例 |
| `metrics_store.py` | Panopticon 侧的 SQLite 存储层（`metrics.db`） |
| `agent_deploy.py` | 一键部署：SSH 到目标机器自动装 agent、写配置、注册并启动 systemd |
| `agent_hosts.py` | 一键部署时"记住密码"选项的加密存储层（密钥文件 `agent_hosts.key`，不进仓库） |
| `app.py` 里的 `POST /api/metrics/report` | agent 上报入口，共享密钥鉴权 |
| `app.py` 里的 `GET /api/metrics/history` | 历史数据读接口，走 dashboard_auth |
| `app.py` 里的 `GET /api/metrics/version` | 返回当前青源版本号 |
| `app.py` 里的 `/api/agent/deploy`、`/api/agent/hosts*` | 一键部署相关接口，走 dashboard_auth |
| `app.py` 里的 `GET /api/agents`、`POST /api/agents/<name>/retire` | 监控名单与"移除机器"，走 dashboard_auth |
| `app.py` 里的 `GET /api/agents/heartbeat` | 给 2702（Nightcord-Status）的只读心跳接口，`status_api.token` 鉴权 |

## 离线判定

- **上报过一次就一直盯**：`agent_last_seen` 表记每台机器最后一次上报的时间（Panopticon 收到时的服务器
  时间，不信 agent 自报的 `ts`），不随历史数据清理。超过 `AGENT_STALE_SECONDS`（180 秒，即默认 60 秒
  上报间隔下连漏 3 次）没上报就算离线：看板卡片保留、标红并显示最后上报时间，同时发"机器离线告警"。
- **移除**：机器退役或卸掉 agent 后，在 `/settings`「青源机器」里移除，之后不再显示、不再告警。
  移除后它如果又上报了，会自动回到名单里。
- **全部同时离线**（至少两台）只发一条"全部机器同时失联"：多半是 Panopticon 自己这边的网络或
  WireGuard 断了，不是每台机器都挂了。
- **恢复通知**：任何告警（离线、CPU、内存、磁盘）解除时发一条绿色"已恢复"，带持续时长。机器离线期间，
  它之前未解决的指标告警原样保留，不会被误报成"已恢复"。
- **移除不会被当成恢复**：巡检每轮按数据库里的名单把成员已移除的告警静默作废，所以移除请求跑在另一个
  进程（Gunicorn + `worker.py`）、或者刚好插在巡检中途，都不会冒出"已恢复"。
- **重启的代价**：告警状态（哪些已通知、冷却记录）只在内存里。Panopticon 重启后仍存在的问题会再报一次；
  在重启后第一轮巡检之前就恢复了的问题，收不到"已恢复"。
- **心跳接口**：`GET /api/agents/heartbeat`，请求头 `X-Status-Token` 对上 `config.yaml` 的
  `status_api.token` 才返回。只给每台机器的名字、最后上报时间、在线判定，不含 IP 和磁盘细节。

## 部署方式

两种都行，效果一样：

1. **一键部署（推荐）**：`/settings` 页面「一键部署 agent」区块，填服务器 IP / 端口 / SSH 账号密码，
   点一下自动装完（对应 `agent_deploy.py`）。`metrics_agent.enabled` 和 `shared_secret` 会在第一次
   一键部署时自动补全，不用再手动改 `config.yaml`。
2. **手动部署**：见 `agent/README.zh-CN.md`，自己 rsync 代码、装依赖、写 `agent_config.yaml`、配 systemd。

一键部署目前假设目标机器上有 `python3`（含 `venv` 模块）和 `systemd`；账号不是 `root` 的话会自动走
`sudo -S`（要求 sudo 密码跟 SSH 登录密码一致）。主机指纹用的是"首次连接自动信任"（TOFU），部署日志里
会打印出来，多疑的话可以自己去目标机核对。

## agent 怎么连回 Panopticon：要不要 WireGuard

agent 上报走的是 `config.yaml` 里 `metrics_agent.report_url`（没填的话会退而求其次猜一个，见下面
「一键部署踩坑」）。这个地址该填什么，取决于 agent 跑在哪：

- **agent 跟 Panopticon 是同一台机器**（比如宝塔面板服务器本身也跑着 Panopticon）：直接填
  `http://127.0.0.1:1810` 就行，不需要 WireGuard，本机回环访问不存在跨网络的问题。
- **agent 跑在别的机器上**：这时候才需要 Panopticon 和这台机器之间有一条互通的内网路径——
  WireGuard 是推荐做法（见 [README.zh-CN.md](README.zh-CN.md#推荐架构panopticon-长期公网可见宝塔面板后台完全收进-wireguard)
  的整体架构说明），`report_url` 填 Panopticon 在 WireGuard 网段里的地址（例如 `http://10.10.0.1:1810`）。
  不建议图省事直接把 1810 端口暴露公网——这个接口只有共享密钥鉴权，没有来源限制，公网直连意味着
  任何拿到密钥的人都能往里灌假数据，攻击面跟"必要性"完全不成比例。

判断标准就一句话：**只要 Panopticon 和 agent 不在同一台机器上，就需要某种内网互通方式，WireGuard 是
目前唯一测过、文档化的方案**；只有 agent 装在 Panopticon 自己身上这一种情况可以完全跳过组网。

### 一键部署踩坑：`report_url` 猜错

`metrics_agent.report_url` 不填的话，一键部署会拿"你点安装按钮那一刻浏览器访问设置页用的地址"顶上——
如果你是通过 SSH 隧道（`tunnel.sh`）访问 `127.0.0.1:1810`，但部署目标是别的机器，这个猜测就是错的：
agent 会拿到一个只在它自己身上才有意义的地址，上报永远静默失败，dashboard 上也不会出现这张卡片，
而且不会有任何报错提示你发生了什么。**只要会给"本机以外"的机器装 agent，就强烈建议先手动把
`metrics_agent.report_url` 填好**，一键部署检测到这种情况也会在安装日志里弹警告，但填了就不用等它提醒。

## 版本历史

> 版本号是主观起的代号式编号，不是"数字越大越新"的语义化版本——`7.17-beta` 在 `8.16-beta` 之后，
> 纯粹是这一版换了新代号。等正式版发布会切到真正的语义化版本号。

- 未发版（在 `7.17-beta` 之上）：
  - **离线告警**：上报过的机器一直盯，3 分钟没上报就告警、卡片不再消失；设置页可"移除"机器；
    全部同时离线合并成一条。详见上面「离线判定」。
  - **恢复通知**：告警解除时发绿色"已恢复"卡片（不 @所有人）和邮件。
  - **给 2702 的心跳接口** `GET /api/agents/heartbeat`（`status_api.token` 鉴权）。
- `7.17-beta`（代号 `yukisakura`）——比初版多了一整圈能力：
  - **IP 采集**：agent 上报内网 IP（出口路由探测）+ 公网 IP（可关），告警卡片和 dashboard 卡片都能显示。
  - **磁盘从"只看根分区"升级成真实多挂载点**：枚举所有真实块设备，每个都是真实 total/used/percent，
    不再是单一个百分比占位；告警也跟着按挂载点逐个判断阈值，不再只盯 `/`。
  - **ESXi 集成**（`esxi_reporter/`）：独立脚本通过 vSphere API 把 ESXi 宿主机指标接进同一套流水线，
    不用在 ESXi 本机装任何东西。
  - **告警改成边缘触发**：同一个问题只在刚发生时通知一次，不会每轮巡检都重发；解决后又复发才会再通知。
  - **飞书通知从纯文本换成 Schema 2.0 卡片**：带颜色标题栏、@所有人，设置页新增"发送测试通知"按钮。
  - **一键部署体验优化**：安装日志实时流式显示（不用再等一两分钟看不到进度）；自动检测并修复
    `python3-venv` 缺失（Debian/Ubuntu 常见坑）；新增"绑定到已有宝塔面板"选项 + IP 兜底匹配，解决
    同一台机器装了宝塔又装青源时在 dashboard 上被拆成两张卡片的问题。
  - **修了几个部署链路上的真实 bug**：`report_url` 在通过 SSH 隧道访问设置页时会猜错成本机地址；
    `systemctl enable --now` 对已运行的服务是空操作，重新部署时新配置永远吃不到，改成 `enable` + `restart`。
- `8.16-beta`（代号 `yukikaze`）——首个可用版本：agent 采集/上报 + SQLite 历史存储 + 接收端接口 +
  前端历史趋势图（CPU/内存/磁盘三线 SVG 图表，支持 1h/6h/24h/7d 范围切换；无上报数据的面板不显示该区块）。
