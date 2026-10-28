"""Local, server-rendered Flask workflow for the INT6181 extension."""
from __future__ import annotations

from career_assistant.config import resume_path

import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit
import uuid

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, stream_with_context, url_for, jsonify
from career_assistant.config import DATA_DIR, DB_PATH
from career_assistant.db import Database
from career_assistant.models import JobPosting, now_hk, parse_iso
from career_assistant.scrapers.base import BaseScraper
from .store import Store, APPLICATION_STATUSES, stamp, encode
from .service import require, material_markdown, queue_search

LABELS = {"saved": "收藏", "preparing": "准备中", "applied": "已投递", "interview": "面试",
          "offer": "Offer", "rejected": "拒绝", "withdrawn": "撤回"}


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
    return f"{safe or 'resume'}.md"


def create_app(store=None, data_dir=None, testing=False):
    app = Flask(__name__)
    data_dir = Path(data_dir or os.environ.get("RADAR_DATA_DIR", str(DATA_DIR)))
    uploads = data_dir / "course_uploads"
    uploads.mkdir(parents=True, exist_ok=True)
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
                      TESTING=testing, UPLOAD_DIR=uploads, BASE_URL="http://127.0.0.1:5000")
    store = store or Store(os.environ.get("RADAR_DB_PATH", str(DB_PATH)))
    store.migrate()
    app.extensions["store"] = store

    @app.before_request
    def protect_local():
        if request.host.split(":")[0] not in {"localhost", "127.0.0.1"}:
            abort(403)
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(32)
        if request.method == "POST":
            supplied = request.form.get("csrf") or request.headers.get("X-CSRF") or ""
            if request.is_json:
                supplied = supplied or str((request.get_json(silent=True) or {}).get("csrf") or "")
            if not secrets.compare_digest(str(supplied), session["csrf"]):
                abort(403)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.context_processor
    def context():
        return {"csrf": session.get("csrf", ""), "labels": LABELS, "operation": secrets.token_hex(16)}

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
        return invalid(ValueError("简历不得超过 10 MB。请替换文件。")) if request.form.get("return_to") else (render_template("page.html", page="error", error="简历不得超过 10 MB。"), 413)

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
            return redirect(url_for("profile", task=task_id))
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
        matched = store.matched_job_uids()
        pending = [item for item in favorites if not tailored.get(item["job_uid"])]
        reports = [item for item in favorites if item["job_uid"] in matched]
        fav = request.args.get("fav") or "pending"
        if fav not in {"pending", "reports"}:
            fav = "pending"
        pool = pending if fav == "pending" else reports
        listed, fav_page, fav_pages, fav_total = paginate_items(pool, "p")
        return render_template(
            "page.html", page="home",
            preparations=store.list("preparations"), resumes=store.list("resumes"),
            versions=store.list("resume_versions"), preferences=store.list("preferences"),
            tasks=store.list("tasks")[:20], applications=store.list("applications"),
            matches=store.list("matches"),
            favorites=favorites, listed_favorites=listed,
            pending_favorites=pending, report_favorites=reports,
            fav_filter=fav, fav_page=fav_page, fav_pages=fav_pages, fav_total=fav_total,
        )

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
        task_id = store.enqueue("analyze", {"resume_id": resume_id}, f"analyze:{resume_id}", max_attempts=2)
        return task_redirect(task_id)

    @app.get("/resumes/<int:resume_id>")
    def resume(resume_id):
        item = require(store, "resumes", resume_id)
        reports = store.list("reports", resume_id=resume_id)
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
                return task_redirect(queue_search(store, pid, f"search:{pid}:{request.form.get('operation') or secrets.token_hex(16)}"))
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
        return render_template("workspace.html", page="jobs", jobs=snapshots, result_jobs=result_jobs,
            listed=listed, listed_page=listed_page, listed_pages=listed_pages, listed_total=listed_total,
            other_jobs=saved, saved_page=saved_page, saved_pages=saved_pages, saved_total=saved_total,
            preferences=store.list("preferences"),
            capabilities=__import__("career_assistant.integrations", fromlist=["SOURCE_CAPABILITIES"]).SOURCE_CAPABILITIES,
            favorites=favorites, task=task)

    @app.post("/search")
    def search():
        preference_id = int(request.form["preference_id"])
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
        canonical = next(j for j in store.list("jobs") if j["url_normalized"] == normal)
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
        return render_template("page.html", page="material", material=item, markdown=material_markdown(item["data"]))

    @app.get("/materials/<int:material_id>/export")
    def export_material(material_id):
        item = require(store, "materials", material_id)
        return send_file(io.BytesIO(material_markdown(item["data"]).encode()), mimetype="text/markdown; charset=utf-8",
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
            events=store.list("application_events", application_id=application_id))

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
        return task_redirect(store.enqueue("email", {"type": kind, "id": record_id},
                    f"email:{kind}:{record_id}", max_attempts=2, side_effect=True))

    @app.get("/tasks/<int:task_id>")
    def task(task_id):
        item = require(store, "tasks", task_id)
        return render_template("page.html", page="task", task=item, attempts=store.list("email_attempts", task_id=task_id))

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

    @app.get("/assistant")
    def assistant():
        resume_id = session.get("assistant_resume_id") or session.get("resume_id")
        favorites = [annotate_favorite(item) for item in store.list_favorites()]
        counts = store.tailored_resume_counts()
        pending = [item for item in favorites if not counts.get(item["job_uid"])]
        ready = [item for item in favorites if counts.get(item["job_uid"])]
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
        return jsonify({"ok": True, "count": len(selected)})

    @app.post("/assistant/chat")
    def assistant_chat():
        from career_assistant.assistant_agent import stream_assistant
        data = request.get_json(silent=True) or {}
        message = str(data.get("message") or "").strip()
        if not message:
            return jsonify({"ok": False, "error": "请输入问题或任务说明。"}), 400
        selected = _assistant_selected()
        resume_id = session.get("assistant_resume_id") or session.get("resume_id")
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
        return render_template("workspace.html", page="applications", applications=store.list("applications"))

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
        return '<h1>汇总内容预览，不代表已发送</h1>' + task['payload']['html']

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
        return render_template("workspace.html", page="job", **args)

    @app.get("/jobs/<int:snapshot_id>/resumes/<int:record_id>")
    def tailored_resume(snapshot_id, record_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        item = store.get_tailored_resume(snapshot_id, record_id)
        if not item:
            abort(404)
        return render_template("workspace.html", page="tailored_resume", snapshot=snapshot, item=item)

    @app.get("/jobs/<int:snapshot_id>/resumes/<int:record_id>/download")
    def download_tailored_resume(snapshot_id, record_id):
        snapshot = require(store, "job_snapshots", snapshot_id)
        item = store.get_tailored_resume(snapshot_id, record_id)
        if not item:
            abort(404)
        payload = (item["body"] or "").encode("utf-8")
        return send_file(
            io.BytesIO(payload),
            mimetype="text/markdown; charset=utf-8",
            as_attachment=True,
            download_name=tailored_download_name(snapshot, item),
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
