"""草稿切换：先落草稿区，与现行 is_current / 区间规则对撞，通过后原子切正式。

落库架构（全局唯一，站内只贯彻这一种）:
    甲 · 失败草稿留痕为准
      - 对撞失败时正式态（hist / current）完全不动；
      - 草稿行保留，status=failed 且 fail_reason 可读；
      - 核证只读接口直接返回失败草稿，供人核对；
      - 另写 switch_events 作为旁路审计（不替代草稿留痕）。

硬约束:
    - 拟关旧 / 拟开新必须先入草稿区；禁止无对撞直接改正式；
    - 对撞失败后正式态原样，且不得出现双 is_current / 半截切换（镜已新、旧段未关）；
    - 草稿按 store_id 隔离，邻键草稿不得串切本键；
    - 存在未提交草稿（draft / validated）时，后到应用不得并行切正式；
    - 提交在单个 BEGIN IMMEDIATE 事务内完成：关旧 + 开新 + 换镜要么全成、要么全不成。
"""

import json
import re
from uuid import uuid4

from app.db import connect
from app.seed import now_iso, row_hash

ARCHITECTURE = "draft_retained_on_failure"

PENDING_STATUSES = ("draft", "validated")

_OPEN_STATUSES = ("open", "suspended", "closed")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MASK_RE = re.compile(r"^[01]{7}$")


class SwitchError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# 只读工具
# ---------------------------------------------------------------------------

def _connect():
    conn = connect()
    conn.isolation_level = None  # 手动控制 BEGIN IMMEDIATE / COMMIT / ROLLBACK
    return conn


def _row_dict(row):
    return dict(row) if row is not None else None


def _validate_formal(conn, store_id: str) -> dict:
    """现行正式态自检：is_current、区间连续、镜与当前行互证。

    任何一条不满足都算“对撞前正式态已坏”，应用必须拒绝且不得动正式。
    """
    rows = conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
        (store_id,),
    ).fetchall()
    issues = []

    for r in rows:
        if r["valid_to"] is not None and r["valid_to"] <= r["valid_from"]:
            issues.append(
                {
                    "code": "non_positive_interval",
                    "hist_id": r["id"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                }
            )

    currents = [r for r in rows if r["is_current"] == 1]
    if len(currents) > 1:
        issues.append(
            {
                "code": "multiple_current",
                "count": len(currents),
                "hist_ids": [r["id"] for r in currents],
            }
        )
    elif len(currents) == 0 and rows:
        issues.append({"code": "no_current", "count": len(rows)})

    open_rows = [r for r in rows if r["valid_to"] is None]
    if len(open_rows) > 1:
        issues.append(
            {
                "code": "multiple_open_ended",
                "count": len(open_rows),
                "hist_ids": [r["id"] for r in open_rows],
            }
        )
    elif len(open_rows) == 0 and rows:
        issues.append({"code": "no_open_ended"})

    for prev, nxt in zip(rows, rows[1:]):
        if prev["valid_to"] != nxt["valid_from"]:
            issues.append(
                {
                    "code": "gap_or_overlap",
                    "prev_hist_id": prev["id"],
                    "next_hist_id": nxt["id"],
                    "prev_valid_to": prev["valid_to"],
                    "next_valid_from": nxt["valid_from"],
                }
            )

    if rows:
        if rows[-1]["valid_to"] is not None:
            issues.append({"code": "last_row_closed", "hist_id": rows[-1]["id"]})
        for r in rows[:-1]:
            if r["valid_to"] is None:
                issues.append({"code": "early_open_ended", "hist_id": r["id"]})
        if len(currents) == 1 and rows[-1]["id"] != currents[0]["id"]:
            issues.append(
                {
                    "code": "current_not_latest",
                    "current_hist_id": currents[0]["id"],
                    "latest_hist_id": rows[-1]["id"],
                }
            )

    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
    ).fetchone()
    if mirror is None:
        if rows:
            issues.append({"code": "mirror_missing"})
    else:
        mh = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=?", (mirror["hist_id"],)
        ).fetchone()
        if mh is None:
            issues.append({"code": "mirror_hist_missing", "hist_id": mirror["hist_id"]})
        else:
            if mh["store_id"] != store_id:
                issues.append(
                    {"code": "mirror_store_mismatch", "hist_id": mh["id"]}
                )
            if mh["is_current"] != 1:
                issues.append(
                    {"code": "mirror_not_current", "hist_id": mh["id"]}
                )
            if mh["row_hash"] != mirror["row_hash"]:
                issues.append(
                    {
                        "code": "mirror_hash_mismatch",
                        "hist_hash": mh["row_hash"],
                        "mirror_hash": mirror["row_hash"],
                    }
                )
            recomputed = row_hash(
                mh["open_time"], mh["close_time"], mh["weekday_mask"], mh["biz_status"]
            )
            if recomputed != mh["row_hash"]:
                issues.append(
                    {
                        "code": "hist_hash_mismatch",
                        "hist_id": mh["id"],
                        "stored": mh["row_hash"],
                        "recomputed": recomputed,
                    }
                )
            if (
                mh["open_time"] != mirror["open_time"]
                or mh["close_time"] != mirror["close_time"]
                or mh["weekday_mask"] != mirror["weekday_mask"]
                or mh["biz_status"] != mirror["biz_status"]
            ):
                issues.append(
                    {"code": "mirror_attrs_mismatch", "hist_id": mh["id"]}
                )

    return {
        "store_id": store_id,
        "valid": not issues,
        "issues": issues,
        "current_hist_id": currents[0]["id"] if len(currents) == 1 else None,
        "hist_rows": [_row_dict(r) for r in rows],
        "mirror": _row_dict(mirror),
    }


def _formal_snapshot(conn, store_id: str) -> dict:
    """正式态只读快照，用于事后核对“失败后原样”。"""
    hist = conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
        (store_id,),
    ).fetchall()
    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
    ).fetchone()
    return {
        "store_id": store_id,
        "hist": [_row_dict(r) for r in hist],
        "mirror": _row_dict(mirror),
    }


