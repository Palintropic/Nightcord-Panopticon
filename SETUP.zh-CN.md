[English](SETUP.md) | **简体中文**

# 配置指南

面向任何想部署这个项目的人的通用指南（不绑定某个具体的服务器/IP）。如果你想看一份具体案例，
可以参考 [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md)——那是按"上海 + 东京，走 WireGuard"这个真实场景写的，
本文档是更通用的版本。

## 目录

- [环境要求](#环境要求)
- [快速开始（本地试跑）](#快速开始本地试跑)
- [第一步：配置看板](#第一步配置看板)
- [第二步：安装青源 agent](#第二步安装青源-agent)
- [第三步：选一档安全方案](#第三步选一档安全方案)
- [第四步：告警通知（可选）](#第四步告警通知可选)
- [生产环境部署](#生产环境部署)
- [常见问题](#常见问题)

---

## 环境要求

- 跑看板的机器：Python 3.8+
- 要监控的 Linux 服务器：有 `python3`（含 `venv` 模块）和 `systemd`（一键安装 agent 要用），能 SSH 上去
- 每台被监控的服务器都要能访问到看板的 1810 端口（同一台机器 / 内网 / WireGuard 之类的 VPN 均可）

## 快速开始（本地试跑）

```bash
git clone https://github.com/Akiyama-Mizuki-44/Nightcord-Panopticon.git
cd Nightcord-Panopticon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

浏览器打开 `http://127.0.0.1:1810`，第一次访问因为还没配置过，设置页会提示你先配好。

## 第一步：配置看板

两种方式任选：

**方式 A：可视化设置页（推荐）**
启动服务后打开 `http://<看板地址>:1810`，在设置里填登录密码、告警阈值和通知渠道，保存即生效，
随时能回来改。编辑时密钥类字段留空 = 不修改，不会把已保存的密钥清空。

**方式 B：手动编辑 YAML**
```bash
cp config.example.yaml config.yaml
```
按文件里的注释填 `dashboard_auth` / `notifications` / `metrics_agent` 几段。登录密码不能填明文，
要先跑 `python gen_password_hash.py "你的密码"` 生成哈希再粘进去。

两种方式最终都是在写同一个 `config.yaml`，可以混着用（先用 UI 配一遍，之后想批量改也可以直接
编辑 YAML）。

## 第二步：安装青源 agent

agent 跟看板不在同一台机器上时，先在 `config.yaml` 里把 `metrics_agent.report_url` 填成那台服务器
能访问到的看板地址（比如走 WireGuard 的 `http://10.10.0.1:1810`）。不填的话安装器会拿你打开设置页
用的地址去猜，通过隧道或反代打开设置页时就会猜错。

然后 设置 → 部署：填服务器 IP、SSH 端口、账号和密码，一键安装，安装日志会实时显示。勾上"记住密码"
以后就能一键重新部署（agent 更新时要用）。机器第一次上报后（大约一分钟）就会出现在看板上。

手动安装和 agent 自己的配置见 [agent/README.zh-CN.md](agent/README.zh-CN.md)；ESXi 宿主机用单独的
上报脚本接入，见 [ESXI_QINGYUAN_MONITORING.zh-CN.md](ESXI_QINGYUAN_MONITORING.zh-CN.md)。

## 第三步：选一档安全方案

看板里存着你选择记住的 SSH 密码，以及 agent 上报用的共享密钥。选一档跟你的场景匹配的方案：

| 场景 | 方案 |
|---|---|
| 只有自己用，看板和服务器都在同一个内网/同一台机器 | `dashboard_auth.enabled: false` 即可，不用折腾 HTTPS/VPN，靠网络本身不可达来保护 |
| 服务器分散在各处，看板只从 VPN 里访问 | agent 走 WireGuard/Tailscale 之类的私有网络上报，1810 端口防火墙只放行这个私有网段。WireGuard 的具体操作见 [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) |
| 看板要长期公网暴露 | 上面的基础上，开 `dashboard_auth`（登录+防爆破，本项目内置）+ Nginx 反代 HTTPS。别把 `/api/metrics/report`、`/api/status/report`、`/api/agents/heartbeat` 转到公网：它们不走登录，只认密钥 / token |

不管选哪档，只要看板会被公网访问到，`dashboard_auth.enabled` 必须开：

```bash
python gen_password_hash.py "你的密码"
```
把输出粘到 `config.yaml`（或者直接在 设置 → 登录 里设置，更省事）。

## 第四步：告警通知（可选）

支持飞书自定义机器人 Webhook、SMTP 邮件，两个都在设置页或 `config.yaml` 的 `notifications` 段配置。
触发条件是机器 3 分钟没上报、CPU/内存/磁盘使用率超过设定的百分比、机器重启；问题解除时发恢复通知。
同一个问题只在刚发生时通知一次，不会每轮巡检都重发。

## 生产环境部署

不要用 `python app.py` 长期跑在前台，参考：

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:1810 app:app
```

监听 `0.0.0.0` 是为了让 agent 能从 WireGuard 连进来；1810 端口防火墙只放行 WireGuard 网卡
（`ufw allow in on wg0 to any port 1810 proto tcp`），不对公网开放。

用 systemd 管理进程、Nginx 反代 + Let's Encrypt 签 HTTPS 的完整示例配置见
[DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) 第二步。

> 注意：Gunicorn 只提供 HTTP 服务。要同时使用 Gunicorn 和告警功能，请另起一个 `python worker.py`
> 服务；它只跑巡检任务，不会再绑定 HTTP 端口。
> （细节同样在 DEPLOY.zh-CN.md 里）。

## 常见问题

**装了 agent，看板上一直没出现这台机器？**
多半是 `metrics_agent.report_url` 指向了那台服务器访问不到的地址。在服务器上
`journalctl -u nightcord-metrics-agent -f` 能看到每次上报失败的原因：`401` 是共享密钥对不上，
`400` 是上报内容格式不对。

**保存设置页之后没生效？**
设置页保存后会清空内存缓存、下次刷新自动拉取新配置，不需要重启进程。如果用的是手动编辑
YAML 的方式，改完文件本身也不需要重启——`app.py` 每次请求都会重新读文件，除非你在用
`gunicorn` 多进程且改的是热重载不支持的场景（一般不会遇到）。

**忘记登录密码了怎么办？**
直接编辑 `config.yaml`，把 `dashboard_auth.enabled` 改成 `false` 重启一次，打开设置页（这时不用密码），
用 `gen_password_hash.py` 生成新哈希填回去，再改回 `true`。
