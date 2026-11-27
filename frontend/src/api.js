const j = async (url, opt) => {
  const r = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opt,
  });
  return r.json();
};

export const api = {
  health: () => j("/api/健康"),
  images: () => j("/api/镜像"),
  advancePull: (id) => j(`/api/镜像/${id}/推进拉取`, { method: "POST" }),
  retag: (id, tag) => j(`/api/镜像/${id}/改标`, { method: "POST", body: JSON.stringify({ tag }) }),
  tagHistory: (id) => j(`/api/镜像/${id}/标签史`),
  pods: () => j("/api/舱体"),
  orders: () => j("/api/启停单"),
  createOrder: (body) => j("/api/启停单", { method: "POST", body: JSON.stringify(body) }),
  settleOrder: (id) => j(`/api/启停单/${id}/收口`, { method: "POST" }),
  rollbackOrder: (id, reason) =>
    j(`/api/启停单/${id}/回滚`, { method: "POST", body: JSON.stringify({ reason }) }),
  probes: () => j("/api/探针"),
  setProbe: (podId, body) => j(`/api/探针/${podId}`, { method: "POST", body: JSON.stringify(body) }),
  quota: () => j("/api/配额"),
  policy: () => j("/api/重启策"),
  setPolicy: (body) => j("/api/重启策", { method: "POST", body: JSON.stringify(body) }),
  align: () => j("/api/对读"),
  bucket: (heldOnly = 0) => j(`/api/对读/延后桶?held_only=${heldOnly}`),
  replay: (uptoSeq) => j(`/api/对读/重放${uptoSeq ? `?upto_seq=${uptoSeq}` : ""}`),
  arm: () => j("/api/时序臂"),
  setArm: (arm) => j("/api/时序臂", { method: "POST", body: JSON.stringify({ arm }) }),
  settings: () => j("/api/舱规"),
  events: () => j("/api/事件"),
};
