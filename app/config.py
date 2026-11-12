import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me-in-production")
    DATABASE = os.environ.get(
        "DATABASE_PATH",
        str(BASE_DIR / "instance" / "crawl_lab.db"),
    )
    # 服务端日期口径；留空用系统本地时间。
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "")
    # 测试/验收固定日期用，如 "2026-11-12"。
    SERVER_DATE_OVERRIDE = os.environ.get("SERVER_DATE_OVERRIDE") or None
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
