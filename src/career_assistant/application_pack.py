"""本地投递信息包（application pack）：把已保存的依据整理成一份可自行填写的清单。

这是分工表里「模拟填表」的落地形态，边界与 REQUIREMENTS 第 2 章完全一致：
- **不访问、不识别、不填写**任何真实招聘网站的表单字段；
- **不上传文件、不点击提交**，投递动作始终由用户自己完成；
- 只使用已保存的投递信息版本、简历事实版本、结构化资料与材料版本；
- 缺失的内容一律标为「需本人填写」，绝不编造姓名、联系方式、经历或附件；
- 每次生成保存为不可变版本，历史版本可回看，新的依据只影响新版本。

换句话说：程序负责「把资料整理齐、标清楚哪一格还空着」，剩下的复制粘贴与提交
都由用户在招聘网站上自己完成——这与产品定位里「用户自行投递」完全一致。
"""
from __future__ import annotations

from .store import stamp

PACK_VERSION = 1

STATUS_READY = "ready"
STATUS_UNKNOWN = "unknown"

STATUS_LABELS = {STATUS_READY: "已就绪", STATUS_UNKNOWN: "需本人填写"}

# 事实分类 → 信息包里的分组标题。分组顺序即展示顺序。
FACT_GROUPS = (
    ("education", "教育背景"),
    ("experience", "工作／实习经历"),
    ("project", "项目经历"),
    ("skill", "技能"),
    ("other", "其他事实"),
)

NOTICE = ("本信息包只在本机生成，供本人在招聘网站自行填写与核对；"
          "程序不填写、不上传、不提交任何真实招聘表单。")


def _field(key: str, label: str, value: str, source: str = "") -> dict:
    """一个信息包字段：有内容就是「已就绪」，空的就是「需本人填写」。"""
    text = str(value or "").strip()
    return {
        "key": key,
        "label": label,
        "value": text,
        "status": STATUS_READY if text else STATUS_UNKNOWN,
        "source": source,
    }


def _delivery_fields(delivery: dict | None) -> list[dict]:
    """投递信息：姓名与联系方式，只取用户自己确认过的版本。"""
    data = (delivery or {}).get("data") or {}
    source = f"投递信息版本 #{delivery.get('id')}" if delivery else "未确认投递信息"
    links = [str(item).strip() for item in (data.get("links") or []) if str(item).strip()]
    fields = [
        _field("name", "姓名", data.get("name", ""), source),
        _field("email", "邮箱", data.get("email", ""), source),
        _field("phone", "电话", data.get("phone", ""), source),
        _field("location", "所在地区", data.get("location", ""), source),
    ]
    for index, link in enumerate(links, 1):
        fields.append(_field(f"link_{index}", f"个人链接 {index}", link, source))
    return fields


def _fact_groups(facts: list[dict], source: str) -> list[dict]:
    """把简历事实按分类整理；只搬运已保存的事实原文，不改写。"""
    groups = []
    for key, title in FACT_GROUPS:
        rows = [item for item in facts
                if isinstance(item, dict) and str(item.get("category") or "") == key]
        if not rows:
            continue
        groups.append({
            "key": key,
            "title": title,
            "items": [{
                "text": str(item.get("text") or "").strip(),
                "evidence": str(item.get("evidence") or "").strip(),
                "origin": str(item.get("origin") or ""),
            } for item in rows if str(item.get("text") or "").strip()],
            "source": source,
        })
    return groups


def _profile_groups(profile: dict | None, source: str) -> list[dict]:
    """结构化资料：技能、经历、证书与补充材料，供本人挑选填写。"""
    data = (profile or {}).get("data") or {}
    groups = []
    for key, title in (("skills", "技能（结构化资料）"),
                       ("experiences", "经历（结构化资料）"),
                       ("certificates", "证书（结构化资料）"),
                       ("additional", "补充材料（结构化资料）")):
        rows = [item for item in (data.get(key) or []) if isinstance(item, dict)]
        if not rows:
            continue
        groups.append({"key": key, "title": title, "items": rows, "source": source})
    return groups


def _attachments(resume: dict | None, version: dict | None) -> list[dict]:
    """附件清单：只列出本机已保存的文件，由用户自行上传。"""
    attachments = []
    if resume:
        attachments.append({
            "label": "简历原始 PDF",
            "filename": str(resume.get("filename") or ""),
            "note": "本机已保存，请自行上传到招聘网站。",
        })
    return attachments


def _questions(material: dict | None) -> list[dict]:
    """JD 中实际出现的问题：来自材料里的问题草稿，仍需本人确认。"""
    data = (material or {}).get("data") or {}
    questions = []
    for item in data.get("answers") or []:
        if not isinstance(item, dict):
            continue
        answer = str(item.get("answer") or "").strip()
        questions.append({
            "question": str(item.get("question") or "").strip(),
            "answer": answer,
            "status": STATUS_READY if answer else STATUS_UNKNOWN,
            "note": "草稿仅供参考，提交前请本人核对。",
        })
    return questions


