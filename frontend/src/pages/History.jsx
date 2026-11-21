import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api.js";

export default function History() {
  const [rows, setRows] = useState([]);
  const [storeId, setStoreId] = useState("");
  const [detectRunId, setDetectRunId] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () => {
    const q = storeId.trim() ? `?store_id=${encodeURIComponent(storeId.trim())}` : "";
    return api(`/api/history${q}`).then(setRows).catch((e) => setErr(String(e.message || e)));
  };

  useEffect(() => { reload(); }, []);

  const tryDirect = async () => {
    setErr("");
    setMsg("");
    try {
      const r = await api("/api/scd/apply", {
        method: "POST",
        body: JSON.stringify({ detect_run_id: detectRunId.trim() }),
      });
      setMsg(JSON.stringify(r, null, 2));
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>历史维</h1>
          <p>
            Type2：关旧行（valid_to + is_current=0）并开新行；与当前镜同源。
            本页为只读正式态；写正式必须走草稿切换（草稿 → 对撞 → 原子提交）。
          </p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <input
            value={storeId}
            onChange={(e) => setStoreId(e.target.value)}
            placeholder="按门店过滤，如 ST-EAST-01"
            style={{ minWidth: 200, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button className="secondary" onClick={reload}>查询历史</button>
          <Link to="/switch" className="btn">去草稿切换</Link>
        </div>
        <details>
          <summary className="muted">无对撞直改测试（应被正式写闸拒绝）</summary>
          <div className="actions" style={{ marginTop: "0.6rem" }}>
            <input
              value={detectRunId}
              onChange={(e) => setDetectRunId(e.target.value)}
              placeholder="detect_run_id"
              style={{ minWidth: 220, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
            />
            <button className="danger" onClick={tryDirect}>直改正式（应拒绝）</button>
          </div>
        </details>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      <div className="panel">
        <h2>dim_store_hours_hist</h2>
        <table>
          <thead>
            <tr>
              <th>历史行</th><th>门店</th><th>开-闭</th><th>mask</th><th>状态</th>
              <th>valid_from</th><th>valid_to</th><th>当前</th><th>hash</th><th>closed_by</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.id}</td>
                <td>{r.store_id}</td>
                <td>{r.open_time}–{r.close_time}</td>
                <td className="mono">{r.weekday_mask}</td>
                <td><span className={`badge ${r.biz_status}`}>{r.biz_status}</span></td>
                <td>{r.valid_from}</td>
                <td>{r.valid_to || "—"}</td>
                <td>{r.is_current ? "是" : "否"}</td>
                <td className="mono">{r.row_hash}</td>
                <td className="mono">{r.closed_by_run || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
