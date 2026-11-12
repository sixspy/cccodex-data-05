"""每日配额 + 队列 + 插队 的自动化测试（7 组）。"""

import pytest

from app.db import get_db
from app.services.quota import set_daily_quota
from app.services.runner import add_priority, run_job

ALPHA = "/mock/catalog/alpha"
BETA = "/mock/catalog/beta"
DAY1 = "2026-11-12"
DAY2 = "2026-11-13"


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_job(app, name: str, seed: str) -> int:
    client = app.test_client()
    login(client)
    client.post("/jobs", data={"name": name, "seed_path": seed}, follow_redirects=True)
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM crawl_jobs WHERE name = ?", (name,)
        ).fetchone()["id"]


def set_quota(app, seed: str, value) -> None:
    with app.app_context():
        db = get_db()
        set_daily_quota(db, seed, value)
        db.commit()


def read(app, sql: str, params=()) -> list:
    with app.app_context():
        return get_db().execute(sql, params).fetchall()


def pending_keys(app, job_id: int) -> list[str]:
    rows = read(
        app,
        """
        SELECT item_key FROM queue_entries
        WHERE job_id = ? AND status = 'pending'
        ORDER BY is_priority DESC, id
        """,
        (job_id,),
    )
    return [r["item_key"] for r in rows]


