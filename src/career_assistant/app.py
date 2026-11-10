"""Local, server-rendered Flask workflow for the INT6181 extension."""
from __future__ import annotations

from career_assistant.config import resume_path

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
from urllib.parse import urlsplit
import uuid

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_file, session, stream_with_context, url_for, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from career_assistant.config import DATA_DIR, DB_PATH
from career_assistant.db import Database
from career_assistant.models import JobPosting, now_hk, parse_iso
from career_assistant.scrapers.base import BaseScraper
from .profile_context import (
    AVATAR_CROP_MAX_ZOOM,
    AVATAR_CROP_MIN_ZOOM,
    EXPERIENCE_TYPES,
    EXPERIENCE_TYPE_LABELS,
    PROFILE_SECTION_LABELS,
    PROFILE_SECTIONS,
    SKILL_LEVELS,
    merge_profile_facts,
    normalize_profile_data,
    profile_has_content,
)
from .report_audit import normalize_report_data
from .resume_boundary import (
    BoundaryAction,
    BoundaryEvaluation,
    boundary_check,
    evaluate_boundaries,
    evaluation_from_audit,
    fact_objects_from_facts,
    with_runtime_integrity,
)
from .resume_pdf import file_sha256
from .api import api as api_blueprint
from .application_pack import build_pack, create_pack, pack_markdown
from .share import create_report_share, revoke_share
from .store import (
    APPLICATION_LABELS,
    APPLICATION_STATUS_ORDER,
    APPLICATION_STATUSES,
    Store,
    canonical_output_text,
    encode,
    output_content_hash,
    stamp,
)
from .service import require, material_markdown, queue_search

LABELS = APPLICATION_LABELS

# 只有这两个接口可以在没有登录态的情况下被访问：它们凭一次性令牌换取脱敏摘要。
PUBLIC_ENDPOINTS = {"api.public_share", "api.share_qr"}
LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def share_allowed_hosts() -> set[str]:
    """额外允许访问公开分享接口的主机名；默认只有本机可用。"""
    raw = os.environ.get("CAREER_SHARE_ALLOWED_HOSTS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}

MAX_AVATAR_BYTES = 5 * 1024 * 1024
AVATAR_MAX_EDGE = 1200
AVATAR_MAX_PIXELS = 40_000_000


DATE_BUCKETS = (
    ("today", "今天"),
    ("yesterday", "昨天"),
    ("week", "本周"),
    ("older", "更早"),
)


def annotate_favorite(item: dict, now=None) -> dict:
    now = now or now_hk()
    moment = parse_iso(item.get("created_at") or "") or now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo)
    days = (now.date() - moment.astimezone(now.tzinfo).date()).days
    if days <= 0:
        bucket, when = "today", "今天 " + moment.strftime("%H:%M")
    elif days == 1:
        bucket, when = "yesterday", "昨天"
    elif days < 7:
        bucket, when = "week", f"{days} 天前"
    else:
        bucket, when = "older", moment.strftime("%m-%d")
    return {**item, "when_label": when, "date_bucket": bucket, "favorited_at": moment.timestamp()}


def group_favorites_by_date(items: list[dict]) -> list[dict]:
    groups = []
    for key, title in DATE_BUCKETS:
        rows = [item for item in items if item.get("date_bucket") == key]
        if rows:
            groups.append({"key": key, "title": title, "jobs": rows})
    return groups


def infer_job_type(title: str) -> str:
    text = (title or "").lower()
    if any(word in text for word in ("intern", "internship", "实习", "fresh grad", "graduate")):
        return "实习／校招"
    if any(word in text for word in ("part time", "part-time", "兼职")):
        return "兼职"
    return "全职"


def infer_salary_hint(title: str, salary_text: str) -> str:
    if (salary_text or "").strip():
        return salary_text.strip()
    text = (title or "").lower()
    if any(word in text for word in ("intern", "internship", "实习")):
        return "参考 8-15K HKD/月"
    if any(word in text for word in ("director", "head of", "principal")):
        return "参考 80K+ HKD/月"
    if any(word in text for word in ("senior", "manager", "lead")):
        return "参考 40-70K HKD/月"
    return "参考 25-45K HKD/月"


def card_requirement(title: str, company: str, location: str, snippet: str) -> tuple[str, bool]:
    cleaned = re.sub(r"\s+", " ", snippet or "").strip()
    if cleaned:
        return cleaned[:220], True
    place = location or "地点未注明"
    org = company or "公司未提供"
    role = title or "本岗位"
    low = role.lower()
    if any(word in low for word in ("quant", "量化", "trading", "researcher")):
        focus = "量化研究、交易或风控相关经验；熟悉 Python / SQL、统计建模与市场数据，能独立完成分析并与业务对齐。"
    elif any(word in low for word in ("data", "分析", "analyst", "bi")):
        focus = "数据分析经验；熟悉 SQL、Excel 与可视化，能从业务问题出发做指标、报告和结论。"
    elif any(word in low for word in ("engineer", "工程", "developer", "algorithm")):
        focus = "工程或算法落地能力；熟悉常用开发与协作流程，能把方案做成可维护代码。"
    elif any(word in low for word in ("finance", "财务", "investment", "投资", "alm")):
        focus = "金融、投资或财务管理背景；熟悉报表与风险指标，能支持决策和跨部门沟通。"
    else:
        focus = "与职位方向相符的专业经验，以及清晰的书面与口头沟通能力。"
    return f"{org} · {place}。职位：{role}。{focus}（列表未返回官方 JD，此为按职位名称整理的预览。）", False


def job_display_fields(item: dict, job: dict | None = None) -> dict:
    job = job or {}
    location = (job.get("location") or "").strip()
    salary = (job.get("salary_text") or "").strip()
    snippet = item.get("jd") or job.get("description_snippet") or ""
    requirement, official = card_requirement(
        item.get("title") or "", item.get("company") or "", location, snippet)
    return {
        "location": location or "地点未提供",
        "salary_text": infer_salary_hint(item.get("title") or "", salary),
        "salary_official": bool(salary),
        "posted_at": (job.get("posted_at") or "")[:10],
        "requirement": requirement,
        "requirement_official": official,
        "job_type": infer_job_type(item.get("title") or ""),
        "keyword_matched": (job.get("keyword_matched") or "").strip(),
        "jd_text": (item.get("jd") or "").strip(),
    }


def paginate_items(items: list, arg: str, page_size: int = 8):
    try:
        current = int(request.args.get(arg) or 1)
    except ValueError:
        current = 1
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    current = min(max(1, current), pages)
    start = (current - 1) * page_size
    return items[start:start + page_size], current, pages, total


def tailored_download_name(snapshot: dict, item: dict) -> str:
    raw = f"{snapshot.get('company') or 'company'}-{snapshot.get('title') or 'resume'}-{item['id']}"
    safe = re.sub(r'[\\/:*?"<>|]+', "-", raw)
    safe = re.sub(r"\s+", "-", safe).strip("-.")[:80]
    return f"{safe or 'resume'}.pdf"


def safe_next(target: str | None) -> str:
    target = (target or "").strip()
    parsed = urlsplit(target)
    if (not target.startswith("/") or target.startswith("//") or "\\" in target
            or parsed.scheme or parsed.netloc):
        return ""
    return target


def parse_avatar_crop(values) -> dict:
    """Validate the browser crop values and apply a safe centered default."""
    result = {}
    defaults = {"center_x": 0.5, "center_y": 0.5, "zoom": AVATAR_CROP_MIN_ZOOM}
    ranges = {
        "center_x": (0.0, 1.0, "头像水平位置"),
        "center_y": (0.0, 1.0, "头像垂直位置"),
        "zoom": (AVATAR_CROP_MIN_ZOOM, AVATAR_CROP_MAX_ZOOM, "头像缩放比例"),
    }
    form_keys = {"center_x": "crop_x", "center_y": "crop_y", "zoom": "crop_zoom"}
    for key, default in defaults.items():
        raw = values.get(form_keys[key])
        if raw is None:
            raw = values.get(key)
        if raw is None or str(raw).strip() == "":
            result[key] = default
            continue
        try:
            value = float(str(raw).strip())
        except ValueError:
            raise ValueError("头像取景参数无效，请重新调整") from None
        minimum, maximum, label = ranges[key]
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{label}超出允许范围，请重新调整")
        result[key] = round(value, 6)
    return result


def _save_avatar_jpeg(image, avatar_dir: Path) -> dict:
    """Content-address one metadata-safe JPEG and return its file information."""
    sanitized = io.BytesIO()
    image.save(sanitized, format="JPEG", quality=90, optimize=True)
    payload = sanitized.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    avatar_dir.mkdir(parents=True, exist_ok=True)
    destination = avatar_dir / f"{digest}.jpg"
    if destination.exists():
        if file_sha256(destination) != digest:
            raise ValueError("头像文件哈希冲突，请重新上传")
    else:
        descriptor, temp_name = tempfile.mkstemp(prefix=".avatar-", suffix=".jpg", dir=avatar_dir)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temp_name).chmod(0o600)
            os.replace(temp_name, destination)
        finally:
            Path(temp_name).unlink(missing_ok=True)
    return {
        "path": str(destination.resolve()),
        "sha256": digest,
        "mime_type": "image/jpeg",
        "width": image.width,
        "height": image.height,
    }


