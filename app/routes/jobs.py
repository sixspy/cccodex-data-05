from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import get_db
from app.routes.auth import login_required
from app.services.clock import today_str
from app.services.mock_catalog import CATALOGS
from app.services.quota import (
    catalog_has_priority,
    catalog_queue_count,
    get_daily_quota,
    set_daily_quota,
    today_admitted_count,
)
from app.services.runner import add_priority, job_has_priority, job_pending_count, run_job

bp = Blueprint("jobs", __name__)


def _catalog_stats(db, day: str) -> list[dict]:
    stats = []
    for path in sorted(CATALOGS):
        stats.append(
            {
                "path": path,
                "quota": get_daily_quota(db, path),
                "admitted": today_admitted_count(db, path, day),
                "queued": catalog_queue_count(db, path),
                "has_priority": catalog_has_priority(db, path),
            }
        )
    return stats


def _job_stats(db, job_row, day: str) -> dict:
    return {
        "quota": get_daily_quota(db, job_row["seed_path"]),
        "admitted": today_admitted_count(db, job_row["seed_path"], day),
        "queued": job_pending_count(db, job_row["id"]),
        "has_priority": job_has_priority(db, job_row["id"]),
    }


@bp.route("/")
@login_required
def list_jobs():
    db = get_db()
    day = today_str()
    jobs = db.execute("SELECT * FROM crawl_jobs ORDER BY id DESC").fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    stats = {job["id"]: _job_stats(db, job, day) for job in jobs}
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        stats=stats,
        catalogs=_catalog_stats(db, day),
        today=day,
        seeds=sorted(CATALOGS.keys()),
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status) VALUES (?, ?, 'draft')",
        (name, seed),
    )
    db.commit()
    flash("任务已创建", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/catalogs/quota")
@login_required
def update_quota():
    path = request.form.get("catalog_id", "").strip()
    if path not in CATALOGS:
        flash("目录不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    try:
        value = set_daily_quota(db, path, request.form.get("daily_quota"))
        db.commit()
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("jobs.list_jobs"))
    flash(f"{path} 每日配额已设为 {value} 条", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    try:
        summary = run_job(job_id)
        if summary["status"] == "failed":
            flash(f"运行失败：{summary.get('reason', '未知原因')}", "error")
        else:
            flash(
                "运行完成：本次入库 {inserted} 条，新排队 {queued} 条，排队待处理 "
                "{pending_left} 条（当日配额 {quota}，去重跳过 {skipped_dup}，空编码 "
                "{invalid}）".format(**summary),
                "ok",
            )
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"运行失败: {e}", "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/priority")
@login_required
def priority_view(job_id: int):
    try:
        add_priority(job_id, request.form.get("sku", ""), request.form.get("reason", ""))
        flash("插队已登记，下次继续时优先入库", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    db = get_db()
    day = today_str()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    records = db.execute(
        """
        SELECT * FROM crawl_records WHERE job_id = ?
        ORDER BY admitted_date DESC, id
        """,
        (job_id,),
    ).fetchall()
    queue = db.execute(
        """
        SELECT * FROM queue_entries WHERE job_id = ?
        ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                 is_priority DESC, id ASC
        """,
        (job_id,),
    ).fetchall()
    queue_rows = []
    rank = 0
    for entry in queue:
        if entry["status"] == "pending":
            rank += 1
            position = rank
        else:
            position = None
        queue_rows.append({"entry": entry, "position": position})

    catalog_items = CATALOGS[job["seed_path"]]
    return render_template(
        "job_detail.html",
        job=job,
        stats=_job_stats(db, job, day),
        records=records,
        queue_rows=queue_rows,
        catalog_items=catalog_items,
        today=day,
    )
