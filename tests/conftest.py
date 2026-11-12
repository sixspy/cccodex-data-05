import os
import tempfile

import pytest

from app import create_app
from app.config import Config


class TestConfig(Config):
    TESTING = True
    SERVER_DATE_OVERRIDE = "2026-11-12"
    APP_TIMEZONE = ""


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    TestConfig.DATABASE = path
    application = create_app(TestConfig)
    with application.app_context():
        from app.db import init_db

        init_db(application)
    yield application
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def set_day(app):
    def _set(day: str):
        app.config["SERVER_DATE_OVERRIDE"] = day

    return _set
