"""Structured, user-maintained career profile and AI fact conversion."""
from __future__ import annotations

from datetime import date
import math
import re
from typing import Mapping
from urllib.parse import urlsplit


PROFILE_SECTIONS = ("skills", "experiences", "certificates", "additional")
AVATAR_MIME_TYPES = ("image/jpeg", "image/png", "image/webp")
AVATAR_CROP_MIN_ZOOM = 1.0
AVATAR_CROP_MAX_ZOOM = 3.0
PROFILE_SECTION_LABELS = {
    "skills": "技能树",
    "experiences": "经历",
    "certificates": "证书",
    "additional": "补充资料",
}
SKILL_LEVELS = ("了解", "熟悉", "熟练", "精通", "待确认")
EXPERIENCE_TYPES = (
    ("work", "工作"),
    ("internship", "实习"),
    ("project", "项目"),
    ("freelance", "自由职业"),
    ("volunteer", "志愿活动"),
    ("organization", "学生组织／社团"),
    ("other", "其他"),
)
EXPERIENCE_TYPE_LABELS = dict(EXPERIENCE_TYPES)


def empty_profile_data() -> dict:
    return {"avatar": None, **{section: [] for section in PROFILE_SECTIONS}}


def normalize_profile_avatar_crop(value: object) -> dict:
    """Return a bounded 3:4 crop description in source-image coordinates."""
    source = value if isinstance(value, dict) else {}
    try:
        center_x = float(source.get("center_x", 0.5))
        center_y = float(source.get("center_y", 0.5))
        zoom = float(source.get("zoom", AVATAR_CROP_MIN_ZOOM))
    except (TypeError, ValueError):
        return {"center_x": 0.5, "center_y": 0.5, "zoom": AVATAR_CROP_MIN_ZOOM}
    if not all(map(math.isfinite, (center_x, center_y, zoom))):
        return {"center_x": 0.5, "center_y": 0.5, "zoom": AVATAR_CROP_MIN_ZOOM}
    return {
        "center_x": round(min(1.0, max(0.0, center_x)), 6),
        "center_y": round(min(1.0, max(0.0, center_y)), 6),
        "zoom": round(min(AVATAR_CROP_MAX_ZOOM, max(AVATAR_CROP_MIN_ZOOM, zoom)), 6),
    }


def normalize_profile_avatar(value: object) -> dict | None:
    """Return trusted avatar metadata, or None for legacy/malformed rows."""
    if not isinstance(value, dict):
        return None
    path = str(value.get("path") or "").strip()
    digest = str(value.get("sha256") or "").strip().lower()
    mime_type = str(value.get("mime_type") or "").strip().lower()
    filename = str(value.get("original_filename") or "").strip()
    uploaded_at = str(value.get("uploaded_at") or "").strip()
    try:
        width = int(value.get("width") or 0)
        height = int(value.get("height") or 0)
        source_width = int(value.get("source_width") or width)
        source_height = int(value.get("source_height") or height)
    except (TypeError, ValueError):
        return None
    if (
        not path
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or mime_type not in AVATAR_MIME_TYPES
        or width < 1
        or height < 1
        or source_width < 1
        or source_height < 1
        or not uploaded_at
    ):
        return None
    source_path = str(value.get("source_path") or path).strip()
    source_digest = str(value.get("source_sha256") or digest).strip().lower()
    if not source_path or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        return None
    return {
        "path": path,
        "sha256": digest,
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "source_path": source_path,
        "source_sha256": source_digest,
        "source_width": source_width,
        "source_height": source_height,
        "crop": normalize_profile_avatar_crop(value.get("crop")),
        "original_filename": filename[:255],
        "uploaded_at": uploaded_at,
    }


def normalize_profile_data(value: object) -> dict:
    """Return the stable profile shape used by pages and AI context builders."""
    source = value if isinstance(value, dict) else {}
    result = empty_profile_data()
    result["avatar"] = normalize_profile_avatar(source.get("avatar"))
    for section in PROFILE_SECTIONS:
        rows = source.get(section)
        if isinstance(rows, list):
            result[section] = [
                {str(key): item_value for key, item_value in row.items()}
                for row in rows
                if isinstance(row, dict) and str(row.get("id") or "").strip()
            ]
    return result


def _text(values: Mapping[str, str], key: str, label: str, limit: int, *, required: bool = False) -> str:
    value = str(values.get(key) or "").strip()
    if required and not value:
        raise ValueError(f"请填写{label}")
    if len(value) > limit:
        raise ValueError(f"{label}不能超过 {limit} 个字符")
    return value


def _month(value: str, label: str, *, required: bool = False) -> str:
    value = value.strip()
    if not value:
        if required:
            raise ValueError(f"请填写{label}")
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError(f"{label}格式应为 YYYY-MM")
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError:
        raise ValueError(f"{label}不是有效日期") from None
    return value


def _day(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label}不是有效日期") from None
    return value


def _url(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label}必须是 http/https 地址")
    return value


