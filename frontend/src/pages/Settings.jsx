import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Settings() {
  const [settings, setSettings] = useState({});
  const [stubs, setStubs] = useState([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    Promise.all([api("/api/settings"), api("/api/stubs")])
      .then(([s, st]) => { setSettings(s); setStubs(st.stubs || []); })
      .catch((e) => setErr(String(e.message || e)));
  }, []);

  return (
    <>
      <div className="topbar">
        <div>
          <h1>站规</h1>
          <p>端口 6200 / 11200 · 公有仓 lza0829/hourspan-* · 禁门店运营 CRUD 皮。</p>
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      <div className="panel">
        <h2>设置</h2>
        <table>
          <thead><tr><th>键</th><th>值</th></tr></thead>
          <tbody>
            {Object.entries(settings).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="mono">{v}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>草稿切换口径（全局唯一）</h2>
        <table>
          <thead><tr><th>项</th><th>内容</th></tr></thead>
          <tbody>
            <tr><td>选定架构</td><td className="mono">draft_retained_on_failure（甲）</td></tr>
            <tr><td>对撞失败</td><td>正式态完全不动；草稿行保留 failed + fail_reason；核证只读接口可读失败草稿</td></tr>
            <tr><td>已否决</td><td className="mono">rollback_clears_drafts（乙）：失败连草稿回滚删除，只查审计</td></tr>
            <tr><td>正式写闸</td><td className="mono">POST /api/scd/apply → 409 direct_formal_write_blocked</td></tr>
            <tr><td>主链路</td><td className="mono">/api/switch/apply · /api/switch/commit · /api/switch/abandon · /api/switch/proof</td></tr>
            <tr><td>未提交草稿</td><td>存在 draft/validated 草稿时，后到应用一律 pending_draft_exists 拒绝</td></tr>
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>空桩清单</h2>
        <ul>
          {stubs.map((s) => (
            <li key={s} className="mono">{s}</li>
          ))}
        </ul>
      </div>
    </>
  );
}
