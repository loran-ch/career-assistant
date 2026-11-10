"""B 组：页面轮询与「重复刷新」的回归测试。

修掉的三个问题（见 static/poll.js 顶部说明）：
1. 旧实现每 1.5 秒无条件重写一次轮询容器 → 页面反复闪烁、动画反复重播；
2. 旧实现的整页刷新没有条件 → 任务状态来回时连续整页刷新；
3. 重复点击「开始搜索」每次都新建任务 → 任务堆积，岗位页一直停在「正在获取岗位」。

这里守住三条边界：
- 轮询只有一个实现，模板不再各写一份；
- 整页刷新只在任务状态发生跃迁时发生一次；
- 重复提交搜索表单复用正在跑的任务。
"""
import re

from career_assistant.store import encode, stamp

CSRF = "test-csrf-token"


def search_form(**extra):
    """搜索表单的提交内容。"""
    data = {"csrf": CSRF, "keywords": "数据分析师", "location": "Hong Kong",
            "job_types": ["full time"], "sources": ["local"]}
    data.update(extra)
    return data


def finish_task(store, task_id, status="succeeded"):
    """把任务直接改成终态，模拟 Worker 已经处理完。"""
    with store.connect() as conn:
        conn.execute("UPDATE course_tasks SET status=?, updated_at=? WHERE id=?",
                     (status, stamp(), task_id))


# ---------------------------------------------------------------- 统一轮询实现

def test_pages_use_the_shared_polling_script(auth_client, store):
    """所有页面引用同一份 poll.js，模板里不再各写一份内联轮询。"""
    task_id = store.enqueue("search", {"preference_id": 1}, "search:1")
    for path in ("/profile", "/jobs", f"/jobs?task={task_id}"):
        body = auth_client.get(path).get_data(as_text=True)
        assert "/static/poll.js" in body, path
        assert "querySelectorAll('[data-poll]')" not in body, path


def test_polling_script_is_served(app):
    """轮询脚本可以被浏览器取到。"""
    response = app.test_client().get("/static/poll.js")
    assert response.status_code == 200
    source = response.get_data(as_text=True)
    assert "data-reload-from" in source
    assert "data-reload-on" in source


def test_polling_script_only_reloads_on_a_state_change(app):
    """整页刷新只在状态相对页面加载时发生跃迁时才触发，不会来回刷。"""
    source = app.test_client().get("/static/poll.js").get_data(as_text=True)
    assert "status === from" in source
    assert "location.reload()" in source


def test_polling_script_compares_normalized_html(app):
    """比较前先归一化，否则浏览器序列化差异会让每轮都判定为「有变化」。"""
    source = app.test_client().get("/static/poll.js").get_data(as_text=True)
    assert "normalize(text) === normalize(el.innerHTML)" in source


def test_polling_script_keeps_away_from_inputs_in_progress(app):
    """用户正在输入时，本轮不刷新页面。"""
    source = app.test_client().get("/static/poll.js").get_data(as_text=True)
    assert "isEditing" in source
    assert "dirty" in source


def test_polling_script_ignores_the_volatile_operation_field(app):
    """比较前要剔除 operation 隐藏域。

    operation 是「本次表单提交」的随机标识，context_processor 每次请求都换新值。
    它出现在轮询片段里的重试表单上，不剔除的话每 1.5 秒都会被判定为
    「有变化」而重写 DOM——用户看到的就是页面不停刷新。
    """
    source = app.test_client().get("/static/poll.js").get_data(as_text=True)
    assert 'name="operation"' in source


def test_task_status_fragment_is_stable_once_the_operation_field_is_removed(auth_client, store, resume):
    """轮询片段两次拉取只差 operation；剔除后必须逐字相同。

    这是「一直刷新」的真实复现：预览态的任务会带上「重试失败步骤」表单，
    表单里的 operation 每次请求都变。前端比较时永远判定为「有变化」，
    于是每 1.5 秒重写一次 DOM——用户看到的就是页面不停刷新。
    """
    report_id = store.save_report(resume["id"], {"summary": "分析结论", "facts": [], "audit_items": []})
    task_id = store.enqueue("email", {"type": "report", "id": report_id},
                            "email:1", max_attempts=2, side_effect=True)
    with store.connect() as conn:
        conn.execute("UPDATE course_tasks SET status='succeeded', result_json=? WHERE id=?",
                     (encode({"status": "preview",
                              "error": "未配置 SMTP；仅生成本地预览，未发送。"}), task_id))
    first = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    second = auth_client.get(f"/tasks/{task_id}/status").get_data(as_text=True)
    assert 'name="operation"' in first, "预览态带重试表单，应含 operation 隐藏域"
    assert first != second, "operation 每次请求都应不同"
    strip = re.compile(r'<input[^>]*name="operation"[^>]*>')
    assert strip.sub("", first) == strip.sub("", second)