def validate_profile_item(section: str, values: Mapping[str, str]) -> dict:
    """Validate one editable row; IDs and version metadata remain server-owned."""
    if section not in PROFILE_SECTIONS:
        raise ValueError("未知资料分类")
    if section == "skills":
        return {
            "category": _text(values, "category", "技能分类", 60),
            "name": _text(values, "name", "技能名称", 100, required=True),
            "level": _text(values, "level", "熟练度", 20, required=True),
            "notes": _text(values, "notes", "技能说明", 500),
        }
    if section == "experiences":
        kind = _text(values, "type", "经历类型", 30, required=True)
        if kind not in EXPERIENCE_TYPE_LABELS:
            raise ValueError("未知经历类型")
        start_date = _month(_text(values, "start_date", "开始月份", 7, required=True), "开始月份", required=True)
        end_date = _month(_text(values, "end_date", "结束月份", 7), "结束月份")
        if end_date and end_date < start_date:
            raise ValueError("结束月份不能早于开始月份")
        return {
            "type": kind,
            "role": _text(values, "role", "角色／职位", 120, required=True),
            "organization": _text(values, "organization", "组织／项目名称", 160),
            "start_date": start_date,
            "end_date": end_date,
            "location": _text(values, "location", "地点", 120),
            "description": _text(values, "description", "经历描述", 2000),
            "achievements": _text(values, "achievements", "成果", 2000),
        }
    if section == "certificates":
        return {
            "name": _text(values, "name", "证书名称", 160, required=True),
            "issuer": _text(values, "issuer", "颁发机构", 160, required=True),
            "issued_on": _day(_text(values, "issued_on", "获得日期", 10), "获得日期"),
            "credential_url": _url(_text(values, "credential_url", "证书链接", 500), "证书链接"),
            "notes": _text(values, "notes", "证书说明", 500),
        }
    return {
        "title": _text(values, "title", "补充标题", 120, required=True),
        "content": _text(values, "content", "补充内容", 4000, required=True),
    }


def _date_range(item: dict) -> str:
    start = item.get("start_date") or "未注明开始时间"
    end = item.get("end_date") or "至今"
    return f"{start} 至 {end}"


def profile_fact_items(data: object) -> list[dict]:
    """Convert user-maintained rows into evidence-preserving AI facts."""
    profile = normalize_profile_data(data)
    facts: list[dict] = []
    for item in profile["skills"]:
        parts = [
            "技能",
            f"名称：{item.get('name', '')}",
            f"熟练度：{item.get('level', '')}",
        ]
        if item.get("category"):
            parts.insert(1, f"分类：{item['category']}")
        if item.get("notes"):
            parts.append(f"说明：{item['notes']}")
        text = "｜".join(parts)
        facts.append(_fact("skill", item["id"], text))
    for item in profile["experiences"]:
        kind = EXPERIENCE_TYPE_LABELS.get(item.get("type"), "经历")
        parts = [
            kind,
            f"角色：{item.get('role', '')}",
            f"组织／项目：{item.get('organization') or '未注明'}",
            f"时间：{_date_range(item)}",
        ]
        if item.get("location"):
            parts.append(f"地点：{item['location']}")
        if item.get("description"):
            parts.append(f"描述：{item['description']}")
        if item.get("achievements"):
            parts.append(f"成果：{item['achievements']}")
        facts.append(_fact("experience", item["id"], "｜".join(parts)))
    for item in profile["certificates"]:
        parts = [
            "证书",
            f"名称：{item.get('name', '')}",
            f"颁发机构：{item.get('issuer', '')}",
        ]
        if item.get("issued_on"):
            parts.append(f"获得日期：{item['issued_on']}")
        if item.get("credential_url"):
            parts.append(f"证书链接：{item['credential_url']}")
        if item.get("notes"):
            parts.append(f"说明：{item['notes']}")
        facts.append(_fact("certificate", item["id"], "｜".join(parts)))
    for item in profile["additional"]:
        text = f"补充资料｜标题：{item.get('title', '')}｜内容：{item.get('content', '')}"
        facts.append(_fact("additional", item["id"], text))
    return facts


def _fact(kind: str, item_id: str, text: str) -> dict:
    return {
        "id": f"profile-{kind}-{item_id}",
        "category": f"profile_{kind}",
        "text": text,
        "evidence": text,
        "origin": "profile",
    }


def merge_profile_facts(resume_facts: object, profile_data: object) -> dict:
    """Combine immutable resume facts with one pinned structured profile version."""
    source = resume_facts if isinstance(resume_facts, dict) else {}
    base_rows = source.get("facts") if isinstance(source.get("facts"), list) else []
    rows = [dict(item) for item in base_rows if isinstance(item, dict)]
    known_ids = {str(item.get("id") or "") for item in rows}
    for item in profile_fact_items(profile_data):
        if item["id"] not in known_ids:
            rows.append(item)
            known_ids.add(item["id"])
    return {
        **source,
        "facts": rows,
        "profile_included": bool(profile_fact_items(profile_data)),
    }


def profile_has_content(data: object) -> bool:
    profile = normalize_profile_data(data)
    return any(profile[section] for section in PROFILE_SECTIONS)


def profile_context_text(data: object) -> str:
    profile = normalize_profile_data(data)
    grouped: dict[str, list[str]] = {section: [] for section in PROFILE_SECTIONS}
    for fact in profile_fact_items(profile):
        section = {
            "skill": "skills",
            "experience": "experiences",
            "certificate": "certificates",
            "additional": "additional",
        }.get(fact["category"].removeprefix("profile_"))
        if section:
            grouped[section].append(fact["text"])
    parts = []
    for section in PROFILE_SECTIONS:
        if grouped[section]:
            parts.append(f"## {PROFILE_SECTION_LABELS[section]}")
            parts.extend(f"- {text}" for text in grouped[section])
    return "\n".join(parts)
