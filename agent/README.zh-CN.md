# 自建监控 agent 部署说明

这个目录是**独立**于 Panopticon 主程序的，跑在各台面板服务器上（比如上海 `10.10.0.1`、
日本 `10.10.0.3`），不是跑在 Panopticon 主机上。仓库根目录的 `deploy.sh` 只会把代码同步到
Panopticon 主机，**不会**把这个目录部署到面板服务器——那两台机器需要手动同步这个目录过去，
比如：

```bash
rsync -avz --progress agent/ 你的用户名@面板服务器IP:~/nightcord-metrics-agent/
```

## 部署步骤（每台面板服务器上都要做一遍）

1. 同步这个目录到面板服务器（见上）。
2. SSH 上去，装依赖：
   ```bash
   cd ~/nightcord-metrics-agent
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. 配置：
   ```bash
   cp agent_config.example.yaml agent_config.yaml
   # 编辑 agent_config.yaml：
   #   panel_name    —— 起个能跟 Panopticon 那边对上号的名字
   #   report_url    —— Panopticon 主机在 WireGuard 内网里的地址 + /api/metrics/report
   #   shared_secret —— 要跟 Panopticon 的 config.yaml 里 metrics_agent.shared_secret 完全一致
   ```
4. 同时确认 Panopticon 那边的 `config.yaml` 已经打开了 `metrics_agent.enabled: true`
   并填好同一个 `shared_secret`（改完配置需要重启 Panopticon 服务生效）。
5. 装 systemd 常驻：
   ```bash
   sudo cp nightcord-metrics-agent.service /etc/systemd/system/
   sudo vim /etc/systemd/system/nightcord-metrics-agent.service  # 把 /path/to/... 换成真实路径
   sudo systemctl daemon-reload
   sudo systemctl enable --now nightcord-metrics-agent
   sudo systemctl status nightcord-metrics-agent
   ```

## 排查

- `journalctl -u nightcord-metrics-agent -f` 看 agent 日志，上报失败（网络问题/密钥不对）
  只会打日志，不会让进程退出。
- 密钥错误在 Panopticon 那边会看到 401，字段缺失/类型不对会看到 400——curl 一下
  `report_url` 能帮助快速定位是哪一层的问题。
