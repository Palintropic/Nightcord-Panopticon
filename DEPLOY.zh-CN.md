# 部署到上海服务器

按我们之前定下的架构来：Panopticon 跑在上海服务器上；每台要监控的服务器装一个青源 agent，
通过 WireGuard 往 Panopticon 上报；Panopticon 的 1810 端口只对 WireGuard 开放。要从公网看的话，
再在前面挂 Nginx + HTTPS + 登录。

```
[青源 agent，每台服务器一个] --WireGuard--> [Panopticon，上海服务器，:1810]
                                                    │
                                          （可选）Nginx + HTTPS + 登录
                                                    │
                                          [你，随时随地，公网 HTTPS]
```

---

## 第一步：WireGuard 组网 + 防火墙

### 1.1 组网（如果还没搭）

在**跑 Panopticon 的服务器**和**每一台要监控的服务器**上都要装 WireGuard 并加入同一个私有网段。
以 Debian/Ubuntu 为例，最简流程：

```bash
apt install wireguard -y
cd /etc/wireguard
umask 077
wg genkey | tee privatekey | wg pubkey > publickey
```

`/etc/wireguard/wg0.conf` 大致结构（每台机器一份，IP 各不相同，比如 Panopticon 是 `10.10.0.1`，
服务器 A 是 `10.10.0.2`）：

```ini
[Interface]
PrivateKey = <这台机器的私钥>
Address = 10.10.0.1/24
ListenPort = 51820

[Peer]
# 对端（另一台机器）的公钥和地址
PublicKey = <对端公钥>
AllowedIPs = 10.10.0.2/32
Endpoint = <对端公网IP>:51820
PersistentKeepalive = 25
```

```bash
wg-quick up wg0
systemctl enable wg-quick@wg0
```

你自己的笔记本/手机也作为一个 WireGuard 客户端加入同一网段，这样出差在外也能连回来看看板。
如果你已经有现成的 WireGuard 组网（很多人用 Netmaker / Tailscale 风格的管理面板），这一步跳过，
只要保证 Panopticon 和各台服务器互通即可。

### 1.2 1810 只对 WireGuard 开放

在跑 Panopticon 的服务器上，用 `ufw` 举例：

```bash
ufw allow in on wg0 to any port 1810 proto tcp
```

不要对公网放行 1810。要公网访问就走 2.5 的 Nginx（80/443）。

---

## 第二步：部署 Panopticon

### 2.1 拉取代码

```bash
git clone https://github.com/Akiyama-Mizuki-44/Nightcord-Panopticon.git nightcord-panopticon
cd nightcord-panopticon
```

### 2.2 装依赖（建议用虚拟环境）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install gunicorn   # 生产环境用，见下面
```

### 2.3 配置登录密码、告警、agent 上报地址

服务起来之后（见 2.4），比手动改 YAML 更省事的方式是直接在浏览器里打开设置页
（先只在服务器本机或 WireGuard 内网访问，配置完、密码设好了之后再走 2.5 挂上 Nginx 对公网开放——
`config.yaml` 还不存在时设置页是不需要登录的，别在配置完成前就把端口暴露出去）。
之后随时可以回来改，改密钥类字段时留空就是不修改。

如果更习惯手动编辑 YAML，也可以：

```bash
cp config.example.yaml config.yaml
```

按之前定的架构填：

```yaml
metrics_agent:
  enabled: true
  shared_secret: "生成一个随机字符串"
  report_url: "http://10.10.0.1:1810"  # Panopticon 在 WireGuard 网段里的地址，agent 往这里上报

dashboard_auth:
  enabled: true                       # 公网暴露，必须开
  username: "mizuki"
  password_hash: "见下一步生成"
  max_attempts: 5
  lockout_seconds: 900

notifications:
  cooldown_seconds: 600
  thresholds: { cpu: 90, mem: 90, disk: 85 }
  feishu:
    enabled: true
    webhook: "你的飞书自定义机器人 Webhook"
  email:
    enabled: false   # 按需开
