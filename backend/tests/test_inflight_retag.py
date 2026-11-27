"""在途改标语义夹具。

四类题面夹具，每类都在 lock / track 两臂下分别断言，两臂结论可分开验：
  1. 启动在途再改标
  2. 先改标再启动
  3. 改标与启动并发
  4. 半截脏标签回滚
并要求改标戳(tag_changes)、在途启动事务(start_orders)、对读屏延后桶(align_bucket) 三方互证。
"""
from __future__ import annotations
import os
import sys
import tempfile
import threading

import pytest

# 文件库 + WAL + busy_timeout：并发夹具要靠 BEGIN IMMEDIATE 在写锁上排队，
# 共享内存库不支持表级锁等待，故必须用文件库。
# 沙箱磁盘 DDL 约 0.1s/条，因此 schema 会话内只建一次，夹具间清表而非重建。
_FH = tempfile.NamedTemporaryFile(suffix=".db", dir="/tmp", delete=False)
_FH.close()
os.environ["PODRIFT_DB"] = _FH.name
os.environ.pop("PODRIFT_MEMORY_DB", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import connect, init_schema  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services.inflight import ARM_LOCK, ARM_TRACK  # noqa: E402

_TABLES = (
    "align_bucket", "tag_changes", "image_tags", "events", "start_orders",
    "probes", "quotas", "restart_policy", "settings", "pods", "images",
)


@pytest.fixture(scope="session", autouse=True)
def _schema_once():
    # 常驻连接保活：共享内存库在最后一个连接关闭时会整体销毁
    holder = connect()
    init_schema(holder)
    yield
    holder.close()


@pytest.fixture()
def client():
    # 每次夹具清表重灌种子；共享内存库连接串固定，应用侧与断言侧看到同一份数据
    conn = connect()
    for t in _TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    conn.close()
    seed_if_empty()
    with TestClient(app) as c:
        yield c


def set_arm(client, arm: str):
    r = client.post("/api/时序臂", json={"arm": arm})
    assert r.status_code == 200 and r.json().get("arm") == arm
    return arm


def start_inflight(client, pod_id=1, target_image_id=None):
    body = {"pod_id": pod_id, "action": "启动"}
    if target_image_id is not None:
        body["target_image_id"] = target_image_id
    r = client.post("/api/启停单", json=body)
    data = r.json()
    assert data["status"] == "在途", data
    return data


def retag(client, image_id, new_tag):
    r = client.post(f"/api/镜像/{image_id}/改标", json={"tag": new_tag})
    data = r.json()
    assert "change_id" in data, data
    return data


def settle(client, order_id):
    r = client.post(f"/api/启停单/{order_id}/收口")
    data = r.json()
    assert data["status"] == "已完成", data
    return data


def rollback(client, order_id, reason="夹具回滚"):
    r = client.post(f"/api/启停单/{order_id}/回滚", json={"reason": reason})
    data = r.json()
    assert data["status"] == "失败", data
    return data


def _fresh_read():
    # 读未提交：断言连接不持有 WAL 旧快照，避免并发用例读到改标事务提交前的页面
    conn = connect()
    conn.execute("PRAGMA read_uncommitted = 1")
    return conn


def order_row(order_id):
    conn = _fresh_read()
    row = conn.execute("SELECT * FROM start_orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    return dict(row)


def image_row(image_id):
    conn = _fresh_read()
    row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    conn.close()
    return dict(row)


def bucket_rows(order_id=None, held_only=False):
    conn = _fresh_read()
    sql = "SELECT * FROM align_bucket"
    cond, params = [], []
    if order_id is not None:
        cond.append("order_id=?")
        params.append(order_id)
    if held_only:
        cond.append("held=1")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY seq"
    out = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return out


def change_rows(image_id):
    conn = _fresh_read()
    out = [dict(r) for r in conn.execute(
        "SELECT * FROM tag_changes WHERE image_id=? ORDER BY change_seq", (image_id,)
    ).fetchall()]
    conn.close()
    # 与 API 序列化对齐：存储的逗号串解析为 id 列表
    for r in out:
        raw = r["follow_order_ids"]
        r["follow_order_ids"] = [int(x) for x in raw.split(",")] if raw else []
    return out


# ---------- 夹具一：启动在途 → 改标，两臂结论必须分开 ----------

@pytest.mark.parametrize("arm", [ARM_LOCK, ARM_TRACK])
def test_1_start_inflight_then_retag(client, arm):
    set_arm(client, arm)
    # 种子镜像1 app/core:1.4.2 已就绪，舱体 web-a 挂它
    o = start_inflight(client, pod_id=1)
    oid = o["id"]
    assert o["locked_tag"] == "1.4.2"

    chg = retag(client, 1, "1.5.0")
    od = order_row(oid)
    rail = image_row(1)
    held = bucket_rows(order_id=oid, held_only=True)

    # 镜像轨始终单一观感：轨上标签就是新标，不存在双标签
    assert rail["tag"] == "1.5.0"
    # 改标戳与在途事务互证
    assert chg["old_tag"] == "1.4.2" and chg["new_tag"] == "1.5.0" and chg["arm"] == arm

    if arm == ARM_LOCK:
        # 锁定臂：锁标钉死，改标戳不得挂接在途单
        assert od["locked_tag"] == "1.4.2"
        assert od["effective_tag"] == "1.4.2"
        assert od["follow_change_id"] is None
        assert chg["follow_order_ids"] == []
        # 桶内在途行读屏认锁标
        assert all(r["tag_locked"] == "1.4.2" for r in held)
    else:
        # 跟标臂：在途单跟随新标，且跟标动作双向留痕
        assert od["locked_tag"] == "1.4.2"  # 锁标快照仍在，但不再是落成依据
        assert od["effective_tag"] == "1.5.0"
        assert od["follow_change_id"] == chg["change_id"]
        assert chg["follow_order_ids"] == [oid]
        follow = [r for r in held if r["event_type"] == "在途跟标"]
        assert len(follow) == 1
        assert follow[0]["tag_track"] == "1.5.0" and follow[0]["tag_locked"] == "1.4.2"
        assert follow[0]["ref_change_id"] == chg["change_id"]

    # 对读屏在途期间：延后桶在扣，读屏认标随臂
    view = client.get("/api/对读").json()
    proj = next(p for p in view["舱体投影"] if p["id"] == 1)
    assert proj["inflight"] is True and proj["延后桶"] == "扣住中"
    expected = "1.4.2" if arm == ARM_LOCK else "1.5.0"
    assert proj["读屏认标"] == expected
    assert view["延后桶计数"]["held_count"] >= 1

    # 收口
    done = settle(client, oid)
    od2 = order_row(oid)
    if arm == ARM_LOCK:
        assert done["final_tag"] == "1.4.2"  # 标已改、在途仍落旧标——锁定臂的可验结论
        assert od2["target_image_id"] == 1 and od2["effective_tag"] == "1.4.2"
    else:
        assert done["final_tag"] == "1.5.0"  # 跟标臂落到轨上最新
        assert od2["effective_tag"] == "1.5.0" and od2["follow_change_id"] == chg["change_id"]

    # 收口后桶全部放行，行不删（可重放），读屏回到在轨标签
    assert all(r["held"] == 0 and r["released_at"] for r in bucket_rows(order_id=oid))
    view2 = client.get("/api/对读").json()
    proj2 = next(p for p in view2["舱体投影"] if p["id"] == 1)
    assert proj2["延后桶"] == "已放行"
    assert view2["延后桶计数"]["held_count"] == 0


# ---------- 夹具二：先改标 → 再启动，两臂都应认新标 ----------

@pytest.mark.parametrize("arm", [ARM_LOCK, ARM_TRACK])
def test_2_retag_before_start(client, arm):
    set_arm(client, arm)
    chg = retag(client, 1, "2.0.0")
    # 改标时无在途单：戳上 follow_order_ids 必须为空
    assert chg["follow_order_ids"] == []

    o = start_inflight(client, pod_id=1)
    # 点启动当下快照的就是轨上最新标
    assert o["locked_tag"] == "2.0.0"
    od = order_row(o["id"])
    assert od["locked_tag"] == "2.0.0" and od["effective_tag"] == "2.0.0"

    done = settle(client, o["id"])
    assert done["final_tag"] == "2.0.0"
    # 桶内没有任何在途跟标行（改标早于在途，无单可跟）
    assert not [r for r in bucket_rows() if r["event_type"] == "在途跟标"]


# ---------- 夹具三：改标与点启动并发，只能串行同笔成败，无半截脏标签 ----------

@pytest.mark.parametrize("arm", [ARM_LOCK, ARM_TRACK])
def test_3_concurrent_retag_and_start(client, arm):
    set_arm(client, arm)
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}

    def do_start():
        barrier.wait()
        r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
        results["start"] = r.json()

    def do_retag():
        barrier.wait()
        r = client.post("/api/镜像/1/改标", json={"tag": "9.9.9"})
        results["retag"] = r.json()

    t1 = threading.Thread(target=do_start)
    t2 = threading.Thread(target=do_retag)
    t1.start(); t2.start(); t1.join(); t2.join()

    # 两笔都成功提交，但 BEGIN IMMEDIATE 让它们严格串行：标签快照必为二者之一，无中间态
    assert results["retag"]["new_tag"] == "9.9.9"
    assert results["start"]["status"] == "在途"
    snap = results["start"]["locked_tag"]
    assert snap in ("1.4.2", "9.9.9")  # 不会出现半截/混合标签

    rail = image_row(1)
    assert rail["tag"] == "9.9.9"  # 改标完整生效，轨面唯一

    oid = results["start"]["id"]
    od = order_row(oid)
    chg = change_rows(1)[0]
    if snap == "1.4.2":
        # 启动先拿锁：改标落在在途之后
        if arm == ARM_LOCK:
            assert od["effective_tag"] == "1.4.2" and chg["follow_order_ids"] == []
        else:
            assert od["effective_tag"] == "9.9.9" and chg["follow_order_ids"] == [oid]
    else:
        # 改标先提交：启动快照即新标，改标戳无随途单
        assert od["locked_tag"] == "9.9.9"
        assert chg["follow_order_ids"] == []

    # 重复在途启动必须整笔冲突失败，且不得写出第二张在途单/脏桶
    r2 = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    assert r2.json().get("冲突") is True
    conn = _fresh_read()
    n_inflight = conn.execute(
        "SELECT COUNT(*) c FROM start_orders WHERE pod_id=1 AND status='在途'"
    ).fetchone()["c"]
    conn.close()
    assert n_inflight == 1


# ---------- 夹具四：半截脏标签 → 在途回滚，桶放行、戳与单互证 ----------

@pytest.mark.parametrize("arm", [ARM_LOCK, ARM_TRACK])
def test_4_dirty_tag_rollback(client, arm):
    set_arm(client, arm)
    o = start_inflight(client, pod_id=1)
    oid = o["id"]
    chg = retag(client, 1, "3.1.0")
    assert bucket_rows(order_id=oid, held_only=True), "在途期间桶必须扣住"

    rb = rollback(client, oid, reason="模拟改标与启动并发半截脏标签")
    od = order_row(oid)
    assert od["status"] == "失败" and od["released_at"]
    assert "回滚" in od["note"]

    # 桶：在途行全部放行且保留，追加一行在途回滚，账本可重放
    rows = bucket_rows(order_id=oid)
    assert all(r["held"] == 0 for r in rows)
    assert any(r["event_type"] == "在途回滚" for r in rows)
    # 舱体未被落成运行、配额未被扣
    conn = _fresh_read()
    pod = conn.execute("SELECT run_state FROM pods WHERE id=1").fetchone()
    quota = conn.execute("SELECT cpu_used, mem_used_mb FROM quotas WHERE id=1").fetchone()
    conn.close()
    assert pod["run_state"] == "已停止"
    assert quota["cpu_used"] == 1.2 and quota["mem_used_mb"] == 768

    # 改标戳仍可核：锁定臂无随途，跟标臂挂过随途，回滚不抹痕
    chg_rows = change_rows(1)
    assert chg_rows[0]["id"] == chg["change_id"]
    if arm == ARM_TRACK:
        assert chg_rows[0]["follow_order_ids"] == [oid]

    # 已收口单不能二次回滚/收口
    again = client.post(f"/api/启停单/{oid}/回滚", json={"reason": "x"})
    assert "错误" in again.json()


# ---------- 三戳互证：改标戳 × 在途启动事务 × 延后桶 ----------

@pytest.mark.parametrize("arm", [ARM_LOCK, ARM_TRACK])
def test_5_cross_evidence_triangulation(client, arm):
    set_arm(client, arm)
    o = start_inflight(client, pod_id=1)
    oid = o["id"]
    chg = retag(client, 1, "7.7.7")
    settle(client, oid)

    conn = _fresh_read()
    tc = dict(conn.execute("SELECT * FROM tag_changes WHERE id=?", (chg["change_id"],)).fetchone())
    so = dict(conn.execute("SELECT * FROM start_orders WHERE id=?", (oid,)).fetchone())
    bucket = [dict(r) for r in conn.execute(
        "SELECT * FROM align_bucket WHERE order_id=? ORDER BY seq", (oid,)
    ).fetchall()]
    conn.close()

    # 改标戳 → 在途单：跟标臂双向引用，锁定臂显式不挂
    if arm == ARM_TRACK:
        assert tc["follow_order_ids"] == str(oid)
        assert so["follow_change_id"] == tc["id"]
        assert so["effective_tag"] == "7.7.7"
    else:
        assert tc["follow_order_ids"] == ""
        assert so["follow_change_id"] is None
        assert so["effective_tag"] == "1.4.2"
    # 在途单 → 桶：每行带 order_id，锁标列恒等于启动快照
    assert bucket and all(b["order_id"] == oid for b in bucket)
    assert all(b["tag_locked"] == "1.4.2" for b in bucket)
    # 改标戳 → 桶：跟标/改标行 ref_change_id 指回戳 id
    marked = [b for b in bucket if b["event_type"] in ("在途跟标", "镜像改标")]
    assert marked and all(b["ref_change_id"] == tc["id"] for b in marked)
    # 桶只追加：收口只翻 held，行数与事件数一致
    assert len(bucket) == len({b["seq"] for b in bucket})


# ---------- 可重放：同一账本，两臂认标结论各自独立，不混站 ----------

def test_6_replay_separates_arms(client):
    # 在跟标臂下产生「在途 → 改标 → 收口」账本
    set_arm(client, ARM_TRACK)
    o = start_inflight(client, pod_id=1)
    retag(client, 1, "5.5.5")
    settle(client, o["id"])

    replay = client.get("/api/对读/重放").json()
    ledger = replay["账本"]
    # 每一行同时给出两臂重放认标：锁标列始终旧标，跟标列在改标后变新标
    for r in ledger:
        if r["event_type"] == "启动受理":
            assert r["重放认标_锁定臂"] == "1.4.2" and r["重放认标_跟标臂"] == "1.4.2"
    follow = [r for r in ledger if r["event_type"] == "在途跟标"][0]
    assert follow["重放认标_锁定臂"] == "1.4.2"   # 锁定臂重放仍认启动当下标
    assert follow["重放认标_跟标臂"] == "5.5.5"   # 跟标臂重放认新标
    # 截到改标之前重放，结果确定性可复现
    head = client.get(f"/api/对读/重放?upto_seq={follow['seq'] - 1}").json()
    assert all(r["重放认标_跟标臂"] != "5.5.5" for r in head["账本"])


# ---------- 同笔成败：改标中途失败必须整笔回滚，无半截脏标签 ----------

def test_7_retag_atomic_no_dirty_write(client):
    set_arm(client, ARM_TRACK)
    start_inflight(client, pod_id=1)
    before = image_row(1)["tag"]
    # 空标签 / 同标签都必须被拒，且库内无改标戳、无桶垃圾、在途单不被改
    for bad in ("", "1.4.2"):
        r = client.post("/api/镜像/1/改标", json={"tag": bad})
        assert "错误" in r.json()
    conn = _fresh_read()
    assert conn.execute("SELECT COUNT(*) c FROM tag_changes").fetchone()["c"] == 0
    so = dict(conn.execute(
        "SELECT effective_tag, follow_change_id FROM start_orders WHERE status='在途'"
    ).fetchone())
    conn.close()
    assert so["effective_tag"] == before and so["follow_change_id"] is None
    assert image_row(1)["tag"] == before


# ---------- 异常路径：ApiError 穿透事务整笔回滚，无脏写/脏单/脏桶 ----------

@pytest.mark.parametrize("arm", [ARM_LOCK, ARM_TRACK])
def test_8_api_error_rolls_back_whole_tx(client, arm):
    set_arm(client, arm)
    o = start_inflight(client, pod_id=1)
    oid = o["id"]

    # 在途未收口时停止必须冲突失败，且不得写出停止单、不得改动舱体
    r_stop = client.post("/api/启停单", json={"pod_id": 1, "action": "停止"})
    assert r_stop.json().get("冲突") is True
    conn = _fresh_read()
    stop_n = conn.execute(
        "SELECT COUNT(*) c FROM start_orders WHERE pod_id=1 AND action='停止'"
    ).fetchone()["c"]
    pod_state = conn.execute("SELECT run_state FROM pods WHERE id=1").fetchone()["run_state"]
    conn.close()
    assert stop_n == 0
    assert pod_state == "已停止"

    # 对不存在的单收口/回滚：报错且无副作用
    assert "错误" in client.post("/api/启停单/9999/收口").json()
    assert "错误" in client.post("/api/启停单/9999/回滚", json={"reason": "x"}).json()

    # 正常收口后，二次收口与二次回滚都必须报错，且已落状态不被改写
    settle(client, oid)
    again_settle = client.post(f"/api/启停单/{oid}/收口").json()
    again_rollback = client.post(f"/api/启停单/{oid}/回滚", json={"reason": "x"}).json()
    assert "错误" in again_settle and "错误" in again_rollback
    od = order_row(oid)
    assert od["status"] == "已完成"  # 没有被二次回滚改成失败
    # 桶已全部放行，没有新增在扣行
    assert bucket_rows(held_only=True) == []

    # 收口后的舱体不能再受理第二笔启动失败之外的脏数据：先回滚再验证计数稳定
    bucket_total_before = len(bucket_rows())
    bad = client.post("/api/镜像/1/改标", json={"tag": ""}).json()
    assert "错误" in bad
    assert len(bucket_rows()) == bucket_total_before  # 失败改标未往桶里塞垃圾


# ---------- 多在途单挂同一镜像：改标桶行逐单挂接，分别收口后桶必清零、无泄漏 ----------

@pytest.mark.parametrize("arm", [ARM_LOCK, ARM_TRACK])
def test_9_two_inflight_orders_same_image(client, arm):
    set_arm(client, arm)
    # 两个舱体的在途启动都指向镜像1（舱2默认镜像未就绪，显式指定）
    o1 = start_inflight(client, pod_id=1)
    o2 = start_inflight(client, pod_id=2, target_image_id=1)
    id1, id2 = o1["id"], o2["id"]

    chg = retag(client, 1, "4.4.4")
    # 改标桶行必须逐单挂接：每个在途单都能在桶里找到带自己 order_id 的改标行
    for oid in (id1, id2):
        rows = [r for r in bucket_rows(order_id=oid) if r["event_type"] == "镜像改标"]
        assert rows and all(r["held"] == 1 for r in rows)
        assert all(r["ref_change_id"] == chg["change_id"] for r in rows)

    if arm == ARM_TRACK:
        assert chg["follow_order_ids"] == [id1, id2]
        assert order_row(id1)["effective_tag"] == "4.4.4"
        assert order_row(id2)["effective_tag"] == "4.4.4"
    else:
        assert chg["follow_order_ids"] == []
        assert order_row(id1)["effective_tag"] == "1.4.2"

    # 先收口第一笔：只放行该单的桶行，第二笔仍在扣
    settle(client, id1)
    assert bucket_rows(order_id=id1, held_only=True) == []
    held_2 = bucket_rows(order_id=id2, held_only=True)
    assert held_2, "第二笔在途单的桶行不应被第一笔收口误放行"

    # 再收口第二笔：全桶清零，无遗留 held 行
    settle(client, id2)
    assert bucket_rows(held_only=True) == []
    # 两舱都落成，各自最终标签符合臂语义
    conn = _fresh_read()
    pods = {r["id"]: dict(r) for r in conn.execute("SELECT id, run_state, image_id FROM pods").fetchall()}
    conn.close()
    assert pods[1]["run_state"] == "运行中" and pods[2]["run_state"] == "运行中"
    expect_tag = "1.4.2" if arm == ARM_LOCK else "4.4.4"
    assert order_row(id1)["effective_tag"] == expect_tag
    assert order_row(id2)["effective_tag"] == expect_tag