def _pending_drafts(conn, store_id: str | None = None, exclude_run: str | None = None):
    placeholders = ",".join("?" for _ in PENDING_STATUSES)
    sql = f"SELECT * FROM switch_drafts WHERE status IN ({placeholders})"
    params = list(PENDING_STATUSES)
    if store_id:
        sql += " AND store_id=?"
        params.append(store_id)
    if exclude_run:
        sql += " AND switch_run_id<>?"
        params.append(exclude_run)
    sql += " ORDER BY created_at DESC"
    return conn.execute(sql, params).fetchall()


def _field_issues(snap) -> list[dict]:
    issues = []
    ot, ct = snap["open_time"], snap["close_time"]
    if ot is not None and not _TIME_RE.match(ot):
        issues.append({"code": "bad_open_time", "value": ot})
    if ct is not None and not _TIME_RE.match(ct):
        issues.append({"code": "bad_close_time", "value": ct})
    if ot and ct and _TIME_RE.match(ot) and _TIME_RE.match(ct) and ot >= ct:
        issues.append({"code": "time_order", "open_time": ot, "close_time": ct})
    if not _MASK_RE.match(snap["weekday_mask"] or ""):
        issues.append({"code": "bad_weekday_mask", "value": snap["weekday_mask"]})
    if snap["biz_status"] not in _OPEN_STATUSES:
        issues.append({"code": "bad_biz_status", "value": snap["biz_status"]})
    if not snap["as_of_ts"]:
        issues.append({"code": "missing_as_of"})
    return issues


def _collide_draft(conn, draft, formal_cache: dict, seen_stores: dict) -> list[dict]:
    """对一个草稿做全量对撞，返回碰撞原因列表（空=可提交）。"""
    store_id = draft["store_id"]
    reasons = []

    snap = conn.execute(
        "SELECT * FROM source_snapshots WHERE id=?", (draft["source_snapshot_id"],)
    ).fetchone()
    if snap is None:
        return [
            {
                "code": "snapshot_missing",
                "snapshot_id": draft["source_snapshot_id"],
            }
        ]
    if snap["store_id"] != store_id:
        reasons.append(
            {
                "code": "snapshot_store_mismatch",
                "snapshot_store": snap["store_id"],
                "draft_store": store_id,
            }
        )

    reasons.extend(_field_issues(snap))

    if store_id not in formal_cache:
        formal_cache[store_id] = _validate_formal(conn, store_id)
    formal = formal_cache[store_id]
    if not formal["valid"]:
        reasons.append(
            {
                "code": "formal_state_invalid",
                "issues": formal["issues"],
            }
        )

    if seen_stores.get(store_id, 0) > 1:
        reasons.append(
            {"code": "duplicate_drafts_in_run", "store_id": store_id}
        )

    pending = _pending_drafts(conn, exclude_run=draft["switch_run_id"])
    if pending:
        # 未提交草稿存在（含邻键）：后到应用不得并行切正式。
        reasons.append(
            {
                "code": "pending_draft_exists",
                "store_ids": sorted({r["store_id"] for r in pending}),
                "switch_run_ids": sorted({r["switch_run_id"] for r in pending}),
                "draft_ids": [r["id"] for r in pending],
            }
        )

    mirror = formal["mirror"]
    if mirror is not None and mirror["row_hash"] == draft["new_row_hash"]:
        reasons.append(
            {
                "code": "no_change",
                "row_hash": draft["new_row_hash"],
            }
        )

    if formal["valid"] and formal["current_hist_id"]:
        cur = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=? AND store_id=?",
            (formal["current_hist_id"], store_id),
        ).fetchone()
        if draft["prev_hist_id"] not in (None, cur["id"]):
            reasons.append(
                {
                    "code": "stale_prev_hist",
                    "draft_prev": draft["prev_hist_id"],
                    "actual_current": cur["id"],
                }
            )
        if draft["new_valid_from"] <= cur["valid_from"]:
            reasons.append(
                {
                    "code": "out_of_order",
                    "new_valid_from": draft["new_valid_from"],
                    "current_valid_from": cur["valid_from"],
                }
            )
        if draft["proposed_valid_to"] is None or draft["proposed_valid_to"] <= cur["valid_from"]:
            reasons.append(
                {
                    "code": "invalid_close_point",
                    "proposed_valid_to": draft["proposed_valid_to"],
                    "current_valid_from": cur["valid_from"],
                }
            )
    elif mirror is None:
        # 无现行行：仅允许开新，起始点不得与既有行重叠。
        existing = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND valid_from>=?",
            (store_id, draft["new_valid_from"]),
        ).fetchall()
        if existing:
            reasons.append(
                {
                    "code": "overlaps_existing_rows",
                    "hist_ids": [r["id"] for r in existing],
                }
            )

    if formal["valid"] and draft["prev_hist_id"] is not None:
        prev = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=? AND store_id=?",
            (draft["prev_hist_id"], store_id),
        ).fetchone()
        if prev is None:
            reasons.append(
                {"code": "prev_hist_missing", "prev_hist_id": draft["prev_hist_id"]}
            )
        elif prev["is_current"] != 1:
            reasons.append(
                {"code": "prev_not_current", "prev_hist_id": prev["id"]}
            )

    future = conn.execute(
        "SELECT id, valid_from FROM dim_store_hours_hist "
        "WHERE store_id=? AND valid_from>=? AND id<>?",
        (store_id, draft["new_valid_from"], draft["prev_hist_id"] or ""),
    ).fetchall()
    if future:
        reasons.append(
            {
                "code": "future_row_exists",
                "hist_ids": [r["id"] for r in future],
            }
        )

    return reasons


# ---------------------------------------------------------------------------
# 草稿区
# ---------------------------------------------------------------------------

