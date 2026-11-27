from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db, db_write
from app.services.errors import ApiError
from app.services.inflight import ARMS, ARM_LOCK, ARM_TRACK, get_arm, set_arm

router = APIRouter(tags=["时序臂"])


class ArmIn(BaseModel):
    arm: str


@router.get("/时序臂")
def read_arm():
    with db() as conn:
        return {
            "arm": get_arm(conn),
            "可选": list(ARMS),
            "说明": {
                ARM_LOCK: "在途启动事务锁定启动当下标签，事后改标不得偷改在途目标；对读屏跟锁定标签",
                ARM_TRACK: "在途事务跟随镜像轨最新标签，跟标动作写改标戳可核痕迹；镜像轨单一观感",
            },
        }


@router.post("/时序臂")
def write_arm(body: ArmIn):
    with db_write() as conn:
        try:
            arm = set_arm(conn, body.arm.strip())
        except ValueError as e:
            raise ApiError(str(e))
        return {"ok": True, "arm": arm}