# ---------------------------------------------------------------- 整页刷新的跃迁条件

def test_jobs_page_records_the_state_it_was_rendered_with(auth_client, store):
    """岗位页带上渲染当时的任务状态，作为跃迁判断的起点。"""
    task_id = store.enqueue("search", {"preference_id": 1}, "search:1")
    body = auth_client.get(f"/jobs?task={task_id}").get_data(as_text=True)
    assert 'data-reload-on="succeeded,failed,uncertain"' in body
    assert 'data-reload-from="queued"' in body


def test_jobs_page_marks_a_finished_task_as_already_loaded(auth_client, store):
    """任务已结束时，起点状态就是终态，页面不会再触发整页刷新。"""
    task_id = store.enqueue("search", {"preference_id": 1}, "search:1")
    finish_task(store, task_id)
    body = auth_client.get(f"/jobs?task={task_id}").get_data(as_text=True)
    assert 'data-reload-from="succeeded"' in body


def test_task_page_no_longer_reloads_the_whole_page(auth_client, store):
    """任务详情页不再每三秒整页刷新，改为统一轮询。"""
    task_id = store.enqueue("email", {"type": "report", "id": 1}, "email:1")
    body = auth_client.get(f"/tasks/{task_id}").get_data(as_text=True)
    assert "setTimeout(()=>location.reload()" not in body
    assert f'data-poll="/tasks/{task_id}/status"' in body
    assert 'data-reload-from="queued"' in body
    assert "/static/poll.js" in body


def test_task_page_shows_whether_facts_are_available(auth_client, store, resume):
    """任务页的进度片段能显示简历事实是否已保存。"""
    task_id = store.enqueue("analyze", {"resume_id": resume["id"]}, "analyze:1")
    body = auth_client.get(f"/tasks/{task_id}").get_data(as_text=True)
    assert "尚无可用事实" in body
    store.confirm_facts(resume["id"], {"facts": [{"category": "skill", "text": "会 SQL"}]})
    body = auth_client.get(f"/tasks/{task_id}").get_data(as_text=True)
    assert "已保存，可用于匹配" in body


# ---------------------------------------------------------------- 搜索不堆积任务

def test_repeated_search_reuses_the_running_task(auth_client, store):
    """重复点「开始搜索」复用正在跑的任务，不再堆积新任务。"""
    first = auth_client.post("/preferences", data=search_form())
    second = auth_client.post("/preferences", data=search_form())
    assert first.status_code == 302
    assert second.status_code == 302
    assert first.headers["Location"] == second.headers["Location"]
    assert len(store.list("tasks", kind="search")) == 1


def test_search_creates_a_new_task_after_the_previous_one_finished(auth_client, store):
    """上一个搜索已结束后，再点搜索会真正创建新任务。"""
    first = auth_client.post("/preferences", data=search_form())
    task_id = int(first.headers["Location"].split("task=")[1].split("#")[0].split("&")[0])
    finish_task(store, task_id)
    second = auth_client.post("/preferences", data=search_form())
    assert second.headers["Location"] != first.headers["Location"]
    assert len(store.list("tasks", kind="search")) == 2


def test_search_route_also_reuses_the_running_task(auth_client, store):
    """「搜索岗位」按钮走的是另一个入口，同样不堆积任务。"""
    preference_id = store.confirm_preferences(None, {
        "confirmed": True, "keywords": ["数据分析师"], "location": "Hong Kong",
        "job_types": ["full time"], "sources": ["local"]})
    first = auth_client.post("/search", data={"csrf": CSRF, "preference_id": preference_id})
    second = auth_client.post("/search", data={"csrf": CSRF, "preference_id": preference_id})
    assert first.headers["Location"] == second.headers["Location"]
    assert len(store.list("tasks", kind="search")) == 1


def test_jobs_page_explains_the_scheduled_search_when_enabled(auth_client, store):
    """开启定时搜索时，岗位页要说明它会自己再搜。"""
    body = auth_client.get("/jobs").get_data(as_text=True)
    assert "已开启定时搜索" not in body
    assert "不会堆积多个搜索" in body