def build_pack(store, application_id: int) -> dict:
    """按当前已保存的依据组装投递信息包；不写库。"""
    application = store.get("applications", int(application_id))
    if application is None:
        raise ValueError("申请不存在")
    snapshot = store.get("job_snapshots", application.get("course_snapshot_id") or 0) or {}
    job = store.get("jobs", application.get("job_uid") or "") or {}
    version = store.get("resume_versions", application.get("course_resume_version_id") or 0) or {}
    resume = store.get("resumes", version.get("resume_id") or 0) if version else None
    material = store.get("materials", application.get("course_material_id") or 0) or {}
    delivery = store.latest_delivery_profile()
    profile = store.latest_profile_version()

    facts = list((version.get("facts") or {}).get("facts") or [])
    fact_source = f"简历事实版本 #{version.get('id')}" if version else "未关联简历事实版本"
    profile_source = f"职业资料版本 #{profile.get('id')}" if profile else "未保存职业资料"

    fields = _delivery_fields(delivery)
    fields.append(_field("job_title", "应聘职位", application.get("title", ""), "申请记录"))
    fields.append(_field("job_company", "公司", application.get("company", ""), "申请记录"))
    fields.append(_field("job_url", "原岗位链接", job.get("url") or snapshot.get("url") or "",
                         "岗位来源记录"))
    fields.append(_field("job_location", "工作地点", job.get("location") or "", "岗位来源记录"))
    fields.append(_field("source", "信息来源", snapshot.get("source") or job.get("source") or "",
                         "岗位来源记录"))
    fields.append(_field("status", "当前申请状态", application.get("status", ""), "申请记录"))

    groups = _fact_groups(facts, fact_source) + _profile_groups(profile, profile_source)
    questions = _questions(material)
    missing = [item["label"] for item in fields if item["status"] == STATUS_UNKNOWN]
    missing += [f"问题草稿：{item['question']}" for item in questions
                if item["status"] == STATUS_UNKNOWN]

    return {
        "pack_version": PACK_VERSION,
        "application_id": int(application_id),
        "status": application.get("status", ""),
        "fields": fields,
        "groups": groups,
        "questions": questions,
        "attachments": _attachments(resume, version),
        "cover_letter": str((material.get("data") or {}).get("cover_letter") or ""),
        "missing": missing,
        "sources": {
            "resume_version_id": version.get("id"),
            "resume_id": version.get("resume_id"),
            "material_id": material.get("id"),
            "profile_version_id": profile.get("id") if profile else None,
            "delivery_version_id": delivery.get("id") if delivery else None,
            "snapshot_id": application.get("course_snapshot_id"),
        },
        "notice": NOTICE,
        "generated_at": stamp(),
    }


def pack_markdown(pack: dict) -> str:
    """把信息包导出成可复制的 Markdown 清单。"""
    lines = ["# 投递信息包（本地预览）", "", f"> {pack.get('notice', NOTICE)}", ""]
    lines.append(f"生成时间：{pack.get('generated_at', '')}")
    lines.append("")
    lines.append("## 基础信息")
    for item in pack.get("fields") or []:
        value = item.get("value") or "（需本人填写）"
        lines.append(f"- **{item['label']}**：{value}"
                     + (f"（来源：{item['source']}）" if item.get("source") else ""))
    lines.append("")
    for group in pack.get("groups") or []:
        lines.append(f"## {group['title']}")
        for item in group.get("items") or []:
            if isinstance(item, dict) and "text" in item:
                lines.append(f"- {item['text']}")
            else:
                lines.append(f"- {item}")
        lines.append("")
    if pack.get("cover_letter"):
        lines += ["## 求职信草稿", "", pack["cover_letter"], ""]
    if pack.get("questions"):
        lines.append("## JD 中的问题（草稿，需本人确认）")
        for item in pack["questions"]:
            answer = item.get("answer") or "（需本人填写）"
            lines.append(f"- **{item['question']}**：{answer}")
        lines.append("")
    if pack.get("attachments"):
        lines.append("## 需要自行上传的附件")
        for item in pack["attachments"]:
            lines.append(f"- {item['label']}：{item.get('filename') or '未保存'}"
                         f"（{item.get('note', '')}）")
        lines.append("")
    if pack.get("missing"):
        lines.append("## 仍需本人补齐")
        for label in pack["missing"]:
            lines.append(f"- {label}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def create_pack(store, application_id: int) -> dict:
    """生成并保存一个新的信息包版本，返回带版本号的记录。"""
    pack = build_pack(store, application_id)
    pack_id = store.save_application_pack(application_id, pack)
    return {**pack, "id": pack_id,
            "version": store.get("application_packs", pack_id)["version"]}
