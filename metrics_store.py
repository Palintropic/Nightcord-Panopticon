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
CREATE TABLE IF NOT EXISTS agent_last_seen (
  panel TEXT PRIMARY KEY,
  seen_at INTEGER NOT NULL
);

-- 在设置页"移除"掉的机器。它之后只要再上报一次，记录就会在同一个事务里被删掉，自动回到名单里。
CREATE TABLE IF NOT EXISTS agent_retired (
  panel TEXT PRIMARY KEY,
  retired_at INTEGER NOT NULL
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)
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


def insert_sample(panel, ts, cpu, mem, disk, net_in_kbps, net_out_kbps):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO metrics (panel, ts, cpu, mem, disk, net_in_kbps, net_out_kbps)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(panel, ts) DO UPDATE SET
              cpu=excluded.cpu, mem=excluded.mem, disk=excluded.disk,
              net_in_kbps=excluded.net_in_kbps, net_out_kbps=excluded.net_out_kbps
            """,
            (panel, ts, cpu, mem, disk, net_in_kbps, net_out_kbps),
        )


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
    返回 [{"panel", "seen_at"}, ...]，按名字排序。离线判定由调用方拿 seen_at 跟当前时间比。
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.panel, s.seen_at FROM agent_last_seen s
            LEFT JOIN agent_retired r ON r.panel = s.panel
            WHERE r.panel IS NULL
            ORDER BY s.panel
            """
        ).fetchall()
        return [dict(r) for r in rows]


def all_watched(panels):
    """这些机器是否都还在监控名单里（上报过、且当前没有移除记录）。发通知前的现查用。"""
    panels = set(panels)
    if not panels:
        return True
    with _connect() as conn:
        placeholders = ",".join("?" * len(panels))
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM agent_last_seen s
            LEFT JOIN agent_retired r ON r.panel = s.panel
            WHERE r.panel IS NULL AND s.panel IN ({placeholders})
            """,
            tuple(panels),
        ).fetchone()
        return row[0] == len(panels)


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
            """
            SELECT ts, cpu, mem, disk, net_in_kbps, net_out_kbps
            FROM metrics WHERE panel = ? ORDER BY ts DESC LIMIT 1
            """,
            (panel,),
        ).fetchone()
        return dict(row) if row else None


def query_history(panel, since_ts):
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT ts, cpu, mem, disk, net_in_kbps, net_out_kbps
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
