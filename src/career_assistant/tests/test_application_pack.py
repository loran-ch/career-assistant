"""E 组：本地投递信息包（application pack）的组装、版本与导出测试。

「模拟填表」在本项目里的边界是**只整理、不代劳**：
- 只使用已经保存的投递信息版本、简历事实版本、结构化资料与材料版本；
- 缺什么就标「需本人填写」，绝不编造姓名、联系方式、经历或附件；
- 不访问、不填写、不上传、不提交任何真实招聘网站表单。
"""
from pathlib import Path

import pytest

from career_assistant import application_pack
from career_assistant.application_pack import build_pack, create_pack, pack_markdown


FACTS = {
    "facts": [
        {"id": "f1", "category": "skill", "text": "会 SQL 与 Python",
         "evidence": "课程项目里用过 SQL", "origin": "user_provided"},
        {"id": "f2", "category": "experience", "text": "两年数据分析实习",
         "evidence": "实习证明", "origin": "user_provided"},
    ],
    "confirmed": True,
}


@pytest.fixture()
def application(store, seed_job, resume):
    """一条已关联简历事实版本的申请记录。"""
    _, snapshot_id = seed_job()
    version_id = store.confirm_facts(resume["id"], FACTS)
    application_id = store.application_for_job(snapshot_id, "preparing")
    store.link_application_basis(application_id, version_id, None, "关联简历事实", usage="planned")
    return store.get("applications", application_id)


def field_of(pack, key):
    return next(item for item in pack["fields"] if item["key"] == key)


def test_pack_uses_saved_delivery_information(store, application):
    store.confirm_delivery_profile({
        "name": "张三", "email": "zhangsan@example.com",
        "phone": "13800000000", "location": "香港",
        "links": ["https://github.com/example"],
    })
    pack = build_pack(store, application["id"])
    assert field_of(pack, "name")["value"] == "张三"
    assert field_of(pack, "name")["status"] == "ready"
    assert field_of(pack, "email")["value"] == "zhangsan@example.com"
    assert field_of(pack, "link_1")["value"] == "https://github.com/example"
    assert "投递信息版本" in field_of(pack, "name")["source"]


def test_pack_marks_missing_fields_without_inventing_them(store, application):
    """没有投递信息时留空并标成「需本人填写」，不编造姓名和电话。"""
    pack = build_pack(store, application["id"])
    assert field_of(pack, "name")["value"] == ""
    assert field_of(pack, "name")["status"] == "unknown"
    assert field_of(pack, "phone")["value"] == ""
    assert "姓名" in pack["missing"]
    assert "电话" in pack["missing"]
    assert "张三" not in str(pack)


def test_pack_carries_job_and_resume_facts(store, application):
    pack = build_pack(store, application["id"])
    assert field_of(pack, "job_title")["value"] == "数据分析师"
    assert field_of(pack, "job_company")["value"] == "示例科技"
    texts = [item["text"] for group in pack["groups"] for item in group["items"]]
    assert "会 SQL 与 Python" in texts
    assert "两年数据分析实习" in texts


def test_pack_records_the_sources_it_used(store, application):
    pack = build_pack(store, application["id"])
    assert pack["sources"]["resume_version_id"]
    assert pack["sources"]["snapshot_id"] == application["course_snapshot_id"]
    assert pack["notice"].startswith("本信息包只在本机生成")


def test_pack_versions_are_immutable(store, application):
    """每次生成保存为新版本，历史版本不被覆盖。"""
    first = create_pack(store, application["id"])
    second = create_pack(store, application["id"])
    assert first["version"] == 1
    assert second["version"] == 2
    history = store.list_application_packs(application["id"])
    assert [row["version"] for row in sorted(history, key=lambda r: r["version"])] == [1, 2]
    assert store.latest_application_pack(application["id"])["version"] == 2


def test_pack_markdown_marks_what_is_still_missing(store, application):
    store.confirm_delivery_profile({
        "name": "张三", "email": "zhangsan@example.com",
        "phone": "", "location": "香港", "links": [],
    })
    pack = build_pack(store, application["id"])
    text = pack_markdown(pack)
    assert "投递信息包（本地预览）" in text
    assert "程序不填写、不上传、不提交" in text
    assert "张三" in text
    assert "需本人填写" in text
    assert "仍需本人补齐" in text


def test_pack_of_unknown_application_is_rejected(store):
    with pytest.raises(ValueError, match="申请不存在"):
        build_pack(store, 999)


def test_pack_module_never_touches_the_network():
    """信息包只在本机整理资料，代码里不应出现任何联网或自动填写依赖。"""
    source = Path(application_pack.__file__).read_text(encoding="utf-8")
    for forbidden in ("playwright", "requests", "httpx", "smtplib", "selenium",
                      "urllib.request", "subprocess"):
        assert forbidden not in source


def test_pack_route_requires_login(client, application):
    response = client.get(f"/api/v1/applications/{application['id']}/pack")
    assert response.status_code == 401
    assert response.get_json()["ok"] is False


def test_pack_route_returns_preview_and_saved_version(auth_client, application):
    preview = auth_client.get(f"/api/v1/applications/{application['id']}/pack")
    assert preview.status_code == 200
    assert preview.get_json()["pack"]["application_id"] == application["id"]

    saved = auth_client.post(f"/api/v1/applications/{application['id']}/pack",
                             headers={"X-CSRF": "test-csrf-token"})
    assert saved.status_code == 200
    assert saved.get_json()["pack"]["version"] == 1


def test_pack_export_returns_markdown_attachment(auth_client, application):
    response = auth_client.get(f"/applications/{application['id']}/pack/export")
    assert response.status_code == 200
    assert "text/markdown" in response.headers["Content-Type"]
    assert "attachment" in response.headers["Content-Disposition"]
    assert "投递信息包" in response.get_data(as_text=True)


def test_pack_page_shows_the_saved_version(auth_client, application):
    auth_client.post(f"/applications/{application['id']}/pack",
                     headers={"X-CSRF": "test-csrf-token"})
    page = auth_client.get(f"/applications/{application['id']}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "投递信息包" in body
    assert "程序不填写、不上传、不提交任何真实招聘表单" in body
