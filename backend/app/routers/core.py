import json

from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import STUBS
from app.modules import draft_then_commit_switch as switch
from app.services import catalog, detect, ingest, scd

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = catalog.overview(conn)
        data["空桩"] = STUBS
        return data
    finally:
        conn.close()


@router.get("/stores")
def stores():
    conn = connect()
    try:
        return catalog.store_board(conn)
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(batch_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY as_of_ts, store_id",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_snapshots ORDER BY as_of_ts DESC, store_id LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/batches")
def batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_batches ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/history")
def history(store_id: str | None = None):
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/current")
def current_mirror():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dim_store_hours_current ORDER BY store_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/detect-runs")
def detect_runs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM detect_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/proofs")
def proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scd_proofs ORDER BY id DESC LIMIT 80"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = Body(None)):
    name = (body or {}).get("fixture", "ingest_change_batch.json")
    return ingest.run_batch(name)


@router.post("/detect/run")
def run_detect(body: dict | None = Body(None)):
    batch_id = (body or {}).get("batch_id")
    return detect.run_detect(batch_id)


@router.post("/scd/apply")
def run_scd(body: dict = Body(...)):
    """历史维正式写闸：禁止无对撞直接改正式，统一改走草稿切换。"""
    raise HTTPException(409, switch.DIRECT_WRITE_BLOCKED)


@router.get("/switch/drafts")
def switch_drafts(switch_run_id: str | None = None, store_id: str | None = None):
    conn = connect()
    try:
        sql = "SELECT * FROM switch_drafts"
        where = []
        params = []
        if switch_run_id:
            where.append("switch_run_id=?")
            params.append(switch_run_id)
        if store_id:
            where.append("store_id=?")
            params.append(store_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 300"
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["collision"] = json.loads(r["collision_json"] or "{}")
            item["pending"] = r["status"] in ("draft", "validated")
            out.append(item)
        return out
    finally:
        conn.close()


@router.get("/switch/events")
def switch_events(switch_run_id: str | None = None):
    conn = connect()
    try:
        if switch_run_id:
            rows = conn.execute(
                "SELECT * FROM switch_events WHERE switch_run_id=? ORDER BY id DESC",
                (switch_run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM switch_events ORDER BY id DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/switch/apply")
def run_switch(body: dict = Body(...)):
    """草稿切换：草稿 → 对撞 → 原子切正式（或对撞失败留痕，正式不动）。"""
    detect_run_id = (body or {}).get("detect_run_id")
    if not detect_run_id:
        raise HTTPException(400, "需要 detect_run_id")
    prepare_only = bool((body or {}).get("prepare_only", False))
    return switch.apply_switch(detect_run_id, prepare_only=prepare_only)


@router.post("/switch/commit")
def run_switch_commit(body: dict = Body(...)):
    """提交 prepared 切换跑次：事务内重对撞后原子切正式。"""
    switch_run_id = (body or {}).get("switch_run_id")
    if not switch_run_id:
        raise HTTPException(400, "需要 switch_run_id")
    return switch.commit_prepared(switch_run_id)


@router.post("/switch/abandon")
def run_switch_abandon(body: dict = Body(...)):
    switch_run_id = (body or {}).get("switch_run_id")
    if not switch_run_id:
        raise HTTPException(400, "需要 switch_run_id")
    return switch.abandon_switch(switch_run_id)


@router.get("/switch/proof")
def switch_proof(run_id: str | None = None, store_id: str | None = None):
    """草稿切换只读核证：run、草稿态、正式态、区间校验、valid。"""
    return switch.switch_proof(run_id=run_id, store_id=store_id)


@router.post("/switch/fixture/half")
def plant_half_switch_fixture(body: dict = Body(...)):
    store_id = (body or {}).get("store_id")
    if not store_id:
        raise HTTPException(400, "需要 store_id")
    return switch.plant_half_switch(store_id)


@router.post("/switch/fixture/half/repair")
def repair_half_switch_fixture(body: dict = Body(...)):
    store_id = (body or {}).get("store_id")
    if not store_id:
        raise HTTPException(400, "需要 store_id")
    return switch.repair_half_switch(store_id)


@router.post("/proofs/rebuild")
def rebuild_proofs():
    return scd.rebuild_proofs()


@router.get("/settings")
def settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


@router.get("/stubs")
def stubs():
    return {"stubs": STUBS}
