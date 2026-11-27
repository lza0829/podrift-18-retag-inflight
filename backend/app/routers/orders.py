from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db
from app.services.common import now_iso, rows

router = APIRouter(tags=["启停单"])


class OrderIn(BaseModel):
    pod_id: int
    action: str  # 启动 | 停止
    target_image_id: int | None = None


@router.get("/启停单")
def list_orders():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM start_orders ORDER BY id DESC"))


@router.post("/启停单")
def create_order(body: OrderIn):
    with db() as conn:
        pod = conn.execute("SELECT * FROM pods WHERE id=?", (body.pod_id,)).fetchone()
        if not pod:
            return {"错误": "舱体不存在"}
        img_id = body.target_image_id or pod["image_id"]
        status = "已受理"
        note = ""
        action = (body.action or "").strip()
        if action in ("启动", "start", "Start"):
            img = conn.execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
            if not img or img["pull_state"] != "已就绪":
                status = "失败"
                note = "镜像未就绪"
            else:
                q = conn.execute("SELECT * FROM quotas WHERE id=1").fetchone()
                need_cpu = float(pod["cpu_quota"])
                need_mem = int(pod["mem_quota_mb"])
                if float(q["cpu_used"]) + need_cpu > float(q["cpu_limit"]) or int(q["mem_used_mb"]) + need_mem > int(q["mem_limit_mb"]):
                    status = "失败"
                    note = "配额不足"
                else:
                    conn.execute("UPDATE pods SET run_state=?, image_id=? WHERE id=?", ("运行中", img_id, body.pod_id))
                    conn.execute(
                        "UPDATE quotas SET cpu_used=cpu_used+?, mem_used_mb=mem_used_mb+? WHERE id=1",
                        (need_cpu, need_mem),
                    )
                    status = "已完成"
                    note = "启动成功"
        elif action in ("停止", "stop", "Stop"):
            if pod["run_state"] == "运行中":
                conn.execute("UPDATE pods SET run_state=? WHERE id=?", ("已停止", body.pod_id))
                conn.execute(
                    "UPDATE quotas SET cpu_used=MAX(0, cpu_used-?), mem_used_mb=MAX(0, mem_used_mb-?) WHERE id=1",
                    (float(pod["cpu_quota"]), int(pod["mem_quota_mb"])),
                )
            status = "已完成"
            note = "停止成功"
        else:
            status = "失败"
            note = f"未知动作:{action}"
        # 落库统一中文动作名
        stored = "启动" if action in ("启动", "start", "Start") else ("停止" if action in ("停止", "stop", "Stop") else action)
        cur = conn.execute(
            "INSERT INTO start_orders(pod_id, action, target_image_id, status, note, created_at) VALUES (?,?,?,?,?,?)",
            (body.pod_id, stored, img_id, status, note, now_iso()),
        )
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("启停单", str(cur.lastrowid), f"{stored} → {status} {note}", now_iso()),
        )
        return {"id": cur.lastrowid, "status": status, "note": note}
