from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db, db_write
from app.services.common import now_iso, rows
from app.services.inflight import retag

router = APIRouter(tags=["镜像轨"])

class RetagIn(BaseModel):
    tag: str


@router.get("/镜像")
def list_images():
    with db() as conn:
        out = rows(conn.execute("SELECT * FROM images ORDER BY id"))
        for r in out:
            r["tag_versions"] = rows(
                conn.execute(
                    "SELECT version, tag, updated_at FROM image_tags WHERE image_id=? ORDER BY version",
                    (r["id"],),
                )
            )
        return out


@router.get("/镜像/{image_id}/标签史")
def tag_history(image_id: int):
    with db() as conn:
        return rows(
            conn.execute(
                """
                SELECT tc.id, tc.change_seq, tc.old_tag, tc.new_tag, tc.arm,
                       tc.follow_order_ids, tc.changed_at
                  FROM tag_changes tc WHERE tc.image_id=? ORDER BY tc.change_seq
                """,
                (image_id,),
            )
        )


@router.post("/镜像/{image_id}/改标")
def change_tag(image_id: int, body: RetagIn):
    """改标：立即写事务，与在途启动挂接同笔成败；ApiError 穿透 with 触发整笔回滚，不留半截脏标签。"""
    with db_write() as conn:
        return retag(conn, image_id=image_id, new_tag=body.tag)


@router.post("/镜像/{image_id}/推进拉取")
def advance_pull(image_id: int):
    with db_write() as conn:
        img = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        if not img:
            return {"错误": "镜像不存在"}
        done = min(int(img["layers_done"]) + 1, int(img["layers_total"]))
        ready = done >= int(img["layers_total"])
        ts = now_iso()
        conn.execute(
            "UPDATE images SET layers_done=?, pull_state=?, ready_at=? WHERE id=?",
            (done, "已就绪" if ready else "拉取中", ts if ready else None, image_id),
        )
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("镜像", str(image_id), f"拉取推进到 {done}/{img['layers_total']}", ts),
        )
        return {"ok": True, "layers_done": done, "ready": ready}
