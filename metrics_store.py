"""
青源（Qingyuan）架构的存储层——自建监控 agent 上报数据的落库。SQLite 文件跟 config.yaml 同目录，
不额外引入依赖（sqlite3 是标准库）。一台面板一条时间序列，(panel, ts) 联合主键，
重复上报同一秒直接覆盖而不是报错，方便 agent 端重试。
"""
import json
import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "metrics.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
  panel TEXT NOT NULL,
  ts INTEGER NOT NULL,
  cpu REAL,
  mem REAL,
  disk REAL,
  net_in_kbps REAL,
  net_out_kbps REAL,
  PRIMARY KEY (panel, ts)
);
CREATE INDEX IF NOT EXISTS idx_metrics_panel_ts ON metrics(panel, ts);

CREATE TABLE IF NOT EXISTS agent_ip (
  panel TEXT PRIMARY KEY,
  ip_internal TEXT,
  ip_external TEXT
);

CREATE TABLE IF NOT EXISTS agent_disk_detail (
  panel TEXT PRIMARY KEY,
  detail_json TEXT
);

-- 每台机器最后一次上报的时间（Panopticon 收到上报时的服务器时间，不用 agent 自报的 ts，
-- 免得 agent 时钟漂移导致误判离线）。不随 metrics 历史一起清理：离线再久也还记得这台机器。
-- generation：监控代次，每"移除"一次加 1。告警发出时记下当时的代次，代次变了的旧告警永远作废，
-- 这样"移除 → 又上报回来"之后，移除之前的旧告警不会被当成恢复。
CREATE TABLE IF NOT EXISTS agent_last_seen (
  panel TEXT PRIMARY KEY,
  seen_at INTEGER NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0
);

-- 在设置页"移除"掉的机器。它之后只要再上报一次，记录就会在同一个事务里被删掉，自动回到名单里。
CREATE TABLE IF NOT EXISTS agent_retired (
  panel TEXT PRIMARY KEY,
  retired_at INTEGER NOT NULL
);

-- 只存最新一份、不需要历史的系统信息：开机时间（判断重启）、核数（把负载折算成每核百分比）、swap 总量
-- （0 = 没开 swap）。新版 agent 才会报，老 agent / ESXi 上报器没有这一行。
CREATE TABLE IF NOT EXISTS agent_system (
  panel TEXT PRIMARY KEY,
  boot_time INTEGER,
  cores INTEGER,
  swap_total INTEGER
);

-- Nightcord-Status（2702）推过来的最新一份目标状态，一个来源一行，只留最新。
-- received_at 是 Panopticon 收到时的服务器时间，判断 2702 有没有失联用。
CREATE TABLE IF NOT EXISTS status_push (
  source TEXT PRIMARY KEY,
  received_at INTEGER NOT NULL,
  body_json TEXT NOT NULL
);
"""


# metrics 表后来加的列：老 agent 和 ESXi 上报器不带这些字段，一律可空，缺了就是 NULL。
EXTRA_METRIC_COLUMNS = (
    ("swap", "REAL"),
    ("load1", "REAL"),
    ("load5", "REAL"),
    ("load15", "REAL"),
    ("procs", "INTEGER"),
    ("tcp_established", "INTEGER"),
)
_EXTRA_NAMES = tuple(name for name, _ in EXTRA_METRIC_COLUMNS)
_SAMPLE_COLUMNS = ("ts", "cpu", "mem", "disk", "net_in_kbps", "net_out_kbps") + _EXTRA_NAMES


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(agent_last_seen)")]
        if "generation" not in columns:  # 早期版本建的表没有这一列
            conn.execute("ALTER TABLE agent_last_seen ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
        metric_columns = {r["name"] for r in conn.execute("PRAGMA table_info(metrics)")}
        for name, sql_type in EXTRA_METRIC_COLUMNS:
            if name not in metric_columns:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {name} {sql_type}")
        # 升级前就在上报的机器还没有 agent_last_seen 记录，用历史里最新一条样本补上，
        # 否则它们要等下一次上报才会出现在"应该在线"的名单里。历史里的 ts 是 agent 自报的，
        # 时钟跑快的 agent 会留下未来时间，回填时封顶到现在，不然它停报后会一直显示在线
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_last_seen (panel, seen_at)
            SELECT panel, MIN(MAX(ts), ?) FROM metrics GROUP BY panel
            """,
            (int(time.time()),),
        )


