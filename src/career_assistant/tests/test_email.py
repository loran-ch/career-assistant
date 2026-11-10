"""E 组：邮件发送的四态（preview／failed／accepted／uncertain）单元测试。

四种状态是本项目对外承诺的边界：
- **preview**：没有配置 SMTP，只生成正文，从不声称"已发送"；
- **failed**：配置不完整或服务器明确拒绝，属于确定失败；
- **accepted**：服务器已接受，未验证送达，不代表对方收到；
- **uncertain**：连接在 DATA 阶段中断，结果不确定，必须人工核对后再决定是否重发。

这里不连真实邮箱，全部用假 SMTP 会话驱动，保证测试可重复、离线可跑。
"""
import smtplib
from email import message_from_string

import pytest

from career_assistant import integrations
from career_assistant.service import handlers
from career_assistant.store import encode


CSRF = "test-csrf-token"


SMTP_ENV = {
    "SMTP_HOST": "smtp.163.com",
    "SMTP_PORT": "465",
    "SMTP_USER": "jobradar20261022x7@163.com",
    "SMTP_PASSWORD": "unit-test-authorization-code",
    "SMTP_FROM": "jobradar20261022x7@163.com",
    "SMTP_TO": "jobradar20261022x7@163.com",
    "SMTP_SSL": "true",
    "SMTP_STARTTLS": "false",
}


class FakeSMTP:
    """假 SMTP 会话：记录发送内容，并可在指定步骤抛错。"""

    def __init__(self, fail_at: str = "", refused: dict | None = None):
        self.fail_at = fail_at
        self.refused = refused or {}
        self.sent: list[tuple] = []
        self.logged_in = False
        self.closed = False

    def login(self, user, password):
        if self.fail_at == "login":
            raise smtplib.SMTPAuthenticationError(535, b"Error: authentication failed")
        self.logged_in = True

    def sendmail(self, sender, recipients, message):
        if self.fail_at == "send":
            raise smtplib.SMTPServerDisconnected("连接在 DATA 阶段断开")
        if self.fail_at == "refused":
            raise smtplib.SMTPRecipientsRefused({recipients[0]: (550, b"Mailbox unavailable")})
        self.sent.append((sender, tuple(recipients), message))
        return dict(self.refused)

    def close(self):
        self.closed = True


