"""B 组：用户页面的单元测试。

覆盖分工表模块 5「用户页面」列出的重点：
- 中文上传页、任务状态页、报告页都能正常返回；
- 响应式布局（viewport 与窄屏断点）保留；
- 页面显示的是**真实**任务状态与错误，而不是写死的假文案。

这里的"真实"很关键：任务状态直接来自 `course_tasks`，
失败原因直接来自任务 error 字段，页面不能自己编造进度。
"""
import re

from career_assistant.store import stamp


def set_task_state(store, task_id, status, error="", result=None):
    """直接改库构造任务状态，模拟 Worker 的处理结果。"""
    with store.connect() as conn:
        conn.execute(
            "UPDATE course_tasks SET status=?, error=?, updated_at=?, "
            "result_json=COALESCE(?, result_json) WHERE id=?",
            (status, error, stamp(),
             __import__("json").dumps(result, ensure_ascii=False) if result is not None else None,
             task_id),
        )


# ---------------------------------------------------------------- 上传页

def test_upload_page_is_chinese_and_has_a_pdf_input(auth_client):
    """上传页是中文的，并且只接受 PDF。"""
    response = auth_client.get("/profile")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'lang="zh-Hans"' in body
    assert "上传新简历" in body
    assert 'type="file" name="resume" accept="application/pdf"' in body
    assert "10 MB" in body


def test_upload_page_lists_saved_resumes(auth_client, store, resume):
    """上传页列出已保存的简历，并能进入详情。"""
    body = auth_client.get("/profile").get_data(as_text=True)
    assert resume["filename"] in body
    assert f'href="/resumes/{resume["id"]}"' in body


# ---------------------------------------------------------------- 任务状态页

def test_task_status_page_shows_the_real_state(auth_client, store):
    """任务状态页显示库里的真实状态，不是固定文案。"""
    task_id = store.enqueue("analyze", {"resume_id": 1}, "analyze:1")
    set_task_state(store, task_id, "failed", "AI 超时，未能在限定时间内返回结果")
    body = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    assert 'data-task-status="failed"' in body
    assert "失败" in body
    assert "AI 超时" in body


def test_task_status_page_shows_uncertain_state(auth_client, store):
    """结果不确定时，页面明说"结果不确定"，并提示重试风险。"""
    task_id = store.enqueue("email", {"type": "report", "id": 1}, "email:1")
    set_task_state(store, task_id, "uncertain", "连接在 DATA 阶段断开")
    body = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    assert 'data-task-status="uncertain"' in body
    assert "结果不确定" in body
    assert "可能重复发送" in body


def test_task_status_page_reports_missing_facts(auth_client, store, resume):
    """分析任务在还没有可用事实时，页面如实说明"尚无可用事实"。"""
    task_id = store.enqueue("analyze", {"resume_id": resume["id"]}, "analyze:1")
    body = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    assert "尚无可用事实" in body
    store.confirm_facts(resume["id"], {"facts": [{"category": "skill", "text": "会 SQL"}]})
    body = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    assert "已保存，可用于匹配" in body


def test_task_page_shows_email_attempts(auth_client, store):
    """邮件任务的每一次尝试都列出来，含四态。"""
    task_id = store.enqueue("email", {"type": "report", "id": 1}, "email:1")
    store.record_email_attempt(task_id, "preview", "未配置 SMTP")
    store.record_email_attempt(task_id, "accepted", "SMTP 服务器已接受，未验证送达。")
    body = auth_client.get(f"/tasks/{task_id}").get_data(as_text=True)
    assert "preview" in body
    assert "accepted" in body
    assert "未配置 SMTP" in body


def test_task_page_of_unknown_task_is_an_error(auth_client):
    """不存在的任务给出明确错误，而不是空白页。"""
    response = auth_client.get("/tasks/999")
    assert response.status_code == 400
    assert "记录不存在" in response.get_data(as_text=True)


# ---------------------------------------------------------------- 报告页

