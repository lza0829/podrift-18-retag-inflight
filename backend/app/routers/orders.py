from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db, db_write
from app.services.common import now_iso, rows
from app.services.errors import ApiError
from app.services.inflight import (
    abort_order, complete_order, get_arm, start_order,
)

router = APIRouter(tags=["启停单"])


class OrderIn(BaseModel):
    pod_id: int
    action: str  # 启动 | 停止
    target_image_id: int | None = None


class OrderPatch(BaseModel):
    reason: str | None = None


@router.get("/启停单")
def list_orders():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM start_orders ORDER BY id DESC"))


@router.post("/启停单")
def create_order(body: OrderIn):
    action = (body.action or "").strip()
    if action in ("启动", "start", "Start"):
        # 点启动只受理在途，不落运行态；落成由收口接口完成。
        # 任何 ApiError 穿透 db_write：整笔回滚，不产生半截在途单/脏桶。
        with db_write() as conn:
            arm = get_arm(conn)
            return start_order(conn, pod_id=body.pod_id, target_image_id=body.target_image_id, arm=arm)

    if action in ("停止", "stop", "Stop"):
        with db_write() as conn:
            pod = conn.execute("SELECT * FROM pods WHERE id=?", (body.pod_id,)).fetchone()
            if not pod:
                raise ApiError("舱体不存在")
            # 舱体尚有在途启动未收口时，停止不得介入同一延后桶
            inflight = conn.execute(
                "SELECT id FROM start_orders WHERE pod_id=? AND action='启动' AND status='在途'",
                (body.pod_id,),
            ).fetchone()
            if inflight:
                raise ApiError(f"在途启动单 {inflight['id']} 未收口，不能停止", conflict=True)
            note = "停止成功"
            if pod["run_state"] == "运行中":
                conn.execute("UPDATE pods SET run_state=? WHERE id=?", ("已停止", body.pod_id))
                conn.execute(
                    "UPDATE quotas SET cpu_used=MAX(0, cpu_used-?), mem_used_mb=MAX(0, mem_used_mb-?) WHERE id=1",
                    (float(pod["cpu_quota"]), int(pod["mem_quota_mb"])),
                )
            else:
                note = "舱体未在运行"
            cur = conn.execute(
                "INSERT INTO start_orders(pod_id, action, target_image_id, status, note, created_at) VALUES (?,?,?,?,?,?)",
                (body.pod_id, "停止", pod["image_id"], "已完成", note, now_iso()),
            )
            conn.execute(
                "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
                ("启停单", str(cur.lastrowid), f"停止 → 已完成 {note}", now_iso()),
            )
            return {"id": cur.lastrowid, "status": "已完成", "note": note}

    raise ApiError(f"未知动作:{action}")


@router.post("/启停单/{order_id}/收口")
def settle_order(order_id: int):
    """在途启动事务收口：锁定臂落锁标，跟标臂落最新标，同时放行延后桶。"""
    with db_write() as conn:
        return complete_order(conn, order_id=order_id)


@router.post("/启停单/{order_id}/回滚")
def rollback_order(order_id: int, body: OrderPatch | None = None):
    """在途启动事务回滚：半截脏标签/未就绪等场景整笔撤销并放行延后桶。"""
    with db_write() as conn:
        return abort_order(conn, order_id=order_id, reason=(body.reason if body else None) or "人工回滚")
