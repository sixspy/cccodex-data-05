"""任务执行：拉取 mock 目录 → 按服务端日期配额入库 → 超额进队列。

规则：
- 合法条目（编码非空）在当天配额内直接进正式结果，超额进排队。
- 编码为空的条目不进正式结果、不占配额，也不允许插队。
- 继续时先入库插队条目，再按排队先后（自增 id 升序）处理普通条目。
"""

import json
import sqlite3

from flask import current_app

from app.db import db_transaction, get_db
from app.services.clock import now_text, today_str
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page
from app.services.quota import get_daily_quota, today_admitted_count


def _load_job(db, job_id: int):
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("任务不存在")
    return job


def _catalog_items(job) -> list[dict]:
    html = render_catalog_page(job["seed_path"])
    if isinstance(html, tuple):
        html = html[0]
    return parse_catalog_html(html, job["seed_path"])


def _enqueue_source(db, job, day: str) -> dict:
    """把目录中新出现的条目入队；已处理过的条目不再重复入队。"""
    inserted = 0
    skipped_dup = 0
    invalid = 0
    seen_in_batch: set[str] = set()
    for it in _catalog_items(job):
        key = normalize_item_key(job["seed_path"], it["title"])
        if key in seen_in_batch:
            skipped_dup += 1
            continue
        seen_in_batch.add(key)

        if db.execute(
            """
            SELECT 1 FROM crawl_records WHERE job_id = ? AND item_key = ?
            UNION ALL
            SELECT 1 FROM queue_entries WHERE job_id = ? AND item_key = ?
            LIMIT 1
            """,
            (job["id"], key, job["id"], key),
        ).fetchone():
            continue

        sku = (it.get("sku") or "").strip()
        status = "pending" if sku else "invalid"
        db.execute(
            """
            INSERT INTO queue_entries
                (job_id, item_key, sku, title, payload_json, status,
                 is_priority, queued_at, queued_date)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                job["id"],
                key,
                sku,
                it["title"],
                json.dumps(it, ensure_ascii=False),
                status,
                now_text(),
                day,
            ),
        )
        if status == "invalid":
            invalid += 1
        else:
            inserted += 1
    return {"queued": inserted, "skipped_dup": skipped_dup, "invalid": invalid}


def _admit_pending(db, job, day: str) -> dict:
    """按「插队优先 → 队列先后」入库，直到当天配额用完。"""
    quota = get_daily_quota(db, job["seed_path"])
    used = today_admitted_count(db, job["seed_path"], day)
    remaining = max(0, quota - used)

    admitted = 0
    skipped_dup = 0
    invalid = 0

    # 空编码条目任何情况下都不放行、不占配额，先统一作废。
    db.execute(
        """
        UPDATE queue_entries SET status = 'invalid'
        WHERE job_id = ? AND status = 'pending' AND TRIM(COALESCE(sku, '')) = ''
        """,
        (job["id"],),
    )

    pending = db.execute(
        """
        SELECT * FROM queue_entries
        WHERE job_id = ? AND status = 'pending'
        ORDER BY is_priority DESC, id ASC
        """,
        (job["id"],),
    ).fetchall()

    for entry in pending:
        if remaining <= 0:
            break
        if not (entry["sku"] or "").strip():
            # 上面的批量作废已保证不会走到这里；仅作防御。
            db.execute(
                "UPDATE queue_entries SET status = 'invalid' WHERE id = ?",
                (entry["id"],),
            )
            invalid += 1
            continue
        try:
            db.execute(
                """
                INSERT INTO crawl_records
                    (job_id, item_key, title, payload_json, fetched_at, admitted_date)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"],
                    entry["item_key"],
                    entry["title"],
                    entry["payload_json"],
                    now_text(),
                    day,
                ),
            )
        except sqlite3.IntegrityError:
            db.execute(
                "UPDATE queue_entries SET status = 'duplicate' WHERE id = ?",
                (entry["id"],),
            )
            skipped_dup += 1
            continue

        db.execute(
            """
            UPDATE queue_entries
            SET status = 'admitted', admitted_at = ?, admitted_date = ?
            WHERE id = ?
            """,
            (now_text(), day, entry["id"]),
        )
        admitted += 1
        remaining -= 1

    return {"admitted": admitted, "skipped_dup": skipped_dup, "invalid": invalid}


def job_pending_count(db, job_id: int, status: str = "pending") -> int:
    row = db.execute(
        "SELECT COUNT(*) AS c FROM queue_entries WHERE job_id = ? AND status = ?",
        (job_id, status),
    ).fetchone()
    return int(row["c"])


def job_has_priority(db, job_id: int) -> bool:
    row = db.execute(
        """
        SELECT COUNT(*) AS c FROM queue_entries
        WHERE job_id = ? AND status = 'pending' AND is_priority = 1
        """,
        (job_id,),
    ).fetchone()
    return int(row["c"]) > 0


