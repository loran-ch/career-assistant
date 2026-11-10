"""B 组：会话识别、登录态与越权访问的单元测试。

本项目是**单用户本机应用**（REQUIREMENTS.md §2 明确不建设多人平台），
所以"数据隔离"在 B 组里表现为下面三条边界，而不是五用户互相隔离：

1. **未登录访问**：页面一律跳登录页，API 一律回 401 JSON，不泄露任何业务数据；
2. **会话识别**：会话里的 ``user_id`` 必须在库里真实存在，指不到就当未登录处理；
3. **跨主机访问**：只有本机（localhost / 127.0.0.1）能访问，
   公开分享端点可由 ``CAREER_SHARE_ALLOWED_HOSTS`` 显式放行。

另外还覆盖 CSRF（表单与 JSON 都算）、登出失效、单账户约束与安全响应头。
"""
import pytest
from werkzeug.security import generate_password_hash

from career_assistant.share import create_report_share


CSRF_HEADER = {"X-CSRF": "test-csrf-token"}
PASSWORD = "test-password"
PASSWORD_HASH = generate_password_hash(PASSWORD)


def session_csrf(client):
    """取当前会话里的 CSRF 令牌。"""
    with client.session_transaction() as sess:
        return sess.get("csrf", "")


def make_report(store, resume_id):
    return store.save_report(resume_id, {
        "facts": [{"category": "skill", "text": "会 SQL 与 Python"}],
        "summary": "分析结论",
        "audit_items": [{"category": "技能", "status": "pass",
                          "title": "技能标注合适", "summary": "已标注",
                          "evidence": "熟悉 SQL"}],
    })


# ---------------------------------------------------------------- 未登录访问

