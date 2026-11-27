import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function Align() {
  const [data, setData] = useState(null);
  const [ledger, setLedger] = useState(null);
  const load = () =>
    Promise.all([api.align(), api.replay()]).then(([a, r]) => { setData(a); setLedger(r); });
  useEffect(() => { load(); }, []);
  if (!data || !ledger) return null;

  const switchArm = (arm) => api.setArm(arm).then(load);

  return (
    <>
      <div className="hero">
        <h1>对读屏</h1>
        <p>在途事件先进延后桶扣住，收口才放行。读屏认标由时序臂决定；锁标列与跟标列同屏分列，不得混站。</p>
      </div>

      <div className="panel arm-bar">
        <span>时序臂（二选一，切换只影响此后的在途事务）：</span>
        {["lock", "track"].map((a) => (
          <button key={a} className={`btn ${data["时序臂"] === a ? "ok" : ""}`} onClick={() => switchArm(a)}>
            {a === "lock" ? "锁定启动当下标签" : "跟随镜像轨最新标签"}
          </button>
        ))}
        <span className="sub">当前：{data["时序臂"]}</span>
      </div>

      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>舱</th><th>运行</th><th>探针</th><th>在轨标</th><th>启动锁标</th>
              <th>跟标(effective)</th><th>读屏认标</th><th>延后桶</th><th>最近启停</th>
            </tr>
          </thead>
          <tbody>
            {data["舱体投影"].map((r) => (
              <tr key={r.id} className={r.inflight ? "row-inflight" : ""}>
                <td>{r.name}</td>
                <td>{r.run_state}</td>
                <td>{r.probe_color} · {r.fail_streak}</td>
                <td>{r.rail_tag}</td>
                <td>{r.locked_tag || "—"}</td>
                <td>{r.effective_tag || "—"}</td>
                <td><strong>{r["读屏认标"]}</strong></td>
                <td><span className={`tag ${r.inflight ? "warn" : "ok"}`}>{r["延后桶"]}</span></td>
                <td>{r.last_order_status || "—"} {r.last_order_note || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="sub">
          延后桶计数：在扣 {data["延后桶计数"].held_count ?? 0} / 已放行 {data["延后桶计数"].released_count ?? 0} / 总 {data["延后桶计数"].total}
        </div>
      </div>

      <div className="panel">
        <h2>延后桶·在扣（读屏暂不投影）</h2>
        {data["延后桶在扣"].length === 0 && <div className="sub">无在扣事件</div>}
        <table>
          <thead>
            <tr><th>seq</th><th>事件</th><th>舱</th><th>单</th><th>显示标</th><th>锁标</th><th>跟标</th><th>改标戳</th><th>明细</th></tr>
          </thead>
          <tbody>
            {data["延后桶在扣"].map((b) => (
              <tr key={b.seq}>
                <td>{b.seq}</td><td>{b.event_type}</td><td>{b.pod_id}</td><td>{b.order_id || "—"}</td>
                <td>{b.tag_shown}</td><td>{b.tag_locked || "—"}</td><td>{b.tag_track}</td>
                <td>{b.ref_change_id || "—"}</td><td className="sub">{b.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>可重放账本（按 seq 确定性重放，两臂认标分列）</h2>
        <table>
          <thead>
            <tr><th>seq</th><th>事件</th><th>重放认标·锁定臂</th><th>重放认标·跟标臂</th><th>当前臂认标</th><th>held</th><th>明细</th></tr>
          </thead>
          <tbody>
            {ledger["账本"].map((r) => (
              <tr key={r.seq} className={r.held ? "row-held" : ""}>
                <td>{r.seq}</td><td>{r.event_type}</td>
                <td>{r["重放认标_锁定臂"]}</td>
                <td>{r["重放认标_跟标臂"]}</td>
                <td>{r["当前臂认标"]}</td>
                <td>{r.held ? "在扣" : "放行"}</td>
                <td className="sub">{r.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