def admitted_titles(app, job_id: int) -> list[str]:
    rows = read(
        app,
        "SELECT title FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    )
    return [r["title"] for r in rows]


# 1. 超额之后只排队，不进正式结果。beta 共 6 条合法 + 1 条空编码，配额 2。
def test_over_quota_items_only_queue(app):
    set_quota(app, BETA, 2)
    job_id = create_job(app, "beta-queue", BETA)
    with app.app_context():
        summary = run_job(job_id)

    assert summary["inserted"] == 2
    assert summary["queued"] == 4  # 6 条合法里 2 条入库，4 条排队
    assert summary["invalid"] == 1  # 空编码不占配额
    assert summary["pending_left"] == 4
    assert summary["status"] == "queued"

    assert len(admitted_titles(app, job_id)) == 2
    assert len(pending_keys(app, job_id)) == 4
    # 空编码条目没有进入 pending（不参与排队放行）
    invalid = read(
        app, "SELECT sku, status FROM queue_entries WHERE job_id = ?", (job_id,)
    )
    assert any(r["status"] == "invalid" and r["sku"] == "" for r in invalid)


# 2. 同一天继续多次也不会超过配额。
def test_same_day_never_exceeds_quota(app, client):
    set_quota(app, BETA, 3)
    job_id = create_job(app, "beta-cap", BETA)
    login(client)

    for _ in range(3):
        with app.app_context():
            summary = run_job(job_id)
        total = read(
            app,
            "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ?",
            (job_id,),
        )[0]["c"]
        assert total <= 3

    assert read(app, "SELECT COUNT(*) AS c FROM crawl_records")[0]["c"] == 3
    # 配额用尽后继续仍被允许，但只处理队列且不放行
    with app.app_context():
        summary = run_job(job_id)
    assert summary["inserted"] == 0
    assert summary["pending_left"] == 3


# 3. 换日之后排队不会自动入库，必须手工继续，且受新一天配额限制。
def test_rollover_requires_manual_continue(app, set_day):
    set_quota(app, BETA, 2)
    job_id = create_job(app, "beta-rollover", BETA)
    with app.app_context():
        run_job(job_id)
    assert len(admitted_titles(app, job_id)) == 2

    # 仅把服务端日期推到第二天：队列仍保持 pending
    set_day(DAY2)
    assert len(pending_keys(app, job_id)) == 4
    assert len(admitted_titles(app, job_id)) == 2

    # 手工继续：新一天配额 2 → 再入库 2 条，仍剩 2 条排队
    with app.app_context():
        summary = run_job(job_id)
    assert summary["inserted"] == 2
    assert summary["pending_left"] == 2
    assert len(admitted_titles(app, job_id)) == 4

    # 同一天再继续：配额已用完
    with app.app_context():
        summary = run_job(job_id)
    assert summary["inserted"] == 0
    assert summary["pending_left"] == 2


# 4. 插队先于普通排队。
def test_priority_runs_before_normal_queue(app, set_day):
    set_quota(app, BETA, 2)
    job_id = create_job(app, "beta-priority", BETA)
    with app.app_context():
        run_job(job_id)
    before = pending_keys(app, job_id)
    assert len(before) == 4

    # 挑队列里的第 3 条（B-105 用户投诉回访单）插队
    with app.app_context():
        add_priority(job_id, "B-105", "客户催办")
    after = pending_keys(app, job_id)
    assert after[0] == before[2]
    assert after[0].endswith("用户投诉回访单")

    # 换日继续：插队条目先入库，随后才按排队顺序放行
    set_day(DAY2)
    with app.app_context():
        summary = run_job(job_id)
    titles = read(
        app,
        """
        SELECT r.title FROM crawl_records r
        WHERE r.job_id = ? AND r.admitted_date = ?
        ORDER BY r.id
        """,
        (job_id, DAY2),
    )
    assert [r["title"] for r in titles][0] == "用户投诉回访单"
    assert summary["inserted"] == 2
    assert pending_keys(app, job_id)[0] != after[0]


# 5. 空编码不能插队，也不能占用配额。
def test_blank_sku_cannot_jump(app):
    set_quota(app, BETA, 6)
    job_id = create_job(app, "beta-blank", BETA)

    with pytest.raises(ValueError, match="空"):
        with app.app_context():
            add_priority(job_id, "", "临时插一条")

    with app.app_context():
        summary = run_job(job_id)
    # 6 条合法全部在配额内入库；空编码条目被标 invalid，不计入入库数
    assert summary["inserted"] == 6
    assert summary["invalid"] == 1
    assert summary["pending_left"] == 0
    assert summary["status"] == "done"
    assert "未编号的临时记录" not in admitted_titles(app, job_id)
    row = read(
        app,
        "SELECT status FROM queue_entries WHERE job_id = ? AND sku = ''",
        (job_id,),
    )
    assert row[0]["status"] == "invalid"


# 6. 同一任务已有插队时，再插一条会被拒绝。
def test_second_priority_rejected(app):
    set_quota(app, BETA, 1)
    job_id = create_job(app, "beta-one-priority", BETA)
    with app.app_context():
        run_job(job_id)
        add_priority(job_id, "B-103", "首条插队")

    with pytest.raises(ValueError, match="只能有一条插队"):
        with app.app_context():
            add_priority(job_id, "B-105", "第二条插队")

    pending = read(
        app,
        """
        SELECT item_key FROM queue_entries
        WHERE job_id = ? AND status = 'pending' AND is_priority = 1
        """,
        (job_id,),
    )
    assert len(pending) == 1


# 7. 本次运行尚未结束时不能再次触发。
def test_reject_trigger_while_running(app):
    job_id = create_job(app, "beta-running", BETA)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE crawl_jobs SET status = 'running' WHERE id = ?", (job_id,))
        db.commit()
        with pytest.raises(ValueError, match="尚未结束"):
            run_job(job_id)

    # 路由层同样拒绝，并以 flash 提示
    client = app.test_client()
    login(client)
    response = client.post(f"/jobs/{job_id}/run", follow_redirects=True)
    assert "尚未结束" in response.get_data(as_text=True)


def test_list_shows_quota_queue_and_priority(app, client):
    set_quota(app, BETA, 5)
    job_id = create_job(app, "beta-view", BETA)
    login(client)
    client.post(f"/jobs/{job_id}/run", follow_redirects=True)

    body = client.get("/").get_data(as_text=True)
    assert "每日配额" in body
    assert "目录今日入库 / 配额" in body
    assert "本任务排队" in body
    assert "插队" in body
    # 今日 5 条入库 / 配额 5
    assert "5 / 5" in body

    detail = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "目录今日已入库" in detail
    assert "当日配额" in detail
    assert "排队顺序" in detail


def test_detail_queue_order_columns(app, client):
    """浏览器看排队顺序：插队固定为 1，其余按入队先后递增。"""
    set_quota(app, BETA, 2)
    job_id = create_job(app, "beta-order", BETA)
    login(client)
    client.post(f"/jobs/{job_id}/run", follow_redirects=True)
    client.post(
        f"/jobs/{job_id}/priority",
        data={"sku": "B-105", "reason": "客户催办"},
        follow_redirects=True,
    )

    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert 'class="priority-row"' in body
    assert "插队：客户催办" in body
    assert "<td>1</td>" in body  # 插队排第 1
    assert "<td>2</td>" in body  # 其余普通条目紧随
