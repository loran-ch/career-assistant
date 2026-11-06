"""Validation for the job-focused plan produced before resume writing."""
from __future__ import annotations

from typing import Any

from career_assistant.resume_document import ResumeValidationError


RESUME_PLAN_SCHEMA = (
    '{"job_brief":{"role_focus":string,"priorities":[{"requirement":string,'
    '"importance":"must|preferred"}],"keywords":[string]},'
    '"evidence_map":[{"requirement":string,"fact_ids":[string],'
    '"strength":"strong|partial|missing","usage":string}],'
    '"strategy":{"headline_direction":string,"summary_focus":string,'
    '"section_order":["summary","skills","experiences","education",'
    '"certificates","additional"],"omit":[string]}}'
)

_PLAN_KEYS = {"job_brief", "evidence_map", "strategy"}
_BRIEF_KEYS = {"role_focus", "priorities", "keywords"}
_PRIORITY_KEYS = {"requirement", "importance"}
_EVIDENCE_KEYS = {"requirement", "fact_ids", "strength", "usage"}
_STRATEGY_KEYS = {
    "headline_direction",
    "summary_focus",
    "section_order",
    "omit",
}
_SECTIONS = {
    "summary",
    "skills",
    "experiences",
    "education",
    "certificates",
    "additional",
}


