"""
生产环境的 gunicorn 配置：gunicorn -c gunicorn.conf.py app:app

只开一个 worker 进程、多线程：网页终端的一次性票、远程数据缓存、登录防爆破计数都在进程内存里，
多个 worker 会各看各的。每个打开的终端（WebSocket）占一个线程，所以线程数要比终端上限
（terminal.MAX_TOTAL）多出一截留给看板本身。
巡检、历史清理、审计重试这几个后台线程在 worker 起来之后启动，不用另跑 worker.py。
"""
bind = "0.0.0.0:1810"
workers = 1
worker_class = "gthread"
threads = 32
timeout = 60
graceful_timeout = 10
# 前面的反代（Caddy 默认把到上游的空闲连接留 2 分钟）会复用连接；这里要比它留得久。gunicorn 默认
# 2 秒就关，反代偶尔拿一条刚被关掉的连接发请求，得到 EOF，POST 不会重试，直接回 502。
keepalive = 150


def post_worker_init(worker):
    from app import start_background_workers
    start_background_workers()
