[English](SETUP.md) | **简体中文**

# 配置指南

面向任何想部署这个项目的人的通用指南（不绑定某个具体的服务器/IP）。如果你想看一份具体案例，
可以参考 [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md)——那是按"上海 + 日本两台宝塔面板"这个真实场景写的，
本文档是更通用的版本。

## 目录

- [环境要求](#环境要求)
- [快速开始（本地试跑）](#快速开始本地试跑)
- [第一步：在宝塔面板开启 API](#第一步在宝塔面板开启-api)
- [第二步：配置面板列表](#第二步配置面板列表)
- [第三步：选一档安全方案](#第三步选一档安全方案)
- [第四步：告警通知（可选）](#第四步告警通知可选)
- [生产环境部署](#生产环境部署)
- [常见问题](#常见问题)

---

## 环境要求

- Python 3.8+
- 一台或多台已安装宝塔面板（BT Panel）的服务器，版本不限（字段可能因版本略有差异，但核心接口稳定）
- 跑 Dashboard 的这台机器，能够访问到面板的管理端口（同一台机器 / 内网 / VPN 均可）

## 快速开始（本地试跑）

```bash
git clone https://github.com/Akiyama-Mizuki-44/Nightcord-Panopticon.git
cd Nightcord-Panopticon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

浏览器打开 `http://127.0.0.1:1810`，第一次访问因为还没配置任何面板，会自动引导你去 `/setup`。

## 第一步：在宝塔面板开启 API

对每一台要接入的面板：

1. 面板后台 → 设置 → API 接口 → 打开接口状态，复制"接口密钥"。
2. 同一页面的 **IP 白名单**，填**运行本 Dashboard 的那台机器**的 IP（不是面板自己的 IP）。
   - 如果 Dashboard 和面板在同一台机器上，填 `127.0.0.1`。
   - 如果 Dashboard 在另一台机器上，填那台机器能被面板看到的 IP（公网 IP，或者你们之间组了
     VPN/WireGuard 的话就填内网 IP，见下面的安全方案）。
3. （可选但推荐）把面板管理端口从默认的 `8888` 改成别的端口，减少被扫描到的概率。

## 第二步：配置面板列表

两种方式任选：

**方式 A：可视化设置页（推荐）**
启动服务后打开 `http://<Dashboard地址>:1810/setup`，填面板名称/地址/API 密钥，保存即生效。
这个页面之后常驻在 `/settings`，随时能回来加面板、改告警、改登录密码。编辑时密钥类字段
留空 = 不修改，不会把已保存的密钥清空。

**方式 B：手动编辑 YAML**
```bash
cp config.example.yaml config.yaml
```
按文件里的注释填 `panels` / `dashboard_auth` / `notifications` 三段。登录密码不能填明文，
要先跑 `python gen_password_hash.py "你的密码"` 生成哈希再粘进去。

两种方式最终都是在写同一个 `config.yaml`，可以混着用（先用 UI 配一遍，之后想批量改也可以直接
编辑 YAML，改完重启进程生效）。

## 第三步：选一档安全方案

Dashboard 一旦跑起来，手里就握着能调用所有面板 API 的凭证，选一档跟你的场景匹配的方案：

| 场景 | 方案 |
|---|---|
| 只有自己用，Dashboard 和面板都在同一个内网/同一台机器 | `dashboard_auth.enabled: false` 即可，不用折腾 HTTPS/VPN，靠网络本身不可达来保护 |
| Dashboard 要长期公网暴露，面板不介意也暴露公网 | 开 `dashboard_auth`（登录+防爆破，本项目内置）+ Nginx 反代 HTTPS，面板侧把 IP 白名单锁死成 Dashboard 的固定 IP |
| Dashboard 要公网暴露，但面板管理端口完全不想暴露 | 上面的基础上，Dashboard 和各面板之间再拉一层 WireGuard/Tailscale 之类的私有网络，面板管理端口防火墙只放行这个私有网段。具体操作可以参考 [DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) 里 WireGuard 组网的部分 |

不管选哪档，只要 Dashboard 会被公网访问到，`dashboard_auth.enabled` 必须开：

```bash
python gen_password_hash.py "你的密码"
```
把输出粘到 `config.yaml`（或者直接在 `/settings` 页面里设置，更省事）。

## 第四步：告警通知（可选）

支持飞书自定义机器人 Webhook、SMTP 邮件，两个都在 `/settings` 页面或 `config.yaml` 的
`notifications` 段配置。触发条件是面板离线、或 CPU/内存/磁盘使用率超过设定的百分比，
同一条告警有冷却时间防止刷屏。

## 生产环境部署

不要用 `python app.py` 长期跑在前台，参考：

```bash
pip install gunicorn
gunicorn -w 2 -b 127.0.0.1:1810 app:app
```

用 systemd 管理进程、Nginx 反代 + Let's Encrypt 签 HTTPS 的完整示例配置见
[DEPLOY.zh-CN.md](DEPLOY.zh-CN.md) 第三步。

> 注意：Gunicorn 只提供 HTTP 服务。要同时使用 Gunicorn 和告警功能，请另起一个 `python worker.py`
> 服务；它只跑巡检任务，不会再绑定 HTTP 端口。
> （细节同样在 DEPLOY.zh-CN.md 里）。

## 常见问题

**面板返回签名错误 / 请求失败？**
多半是服务器时间不同步（签名算法里用了时间戳），检查 Dashboard 和面板的系统时间是否一致
（`timedatectl`/NTP）。

**IP 白名单应该填谁的 IP？**
填**发起 API 请求的那台机器**的 IP，也就是运行 Dashboard 的机器，不是面板自己的 IP。

**保存设置页之后没生效？**
`/settings` 保存后会清空内存缓存、下次刷新自动拉取新配置，不需要重启进程。如果用的是手动编辑
YAML 的方式，改完文件本身也不需要重启——`app.py` 每次请求都会重新读文件，除非你在用
`gunicorn` 多进程且改的是热重载不支持的场景（一般不会遇到）。

**忘记登录密码了怎么办？**
直接编辑 `config.yaml`，把 `dashboard_auth.enabled` 改成 `false` 重启一次，登录进 `/settings`
用 `gen_password_hash.py` 生成新哈希填回去，再改回 `true`。