def _crop_portrait_avatar(image, crop: dict):
    """Apply normalized center/zoom values to produce an exact 3:4 crop."""
    from PIL import Image

    width, height = image.size
    zoom = crop["zoom"]
    crop_width = max(1, round(min(width, height * 3 / 4) / zoom))
    crop_height = max(1, round(crop_width * 4 / 3))
    if crop_height > height:
        crop_height = height
        crop_width = max(1, round(crop_height * 3 / 4))
    left = round(crop["center_x"] * width - crop_width / 2)
    top = round(crop["center_y"] * height - crop_height / 2)
    left = min(max(0, left), width - crop_width)
    top = min(max(0, top), height - crop_height)
    portrait = image.crop((left, top, left + crop_width, top + crop_height))
    if portrait.size != (crop_width, crop_height):
        portrait = portrait.resize(
            (crop_width, crop_height),
            Image.Resampling.LANCZOS,
        )
    return portrait


def save_profile_avatar(upload, avatar_dir: Path, crop_values=None) -> dict:
    """Decode, normalize and content-address a cropped, metadata-safe avatar."""
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise RuntimeError("头像处理组件未安装，请先安装 Pillow") from exc

    crop = parse_avatar_crop(crop_values or {})
    data = upload.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError("头像不得超过 5 MB")
    if not data:
        raise ValueError("头像文件为空")

    try:
        with Image.open(io.BytesIO(data)) as source:
            image_format = str(source.format or "").upper()
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("头像只支持 JPG、PNG 或 WebP")
            if source.width * source.height > AVATAR_MAX_PIXELS:
                raise ValueError("头像分辨率过大，请上传较小的图片")
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"}:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((AVATAR_MAX_EDGE, AVATAR_MAX_EDGE), Image.Resampling.LANCZOS)
            source = _save_avatar_jpeg(image, avatar_dir)
            cropped = _save_avatar_jpeg(_crop_portrait_avatar(image, crop), avatar_dir)
    except ValueError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        raise ValueError("无法识别该图片，请上传有效的 JPG、PNG 或 WebP") from None

    return {
        **cropped,
        "source_path": source["path"],
        "source_sha256": source["sha256"],
        "source_width": source["width"],
        "source_height": source["height"],
        "crop": crop,
        "original_filename": Path((upload.filename or "").replace("\\", "/")).name[:255],
        "uploaded_at": stamp(),
    }


