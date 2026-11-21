import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const STATUS_LABEL = {
  draft: "草稿",
  validated: "已对撞",
  committed: "已提交",
  failed: "失败",
  aborted: "整票未提交",
  abandoned: "已放弃",
};

function badgeClass(status) {
  if (status === "committed" || status === "validated") return "ok";
  if (status === "failed") return "bad";
  return "";
}

export default function Switch() {
  const [runs, setRuns] = useState([]);
  const [detectRunId, setDetectRunId] = useState("");
  const [runId, setRunId] = useState("");
  const [proof, setProof] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const reloadRuns = () =>
    api("/api/switch/proof").then((p) => setRuns(p.runs || [])).catch((e) => setErr(String(e.message || e)));

  const loadProof = async (id) => {
    setErr("");
    try {
      const q = id ? `?run_id=${encodeURIComponent(id)}` : "";
      const p = await api(`/api/switch/proof${q}`);
      setProof(p);
      setRunId(id || "");
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  useEffect(() => {
    reloadRuns();
    loadProof("");
  }, []);

  const post = async (path, body, after) => {
    setErr("");
    setBusy(true);
    try {
      const r = await api(path, { method: "POST", body: JSON.stringify(body) });
      setMsg(JSON.stringify(r, null, 2));
      await reloadRuns();
      if (after) await loadProof(after(r));
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const apply = (prepareOnly) =>
    post(
      "/api/switch/apply",
      { detect_run_id: detectRunId.trim(), prepare_only: prepareOnly },
      (r) => r.switch_run_id
    );

  return (
    <>
      <div className="topbar">
        <div>
          <h1>草稿切换</h1>
          <p>
            拟关旧 / 拟开新先入草稿区 → 与现行 is_current、区间规则对撞 → 通过后才原子切正式。
            采用「甲 · 失败草稿留痕」：对撞失败正式态完全不动，草稿保留 failed + 原因，核证可读。
          </p>
        </div>
      </div>

      <div className="panel">
        <div className="actions">
          <input
            value={detectRunId}
            onChange={(e) => setDetectRunId(e.target.value)}
            placeholder="detect_run_id"
            style={{ minWidth: 230, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button disabled={busy} onClick={() => apply(false)}>草稿→对撞→提交</button>
          <button className="secondary" disabled={busy} onClick={() => apply(true)}>
            仅落草稿（不提交）
          </button>
          <button className="secondary" onClick={() => loadProof(runId)}>刷新核证</button>
        </div>
        <p className="muted">
          未提交草稿存在时，任何后到的应用都会被 pending_draft_exists 挡住，不得并行切正式。
        </p>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>

      {proof && (
        <div className="panel">
          <h2>
            核证 · {proof.architecture}
            <span className={`badge ${proof.valid ? "ok" : "bad"}`} style={{ marginLeft: 10 }}>
              {proof.valid ? "通过" : "失败"}
            </span>
          </h2>
          <p className="muted">{proof.architecture_note}</p>
          <div className="actions">
            <button className="secondary" onClick={() => loadProof("")}>全部跑次</button>
          </div>
          <div className="grid-cards">
            <div className="card"><div className="label">草稿总数</div><div className="value">{proof.drafts_state.total}</div></div>
            <div className="card"><div className="label">未提交</div><div className="value">{proof.drafts_state.pending}</div></div>
            <div className="card"><div className="label">失败草稿</div><div className="value">{proof.drafts_state.failed}</div></div>
          </div>

          <h3>切换跑次</h3>
          <table>
            <thead>
              <tr><th>跑次</th><th>检测跑次</th><th>状态</th><th>草稿</th><th>提交</th><th>失败</th><th>时间</th><th /></tr>
            </thead>
            <tbody>
              {proof.runs.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.id}</td>
                  <td className="mono">{r.detect_run_id}</td>
                  <td><span className={`badge ${badgeClass(r.status)}`}>{STATUS_LABEL[r.status] || r.status}</span></td>
                  <td>{r.draft_count}</td>
                  <td>{r.committed_count}</td>
                  <td>{r.failed_count}</td>
                  <td>{r.created_at}</td>
                  <td><button className="secondary" onClick={() => loadProof(r.id)}>核证</button></td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 style={{ marginTop: "1rem" }}>草稿区</h3>
          <table>
            <thead>
              <tr>
                <th>草稿</th><th>门店</th><th>状态</th><th>拟关旧行</th><th>拟开新行</th>
                <th>新 valid_from</th><th>hash</th><th>原因 / 对撞</th>
              </tr>
            </thead>
            <tbody>
              {proof.drafts.map((d) => (
                <tr key={d.id}>
                  <td className="mono">{d.id}</td>
                  <td>{d.store_id}</td>
                  <td><span className={`badge ${badgeClass(d.status)}`}>{STATUS_LABEL[d.status] || d.status}</span></td>
                  <td className="mono">{d.prev_hist_id || "—"}</td>
                  <td className="mono">{d.opened_hist_id || "—"}</td>
                  <td>{d.new_valid_from}</td>
                  <td className="mono">{d.new_row_hash}</td>
                  <td className="mono">
                    {d.fail_reason ? <span className="error">{d.fail_reason}</span> : null}
                    {d.collision && d.collision.length ? (
                      <pre className="msg" style={{ marginTop: 4 }}>
                        {JSON.stringify(d.collision, null, 2)}
                      </pre>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 style={{ marginTop: "1rem" }}>正式态 · 区间校验</h3>
          <table>
            <thead>
              <tr><th>门店</th><th>valid</th><th>单当前</th><th>区间连续</th><th>镜互证</th><th>问题</th></tr>
            </thead>
            <tbody>
              {proof.interval_checks.map((c) => (
                <tr key={c.store_id}>
                  <td>{c.store_id}</td>
                  <td><span className={`badge ${c.valid ? "ok" : "bad"}`}>{c.valid ? "通过" : "失败"}</span></td>
                  <td>{c.single_current ? "是" : "否"}</td>
                  <td>{c.contiguous ? "是" : "否"}</td>
                  <td>{c.mirror_matches ? "是" : "否"}</td>
                  <td className="mono">{c.issues.length ? JSON.stringify(c.issues) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 style={{ marginTop: "1rem" }}>切换审计</h3>
          <table>
            <thead><tr><th>id</th><th>事件</th><th>原因</th><th>门店</th><th>草稿</th><th>时间</th></tr></thead>
            <tbody>
              {proof.events.map((e) => (
                <tr key={e.id}>
                  <td>{e.id}</td>
                  <td className="mono">{e.event}</td>
                  <td className="mono">{e.reason || "—"}</td>
                  <td>{e.store_id || "—"}</td>
                  <td className="mono">{e.draft_id || "—"}</td>
                  <td>{e.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
