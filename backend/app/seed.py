import hashlib
import json
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  region TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_snapshots (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  store_id TEXT NOT NULL REFERENCES stores(id),
  as_of_ts TEXT NOT NULL,
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL DEFAULT '1111100',
  biz_status TEXT NOT NULL DEFAULT 'open',
  payload_json TEXT NOT NULL DEFAULT '{}',
  row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detect_runs (
  id TEXT PRIMARY KEY,
  batch_id TEXT,
  created_at TEXT NOT NULL,
  changed_count INTEGER NOT NULL DEFAULT 0,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dim_store_hours_hist (
  id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES stores(id),
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL,
  biz_status TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  is_current INTEGER NOT NULL DEFAULT 1,
  source_snapshot_id TEXT,
  row_hash TEXT NOT NULL,
  closed_by_run TEXT
);

CREATE TABLE IF NOT EXISTS dim_store_hours_current (
  store_id TEXT PRIMARY KEY REFERENCES stores(id),
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL,
  biz_status TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  as_of_ts TEXT NOT NULL,
  row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scd_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  mirror_hist_id TEXT NOT NULL,
  row_hash TEXT NOT NULL,
  valid INTEGER NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- 草稿切换：一次应用 = 一个切换跑次；拟关旧 / 拟开新先落草稿区。
CREATE TABLE IF NOT EXISTS switch_runs (
  id TEXT PRIMARY KEY,
  detect_run_id TEXT NOT NULL,
  architecture TEXT NOT NULL DEFAULT 'draft_retained_on_failure',
  status TEXT NOT NULL DEFAULT 'draft',
  draft_count INTEGER NOT NULL DEFAULT 0,
  committed_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  report_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  committed_at TEXT
);

CREATE TABLE IF NOT EXISTS switch_drafts (
  id TEXT PRIMARY KEY,
  switch_run_id TEXT NOT NULL,
  detect_run_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  kind TEXT NOT NULL DEFAULT 'attr_change',
  prev_hist_id TEXT,
  prev_row_hash TEXT,
  prev_valid_from TEXT,
  proposed_valid_to TEXT,
  new_open_time TEXT,
  new_close_time TEXT,
  new_weekday_mask TEXT NOT NULL,
  new_biz_status TEXT NOT NULL,
  new_valid_from TEXT NOT NULL,
  new_valid_to TEXT,
  new_row_hash TEXT NOT NULL,
  source_snapshot_id TEXT NOT NULL,
  collision_json TEXT NOT NULL DEFAULT '{}',
  fail_reason TEXT,
  opened_hist_id TEXT,
  created_at TEXT NOT NULL,
  committed_at TEXT
);

CREATE TABLE IF NOT EXISTS switch_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  switch_run_id TEXT,
  draft_id TEXT,
  store_id TEXT,
  event TEXT NOT NULL,
  reason TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hist_store_current
  ON dim_store_hours_hist(store_id, is_current);
CREATE INDEX IF NOT EXISTS idx_snap_store_ts
  ON source_snapshots(store_id, as_of_ts);
CREATE INDEX IF NOT EXISTS idx_switch_draft_store_status
  ON switch_drafts(store_id, status);
CREATE INDEX IF NOT EXISTS idx_switch_draft_run
  ON switch_drafts(switch_run_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def row_hash(open_time, close_time, weekday_mask, biz_status) -> str:
    raw = f"{open_time or ''}|{close_time or ''}|{weekday_mask}|{biz_status}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM settings WHERE key='as_of_mode'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('as_of_mode', 'business_ts')")
    if conn.execute("SELECT COUNT(*) AS c FROM stores").fetchone()["c"] == 0:
        _seed(conn)
    conn.commit()
    conn.close()


def _seed(conn):
    path = FIXTURES_DIR / "seed_stores.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    batch_id = "SEED-BATCH"
    created = now_iso()
    conn.execute(
        "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
        (batch_id, created, json.dumps({"seed": True}, ensure_ascii=False)),
    )
    for s in data.get("stores", []):
        conn.execute(
            "INSERT INTO stores(id, name, region) VALUES (?,?,?)",
            (s["id"], s["name"], s["region"]),
        )
        ot, ct = s.get("open_time"), s.get("close_time")
        wm = s.get("weekday_mask", "1111100")
        st = s.get("biz_status", "open")
        rh = row_hash(ot, ct, wm, st)
        snap_id = f"SNAP-SEED-{s['id']}"
        as_of = s.get("as_of_ts", "2026-11-01T00:00:00Z")
        conn.execute(
            """INSERT INTO source_snapshots(
              id, batch_id, store_id, as_of_ts, open_time, close_time,
              weekday_mask, biz_status, payload_json, row_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                snap_id,
                batch_id,
                s["id"],
                as_of,
                ot,
                ct,
                wm,
                st,
                json.dumps(s.get("payload", {}), ensure_ascii=False),
                rh,
            ),
        )
        hist_id = f"HIST-SEED-{s['id']}"
        conn.execute(
            """INSERT INTO dim_store_hours_hist(
              id, store_id, open_time, close_time, weekday_mask, biz_status,
              valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hist_id, s["id"], ot, ct, wm, st, as_of, None, 1, snap_id, rh, None),
        )
        conn.execute(
            """INSERT INTO dim_store_hours_current(
              store_id, open_time, close_time, weekday_mask, biz_status,
              hist_id, as_of_ts, row_hash
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (s["id"], ot, ct, wm, st, hist_id, as_of, rh),
        )
        conn.execute(
            """INSERT INTO scd_proofs(
              store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                s["id"],
                hist_id,
                hist_id,
                rh,
                1,
                json.dumps({"seed": True}, ensure_ascii=False),
                created,
            ),
        )