def _insert_draft(conn, switch_run_id, detect_run_id, store_id, change, snap, now, cur_hist):
    draft_id = f"DRF-{uuid4().hex[:10]}"
    conn.execute(
        """INSERT INTO switch_drafts(
             id, switch_run_id, detect_run_id, store_id, status, kind,
             prev_hist_id, prev_row_hash, prev_valid_from, proposed_valid_to,
             new_open_time, new_close_time, new_weekday_mask, new_biz_status,
             new_valid_from, new_valid_to, new_row_hash, source_snapshot_id,
             collision_json, fail_reason, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            draft_id,
            switch_run_id,
            detect_run_id,
            store_id,
            "draft",
            change.get("kind", "attr_change"),
            cur_hist["id"] if cur_hist is not None else None,
            cur_hist["row_hash"] if cur_hist is not None else None,
            cur_hist["valid_from"] if cur_hist is not None else None,
            snap["as_of_ts"],
            snap["open_time"],
            snap["close_time"],
            snap["weekday_mask"],
            snap["biz_status"],
            snap["as_of_ts"],
            None,
            snap["row_hash"],
            snap["id"],
            "{}",
            None,
            now,
        ),
    )
    return draft_id


def _set_draft_failure(conn, draft_id, reasons):
    payload = json.dumps(reasons, ensure_ascii=False)
    first = reasons[0]["code"] if reasons else "unknown"
    conn.execute(
        "UPDATE switch_drafts SET status='failed', collision_json=?, fail_reason=? WHERE id=?",
        (payload, first, draft_id),
    )


def _event(conn, switch_run_id, draft_id, store_id, event, reason=None, detail=None, now=None):
    conn.execute(
        """INSERT INTO switch_events(switch_run_id, draft_id, store_id, event, reason, detail_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            switch_run_id,
            draft_id,
            store_id,
            event,
            reason,
            json.dumps(detail or {}, ensure_ascii=False),
            now or now_iso(),
        ),
    )


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------

