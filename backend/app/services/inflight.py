"""在途改标语义核心。

两条时序臂在此各自独立实现、互不混站：

* 锁定臂 lock：在途启动事务在点启动当下锁定镜像与标签（locked_image_id/locked_tag），
  事后改标不得偷偷改动已在途事务的目标；对读屏跟锁定标签，镜像轨只呈现最新标签。
* 跟标臂 track：在途事务跟随镜像轨最新标签，跟标动作必须在改标戳上写下可核痕迹
  （tag_changes.follow_order_ids + start_orders.follow_change_id/effective_tag）；
  镜像轨始终单一观感，不存在长期双标签。

改标与启停推进同笔成败：全部写操作跑在 BEGIN IMMEDIATE 立即写事务里，
任何一步抛错整笔回滚，杜绝半截脏标签。
"""
from __future__ import annotations
import sqlite3

from app.services.common import now_iso
from app.services.errors import ApiError

ARMS = ("lock", "track")
ARM_LOCK, ARM_TRACK = ARMS
SETTING_ARM = "inflight_tag_arm"


class ConflictError(ApiError):
    """舱体已有在途启动事务（对读延后桶未收口），新启动必须整笔失败。"""

    def __init__(self, message: str):
        super().__init__(message, conflict=True)


def get_arm(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTING_ARM,)).fetchone()
    arm = row["value"] if row else ARM_LOCK
    return arm if arm in ARMS else ARM_LOCK


def set_arm(conn: sqlite3.Connection, arm: str) -> str:
    if arm not in ARMS:
        raise ApiError(f"未知时序臂:{arm}")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SETTING_ARM, arm),
    )
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        ("时序臂", SETTING_ARM, f"在途改标语义切换为 {arm}", now_iso()),
    )
    return arm


def _next_change_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(change_seq), 0) + 1 AS s FROM tag_changes").fetchone()
    return int(row["s"])


def _bucket_insert(
    conn: sqlite3.Connection,
    *,
    pod_id: int,
    order_id: int | None,
    image_id: int,
    event_type: str,
    arm: str,
    tag_shown: str,
    tag_locked: str | None,
    tag_track: str,
    ref_change_id: int | None,
    held: bool,
    detail: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO align_bucket
          (at, pod_id, order_id, image_id, event_type, arm, tag_shown, tag_locked,
           tag_track, ref_change_id, held, released_at, detail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now_iso(), pod_id, order_id, image_id, event_type, arm, tag_shown, tag_locked,
            tag_track, ref_change_id, 1 if held else 0, None if held else now_iso(), detail,
        ),
    )
    return int(cur.lastrowid)


