"""Normalize and validate AI resume audit items."""
from __future__ import annotations

import re


AUDIT_STATUSES = frozenset({"pass", "issue"})


def _clean_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def validate_audit_items(items, source: str) -> list[dict]:
    """Return a whitelisted audit list whose evidence exists in the resume."""
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError("AI 审计结果必须包含 1 至 12 条检查项。")

    source_flat = re.sub(r"\s+", " ", source).strip().casefold()
    cleaned = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("AI 审计项必须是结构化对象。")
        status = str(raw.get("status") or "").strip().lower()
        if status not in AUDIT_STATUSES:
            raise ValueError("AI 审计项状态只能是通过或问题。")
        category = _clean_text(raw.get("category"), 30)
        title = _clean_text(raw.get("title"), 80)
        summary = _clean_text(raw.get("summary"), 320)
        recommendation = _clean_text(raw.get("recommendation"), 500)
        evidence = _clean_text(raw.get("evidence"), 1000)
        if not category or not title or not summary or not evidence:
            raise ValueError("AI 审计项缺少分类、结论、摘要或原文依据。")
        if status == "issue" and not recommendation:
            raise ValueError("AI 审计问题项缺少处理建议。")
        if evidence.casefold() not in source_flat:
            raise ValueError("AI 审计项引用了无法在简历原文中找到的依据。")
        cleaned.append({
            "category": category,
            "status": status,
            "title": title,
            "summary": summary,
            "recommendation": recommendation,
            "evidence": evidence,
        })
    return cleaned


def _stored_items(items) -> list[dict]:
    if not isinstance(items, list):
        return []
    cleaned = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "").strip().lower()
        title = _clean_text(raw.get("title"), 80)
        summary = _clean_text(raw.get("summary"), 320)
        evidence = _clean_text(raw.get("evidence"), 1000)
        if status not in AUDIT_STATUSES or not title or not summary or not evidence:
            continue
        cleaned.append({
            "category": _clean_text(raw.get("category"), 30) or "其他检查",
            "status": status,
            "title": title,
            "summary": summary,
            "recommendation": _clean_text(raw.get("recommendation"), 500),
            "evidence": evidence,
        })
    return cleaned


_TITLE_RULES = (
    (r"示例数据|功能试用|请勿当作真实", "简历包含示例经历", "投递准备"),
    (r"与当前日期一致|至今.{0,40}(?:不冲突|一致)", "当前任职状态正常", "时间线"),
    (r"标注.{0,20}(?:基础|入门)|保持.{0,20}限定|不要.{0,20}升级为熟练", "技能熟练度标注合适", "技能"),
    (r"求职意向|多个方向|优先方向", "求职方向需要聚焦", "求职方向"),
    (r"量化|统计口径|样本来源|样本量", "量化成果缺少说明", "量化成果"),
    (r"项目.{0,30}(?:时间|场景|背景)|项目经历未标注", "项目缺少时间与场景", "项目经历"),
    (r"职责边界|独立主导|推动.{0,20}落地|主导落地", "职责边界需要澄清", "经历"),
    (r"教育|课程项目|毕业设计", "教育佐证可以补强", "教育"),
    (r"AI\s*审阅不能替代|人工.{0,12}(?:核对|复核)|投递前.{0,20}(?:核对|复核)", "投递前需要人工复核", "投递准备"),
)

_LEGACY_SUMMARIES = {
    "简历包含示例经历": "简历自述经历为示例数据，不能直接用于正式投递。",
    "当前任职状态正常": "“至今”与审计日期一致，时间线未发现冲突。",
    "技能熟练度标注合适": "基础与入门能力已明确限定，没有夸大熟练度。",
    "求职方向需要聚焦": "简历列出多个求职方向，重点和经历排序还不够集中。",
    "量化成果缺少说明": "成果数字较具体，但统计口径或样本来源尚未说明。",
    "项目缺少时间与场景": "项目未注明时间与所属场景，招聘方难以判断项目性质。",
    "职责边界需要澄清": "部分成果可能被理解为本人主导执行，职责边界需要写清。",
    "教育佐证可以补强": "教育部分目前以课程为主，可补充可验证的项目或毕业设计。",
    "投递前需要人工复核": "AI 审计不能替代本人核对，正式投递前仍需逐项复核。",
}

_PASS_TITLES = frozenset({
    "当前任职状态正常",
    "技能熟练度标注合适",
})

_ISSUE_HINTS = re.compile(
    r"不建议|不宜|需要|需(?:补充|删除|替换|确认|核实|聚焦|明确|调整)|"
    r"建议(?:本人|补充|删除|替换|确认|核实|聚焦|明确|调整|准备)|"
    r"未标注|缺少|待确认|投递前.{0,20}(?:核对|复核)|不能替代"
)


def _legacy_category(text: str, fallback: str) -> str:
    for pattern, category in (
        (r"示例|投递|真实材料|人工|复核", "投递准备"),
        (r"求职意向|方向", "求职方向"),
        (r"量化|数字|统计口径|样本", "量化成果"),
        (r"项目", "项目经历"),
        (r"技能|Power BI|dbt", "技能"),
        (r"教育|课程|毕业设计", "教育"),
        (r"至今|日期|时间线", "时间线"),
        (r"排版|措辞|表达", "表达"),
        (r"职责|主导|执行|落地", "经历"),
    ):
        if re.search(pattern, text, re.I):
            return category
    return fallback or "其他检查"


def _legacy_item(text: str, index: int) -> dict:
    title = ""
    category = ""
    for pattern, candidate, candidate_category in _TITLE_RULES:
        if re.search(pattern, text, re.I):
            title, category = candidate, candidate_category
            break
    if not title:
        category = _legacy_category(text, "其他检查")
        title = f"{category}需要检查"

    positive = title in _PASS_TITLES
    if not positive and title not in _LEGACY_SUMMARIES:
        positive = bool(re.search(r"清晰|完整|一致|符合|没有冲突", text)) and not _ISSUE_HINTS.search(text)
    status = "pass" if positive else "issue"
    summary = _LEGACY_SUMMARIES.get(title) or text
    return {
        "category": category or _legacy_category(text, "其他检查"),
        "status": status,
        "title": title,
        "summary": summary,
        "recommendation": text if status == "issue" else "",
        "evidence": "",
        "legacy_index": index,
    }


def normalize_report_data(data: dict) -> dict:
    """Add audit items to new and legacy report data without changing storage."""
    normalized = dict(data or {})
    items = _stored_items(normalized.get("audit_items"))
    if not items:
        suggestions = normalized.get("suggestions")
        if isinstance(suggestions, list):
            items = [
                _legacy_item(str(text).strip(), index)
                for index, text in enumerate(suggestions, 1)
                if str(text).strip()
            ]
    normalized["audit_items"] = items
    return normalized
