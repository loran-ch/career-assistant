"""分享报告链接：一次性令牌、只含计数的脱敏摘要、可扫描二维码。

安全约定（与 resume_boundary 的"证据可追溯"同源）：
- 令牌明文只在创建时返回一次；数据库只保存 sha256 摘要与前 8 位提示；
- 分享摘要只输出**计数与分类**，不含姓名、联系方式、简历原文、AI 结论文本；
- 摘要由创建时冻结，报告后续变化不会改写已发出的链接内容；
- 二维码依赖可选：缺少 qrcode 库时降级为纯文本，绝不让整个流程崩掉。
"""
from __future__ import annotations

import base64
from collections import Counter
from datetime import timedelta
import hashlib
import io
import secrets

from career_assistant.models import now_hk, to_iso

from .report_audit import normalize_report_data
from .store import stamp

TOKEN_BYTES = 24
DEFAULT_TTL_DAYS = 7
MAX_TTL_DAYS = 30
SCOPE_REPORT = "report"


def new_token() -> str:
    """生成一次性分享令牌（URL 安全字符，约 32 位熵）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(token: str) -> str:
    """令牌摘要：数据库与日志中只允许出现它。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def token_hint(token: str) -> str:
    """令牌提示：够用来认领是哪一条链接，又不足以被重放。"""
    return str(token or "")[:8]


def build_report_summary(report: dict) -> dict:
    """把报告压缩成只含计数的摘要，确保个人信息不外泄。

    这里刻意不输出 audit_items 的标题/摘要/依据，也不输出 AI 的总结文本：
    那些字段是自由文本，可能夹带姓名、项目名或公司内部信息。
    """
    data = normalize_report_data(report.get("data") or {})
    facts = data.get("facts") if isinstance(data.get("facts"), list) else []
    categories = Counter(
        str(item.get("category") or "other").strip().lower()
        for item in facts if isinstance(item, dict)
    )
    items = [item for item in (data.get("audit_items") or []) if isinstance(item, dict)]
    passed = sum(1 for item in items if item.get("status") == "pass")
    issue = sum(1 for item in items if item.get("status") == "issue")
    checked = passed + issue
    return {
        "facts_total": len(facts),
        "skills_total": categories.get("skill", 0),
        "experiences_total": categories.get("experience", 0) + categories.get("project", 0),
        "facts_by_category": dict(sorted(categories.items())),
        "audit_total": checked,
        "audit_pass": passed,
        "audit_issue": issue,
        "coverage_percent": round(passed * 100 / checked) if checked else 0,
        "audit_items": [
            {"category": str(item.get("category") or "其他检查"), "status": str(item.get("status") or "issue")}
            for item in items
        ],
    }


def build_share_url(base_url: str, token: str) -> str:
    """拼出可分享链接。base_url 允许是局域网地址，便于手机扫码。"""
    root = str(base_url or "").rstrip("/")
    return f"{root}/api/v1/share/{token}"


def qr_png(url: str) -> bytes | None:
    """把链接渲染成 PNG 二维码；缺少 qrcode 依赖时返回 None。"""
    text = str(url or "").strip()
    if not text:
        return None
    try:
        import qrcode
    except ImportError:
        return None
    code = qrcode.QRCode(box_size=6, border=2)
    code.add_data(text)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def qr_payload(url: str) -> dict:
    """二维码载荷：可用时给 data URI，不可用时降级为纯文本。"""
    text = str(url or "").strip()
    payload = qr_png(text)
    if payload is None:
        return {"format": "text", "available": False, "payload": text}
    encoded = base64.b64encode(payload).decode("ascii")
    return {
        "format": "png",
        "available": True,
        "mime_type": "image/png",
        "payload": text,
        "data_uri": f"data:image/png;base64,{encoded}",
    }


def create_report_share(store, report_id: int, base_url: str, *, ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
    """为一份报告创建分享链接，返回令牌明文（只此一次）。"""
    report = store.get("reports", int(report_id))
    if report is None:
        raise LookupError("报告不存在")
    days = max(1, min(int(ttl_days or DEFAULT_TTL_DAYS), MAX_TTL_DAYS))
    token = new_token()
    expires_at = to_iso(now_hk() + timedelta(days=days))
    summary = build_report_summary(report)
    url = build_share_url(base_url, token)
    # 只存站点根地址：明文令牌一旦落库，就等于把链接永久公开。
    store.create_share_token(
        token_digest(token),
        token_hint(token),
        SCOPE_REPORT,
        "report",
        int(report_id),
        summary,
        expires_at,
        str(base_url or "").rstrip("/"),
    )
    return {
        "token": token,
        "url": url,
        "expires_at": expires_at,
        "ttl_days": days,
        "summary": summary,
        "qr": qr_payload(url),
    }


def read_share(store, token: str, *, count: bool = True) -> dict:
    """按令牌读取分享内容；无效、撤销或过期都抛 LookupError。"""
    digest = token_digest(token)
    record = store.get_share_token(digest)
    if record is None:
        raise LookupError("分享链接无效")
    if record.get("revoked_at"):
        raise LookupError("分享链接已被撤销")
    expires_at = record.get("expires_at")
    if expires_at and expires_at < stamp():
        raise LookupError("分享链接已过期")
    # 完整链接用调用方带来的明文令牌临时拼出，不写回数据库。
    base = str(record.get("base_url") or "").rstrip("/")
    record["base_url"] = base
    record["url"] = build_share_url(base, token)
    if count:
        store.touch_share_token(digest)
        record["access_count"] = int(record.get("access_count") or 0) + 1
    return record


def revoke_share(store, token: str) -> bool:
    """撤销一条分享链接；返回是否确实撤销了一条。"""
    return store.revoke_share_token(token_digest(token))
