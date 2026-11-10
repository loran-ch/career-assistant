"""B 组：简历纠正与版本的单元测试。

覆盖分工表模块 6「简历纠正与版本」列出的重点：
- 原版本保留：纠正后旧版本仍在库里，历史不被改写；
- 新旧版本关联：新版本挂回同一份简历，并指向最新报告；
- 重新分析不覆盖旧报告：同一份简历可以有多份报告，旧报告保持原样。

同时锁住"依据（evidence）与来源（origin）不被悄悄改掉"这条底线：
用户改一行文字，可以改内容，但不能把这条事实变成"AI 自动提取且有原文依据"。
"""
import hashlib
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from career_assistant.service import handlers


def text_pdf(text="Data Analyst") -> bytes:
    """构造一份带文字层的最小 PDF，供分析任务真实读取。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode("ascii"))
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    fonts = DictionaryObject()
    fonts[NameObject("/F1")] = writer._add_object(font)
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture()
def parsed_resume(store, tmp_path):
    """一份带真实 PDF、原文尚未解析的简历。"""
    payload = text_pdf()
    path = tmp_path / "parsed.pdf"
    path.write_bytes(payload)
    resume_id = store.add_resume("parsed.pdf", path,
                                 hashlib.sha256(payload).hexdigest(), "")
    return store.get("resumes", resume_id)


def make_report(store, resume_id, summary="第一次分析"):
    return store.save_report(resume_id, {
        "facts": [{"category": "skill", "text": "会 SQL 与 Python"}],
        "summary": summary,
        "audit_items": [{"category": "技能", "status": "pass", "title": "技能标注合适",
                          "summary": "已标注", "evidence": "熟悉 SQL"}],
    })


def confirm(client, resume_id, text, csrf):
    """走真实的纠正入口。"""
    return client.post(f"/resumes/{resume_id}/confirm",
                        data={"csrf": csrf, "facts": text})


# ---------------------------------------------------------------- 版本不可变

def test_correction_creates_a_new_version_and_keeps_the_old_one(store, resume):
    """纠正事实只新增版本，旧版本原样保留。"""
    first_id = store.confirm_facts(resume["id"], {
        "facts": [{"category": "skill", "text": "会 SQL", "evidence": "课程项目",
                   "origin": "automatic"}],
    })
    second_id = store.confirm_facts(resume["id"], {
        "facts": [{"category": "skill", "text": "会 SQL 和 Python", "evidence": "课程项目",
                   "origin": "user_edited"}],
    })
    versions = sorted(store.list("resume_versions", resume_id=resume["id"]),
                       key=lambda item: item["version"])
    assert [item["id"] for item in versions] == [first_id, second_id]
    assert [item["version"] for item in versions] == [1, 2]
    assert versions[0]["facts"]["facts"][0]["text"] == "会 SQL"
    assert versions[1]["facts"]["facts"][0]["text"] == "会 SQL 和 Python"
    assert all(item["resume_id"] == resume["id"] for item in versions)


def test_confirmed_text_cannot_be_overwritten(store, resume):
    """已提取的简历原文不允许被再次覆盖。"""
    with pytest.raises(ValueError, match="不能覆盖已经提取的简历原文"):
        store.set_resume_text(resume["id"], "换一份原文")


def test_facts_require_parsed_resume_text(store, tmp_path):
    """原文还没解析出来时不能确认事实。"""
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"%PDF-1.4\n% sample\n")
    resume_id = store.add_resume("empty.pdf", path, "sha-empty", "")
    with pytest.raises(ValueError, match="简历原文未解析"):
        store.confirm_facts(resume_id, {"facts": [{"text": "会 SQL"}]})


def test_report_must_belong_to_the_same_resume(store, resume, tmp_path):
    """报告与简历不一致时拒绝落库，避免把依据挂错简历。"""
    other_path = tmp_path / "other.pdf"
    other_path.write_bytes(b"%PDF-1.4\n% sample\n")
    other_id = store.add_resume("other.pdf", other_path, "sha-other", "另一份原文")
    other_report = store.save_report(other_id, {"facts": []})
    with pytest.raises(ValueError, match="报告与简历不一致"):
        store.confirm_facts(resume["id"], {"facts": [{"text": "会 SQL"}]}, other_report)


# ---------------------------------------------------------------- 纠正入口行为

def test_route_keeps_the_previous_version(auth_client, store, resume, csrf):
    """通过页面纠正后，旧版本仍在，新版本另起一条。"""
    store.confirm_facts(resume["id"], {
        "facts": [{"category": "skill", "text": "会 SQL", "evidence": "课程项目",
                   "origin": "automatic"}],
    })
    response = confirm(auth_client, resume["id"], "会 SQL\n会 Python", csrf)
    assert response.status_code == 302
    versions = sorted(store.list("resume_versions", resume_id=resume["id"]),
                       key=lambda item: item["version"])
    assert len(versions) == 2
    assert versions[0]["facts"]["facts"][0]["text"] == "会 SQL"
    assert [fact["text"] for fact in versions[1]["facts"]["facts"]] == ["会 SQL", "会 Python"]


def test_route_keeps_evidence_and_origin_of_untouched_facts(auth_client, store, resume, csrf):
    """没动过的事实保留原文依据与来源，不会被降级成"用户凭空提供"。"""
    store.confirm_facts(resume["id"], {
        "facts": [{"category": "skill", "text": "会 SQL", "evidence": "课程项目里用过 SQL",
                   "origin": "automatic"}],
    })
    confirm(auth_client, resume["id"], "会 SQL\n会 Python", csrf)
    latest = store.list("resume_versions", resume_id=resume["id"])[0]
    facts = {fact["text"]: fact for fact in latest["facts"]["facts"]}
    assert facts["会 SQL"]["evidence"] == "课程项目里用过 SQL"
    assert facts["会 SQL"]["origin"] == "automatic"
    assert facts["会 SQL"]["category"] == "skill"


def test_route_marks_new_lines_as_user_supplied(auth_client, store, resume, csrf):
    """新增的一行标成用户编辑，且不带 AI 依据。"""
    store.confirm_facts(resume["id"], {
        "facts": [{"category": "skill", "text": "会 SQL", "evidence": "课程项目",
                   "origin": "automatic"}],
    })
    confirm(auth_client, resume["id"], "会 SQL\n五年经验带团队", csrf)
    latest = store.list("resume_versions", resume_id=resume["id"])[0]
    added = next(fact for fact in latest["facts"]["facts"] if fact["text"] == "五年经验带团队")
    assert added["origin"] == "user_edited"
    assert added["category"] == "user_provided"


def test_route_marks_the_very_first_version_as_user_provided(auth_client, store, resume, csrf):
    """没有任何历史版本时，新事实标为用户提供。"""
    confirm(auth_client, resume["id"], "会 SQL", csrf)
    latest = store.list("resume_versions", resume_id=resume["id"])[0]
    fact = latest["facts"]["facts"][0]
    assert fact["origin"] == "user_provided"
    assert fact["category"] == "user_provided"


def test_identical_facts_do_not_create_a_new_version(auth_client, store, resume, csrf):
    """内容没变就不新增版本，避免版本号被无意义地推高。"""
    confirm(auth_client, resume["id"], "会 SQL", csrf)
    confirm(auth_client, resume["id"], "会 SQL", csrf)
    assert len(store.list("resume_versions", resume_id=resume["id"])) == 1


def test_empty_facts_are_rejected(auth_client, store, resume, csrf):
    """一条事实都不留会被拒绝，页面给出明确提示。"""
    response = confirm(auth_client, resume["id"], "   \n  ", csrf)
    assert response.status_code == 400
    assert "事实不能为空" in response.get_data(as_text=True)
    assert not store.list("resume_versions", resume_id=resume["id"])


def test_correction_requires_login(app, store, resume):
    """未登录不能纠正事实。"""
    store.create_initial_user("tester", "hash")
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess["csrf"]
    response = confirm(client, resume["id"], "会 SQL", token)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert not store.list("resume_versions", resume_id=resume["id"])


# ---------------------------------------------------------------- 报告不被覆盖

def test_reanalysis_keeps_the_previous_report(store, resume):
    """重新分析得到新报告，旧报告内容不变。"""
    first = make_report(store, resume["id"], "第一次分析")
    second = make_report(store, resume["id"], "第二次分析")
    reports = sorted(store.list("reports", resume_id=resume["id"]),
                     key=lambda item: item["id"])
    assert [item["id"] for item in reports] == [first, second]
    assert reports[0]["data"]["summary"] == "第一次分析"
    assert reports[1]["data"]["summary"] == "第二次分析"


def test_new_version_points_to_the_latest_report(auth_client, store, resume, csrf):
    """新版本关联最新一份报告，新旧版本各自指向自己的报告。"""
    first = make_report(store, resume["id"], "第一次分析")
    confirm(auth_client, resume["id"], "会 SQL", csrf)
    second = make_report(store, resume["id"], "第二次分析")
    confirm(auth_client, resume["id"], "会 SQL 和 Python", csrf)
    versions = sorted(store.list("resume_versions", resume_id=resume["id"]),
                       key=lambda item: item["version"])
    assert versions[0]["report_id"] == first
    assert versions[1]["report_id"] == second


def test_same_task_does_not_produce_two_reports(store, resume):
    """同一任务重复落库只保留一份报告（任务幂等）。"""
    task_id = store.enqueue("analyze", {"resume_id": resume["id"]}, "analyze:1")
    first = store.save_report(resume["id"], {"summary": "分析"}, task_id=task_id)
    second = store.save_report(resume["id"], {"summary": "分析"}, task_id=task_id)
    assert first == second
    assert len(store.list("reports", resume_id=resume["id"])) == 1


def test_reanalyze_task_reuses_the_saved_report(store, tmp_path, parsed_resume):
    """重复消费同一分析任务时，复用已有报告而不是再写一份。"""

    class FakeEngine:
        def analyze(self, text, facts=None, on_facts=None):
            raise AssertionError("已有报告时不应再次调用 AI")

    task_id = store.enqueue("analyze", {"resume_id": parsed_resume["id"]}, "analyze:1")
    store.save_report(parsed_resume["id"], {"summary": "已有报告"}, task_id=task_id)
    handle = handlers(store, tmp_path, "http://127.0.0.1:5000", ai=FakeEngine())["analyze"]
    result = handle(store.get("tasks", task_id))
    reports = store.list("reports", resume_id=parsed_resume["id"])
    assert result["report_id"] == reports[0]["id"]
    assert len(reports) == 1


# ---------------------------------------------------------------- 页面展示

def test_resume_page_lists_every_version(auth_client, store, resume):
    """简历页把全部事实版本都列出来，历史可见。"""
    store.confirm_facts(resume["id"], {
        "facts": [{"category": "skill", "text": "会 SQL", "evidence": "课程项目",
                   "origin": "automatic"}],
    })
    store.confirm_facts(resume["id"], {
        "facts": [{"category": "skill", "text": "会 Python", "evidence": "课程项目",
                   "origin": "user_edited"}],
    })
    page = auth_client.get(f"/resumes/{resume['id']}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert body.count("事实版本 #") == 2
    assert "会 SQL" in body
    assert "会 Python" in body


def test_resume_page_shows_all_reports(auth_client, store, resume):
    """同一份简历的多份报告在页面上分别展示，互不覆盖。"""
    make_report(store, resume["id"], "第一次分析")
    make_report(store, resume["id"], "第二次分析")
    body = auth_client.get(f"/resumes/{resume['id']}").get_data(as_text=True)
    assert "第一次分析" in body
    assert "第二次分析" in body