```

生成登录密码哈希：

```bash
python gen_password_hash.py "你的密码"
```

把输出粘到上面的 `password_hash` 里。

### 2.4 用 systemd 常驻运行

监听 `0.0.0.0` 是为了让 agent 能从 WireGuard 连进来；公网挡在 1.2 的 ufw 规则外面。

新建 `/etc/systemd/system/nightcord-panopticon.service`：

```ini
[Unit]
Description=Nightcord Panopticon Dashboard
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/nightcord-panopticon
Environment=PATH=/path/to/nightcord-panopticon/.venv/bin
ExecStart=/path/to/nightcord-panopticon/.venv/bin/gunicorn -w 2 -b 0.0.0.0:1810 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> 注意：Gunicorn 只提供 Web 服务；后台告警巡检需要单独起一个不监听 HTTP 端口的 worker：
> ```ini
> # 再建一个 nightcord-panopticon-alerts.service：
> ExecStart=/path/to/nightcord-panopticon/.venv/bin/python worker.py
> ```
> `worker.py` 不会绑定 1810，因此能与 Gunicorn 同时运行。个人项目若直接用 `python app.py`，
> Web 服务和后台线程会一起启动，不需要额外 worker。

```bash
systemctl daemon-reload
systemctl enable --now nightcord-panopticon
systemctl status nightcord-panopticon
```

### 2.5 Nginx 反代 + HTTPS（要公网访问时）

Panopticon 要长期公网可见，必须走 HTTPS（不然登录密码等于明文传输）。只在 WireGuard 里看的话这一步可以跳过。

```nginx
server {
    listen 80;
    server_name panopticon.yourdomain.com;
    # 上报 / 心跳接口不走登录，只认密钥 / token，只让 WireGuard 里的机器直连 1810，不经公网反代
    location ~ ^/api/(metrics/report|status/report|agents/heartbeat)$ {
        return 404;
    }
    location / {
        proxy_pass http://127.0.0.1:1810;
        proxy_set_header Host $host;
        # 覆盖客户端自行伪造的 X-Forwarded-For，应用只信任 Nginx 注入的真实来源地址。
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

用 certbot 一键签发并自动改写成 443 + 自动跳转：

```bash
apt install certbot python3-certbot-nginx -y
certbot --nginx -d panopticon.yourdomain.com
```

`auth.py` 里的 `get_client_ip()` 会优先读 `X-Forwarded-For`，配合上面 Nginx 配置，暴力破解锁定
统计的是访问者真实 IP，而不是 Nginx 自己的回环地址。

---

## 第三步：给每台服务器装青源 agent

设置 → 部署：填服务器的 WireGuard 内网 IP、SSH 账号和密码，点一键安装。勾上"记住密码"，以后 agent
更新时可以在"已记住密码的服务器"里一键重新部署。跑 Panopticon 的这台服务器自己也可以装一个。

---

## 第四步：验证

```bash
# 1. 没登录访问应该被跳转到 /login
curl -I https://panopticon.yourdomain.com/

# 2. 上报接口从公网应该是 404
curl -I -X POST https://panopticon.yourdomain.com/api/metrics/report
```

浏览器登录进去后，装了 agent 的每台服务器都应该有一张卡片，CPU/内存/磁盘有数，说明 WireGuard 上报
链路是通的。没出现的话看那台服务器上的 `journalctl -u nightcord-metrics-agent -f`。

告警可以先把 `thresholds` 临时调很低（比如 `cpu: 1`）触发一次测试，收到飞书/邮件推送后再改回正常值。

---

## 日常维护

- 更新代码：`git pull && systemctl restart nightcord-panopticon`
- 看日志：`journalctl -u nightcord-panopticon -f`
- 加了新服务器：先让它加入 WireGuard，再在 设置 → 部署 里给它装 agent
- 机器退役：在 设置 → 青源机器 里"移除"，不然它会一直报离线