def apply_switch(detect_run_id: str, prepare_only: bool = False) -> dict:
    """一次应用 = 一个切换跑次：草稿 → 对撞 → 原子切正式。"""
    conn = _connect()
    now = now_iso()
    switch_run_id = f"SW-{uuid4().hex[:8]}"
    run = None
    draft_dicts = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT * FROM detect_runs WHERE id=?", (detect_run_id,)
        ).fetchone()
        if run is None:
            conn.execute("ROLLBACK")
            return {
                "ok": False,
                "architecture": ARCHITECTURE,
                "message": "检测跑次不存在",
                "detect_run_id": detect_run_id,
            }

        report = json.loads(run["report_json"])
        changes = report.get("changes", [])
        if not changes:
            conn.execute("ROLLBACK")
            return {
                "ok": False,
                "architecture": ARCHITECTURE,
                "message": "检测跑次无变更，无需切换",
                "detect_run_id": detect_run_id,
            }

        already = conn.execute(
            "SELECT id FROM switch_runs WHERE detect_run_id=? AND status='committed'",
            (detect_run_id,),
        ).fetchone()
        if already is not None:
            conn.execute("ROLLBACK")
            return {
                "ok": False,
                "architecture": ARCHITECTURE,
                "message": "该检测跑次已成功切换，拒绝重复应用",
                "detect_run_id": detect_run_id,
                "existing_switch_run_id": already["id"],
            }

        conn.execute(
            """INSERT INTO switch_runs(
                 id, detect_run_id, architecture, status, draft_count,
                 committed_count, failed_count, report_json, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                switch_run_id,
                detect_run_id,
                ARCHITECTURE,
                "draft",
                len(changes),
                0,
                0,
                "{}",
                now,
            ),
        )

        draft_ids = []
        draft_dicts = []
        seen_stores = {}
        formal_before = {}
        for change in changes:
            store_id = change["store_id"]
            seen_stores[store_id] = seen_stores.get(store_id, 0) + 1
            if store_id not in formal_before:
                formal_before[store_id] = _formal_snapshot(conn, store_id)
            snap = conn.execute(
                "SELECT * FROM source_snapshots WHERE id=?",
                (change.get("snapshot_id"),),
            ).fetchone()
            if snap is None:
                draft_id = f"DRF-{uuid4().hex[:10]}"
                conn.execute(
                    """INSERT INTO switch_drafts(
                         id, switch_run_id, detect_run_id, store_id, status, kind,
                         new_weekday_mask, new_biz_status, new_valid_from,
                         new_row_hash, source_snapshot_id, collision_json,
                         fail_reason, created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        draft_id,
                        switch_run_id,
                        detect_run_id,
                        store_id,
                        "draft",
                        change.get("kind", "attr_change"),
                        "",
                        "",
                        now,
                        change.get("to_hash", ""),
                        change.get("snapshot_id") or "",
                        "{}",
                        None,
                        now,
                    ),
                )
                draft_ids.append((draft_id, store_id, change, None))
                draft_dicts.append(
                    {
                        "id": draft_id,
                        "store_id": store_id,
                        "kind": change.get("kind", "attr_change"),
                        "new_row_hash": change.get("to_hash", ""),
                        "source_snapshot_id": change.get("snapshot_id") or "",
                        "new_valid_from": now,
                        "created_at": now,
                    }
                )
                continue
            cur_hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
                (store_id,),
            ).fetchone()
            draft_id = _insert_draft(
                conn, switch_run_id, detect_run_id, store_id, change, snap, now, cur_hist
            )
            draft_ids.append((draft_id, store_id, change, snap))
            draft_dicts.append(
                {
                    "id": draft_id,
                    "store_id": store_id,
                    "kind": change.get("kind", "attr_change"),
                    "prev_hist_id": cur_hist["id"] if cur_hist is not None else None,
                    "prev_row_hash": cur_hist["row_hash"] if cur_hist is not None else None,
                    "prev_valid_from": cur_hist["valid_from"] if cur_hist is not None else None,
                    "proposed_valid_to": snap["as_of_ts"],
                    "new_open_time": snap["open_time"],
                    "new_close_time": snap["close_time"],
                    "new_weekday_mask": snap["weekday_mask"],
                    "new_biz_status": snap["biz_status"],
                    "new_valid_from": snap["as_of_ts"],
                    "new_row_hash": snap["row_hash"],
                    "source_snapshot_id": snap["id"],
                    "created_at": now,
                }
            )

        # 对撞（草稿已在事务内落库，失败也会先 commit 留痕）
        formal_cache = {}
        results = []
        any_failed = False
        for draft_id, store_id, change, snap in draft_ids:
            row = conn.execute(
                "SELECT * FROM switch_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if snap is None:
                reasons = [
                    {
                        "code": "snapshot_missing",
                        "snapshot_id": change.get("snapshot_id"),
                    }
                ]
            else:
                reasons = _collide_draft(conn, row, formal_cache, seen_stores)
            if reasons:
                any_failed = True
                _set_draft_failure(conn, draft_id, reasons)
                _event(
                    conn,
                    switch_run_id,
                    draft_id,
                    store_id,
                    "collision_failed",
                    reasons[0]["code"],
                    {"reasons": reasons},
                    now,
                )
            else:
                conn.execute(
                    "UPDATE switch_drafts SET status='validated' WHERE id=?",
                    (draft_id,),
                )
            results.append(
                {
                    "draft_id": draft_id,
                    "store_id": store_id,
                    "status": "failed" if reasons else "validated",
                    "reasons": reasons,
                }
            )

        if any_failed:
            # 甲：正式态完全不动，草稿留痕（failed + 原因）。
            untouched = {}
            for sid, before in formal_before.items():
                after = _formal_snapshot(conn, sid)
                untouched[sid] = before == after
            # 甲：整票不提交时，其余已过对撞的草稿也必须在草稿区留 failed + 原因，
            # 否则核证页会看到“无原因悬空草稿”。
            waiting = conn.execute(
                "SELECT id, store_id FROM switch_drafts "
                "WHERE switch_run_id=? AND status='validated'",
                (switch_run_id,),
            ).fetchall()
            for w in waiting:
                sibling = [
                    {
                        "code": "sibling_collision_failed",
                        "message": "同批另有草稿对撞失败，整票不切正式",
                    }
                ]
                _set_draft_failure(conn, w["id"], sibling)
                _event(
                    conn,
                    switch_run_id,
                    w["id"],
                    w["store_id"],
                    "draft_failed_run_aborted",
                    "sibling_collision_failed",
                    {},
                    now,
                )
            for item in results:
                if item["status"] == "validated":
                    item["status"] = "failed"
                    item["reasons"] = [
                        {
                            "code": "sibling_collision_failed",
                            "message": "同批另有草稿对撞失败，整票不切正式",
                        }
                    ]
            if not all(untouched.values()):
                # 对撞阶段本不应触碰正式；若真发生，整票回滚（不提交任何草稿）。
                conn.execute("ROLLBACK")
                return {
                    "ok": False,
                    "architecture": ARCHITECTURE,
                    "switch_run_id": switch_run_id,
                    "detect_run_id": detect_run_id,
                    "status": "failed",
                    "message": "对撞阶段意外改动正式态，已整票回滚",
                    "formal_untouched": False,
                }
            conn.execute(
                "UPDATE switch_runs SET status='failed', failed_count=?, report_json=? WHERE id=?",
                (
                    sum(1 for r in results if r["status"] == "failed"),
                    json.dumps(
                        {"results": results, "formal_untouched": untouched},
                        ensure_ascii=False,
                    ),
                    switch_run_id,
                ),
            )
            _event(
                conn,
                switch_run_id,
                None,
                None,
                "run_failed",
                "collision_failed",
                {"formal_untouched": untouched},
                now,
            )
            conn.execute("COMMIT")
            return {
                "ok": False,
                "architecture": ARCHITECTURE,
                "switch_run_id": switch_run_id,
                "detect_run_id": detect_run_id,
                "status": "failed",
                "message": "对撞失败：正式态未改动，失败草稿已留痕",
                "drafts": results,
                "formal_untouched": True,
                "formal_untouched_by_store": untouched,
            }

        if prepare_only:
            conn.execute(
                "UPDATE switch_runs SET status='prepared', report_json=? WHERE id=?",
                (json.dumps({"results": results}, ensure_ascii=False), switch_run_id),
            )
            _event(
                conn,
                switch_run_id,
                None,
                None,
                "run_prepared",
                None,
                {"draft_count": len(results)},
                now,
            )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "architecture": ARCHITECTURE,
                "switch_run_id": switch_run_id,
                "detect_run_id": detect_run_id,
                "status": "prepared",
                "message": "草稿已就绪，尚未切正式（存在未提交草稿，后到应用将被拒绝）",
                "drafts": results,
            }

        # 原子切正式：关旧 + 开新 + 换镜 + 草稿置 committed，同一事务。
        drafts = [
            conn.execute("SELECT * FROM switch_drafts WHERE id=?", (draft_id,)).fetchone()
            for draft_id, _store_id, _change, _snap in draft_ids
        ]
        committed = _commit_drafts(conn, switch_run_id, drafts, now)
        for item in results:
            item["status"] = "committed"

        conn.execute(
            """UPDATE switch_runs
               SET status='committed', committed_count=?, committed_at=?, report_json=?
               WHERE id=?""",
            (
                len(committed),
                now,
                json.dumps(
                    {"results": results, "committed": committed}, ensure_ascii=False
                ),
                switch_run_id,
            ),
        )
        _event(
            conn,
            switch_run_id,
            None,
            None,
            "run_committed",
            None,
            {"committed": committed},
            now,
        )
        conn.execute("COMMIT")
        return {
            "ok": True,
            "architecture": ARCHITECTURE,
            "switch_run_id": switch_run_id,
            "detect_run_id": detect_run_id,
            "status": "committed",
            "message": "对撞通过，已原子切正式",
            "committed": committed,
            "drafts": results,
        }
    except SwitchError as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        _record_rollback_failure(
            switch_run_id,
            detect_run_id,
            draft_dicts,
            "commit_failed_rolled_back",
            exc.message,
            now,
        )
        return {
            "ok": False,
            "architecture": ARCHITECTURE,
            "switch_run_id": switch_run_id,
            "detect_run_id": detect_run_id,
            "status": "failed",
            "message": exc.message,
            "formal_untouched": True,
            "rolled_back": True,
        }
    except Exception as exc:  # noqa: BLE001 - 任何异常都不得留下半截正式
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        _record_rollback_failure(
            switch_run_id,
            detect_run_id,
            draft_dicts,
            "commit_failed_rolled_back",
            f"切换异常，已整票回滚：{exc}",
            now,
        )
        return {
            "ok": False,
            "architecture": ARCHITECTURE,
            "switch_run_id": switch_run_id,
            "detect_run_id": detect_run_id,
            "status": "failed",
            "message": f"切换异常，已整票回滚：{exc}",
            "formal_untouched": True,
            "rolled_back": True,
        }
    finally:
        conn.close()