def start_order(
    conn: sqlite3.Connection,
    *,
    pod_id: int,
    target_image_id: int | None,
    arm: str,
) -> dict:
    """点启动：受理一笔在途启动事务。调用方须已持有立即写事务。"""
    pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()
    if not pod:
        raise ApiError("舱体不存在")

    # 同舱在途未收口 → 整笔失败，不允许第二笔在途启动叠在同一延后桶上
    inflight = conn.execute(
        "SELECT id FROM start_orders WHERE pod_id=? AND action='启动' AND status='在途'",
        (pod_id,),
    ).fetchone()
    if inflight:
        raise ConflictError(f"舱体 {pod_id} 已有在途启动单 {inflight['id']} 未收口")

    img_id = target_image_id or pod["image_id"]
    img = conn.execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
    if not img:
        raise ApiError("镜像不存在")

    # 受理即校验就绪与配额：校验不过就不产生在途单，整笔失败
    if img["pull_state"] != "已就绪":
        raise ApiError("镜像未就绪")
    q = conn.execute("SELECT * FROM quotas WHERE id=1").fetchone()
    need_cpu = float(pod["cpu_quota"])
    need_mem = int(pod["mem_quota_mb"])
    if float(q["cpu_used"]) + need_cpu > float(q["cpu_limit"]) or \
       int(q["mem_used_mb"]) + need_mem > int(q["mem_limit_mb"]):
        raise ApiError("配额不足")

    # 点启动当下：两臂都在此刻快照标签。锁定臂把它钉死；跟标臂把它作为跟标起点。
    locked_tag = img["tag"]
    cur = conn.execute(
        """
        INSERT INTO start_orders
          (pod_id, action, target_image_id, status, note, created_at,
           locked_image_id, locked_tag, effective_tag, follow_change_id, released_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pod_id, "启动", img_id, "在途", "启动事务在途", now_iso(),
            img_id, locked_tag, locked_tag, None, None,
        ),
    )
    order_id = int(cur.lastrowid)

    # 对读屏延后桶：在途事件先进桶扣住（held=1），读屏在收口前不投影在途标签观感
    _bucket_insert(
        conn,
        pod_id=pod_id, order_id=order_id, image_id=img_id,
        event_type="启动受理", arm=arm,
        tag_shown=locked_tag, tag_locked=locked_tag, tag_track=locked_tag,
        ref_change_id=None, held=True,
        detail=f"启动单 {order_id} 受理入在途，快照标签 {locked_tag}",
    )
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        ("启停单", str(order_id), f"启动 → 在途（{arm} 臂，快照标签 {locked_tag}）", now_iso()),
    )
    return {"id": order_id, "status": "在途", "locked_image_id": img_id, "locked_tag": locked_tag}


def complete_order(conn: sqlite3.Connection, *, order_id: int) -> dict:
    """收口：在途启动事务落成。落哪个标签由臂决定，与改标同笔成败。"""
    order = conn.execute("SELECT * FROM start_orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError("启停单不存在")
    if order["action"] != "启动":
        raise ApiError("仅启动单可在途收口")
    if order["status"] != "在途":
        raise ApiError(f"启停单 {order_id} 不在在途态（当前 {order['status']}）")

    arm = get_arm(conn)
    pod_id = int(order["pod_id"])
    pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()

    if arm == ARM_LOCK:
        # 锁定臂：只认点启动当下锁定的镜像与标签，轨上事后改标与本笔无关
        final_image_id = int(order["locked_image_id"])
        final_tag = order["locked_tag"]
        follow_change_id = None
        detail = f"按锁定标签 {final_tag} 落成，无视收口前沿轨改标"
    else:
        # 跟标臂：跟随镜像轨最新标签（effective_tag 已由改标事务逐次改写并留痕）
        img = conn.execute("SELECT * FROM images WHERE id=?", (order["locked_image_id"],)).fetchone()
        final_image_id = int(img["id"])
        final_tag = img["tag"]
        follow_change_id = order["follow_change_id"]
        if final_tag != order["locked_tag"]:
            detail = f"跟随最新标签 {final_tag} 落成（启动快照 {order['locked_tag']}，跟标戳 {follow_change_id}）"
        else:
            detail = f"标签未变，按 {final_tag} 落成"

    img = conn.execute("SELECT * FROM images WHERE id=?", (final_image_id,)).fetchone()
    if img["pull_state"] != "已就绪":
        raise ApiError(f"目标镜像标签 {final_tag} 未就绪，收口失败")
    q = conn.execute("SELECT * FROM quotas WHERE id=1").fetchone()
    need_cpu = float(pod["cpu_quota"])
    need_mem = int(pod["mem_quota_mb"])
    if float(q["cpu_used"]) + need_cpu > float(q["cpu_limit"]) or \
       int(q["mem_used_mb"]) + need_mem > int(q["mem_limit_mb"]):
        raise ApiError("配额不足，收口失败")

    conn.execute(
        "UPDATE pods SET run_state=?, image_id=? WHERE id=?",
        ("运行中", final_image_id, pod_id),
    )
    conn.execute(
        "UPDATE quotas SET cpu_used=cpu_used+?, mem_used_mb=mem_used_mb+? WHERE id=1",
        (need_cpu, need_mem),
    )
    ts = now_iso()
    conn.execute(
        """
        UPDATE start_orders
           SET status='已完成', note=?, target_image_id=?, effective_tag=?, released_at=?
         WHERE id=?
        """,
        (detail, final_image_id, final_tag, ts, order_id),
    )
    # 桶内该单所有在途行一次性放行；行不删改，只翻 held/released_at，重放可复现两次观感
    conn.execute(
        "UPDATE align_bucket SET held=0, released_at=? WHERE order_id=? AND held=1",
        (ts, order_id),
    )
    _bucket_insert(
        conn,
        pod_id=pod_id, order_id=order_id, image_id=final_image_id,
        event_type="启动收口", arm=arm,
        tag_shown=final_tag,
        tag_locked=order["locked_tag"],
        tag_track=final_tag,
        ref_change_id=follow_change_id,
        held=False,
        detail=f"启动单 {order_id} 收口落成，{detail}",
    )
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        ("启停单", str(order_id), f"启动 → 已完成 {detail}", ts),
    )
    return {"id": order_id, "status": "已完成", "final_image_id": final_image_id, "final_tag": final_tag, "arm": arm}


def abort_order(conn: sqlite3.Connection, *, order_id: int, reason: str) -> dict:
    """在途回滚：失败/取消同样收口延后桶，桶内事件翻为已放行的失败痕迹。"""
    order = conn.execute("SELECT * FROM start_orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ApiError("启停单不存在")
    if order["status"] != "在途":
        raise ApiError(f"启停单 {order_id} 不在在途态（当前 {order['status']}）")
    arm = get_arm(conn)
    ts = now_iso()
    note = f"在途回滚：{reason}"
    conn.execute(
        "UPDATE start_orders SET status='失败', note=?, released_at=? WHERE id=?",
        (note, ts, order_id),
    )
    conn.execute(
        "UPDATE align_bucket SET held=0, released_at=? WHERE order_id=? AND held=1",
        (ts, order_id),
    )
    _bucket_insert(
        conn,
        pod_id=int(order["pod_id"]), order_id=order_id, image_id=int(order["locked_image_id"]),
        event_type="在途回滚", arm=arm,
        tag_shown=order["locked_tag"], tag_locked=order["locked_tag"],
        tag_track=order["effective_tag"] or order["locked_tag"],
        ref_change_id=order["follow_change_id"],
        held=False,
        detail=f"启动单 {order_id} {note}",
    )
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        ("启停单", str(order_id), f"启动 → 失败 {note}", ts),
    )
    return {"id": order_id, "status": "失败", "note": note}


def retag(conn: sqlite3.Connection, *, image_id: int, new_tag: str) -> dict:
    """改标：与在途挂接同一笔立即写事务，同笔成败。"""
    img = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    if not img:
        raise ApiError("镜像不存在")
    new_tag = (new_tag or "").strip()
    if not new_tag:
        raise ApiError("新标签不能为空")
    old_tag = img["tag"]
    if new_tag == old_tag:
        raise ApiError("新标签与当前标签相同")

    arm = get_arm(conn)
    ts = now_iso()
    seq = _next_change_seq(conn)

    # 1) 镜像轨单一观感：images.tag 与 image_tags 新版本同笔推进
    ver_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM image_tags WHERE image_id=?",
        (image_id,),
    ).fetchone()
    version = int(ver_row["v"])
    conn.execute("UPDATE images SET tag=? WHERE id=?", (new_tag, image_id))
    conn.execute(
        "INSERT INTO image_tags(image_id, version, tag, updated_at) VALUES (?,?,?,?)",
        (image_id, version, new_tag, ts),
    )

    # 2) 与在途挂接
    follow_rows = conn.execute(
        "SELECT id, pod_id, locked_tag, effective_tag FROM start_orders "
        "WHERE locked_image_id=? AND status='在途' ORDER BY id",
        (image_id,),
    ).fetchall()
    follow_ids: list[int] = []

    # 先插改标戳占位，拿到自增 id 供在途单/桶行互证引用
    cur = conn.execute(
        """
        INSERT INTO tag_changes
          (change_seq, image_id, old_tag, new_tag, arm, follow_order_ids, changed_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (seq, image_id, old_tag, new_tag, arm, "", ts),
    )
    change_id = int(cur.lastrowid)

    if arm == ARM_LOCK:
        # 锁定臂：在途单一行不动，锁标仍是启动当下标签；改标戳 follow_order_ids 留空即可核
        pass
    else:
        # 跟标臂：每个在途单逐笔跟随，effective_tag/follow_change_id 同笔改写，留可核痕迹
        for r in follow_rows:
            follow_ids.append(int(r["id"]))
            conn.execute(
                "UPDATE start_orders SET effective_tag=?, follow_change_id=? WHERE id=?",
                (new_tag, change_id, int(r["id"])),
            )
            _bucket_insert(
                conn,
                pod_id=int(r["pod_id"]), order_id=int(r["id"]), image_id=image_id,
                event_type="在途跟标", arm=arm,
                tag_shown=new_tag, tag_locked=r["locked_tag"], tag_track=new_tag,
                ref_change_id=change_id, held=True,
                detail=(
                    f"改标戳 {change_id}：启动单 {r['id']} 跟随标签 "
                    f"{r['effective_tag']} → {new_tag}（锁标 {r['locked_tag']} 不动）"
                ),
            )
        conn.execute(
            "UPDATE tag_changes SET follow_order_ids=? WHERE id=?",
            (",".join(str(i) for i in follow_ids), change_id),
        )

    # 改标自身进延后桶：每笔在途单各扣一行，随该单收口放行（两臂皆然，锁定臂只扣不跟）；
    # 无在途单时写一行直接放行的账，保证改标在桶账本里不缺痕。
    if follow_rows:
        for r in follow_rows:
            _bucket_insert(
                conn,
                pod_id=int(r["pod_id"]), order_id=int(r["id"]), image_id=image_id,
                event_type="镜像改标", arm=arm,
                tag_shown=new_tag, tag_locked=r["locked_tag"], tag_track=new_tag,
                ref_change_id=change_id, held=True,
                detail=f"改标戳 {change_id}（序号 {seq}）：{old_tag} → {new_tag}，臂={arm}，随途单 {int(r['id'])}",
            )
    else:
        _bucket_insert(
            conn,
            pod_id=0, order_id=None, image_id=image_id,
            event_type="镜像改标", arm=arm,
            tag_shown=new_tag, tag_locked=None, tag_track=new_tag,
            ref_change_id=change_id, held=False,
            detail=f"改标戳 {change_id}（序号 {seq}）：{old_tag} → {new_tag}，臂={arm}，无在途单",
        )
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        ("镜像", str(image_id), f"改标 {old_tag} → {new_tag}（臂 {arm}，戳 {change_id}，随途 {follow_ids or '无'}）", ts),
    )
    return {
        "change_id": change_id, "change_seq": seq, "image_id": image_id,
        "old_tag": old_tag, "new_tag": new_tag, "arm": arm,
        "follow_order_ids": follow_ids,
    }