def test_anonymous_page_is_redirected_to_login(app, store):
    """已有账户时，未登录访问页面跳到登录页，不返回任何业务内容。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    for path in ("/jobs", "/profile", "/applications", "/resumes/1", "/tasks/1"):
        response = client.get(path)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"], path


def test_anonymous_api_returns_401_json(app, store):
    """未登录调用 API 回 401 JSON，而不是跳登录页。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    for path in ("/api/v1/board", "/api/v1/applications", "/api/v1/tasks",
                 "/api/v1/jobs", "/api/v1/shares"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.get_json()["ok"] is False


def test_anonymous_upload_and_write_are_blocked(app, store, resume):
    """未登录时不能上传简历，也不能生成投递信息包版本。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    client.get("/login")
    token = session_csrf(client)
    response = client.post("/api/v1/applications/1/pack",
                           headers={"X-CSRF": token})
    assert response.status_code == 401
    assert response.get_json()["ok"] is False


def test_first_run_without_account_redirects_to_register(app):
    """一台机器还没有账户时，未登录访问跳到注册页。"""
    client = app.test_client()
    response = client.get("/jobs")
    assert response.status_code == 302
    assert "/register" in response.headers["Location"]


# ---------------------------------------------------------------- 会话识别

def test_session_with_unknown_user_is_treated_as_anonymous(app, store):
    """会话里的 user_id 指向不存在的账户时，等同未登录并清掉该字段。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 999
        sess["csrf"] = "test-csrf-token"
    response = client.get("/jobs")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_session_is_bound_to_the_logged_in_account(app, store):
    """登录后页面能取到当前用户，而不是空值。"""
    store.create_initial_user("tester", PASSWORD_HASH)
    client = app.test_client()
    client.get("/login")
    token = session_csrf(client)
    response = client.post("/login", data={"csrf": token, "username": "tester",
                                           "password": PASSWORD})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs")


def test_logout_invalidates_the_session(auth_client, csrf):
    """登出后会话失效，再访问页面要重新登录。"""
    assert auth_client.get("/jobs").status_code == 200
    response = auth_client.post("/logout", headers=CSRF_HEADER)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    after = auth_client.get("/jobs")
    assert after.status_code == 302
    assert "/login" in after.headers["Location"]


def test_login_next_parameter_cannot_redirect_offsite(app, store):
    """next 参数只接受站内路径，外站地址被丢弃。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    response = client.get("/login?next=https://evil.example.com/steal")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "evil.example.com" not in body


# ---------------------------------------------------------------- CSRF

def test_post_without_csrf_is_rejected(auth_client):
    """所有写操作都要 CSRF；缺失时 403。"""
    assert auth_client.post("/preferences", data={"keywords": "数据分析师",
                                                   "location": "Hong Kong",
                                                   "job_types": "full time"}).status_code == 403


def test_post_with_wrong_csrf_is_rejected(auth_client):
    """CSRF 值不匹配时 403。"""
    response = auth_client.post("/preferences", data={"csrf": "wrong-token",
                                                       "keywords": "数据分析师",
                                                       "location": "Hong Kong",
                                                       "job_types": "full time"})
    assert response.status_code == 403


def test_delete_is_covered_by_csrf(auth_client, store, resume):
    """DELETE 也在 CSRF 覆盖范围内。"""
    report_id = make_report(store, resume["id"])
    token = create_report_share(store, report_id, "http://127.0.0.1:5000")["token"]
    assert auth_client.delete(f"/api/v1/share/{token}").status_code == 403
    assert auth_client.delete(f"/api/v1/share/{token}",
                              headers=CSRF_HEADER).status_code == 200


def test_json_body_can_carry_the_csrf_token(auth_client, store, resume):
    """JSON 请求把 csrf 放在 body 里同样有效。"""
    report_id = make_report(store, resume["id"])
    response = auth_client.post(f"/api/v1/reports/{report_id}/share",
                                 json={"csrf": "test-csrf-token", "ttl_days": 3})
    assert response.status_code == 200


# ---------------------------------------------------------------- 跨主机访问

def test_non_local_host_is_refused(app, store):
    """非本机 Host 一律 403。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    response = client.get("/jobs", headers={"Host": "evil.example.com"})
    assert response.status_code == 403


def test_public_share_is_refused_from_unknown_host(app, store, resume):
    """公开分享端点也只对放行主机开放。"""
    store.create_initial_user("tester", "hash")
    report_id = make_report(store, resume["id"])
    token = create_report_share(store, report_id, "http://127.0.0.1:5000")["token"]
    client = app.test_client()
    response = client.get(f"/api/v1/share/{token}",
                          headers={"Host": "evil.example.com"})
    assert response.status_code == 403


def test_allowed_host_can_read_public_share(app, store, resume, monkeypatch):
    """显式放行的主机可以读公开分享，且不需要登录态。"""
    monkeypatch.setenv("CAREER_SHARE_ALLOWED_HOSTS", "share.example.com")
    store.create_initial_user("tester", "hash")
    report_id = make_report(store, resume["id"])
    token = create_report_share(store, report_id, "http://127.0.0.1:5000")["token"]
    client = app.test_client()
    response = client.get(f"/api/v1/share/{token}",
                          headers={"Host": "share.example.com"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["summary"]["facts_total"] == 1


def test_public_share_never_returns_resume_text(app, store, resume):
    """公开端点只给脱敏摘要，不含简历原文或个人信息。"""
    store.create_initial_user("tester", "hash")
    report_id = store.save_report(resume["id"], {
        "facts": [{"category": "skill", "text": "张三会 SQL"}],
        "summary": "张三是数据分析师，邮箱 zhangsan@example.com",
        "audit_items": [{"category": "技能", "status": "pass", "title": "已标注",
                          "summary": "熟悉 SQL", "evidence": "熟悉 SQL"}],
    })
    token = create_report_share(store, report_id, "http://127.0.0.1:5000")["token"]
    client = app.test_client()
    body = client.get(f"/api/v1/share/{token}").get_data(as_text=True)
    for secret in ("张三", "zhangsan@example.com", "熟悉 SQL"):
        assert secret not in body


# ---------------------------------------------------------------- 单账户约束

def test_only_one_account_can_be_created(store):
    """本机账户只有一个；重复创建被拒绝。"""
    store.create_initial_user("tester", "hash")
    with pytest.raises(ValueError, match="本机账户已经创建"):
        store.create_initial_user("other", "hash2")
    assert store.user_count() == 1


def test_register_page_redirects_once_account_exists(app, store):
    """账户已存在时，注册页直接跳登录页。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    response = client.get("/register")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_register_creates_the_first_account(app):
    """第一个账户由注册页创建，创建后立刻处于登录态。"""
    client = app.test_client()
    client.get("/register")
    token = session_csrf(client)
    response = client.post("/register", data={
        "csrf": token, "username": "tester",
        "password": "test-password", "password_confirm": "test-password",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/jobs")
    assert client.get("/profile").status_code == 200


# ---------------------------------------------------------------- 输出边界

def test_task_list_never_returns_payload(auth_client, store):
    """任务列表只给状态字段，不带简历内容。"""
    store.enqueue("analyze", {"resume_id": 1, "text": "简历原文"}, "analyze:1")
    payload = auth_client.get("/api/v1/tasks").get_json()
    assert payload["items"]
    for item in payload["items"]:
        assert "payload" not in item
    assert "简历原文" not in str(payload)


def test_security_headers_are_set(auth_client):
    """关键响应头必须存在，页面不能被缓存或被嵌套。"""
    response = auth_client.get("/jobs")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "same-origin"
