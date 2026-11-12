"""服务端日期与时间。

配额按服务端日期计算，浏览器提交的任何日期字段都不参与判定。
测试可通过 ``SERVER_DATE_OVERRIDE`` 固定/推进日期。
"""

from datetime import date, datetime, time

from flask import current_app, has_app_context


def _override():
    if not has_app_context():
        return None
    return current_app.config.get("SERVER_DATE_OVERRIDE")


def _now() -> datetime:
    override = _override()
    if override:
        if isinstance(override, datetime):
            return override
        text = str(override).strip()
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            # 只给了日期时，固定到当天中午，便于展示入队/入库时间。
            return datetime.combine(date.fromisoformat(text), time(12, 0))

    tz_name = None
    if has_app_context():
        tz_name = current_app.config.get("APP_TIMEZONE")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def server_date() -> date:
    return _now().date()


def today_str() -> str:
    return server_date().isoformat()


def now_text() -> str:
    return _now().replace(microsecond=0).isoformat(sep=" ")