def insert_sample(panel, ts, cpu, mem, disk, net_in_kbps, net_out_kbps, extra=None):
    """extra：EXTRA_METRIC_COLUMNS 里的字段，缺哪个就存 NULL。"""
    extra = extra or {}
    values = (panel, ts, cpu, mem, disk, net_in_kbps, net_out_kbps) + tuple(extra.get(n) for n in _EXTRA_NAMES)
    columns = ("panel",) + _SAMPLE_COLUMNS
    updates = ", ".join(f"{c}=excluded.{c}" for c in _SAMPLE_COLUMNS[1:])
    with _connect() as conn:
        conn.execute(
            f"""
            INSERT INTO metrics ({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))})
            ON CONFLICT(panel, ts) DO UPDATE SET {updates}
            """,
            values,
        )


def update_system(panel, boot_time, cores, swap_total):
    """
    记下最新的开机时间 / 核数 / swap 总量，返回之前记的开机时间（第一次报或之前没报过就是 None）。
    读旧值和写新值在同一个事务里，两个并发上报不会都拿到同一个旧值、各发一次重启通知。
    哪项这次没报（None）就保留原来的值。
    """
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT boot_time FROM agent_system WHERE panel = ?", (panel,)).fetchone()
        conn.execute(
            """
            INSERT INTO agent_system (panel, boot_time, cores, swap_total) VALUES (?, ?, ?, ?)
            ON CONFLICT(panel) DO UPDATE SET
              boot_time=COALESCE(excluded.boot_time, agent_system.boot_time),
              cores=COALESCE(excluded.cores, agent_system.cores),
              swap_total=COALESCE(excluded.swap_total, agent_system.swap_total)
            """,
            (panel, boot_time, cores, swap_total),
        )
        return row["boot_time"] if row else None


def get_system(panel):
    with _connect() as conn:
        row = conn.execute(
            "SELECT boot_time, cores, swap_total FROM agent_system WHERE panel = ?", (panel,)
        ).fetchone()
        return dict(row) if row else {"boot_time": None, "cores": None, "swap_total": None}


def mark_seen(panel, seen_at):
    """
    记一次有效上报。直接覆盖成这次的服务器时间（不取 MAX，免得哪个错误的大值把它永远钉住），
    并在同一个事务里取消"已移除"标记——上报本身就说明机器回来了，不靠比较时间戳推断先后。
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_last_seen (panel, seen_at) VALUES (?, ?)
            ON CONFLICT(panel) DO UPDATE SET seen_at=excluded.seen_at
            """,
            (panel, int(seen_at)),
        )
        conn.execute("DELETE FROM agent_retired WHERE panel = ?", (panel,))


def list_known_panels():
    """
    所有"应该在线"的机器：上报过至少一次、且没被移除（移除后再上报会自动取消移除，见 mark_seen）。
    返回 [{"panel", "seen_at", "generation"}, ...]，按名字排序。离线判定由调用方拿 seen_at 跟当前时间比；
    generation 是读取这一刻的监控代次，告警计划带着它，发送时代次变了就作废（见 Notifier.notify）。
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.panel, s.seen_at, s.generation FROM agent_last_seen s
            LEFT JOIN agent_retired r ON r.panel = s.panel
            WHERE r.panel IS NULL
            ORDER BY s.panel
            """
        ).fetchall()
        return [dict(r) for r in rows]