def adjust_profile_avatar(avatar: dict, crop_values, avatar_dir: Path) -> dict:
    """Crop the original managed image again without uploading a replacement."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("头像处理组件未安装，请先安装 Pillow") from exc

    current = normalize_profile_data({"avatar": avatar})["avatar"]
    if current is None:
        raise ValueError("当前资料库没有可调整的头像")
    crop = parse_avatar_crop(crop_values)
    source_path = safe_avatar_source_path(current, avatar_dir)
    try:
        with Image.open(source_path) as source:
            image = source.convert("RGB")
    except OSError:
        raise ValueError("头像原图无法读取，请重新上传") from None
    cropped = _save_avatar_jpeg(_crop_portrait_avatar(image, crop), avatar_dir)
    return {
        **current,
        **cropped,
        "source_width": image.width,
        "source_height": image.height,
        "crop": crop,
    }


def _safe_managed_avatar_path(path_value: object, digest_value: object, avatar_dir: Path) -> Path:
    """Resolve a managed avatar file and verify that its content was not changed."""
    root = avatar_dir.resolve()
    path = Path(str(path_value or "")).expanduser().resolve()
    if path.parent != root or not path.is_file():
        raise ValueError("头像文件不存在，请重新上传")
    if file_sha256(path) != str(digest_value or ""):
        raise ValueError("头像文件完整性检查未通过，请重新上传")
    return path


def safe_avatar_path(avatar: dict, avatar_dir: Path) -> Path:
    """Resolve the current cropped avatar from managed storage."""
    return _safe_managed_avatar_path(avatar.get("path"), avatar.get("sha256"), avatar_dir)


def safe_avatar_source_path(avatar: dict, avatar_dir: Path) -> Path:
    """Resolve the uncropped source image used by the avatar crop editor."""
    return _safe_managed_avatar_path(
        avatar.get("source_path") or avatar.get("path"),
        avatar.get("source_sha256") or avatar.get("sha256"),
        avatar_dir,
    )


def validated_username(value: str) -> str:
    username = (value or "").strip()
    if not 3 <= len(username) <= 32:
        raise ValueError("用户名需为 3 至 32 个字符")
    if not re.fullmatch(r"[\w.@+\-\u3400-\u4dbf\u4e00-\u9fff]+", username):
        raise ValueError("用户名只能包含文字、字母、数字及 . _ @ + -")
    return username


def validated_password(value: str) -> str:
    if not 8 <= len(value) <= 128:
        raise ValueError("密码需为 8 至 128 个字符")
    return value


def create_app(store=None, data_dir=None, testing=False):
    app = Flask(__name__)
    data_dir = Path(data_dir or os.environ.get("RADAR_DATA_DIR", str(DATA_DIR)))
    uploads = data_dir / "course_uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    avatars = data_dir / "course_avatars"
    avatars.mkdir(parents=True, exist_ok=True)
    secret_path = data_dir / "course.secret"
    try:
        fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_hex(32))
    app.config.update(SECRET_KEY=secret_path.read_text().strip(), MAX_CONTENT_LENGTH=11 * 1024 * 1024,
                      SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_NAME="career_assistant_session",
                      TESTING=testing, UPLOAD_DIR=uploads, BASE_URL="http://127.0.0.1:5000",
                      SHARE_BASE_URL=os.environ.get("CAREER_SHARE_BASE_URL", "").strip())
    app.config["AVATAR_DIR"] = avatars
    store = store or Store(os.environ.get("RADAR_DB_PATH", str(DB_PATH)))
    store.migrate()
    app.extensions["store"] = store
    app.register_blueprint(api_blueprint)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "task_backend": store.task_backend})

    @app.before_request
    def protect_local():
        host = request.host.split(":")[0].lower()
        public = request.endpoint in PUBLIC_ENDPOINTS
        # 本机以外的地址一律拒绝；只有公开分享接口允许显式放行的主机。
        if host not in LOCAL_HOSTS and not (public and host in share_allowed_hosts()):
            abort(403)
        if request.endpoint == "healthz":
            return None
        if public:
            return None
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(32)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.form.get("csrf") or request.headers.get("X-CSRF") or ""
            if request.is_json:
                supplied = supplied or str((request.get_json(silent=True) or {}).get("csrf") or "")
            if not secrets.compare_digest(str(supplied), session["csrf"]):
                abort(403)
        if request.endpoint == "static":
            return None
        user_id = session.get("user_id")
        g.user = store.get_user(user_id) if user_id else None
        if g.user is None and user_id:
            session.pop("user_id", None)
        if request.endpoint in {"login", "register"}:
            return None
        if g.user is None:
            if request.path.startswith("/api/") or (request.path.startswith("/assistant") and request.is_json):
                return jsonify({"ok": False, "error": "登录状态已失效，请重新登录"}), 401
            target = safe_next(request.full_path.rstrip("?"))
            if store.user_count() == 0:
                return redirect(url_for("register", next=target))
            return redirect(url_for("login", next=target))
        return None

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = (
            "SAMEORIGIN"
            if request.endpoint == "tailored_resume_pdf"
            else "DENY"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.context_processor
    def context():
        return {
            "csrf": session.get("csrf", ""),
            "labels": LABELS,
            "operation": secrets.token_hex(16),
            "current_user": getattr(g, "user", None),
        }

    def sign_in(user: dict) -> None:
        session.clear()
        session["user_id"] = user["id"]
        session["csrf"] = secrets.token_hex(32)
        g.user = user

    def _source_version(label: str, value: int | None) -> dict:
        return {"label": label, "value": value, "missing": value is None}

    def _material_output(material: dict) -> dict:
        data = material.get("data") or {}
        review = data.get("review") or {}
        issues = [str(item) for item in review.get("issues") or [] if str(item).strip()]
        status = data.get("status")
        if status == "problem_draft":
            audit_status, audit_label = "issue", "存在待处理问题"
        elif status == "reviewed":
            audit_status, audit_label = "pass", "AI 审阅通过"
        else:
            audit_status, audit_label = "unreviewed", "未经独立 AI 审阅"
        match = store.get("matches", material["match_id"]) or {}
        content = canonical_output_text(material_markdown(data))
        if audit_status == "pass":
            checks = [boundary_check("audit_passed")]
        elif audit_status == "issue":
            checks = [boundary_check("audit_review_required")]
        else:
            checks = [boundary_check("material_unreviewed")]
        boundary = evaluate_boundaries(
            checks,
            subject={"artifact_id": material["id"]},
        )
        approve_decision = boundary.decide(
            BoundaryAction.APPROVE,
            confirmed=True,
        )
        export_decision = boundary.decide(
            BoundaryAction.EXPORT,
            approved=True,
        )
        return {
            "artifact_type": "material",
            "artifact_id": material["id"],
            "artifact_version": material["version"],
            "approval_scope": "export",
            "kind_label": "求职材料",
            "artifact_kind": "material_markdown",
            "title": f"求职材料 · 版本 {material['version']}",
            "content": content,
            "content_hash": output_content_hash(content),
            "audit_status": audit_status,
            "audit_label": audit_label,
            "audit_summary": review.get("summary") or "",
            "audit_findings": [],
            "audit_issues": issues,
            "boundary": boundary.to_dict(),
            "refusal_level": boundary.level.value,
            "refusal_label": boundary.level.label,
            "boundary_checks": [
                check.to_dict()
                for check in boundary.checks
            ],
            "can_approve": approve_decision.allowed,
            "can_export": export_decision.allowed,
            "legacy": False,
            "integrity_error": "",
            "pdf_url": "",
            "pdf_sha256": "",
            "document_sha256": "",
            "current_pdf_sha256": "",
            "page_count": None,
            "template_version": "",
            "renderer_version": "",
            "source_versions": [
                _source_version("岗位 JD 依据", match.get("snapshot_id")),
                _source_version("简历事实版本", match.get("resume_version_id")),
                _source_version("职业资料版本", match.get("profile_version_id")),
                _source_version("求职偏好版本", match.get("preference_id")),
            ],
            "return_url": url_for("material", material_id=material["id"]),
            "approve_url": url_for("approve_material", material_id=material["id"]),
            "download_url": url_for("export_material", material_id=material["id"]),
        }

    def _tailored_output(snapshot: dict, item: dict) -> dict:
        source_snapshot = store.get("job_snapshots", item["snapshot_id"]) or snapshot
        artifact_status = item.get("artifact_status") or "legacy"
        is_pdf = artifact_status in {"audit_pass", "audit_issue"}
        audit = item.get("audit") or {}
        raw_boundary = item.get("boundary")
        if raw_boundary:
            try:
                boundary = BoundaryEvaluation.from_dict(raw_boundary)
            except ValueError:
                boundary = evaluate_boundaries(
                    [boundary_check("boundary_record_invalid")],
                    subject={"artifact_id": item["id"]},
                )
        else:
            resume_version = (
                store.get("resume_versions", item.get("resume_version_id"))
                if item.get("resume_version_id")
                else None
            )
            profile_version = (
                store.get("profile_versions", item.get("profile_version_id"))
                if item.get("profile_version_id")
                else None
            )
            historical_facts = merge_profile_facts(
                (resume_version or {}).get("facts") or {},
                normalize_profile_data((profile_version or {}).get("data") or {}),
            )
            boundary = evaluation_from_audit(
                audit,
                legacy=not is_pdf,
                fact_objects=fact_objects_from_facts(historical_facts),
                evidence_refs=(
                    fact_id
                    for finding in audit.get("findings") or []
                    if isinstance(finding, dict)
                    for fact_id in finding.get("fact_ids") or []
                ),
            )
        findings = [
            dict(row)
            for row in (audit.get("findings") or [])
            if isinstance(row, dict) and row.get("status") in {"pass", "issue"}
        ]
        issues = [
            f"{row.get('title', '审计项')}：{row.get('detail', '')}".strip()
            for row in findings
            if row.get("status") == "issue"
        ]
        current_pdf_sha = ""
        integrity_error = ""
        pdf_path = ""
        if is_pdf:
            audit_status = artifact_status.removeprefix("audit_")
            audit_label = "独立审计通过" if audit_status == "pass" else "独立审计发现问题"
            content = ""
            content_hash = str(item.get("pdf_sha256") or "")
            try:
                candidate = Path(str(item.get("pdf_path") or "")).expanduser().resolve()
                pdf_root = (Path(store.path).parent / "course_resume_pdfs").resolve()
                if not candidate.is_relative_to(pdf_root):
                    integrity_error = "PDF 文件路径不在受控产物目录内"
                elif not candidate.is_file():
                    integrity_error = "PDF 文件缺失，无法审批或下载"
                else:
                    pdf_path = str(candidate)
                    current_pdf_sha = file_sha256(candidate)
                    if not secrets.compare_digest(current_pdf_sha, content_hash):
                        integrity_error = "PDF 文件指纹与生成记录不一致，文件可能已被修改"
            except (OSError, ValueError):
                integrity_error = "PDF 文件无法读取，已阻止审批和下载"
            boundary = with_runtime_integrity(boundary, integrity_error)
        else:
            audit_status = "unreviewed"
            audit_label = "历史 Markdown，无独立审计报告"
            content = canonical_output_text(item.get("body") or "")
            content_hash = output_content_hash(content)
        approve_decision = boundary.decide(
            BoundaryAction.APPROVE,
            confirmed=True,
            hash_matches=not integrity_error,
        )
        export_decision = boundary.decide(
            BoundaryAction.EXPORT,
            approved=True,
            hash_matches=not integrity_error,
        )
        return {
            "artifact_type": "tailored_resume",
            "artifact_id": item["id"],
            "artifact_version": item["id"],
            "approval_scope": "download",
            "kind_label": "针对性简历",
            "title": item["title"],
            "content": content,
            "content_hash": content_hash,
            "audit_status": audit_status,
            "audit_label": audit_label,
            "audit_summary": audit.get("summary") or "",
            "audit_findings": findings,
            "audit_issues": issues,
            "boundary": boundary.to_dict(),
            "refusal_level": boundary.level.value,
            "refusal_label": boundary.level.label,
            "boundary_checks": [
                check.to_dict()
                for check in boundary.checks
            ],
            "can_approve": approve_decision.allowed,
            "can_export": export_decision.allowed,
            "artifact_kind": "pdf" if is_pdf else "legacy_markdown",
            "legacy": not is_pdf,
            "pdf_path": pdf_path,
            "pdf_url": (
                url_for(
                    "tailored_resume_pdf",
                    snapshot_id=snapshot["id"],
                    record_id=item["id"],
                )
                if is_pdf
                else ""
            ),
            "pdf_sha256": str(item.get("pdf_sha256") or ""),
            "document_sha256": str(item.get("content_sha256") or ""),
            "current_pdf_sha256": current_pdf_sha,
            "integrity_error": integrity_error,
            "page_count": item.get("page_count"),
            "template_version": item.get("template_version") or "",
            "renderer_version": item.get("renderer_version") or "",
            "source_versions": [
                _source_version("岗位 JD 依据", source_snapshot.get("id")),
                _source_version("上传简历", item.get("resume_id")),
                _source_version("简历事实版本", item.get("resume_version_id")),
                _source_version("职业资料版本", item.get("profile_version_id")),
                _source_version("投递信息版本", item.get("delivery_profile_id")),
            ],
            "return_url": url_for(
                "tailored_resume",
                snapshot_id=snapshot["id"],
                record_id=item["id"],
            ),
            "approve_url": url_for(
                "approve_tailored_resume",
                snapshot_id=snapshot["id"],
                record_id=item["id"],
            ),
            "download_url": url_for(
                "download_tailored_resume",
                snapshot_id=snapshot["id"],
                record_id=item["id"],
            ),
        }

    def _existing_output_approval(output: dict) -> dict | None:
        approval = store.find_output_approval(
            output["artifact_type"],
            output["artifact_id"],
            output["content_hash"],
            output["approval_scope"],
        )
        if (
            approval
            and output.get("artifact_kind") == "pdf"
            and (
                output.get("integrity_error")
                or not secrets.compare_digest(
                    output.get("current_pdf_sha256") or "",
                    output["content_hash"],
                )
            )
        ):
            return None
        return approval

    def _output_audit_snapshot(output: dict) -> dict:
        return {
            "title": output["title"],
            "kind_label": output["kind_label"],
            "artifact_version": output["artifact_version"],
            "audit_status": output["audit_status"],
            "audit_label": output["audit_label"],
            "audit_summary": output.get("audit_summary") or "",
            "audit_issues": output["audit_issues"],
            "audit_findings": output.get("audit_findings") or [],
            "boundary": output.get("boundary") or {},
            "refusal_level": output.get("refusal_level") or "",
            "pdf_sha256": output.get("pdf_sha256") or "",
            "document_sha256": output.get("document_sha256") or "",
            "page_count": output.get("page_count"),
            "template_version": output.get("template_version") or "",
            "renderer_version": output.get("renderer_version") or "",
            "source_versions": {
                row["label"]: row["value"] for row in output["source_versions"]
            },
        }

    def _approve_output(output: dict):
        submitted_hash = str(request.form.get("content_hash") or "").strip()
        confirmed = request.form.get("confirm_review") == "yes"
        hash_matches = secrets.compare_digest(
            submitted_hash,
            output["content_hash"],
        )
        if output.get("boundary"):
            decision = BoundaryEvaluation.from_dict(
                output["boundary"]
            ).decide(
                BoundaryAction.APPROVE,
                confirmed=confirmed,
                hash_matches=hash_matches,
            )
            if not decision.allowed:
                raise ValueError(
                    f"审批被拒绝（{decision.level.value.upper()} · "
                    f"{decision.code}）：{decision.summary}"
                )
        if not hash_matches:
            raise ValueError("输出内容已变化，请刷新审批页后重新核对")
        if output.get("artifact_kind") == "pdf":
            if output.get("integrity_error"):
                raise ValueError(output["integrity_error"])
            if not secrets.compare_digest(
                output.get("current_pdf_sha256") or "",
                submitted_hash,
            ):
                raise ValueError("PDF 文件已变化，请刷新审批页后重新核对")
        if not confirmed:
            raise ValueError("请先确认已核对当前版本及其来源")
        store.record_output_approval(
            output["artifact_type"],
            output["artifact_id"],
            output["artifact_version"],
            output["content_hash"],
            _output_audit_snapshot(output),
            g.user["id"],
            output["approval_scope"],
        )
        if output.get("artifact_kind") == "pdf":
            try:
                after_hash = file_sha256(output["pdf_path"])
            except OSError:
                raise ValueError("PDF 文件在审批过程中不可读取，请重新核对") from None
            if not secrets.compare_digest(after_hash, submitted_hash):
                raise ValueError("PDF 文件在审批过程中发生变化，请重新审批")
        elif output_content_hash(output["content"]) != submitted_hash:
            raise ValueError("输出内容在审批过程中发生变化，请重新审批")
        return redirect(output["download_url"])

    @app.route("/register", methods=["GET", "POST"])
    def register():
        next_url = safe_next(request.values.get("next"))
        if store.user_count():
            flash("本机账户已经创建，请直接登录。")
            return redirect(url_for("login", next=next_url) if next_url else url_for("login"))

        error = ""
        username = (request.form.get("username") or "").strip()
        if request.method == "POST":
            try:
                username = validated_username(username)
                password = validated_password(request.form.get("password") or "")
                confirmation = request.form.get("password_confirm") or ""
                if password != confirmation:
                    raise ValueError("两次输入的密码不一致")
                user_id = store.create_initial_user(username, generate_password_hash(password))
            except ValueError as exc:
                error = str(exc)
            except sqlite3.IntegrityError:
                error = "该用户名不可用，请更换后重试"
            else:
                user = store.get_user(user_id)
                if user is None:
                    raise RuntimeError("账户创建后无法读取")
                store.mark_user_login(user_id)
                sign_in(user)
                flash("账户已创建，欢迎使用。")
                return redirect(next_url or url_for("jobs"))

        return render_template(
            "auth.html", mode="register", error=error, username=username,
            next_url=next_url,
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        next_url = safe_next(request.values.get("next"))
        if store.user_count() == 0:
            return redirect(url_for("register", next=next_url) if next_url else url_for("register"))
        if getattr(g, "user", None):
            return redirect(next_url or url_for("jobs"))

        error = ""
        username = (request.form.get("username") or "").strip()
        if request.method == "POST":
            user = store.get_user_by_username(username)
            password = request.form.get("password") or ""
            if user and check_password_hash(user["password_hash"], password):
                store.mark_user_login(user["id"])
                user.pop("password_hash", None)
                sign_in(user)
                return redirect(next_url or url_for("jobs"))
            error = "用户名或密码不正确"

        return render_template(
            "auth.html", mode="login", error=error, username=username,
            next_url=next_url,
        )

    @app.post("/logout")
    def logout():
        session.clear()
        session["csrf"] = secrets.token_hex(32)
        flash("已退出登录。")
        return redirect(url_for("login"))

    @app.errorhandler(ValueError)
    def invalid(exc):
        target = request.form.get("return_to", "")
        if request.path.startswith("/assistant") and (request.is_json or request.accept_mimetypes.best == "application/json"):
            return jsonify({"ok": False, "error": str(exc)}), 400
        if target.startswith(("/jobs", "/assistant", "/profile", "/preferences", "/applications/", "/materials/")) and not target.startswith("//") and "?" not in target:
            session["form_error"] = str(exc)
            session["form_values"] = {k: v for k, v in request.form.items() if k not in {"csrf"}}
            return redirect(target)
        return render_template("page.html", page="error", error=str(exc)), 400

    @app.errorhandler(sqlite3.IntegrityError)
    def integrity(exc):
        return render_template("page.html", page="error", error="数据关联或版本校验失败，请刷新后重试。"), 400

    @app.errorhandler(413)
    def too_large(exc):
        if request.path == "/profile/avatar":
            session["form_error"] = "头像不得超过 5 MB。请替换文件。"
            return redirect(url_for("profile", _anchor="profile-avatar"))
        return invalid(ValueError("简历不得超过 10 MB。请替换文件。")) if request.form.get("return_to") else (render_template("page.html", page="error", error="简历不得超过 10 MB。"), 413)

    def active_search_task(preference_id: int) -> dict | None:
        """正在排队或执行中的搜索任务。

        重复点击「开始搜索」时复用同一个任务，不再堆积新任务：
        任务堆积会让岗位页一直停在「正在获取岗位」并反复刷新。
        """
        for item in store.list("tasks", kind="search"):
            if item["status"] in {"queued", "running"} and \
                    (item.get("payload") or {}).get("preference_id") == preference_id:
                return item
        return None

    def task_redirect(task_id):
        item = require(store, "tasks", task_id)
        if item["kind"] == "search":
            return redirect(url_for("jobs", task=task_id, _anchor="search-results"))
        if item["kind"] == "materials":
            match = require(store, "matches", item["payload"]["match_id"])
            return redirect(url_for("job_detail", snapshot_id=match["snapshot_id"], task=task_id, match=match["id"]))
        if item["kind"] == "analyze":
            pid = request.form.get("preparation_id")
            if pid:
                record = require(store, "preparations", int(pid))
                if record["resume_version_id"] is None:
                    store.update_preparation(record["id"], resume_id=item["payload"]["resume_id"])
                    store.resume_waiting(item["payload"]["resume_id"])
                return redirect(url_for("job_detail", snapshot_id=record["snapshot_id"], task=task_id))
            return redirect(url_for("profile", task=task_id, _anchor="profile-resumes"))
        if item["kind"] == "email":
            if item["payload"]["type"] == "report":
                report = require(store, "reports", item["payload"]["id"])
                return redirect(url_for("resume", resume_id=report["resume_id"], task=task_id))
            match = require(store, "matches", item["payload"]["id"])
            return redirect(url_for("job_detail", snapshot_id=match["snapshot_id"], match=match["id"]))
        return redirect(url_for("task", task_id=task_id))

    @app.get("/")
    def index():
        return redirect(url_for("jobs"))

    @app.get("/profile")
    def profile():
        favorites = store.list_favorites()
        tailored = store.tailored_resume_counts()
        pending = [item for item in favorites if not tailored.get(item["job_uid"])]
        reports = [item for item in favorites if tailored.get(item["job_uid"])]
        fav = request.args.get("fav") or "pending"
        if fav not in {"pending", "reports"}:
            fav = "pending"
        pool = pending if fav == "pending" else reports
        listed, fav_page, fav_pages, fav_total = paginate_items(pool, "p")
        profile_version = store.latest_profile_version()
        profile_data = normalize_profile_data(profile_version["data"] if profile_version else {})
        task_arg = request.args.get("task") or ""
        upload_task = store.get("tasks", int(task_arg)) if task_arg.isdigit() else None
        if upload_task and upload_task["kind"] != "analyze":
            upload_task = None
        upload_facts = (
            store.list("resume_versions", resume_id=upload_task["payload"]["resume_id"])
            if upload_task else []
        )
        return render_template(
            "page.html", page="home",
            preparations=store.list("preparations"), resumes=store.list("resumes"),
            versions=store.list("resume_versions"), preferences=store.list("preferences"),
            tasks=store.list("tasks")[:20], applications=store.list("applications"),
            matches=store.list("matches"),
            profile_version=profile_version, profile_data=profile_data,
            profile_history=[
                {**item, "data": normalize_profile_data(item.get("data"))}
                for item in store.list("profile_versions")[:12]
            ],
            profile_sections=PROFILE_SECTIONS,
            profile_section_labels=PROFILE_SECTION_LABELS,
            delivery_profile=store.latest_delivery_profile(),
            selected_resume=session.get("resume_id"),
            skill_levels=SKILL_LEVELS, experience_types=EXPERIENCE_TYPES,
            experience_type_labels=EXPERIENCE_TYPE_LABELS,
            upload_task=upload_task, upload_facts_available=bool(upload_facts),
            favorites=favorites, listed_favorites=listed,
            pending_favorites=pending, report_favorites=reports,
            fav_filter=fav, fav_page=fav_page, fav_pages=fav_pages, fav_total=fav_total,
        )

    @app.post("/profile/delivery")
    def save_delivery_profile():
        values = {
            "name": request.form.get("name") or "",
            "email": request.form.get("email") or "",
            "phone": request.form.get("phone") or "",
            "location": request.form.get("location") or "",
            "links": [
                value.strip()
                for value in request.form.getlist("links")
                if value.strip()
            ],
        }
        version_id = store.confirm_delivery_profile(values)
        flash(f"投递信息已确认并保存为版本 #{version_id}")
        return redirect(url_for("profile", _anchor="profile-delivery"))

    @app.post("/profile/avatar")
    def upload_profile_avatar():
        file = request.files.get("avatar")
        if not file or not file.filename:
            raise ValueError("请选择头像图片")
        metadata = save_profile_avatar(file, app.config["AVATAR_DIR"], request.form)
        try:
            version_id = store.set_profile_avatar(metadata)
        except Exception:
            # Content-addressed files can remain for another snapshot without exposing them.
            raise
        flash(f"头像已保存为职业资料版本 #{version_id}")
        return redirect(url_for("profile", _anchor="profile-avatar"))

    @app.post("/profile/avatar/adjust")
    def adjust_profile_avatar_route():
        profile_version = store.latest_profile_version()
        profile = normalize_profile_data(profile_version["data"] if profile_version else {})
        avatar = profile["avatar"]
        if avatar is None:
            raise ValueError("当前资料库没有可调整的头像")
        metadata = adjust_profile_avatar(avatar, request.form, app.config["AVATAR_DIR"])
        version_id = store.set_profile_avatar(metadata)
        flash(f"头像取景已保存为职业资料版本 #{version_id}")
        return redirect(url_for("profile", _anchor="profile-avatar"))

    @app.post("/profile/avatar/delete")
    def delete_profile_avatar():
        version_id = store.delete_profile_avatar()
        flash(f"头像已移除并保存为职业资料版本 #{version_id}")
        return redirect(url_for("profile", _anchor="profile-avatar"))

    @app.get("/profile/avatar/image")
    def profile_avatar_image():
        profile_version = store.latest_profile_version()
        profile = normalize_profile_data(profile_version["data"] if profile_version else {})
        avatar = profile["avatar"]
        if avatar is None:
            abort(404)
        path = safe_avatar_path(avatar, app.config["AVATAR_DIR"])
        response = send_file(path, mimetype="image/jpeg", as_attachment=False)
        response.cache_control.no_store = True
        response.cache_control.private = True
        return response

    @app.get("/profile/avatar/source")
    def profile_avatar_source():
        profile_version = store.latest_profile_version()
        profile = normalize_profile_data(profile_version["data"] if profile_version else {})
        avatar = profile["avatar"]
        if avatar is None:
            abort(404)
        path = safe_avatar_source_path(avatar, app.config["AVATAR_DIR"])
        response = send_file(path, mimetype="image/jpeg", as_attachment=False)
        response.cache_control.no_store = True
        response.cache_control.private = True
        return response

    def profile_section(section: str) -> str:
        if section not in PROFILE_SECTIONS:
            raise ValueError("未知资料分类")
        return section

    @app.post("/profile/<section>/items")
    def add_profile_item(section):
        section = profile_section(section)
        version_id = store.add_profile_item(section, request.form)
        flash(f"{PROFILE_SECTION_LABELS[section]}已保存为资料版本 #{version_id}")
        return redirect(url_for("profile", _anchor=f"profile-{section}"))

    @app.post("/profile/<section>/items/<item_id>")
    def update_profile_item(section, item_id):
        section = profile_section(section)
        version_id = store.update_profile_item(section, item_id, request.form)
        flash(f"{PROFILE_SECTION_LABELS[section]}已更新为资料版本 #{version_id}")
        return redirect(url_for("profile", _anchor=f"profile-{section}"))

    @app.post("/profile/<section>/items/<item_id>/delete")
    def delete_profile_item(section, item_id):
        section = profile_section(section)
        version_id = store.delete_profile_item(section, item_id)
        flash(f"{PROFILE_SECTION_LABELS[section]}已删除并保存为资料版本 #{version_id}")
        return redirect(url_for("profile", _anchor=f"profile-{section}"))

    @app.post("/resumes")
    def upload():
        file = request.files.get("resume")
        if not file or not file.filename:
            raise ValueError("请选择文本型 PDF")
        data = file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            abort(413)
        if not data.startswith(b"%PDF-"):
            raise ValueError("文件实际内容不是 PDF")
        filename = Path(file.filename.replace("\\", "/")).name
        path = uploads / f"{uuid.uuid4().hex}.pdf"
        path.write_bytes(data)
        path.chmod(0o600)
        try:
            resume_id = store.add_resume(filename, path, hashlib.sha256(data).hexdigest(), "")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        session["resume_id"] = resume_id
        import_values = request.form.getlist("import_profile")
        import_profile = "yes" in import_values if import_values else True
        task_id = store.enqueue(
            "analyze",
            {"resume_id": resume_id, "import_profile": import_profile},
            f"analyze:{resume_id}",
            max_attempts=2,
        )
        return task_redirect(task_id)

    @app.get("/resumes/<int:resume_id>")
    def resume(resume_id):
        item = require(store, "resumes", resume_id)
        reports = store.list("reports", resume_id=resume_id)
        reports = [
            {**report, "data": normalize_report_data(report["data"])}
            for report in reports
        ]
        versions = store.list("resume_versions", resume_id=resume_id)
        facts = versions[0]["facts"].get("facts", []) if versions else reports[0]["data"].get("facts", []) if reports else []
        return render_template("page.html", page="resume", resume=item, reports=reports, versions=versions,
                               mail_task=store.get("tasks", int(request.args.get("task") or 0)),
                               fact_lines="\n".join(f["text"] for f in facts))

    @app.get("/resumes/<int:resume_id>/pdf")
    def resume_pdf(resume_id):
        item = require(store, "resumes", resume_id)
        return send_file(resume_path(item["path"]), mimetype="application/pdf", as_attachment=True, download_name=item["filename"])

    @app.post("/resumes/<int:resume_id>/confirm")
    def confirm(resume_id):
        require(store, "resumes", resume_id)
        reports = store.list("reports", resume_id=resume_id)
        lines = [line.strip() for line in request.form.get("facts", "").splitlines() if line.strip()]
        if not lines:
            raise ValueError("事实不能为空；至少保留一条可用事实")
        prior = store.list("resume_versions", resume_id=resume_id)
        prior_facts = {f["text"]: f for f in prior[0]["facts"].get("facts", [])} if prior else {}
        facts = {"facts": [], "confirmed": False}
        for i, line in enumerate(lines, 1):
            previous = prior_facts.get(line)
            fact = dict(previous) if previous else {"category": "user_provided", "text": line, "evidence": line}
            fact["id"] = f"f{i}"
            fact["origin"] = previous.get("origin", prior[0]["origin"]) if previous else ("user_edited" if prior else "user_provided")
            facts["facts"].append(fact)
        if not prior or prior[0]["facts"] != facts:
            store.confirm_facts(resume_id, facts, reports[0]["id"] if reports else None)
        session["resume_id"] = resume_id
        store.resume_waiting(resume_id)
        return redirect(url_for("resume", resume_id=resume_id))

    @app.route("/preferences", defaults={"version_id": None}, methods=["GET", "POST"])
    @app.route("/preferences/<int:version_id>", methods=["GET", "POST"])
    def preferences(version_id):
        existing = store.list("preferences")
        if request.method == "POST":
            keywords = [x.strip().lower() for x in request.form.get("keywords", "").splitlines() if x.strip()]
            location = request.form.get("location", "").strip()
            types = request.form.getlist("job_types")
            sources = request.form.getlist("sources")
            if not keywords or not location or not types:
                raise ValueError("请填写目标岗位、地区并选择岗位类型")
            if len(keywords) > 7:
                raise ValueError("当前 Radar 免费基线最多 7 个关键词")
            if not set(types).issubset({"internship", "part time", "full time"}):
                raise ValueError("未知岗位类型")
            if not set(sources).issubset({"local", "jobsdb", "indeed", "linkedin"}):
                raise ValueError("未知岗位来源")
            data = dict(confirmed=True, keywords=keywords, location=location, job_types=types, sources=sources)
            pid = existing[0]["id"] if existing and existing[0]["data"] == data else store.confirm_preferences(None, data)
            if sources:
                running = active_search_task(pid)
                if running:
                    return task_redirect(running["id"])
                return task_redirect(queue_search(store, pid, f"search:{pid}:{secrets.token_hex(16)}"))
            return redirect(url_for("jobs"))
        return render_template("page.html", page="preferences", preference=existing[0] if existing else None)

    @app.post("/resumes/<int:resume_id>/select")
    def select_resume(resume_id):
        require(store, "resumes", resume_id)
        session["resume_id"] = resume_id
        return redirect(url_for("jobs"))

    @app.get("/jobs")
    def jobs():
        snapshots = list({j["job_uid"]: j for j in reversed(store.list("job_snapshots"))}.values())
        task = store.get("tasks", int(request.args.get("task") or 0))
        result_uids = set()
        if task and task["kind"] == "search":
            uid_by_id = {j["id"]: j["job_uid"] for j in store.list("job_snapshots")}
            result_uids = {uid_by_id[sid] for sid in (task.get("result") or {}).get("snapshot_ids", [])
                           if sid in uid_by_id}
        result_jobs = [j for j in snapshots if j["job_uid"] in result_uids]
        other_jobs = [j for j in snapshots if j["job_uid"] not in result_uids]
        favorites = {item["job_uid"] for item in store.list("favorites")}
        searching = bool(task and task["kind"] == "search")
        listed, listed_page, listed_pages, listed_total = paginate_items(
            result_jobs if searching else snapshots, "p")
        saved, saved_page, saved_pages, saved_total = paginate_items(other_jobs, "s")

        def enrich_cards(items):
            cards = []
            for item in items:
                cards.append({**item, **job_display_fields(item, store.get("jobs", item["job_uid"]))})
            return cards

        listed, saved = enrich_cards(listed), enrich_cards(saved)
        scheduler = app.extensions.get("scheduler")
        auto_search_minutes = 0
        if scheduler is not None:
            monitoring = scheduler.get_job("monitoring")
            interval = getattr(monitoring.trigger, "interval", None) if monitoring is not None else None
            auto_search_minutes = int(interval.total_seconds() // 60) if interval is not None else 0
        return render_template("workspace.html", page="jobs", jobs=snapshots, result_jobs=result_jobs,
            listed=listed, listed_page=listed_page, listed_pages=listed_pages, listed_total=listed_total,
            other_jobs=saved, saved_page=saved_page, saved_pages=saved_pages, saved_total=saved_total,
            preferences=store.list("preferences"),
            capabilities=__import__("career_assistant.integrations", fromlist=["SOURCE_CAPABILITIES"]).SOURCE_CAPABILITIES,
            favorites=favorites, task=task, auto_search_minutes=auto_search_minutes)

    @app.post("/search")
    def search():
        preference_id = int(request.form["preference_id"])
        running = active_search_task(preference_id)
        if running:
            return task_redirect(running["id"])
        operation = request.form.get("operation") or uuid.uuid4().hex
        return task_redirect(queue_search(store, preference_id, f"search:{preference_id}:{operation}"))

    @app.post("/jobs")
    def import_job():
        title, company, url, jd = (request.form.get(k, "").strip() for k in ("title", "company", "url", "jd"))
        if not title:
            raise ValueError("请填写职位；公司和链接可留空")
        if url and (urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname):
            raise ValueError("岗位链接必须为 http/https 地址")
        normal = BaseScraper.normalize_url(None, url) if url else "manual:" + hashlib.sha256(encode([title, company, jd]).encode()).hexdigest()
        external_id = hashlib.sha256(normal.encode()).hexdigest()[:24]
        job = JobPosting("manual", external_id, title, company, url, normal, description_snippet=jd[:500])
        db = Database(store.db_path)
        try:
            db.upsert_job(job)
        finally:
            db.close()
        canonical = store.find_job_by_url(normal)
        if canonical is None:
            raise ValueError("岗位保存失败，请重试")
        sid = store.snapshot_job(canonical["job_uid"], jd, bool(jd) and request.form.get("is_full") == "yes",
                           metadata={key: getattr(job, key) for key in ("title", "company", "source", "external_id", "url")})
        return redirect(url_for("job_detail", snapshot_id=sid))

    @app.post("/jobs/<int:snapshot_id>/complete")
    def complete_jd(snapshot_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        sid = store.snapshot_job(snapshot["job_uid"], request.form.get("jd", ""), True, metadata=snapshot)
        for item in store.list("preparations", snapshot_id=snapshot_id):
            if not snapshot["is_full"]:
                store.update_preparation(item["id"], snapshot_id=sid, state="queued", error="")
                store.enqueue("prepare", {"preparation_id": item["id"]}, f"jd-ready:{item['id']}:{sid}", max_attempts=2)
        return redirect(url_for("job_detail", snapshot_id=sid))

    @app.post("/jobs/<int:snapshot_id>/match")
    def match_job(snapshot_id):
        return prepare_job(snapshot_id)

    @app.get("/matches/<int:match_id>")
    def match_view(match_id):
        item = require(store, "matches", match_id)
        return redirect(url_for("job_detail", snapshot_id=item["snapshot_id"], match=match_id))

    @app.post("/matches/<int:match_id>/materials")
    def generate_material(match_id):
        require(store, "matches", match_id)
        return task_redirect(store.enqueue("materials", {"match_id": match_id}, f"materials:{match_id}", max_attempts=2))

    @app.route("/materials/<int:material_id>", methods=["GET", "POST"])
    def material(material_id):
        item = require(store, "materials", material_id)
        if request.method == "POST":
            cover_letter = request.form.get("cover_letter", "").strip()
            if not cover_letter:
                raise ValueError("求职信不能为空")
            # Preserve AI evidence metadata; human editing is explicitly identified.
            data = {**item["data"], "cover_letter": cover_letter, "human_edited": True, "human_confirmed": False, "status": "user_edited", "review": {"approved": False, "issues": ["用户编辑，未经 AI 审阅"]}}
            data["answers"] = [{**a, "answer": request.form.get(f"answer_{i}", "").strip()}
                               for i, a in enumerate(item["data"].get("answers", []))]
            data["suggestions"] = [{**a, "suggested": request.form.get(f"suggestion_{i}", "").strip()}
                                   for i, a in enumerate(item["data"].get("suggestions", []))]
            data.pop("markdown", None)
            data.pop("review_history", None)
            data["markdown"] = material_markdown(data)
            children = store.list("materials", parent_id=material_id)
            new_id = next((m["id"] for m in children if m["data"] == data), None)
            if new_id is None:
                new_id = store.save_material(item["match_id"], data, parent_id=material_id)
            return redirect(url_for("material", material_id=new_id))
        output = _material_output(item)
        return render_template(
            "page.html",
            page="material",
            material=item,
            markdown=material_markdown(item["data"]),
            output_approved=_existing_output_approval(output) is not None,
        )

    @app.route("/materials/<int:material_id>/approve", methods=["GET", "POST"])
    def approve_material(material_id):
        item = require(store, "materials", material_id)
        output = _material_output(item)
        if request.method == "POST":
            return _approve_output(output)
        return render_template(
            "approval.html",
            page="approval",
            output=output,
            existing_approval=_existing_output_approval(output),
        )

    @app.get("/materials/<int:material_id>/export")
    def export_material(material_id):
        item = require(store, "materials", material_id)
        output = _material_output(item)
        approval = _existing_output_approval(output)
        if approval is None:
            return redirect(url_for("approve_material", material_id=material_id))
        current_hash = output_content_hash(material_markdown(item["data"]))
        if not secrets.compare_digest(current_hash, approval["content_hash"]):
            return redirect(url_for("approve_material", material_id=material_id))
        decision = BoundaryEvaluation.from_dict(
            output["boundary"]
        ).decide(
            BoundaryAction.EXPORT,
            approved=True,
            hash_matches=secrets.compare_digest(
                current_hash,
                approval["content_hash"],
            ),
        )
        if not decision.allowed:
            flash(
                f"导出被拒绝（{decision.level.value.upper()} · "
                f"{decision.code}）：{decision.summary}"
            )
            return redirect(url_for("approve_material", material_id=material_id))
        payload = canonical_output_text(material_markdown(item["data"])).encode("utf-8")
        return send_file(io.BytesIO(payload), mimetype="text/markdown; charset=utf-8",
                         as_attachment=True, download_name=f'material-{material_id}-v{item["version"]}.md')

    @app.post("/materials/<int:material_id>/application")
    def create_application(material_id):
        material = require(store, "materials", material_id)
        return redirect(url_for("application", application_id=store.create_application(material_id)))

    @app.route("/applications/<int:application_id>", methods=["GET", "POST"])
    def application(application_id):
        item = require(store, "applications", application_id)
        if request.method == "POST":
            store.transition_application(application_id, request.form.get("status", ""), request.form.get("note", ""), event_date=request.form.get("event_date", ""))
            return redirect(url_for("application", application_id=application_id))
        snapshot = store.get("job_snapshots", item["course_snapshot_id"]) if item["course_snapshot_id"] else store.get("jobs", item["job_uid"])
        return render_template("page.html", page="application", application=item, snapshot=snapshot,
            versions=store.list("resume_versions"), materials=store.list("materials"),
            events=store.list("application_events", application_id=application_id),
            pack=store.latest_application_pack(application_id))

    @app.post("/applications/<int:application_id>/pack")
    def create_application_pack(application_id):
        """生成新的投递信息包版本；程序不填写、不上传、不提交任何真实表单。"""
        require(store, "applications", application_id)
        result = create_pack(store, application_id)
        flash(f"已生成投递信息包版本 #{result['version']}；请自行前往招聘网站填写与提交。")
        return redirect(url_for("application", application_id=application_id))

    @app.get("/applications/<int:application_id>/pack/export")
    def export_application_pack(application_id):
        """导出投递信息包 Markdown；没有已保存版本时按当前依据生成一份预览。"""
        require(store, "applications", application_id)
        record = store.latest_application_pack(application_id)
        pack = record["data"] if record else build_pack(store, application_id)
        response = Response(pack_markdown(pack), mimetype="text/markdown; charset=utf-8")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="application-pack-{application_id}.md"')
        return response

    def share_base_url() -> str:
        """分享链接基准地址：优先 CAREER_SHARE_BASE_URL，其次当前请求来源。"""
        configured = str(app.config.get("SHARE_BASE_URL") or "").strip()
        return configured.rstrip("/") if configured else str(request.host_url or "").rstrip("/")

    @app.post("/reports/<int:report_id>/share")
    def share_report(report_id):
        """为报告生成分享链接。

        链接明文与二维码只出现在**这一次响应**里：不写进 URL、不进 session、不进日志。
        数据库只保存 sha256 摘要与前 8 位提示，所以离开本页后无法再找回明文。
        """
        require(store, "reports", report_id)
        base = share_base_url()
        result = create_report_share(store, report_id, base)
        result["local_only"] = (urlsplit(base).hostname or "") in {"localhost", "127.0.0.1", "::1"}
        report = store.get("reports", report_id)
        return render_template("page.html", page="share", share=result,
                               resume_id=report["resume_id"])

    @app.post("/shares/revoke")
    def revoke_share_link():
        """撤销一条分享链接；令牌明文只在表单里出现一次。"""
        token = str(request.form.get("token") or "").strip()
        if not token:
            raise ValueError("缺少分享令牌")
        if not revoke_share(store, token):
            raise ValueError("分享链接不存在或已撤销")
        flash("分享链接已撤销，对方再打开会看到「分享链接已被撤销」。")
        resume_id = int(request.form.get("resume_id") or 0)
        target = url_for("resume", resume_id=resume_id) if resume_id else url_for("applications")
        return redirect(target)

    @app.post("/jobs/<int:snapshot_id>/favorite")
    def toggle_favorite(snapshot_id):
        require(store, "job_snapshots", snapshot_id)
        favorited = store.toggle_favorite(snapshot_id)
        flash("已收藏到我的资料" if favorited else "已取消收藏")
        target = request.form.get("return_to", "")
        if target.startswith("/jobs") or target.startswith("/profile"):
            return redirect(target)
        return redirect(url_for("jobs"))

    @app.post("/jobs/<int:snapshot_id>/application")
    def application_from_job(snapshot_id):
        status, note = request.form.get("status", "saved"), request.form.get("note", "")
        aid = store.application_for_job(snapshot_id, status, note=note, event_date=request.form.get("event_date", ""))
        if store.get("applications", aid)["status"] != status or note:
            store.transition_application(aid, status, note, event_date=request.form.get("event_date", ""))
        return redirect(url_for("application", application_id=aid))

    @app.post("/applications/<int:application_id>/basis")
    def application_basis(application_id):
        store.link_application_basis(application_id, int(request.form.get("resume_version_id") or 0) or None,
            int(request.form.get("material_id") or 0) or None, request.form.get("note", ""), usage=request.form.get("usage"))
        return redirect(url_for("application", application_id=application_id))

    @app.post("/jobs/<int:snapshot_id>/prepare")
    def prepare_job(snapshot_id):
        require(store, "job_snapshots", snapshot_id)
        rid = int(request.form.get("resume_id") or session.get("resume_id") or 0) or None
        pid = int(request.form.get("preference_id") or 0) or None
        if rid: require(store, "resumes", rid); session["resume_id"] = rid
        if pid: require(store, "preferences", pid)
        preparation_id = store.prepare(snapshot_id, rid, pid)
        store.enqueue("prepare", {"preparation_id": preparation_id}, f"prepare:{preparation_id}", max_attempts=2)
        return redirect(url_for("job_detail", snapshot_id=require(store, "preparations", preparation_id)["snapshot_id"]))

    @app.route("/preparations/<int:preparation_id>", methods=["GET", "POST"])
    def preparation(preparation_id):
        item = require(store, "preparations", preparation_id)
        if request.method == "POST":
            if item["state"] not in {"waiting_facts", "waiting_jd", "failed", "problem_draft"}:
                return redirect(url_for("job_detail", snapshot_id=require(store, "preparations", preparation_id)["snapshot_id"]))
            if not item["resume_version_id"] and request.form.get("resume_id"):
                rid = int(request.form["resume_id"])
                require(store, "resumes", rid)
                versions = store.list("resume_versions", resume_id=rid)
                store.update_preparation(preparation_id, resume_id=rid)
                if versions:
                    store.update_preparation(preparation_id, resume_version_id=versions[0]["id"])
            old = require(store, "job_snapshots", item["snapshot_id"])
            if not old["is_full"] and request.form.get("jd", "").strip():
                sid = store.snapshot_job(old["job_uid"], request.form["jd"], True, metadata=old)
                store.update_preparation(preparation_id, snapshot_id=sid)
            previous = next((t["id"] for t in store.list("tasks", kind="prepare")
                             if t["payload"].get("preparation_id") == preparation_id), 0)
            store.enqueue("prepare", {"preparation_id": preparation_id}, f"continue:{preparation_id}:{request.form.get('operation') or previous}", max_attempts=2, resume_preparation=preparation_id)
            return redirect(url_for("job_detail", snapshot_id=require(store, "preparations", preparation_id)["snapshot_id"]))
        return redirect(url_for("job_detail", snapshot_id=item["snapshot_id"]))

    @app.post("/email/<kind>/<int:record_id>")
    def email(kind, record_id):
        if kind not in {"report", "match"}:
            abort(404)
        require(store, "reports" if kind == "report" else "matches", record_id)
        payload = {"type": kind, "id": record_id}
        # 幂等键原本写死成 email:{kind}:{id}，会把页面钉死在**第一次**那条任务上：
        # 第一次没配 SMTP 时留下的是「仅预览」，之后即使配好了再点也只会回到旧任务，
        # 用户看到的永远是「未发送，仅预览」，像是没走邮箱配置。
        # 现在按「这份内容最近一次邮件任务」的状态决定复用还是新建。
        previous = next((t for t in store.list("tasks", kind="email")
                         if (t.get("payload") or {}) == payload), None)
        if previous is not None:
            if previous["status"] in {"queued", "running"}:
                return task_redirect(previous["id"])      # 连点：复用正在跑的那条
            if store.list("email_attempts", task_id=previous["id"], status="accepted"):
                flash("这份内容此前已发送成功（服务器已接受），不会重复发送。")
                return task_redirect(previous["id"])
        # 新任务的幂等键带上上一条任务 id，保证每次点击都是新的一条、且不会撞键。
        key = f"email:{kind}:{record_id}" if previous is None \
            else f"email:{kind}:{record_id}:{previous['id']}"
        return task_redirect(store.enqueue("email", payload, key,
                                           max_attempts=2, side_effect=True))

    @app.get("/tasks/<int:task_id>")
    def task(task_id):
        item = require(store, "tasks", task_id)
        facts = store.list("resume_versions", resume_id=item["payload"]["resume_id"]) if item["kind"] == "analyze" else []
        return render_template("page.html", page="task", task=item, attempts=store.list("email_attempts", task_id=task_id),
                              facts_available=bool(facts))

    @app.post("/tasks/<int:task_id>/retry")
    def retry(task_id):
        item = require(store, "tasks", task_id)
        if item["status"] == "uncertain" and request.form.get("confirmed") != "yes":
            raise ValueError("结果不确定；请检查外部结果后确认重试，可能重复发送")
        return task_redirect(store.retry(task_id))

    def _assistant_selected():
        allowed = {item["snapshot_id"] for item in store.list_favorites()}
        selected = [int(i) for i in (session.get("assistant_jobs") or []) if int(i) in allowed]
        session["assistant_jobs"] = selected
        return selected

    def _assistant_resume_id():
        if "assistant_resume_id" in session:
            return session.get("assistant_resume_id")
        return session.get("resume_id")

    @app.get("/assistant")
    def assistant():
        resume_id = _assistant_resume_id()
        favorites = [annotate_favorite(item) for item in store.list_favorites()]
        counts = store.tailored_resume_counts()
        pending = [item for item in favorites if not counts.get(item["job_uid"])]
        ready = [item for item in favorites if counts.get(item["job_uid"])]
        profile_version = store.latest_profile_version()
        profile_data = normalize_profile_data(profile_version["data"] if profile_version else {})
        return render_template(
            "workspace.html",
            page="assistant",
            favorites=favorites,
            pending_jobs=pending,
            ready_jobs=ready,
            pending_groups=group_favorites_by_date(pending),
            ready_groups=group_favorites_by_date(ready),
            tailored_counts=counts,
            selected_jobs=_assistant_selected(),
            resumes=store.list("resumes"),
            selected_resume=resume_id,
            profile_version=profile_version,
            profile_counts={
                section: len(profile_data[section]) for section in PROFILE_SECTIONS
            },
            profile_has_content=profile_has_content(profile_data),
            messages=session.get("assistant_chat") or [],
            ai_ready=all(os.environ.get(k) for k in ("RADAR_AI_API_KEY", "RADAR_AI_MODEL", "RADAR_AI_BASE_URL")),
        )

    @app.post("/assistant/select")
    def assistant_select():
        data = request.get_json(silent=True) or {}
        allowed = {item["snapshot_id"] for item in store.list_favorites()}
        raw = data.get("snapshot_ids", [])
        if isinstance(raw, str):
            raw = [part for part in raw.split(",") if part.strip()]
        selected = []
        for value in raw or []:
            try:
                snapshot_id = int(value)
            except (TypeError, ValueError):
                continue
            if snapshot_id in allowed:
                selected.append(snapshot_id)
        session["assistant_jobs"] = selected
        resume_id = data.get("resume_id")
        if resume_id not in (None, ""):
            resume = require(store, "resumes", int(resume_id))
            session["assistant_resume_id"] = resume["id"]
            session["resume_id"] = resume["id"]
        else:
            session["assistant_resume_id"] = None
        return jsonify({"ok": True, "count": len(selected)})

    @app.post("/assistant/chat")
    def assistant_chat():
        from career_assistant.assistant_agent import stream_assistant
        data = request.get_json(silent=True) or {}
        message = str(data.get("message") or "").strip()
        if not message:
            return jsonify({"ok": False, "error": "请输入问题或任务说明。"}), 400
        selected = _assistant_selected()
        resume_id = _assistant_resume_id()
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in (session.get("assistant_chat") or [])[-12:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        search_ids = list(session.get("assistant_search_jobs") or [])
        chat = list(session.get("assistant_chat") or [])
        chat.append({"role": "user", "content": message})
        session["assistant_chat"] = chat[-20:]

        def generate():
            for event in stream_assistant(store, message, selected, resume_id, history, search_ids):
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/assistant/save")
    def assistant_save():
        data = request.get_json(silent=True) or {}
        reply = str(data.get("reply") or "").strip()
        found = data.get("found") or []
        if found:
            cleaned = []
            for value in found:
                try:
                    cleaned.append(int(value))
                except (TypeError, ValueError):
                    continue
            session["assistant_search_jobs"] = cleaned
        if reply:
            chat = list(session.get("assistant_chat") or [])
            last = chat[-1] if chat else None
            if not last or last.get("role") != "assistant" or last.get("content") != reply:
                chat.append({"role": "assistant", "content": reply})
            session["assistant_chat"] = chat[-20:]
        return jsonify({"ok": True})

    @app.post("/assistant/clear")
    def assistant_clear():
        session["assistant_chat"] = []
        return jsonify({"ok": True})

    @app.get("/applications")
    def applications():
        """申请管理：看板视图 + 状态／关键词筛选；筛选结果仍可进入详情。"""
        status = str(request.args.get("status") or "").strip()
        keyword = str(request.args.get("q") or "").strip()[:80]
        if status and status not in APPLICATION_STATUSES:
            raise ValueError("未知申请状态")
        rows, total = store.query_applications(status=status, keyword=keyword,
                                               limit=100, offset=0)
        columns: dict[str, list[dict]] = {key: [] for key in APPLICATION_STATUS_ORDER}
        for row in store.query_application_board():
            key = row["status"] if row["status"] in columns else "saved"
            columns[key].append(row)
        return render_template("workspace.html", page="applications", applications=rows,
                               application_total=total, board_columns=columns,
                               board_summary=store.board_summary(),
                               application_status_order=APPLICATION_STATUS_ORDER,
                               filter_status=status, filter_keyword=keyword)

    @app.post('/settings/digest')
    def save_digest():
        from .digest import save_settings
        save_settings(store, request.form.get('enabled') == 'yes', request.form.get('time', ''))
        return redirect(url_for('settings'))

    @app.post('/settings/digest/<int:task_id>/retry')
    def retry_digest(task_id):
        task = require(store, 'tasks', task_id)
        if task['kind'] != 'email' or task['payload'].get('type') != 'digest':
            abort(404)
        store.retry(task_id)
        return redirect(url_for('settings'))

    @app.get('/settings/digest/<int:task_id>/preview')
    def digest_preview(task_id):
        task = require(store, 'tasks', task_id)
        if task['kind'] != 'email' or task['payload'].get('type') != 'digest':
            abort(404)
        # 正文由 digest 生成时已逐段转义，这里补上文档声明避免中文乱码。
        body = task['payload'].get('html') or ''
        return Response(
            '<!doctype html><html lang="zh-Hans"><head><meta charset="utf-8">'
            '<title>汇总内容预览</title></head><body>'
            '<h1>汇总内容预览，不代表已发送</h1>' + body + '</body></html>',
            mimetype="text/html; charset=utf-8",
        )

    @app.get("/settings")
    def settings():
        from .digest import settings as digest_settings, tasks as digest_tasks
        scheduler = app.extensions.get('scheduler')
        return render_template("workspace.html", page="settings", digest=digest_settings(store),
            digest_tasks=digest_tasks(store)[:10], scheduler_running=bool(scheduler and scheduler.running), ai_ready=all(os.environ.get(k) for k in
            ("RADAR_AI_API_KEY", "RADAR_AI_MODEL", "RADAR_AI_BASE_URL")), smtp_ready=bool(os.environ.get("SMTP_HOST")))

    @app.get("/jobs/<int:snapshot_id>")
    def job_detail(snapshot_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        history = store.list("job_snapshots", job_uid=snapshot["job_uid"])
        ids = {j["id"] for j in history}
        preparations = [p for p in store.list("preparations") if p["snapshot_id"] in ids]
        preparation = preparations[0] if preparations else None
        matches = [m for m in store.list("matches") if m["snapshot_id"] in ids]
        selected = int(request.args.get("match") or 0)
        match = next((m for m in matches if m["id"] == selected), None) if selected else (matches[0] if matches else None)
        tasks = [t for t in store.list("tasks") if (t["kind"] == "materials" and match and t["payload"].get("match_id") == match["id"])
                 or (t["kind"] == "email" and t["payload"].get("type") == "match" and match and t["payload"].get("id") == match["id"])
                 or (t["kind"] == "analyze" and preparation and t["payload"].get("resume_id") == preparation["resume_id"])]
        tasks = list({t["kind"]: t for t in reversed(tasks)}.values())
        job = store.get("jobs", snapshot["job_uid"])
        args = dict(snapshot=snapshot, history=history, preparation=preparation, match=match, matches=matches,
            job=job, intro=job_display_fields(snapshot, job),
            favorited=store.is_favorite(snapshot["job_uid"]),
            facts_available=bool(preparation and preparation["resume_version_id"]), tasks=tasks, materials=store.list("materials", match_id=match["id"]) if match else [],
            resumes=store.list("resumes"), selected_resume=session.get("resume_id"), preferences=store.list("preferences"),
            applications=store.list("applications", job_uid=snapshot["job_uid"]),
            tailored_resumes=store.list_tailored_resumes(snapshot["job_uid"]))
        if request.args.get("fragment"):
            return render_template("job_progress.html", **args)
        for item in args["tailored_resumes"]:
            output = _tailored_output(snapshot, item)
            item["output_approved"] = _existing_output_approval(output) is not None
        return render_template("workspace.html", page="job", **args)

    @app.get("/jobs/<int:snapshot_id>/resumes/<int:record_id>")
    def tailored_resume(snapshot_id, record_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        item = store.get_tailored_resume(snapshot_id, record_id)
        if not item:
            abort(404)
        output = _tailored_output(snapshot, item)
        return render_template(
            "workspace.html",
            page="tailored_resume",
            snapshot=snapshot,
            item=item,
            output=output,
            output_approved=_existing_output_approval(output) is not None,
        )

    @app.post("/jobs/<int:snapshot_id>/resumes/<int:record_id>/delete")
    def delete_tailored_resume(snapshot_id, record_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        item = store.get_tailored_resume(snapshot_id, record_id)
        if not item:
            abort(404)
        removed = store.delete_tailored_resume(
            snapshot_id,
            record_id,
            g.user["id"],
        )
        if removed:
            flash("针对性简历已删除；职业资料、审批记录和底层审计版本仍保留。")
        else:
            flash("针对性简历已从列表移除。")
        return redirect(url_for("job_detail", snapshot_id=snapshot["id"]))

    @app.route("/jobs/<int:snapshot_id>/resumes/<int:record_id>/approve", methods=["GET", "POST"])
    def approve_tailored_resume(snapshot_id, record_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        item = store.get_tailored_resume(snapshot_id, record_id)
        if not item:
            abort(404)
        output = _tailored_output(snapshot, item)
        if request.method == "POST":
            return _approve_output(output)
        return render_template(
            "approval.html",
            page="approval",
            output=output,
            existing_approval=_existing_output_approval(output),
        )

    @app.get("/jobs/<int:snapshot_id>/resumes/<int:record_id>/pdf")
    def tailored_resume_pdf(snapshot_id, record_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        item = store.get_tailored_resume(snapshot_id, record_id)
        if not item or item.get("artifact_status") not in {"audit_pass", "audit_issue"}:
            abort(404)
        output = _tailored_output(snapshot, item)
        if output.get("integrity_error"):
            abort(409, description=output["integrity_error"])
        response = send_file(
            Path(output["pdf_path"]),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=tailored_download_name(snapshot, item),
            conditional=True,
        )
        return response

    @app.get("/jobs/<int:snapshot_id>/resumes/<int:record_id>/download")
    def download_tailored_resume(snapshot_id, record_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        item = store.get_tailored_resume(snapshot_id, record_id)
        if not item:
            abort(404)
        output = _tailored_output(snapshot, item)
        approval = _existing_output_approval(output)
        if approval is None:
            return redirect(
                url_for(
                    "approve_tailored_resume",
                    snapshot_id=snapshot_id,
                    record_id=record_id,
                )
            )
        if output.get("artifact_kind") == "pdf":
            if output.get("integrity_error"):
                flash(output["integrity_error"])
                return redirect(
                    url_for(
                        "approve_tailored_resume",
                        snapshot_id=snapshot_id,
                        record_id=record_id,
                    )
                )
            current_hash = output["current_pdf_sha256"]
        else:
            current_hash = output_content_hash(item.get("body") or "")
        if not secrets.compare_digest(current_hash, approval["content_hash"]):
            flash("文件指纹已变化，旧审批不能继续用于下载。")
            return redirect(
                url_for(
                    "approve_tailored_resume",
                    snapshot_id=snapshot_id,
                    record_id=record_id,
                )
            )
        if output.get("boundary"):
            decision = BoundaryEvaluation.from_dict(
                output["boundary"]
            ).decide(
                BoundaryAction.EXPORT,
                approved=True,
                hash_matches=secrets.compare_digest(
                    current_hash,
                    approval["content_hash"],
                ),
            )
            if not decision.allowed:
                flash(
                    f"导出被拒绝（{decision.level.value.upper()} · "
                    f"{decision.code}）：{decision.summary}"
                )
                return redirect(
                    url_for(
                        "approve_tailored_resume",
                        snapshot_id=snapshot_id,
                        record_id=record_id,
                    )
                )
        if output.get("artifact_kind") == "pdf":
            return send_file(
                Path(output["pdf_path"]),
                mimetype="application/pdf",
                as_attachment=True,
                download_name=tailored_download_name(snapshot, item),
                conditional=True,
            )
        payload = canonical_output_text(item.get("body") or "").encode("utf-8")
        return send_file(
            io.BytesIO(payload),
            mimetype="text/markdown; charset=utf-8",
            as_attachment=True,
            download_name=tailored_download_name(snapshot, item).removesuffix(".pdf")
            + ".md",
        )

    @app.post("/tasks/<int:task_id>/inputs")
    def material_inputs(task_id):
        task = require(store, "tasks", task_id)
        cp = store.accept_material_inputs(task_id, request.form)
        new_id = store.enqueue("materials", task["payload"], f"inputs:{task_id}", max_attempts=2, checkpoint=cp)
        return task_redirect(new_id)

    @app.get("/tasks/<int:task_id>/status")
    def task_status(task_id):
        task = require(store, "tasks", task_id)
        facts = store.list("resume_versions", resume_id=task["payload"]["resume_id"]) if task["kind"] == "analyze" else []
        return render_template("task_progress.html", task=task, facts_available=bool(facts))

    return app
