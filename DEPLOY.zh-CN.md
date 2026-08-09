# 部署到上海服务器

按我们之前定下的架构来：Panopticon（这个 Dashboard）长期公网可见、带登录验证；
宝塔面板后台完全关闭公网访问，只信 WireGuard；两者之间的 API 通信全程走 WireGuard 隧道。

```
[你，随时随地] --公网 HTTPS--> [Panopticon，上海服务器，Nginx+HTTPS+登录]
                                          │
                                    WireGuard 隧道
                                          │
                              [宝塔面板后台，端口 18101，只信 WireGuard，不对公网开放]
```

以下步骤假设上海服务器上已经装了宝塔面板，且要在同一台（或另一台专门跑 Dashboard 的）机器上部署 Panopticon。

---

## 第一步：宝塔面板设置

### 1.1 开启 API 接口

面板后台 → 设置 → API 接口 → 打开接口状态，复制"接口密钥"（后面填进 `config.yaml` 的 `api_key`）。

### 1.2 面板端口改成 18101

面板后台 → 设置 → 面板设置 → 面板端口，改成 `18101`，保存并按提示重启面板服务。

### 1.3 关闭公网访问该端口，只信 WireGuard

前提是这台服务器已经加入了你的 WireGuard 网段（见第二步）。用 `ufw` 举例：

```bash
ufw deny 18101/tcp
ufw allow in on wg0 to any port 18101 proto tcp
```

如果这台服务器同时对外提供网站服务，网站端口不受影响，正常放行：

```bash
ufw allow 80/tcp
ufw allow 443/tcp
```

### 1.4 IP 白名单（双保险）

面板后台 → 设置 → API 接口 → IP 白名单，这里填的是**谁允许来调用这台面板的 API**，
所以应该填 **Panopticon/hub 所在服务器的 WireGuard 内网 IP**（按目前的组网就是上海那台的 `10.10.0.1`），
不是这台面板自己的 IP，也不是公网 IP。因为 ufw 已经把 `18101` 端口锁死只放行 wg0 网段，
这一步理论上只有 `10.10.0.1` 能连进来，白名单填对了就是双保险，填反了 API 请求会直接被拒绝。

---

## 第二步：WireGuard 组网（如果还没搭）

在**跑 Dashboard 的服务器**和**每一台宝塔服务器**上都要装 WireGuard 并加入同一个私有网段。
以 Debian/Ubuntu 为例，最简流程：

```bash
apt install wireguard -y
cd /etc/wireguard
umask 077
wg genkey | tee privatekey | wg pubkey > publickey
```

`/etc/wireguard/wg0.conf` 大致结构（每台机器一份，IP 各不相同，比如 Dashboard 是 `10.10.0.1`，
面板 A 是 `10.10.0.2`）：

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

你自己的笔记本/手机也作为一个 WireGuard 客户端加入同一网段，这样出差在外也能连回来管理面板。
如果你已经有现成的 WireGuard 组网（很多人用 Netmaker / Tailscale 风格的管理面板），这一步跳过，
只要保证 Dashboard 服务器和各面板服务器互通即可。

---

## 第三步：部署 Panopticon

### 3.1 拉取代码

```bash
git clone https://github.com/Akiyama-Mizuki-44/Nightcord-Panopticon.git nightcord-panopticon
cd nightcord-panopticon
```

### 3.2 装依赖（建议用虚拟环境）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install gunicorn   # 生产环境用，见下面
```

### 3.3 配置面板、登录密码、告警

服务起来之后（见 3.4），比手动改 YAML 更省事的方式是直接打开 `http://127.0.0.1:1810/setup`
（先只在服务器本机或 WireGuard 内网访问，配置完、密码设好了之后再走 3.5 挂上 Nginx 对公网开放——
配置向导本身在 `config.yaml` 还不存在时是不需要登录的，别在配置完成前就把端口暴露出去）。
之后这个页面常驻在 `/settings`，随时可以回来改，改密钥类字段时留空就是不修改。

如果更习惯手动编辑 YAML，也可以：

```bash
cp config.example.yaml config.yaml
```

按之前定的架构填：

```yaml
panels:
  - name: "上海-Web01"
    url: "http://10.10.0.2:18101"     # 面板在 WireGuard 网段里的内网地址
    api_key: "第一步复制的接口密钥"
    verify_ssl: false                 # 流量已在 WireGuard 隧道内加密

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

### 3.4 用 systemd 常驻运行

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
ExecStart=/path/to/nightcord-panopticon/.venv/bin/gunicorn -w 2 -b 127.0.0.1:1810 app:app
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

### 3.5 Nginx 反代 + HTTPS

Panopticon 要长期公网可见，必须走 HTTPS（不然 Basic Auth 的用户名密码等于明文传输）。

```nginx
server {
    listen 80;
    server_name panopticon.yourdomain.com;
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

## 第四步：验证

```bash
# 1. 没有凭证应该 401
curl -I https://panopticon.yourdomain.com/

# 2. 浏览器打开，应该弹出登录框，输入 config.yaml 里设的用户名密码
```

登录进去后应该能看到该面板卡片，展开"查看原始数据"确认 CPU/内存/磁盘/网站/数据库字段都有值，
说明 WireGuard 隧道 + 签名请求链路是通的。

告警可以先把 `thresholds` 临时调很低（比如 `cpu: 1`）触发一次测试，收到飞书/邮件推送后再改回正常值。

---

## 日常维护

- 更新代码：`git pull && systemctl restart nightcord-panopticon`
- 看日志：`journalctl -u nightcord-panopticon -f`
- 面板加了新服务器：在 `config.yaml` 的 `panels` 里加一条，重启服务即可生效