def watch_generations(panels):
    """
    发通知前的现查：这些机器都还在监控名单里时返回 {panel: 监控代次}，有任何一台不在（没上报过 /
    当前被移除）就返回 None。调用方拿发送时记下的代次跟恢复时的比，不一样就说明中间移除过。
    """
    panels = set(panels)
    if not panels:
        return {}
    with _connect() as conn:
        placeholders = ",".join("?" * len(panels))
        rows = conn.execute(
            f"""
            SELECT s.panel, s.generation FROM agent_last_seen s
            LEFT JOIN agent_retired r ON r.panel = s.panel
            WHERE r.panel IS NULL AND s.panel IN ({placeholders})
            """,
            tuple(panels),
        ).fetchall()
        if len(rows) != len(panels):
            return None
        return {r["panel"]: r["generation"] for r in rows}


def retire_panel(panel, retired_at):
    """把机器移出"应该在线"的名单。只认上报过的机器，返回是否真的移除了。"""
    with _connect() as conn:
        known = conn.execute("SELECT 1 FROM agent_last_seen WHERE panel = ?", (panel,)).fetchone()
        if not known:
            return False
        conn.execute(
            """
            INSERT INTO agent_retired (panel, retired_at) VALUES (?, ?)
            ON CONFLICT(panel) DO UPDATE SET retired_at=excluded.retired_at
            """,
            (panel, int(retired_at)),
        )
        conn.execute("UPDATE agent_last_seen SET generation = generation + 1 WHERE panel = ?", (panel,))
        return True


def upsert_ip(panel, ip_internal, ip_external):
    """agent 每次上报都可能带 IP，直接覆盖成最新的即可，IP 不需要历史。"""
    if not ip_internal and not ip_external:
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_ip (panel, ip_internal, ip_external) VALUES (?, ?, ?)
            ON CONFLICT(panel) DO UPDATE SET
              ip_internal=COALESCE(excluded.ip_internal, agent_ip.ip_internal),
              ip_external=COALESCE(excluded.ip_external, agent_ip.ip_external)
            """,
            (panel, ip_internal, ip_external),
        )


def get_ip(panel):
    with _connect() as conn:
        row = conn.execute(
            "SELECT ip_internal, ip_external FROM agent_ip WHERE panel = ?", (panel,)
        ).fetchone()
        return dict(row) if row else {"ip_internal": None, "ip_external": None}


def upsert_disk_detail(panel, detail):
    """跟 IP 一样只存最新一份——每个挂载点的历史用量没必要留，卡片只需要"现在什么样"。"""
    if not detail:
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_disk_detail (panel, detail_json) VALUES (?, ?)
            ON CONFLICT(panel) DO UPDATE SET detail_json=excluded.detail_json
            """,
            (panel, json.dumps(detail)),
        )


def get_disk_detail(panel):
    with _connect() as conn:
        row = conn.execute(
            "SELECT detail_json FROM agent_disk_detail WHERE panel = ?", (panel,)
        ).fetchone()
        return json.loads(row["detail_json"]) if row else None


def get_latest(panel):
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT {", ".join(_SAMPLE_COLUMNS)}
            FROM metrics WHERE panel = ? ORDER BY ts DESC LIMIT 1
            """,
            (panel,),
        ).fetchone()
        return dict(row) if row else None


def query_history(panel, since_ts):
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {", ".join(_SAMPLE_COLUMNS)}
            FROM metrics WHERE panel = ? AND ts >= ?
            ORDER BY ts ASC
            """,
            (panel, since_ts),
        ).fetchall()
        return [dict(r) for r in rows]


def prune_older_than(cutoff_ts):
    with _connect() as conn:
        cur = conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff_ts,))
        return cur.rowcount


def save_status_push(source, received_at, body):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO status_push (source, received_at, body_json) VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET received_at=excluded.received_at, body_json=excluded.body_json
            """,
            (source, int(received_at), json.dumps(body, ensure_ascii=False)),
        )


def get_status_push(source):
    """返回 (received_at, body)，还没收到过就返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT received_at, body_json FROM status_push WHERE source = ?", (source,)
        ).fetchone()
    if row is None:
        return None
    return row["received_at"], json.loads(row["body_json"])
