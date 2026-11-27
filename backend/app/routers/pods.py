from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services.common import rows

router = APIRouter(tags=["舱体"])


@router.get("/舱体")
def list_pods():
    with db() as conn:
        return rows(
            conn.execute(
                """
                SELECT p.*, i.name AS image_name, i.tag AS image_tag, i.pull_state,
                       pr.color AS probe_color, pr.fail_streak
                FROM pods p
                LEFT JOIN images i ON i.id = p.image_id
                LEFT JOIN probes pr ON pr.pod_id = p.id
                ORDER BY p.id
                """
            )
        )