def _object(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ResumeValidationError(f"{label}字段不符合约定结构")
    return value


def _text(value: Any, label: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ResumeValidationError(f"{label}必须为文字")
    result = value.strip()
    if required and not result:
        raise ResumeValidationError(f"{label}不能为空")
    if len(result) > limit:
        raise ResumeValidationError(f"{label}不能超过 {limit} 个字符")
    if "\x00" in result or "\r" in result:
        raise ResumeValidationError(f"{label}包含无效字符")
    return result


def _text_list(
    value: Any,
    label: str,
    *,
    limit: int,
    item_limit: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ResumeValidationError(f"{label}必须为最多 {limit} 项的列表")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item, label, item_limit)
        key = " ".join(text.casefold().split())
        if key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _known_fact_ids(facts: dict) -> set[str]:
    rows = facts.get("facts") if isinstance(facts, dict) else None
    if not isinstance(rows, list):
        raise ResumeValidationError("职业事实格式无效")
    known = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        fact_id = str(row.get("id") or "").strip()
        if fact_id and str(row.get("text") or "").strip():
            known.add(fact_id)
    if not known:
        raise ResumeValidationError("职业资料库没有可用于生成简历的事实")
    return known


def validate_resume_plan(raw: Any, facts: dict) -> dict:
    """Normalize a writer plan and reject unsupported evidence references."""
    known = _known_fact_ids(facts)
    row = _object(raw, _PLAN_KEYS, "简历策略")
    brief = _object(row.get("job_brief"), _BRIEF_KEYS, "岗位简报")
    role_focus = _text(brief.get("role_focus"), "岗位重点", 300)

    raw_priorities = brief.get("priorities")
    if not isinstance(raw_priorities, list) or not 1 <= len(raw_priorities) <= 8:
        raise ResumeValidationError("岗位重点必须包含 1 至 8 项")
    priorities = []
    priority_keys: set[str] = set()
    for index, item in enumerate(raw_priorities, 1):
        item = _object(item, _PRIORITY_KEYS, f"岗位重点 {index}")
        requirement = _text(item.get("requirement"), f"岗位重点 {index}", 500)
        importance = _text(item.get("importance"), f"岗位重点 {index} 级别", 20)
        if importance not in {"must", "preferred"}:
            raise ResumeValidationError("岗位重点级别必须为 must 或 preferred")
        key = " ".join(requirement.casefold().split())
        if key in priority_keys:
            raise ResumeValidationError("岗位重点不能重复")
        priority_keys.add(key)
        priorities.append({"requirement": requirement, "importance": importance})

    keywords = _text_list(
        brief.get("keywords"),
        "ATS 关键词",
        limit=16,
        item_limit=80,
    )

    raw_evidence = row.get("evidence_map")
    if not isinstance(raw_evidence, list) or not 1 <= len(raw_evidence) <= 16:
        raise ResumeValidationError("证据映射必须包含 1 至 16 项")
    evidence_map = []
    usable_fact_ids: set[str] = set()
    for index, item in enumerate(raw_evidence, 1):
        item = _object(item, _EVIDENCE_KEYS, f"证据映射 {index}")
        requirement = _text(item.get("requirement"), f"证据映射 {index} 要求", 500)
        strength = _text(item.get("strength"), f"证据映射 {index} 强度", 20)
        if strength not in {"strong", "partial", "missing"}:
            raise ResumeValidationError("证据强度必须为 strong、partial 或 missing")
        usage = _text(
            item.get("usage"),
            f"证据映射 {index} 用途",
            500,
            required=False,
        )
        refs = item.get("fact_ids")
        if not isinstance(refs, list) or any(not isinstance(fact_id, str) for fact_id in refs):
            raise ResumeValidationError(f"证据映射 {index} 必须引用事实 ID 列表")
        normalized_refs = []
        for fact_id in refs:
            fact_id = fact_id.strip()
            if not fact_id or fact_id not in known:
                raise ResumeValidationError(
                    f"证据映射 {index} 引用了不存在的事实：{fact_id or '空值'}"
                )
            if fact_id not in normalized_refs:
                normalized_refs.append(fact_id)
        if strength == "missing" and normalized_refs:
            raise ResumeValidationError("缺失证据不能引用事实 ID")
        if strength != "missing" and not normalized_refs:
            raise ResumeValidationError("已有证据必须引用事实 ID")
        usable_fact_ids.update(normalized_refs)
        evidence_map.append(
            {
                "requirement": requirement,
                "fact_ids": normalized_refs,
                "strength": strength,
                "usage": usage,
            }
        )
    if not usable_fact_ids:
        raise ResumeValidationError("简历策略没有选择任何可用的候选人证据")

    strategy = _object(row.get("strategy"), _STRATEGY_KEYS, "写作策略")
    headline_direction = _text(strategy.get("headline_direction"), "标题方向", 300)
    summary_focus = _text(strategy.get("summary_focus"), "摘要重点", 800)
    raw_order = strategy.get("section_order")
    if not isinstance(raw_order, list) or not 1 <= len(raw_order) <= len(_SECTIONS):
        raise ResumeValidationError("版块顺序必须包含 1 至 6 个版块")
    section_order = []
    for section in raw_order:
        if not isinstance(section, str) or section not in _SECTIONS:
            raise ResumeValidationError("版块顺序包含未知版块")
        if section in section_order:
            raise ResumeValidationError("版块顺序不能重复")
        section_order.append(section)
    omit = _text_list(
        strategy.get("omit"),
        "省略内容",
        limit=10,
        item_limit=200,
    )
    return {
        "job_brief": {
            "role_focus": role_focus,
            "priorities": priorities,
            "keywords": keywords,
        },
        "evidence_map": evidence_map,
        "strategy": {
            "headline_direction": headline_direction,
            "summary_focus": summary_focus,
            "section_order": section_order,
            "omit": omit,
        },
    }


def _resume_fact_ids(spec: dict) -> set[str]:
    used = set(spec.get("headline_fact_ids") or [])
    used.update((spec.get("summary") or {}).get("fact_ids") or [])
    for group in spec.get("skill_groups") or []:
        for item in group.get("items") or []:
            used.update(item.get("fact_ids") or [])
    for collection, nested in (
        ("experiences", "highlights"),
        ("education", "details"),
        ("certificates", None),
        ("additional", None),
    ):
        for item in spec.get(collection) or []:
            used.update(item.get("fact_ids") or [])
            if nested:
                for child in item.get(nested) or []:
                    used.update(child.get("fact_ids") or [])
    return used


def validate_resume_plan_coverage(spec: dict, plan: dict, facts: dict) -> None:
    """Require the writer to use every fact selected by the validated plan."""
    selected = {
        fact_id
        for row in plan.get("evidence_map") or []
        for fact_id in row.get("fact_ids") or []
    }
    missing = selected - _resume_fact_ids(spec)
    if missing:
        raise ResumeValidationError(
            "成稿未使用简历策略选中的事实："
            + "、".join(sorted(missing))
        )

    known = {
        str(row.get("id")): row
        for row in facts.get("facts") or []
        if isinstance(row, dict) and row.get("id")
    }
    selected_categories = {
        str((known.get(fact_id) or {}).get("category") or "")
        for fact_id in selected
    }
    if selected_categories & {"experience", "project", "profile_experience"}:
        if not spec.get("experiences"):
            raise ResumeValidationError(
                "简历策略选中了经历类事实，但成稿没有生成经历版块"
            )
    if selected_categories & {"education", "profile_education"}:
        if not spec.get("education"):
            raise ResumeValidationError(
                "简历策略选中了教育类事实，但成稿没有生成教育版块"
            )