def test_report_page_shows_audit_items_with_evidence(auth_client, store, resume):
    """报告页逐条展示审计结果与原文依据。"""
    report_id = store.save_report(resume["id"], {
        "summary": "整体匹配度中等",
        "analysis_date": "2026-11-10",
        "audit_items": [
            {"category": "技能", "status": "pass", "title": "技能标注合适",
             "summary": "已按熟练度标注", "evidence": "熟悉 SQL"},
            {"category": "投递准备", "status": "issue", "title": "投递前需人工复核",
             "summary": "AI 审计不能替代本人核对", "evidence": "投递前请核对"},
        ],
    })
    body = auth_client.get(f"/resumes/{resume['id']}").get_data(as_text=True)
    assert f"AI审计 #{report_id}" in body
    assert "技能标注合适" in body
    assert "查看原文依据" in body
    assert "熟悉 SQL" in body
    assert "需处理" in body


def test_report_page_says_when_no_report_exists(auth_client, resume):
    """没有报告时给出明确提示，不显示空白审计区。"""
    body = auth_client.get(f"/resumes/{resume['id']}").get_data(as_text=True)
    assert "AI审计尚未生成" in body


def test_resume_page_shows_the_original_text_and_download_link(auth_client, resume):
    """简历页提供原始文本与原始 PDF 下载入口。"""
    body = auth_client.get(f"/resumes/{resume['id']}").get_data(as_text=True)
    assert "原始提取文本" in body
    assert resume["text"] in body
    assert f'href="/resumes/{resume["id"]}/pdf"' in body


# ---------------------------------------------------------------- 申请看板页

def test_application_board_page_groups_by_status(auth_client, store, seed_job):
    """申请页按状态分列，并给出总览计数。"""
    _, snapshot_id = seed_job()
    store.application_for_job(snapshot_id, "applied")
    body = auth_client.get("/applications").get_data(as_text=True)
    assert "看板" in body
    assert "已投递" in body
    assert "总览" in body


def test_application_board_page_filters_by_keyword(auth_client, store, seed_job):
    """申请页支持关键词筛选：看板保留全貌，列表只给匹配结果。"""
    _, first = seed_job(title="数据分析师", company="示例科技", uid="seed-1")
    _, second = seed_job(title="后端工程师", company="另一家", uid="seed-2")
    store.application_for_job(first, "applied")
    store.application_for_job(second, "saved")
    body = auth_client.get("/applications?q=后端").get_data(as_text=True)
    # 看板是全量视图，所以两个岗位都在；筛选只作用于下方列表。
    assert "数据分析师" in body
    assert "后端工程师" in body
    assert "当前显示 1 条（全部 2 条）" in body
    assert "清除筛选" in body


def test_application_filtered_list_only_contains_matches(auth_client, store, seed_job):
    """筛选结果段落里只有匹配的记录。"""
    _, first = seed_job(title="数据分析师", company="示例科技", uid="seed-1")
    _, second = seed_job(title="后端工程师", company="另一家", uid="seed-2")
    store.application_for_job(first, "applied")
    store.application_for_job(second, "saved")
    body = auth_client.get("/applications?q=后端").get_data(as_text=True)
    listed = body.split("<h2>筛选</h2>", 1)[1]
    assert "后端工程师" in listed
    assert "数据分析师" not in listed


def test_unknown_application_status_is_rejected(auth_client):
    """筛选参数非法时给出明确错误，不静默返回全部。"""
    response = auth_client.get("/applications?status=not-a-status")
    assert response.status_code == 400
    assert "未知申请状态" in response.get_data(as_text=True)


# ---------------------------------------------------------------- 响应式布局

def test_pages_declare_a_responsive_viewport(auth_client):
    """所有主要页面都声明 viewport，移动端不会横向溢出。"""
    for path in ("/profile", "/jobs", "/applications", "/assistant"):
        body = auth_client.get(path).get_data(as_text=True)
        assert 'name="viewport" content="width=device-width' in body, path


def test_stylesheet_keeps_narrow_screen_breakpoints(app):
    """样式表保留窄屏断点，看板与信息包在手机上也能换行。"""
    css = (app.static_folder and
           (app.root_path + "/static/workspace.css"))
    from pathlib import Path

    source = Path(css).read_text(encoding="utf-8")
    breakpoints = re.findall(r"@media\s*\(max-width:\s*(\d+)px\)", source)
    assert breakpoints, "缺少响应式断点"
    assert min(int(value) for value in breakpoints) <= 560
    assert ".board-columns" in source
    assert ".pack-fields" in source
