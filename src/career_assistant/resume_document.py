"""Structured resume document validation and deterministic text conversion."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from career_assistant.ai import AIError, AIReviewError, AIService


TEMPLATE_VERSION = "resume-a4-v1"
DELIVERY_FIELDS = ("name", "email", "phone", "location", "links")
RESUME_KEYS = {
    "headline",
    "headline_fact_ids",
    "summary",
    "skill_groups",
    "experiences",
    "education",
    "certificates",
    "additional",
}
RESUME_SCHEMA = (
    '{"headline":string,"headline_fact_ids":[string],'
    '"summary":{"text":string,"fact_ids":[string]},'
    '"skill_groups":[{"name":string,"items":[{"name":string,'
    '"fact_ids":[string]}]}],'
    '"experiences":[{"role":string,"organization":string,"location":string,'
    '"start_date":"YYYY-MM","end_date":"YYYY-MM|","fact_ids":[string],'
    '"highlights":[{"text":string,"fact_ids":[string]}]}],'
    '"education":[{"institution":string,"degree":string,"location":string,'
    '"start_date":"YYYY-MM","end_date":"YYYY-MM|","fact_ids":[string],'
    '"details":[{"text":string,"fact_ids":[string]}]}],'
    '"certificates":[{"name":string,"issuer":string,"issued_on":"YYYY-MM-DD|",'
    '"fact_ids":[string]}],'
    '"additional":[{"title":string,"text":string,"fact_ids":[string]}]}'
)
AUDIT_SCHEMA = (
    '{"approved":boolean,"summary":string,"findings":[{"id":string,'
    '"section":string,"status":"pass|issue","title":string,"detail":string,'
    '"fact_ids":[string]}]}'
)


class ResumeValidationError(ValueError):
    """The generated resume is not safe to render or save."""

    def __init__(self, message: str, *, boundary_code: str = "") -> None:
        self.boundary_code = boundary_code
        super().__init__(message)


def canonical_resume_json(spec: dict) -> str:
    return json.dumps(
        spec,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def resume_content_hash(spec: dict) -> str:
    return hashlib.sha256(canonical_resume_json(spec).encode("utf-8")).hexdigest()


def validate_delivery_profile(values: Any) -> dict:
    source = values if isinstance(values, dict) else {}

    def text(key: str, label: str, limit: int, *, required: bool = False) -> str:
        value = str(source.get(key) or "").strip()
        if required and not value:
            raise ResumeValidationError(f"请填写{label}")
        if len(value) > limit:
            raise ResumeValidationError(f"{label}不能超过 {limit} 个字符")
        if "\x00" in value or "\r" in value:
            raise ResumeValidationError(f"{label}包含无效字符")
        return value

    name = text("name", "姓名", 80, required=True)
    email = text("email", "邮箱", 254)
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ResumeValidationError("邮箱格式不正确")
    phone = text("phone", "电话", 40)
    if phone and (
        not re.fullmatch(r"[0-9+()\-. \u00a0]+", phone)
        or not 6 <= len(re.sub(r"\D", "", phone)) <= 20
    ):
        raise ResumeValidationError("电话格式不正确")
    location = text("location", "地点", 120)
    raw_links = source.get("links", [])
    if isinstance(raw_links, str):
        raw_links = re.split(r"[\n,]+", raw_links)
    if not isinstance(raw_links, list):
        raise ResumeValidationError("链接必须为列表")
    links: list[str] = []
    for raw_link in raw_links:
        link = str(raw_link or "").strip()
        if not link:
            continue
        if len(link) > 500:
            raise ResumeValidationError("单个链接不能超过 500 个字符")
        parsed = urlsplit(link)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ResumeValidationError("链接必须是 HTTP(S) 地址")
        if link not in links:
            links.append(link)
    if len(links) > 5:
        raise ResumeValidationError("最多填写 5 个链接")
    return {
        "name": name,
        "email": email,
        "phone": phone,
        "location": location,
        "links": links,
    }


def _known_facts(facts: dict) -> dict[str, dict]:
    rows = facts.get("facts") if isinstance(facts, dict) else None
    if not isinstance(rows, list):
        raise ResumeValidationError("职业事实格式无效")
    known: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fact_id = str(row.get("id") or "").strip()
        text = str(row.get("text") or "").strip()
        if fact_id and text:
            known[fact_id] = row
    if not known:
        raise ResumeValidationError("职业资料库没有可用于生成简历的事实")
    return known


def _text(
    value: Any,
    label: str,
    limit: int,
    *,
    required: bool = False,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ResumeValidationError(f"{label}必须为文字")
    result = value.strip()
    if required and not result:
        raise ResumeValidationError(f"{label}不能为空")
    if len(result) > limit:
        raise ResumeValidationError(f"{label}不能超过 {limit} 个字符")
    if any(token in result for token in ("\x00", "\r")):
        raise ResumeValidationError(f"{label}包含无效字符")
    return result


def _keys(row: Any, expected: set[str], label: str) -> dict:
    if not isinstance(row, dict) or set(row) != expected:
        raise ResumeValidationError(f"{label}字段不符合约定结构")
    return row


def _fact_ids(
    value: Any,
    known: dict[str, dict],
    label: str,
    *,
    required: bool = True,
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ResumeValidationError(f"{label}必须为事实 ID 列表")
    result: list[str] = []
    for item in value:
        fact_id = item.strip()
        if not fact_id or fact_id not in known:
            raise ResumeValidationError(
                f"{label}引用了不存在的事实：{fact_id or '空值'}",
                boundary_code="unsupported_fact_reference",
            )
        if fact_id not in result:
            result.append(fact_id)
    if required and not result:
        raise ResumeValidationError(f"{label}缺少事实依据")
    return result


def _fact_text(refs: list[str], known: dict[str, dict]) -> str:
    values = []
    for fact_id in refs:
        row = known[fact_id]
        values.extend([str(row.get("text") or ""), str(row.get("evidence") or "")])
    return "\n".join(values)


def _check_claim(
    text: str,
    refs: list[str],
    known: dict[str, dict],
    label: str,
) -> None:
    try:
        AIService._check_numbers(text, refs, known)
    except AIError as exc:
        raise ResumeValidationError(
            f"{label}：{exc}",
            boundary_code="unsupported_numeric_claim",
        ) from None
    try:
        AIService._check_ownership(text, refs, known)
    except AIReviewError as exc:
        raise ResumeValidationError(
            f"{label}：{exc}",
            boundary_code="ownership_overclaim",
        ) from None
    except AIError as exc:
        raise ResumeValidationError(f"{label}：{exc}") from None
    if re.search(
        r"待确认|待確認|需本人补充|需本人補充|未知|TBD|TODO|"
        r"\[[^\]]*(?:补充|補充|TODO|TBD)[^\]]*\]",
        text,
        re.I,
    ):
        raise ResumeValidationError(f"{label}包含未解决的内容占位")


def _check_phrase(value: str, refs: list[str], known: dict[str, dict], label: str) -> None:
    if not value:
        return
    evidence = _fact_text(refs, known).casefold()
    if value.casefold() not in evidence:
        raise ResumeValidationError(f"{label}没有在引用事实中直接出现")


def _date(value: Any, label: str) -> str:
    text = _text(value, label, 7)
    if not text:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}", text):
        raise ResumeValidationError(
            f"{label}格式应为 YYYY-MM",
            boundary_code="unsupported_numeric_claim",
        )
    try:
        date.fromisoformat(f"{text}-01")
    except ValueError:
        raise ResumeValidationError(
            f"{label}不是有效月份",
            boundary_code="unsupported_numeric_claim",
        ) from None
    return text


def _date_range(start: str, end: str, label: str) -> tuple[str, str]:
    if end and not start:
        raise ResumeValidationError(
            f"{label}有结束时间时必须填写开始时间",
            boundary_code="unsupported_numeric_claim",
        )
    if start and end and end < start:
        raise ResumeValidationError(
            f"{label}结束时间不能早于开始时间",
            boundary_code="unsupported_numeric_claim",
        )
    return start, end


def validate_resume_spec(raw: Any, facts: dict) -> dict:
    """Validate all model output and return the canonical ResumeSpec shape."""
    known = _known_facts(facts)
    row = _keys(raw, RESUME_KEYS, "简历")
    headline = _text(row.get("headline"), "简历标题", 160, required=True)
    headline_ids = _fact_ids(
        row.get("headline_fact_ids"),
        known,
        "简历标题",
        required=True,
    )
    _check_claim(headline, headline_ids, known, "简历标题")

    summary_row = _keys(row.get("summary"), {"text", "fact_ids"}, "摘要")
    summary_text = _text(summary_row.get("text"), "摘要", 1200, required=True)
    summary_ids = _fact_ids(summary_row.get("fact_ids"), known, "摘要")
    _check_claim(summary_text, summary_ids, known, "摘要")

    groups = row.get("skill_groups")
    if not isinstance(groups, list) or len(groups) > 8:
        raise ResumeValidationError("技能分组必须为最多 8 组的列表")
    skill_groups = []
    total_skills = 0
    for group_index, group in enumerate(groups, 1):
        group = _keys(group, {"name", "items"}, f"技能分组 {group_index}")
        group_name = _text(group.get("name"), f"技能分组 {group_index} 名称", 60, required=True)
        items = group.get("items")
        if not isinstance(items, list) or len(items) > 30:
            raise ResumeValidationError("每个技能分组最多包含 30 项")
        normalized_items = []
        for item_index, item in enumerate(items, 1):
            item = _keys(item, {"name", "fact_ids"}, "技能")
            name = _text(item.get("name"), "技能名称", 100, required=True)
            refs = _fact_ids(item.get("fact_ids"), known, f"技能「{name}」")
            _check_claim(name, refs, known, f"技能「{name}」")
            _check_phrase(name, refs, known, f"技能「{name}」")
            normalized_items.append({"name": name, "fact_ids": refs})
            total_skills += 1
        if normalized_items:
            skill_groups.append({"name": group_name, "items": normalized_items})
    if total_skills > 40:
        raise ResumeValidationError("技能总数不能超过 40 项")

    experiences = row.get("experiences")
    if not isinstance(experiences, list) or len(experiences) > 12:
        raise ResumeValidationError("经历必须为最多 12 段的列表")
    normalized_experiences = []
    for index, item in enumerate(experiences, 1):
        item = _keys(
            item,
            {
                "role",
                "organization",
                "location",
                "start_date",
                "end_date",
                "fact_ids",
                "highlights",
            },
            f"经历 {index}",
        )
        role = _text(item.get("role"), f"经历 {index} 角色", 120, required=True)
        organization = _text(item.get("organization"), f"经历 {index} 组织", 160)
        location = _text(item.get("location"), f"经历 {index} 地点", 120)
        start = _date(item.get("start_date"), f"经历 {index} 开始月份")
        end = _date(item.get("end_date"), f"经历 {index} 结束月份")
        _date_range(start, end, f"经历 {index}")
        refs = _fact_ids(item.get("fact_ids"), known, f"经历 {index}")
        _check_phrase(role, refs, known, f"经历 {index} 角色")
        _check_phrase(organization, refs, known, f"经历 {index} 组织")
        _check_claim(
            " ".join((role, organization, location, start, end)),
            refs,
            known,
            f"经历 {index}",
        )
        highlights = item.get("highlights")
        if not isinstance(highlights, list) or len(highlights) > 8:
            raise ResumeValidationError(f"经历 {index} 的要点必须为最多 8 条列表")
        normalized_highlights = []
        for highlight in highlights:
            highlight = _keys(highlight, {"text", "fact_ids"}, "经历要点")
            highlight_text = _text(highlight.get("text"), "经历要点", 700, required=True)
            highlight_refs = _fact_ids(
                highlight.get("fact_ids"),
                known,
                "经历要点",
            )
            _check_claim(highlight_text, highlight_refs, known, "经历要点")
            normalized_highlights.append(
                {"text": highlight_text, "fact_ids": highlight_refs}
            )
        normalized_experiences.append(
            {
                "role": role,
                "organization": organization,
                "location": location,
                "start_date": start,
                "end_date": end,
                "fact_ids": refs,
                "highlights": normalized_highlights,
            }
        )

    education = row.get("education")
    if not isinstance(education, list) or len(education) > 8:
        raise ResumeValidationError("教育经历必须为最多 8 段的列表")
    normalized_education = []
    for index, item in enumerate(education, 1):
        item = _keys(
            item,
            {
                "institution",
                "degree",
                "location",
                "start_date",
                "end_date",
                "fact_ids",
                "details",
            },
            f"教育经历 {index}",
        )
        institution = _text(item.get("institution"), f"教育经历 {index} 学校", 200, required=True)
        degree = _text(item.get("degree"), f"教育经历 {index} 学位", 160)
        location = _text(item.get("location"), f"教育经历 {index} 地点", 120)
        start = _date(item.get("start_date"), f"教育经历 {index} 开始月份")
        end = _date(item.get("end_date"), f"教育经历 {index} 结束月份")
        _date_range(start, end, f"教育经历 {index}")
        refs = _fact_ids(item.get("fact_ids"), known, f"教育经历 {index}")
        _check_phrase(institution, refs, known, f"教育经历 {index} 学校")
        _check_phrase(degree, refs, known, f"教育经历 {index} 学位")
        details = item.get("details")
        if not isinstance(details, list) or len(details) > 6:
            raise ResumeValidationError(f"教育经历 {index} 详情必须为最多 6 条列表")
        normalized_details = []
        for detail in details:
            detail = _keys(detail, {"text", "fact_ids"}, "教育详情")
            detail_text = _text(detail.get("text"), "教育详情", 500, required=True)
            detail_refs = _fact_ids(detail.get("fact_ids"), known, "教育详情")
            _check_claim(detail_text, detail_refs, known, "教育详情")
            normalized_details.append({"text": detail_text, "fact_ids": detail_refs})
        normalized_education.append(
            {
                "institution": institution,
                "degree": degree,
                "location": location,
                "start_date": start,
                "end_date": end,
                "fact_ids": refs,
                "details": normalized_details,
            }
        )

    certificates = row.get("certificates")
    if not isinstance(certificates, list) or len(certificates) > 12:
        raise ResumeValidationError("证书必须为最多 12 项的列表")
    normalized_certificates = []
    for index, item in enumerate(certificates, 1):
        item = _keys(
            item,
            {"name", "issuer", "issued_on", "fact_ids"},
            f"证书 {index}",
        )
        name = _text(item.get("name"), f"证书 {index} 名称", 160, required=True)
        issuer = _text(item.get("issuer"), f"证书 {index} 颁发机构", 160, required=True)
        issued_on = _text(item.get("issued_on"), f"证书 {index} 日期", 10)
        if issued_on:
            try:
                date.fromisoformat(issued_on)
            except ValueError:
                raise ResumeValidationError(f"证书 {index} 日期不是有效日期") from None
        refs = _fact_ids(item.get("fact_ids"), known, f"证书 {index}")
        _check_phrase(name, refs, known, f"证书 {index} 名称")
        _check_phrase(issuer, refs, known, f"证书 {index} 颁发机构")
        _check_claim(
            " ".join((name, issuer, issued_on)),
            refs,
            known,
            f"证书 {index}",
        )
        normalized_certificates.append(
            {
                "name": name,
                "issuer": issuer,
                "issued_on": issued_on,
                "fact_ids": refs,
            }
        )

    additional = row.get("additional")
    if not isinstance(additional, list) or len(additional) > 12:
        raise ResumeValidationError("补充资料必须为最多 12 项的列表")
    normalized_additional = []
    for index, item in enumerate(additional, 1):
        item = _keys(item, {"title", "text", "fact_ids"}, f"补充资料 {index}")
        title = _text(item.get("title"), f"补充资料 {index} 标题", 120, required=True)
        text = _text(item.get("text"), f"补充资料 {index} 内容", 1200, required=True)
        refs = _fact_ids(item.get("fact_ids"), known, f"补充资料 {index}")
        _check_claim(text, refs, known, f"补充资料 {index}")
        normalized_additional.append({"title": title, "text": text, "fact_ids": refs})

    if not (
        summary_text
        or skill_groups
        or normalized_experiences
        or normalized_education
        or normalized_certificates
        or normalized_additional
    ):
        raise ResumeValidationError("简历没有可用于展示的内容")

    return {
        "headline": headline,
        "headline_fact_ids": headline_ids,
        "summary": {"text": summary_text, "fact_ids": summary_ids},
        "skill_groups": skill_groups,
        "experiences": normalized_experiences,
        "education": normalized_education,
        "certificates": normalized_certificates,
        "additional": normalized_additional,
    }


def validate_resume_audit(raw: Any, known_fact_ids: set[str]) -> dict:
    row = _keys(raw, {"approved", "summary", "findings"}, "独立审计")
    if type(row.get("approved")) is not bool:
        raise ResumeValidationError("独立审计缺少有效结论")
    summary = _text(row.get("summary"), "审计总结", 1000, required=True)
    findings = row.get("findings")
    if not isinstance(findings, list) or not 1 <= len(findings) <= 40:
        raise ResumeValidationError("独立审计必须包含 1 至 40 条分析")
    normalized = []
    seen: set[str] = set()
    for index, item in enumerate(findings, 1):
        item = _keys(
            item,
            {"id", "section", "status", "title", "detail", "fact_ids"},
            f"审计项 {index}",
        )
        finding_id = _text(item.get("id"), f"审计项 {index} ID", 80, required=True)
        if finding_id in seen:
            raise ResumeValidationError("审计项 ID 不能重复")
        seen.add(finding_id)
        section = _text(item.get("section"), f"审计项 {index} 分类", 80, required=True)
        status = _text(item.get("status"), f"审计项 {index} 状态", 20, required=True)
        if status not in {"pass", "issue"}:
            raise ResumeValidationError("审计项状态必须为 pass 或 issue")
        title = _text(item.get("title"), f"审计项 {index} 标题", 160, required=True)
        detail = _text(item.get("detail"), f"审计项 {index} 说明", 1000, required=True)
        refs = _fact_ids(
            item.get("fact_ids"),
            {fact_id: {} for fact_id in known_fact_ids},
            f"审计项 {index}",
            required=False,
        )
        normalized.append(
            {
                "id": finding_id,
                "section": section,
                "status": status,
                "title": title,
                "detail": detail,
                "fact_ids": refs,
            }
        )
    has_issue = any(item["status"] == "issue" for item in normalized)
    if row["approved"] == has_issue:
        raise ResumeValidationError("审计总结论与逐项结果矛盾")
    return {"approved": row["approved"], "summary": summary, "findings": normalized}


def _period(start: str, end: str) -> str:
    if start and end:
        return f"{start} - {end}"
    if start:
        return f"{start} - 至今"
    return ""


def resume_plaintext(spec: dict, delivery: dict) -> str:
    """Flatten the final document for storage/search without changing the PDF."""
    lines = [delivery["name"]]
    contact = [delivery.get("email"), delivery.get("phone"), delivery.get("location")]
    contact.extend(delivery.get("links") or [])
    lines.extend(value for value in contact if value)
    lines.extend(["", spec["headline"], "", "摘要", spec["summary"]["text"]])
    if spec["skill_groups"]:
        lines.extend(["", "技能"])
        for group in spec["skill_groups"]:
            values = [item["name"] for item in group["items"]]
            lines.append(f"{group['name']}：{'、'.join(values)}")
    if spec["experiences"]:
        lines.extend(["", "经历"])
        for item in spec["experiences"]:
            heading = " | ".join(
                value
                for value in (
                    item["role"],
                    item["organization"],
                    item["location"],
                    _period(item["start_date"], item["end_date"]),
                )
                if value
            )
            lines.append(heading)
            lines.extend(f"- {row['text']}" for row in item["highlights"])
    if spec["education"]:
        lines.extend(["", "教育"])
        for item in spec["education"]:
            heading = " | ".join(
                value
                for value in (
                    item["institution"],
                    item["degree"],
                    item["location"],
                    _period(item["start_date"], item["end_date"]),
                )
                if value
            )
            lines.append(heading)
            lines.extend(f"- {row['text']}" for row in item["details"])
    if spec["certificates"]:
        lines.extend(["", "证书"])
        for item in spec["certificates"]:
            value = f"{item['name']} - {item['issuer']}"
            if item["issued_on"]:
                value += f" ({item['issued_on']})"
            lines.append(value)
    if spec["additional"]:
        lines.extend(["", "补充"])
        for item in spec["additional"]:
            lines.append(f"{item['title']}：{item['text']}")
    return "\n".join(line for line in lines if line is not None).strip()
