"""站内目录的每日入库配额。

- 配额按目录（种子路径）配置，留空或从未配置时使用默认 5 条。
- 已入库条数与配额都按服务端日期统计，跨任务共享同一个目录额度。
"""

DEFAULT_DAILY_QUOTA = 5


def get_daily_quota(db, catalog_path: str) -> int:
    row = db.execute(
        "SELECT daily_quota FROM catalog_quotas WHERE catalog_id = ?",
        (catalog_path,),
    ).fetchone()
    if row is None:
        return DEFAULT_DAILY_QUOTA
    return int(row["daily_quota"])


def set_daily_quota(db, catalog_path: str, raw_value) -> int:
    """保存目录配额；空值表示恢复默认 5 条。"""
    text = "" if raw_value is None else str(raw_value).strip()
    if not text:
        db.execute("DELETE FROM catalog_quotas WHERE catalog_id = ?", (catalog_path,))
        return DEFAULT_DAILY_QUOTA

    try:
        value = int(text)
    except (TypeError, ValueError):
        raise ValueError("配额必须是非负整数") from None
    if value < 0:
        raise ValueError("配额必须是非负整数")

    db.execute(
        """
        INSERT INTO catalog_quotas (catalog_id, daily_quota) VALUES (?, ?)
        ON CONFLICT(catalog_id) DO UPDATE SET daily_quota = excluded.daily_quota
        """,
        (catalog_path, value),
    )
    return value


def today_admitted_count(db, catalog_path: str, day: str) -> int:
    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM crawl_records r
        JOIN crawl_jobs j ON j.id = r.job_id
        WHERE j.seed_path = ? AND r.admitted_date = ?
        """,
        (catalog_path, day),
    ).fetchone()
    return int(row["c"])


def catalog_queue_count(db, catalog_path: str, status: str = "pending") -> int:
    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM queue_entries q
        JOIN crawl_jobs j ON j.id = q.job_id
        WHERE j.seed_path = ? AND q.status = ?
        """,
        (catalog_path, status),
    ).fetchone()
    return int(row["c"])


def catalog_has_priority(db, catalog_path: str) -> bool:
    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM queue_entries q
        JOIN crawl_jobs j ON j.id = q.job_id
        WHERE j.seed_path = ? AND q.status = 'pending' AND q.is_priority = 1
        """,
        (catalog_path,),
    ).fetchone()
    return int(row["c"]) > 0