def commit_prepared(switch_run_id: str) -> dict:
    """提交一个 prepared 切换跑次：再次对撞后原子切正式。

    草稿未提交期间（prepared），后到应用一律被 pending_draft_exists 拒绝；
    本函数是它唯一的提交出口，事务内重做对撞，避免草稿落库后正式态已变。
    """
    conn = _connect()
    now = now_iso()
    run = None
    draft_dicts = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT * FROM switch_runs WHERE id=?", (switch_run_id,)
        ).fetchone()
        if run is None:
            conn.execute("ROLLBACK")
            return {"ok": False, "message": "切换跑次不存在"}
        if run["status"] == "committed":
            conn.execute("ROLLBACK")
            return {"ok": False, "message": "已提交，拒绝重复提交"}
        if run["status"] != "prepared":
            conn.execute("ROLLBACK")
            return {
                "ok": False,
                "message": f"跑次状态 {run['status']} 不可提交（仅 prepared 可提交）",
            }

        drafts = conn.execute(
            "SELECT * FROM switch_drafts WHERE switch_run_id=? AND status='validated' "
            "ORDER BY store_id, created_at",
            (switch_run_id,),
        ).fetchall()
        if not drafts:
            conn.execute("ROLLBACK")
            return {"ok": False, "message": "无可提交草稿"}
        draft_dicts = [_row_dict(d) for d in drafts]

        seen_stores = {}
        for d in drafts:
            seen_stores[d["store_id"]] = seen_stores.get(d["store_id"], 0) + 1
        formal_cache = {}
        results = []
        for draft in drafts:
            reasons = _collide_draft(conn, draft, formal_cache, seen_stores)
            results.append(
                {
                    "draft_id": draft["id"],
                    "store_id": draft["store_id"],
                    "status": "failed" if reasons else "validated",
                    "reasons": reasons,
                }
            )
            if reasons:
                _set_draft_failure(conn, draft["id"], reasons)
                _event(
                    conn,
                    switch_run_id,
                    draft["id"],
                    draft["store_id"],
                    "collision_failed_on_commit",
                    reasons[0]["code"],
                    {"reasons": reasons},
                    now,
                )

        if any(r["status"] == "failed" for r in results):
            # 甲：正式不动，失败草稿留痕，其余未提交草稿同样留 failed 原因。
            waiting = conn.execute(
                "SELECT id, store_id FROM switch_drafts "
                "WHERE switch_run_id=? AND status='validated'",
                (switch_run_id,),
            ).fetchall()
            for w in waiting:
                sibling = [
                    {
                        "code": "sibling_collision_failed",
                        "message": "同批另有草稿对撞失败，整票不切正式",
                    }
                ]
                _set_draft_failure(conn, w["id"], sibling)
                _event(
                    conn,
                    switch_run_id,
                    w["id"],
                    w["store_id"],
                    "draft_failed_run_aborted",
                    "sibling_collision_failed",
                    {},
                    now,
                )
            for item in results:
                if item["status"] == "validated":
                    item["status"] = "failed"
                    item["reasons"] = [
                        {
                            "code": "sibling_collision_failed",
                            "message": "同批另有草稿对撞失败，整票不切正式",
                        }
                    ]
            conn.execute(
                "UPDATE switch_runs SET status='failed', failed_count=?, report_json=? WHERE id=?",
                (
                    sum(1 for r in results if r["status"] == "failed"),
                    json.dumps({"results": results}, ensure_ascii=False),
                    switch_run_id,
                ),
            )
            _event(
                conn,
                switch_run_id,
                None,
                None,
                "run_failed",
                "collision_failed_on_commit",
                {"formal_untouched": True},
                now,
            )
            conn.execute("COMMIT")
            return {
                "ok": False,
                "architecture": ARCHITECTURE,
                "switch_run_id": switch_run_id,
                "status": "failed",
                "message": "提交前重对撞失败：正式态未改动，失败草稿已留痕",
                "drafts": results,
                "formal_untouched": True,
            }

        committed = _commit_drafts(conn, switch_run_id, drafts, now)
        conn.execute(
            """UPDATE switch_runs
               SET status='committed', committed_count=?, committed_at=?, report_json=?
               WHERE id=?""",
            (
                len(committed),
                now,
                json.dumps({"results": results, "committed": committed}, ensure_ascii=False),
                switch_run_id,
            ),
        )
        _event(
            conn, switch_run_id, None, None, "run_committed", None,
            {"committed": committed}, now,
        )
        conn.execute("COMMIT")
        return {
            "ok": True,
            "architecture": ARCHITECTURE,
            "switch_run_id": switch_run_id,
            "detect_run_id": run["detect_run_id"],
            "status": "committed",
            "message": "对撞通过，已原子切正式",
            "committed": committed,
        }
    except SwitchError as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        _record_rollback_failure(
            switch_run_id,
            run["detect_run_id"] if run is not None else "",
            draft_dicts,
            "commit_failed_rolled_back",
            exc.message,
            now,
        )
        return {
            "ok": False,
            "architecture": ARCHITECTURE,
            "switch_run_id": switch_run_id,
            "status": "failed",
            "message": exc.message,
            "formal_untouched": True,
            "rolled_back": True,
        }
    except Exception as exc:  # noqa: BLE001
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        _record_rollback_failure(
            switch_run_id,
            run["detect_run_id"] if run is not None else "",
            draft_dicts,
            "commit_failed_rolled_back",
            f"提交异常，已整票回滚：{exc}",
            now,
        )
        return {
            "ok": False,
            "architecture": ARCHITECTURE,
            "switch_run_id": switch_run_id,
            "status": "failed",
            "message": f"提交异常，已整票回滚：{exc}",
            "formal_untouched": True,
            "rolled_back": True,
        }
    finally:
        conn.close()


