import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api.js";

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [sw, setSw] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () =>
    Promise.all([api("/api/proofs"), api("/api/switch/proof")])
      .then(([p, s]) => { setRows(p); setSw(s); })
      .catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reload(); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>
            当前镜 hist_id / row_hash 必须与 is_current 历史行一致；失败须可观测。
            草稿切换另有只读核证：run、草稿态、正式态、区间校验、valid。
          </p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建核证</button>
          <button className="secondary" onClick={reload}>刷新</button>
          <Link to="/switch" className="btn">草稿切换核证</Link>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>

      {sw && (
        <div className="panel">
          <h2>
            草稿切换核证 · {sw.architecture}
            <span className={`badge ${sw.valid ? "ok" : "bad"}`} style={{ marginLeft: 10 }}>
              {sw.valid ? "通过" : "失败"}
            </span>
          </h2>
          <p className="muted">{sw.architecture_note}</p>
          {sw.architecture_contract && (
            <div className="switch-note">
              选定口径：{sw.architecture_contract.chosen}（{sw.architecture_contract.chosen_rule}）
              <br />
              已否决：{sw.architecture_contract.rejected}（{sw.architecture_contract.rejected_rule}）
            </div>
          )}
          <div className="grid-cards">
            <div className="card"><div className="label">草稿总数</div><div className="value">{sw.drafts_state.total}</div></div>
            <div className="card"><div className="label">未提交</div><div className="value">{sw.drafts_state.pending}</div></div>
            <div className="card"><div className="label">失败草稿</div><div className="value">{sw.drafts_state.failed}</div></div>
            <div className="card"><div className="label">切换跑次</div><div className="value">{sw.runs.length}</div></div>
          </div>
          <table>
            <thead><tr><th>核验项</th><th>结果</th></tr></thead>
            <tbody>
              {Object.entries(sw.checks).map(([k, v]) => (
                <tr key={k}>
                  <td className="mono">{k}</td>
                  <td><span className={`badge ${v ? "ok" : "bad"}`}>{v ? "通过" : "失败"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          {sw.failed_drafts.length > 0 && (
            <>
              <h3 style={{ marginTop: "0.9rem" }}>失败草稿（正式态未动，失败可查）</h3>
              <table>
                <thead><tr><th>草稿</th><th>跑次</th><th>门店</th><th>原因</th><th>对撞</th></tr></thead>
                <tbody>
                  {sw.failed_drafts.map((d) => (
                    <tr key={d.id}>
                      <td className="mono">{d.id}</td>
                      <td className="mono">{d.switch_run_id}</td>
                      <td>{d.store_id}</td>
                      <td className="error mono">{d.fail_reason}</td>
                      <td className="mono">{JSON.stringify(d.collision)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}

      <div className="panel">
        <h2>scd_proofs</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
