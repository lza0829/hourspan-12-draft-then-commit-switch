"""草稿切换回归：甲口径 · 失败草稿留痕为准。

覆盖：
  1. 成功切换可测（草稿→对撞→原子切正式，镜/区间/核证一致）；
  2. 故意对撞失败：正式原样；失败草稿可见且带原因；另一跑次核证草稿空、审计可查；
  3. 半截切换夹具：正式态必须原样（应用拒绝且不改正式）；
  4. 未提交草稿存在时，后到应用不得并行切正式；
  5. 邻键草稿不串切本键；
  6. 历史维正式写闸拒绝无对撞直改。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "FIXTURES_DIR",
        os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "fixtures")
        ),
    )
    for mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(mod, None)
    from app import seed

    seed.init_db()
    from app.db import connect
    from app.modules import draft_then_commit_switch as switch
    from app.services import detect, ingest

    yield {
        "seed": seed,
        "switch": switch,
        "detect": detect,
        "ingest": ingest,
        "connect": connect,
    }


def _formal(connect):
    conn = connect()
    try:
        hist = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
            ).fetchall()
        ]
        mirror = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM dim_store_hours_current ORDER BY store_id"
            ).fetchall()
        ]
        return {"hist": hist, "mirror": mirror}
    finally:
        conn.close()


def _run_change(env, fixture="ingest_change_batch.json"):
    env["ingest"].run_batch(fixture)
    return env["detect"].run_detect()


def test_happy_path_switches_atomically(env):
    before = _formal(env["connect"])
    run = _run_change(env)
    assert run["changed_count"] == 2

    out = env["switch"].apply_switch(run["detect_run_id"])
    assert out["ok"] is True, out
    assert out["status"] == "committed"
    assert out["architecture"] == "draft_retained_on_failure"
    assert len(out["committed"]) == 2

    after = _formal(env["connect"])
    # 每店恰好一行 is_current，且镜指向该行。
    by_store = {}
    for row in after["hist"]:
        by_store.setdefault(row["store_id"], []).append(row)
    for store_id in ("ST-EAST-01", "ST-SOUTH-01"):
        rows = by_store[store_id]
        assert len(rows) == 2
        currents = [r for r in rows if r["is_current"] == 1]
        assert len(currents) == 1
        old = [r for r in rows if r["is_current"] == 0][0]
        assert old["valid_to"] == currents[0]["valid_from"]
        mirror = [m for m in after["mirror"] if m["store_id"] == store_id][0]
        assert mirror["hist_id"] == currents[0]["id"]
        assert mirror["row_hash"] == currents[0]["row_hash"]
    for sid in ("ST-EAST-02", "ST-WEST-01"):
        assert [r for r in before["hist"] if r["store_id"] == sid] == [
            r for r in after["hist"] if r["store_id"] == sid
        ]

    proof = env["switch"].switch_proof(run_id=out["switch_run_id"])
    assert proof["valid"] is True
    assert proof["drafts_state"]["by_status"] == {"committed": 2}
    assert proof["failed_drafts"] == []
    assert all(chk["valid"] for chk in proof["interval_checks"])


def test_collision_failure_keeps_formal_and_leaves_failed_drafts(env):
    before = _formal(env["connect"])
    run = _run_change(env, "bad_collision_batch.json")
    assert run["changed_count"] == 2

    out = env["switch"].apply_switch(run["detect_run_id"])
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert out["formal_untouched"] is True

    # 正式态完全原样（含 is_current / 区间 / 镜）
    assert _formal(env["connect"]) == before

    proof = env["switch"].switch_proof(run_id=out["switch_run_id"])
    assert proof["valid"] is True, proof
    assert proof["checks"]["formal_untouched_by_failures"] is True
    assert proof["checks"]["failed_drafts_have_reason"] is True
    assert len(proof["failed_drafts"]) == 2
    codes = {d["store_id"]: d["fail_reason"] for d in proof["failed_drafts"]}
    assert codes["ST-WEST-01"] == "time_order"
    assert codes["ST-EAST-02"] == "sibling_collision_failed"
    assert all(d["status"] == "failed" for d in proof["failed_drafts"])
    assert all(d["collision"] for d in proof["failed_drafts"])
    # 失败草稿在“本跑次”核证中可见，正是甲口径。
    assert proof["drafts_state"]["failed"] == 2
    assert proof["drafts_state"]["pending"] == 0

    # 乙口径的反面：清库式核证（只看该检测跑的 committed 切换）看不到残留草稿，
    # 但审计事件仍可查——此处验证“核证可读失败草稿”而非“只能查审计”。
    other = env["switch"].switch_proof(store_id="ST-NORTH-01")
    assert other["drafts"] == []
    assert other["drafts_state"]["empty"] is True


def test_half_switch_fixture_formal_state_untouched(env):
    planted = env["switch"].plant_half_switch("ST-EAST-01")
    assert planted["ok"] is True
    before = _formal(env["connect"])
    assert env["switch"].switch_proof(store_id="ST-EAST-01")["formal"]["ST-EAST-01"]["valid"] is False

    run = _run_change(env)  # 该批含 ST-EAST-01
    out = env["switch"].apply_switch(run["detect_run_id"])
    assert out["ok"] is False, out
    assert out["formal_untouched"] is True
    assert _formal(env["connect"]) == before

    proof = env["switch"].switch_proof(run_id=out["switch_run_id"])
    failed = {d["store_id"]: d for d in proof["failed_drafts"]}
    assert "formal_state_invalid" in [r["code"] for r in failed["ST-EAST-01"]["collision"]]
    # 半截夹具仍在（核证如实反映），正式未被“顺手修复”。
    assert proof["checks"]["failed_drafts_have_reason"] is True

    repair = env["switch"].repair_half_switch("ST-EAST-01")
    assert repair["ok"] is True and repair["repaired"] is True
    assert env["switch"].switch_proof(store_id="ST-EAST-01")["formal"]["ST-EAST-01"]["valid"] is True


def test_pending_draft_blocks_later_switch(env):
    run1 = _run_change(env)
    prepared = env["switch"].apply_switch(run1["detect_run_id"], prepare_only=True)
    assert prepared["ok"] is True and prepared["status"] == "prepared"
    before = _formal(env["connect"])

    # 后到应用：另一批可过对撞的变更，必须被未提交草稿挡住。
    env["ingest"].run_batch("ingest_change_batch.json")
    run2 = env["detect"].run_detect()
    out = env["switch"].apply_switch(run2["detect_run_id"])
    assert out["ok"] is False
    assert "对撞失败" in out["message"]
    assert _formal(env["connect"]) == before
    reasons = [r["code"] for d in out["drafts"] for r in d["reasons"]]
    assert "pending_draft_exists" in reasons

    # 放弃未提交草稿后，仍可按原跑次提交（重对撞通过）。
    abandoned = env["switch"].abandon_switch(prepared["switch_run_id"])
    assert abandoned["ok"] is True
    committed = env["switch"].commit_prepared(prepared["switch_run_id"])
    assert committed["ok"] is False  # abandoned 不可提交
    # 后到应用现在可以走
    out2 = env["switch"].apply_switch(run2["detect_run_id"])
    assert out2["ok"] is True, out2


def test_abandon_then_later_apply_commits_and_mirror_matches(env):
    run1 = _run_change(env)
    prepared = env["switch"].apply_switch(run1["detect_run_id"], prepare_only=True)
    assert prepared["status"] == "prepared"
    env["switch"].abandon_switch(prepared["switch_run_id"])

    out = env["switch"].apply_switch(run1["detect_run_id"])
    assert out["ok"] is True
    proof = env["switch"].switch_proof(run_id=out["switch_run_id"])
    assert proof["valid"] is True
    for chk in proof["interval_checks"]:
        assert chk["single_current"] and chk["contiguous"] and chk["mirror_matches"]


def test_neighbour_draft_cannot_switch_this_key(env):
    # 只让 ST-EAST-01 有草稿：用单店快照跑检测
    from app.db import connect

    conn = connect()
    conn.execute(
        """INSERT INTO source_snapshots(
             id, batch_id, store_id, as_of_ts, open_time, close_time,
             weekday_mask, biz_status, payload_json, row_hash
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "SNAP-ONLY-EAST",
            "B-CUSTOM",
            "ST-EAST-01",
            "2026-11-18T00:00:00Z",
            "09:45",
            "21:45",
            "1111100",
            "open",
            "{}",
            "hash-east",
        ),
    )
    conn.commit()
    conn.close()
    run = env["detect"].run_detect("B-CUSTOM")
    assert [c["store_id"] for c in run["changes"]] == ["ST-EAST-01"]

    prepared = env["switch"].apply_switch(run["detect_run_id"], prepare_only=True)
    assert prepared["status"] == "prepared"
    before = _formal(env["connect"])

    # 另一店（ST-EAST-02）的应用不得借邻键草稿切正式，也不得改动邻键正式态。
    conn = connect()
    conn.execute(
        """INSERT INTO source_snapshots(
             id, batch_id, store_id, as_of_ts, open_time, close_time,
             weekday_mask, biz_status, payload_json, row_hash
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "SNAP-ONLY-EAST2",
            "B-CUSTOM2",
            "ST-EAST-02",
            "2026-11-18T00:00:00Z",
            "10:15",
            "22:15",
            "1111110",
            "open",
            "{}",
            "hash-east2",
        ),
    )
    conn.commit()
    conn.close()
    run2 = env["detect"].run_detect("B-CUSTOM2")
    out = env["switch"].apply_switch(run2["detect_run_id"])
    assert out["ok"] is False
    assert _formal(env["connect"]) == before
    # 邻键草稿未被本跑次触碰：仍是 prepared 状态、未 committed。
    proof = env["switch"].switch_proof(run_id=prepared["switch_run_id"])
    assert proof["drafts_state"]["by_status"] == {"validated": 1}


def test_direct_formal_write_is_blocked(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    res = client.post("/api/scd/apply", json={"detect_run_id": "DET-whatever"})
    assert res.status_code == 409
    body = res.json()["detail"]
    assert body["code"] == "direct_formal_write_blocked"
    assert body["apply_url"] == "/api/switch/apply"


def test_repeat_apply_of_committed_run_is_refused(env):
    run = _run_change(env)
    first = env["switch"].apply_switch(run["detect_run_id"])
    assert first["ok"] is True
    after_first = _formal(env["connect"])
    second = env["switch"].apply_switch(run["detect_run_id"])
    assert second["ok"] is False
    assert "已成功切换" in second["message"]
    assert _formal(env["connect"]) == after_first


def test_switch_proof_readonly_does_not_change_state(env):
    run = _run_change(env, "bad_collision_batch.json")
    out = env["switch"].apply_switch(run["detect_run_id"])
    before = _formal(env["connect"])
    first = env["switch"].switch_proof(run_id=out["switch_run_id"])
    second = env["switch"].switch_proof(run_id=out["switch_run_id"])
    assert _formal(env["connect"]) == before
    assert first["drafts"] == second["drafts"]
    assert first["valid"] == second["valid"]
    assert first["architecture"] == "draft_retained_on_failure"


def test_api_flow_end_to_end(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    ingest = client.post("/api/ingest/run", json={}).json()
    assert ingest["accepted"] == 2
    detect_run = client.post("/api/detect/run", json={"batch_id": ingest["batch_id"]}).json()
    applied = client.post(
        "/api/switch/apply", json={"detect_run_id": detect_run["detect_run_id"]}
    ).json()
    assert applied["ok"] is True
    proof = client.get("/api/switch/proof", params={"run_id": applied["switch_run_id"]}).json()
    assert proof["valid"] is True
    assert proof["run"]["status"] == "committed"
    assert proof["events"]
    # 当前镜与历史维接口反映同一结果
    current = {r["store_id"]: r for r in client.get("/api/current").json()}
    hist = client.get("/api/history").json()
    for store_id in ("ST-EAST-01", "ST-SOUTH-01"):
        cur_rows = [r for r in hist if r["store_id"] == store_id and r["is_current"] == 1]
        assert len(cur_rows) == 1
        assert current[store_id]["hist_id"] == cur_rows[0]["id"]


def test_failure_then_retry_after_fixing_snapshot(env):
    """对撞失败后正式不动；修正后同一检测跑次的重试仍可提交（失败草稿不挡路）。"""
    run = _run_change(env, "bad_collision_batch.json")
    failed = env["switch"].apply_switch(run["detect_run_id"])
    assert failed["ok"] is False

    from app.db import connect
    from app.seed import row_hash

    conn = connect()
    good = row_hash("08:00", "20:00", "1111100", "open")
    conn.execute(
        "UPDATE source_snapshots SET open_time='08:00', close_time='20:00', row_hash=? WHERE id='SNAP-BAD-WEST-01'",
        (good,),
    )
    # 检测报告里的 to_hash 也要同步，保持报告与快照一致
    row = conn.execute("SELECT * FROM detect_runs WHERE id=?", (run["detect_run_id"],)).fetchone()
    report = json.loads(row["report_json"])
    for ch in report["changes"]:
        if ch["store_id"] == "ST-WEST-01":
            ch["to_hash"] = good
    conn.execute(
        "UPDATE detect_runs SET report_json=? WHERE id=?",
        (json.dumps(report, ensure_ascii=False), run["detect_run_id"]),
    )
    conn.commit()
    conn.close()

    retry = env["switch"].apply_switch(run["detect_run_id"])
    assert retry["ok"] is True, retry
    proof = env["switch"].switch_proof(run_id=retry["switch_run_id"])
    assert proof["valid"] is True
    # 历史失败草稿仍在，但不再 pending。
    assert proof["drafts_state"]["pending"] == 0


def test_commit_stage_failure_rolls_back_and_still_leaves_failed_drafts(env, monkeypatch):
    """提交阶段异常（事务内）→ 整票回滚，但甲口径要求失败仍可查。"""
    run = _run_change(env)
    before = _formal(env["connect"])

    from app.modules.draft_then_commit_switch import service as svc

    def boom(conn, switch_run_id, drafts, now):
        raise RuntimeError("模拟提交写入异常")

    monkeypatch.setattr(svc, "_commit_drafts", boom)
    out = svc.apply_switch(run["detect_run_id"])
    assert out["ok"] is False
    assert out["rolled_back"] is True
    assert out["formal_untouched"] is True
    assert _formal(env["connect"]) == before

    proof = svc.switch_proof(run_id=out["switch_run_id"])
    assert proof["run"]["status"] == "failed"
    assert proof["drafts_state"]["failed"] == 2
    assert all(d["fail_reason"] == "commit_failed_rolled_back" for d in proof["failed_drafts"])
    assert proof["checks"]["failed_drafts_have_reason"] is True
    # 回滚后不得残留未提交草稿，避免无谓阻塞后续应用。
    assert proof["drafts_state"]["pending"] == 0

    monkeypatch.undo()
    retry = svc.apply_switch(run["detect_run_id"])
    assert retry["ok"] is True, retry


def test_commit_prepared_stage_failure_keeps_failure_trail(env, monkeypatch):
    """prepared 跑次在提交阶段异常：正式不动，跑次 failed，草稿 failed 带原因，detect_run_id 正确。"""
    run = _run_change(env)
    prepared = env["switch"].apply_switch(run["detect_run_id"], prepare_only=True)
    assert prepared["status"] == "prepared"
    before = _formal(env["connect"])

    from app.modules.draft_then_commit_switch import service as svc

    def boom(conn, switch_run_id, drafts, now):
        raise RuntimeError("模拟提交写入异常")

    monkeypatch.setattr(svc, "_commit_drafts", boom)
    out = svc.commit_prepared(prepared["switch_run_id"])
    assert out["ok"] is False and out["rolled_back"] is True
    assert _formal(env["connect"]) == before

    proof = svc.switch_proof(run_id=prepared["switch_run_id"])
    assert proof["run"]["status"] == "failed"
    # 关键回归：失败留痕必须挂回原检测跑次，否则审计链断裂。
    assert proof["run"]["detect_run_id"] == run["detect_run_id"]
    assert proof["drafts_state"]["failed"] == 2
    assert proof["drafts_state"]["pending"] == 0
    assert all(d["fail_reason"] == "commit_failed_rolled_back" for d in proof["failed_drafts"])
