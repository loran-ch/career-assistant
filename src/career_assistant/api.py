"""课程工作台的 JSON 接口：看板、筛选、任务列表、分享链接与投递信息包。

页面由服务端渲染，本蓝图提供同一批数据的机器可读出口，用于看板视图、
申请筛选、报告分享和投递信息包。对外只输出聚合计数与岗位字段，不输出简历原文。

公开端点只有 `public_share` 与 `share_qr` 两个：它们凭一次性令牌换取脱敏摘要，
其余接口都需要登录态并通过 CSRF 校验。
"""
from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify, request

from .application_pack import build_pack, create_pack
from .share import (
    DEFAULT_TTL_DAYS,
    create_report_share,
    qr_png,
    read_share,
    revoke_share,
)
from .store import APPLICATION_LABELS, APPLICATION_STATUS_ORDER, APPLICATION_STATUSES

api = Blueprint("api", __name__, url_prefix="/api/v1")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _store():
    return current_app.extensions["store"]


def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(request.args.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _text_arg(name: str, limit: int = 120) -> str:
    return str(request.args.get(name) or "").strip()[:limit]


def _page_meta(total: int, page: int, size: int) -> dict:
    pages = max(1, (total + size - 1) // size) if total else 1
    return {"page": page, "page_size": size, "pages": pages, "total": total}


def _share_base_url() -> str:
    """分享链接基准地址：优先外部配置，其次当前请求来源。"""
    configured = str(current_app.config.get("SHARE_BASE_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.host_url or "").rstrip("/")


@api.get("/board")
def board():
    """匿名看板：按申请状态分列，只显示岗位与公司，不含个人材料。"""
    store = _store()
    rows = store.query_application_board()
    columns: dict[str, list[dict]] = {status: [] for status in APPLICATION_STATUS_ORDER}
    for row in rows:
        status = row["status"] if row["status"] in columns else "saved"
        columns[status].append({
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "status": status,
            "stage_status": row["stage_status"] or status,
            "event_date": row["stage_event_date"] or "",
            "changed_at": row["stage_changed_at"] or row["updated_at"],
        })
    return jsonify({
        "ok": True,
        "board": {
            "columns": [
                {
                    "status": status,
                    "label": APPLICATION_LABELS.get(status, status),
                    "count": len(columns.get(status, [])),
                    "cards": columns.get(status, []),
                }
                for status in APPLICATION_STATUS_ORDER
            ],
            "total": len(rows),
        },
        "summary": store.board_summary(),
    })


@api.get("/applications")
def applications():
    """申请管理：按状态、关键词与日期区间筛选并分页。"""
    store = _store()
    status = _text_arg("status", 20)
    if status and status not in APPLICATION_STATUSES:
        return jsonify({"ok": False, "error": "未知申请状态"}), 400
    keyword = _text_arg("q", 80)
    date_from = _text_arg("from", 10)
    date_to = _text_arg("to", 10)
    page = _int_arg("page", 1, 1, 100000)
    size = _int_arg("page_size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    rows, total = store.query_applications(
        status=status, keyword=keyword, date_from=date_from, date_to=date_to,
        limit=size, offset=(page - 1) * size,
    )
    return jsonify({
        "ok": True,
        "items": [{
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "status": row["status"],
            "label": APPLICATION_LABELS.get(row["status"], row["status"]),
            "applied_date": row["applied_date"],
            "channel": row["channel"],
            "snapshot_id": row["course_snapshot_id"],
            "resume_version_id": row["course_resume_version_id"],
            "material_id": row["course_material_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        } for row in rows],
        "page": _page_meta(total, page, size),
        "filters": {"status": status, "q": keyword, "from": date_from, "to": date_to},
    })


@api.get("/tasks")
def tasks():
    """任务列表：按类型与状态筛选；不返回任务载荷，避免带出简历内容。"""
    store = _store()
    kind = _text_arg("kind", 30)
    status = _text_arg("status", 20)
    page = _int_arg("page", 1, 1, 100000)
    size = _int_arg("page_size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    rows, total = store.query_tasks(
        kind=kind, status=status, limit=size, offset=(page - 1) * size,
    )
    return jsonify({
        "ok": True,
        "items": [{
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "side_effect": bool(row["side_effect"]),
            "error": row["error"],
            "available_at": row["available_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        } for row in rows],
        "page": _page_meta(total, page, size),
        "filters": {"kind": kind, "status": status},
    })


@api.get("/jobs")
def jobs():
    """岗位列表：按关键词与来源筛选并分页。"""
    store = _store()
    keyword = _text_arg("q", 80)
    source = _text_arg("source", 30)
    page = _int_arg("page", 1, 1, 100000)
    size = _int_arg("page_size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    rows, total = store.query_jobs(
        keyword=keyword, source=source, limit=size, offset=(page - 1) * size,
    )
    return jsonify({
        "ok": True,
        "items": [{
            "job_uid": row["job_uid"],
            "title": row["title"],
            "company": row["company"],
            "source": row["source"],
            "url": row["url"],
            "location": row["location"],
            "salary_text": row["salary_text"],
            "status": row["status"],
            "first_seen_at": row["first_seen_at"],
            "last_fetched_at": row["last_fetched_at"],
            "llm_score": row["llm_score"],
        } for row in rows],
        "page": _page_meta(total, page, size),
        "filters": {"q": keyword, "source": source},
    })


@api.post("/reports/<int:report_id>/share")
def create_share(report_id):
    """为报告生成分享链接；令牌明文只在此处返回一次。"""
    payload = request.get_json(silent=True) or {}
    try:
        ttl_days = int(payload.get("ttl_days", DEFAULT_TTL_DAYS))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "有效期必须是天数"}), 400
    try:
        result = create_report_share(_store(), report_id, _share_base_url(), ttl_days=ttl_days)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@api.get("/shares")
def list_shares():
    """列出已创建的分享；只给令牌提示与站点根地址，不回显明文令牌。"""
    rows = _store().list_share_tokens()
    return jsonify({
        "ok": True,
        "items": [{
            "id": row["id"],
            "token_hint": row["token_hint"],
            "scope": row["scope"],
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "base_url": row["base_url"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "access_count": row["access_count"],
            "last_accessed_at": row["last_accessed_at"],
            "summary": row["summary"],
        } for row in rows],
    })


@api.delete("/share/<token>")
def delete_share(token):
    """撤销一条分享链接。"""
    if not revoke_share(_store(), token):
        return jsonify({"ok": False, "error": "分享链接不存在或已撤销"}), 404
    return jsonify({"ok": True})


@api.get("/share/<token>")
def public_share(token):
    """公开只读：凭令牌换取脱敏摘要。"""
    try:
        record = read_share(_store(), token)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({
        "ok": True,
        "scope": record["scope"],
        "subject_type": record["subject_type"],
        "subject_id": record["subject_id"],
        "summary": record["summary"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "access_count": record["access_count"],
    })


@api.get("/share/<token>/qr.png")
def share_qr(token):
    """分享链接的二维码图片；缺少 qrcode 组件时明确告知。"""
    try:
        record = read_share(_store(), token, count=False)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    png = qr_png(record.get("url") or "")
    if png is None:
        return jsonify({"ok": False, "error": "二维码组件未安装，请先安装 qrcode"}), 503
    return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})


@api.get("/applications/<int:application_id>/pack")
def application_pack(application_id):
    """投递信息包预览：只组装，不落库。"""
    store = _store()
    try:
        pack = build_pack(store, application_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "pack": pack})


@api.post("/applications/<int:application_id>/pack")
def save_application_pack(application_id):
    """生成并保存一个新的投递信息包版本；历史版本不被覆盖。"""
    store = _store()
    try:
        pack = create_pack(store, application_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "pack": pack})
