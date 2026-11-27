import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function Images() {
  const [rows, setRows] = useState([]);
  const [drafts, setDrafts] = useState({});
  const [msg, setMsg] = useState("");
  const load = () => api.images().then(setRows);
  useEffect(() => { load(); }, []);

  const onRetag = (id) => {
    const tag = (drafts[id] || "").trim();
    if (!tag) { setMsg("新标签不能为空"); return; }
    api.retag(id, tag).then((r) => {
      if (r["错误"]) { setMsg(`改标被拒：${r["错误"]}`); return; }
      setMsg(
        `改标戳 ${r.change_id}（序号 ${r.change_seq}）：${r.old_tag} → ${r.new_tag}，` +
        `${r.arm === "lock" ? "锁定臂不挂在途" : `跟标臂随途单 ${r.follow_order_ids.join(",") || "无"}`}`
      );
      setDrafts((d) => ({ ...d, [id]: "" }));
      load();
    });
  };

  return (
    <>
      <div className="hero">
        <h1>镜像轨</h1>
        <p>改标是带戳事务：轨面始终只有一个最新标签；在途单锁不锁由时序臂决定，戳上留可核痕迹。</p>
      </div>
      {msg && <div className="banner">{msg}</div>}
      <div className="panel">
        <table>
          <thead>
            <tr><th>ID</th><th>镜像</th><th>在轨标签</th><th>状态</th><th>层</th><th>就绪</th><th>改标（写戳）</th><th></th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.name}</td>
                <td><strong>{r.tag}</strong>
                  <div className="sub">版本史：{r.tag_versions.map((v) => v.tag).join(" → ")}</div>
                </td>
                <td><span className={`tag ${r.pull_state === "已就绪" ? "ok" : "warn"}`}>{r.pull_state}</span></td>
                <td>{r.layers_done}/{r.layers_total}</td>
                <td>{r.ready_at || "—"}</td>
                <td>
                  <input
                    className="input"
                    placeholder="新标签，如 1.5.0"
                    value={drafts[r.id] || ""}
                    onChange={(e) => setDrafts((d) => ({ ...d, [r.id]: e.target.value }))}
                  />
                  <button className="btn" onClick={() => onRetag(r.id)}>改标</button>
                </td>
                <td>
                  {r.pull_state !== "已就绪" && (
                    <button className="btn" onClick={() => api.advancePull(r.id).then(load)}>推进拉取</button>
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
