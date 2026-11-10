import re

import pytest

from career_assistant.share import (
    build_report_summary,
    create_report_share,
    new_token,
    read_share,
    revoke_share,
    token_digest,
    token_hint,
)

CSRF = "test-csrf-token"


def make_report(store, resume_id):
    return store.save_report(resume_id, {
        "facts": [
            {"category": "skill", "text": "张三是数据分析师"},
            {"category": "experience", "text": "在某公司做过两年报表"},
        ],
        "summary": "张三是香港的数据分析师，邮箱 zhangsan@example.com",
        "audit_items": [
            {"category": "技能", "status": "pass", "title": "技能熟练度标注合适",
             "summary": "基础与入门能力已明确限定", "evidence": "熟悉 SQL"},
            {"category": "投递准备", "status": "issue", "title": "投递前需要人工复核",
             "summary": "AI 审计不能替代本人核对", "evidence": "投递前请核对"},
        ],
        "suggestions": ["建议补充项目背景"],
    })


def test_token_is_never_stored_in_plaintext(store, resume):
    report = make_report(store, resume["id"])
    result = create_report_share(store, report, "http://127.0.0.1:5000")
    token = result["token"]
    assert token not in token_digest(token)
    assert token_hint(token) == token[:8]
    stored = store.list_share_tokens()[0]
    assert stored["token_hash"] == token_digest(token)
    assert stored["token_hash"] != token
    assert token not in str(stored)


def test_summary_contains_no_personal_text(store, resume):
    report = make_report(store, resume["id"])
    summary = build_report_summary(store.get("reports", report))
    assert summary["facts_total"] == 2
    assert summary["skills_total"] == 1
    assert summary["audit_pass"] == 1
    assert summary["audit_issue"] == 1
    assert summary["coverage_percent"] == 50
    serialized = str(summary)
    for secret in ("张三", "zhangsan@example.com", "熟悉 SQL", "建议补充项目背景"):
        assert secret not in serialized


def test_invalid_token_is_rejected(store):
    with pytest.raises(LookupError):
        read_share(store, new_token())


def test_revoked_token_is_rejected(store, resume):
    report = make_report(store, resume["id"])
    token = create_report_share(store, report, "http://127.0.0.1:5000")["token"]
    assert revoke_share(store, token) is True
    with pytest.raises(LookupError):
        read_share(store, token)
    assert revoke_share(store, token) is False


def test_expired_token_is_rejected(store, resume):
    report = make_report(store, resume["id"])
    token = create_report_share(store, report, "http://127.0.0.1:5000")["token"]
    with store.connect() as conn:
        conn.execute("UPDATE course_share_tokens SET expires_at='2000-01-01T00:00:00+08:00'")
    with pytest.raises(LookupError):
        read_share(store, token)


def test_access_count_is_tracked(store, resume):
    report = make_report(store, resume["id"])
    token = create_report_share(store, report, "http://127.0.0.1:5000")["token"]
    read_share(store, token)
    read_share(store, token)
    assert store.get_share_token(token_digest(token))["access_count"] == 2


# ---------------------------------------------------------------- 分享页面

def test_share_page_shows_the_link_and_the_qr_code(auth_client, store, resume):
    """生成分享链接要给一个页面：链接与二维码都在这一页，且只显示这一次。"""
    report = make_report(store, resume["id"])
    response = auth_client.post(f"/reports/{report}/share", data={"csrf": CSRF})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "分享链接已生成" in body
    assert "/api/v1/share/" in body
    assert "只显示这一次" in body
    # 二维码真的渲染成图片，而不是只给一段文字
    assert "data:image/png;base64," in body


def test_share_page_never_shows_personal_text(auth_client, store, resume):
    """分享页本身也不能露出姓名或邮箱。"""
    report = make_report(store, resume["id"])
    body = auth_client.post(f"/reports/{report}/share", data={"csrf": CSRF}).get_data(as_text=True)
    for secret in ("张三", "zhangsan@example.com", "熟悉 SQL"):
        assert secret not in body


def test_share_token_is_not_exposed_in_the_url_or_session(auth_client, store, resume):
    """明文令牌不进 URL、不进 session：只出现在这一次响应体里。"""
    report = make_report(store, resume["id"])
    response = auth_client.post(f"/reports/{report}/share", data={"csrf": CSRF})
    body = response.get_data(as_text=True)
    token = re.search(r'name="token" value="([^"]+)"', body).group(1)
    assert token not in response.headers.get("Location", "")
    with auth_client.session_transaction() as sess:
        assert token not in str(dict(sess))


def test_share_page_can_revoke_the_link(auth_client, store, resume):
    """分享页上就能撤销，不用去调 API。"""
    report = make_report(store, resume["id"])
    body = auth_client.post(f"/reports/{report}/share", data={"csrf": CSRF}).get_data(as_text=True)
    token = re.search(r'name="token" value="([^"]+)"', body).group(1)
    response = auth_client.post("/shares/revoke", data={"csrf": CSRF, "token": token})
    assert response.status_code == 302
    with pytest.raises(LookupError):
        read_share(store, token)


def test_revoked_link_returns_404_to_the_visitor(auth_client, store, resume):
    """撤销后对方再打开公开端点，应该是 404 而不是仍能看到摘要。"""
    report = make_report(store, resume["id"])
    body = auth_client.post(f"/reports/{report}/share", data={"csrf": CSRF}).get_data(as_text=True)
    token = re.search(r'name="token" value="([^"]+)"', body).group(1)
    assert auth_client.get(f"/api/v1/share/{token}").status_code == 200
    auth_client.post("/shares/revoke", data={"csrf": CSRF, "token": token})
    assert auth_client.get(f"/api/v1/share/{token}").status_code == 404