def _record_rollback_failure(
    switch_run_id, detect_run_id, draft_dicts, reason_code, message, now
):
    """整票回滚后另起事务登记失败留痕（甲口径：失败必须可查）。

    apply 路径的草稿与跑次和正式写入同处一个事务，回滚会一并抹掉；
    这里在新事务里重建 failed 草稿与跑次，保证“失败后无残留草稿”不会发生。
    prepared 路径的草稿已在库中持久化，这里只改状态。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT id FROM switch_runs WHERE id=?", (switch_run_id,)
        ).fetchone()
        if run is None:
            conn.execute(
                """INSERT INTO switch_runs(
                     id, detect_run_id, architecture, status, draft_count,
                     committed_count, failed_count, report_json, created_at, committed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    switch_run_id,
                    detect_run_id,
                    ARCHITECTURE,
                    "failed",
                    len(draft_dicts),
                    0,
                    len(draft_dicts),
                    json.dumps({"rollback": message}, ensure_ascii=False),
                    now,
                    None,
                ),
            )
        else:
            conn.execute(
                "UPDATE switch_runs SET status='failed', failed_count=?, report_json=? WHERE id=?",
                (
                    len(draft_dicts),
                    json.dumps({"rollback": message}, ensure_ascii=False),
                    switch_run_id,
                ),
            )

        failure = [{"code": reason_code, "message": message}]
        for d in draft_dicts:
            existing = conn.execute(
                "SELECT id FROM switch_drafts WHERE id=?", (d["id"],)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO switch_drafts(
                         id, switch_run_id, detect_run_id, store_id, status, kind,
                         prev_hist_id, prev_row_hash, prev_valid_from, proposed_valid_to,
                         new_open_time, new_close_time, new_weekday_mask, new_biz_status,
                         new_valid_from, new_valid_to, new_row_hash, source_snapshot_id,
                         collision_json, fail_reason, opened_hist_id, created_at, committed_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        d["id"],
                        switch_run_id,
                        detect_run_id,
                        d["store_id"],
                        "failed",
                        d.get("kind") or "attr_change",
                        d.get("prev_hist_id"),
                        d.get("prev_row_hash"),
                        d.get("prev_valid_from"),
                        d.get("proposed_valid_to"),
                        d.get("new_open_time"),
                        d.get("new_close_time"),
                        d.get("new_weekday_mask") or "",
                        d.get("new_biz_status") or "",
                        d.get("new_valid_from") or now,
                        d.get("new_valid_to"),
                        d.get("new_row_hash") or "",
                        d.get("source_snapshot_id") or "",
                        json.dumps(failure, ensure_ascii=False),
                        reason_code,
                        None,
                        d.get("created_at") or now,
                        None,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE switch_drafts SET status='failed', collision_json=?, fail_reason=? WHERE id=?",
                    (json.dumps(failure, ensure_ascii=False), reason_code, d["id"]),
                )
            _event(
                conn,
                switch_run_id,
                d["id"],
                d["store_id"],
                "rollback_failed",
                reason_code,
                {"message": message},
                now,
            )
        _event(
            conn,
            switch_run_id,
            None,
            None,
            "run_failed",
            reason_code,
            {"formal_untouched": True, "rolled_back": True},
            now,
        )
        conn.execute("COMMIT")
    except Exception:  # noqa: BLE001 - 留痕失败不得掩盖原始失败
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def _commit_drafts(conn, switch_run_id, drafts, now):
    """原子切正式：关旧 + 开新 + 换镜 + 草稿置 committed（调用方负责事务）。"""
    committed = []
    for draft in drafts:
        store_id = draft["store_id"]
        prev_id = draft["prev_hist_id"]
        closed = None
        if prev_id:
            cur = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=? AND store_id=? AND is_current=1",
                (prev_id, store_id),
            ).fetchone()
            if cur is None:
                raise SwitchError(f"提交前校验失败：{store_id} 的当前行已变")
            updated = conn.execute(
                """UPDATE dim_store_hours_hist
                   SET valid_to=?, is_current=0, closed_by_run=?
                   WHERE id=? AND store_id=? AND is_current=1""",
                (draft["proposed_valid_to"], switch_run_id, prev_id, store_id),
            )
            if updated.rowcount != 1:
                raise SwitchError(f"关旧行失败：{store_id} / {prev_id}")
            closed = prev_id

        new_id = f"HIST-{uuid4().hex[:10]}"
        conn.execute(
            """INSERT INTO dim_store_hours_hist(
                 id, store_id, open_time, close_time, weekday_mask, biz_status,
                 valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id,
                store_id,
                draft["new_open_time"],
                draft["new_close_time"],
                draft["new_weekday_mask"],
                draft["new_biz_status"],
                draft["new_valid_from"],
                draft["new_valid_to"],
                1,
                draft["source_snapshot_id"],
                draft["new_row_hash"],
                None,
            ),
        )
        conn.execute(
            """INSERT INTO dim_store_hours_current(
                 store_id, open_time, close_time, weekday_mask, biz_status,
                 hist_id, as_of_ts, row_hash
               ) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(store_id) DO UPDATE SET
                 open_time=excluded.open_time,
                 close_time=excluded.close_time,
                 weekday_mask=excluded.weekday_mask,
                 biz_status=excluded.biz_status,
                 hist_id=excluded.hist_id,
                 as_of_ts=excluded.as_of_ts,
                 row_hash=excluded.row_hash""",
            (
                store_id,
                draft["new_open_time"],
                draft["new_close_time"],
                draft["new_weekday_mask"],
                draft["new_biz_status"],
                new_id,
                draft["new_valid_from"],
                draft["new_row_hash"],
            ),
        )
        conn.execute(
            """UPDATE switch_drafts
               SET status='committed', opened_hist_id=?, committed_at=?
               WHERE id=? AND store_id=? AND status IN ('validated','draft')""",
            (new_id, now, draft["id"], store_id),
        )
        if conn.execute("SELECT changes() AS c").fetchone()["c"] != 1:
            raise SwitchError(f"草稿提交状态异常：{draft['id']}")
        _event(
            conn,
            switch_run_id,
            draft["id"],
            store_id,
            "committed",
            None,
            {
                "closed_hist_id": closed,
                "opened_hist_id": new_id,
                "mirror_hist_id": new_id,
            },
            now,
        )
        committed.append(
            {
                "draft_id": draft["id"],
                "store_id": store_id,
                "closed_hist_id": closed,
                "opened_hist_id": new_id,
            }
        )

    # 提交后自检：任一门店不满足正式态规则 → 整票回滚（不会出现半截）。
    for item in committed:
        formal = _validate_formal(conn, item["store_id"])
        if not formal["valid"]:
            raise SwitchError(f"提交后自检失败：{item['store_id']} {formal['issues']}")
    return committed


def abandon_switch(switch_run_id: str) -> dict:
    """放弃一个未提交切换跑次：草稿置 abandoned，正式不动。"""
    conn = _connect()
    now = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT * FROM switch_runs WHERE id=?", (switch_run_id,)
        ).fetchone()
        if run is None:
            conn.execute("ROLLBACK")
            return {"ok": False, "message": "切换跑次不存在"}
        if run["status"] == "committed":
            conn.execute("ROLLBACK")
            return {"ok": False, "message": "已提切换跑次不可放弃"}
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        rows = conn.execute(
            f"SELECT id FROM switch_drafts WHERE switch_run_id=? AND status IN ({placeholders})",
            (switch_run_id, *PENDING_STATUSES),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE switch_drafts SET status='abandoned' WHERE id=?", (r["id"],)
            )
            _event(conn, switch_run_id, r["id"], None, "draft_abandoned", None, {}, now)
        conn.execute(
            "UPDATE switch_runs SET status='abandoned', report_json=? WHERE id=?",
            (
                json.dumps({"abandoned": [r["id"] for r in rows]}, ensure_ascii=False),
                switch_run_id,
            ),
        )
        _event(conn, switch_run_id, None, None, "run_abandoned", None, {}, now)
        conn.execute("COMMIT")
        return {"ok": True, "switch_run_id": switch_run_id, "abandoned": len(rows)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 只读核证
# ---------------------------------------------------------------------------

def switch_proof(run_id: str | None = None, store_id: str | None = None) -> dict:
    """草稿切换只读核证：run、草稿态、正式态、区间校验、valid。

    甲口径：失败草稿必须可见且带 fail_reason；正式态须与切换前原样（无半截/双当前）。
    """
    conn = connect()
    try:
        if run_id:
            runs = conn.execute(
                "SELECT * FROM switch_runs WHERE id=?", (run_id,)
            ).fetchall()
        else:
            runs = conn.execute(
                "SELECT * FROM switch_runs ORDER BY created_at DESC, id DESC LIMIT 20"
            ).fetchall()

        if run_id:
            drafts = conn.execute(
                "SELECT * FROM switch_drafts WHERE switch_run_id=? ORDER BY store_id, created_at",
                (run_id,),
            ).fetchall()
        elif store_id:
            drafts = conn.execute(
                "SELECT * FROM switch_drafts WHERE store_id=? ORDER BY created_at DESC LIMIT 200",
                (store_id,),
            ).fetchall()
        else:
            drafts = conn.execute(
                "SELECT * FROM switch_drafts ORDER BY created_at DESC, id DESC LIMIT 200"
            ).fetchall()

        if store_id:
            scope_stores = [store_id]
        elif run_id:
            scope_stores = sorted({d["store_id"] for d in drafts})
        elif drafts:
            scope_stores = sorted({d["store_id"] for d in drafts})
        else:
            scope_stores = [
                r["store_id"]
                for r in conn.execute(
                    "SELECT store_id FROM stores ORDER BY store_id"
                ).fetchall()
            ]

        formal = {sid: _validate_formal(conn, sid) for sid in scope_stores}

        events = conn.execute(
            "SELECT * FROM switch_events ORDER BY id DESC LIMIT 100"
            if not run_id
            else "SELECT * FROM switch_events WHERE switch_run_id=? ORDER BY id DESC",
            () if not run_id else (run_id,),
        ).fetchall()

        draft_dicts = []
        for d in drafts:
            item = _row_dict(d)
            item["collision"] = json.loads(d["collision_json"] or "{}")
            item["pending"] = d["status"] in PENDING_STATUSES
            draft_dicts.append(item)

        failed_drafts = [d for d in draft_dicts if d["status"] == "failed"]
        pending_drafts = [d for d in draft_dicts if d["pending"]]
        run_dicts = []
        for r in runs:
            item = _row_dict(r)
            item["report"] = json.loads(r["report_json"] or "{}")
            run_dicts.append(item)

        formal_valid = all(f["valid"] for f in formal.values())
        failed_reasons_ok = all(d.get("fail_reason") for d in failed_drafts)
        run_status_ok = True
        for r in run_dicts:
            if r["status"] == "failed" and r["failed_count"] <= 0:
                run_status_ok = False
            if r["status"] == "committed" and r["committed_count"] <= 0:
                run_status_ok = False

        # 甲口径特有：失败跑次不得留下未提交草稿，否则会无谓阻塞后到应用。
        failed_run_no_pending = all(
            d["status"] not in PENDING_STATUSES
            for d in draft_dicts
            if any(
                r["id"] == d["switch_run_id"] and r["status"] == "failed"
                for r in run_dicts
            )
        )

        return {
            "architecture": ARCHITECTURE,
            "architecture_note": "失败草稿留痕：对撞失败正式态不动，草稿保留 failed+原因，核证可读失败草稿",
            "architecture_contract": {
                "chosen": ARCHITECTURE,
                "chosen_rule": "对撞失败：正式态完全不动；草稿行保留 failed + fail_reason；核证只读接口返回失败草稿",
                "rejected": "rollback_clears_drafts",
                "rejected_rule": "对撞失败：连草稿一并回滚删除，失败原因只写入独立切换审计；核证不得看到残留草稿",
                "why": "失败必须可查且与草稿一一对应；正式态与草稿同事务提交/回滚，回滚路径另事务补记失败留痕",
            },
            "run": run_dicts[0] if run_id and run_dicts else None,
            "runs": run_dicts,
            "drafts": draft_dicts,
            "drafts_state": {
                "total": len(draft_dicts),
                "by_status": {
                    s: sum(1 for d in draft_dicts if d["status"] == s)
                    for s in sorted({d["status"] for d in draft_dicts})
                },
                "pending": len(pending_drafts),
                "failed": len(failed_drafts),
                "empty": len(draft_dicts) == 0,
            },
            "failed_drafts": failed_drafts,
            "pending_drafts": pending_drafts,
            "formal": formal,
            "interval_checks": [
                {
                    "store_id": sid,
                    "valid": f["valid"],
                    "issues": f["issues"],
                    "single_current": all(
                        i["code"] not in ("multiple_current", "no_current")
                        for i in f["issues"]
                    ),
                    "contiguous": all(
                        i["code"] not in ("gap_or_overlap",)
                        for i in f["issues"]
                    ),
                    "mirror_matches": all(
                        i["code"] not in ("mirror_not_current", "mirror_hash_mismatch", "mirror_attrs_mismatch")
                        for i in f["issues"]
                    ),
                }
                for sid, f in formal.items()
            ],
            "events": [_row_dict(e) for e in events],
            "checks": {
                "formal_untouched_by_failures": formal_valid,
                "failed_drafts_have_reason": failed_reasons_ok,
                "run_status_consistent": run_status_ok,
                "failed_runs_leave_no_pending_drafts": failed_run_no_pending,
            },
            "valid": (
                formal_valid
                and failed_reasons_ok
                and run_status_ok
                and failed_run_no_pending
            ),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 半截切换夹具（测试专用；不走应用提交链路）
# ---------------------------------------------------------------------------

def plant_half_switch(store_id: str) -> dict:
    """植入“镜已新、旧段未关”的坏正式态，供对撞失败原样性测试。

    只动 hist / current 以构造夹具，不属于正式应用路径。
    """
    conn = _connect()
    now = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
            (store_id,),
        ).fetchone()
        if cur is None:
            conn.execute("ROLLBACK")
            return {"ok": False, "message": "门店无当前历史行，无法植入夹具"}
        existing_fixture = conn.execute(
            "SELECT id FROM dim_store_hours_hist WHERE store_id=? AND closed_by_run='FIXTURE-HALF-SWITCH'",
            (store_id,),
        ).fetchone()
        if existing_fixture is not None:
            conn.execute("ROLLBACK")
            return {
                "ok": True,
                "already_planted": True,
                "store_id": store_id,
                "fixture_hist_id": existing_fixture["id"],
            }
        new_id = f"HIST-FIX-{uuid4().hex[:8]}"
        rh = row_hash(
            "07:00",
            "19:00",
            cur["weekday_mask"],
            cur["biz_status"],
        )
        conn.execute(
            """INSERT INTO dim_store_hours_hist(
                 id, store_id, open_time, close_time, weekday_mask, biz_status,
                 valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id,
                store_id,
                "07:00",
                "19:00",
                cur["weekday_mask"],
                cur["biz_status"],
                "2099-01-01T00:00:00Z",
                None,
                0,
                cur["source_snapshot_id"],
                rh,
                "FIXTURE-HALF-SWITCH",
            ),
        )
        conn.execute(
            """UPDATE dim_store_hours_current
               SET open_time='07:00', close_time='19:00', weekday_mask=?,
                   biz_status=?, hist_id=?, as_of_ts='2099-01-01T00:00:00Z', row_hash=?
               WHERE store_id=?""",
            (cur["weekday_mask"], cur["biz_status"], new_id, rh, store_id),
        )
        _event(conn, "FIXTURE-HALF-SWITCH", None, store_id, "fixture_planted", "half_switch", {"fixture_hist_id": new_id}, now)
        conn.execute("COMMIT")
        return {
            "ok": True,
            "store_id": store_id,
            "fixture_hist_id": new_id,
            "note": "镜已新、旧段未关；应用应检出 formal_state_invalid 且正式原样",
        }
    finally:
        conn.close()


