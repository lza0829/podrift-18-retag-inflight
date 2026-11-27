import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function Orders() {
  const [pods, setPods] = useState([]);
  const [orders, setOrders] = useState([]);
  const [arm, setArm] = useState("");
  const [msg, setMsg] = useState("");
  const load = () =>
    Promise.all([api.pods(), api.orders(), api.arm()]).then(([p, o, a]) => {
      setPods(p); setOrders(o); setArm(a.arm);
    });
  useEffect(() => { load(); }, []);

  const flash = (r) => {
    if (r["错误"]) { setMsg(`整笔失败已回滚：${r["错误"]}`); }
    load();
  };
  const act = (podId, action) => api.createOrder({ pod_id: podId, action }).then(flash);

  return (
    <>
      <div className="hero">
        <h1>启停单</h1>
        <p>点启动只受理「在途」，不直接落成；收口才落运行态。当前时序臂：<strong>{arm}</strong>
          （{arm === "lock" ? "在途锁定启动当下标签" : "在途跟随镜像轨最新标签并留跟标戳"}）。</p>
      </div>
      {msg && <div className="banner bad">{msg}</div>}
      <div className="panel">
        <div className="row-actions">
          {pods.map((p) => (
            <span key={p.id}>
              <strong>{p.name}</strong>{" "}
              <button className="btn" onClick={() => act(p.id, "启动")}>点启动（入在途）</button>
              <button className="btn" onClick={() => act(p.id, "停止")}>停止</button>
            </span>
          ))}
        </div>
      </div>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>ID</th><th>舱</th><th>动作</th><th>状态</th><th>启动锁标</th><th>跟标/落成标</th>
              <th>跟标戳</th><th>说明</th><th>在途操作</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id} className={o.status === "在途" ? "row-inflight" : ""}>
                <td>{o.id}</td>
                <td>{o.pod_id}</td>
                <td>{o.action}</td>
                <td><span className={`tag ${o.status === "已完成" ? "ok" : o.status === "失败" ? "bad" : "warn"}`}>{o.status}</span></td>
                <td>{o.locked_tag || "—"}</td>
                <td>{o.effective_tag || "—"}{o.effective_tag && o.effective_tag !== o.locked_tag ? " ⟵跟随" : ""}</td>
                <td>{o.follow_change_id || "—"}</td>
                <td>{o.note}</td>
                <td>
                  {o.status === "在途" && (
                    <>
                      <button className="btn ok" onClick={() => api.settleOrder(o.id).then(flash)}>收口落成</button>
                      <button className="btn bad" onClick={() => api.rollbackOrder(o.id, "人工回滚").then(flash)}>在途回滚</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
