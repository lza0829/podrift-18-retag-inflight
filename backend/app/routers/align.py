from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services.common import rows
from app.services.inflight import ARM_LOCK, ARM_TRACK, get_arm

router = APIRouter(tags=["对读屏"])


@router.get("/对读")
def align_view():
    """读侧投影。

    在途期间延后桶 held=1 的事件被扣住：对读屏不把在途标签观感提前放出。
    锁定臂读锁标（start_orders.locked_tag），跟标臂读跟标（effective_tag），
    两列同时回传以便核验「锁定启动标与跟最新标不得混站」。
    """
    with db() as conn:
        arm = get_arm(conn)
        pods = rows(
            conn.execute(
                """
                SELECT p.id, p.name, p.run_state, pr.color AS probe_color, pr.fail_streak,
                       i.tag AS rail_tag, i.pull_state,
                       o.id AS inflight_order_id, o.status AS last_order_status,
                       o.note AS last_order_note, o.locked_tag, o.effective_tag,
                       o.follow_change_id, o.released_at
                FROM pods p
                LEFT JOIN probes pr ON pr.pod_id = p.id
                LEFT JOIN images i ON i.id = p.image_id
                LEFT JOIN start_orders o ON o.id = (
                  SELECT MAX(id) FROM start_orders WHERE pod_id = p.id
                )
                ORDER BY p.id
                """
            )
        )
        for r in pods:
            inflight = r["last_order_status"] == "在途"
            r["inflight"] = inflight
            if inflight:
                # 读屏该认谁：臂决定，且两值都给，核验时可对读不混站
                r["读屏认标"] = r["locked_tag"] if arm == ARM_LOCK else r["effective_tag"]
                r["延后桶"] = "扣住中"
            else:
                r["读屏认标"] = r["rail_tag"]
                r["延后桶"] = "已放行"
        held = rows(
            conn.execute(
                """
                SELECT b.seq, b.at, b.pod_id, b.order_id, b.image_id, b.event_type,
                       b.arm, b.tag_shown, b.tag_locked, b.tag_track, b.ref_change_id, b.detail
                  FROM align_bucket b WHERE b.held=1 ORDER BY b.seq
                """
            )
        )
        bucket_stats = dict(
            conn.execute(
                "SELECT SUM(held=1) AS held_count, SUM(held=0) AS released_count, COUNT(*) AS total "
                "FROM align_bucket"
            ).fetchone()
        )
        quota = dict(conn.execute("SELECT * FROM quotas WHERE id=1").fetchone())
        policy = dict(conn.execute("SELECT * FROM restart_policy WHERE id=1").fetchone())
        return {
            "时序臂": arm,
            "舱体投影": pods,
            "延后桶在扣": held,
            "延后桶计数": bucket_stats,
            "配额": quota,
            "重启策": policy,
        }


@router.get("/对读/延后桶")
def bucket_view(held_only: int = 0):
    """延后桶全量账本；held_only=1 只看在扣行。按 seq 排序即确定性重放顺序。"""
    with db() as conn:
        sql = "SELECT * FROM align_bucket"
        if held_only:
            sql += " WHERE held=1"
        sql += " ORDER BY seq"
        return rows(conn.execute(sql))


@router.get("/对读/重放")
def replay(upto_seq: int | None = None):
    """可重放读侧：按 seq 重放桶账本，给出每一步读屏应认的标签。

    锁定臂重放认 tag_locked，跟标臂重放认 tag_track；同一账本两种臂结论各自独立，
    便于把「标已改、在途仍落旧标」锁不锁分开验证。
    """
    with db() as conn:
        arm = get_arm(conn)
        sql = "SELECT * FROM align_bucket"
        params: tuple = ()
        if upto_seq is not None:
            sql += " WHERE seq<=?"
            params = (upto_seq,)
        sql += " ORDER BY seq"
        ledger = rows(conn.execute(sql, params))
        for r in ledger:
            r["重放认标_锁定臂"] = r["tag_locked"] or r["tag_shown"]
            r["重放认标_跟标臂"] = r["tag_track"]
            r["当前臂认标"] = (r["tag_locked"] or r["tag_shown"]) if arm == ARM_LOCK else r["tag_track"]
        changes = rows(conn.execute("SELECT * FROM tag_changes ORDER BY change_seq"))
        return {"时序臂": arm, "账本": ledger, "改标戳": changes}