@pytest.fixture()
def smtp_env(monkeypatch):
    """给出完整的 SMTP 配置；每个用例先清空环境，避免受本机 .env 影响。"""
    for key in list(SMTP_ENV) + ["SMTP_SSL", "SMTP_STARTTLS"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    return SMTP_ENV


def test_missing_smtp_host_only_previews(monkeypatch):
    """没配 SMTP 时只出预览，绝不能说成"已发送"。"""
    for key in SMTP_ENV:
        monkeypatch.delenv(key, raising=False)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "preview"
    assert "未配置 SMTP" in result["error"]


def test_missing_recipient_is_a_definite_failure(smtp_env, monkeypatch):
    monkeypatch.delenv("SMTP_TO", raising=False)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "failed"
    assert "配置不完整" in result["error"]


def test_missing_password_is_a_definite_failure(smtp_env, monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "failed"
    assert "配置不完整" in result["error"]


def test_remote_plain_connection_is_refused(smtp_env, monkeypatch):
    """远程服务器必须启用 SSL 或 STARTTLS，明文连接一律拒绝。"""
    monkeypatch.setenv("SMTP_SSL", "false")
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "failed"
    assert "SSL" in result["error"]


def test_accepted_when_server_returns_positive_reply(smtp_env, monkeypatch):
    fake = FakeSMTP()
    monkeypatch.setattr(integrations.EmailChannel, "_connect", lambda self: fake)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "accepted"
    assert result["refused_count"] == 0
    assert "未验证送达" in result["detail"]
    assert fake.logged_in is True
    assert len(fake.sent) == 1
    sender, recipients, message = fake.sent[0]
    assert sender == SMTP_ENV["SMTP_FROM"]
    assert recipients == (SMTP_ENV["SMTP_TO"],)
    parsed = message_from_string(message)
    bodies = [part.get_payload(decode=True).decode("utf-8") for part in parsed.walk()
              if part.get_content_type() == "text/html"]
    assert "<p>正文</p>" in bodies[0]


def test_connection_failure_before_send_is_failed(smtp_env, monkeypatch):
    def boom(self):
        raise OSError("网络不可达")

    monkeypatch.setattr(integrations.EmailChannel, "_connect", boom)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "failed"
    assert "连接或配置失败" in result["error"]


def test_disconnect_during_send_is_uncertain(smtp_env, monkeypatch):
    """DATA 阶段断线：不能当失败自动重发，也不能当成功。"""
    fake = FakeSMTP(fail_at="send")
    monkeypatch.setattr(integrations.EmailChannel, "_connect", lambda self: fake)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "uncertain"
    assert "不确定" in result["error"]
    assert fake.sent == []


def test_refused_recipient_is_reported_without_resending(smtp_env, monkeypatch):
    fake = FakeSMTP(refused={SMTP_ENV["SMTP_TO"]: (550, b"mailbox unavailable")})
    monkeypatch.setattr(integrations.EmailChannel, "_connect", lambda self: fake)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "accepted"
    assert result["refused_count"] == 1
    assert "不要整封自动重发" in result["detail"]


def test_authentication_failure_reports_the_server_reason(smtp_env, monkeypatch):
    """认证失败要带上服务器的真实错误码与回复，不能只显示一个异常类名。

    只丢类名的话，用户在页面上只看到「SMTPAuthenticationError」，
    无从判断是授权码错了还是 SMTP 服务没开。
    """
    fake = FakeSMTP(fail_at="login")
    monkeypatch.setattr(integrations.EmailChannel, "_connect", lambda self: fake)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "failed"
    assert "535" in result["error"]
    assert "authentication failed" in result["error"]
    assert "客户端授权码" in result["error"]


def test_smtp_error_never_leaks_the_authorization_code(smtp_env, monkeypatch):
    """错误信息里绝不能出现授权码。"""
    fake = FakeSMTP(fail_at="login")
    monkeypatch.setattr(integrations.EmailChannel, "_connect", lambda self: fake)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert SMTP_ENV["SMTP_PASSWORD"] not in result["error"]
    assert SMTP_ENV["SMTP_PASSWORD"] not in result.get("detail", "")


def test_refused_recipient_error_names_the_address(smtp_env, monkeypatch):
    """收件人被拒时，错误信息里能看到是谁被拒、拒收代码是多少。"""
    fake = FakeSMTP(fail_at="refused")
    monkeypatch.setattr(integrations.EmailChannel, "_connect", lambda self: fake)
    result = integrations.send_email("主题", "<p>正文</p>")
    assert result["status"] == "failed"
    assert "550" in result["error"]
    assert SMTP_ENV["SMTP_TO"] in result["error"]


def test_email_task_records_each_state(store, tmp_path, monkeypatch, resume):
    """邮件任务把状态写进 email_attempts，四态在库里可追溯。"""
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    task_id = store.enqueue("email", {"type": "report", "id": report_id},
                            "email:report:1", max_attempts=2, side_effect=True)
    task = store.get("tasks", task_id)
    handle = handlers(store, tmp_path, "http://127.0.0.1:5000")["email"]

    for status, detail in (("preview", "未配置"), ("failed", "配置不完整"),
                           ("accepted", "已接受"), ("uncertain", "结果不确定")):
        monkeypatch.setattr(integrations, "send_email",
                            lambda subject, body, _s=status, _d=detail: {
                                "status": _s, "error": _d, "detail": _d})
        result = handle(task)
        assert result["status"] == status
        assert result["html"]

    attempts = store.list("email_attempts", task_id=task_id)
    # store.list 默认按 id 倒序，最新的尝试排在最前。
    assert [row["status"] for row in reversed(attempts)] == [
        "preview", "failed", "accepted", "uncertain"]


def test_accepted_attempt_blocks_automatic_retry(store, tmp_path, monkeypatch, resume):
    """服务器已接受后不允许再整封重发，避免重复投递。"""
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    task_id = store.enqueue("email", {"type": "report", "id": report_id},
                            "email:report:1", max_attempts=2, side_effect=True)
    task = store.get("tasks", task_id)
    handle = handlers(store, tmp_path, "http://127.0.0.1:5000")["email"]
    monkeypatch.setattr(integrations, "send_email", lambda subject, body: {
        "status": "accepted", "error": "", "detail": "SMTP 服务器已接受，未验证送达。"})
    handle(task)
    with pytest.raises(ValueError, match="不能重复重试"):
        store.retry(task_id)


# ---------------------------------------------------------------- 推送按钮的语义

def _push_report(auth_client, report_id):
    """点一次「推送审计」，返回落点地址。"""
    response = auth_client.post(f"/email/report/{report_id}", data={"csrf": CSRF})
    assert response.status_code == 302
    return response.headers["Location"]


def _task_id_of(location: str) -> int:
    return int(location.split("task=")[1].split("&")[0].split("#")[0])


def _finish_as_preview(store, task_id):
    """把任务标记成「只生成了预览、根本没发出去」的终态。"""
    with store.connect() as conn:
        conn.execute("UPDATE course_tasks SET status='succeeded', result_json=? WHERE id=?",
                     (encode({"status": "preview",
                              "error": "未配置 SMTP；仅生成本地预览，未发送。"}), task_id))


def test_push_creates_a_new_task_after_a_preview_only_run(auth_client, store, resume):
    """第一次只预览（没配 SMTP）之后，再点推送要新建一条任务。

    幂等键原本写死成 email:{kind}:{id}，会把页面钉死在第一次那条「仅预览」上；
    配好 SMTP 后再点也只会回到旧任务，用户以为一直没走邮箱配置。
    """
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    first = _push_report(auth_client, report_id)
    _finish_as_preview(store, _task_id_of(first))
    second = _push_report(auth_client, report_id)
    assert _task_id_of(first) != _task_id_of(second)
    assert len(store.list("tasks", kind="email")) == 2


def test_push_reuses_the_running_task(auth_client, store, resume):
    """任务还在排队时连点，复用同一条，不堆积。"""
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    first = _push_report(auth_client, report_id)
    second = _push_report(auth_client, report_id)
    assert first == second
    assert len(store.list("tasks", kind="email")) == 1


def test_push_does_not_resend_after_the_server_accepted(auth_client, store, resume):
    """服务器已接受后不再重复发送，页面给出提示并跳回原任务。"""
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    first = _push_report(auth_client, report_id)
    task_id = _task_id_of(first)
    store.record_email_attempt(task_id, "accepted", "SMTP 服务器已接受，未验证送达。")
    with store.connect() as conn:
        conn.execute("UPDATE course_tasks SET status='succeeded' WHERE id=?", (task_id,))
    again = _push_report(auth_client, report_id)
    assert again == first
    assert len(store.list("tasks", kind="email")) == 1
    body = auth_client.get(again).get_data(as_text=True)
    assert "不会重复发送" in body


def test_email_card_explains_why_nothing_was_sent(auth_client, store, resume):
    """预览态也要说清楚原因，不能只显示一句「未发送，仅预览」。"""
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    task_id = store.enqueue("email", {"type": "report", "id": report_id},
                            "email:report:1", max_attempts=2, side_effect=True)
    with store.connect() as conn:
        conn.execute("UPDATE course_tasks SET status='succeeded', result_json=? WHERE id=?",
                     (encode({"status": "preview",
                              "error": "未配置 SMTP；仅生成本地预览，未发送。"}), task_id))
    body = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    assert "未发送，仅预览" in body
    assert "未配置 SMTP" in body


def test_email_card_does_not_claim_completion_for_a_preview_only_run(auth_client, store, resume):
    """只生成了预览时，卡片不能显示「完成」。

    任务本身是 succeeded（程序跑完了），但邮件根本没发出去。
    标题写「完成」再写「未发送，仅预览」，两句话自相矛盾，
    用户只会当成状态错乱或者发送失败。
    """
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    task_id = store.enqueue("email", {"type": "report", "id": report_id},
                            "email:report:1", max_attempts=2, side_effect=True)
    _finish_as_preview(store, task_id)
    body = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    assert "</strong> · 未发送，仅预览" in body
    assert "</strong> · 完成" not in body


def test_preview_card_offers_to_resend_with_the_current_configuration(auth_client, store, resume):
    """预览态的按钮要说清楚是「按当前配置重新发送」，而不是「重试失败步骤」。

    邮件没失败，只是当时没配 SMTP。按钮叫「重试失败步骤」会让人以为坏了，
    于是反复点也看不到邮件发出去——这正是「没走邮箱配置」的观感来源。
    """
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    task_id = store.enqueue("email", {"type": "report", "id": report_id},
                            "email:report:1", max_attempts=2, side_effect=True)
    _finish_as_preview(store, task_id)
    body = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    assert "按当前配置重新发送" in body
    assert "重试失败步骤" not in body


def test_resume_page_renders_the_mail_card_right_away(auth_client, store, resume):
    """简历页的邮件卡片首屏就渲染出来，不靠轮询脚本注入。

    轮询容器原本是空的，卡片全靠 1.5 秒后由脚本填进去：首屏空白，
    而且注入的内容每次都因为 operation 变化而被判定为「有变化」，
    于是每 1.5 秒重写一次——用户看到的就是页面不停闪。
    """
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    task_id = store.enqueue("email", {"type": "report", "id": report_id},
                            "email:report:1", max_attempts=2, side_effect=True)
    body = auth_client.get(f"/resumes/{resume['id']}?task={task_id}").get_data(as_text=True)
    assert f'data-poll="/tasks/{task_id}/status"' in body
    assert 'data-task-status="queued"' in body, "卡片内容应随页面一起渲染"