def run_job(job_id: int) -> dict:
    db = get_db()
    job = _load_job(db, job_id)

    if job["status"] == "running":
        raise ValueError("本次运行尚未结束，不能再次触发")
    if job["status"] == "done" and job_pending_count(db, job_id) == 0:
        raise ValueError("任务已完成，没有待处理条目")

    seed = job["seed_path"]
    if seed not in CATALOGS:
        db.execute(
            "UPDATE crawl_jobs SET status = 'failed', finished_at = ? WHERE id = ?",
            (now_text(), job_id),
        )
        db.commit()
        return {
            "status": "failed",
            "reason": "unknown seed",
            "day": today_str(),
            "quota": 0,
            "inserted": 0,
            "queued": 0,
            "pending_left": job_pending_count(db, job_id),
            "skipped_dup": 0,
            "invalid": 0,
            "total_source": 0,
        }

    day = today_str()

    # 原子占用运行权：先落 running 再干活，同步 runner 期间的重复触发会在这里被挡住。
    with db_transaction() as tx:
        claimed = tx.execute(
            """
            UPDATE crawl_jobs SET status = 'running', finished_at = NULL
            WHERE id = ? AND status != 'running'
            """,
            (job_id,),
        ).rowcount
    if claimed == 0:
        raise ValueError("本次运行尚未结束，不能再次触发")
    pending_before = job_pending_count(db, job_id)

    try:
        with db_transaction() as tx:
            enq = _enqueue_source(tx, job, day)
            admitted = _admit_pending(tx, job, day)
            pending_left = job_pending_count(tx, job_id)
            tx.execute(
                "UPDATE crawl_jobs SET status = ?, finished_at = ? WHERE id = ?",
                ("queued" if pending_left else "done", now_text(), job_id),
            )
    except Exception:
        db.execute(
            "UPDATE crawl_jobs SET status = 'failed', finished_at = ? WHERE id = ?",
            (now_text(), job_id),
        )
        db.commit()
        raise

    total_source = len(_catalog_items(job))
    current_app.logger.info(
        "job %s day=%s admitted=%s queued=%s pending_left=%s dup=%s invalid=%s",
        job_id,
        day,
        admitted["admitted"],
        enq["queued"],
        pending_left,
        admitted["skipped_dup"] + enq["skipped_dup"],
        admitted["invalid"] + enq["invalid"],
    )
    return {
        "status": "queued" if pending_left else "done",
        "day": day,
        "quota": get_daily_quota(db, seed),
        "inserted": admitted["admitted"],
        # 本次新入队且仍在排队的合法条目数（已放行的不计入）。
        "queued": max(0, pending_left - pending_before),
        "pending_left": pending_left,
        "skipped_dup": admitted["skipped_dup"] + enq["skipped_dup"],
        "invalid": admitted["invalid"] + enq["invalid"],
        "total_source": total_source,
    }


def add_priority(job_id: int, sku: str, reason: str) -> dict:
    """为任务登记一条插队；同一时刻每个任务只允许一条。"""
    reason = (reason or "").strip()
    sku = (sku or "").strip()

    if not sku:
        raise ValueError("编码为空的条目不能插队")
    if not reason:
        raise ValueError("插队必须填写原因")

    db = get_db()
    job = _load_job(db, job_id)
    if job["status"] == "running":
        raise ValueError("本次运行尚未结束，不能插队")

    if job_has_priority(db, job_id):
        raise ValueError("同一任务同一时刻只能有一条插队，请先处理已插队的那条")

    items = _catalog_items(job)
    item = next((it for it in items if (it.get("sku") or "").strip() == sku), None)
    if item is None:
        raise ValueError("该编码不在当前目录中")

    key = normalize_item_key(job["seed_path"], item["title"])
    if db.execute(
        "SELECT 1 FROM crawl_records WHERE job_id = ? AND item_key = ?",
        (job_id, key),
    ).fetchone():
        raise ValueError("该条目已入库，无需插队")

    existing = db.execute(
        "SELECT * FROM queue_entries WHERE job_id = ? AND item_key = ?",
        (job_id, key),
    ).fetchone()
    if existing is None:
        db.execute(
            """
            INSERT INTO queue_entries
                (job_id, item_key, sku, title, payload_json, status,
                 is_priority, priority_reason, priority_at, queued_at, queued_date)
            VALUES (?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?, ?)
            """,
            (
                job_id,
                key,
                sku,
                item["title"],
                json.dumps(item, ensure_ascii=False),
                reason,
                now_text(),
                now_text(),
                today_str(),
            ),
        )
    elif existing["status"] == "pending":
        db.execute(
            """
            UPDATE queue_entries
            SET is_priority = 1, priority_reason = ?, priority_at = ?
            WHERE id = ?
            """,
            (reason, now_text(), existing["id"]),
        )
    else:
        raise ValueError("该条目已处理，无法插队")

    db.commit()
    return {"sku": sku, "title": item["title"], "reason": reason}
