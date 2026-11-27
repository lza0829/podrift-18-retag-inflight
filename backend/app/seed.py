from __future__ import annotations
from app.db import connect, init_schema
from app.services.common import now_iso


def seed_if_empty() -> None:
    conn = connect()
    init_schema(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM pods").fetchone()["c"]
    if not n:
        _seed_fresh(conn)
        conn.commit()
        conn.close()
        return
    # 老库增量回填：新表/新设置在旧库上不存在基线时幂等补齐
    _backfill(conn)
    conn.commit()
    conn.close()


def _backfill(conn) -> None:
    ts = now_iso()
    # 镜像标签版本史基线：每个镜像从当前在轨标签起 version=1
    for img in conn.execute("SELECT id, tag FROM images").fetchall():
        exists = conn.execute(
            "SELECT 1 FROM image_tags WHERE image_id=? AND version=1", (img["id"],)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO image_tags(image_id, version, tag, updated_at) VALUES (?,?,?,?)",
                (img["id"], 1, img["tag"], ts),
            )
    # 时序臂默认锁定臂
    arm = conn.execute(
        "SELECT 1 FROM settings WHERE key='inflight_tag_arm'"
    ).fetchone()
    if not arm:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?)",
            ("inflight_tag_arm", "lock"),
        )


def _seed_fresh(conn) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO images(name, tag, pull_state, layers_done, layers_total, ready_at) VALUES (?,?,?,?,?,?)",
        ("app/core", "1.4.2", "已就绪", 4, 4, ts),
    )
    conn.execute(
        "INSERT INTO images(name, tag, pull_state, layers_done, layers_total, ready_at) VALUES (?,?,?,?,?,?)",
        ("app/worker", "0.9.1", "拉取中", 2, 4, None),
    )
    # 标签版本史与镜像轨初始标签对齐，保证单一观感
    conn.execute(
        "INSERT INTO image_tags(image_id, version, tag, updated_at) VALUES (1,1,?,?)",
        ("1.4.2", ts),
    )
    conn.execute(
        "INSERT INTO image_tags(image_id, version, tag, updated_at) VALUES (2,1,?,?)",
        ("0.9.1", ts),
    )
    conn.execute(
        "INSERT INTO pods(name, image_id, run_state, sidecar, cpu_quota, mem_quota_mb) VALUES (?,?,?,?,?,?)",
        ("web-a", 1, "已停止", "log-ship", 0.5, 256),
    )
    conn.execute(
        "INSERT INTO pods(name, image_id, run_state, sidecar, cpu_quota, mem_quota_mb) VALUES (?,?,?,?,?,?)",
        ("worker-b", 2, "已停止", None, 1.0, 512),
    )
    conn.execute(
        "INSERT INTO probes(pod_id, color, fail_streak, updated_at) VALUES (?,?,?,?)",
        (1, "绿", 0, ts),
    )
    conn.execute(
        "INSERT INTO probes(pod_id, color, fail_streak, updated_at) VALUES (?,?,?,?)",
        (2, "红", 3, ts),
    )
    conn.execute(
        "INSERT INTO quotas(id, cpu_used, cpu_limit, mem_used_mb, mem_limit_mb) VALUES (1,?,?,?,?)",
        (1.2, 4.0, 768, 2048),
    )
    conn.execute(
        "INSERT INTO restart_policy(id, mode, backoff_sec) VALUES (1,?,?)",
        ("失败重启", 8),
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?)",
        ("probe_fail_threshold", "3"),
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?)",
        ("inflight_tag_arm", "lock"),
    )
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        ("种子", "系统", "舱隙底座初始化", ts),
    )