def repair_half_switch(store_id: str) -> dict:
    """撤销半截切换夹具：删夹具行，镜回到仍 open 的旧当前行。"""
    conn = _connect()
    now = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fixtures = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND closed_by_run='FIXTURE-HALF-SWITCH'",
            (store_id,),
        ).fetchall()
        if not fixtures:
            conn.execute("ROLLBACK")
            return {"ok": True, "store_id": store_id, "repaired": False, "message": "无夹具行"}
        cur = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1 "
            "AND (closed_by_run IS NULL OR closed_by_run<>'FIXTURE-HALF-SWITCH')",
            (store_id,),
        ).fetchone()
        if cur is None:
            conn.execute("ROLLBACK")
            return {"ok": False, "message": "找不到可恢复的当前历史行"}
        conn.execute(
            "DELETE FROM dim_store_hours_hist WHERE store_id=? AND closed_by_run='FIXTURE-HALF-SWITCH'",
            (store_id,),
        )
        conn.execute(
            """UPDATE dim_store_hours_current
               SET open_time=?, close_time=?, weekday_mask=?, biz_status=?,
                   hist_id=?, as_of_ts=?, row_hash=?
               WHERE store_id=?""",
            (
                cur["open_time"],
                cur["close_time"],
                cur["weekday_mask"],
                cur["biz_status"],
                cur["id"],
                cur["valid_from"],
                cur["row_hash"],
                store_id,
            ),
        )
        _event(conn, "FIXTURE-HALF-SWITCH", None, store_id, "fixture_repaired", None, {}, now)
        conn.execute("COMMIT")
        return {"ok": True, "store_id": store_id, "repaired": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 历史维正式写闸
# ---------------------------------------------------------------------------

DIRECT_WRITE_BLOCKED = {
    "ok": False,
    "code": "direct_formal_write_blocked",
    "message": "禁止无对撞直接改正式：请走草稿切换 /api/switch/apply（草稿 → 对撞 → 原子提交）",
    "apply_url": "/api/switch/apply",
    "proof_url": "/api/switch/proof",
}
