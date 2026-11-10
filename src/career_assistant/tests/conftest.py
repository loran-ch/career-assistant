import hashlib

import pytest
from werkzeug.security import generate_password_hash

from career_assistant.app import create_app
from career_assistant.db import Database
from career_assistant.models import JobPosting
from career_assistant.store import Store

CSRF = "test-csrf-token"


@pytest.fixture()
def store(tmp_path):
    instance = Store(tmp_path / "radar.db")
    instance.migrate()
    return instance


@pytest.fixture()
def app(store, tmp_path):
    return create_app(store=store, data_dir=tmp_path / "data", testing=True)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def csrf():
    return CSRF


@pytest.fixture()
def auth_client(app, store):
    """A logged-in client; the session is seeded to keep tests focused."""
    store.create_initial_user("tester", generate_password_hash("test-password"))
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["csrf"] = CSRF
    return client


@pytest.fixture()
def resume(store, tmp_path):
    """一份已落库的简历；报告、事实版本与材料都要挂在它下面。"""
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4\n% sample\n")
    resume_id = store.add_resume("resume.pdf", path,
                                 hashlib.sha256(path.read_bytes()).hexdigest(), "样例简历原文")
    return store.get("resumes", resume_id)


@pytest.fixture()
def seed_job(store):
    """Insert one job plus its full snapshot, and return both."""
    def _seed(title="数据分析师", company="示例科技", uid="seed-1"):
        job = JobPosting("manual", uid, title, company, "", f"manual:{uid}",
                          description_snippet="负责数据分析与报表")
        db = Database(store.db_path)
        try:
            db.upsert_job(job)
        finally:
            db.close()
        canonical = store.find_job_by_url(f"manual:{uid}")
        snapshot_id = store.snapshot_job(canonical["job_uid"], "完整 JD 正文", True)
        return canonical, snapshot_id
    return _seed
