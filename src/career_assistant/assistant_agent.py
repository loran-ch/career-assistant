"""Local Deep Agent for job matching and resume drafts.

The model never receives unselected favorites. Job and resume text is loaded
only through tools that check the current request's selected snapshot IDs.
"""
from __future__ import annotations

import contextvars
import os
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlparse

from career_assistant.ai import AIService, AIError, minimize_personal_data
from career_assistant.profile_context import (
    merge_profile_facts,
    normalize_profile_data,
    profile_context_text,
)
from career_assistant.resume_document import (
    ResumeValidationError,
    resume_content_hash,
    resume_plaintext,
    validate_resume_audit,
    validate_resume_spec,
)
from career_assistant.resume_boundary import (
    boundary_check,
    contains_instruction_injection,
    evaluate_boundaries,
    fact_objects_from_facts,
)
from career_assistant.resume_pdf import ResumeRenderError, render_resume_pdf
from career_assistant.store import Store

_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("assistant_ctx")
_AGENT = None

SYSTEM_PROMPT = """你是 Career Assistant 里的求职 Deep Agent。
必须遵守：
1. 收藏岗位只能通过工具读取。用户未点选的收藏岗位你看不到，禁止猜测其 JD。本次搜索返回的岗位可以用 list_found_jobs / read_selected_job 查看。
2. 分析或改写已收藏岗位时，若 list_selected_jobs 为空，请用户先在左侧勾选。
3. 上传简历和用户维护的职业资料库都是有效职业事实来源。使用 read_selected_resume 可同时读取当前选择的上传简历和结构化资料；需要单独查看职业资料库时使用 read_career_profile。
4. 用户可以选择“仅使用职业资料库”，此时不得声称已读取上传简历。两种来源都没有实质内容时，才请用户上传简历或到「我的资料」补充。
5. 不得编造经历、技能、学历、成果数字、公司或在招职位。缺信息标为未知或省略。
6. 批量生成简历：由程序固定流程先拆解岗位要求、映射事实证据，再生成结构化简历、PDF 和独立审计报告。职业资料库足以支撑时可直接生成，不要求用户额外上传简历。搜索到但未收藏点选的岗位不能保存针对性简历，请用户先收藏并勾选。
7. 用户要找网上岗位或要推荐时：先读取可用职业事实，从简历或职业资料库提炼至多 7 个职位关键词（优先与职业方向一致的英文岗位名），再 search_jobs_online。只根据工具返回的岗位推荐，对照职业事实说明匹配理由、缺口与优先投递顺序。可建议打开 /jobs 查看完整列表并收藏。
8. 用中文回答，结构清楚。
"""

_LOCATIONS = {
    "香港": "Hong Kong", "hong kong": "Hong Kong", "hk": "Hong Kong",
    "中国": "China", "中国内地": "China", "内地": "China",
    "美国": "United States", "英国": "United Kingdom", "新加坡": "Singapore",
    "加拿大": "Canada", "澳大利亚": "Australia", "日本": "Japan",
    "台湾": "Taiwan", "澳门": "Macau", "远程": "Remote",
}


def _selected_ids() -> set[int]:
    return set(_CTX.get().get("snapshot_ids") or [])


def _visible_ids() -> set[int]:
    return _selected_ids() | set(_CTX.get().get("search_ids") or [])


def _store() -> Store:
    return _CTX.get()["store"]


def list_selected_jobs() -> str:
    """列出用户当前点选、助手可见的收藏岗位。未点选的岗位不会出现。"""
    store = _store()
    ids = _selected_ids()
    if not ids:
        return "当前没有点选任何收藏岗位。助手看不到岗位 JD。请用户在左侧勾选后再问。"
    lines = []
    for snapshot_id in sorted(ids):
        item = store.get("job_snapshots", snapshot_id)
        if not item:
            continue
        lines.append(f"#{snapshot_id} {item['title']} · {item['company'] or '公司未提供'} · {item['source']}")
    return "\n".join(lines) if lines else "点选的岗位已不存在，请重新勾选。"


def read_selected_job(snapshot_id: int) -> str:
    """读取一个已点选收藏岗位，或本次搜索返回岗位的介绍与 JD。其他岗位拒绝。"""
    if snapshot_id not in _visible_ids():
        return f"岗位 #{snapshot_id} 未点选且不在本次搜索结果中，助手不能查看。"
    item = _store().get("job_snapshots", snapshot_id)
    if not item:
        return "岗位不存在。"
    job = _store().get("jobs", item["job_uid"]) or {}
    parts = [
        f"职位：{item['title']}",
        f"公司：{item['company'] or '未提供'}",
        f"来源：{item['source']}",
        f"地点：{job.get('location') or '未提供'}",
        f"薪资：{job.get('salary_text') or '未提供'}",
        f"链接：{item['url'] or '未提供'}",
        f"完整JD：{'是' if item['is_full'] else '否（以下为摘要）'}",
        "介绍：",
        item.get("jd") or "（无介绍）",
    ]
    return minimize_personal_data("\n".join(parts))


def read_selected_resume() -> str:
    """读取当前请求固定的简历与结构化资料，两者共同作为职业事实来源。"""
    resume_id = _CTX.get().get("resume_id")
    profile_version_id = _CTX.get().get("profile_version_id")
    store = _store()
    versions = store.list("resume_versions", resume_id=int(resume_id)) if resume_id else []
    resume = store.get("resumes", int(resume_id)) if resume_id else None
    parts = []
    if resume_id and not resume:
        parts.append("选择的上传简历不存在。")
    elif resume:
        facts = ""
        if versions:
            items = (versions[0].get("facts") or {}).get("facts") or []
            facts = "\n".join(f"- {row.get('text','')}" for row in items if row.get("text"))
        text = (resume.get("text") or "")[:12000]
        parts.extend([
            f"上传简历：{resume.get('filename')}",
            f"简历事实：\n{facts or '（尚无提取事实）'}",
            f"简历原文摘录：\n{text or '（无文本）'}",
        ])
    else:
        parts.append("上传简历：未选择")
    profile_version = (
        store.get("profile_versions", profile_version_id) if profile_version_id else None
    )
    profile_text = profile_context_text(profile_version["data"] if profile_version else {})
    parts.append(f"用户维护的结构化资料：\n{profile_text or '（尚未填写）'}")
    if not resume and not profile_text:
        return "当前没有可用的上传简历或职业资料库内容。请先选择一份简历，或到「我的资料」添加技能、经历、证书或补充资料。"
    return minimize_personal_data("\n\n".join(parts))


def read_career_profile() -> str:
    """读取当前请求固定版本的用户职业资料库；不读取上传简历。"""
    profile_version_id = _CTX.get().get("profile_version_id")
    profile_version = (
        _store().get("profile_versions", profile_version_id) if profile_version_id else None
    )
    text = profile_context_text(profile_version["data"] if profile_version else {})
    if not text:
        return "职业资料库还没有内容。请用户到「我的资料」添加技能、经历、证书或补充资料。"
    version = profile_version.get("version") if profile_version else "未知"
    return minimize_personal_data(f"职业资料库版本：v{version}\n\n{text}")


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,，、\n;]+", value or "") if part.strip()]


def list_found_jobs() -> str:
    """列出本次（或上一轮）搜索返回、助手当前可见的岗位。"""
    ids = list(_CTX.get().get("search_ids") or [])
    if not ids:
        return "还没有搜索结果。请先调用 search_jobs_online。"
    return _format_jobs(ids, limit=25)


def _format_jobs(snapshot_ids: list[int], limit: int = 25) -> str:
    store = _store()
    lines = []
    for snapshot_id in snapshot_ids[:limit]:
        item = store.get("job_snapshots", snapshot_id)
        if not item:
            continue
        job = store.get("jobs", item["job_uid"]) or {}
        snippet = re.sub(r"\s+", " ", (item.get("jd") or "")).strip()[:180]
        lines.append(
            f"#{snapshot_id} {item['title']} · {item['company'] or '公司未提供'} · {item['source']}"
            f" · {job.get('location') or '地点未提供'} · {job.get('salary_text') or '薪资未提供'}"
            f"{' · ' + snippet if snippet else ''}"
            f" · 详情 /jobs/{snapshot_id}"
        )
    extra = len(snapshot_ids) - limit
    if extra > 0:
        lines.append(f"另有 {extra} 条已保存，请到岗位页查看。")
    return "\n".join(lines) if lines else "没有可展示的岗位。"


def search_jobs_online(keywords: str, location: str = "Hong Kong",
                       job_types: str = "full time", sources: str = "linkedin,local") -> str:
    """按关键词搜索网上（LinkedIn/JobsDB/Indeed）和本地岗位，结果写入岗位页并可供推荐。请先读简历再填关键词，最多 7 个。"""
    store = _store()
    keys = [item.lower() for item in _split_csv(keywords)][:7]
    if not keys:
        return "请提供 1–7 个职位关键词（建议从简历方向提炼，如 data analyst）。"
    place = _LOCATIONS.get((location or "").strip().lower(), (location or "").strip() or "Hong Kong")
    types = [item.lower() for item in _split_csv(job_types)]
    types = [item for item in types if item in {"internship", "part time", "full time"}]
    if not types:
        types = ["full time"]
    allowed_sources = {"local", "linkedin", "jobsdb", "indeed"}
    chosen = [item.lower() for item in _split_csv(sources)]
    chosen = [item for item in chosen if item in allowed_sources]
    if not chosen:
        chosen = ["linkedin", "local"]
    data = dict(confirmed=True, keywords=keys, location=place, job_types=types, sources=chosen)
    preference_id = store.confirm_preferences(None, data)
    task_id = store.enqueue(
        "search", {"preference_id": preference_id},
        f"search:assistant:{uuid.uuid4().hex}", max_attempts=2,
    )
    deadline = time.time() + 120
    task = store.get("tasks", task_id)
    while task and task["status"] in {"queued", "running"} and time.time() < deadline:
        time.sleep(1.2)
        task = store.get("tasks", task_id)
    if not task:
        return "搜索任务丢失，请重试。"
    if task["status"] in {"queued", "running"}:
        return (
            f"搜索仍在进行（任务 #{task_id}）。请稍后到岗位页查看，或再问一次「上次搜索结果如何」。"
            "网上来源可能较慢或受配额限制。"
        )
    result = task.get("result") or {}
    ids = list(dict.fromkeys(int(i) for i in (result.get("snapshot_ids") or []) if str(i).isdigit() or isinstance(i, int)))
    _CTX.get()["search_ids"] = ids
    source_lines = []
    for name, row in (result.get("sources") or {}).items():
        status = {"succeeded": "成功", "failed": "失败"}.get(row.get("status"), row.get("status"))
        extra = row.get("error") or (f"条数 {row.get('count')}" if row.get("count") is not None else "")
        source_lines.append(f"- {name}：{status}{(' · ' + extra) if extra else ''}")
    head = [
        f"搜索{'完成' if task['status']=='succeeded' else task['status']}，保存 {len(ids)} 个岗位。关键词：{', '.join(keys)}。地区：{place}。",
        "来源：",
        *(source_lines or ["- （无来源结果）"]),
        "岗位列表：",
        _format_jobs(ids),
    ]
    if task.get("error"):
        head.append("说明：" + task["error"])
    return "\n".join(head)


def _model():
    key = os.environ.get("RADAR_AI_API_KEY", "").strip()
    base = os.environ.get("RADAR_AI_BASE_URL", "").strip()
    model = os.environ.get("RADAR_AI_MODEL", "").strip()
    if not key or not base or not model:
        raise AIError("AI 未配置：需要 RADAR_AI_API_KEY、RADAR_AI_BASE_URL 和 RADAR_AI_MODEL。")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AIError("RADAR_AI_BASE_URL 必须为 HTTP(S) 地址。")
    from langchain_openai import ChatOpenAI
    kwargs = dict(
        model=model, api_key=key, base_url=base, temperature=0,
        timeout=180, streaming=True, stream_usage=False,
    )
    if parsed.hostname == "api.deepseek.com":
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs)


def _agent():
    global _AGENT
    if _AGENT is None:
        from deepagents import create_deep_agent
        tools = [
            list_selected_jobs,
            list_found_jobs,
            read_selected_job,
            read_selected_resume,
            read_career_profile,
            search_jobs_online,
        ]
        _AGENT = create_deep_agent(
            model=_model(),
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            subagents=[
                {
                    "name": "match_analyst",
                    "description": "对照点选岗位或搜索结果与简历做匹配度分析。",
                    "system_prompt": "只用工具中的岗位、简历与结构化资料。不得编造。用中文给出匹配、缺口与建议。",
                    "tools": tools,
                },
                {
                    "name": "job_scout",
                    "description": "结合简历搜索网上岗位并给出推荐。",
                    "system_prompt": "先读简历再搜索。只推荐工具返回的岗位，说明与简历的匹配理由。不要编造在招职位。",
                    "tools": tools,
                },
            ],
            name="career-assistant",
        )
    return _AGENT


def _message_text(message) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text") or "")
            else:
                parts.append(getattr(item, "text", "") or "")
        return "".join(parts)
    return str(content or "")


_TOOL_STATUS = {
    "list_selected_jobs": "正在查看已勾选岗位…",
    "list_found_jobs": "正在整理搜索结果…",
    "read_selected_job": "正在读取岗位介绍…",
    "read_selected_resume": "正在读取简历…",
    "read_career_profile": "正在读取职业资料库…",
    "search_jobs_online": "正在搜索网上岗位，可能需要一两分钟…",
    "task": "正在调用子任务…",
}


def _tool_name(chunk) -> str:
    kind = getattr(chunk, "type", "") or ""
    if kind == "tool":
        return str(getattr(chunk, "name", "") or "")
    for item in getattr(chunk, "tool_call_chunks", None) or []:
        name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
        if name:
            return str(name)
    for item in getattr(chunk, "tool_calls", None) or []:
        name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
        if name:
            return str(name)
    return ""


def _wants_tailored_resumes(message: str) -> bool:
    text = re.sub(r"\s+", "", message or "")
    return any(key in text for key in ("针对性简历", "对应简历", "生成简历", "改写简历", "为每个勾选"))


def _boundary_refusal_text(
    code: str,
    detail: str,
    *,
    title: str = "",
) -> str:
    check = boundary_check(code)
    prefix = f"「{title}」" if title else ""
    return (
        f"\n\n{prefix}未生成：{check.level.label}"
        f"（{check.level.value.upper()} · {check.code}）。{detail}\n"
    )


def _generate_tailored_resumes():
    ids = sorted(_selected_ids())
    if not ids:
        yield {
            "type": "token",
            "text": _boundary_refusal_text(
                "preflight_jobs_missing",
                "请先在左侧勾选收藏岗位。未勾选的岗位不会保存。",
            ),
        }
        return
    store = _store()
    delivery = store.latest_delivery_profile()
    if delivery is None:
        yield {
            "type": "token",
            "text": _boundary_refusal_text(
                "preflight_delivery_missing",
                (
                    "请先到「我的资料 -> 投递信息」确认姓名。"
                    "姓名和联系方式只用于 PDF 页眉，不会发送给生成或审计模型。"
                ),
            ),
        }
        return

    context = _CTX.get()
    resume_id = context.get("resume_id")
    resume_version_id = context.get("resume_version_id")
    profile_version_id = context.get("profile_version_id")
    resume_facts: dict = {}
    if resume_id:
        resume_version = (
            store.get("resume_versions", resume_version_id)
            if resume_version_id
            else None
        )
        if resume_version:
            resume_facts = resume_version.get("facts") or {}
    profile_version = (
        store.get("profile_versions", profile_version_id)
        if profile_version_id
        else None
    )
    profile_data = normalize_profile_data(profile_version["data"] if profile_version else {})
    facts = merge_profile_facts(
        resume_facts,
        profile_data,
    )
    if not facts.get("facts"):
        yield {
            "type": "token",
            "text": _boundary_refusal_text(
                "preflight_facts_missing",
                (
                    "当前没有可用于生成简历的职业事实。"
                    "请上传简历，或到「我的资料」添加技能、经历、证书或补充资料。"
                ),
            ),
        }
        return

    fact_objects = fact_objects_from_facts(facts)
    known_fact_ids = {
        str(row.get("id") or "")
        for row in facts["facts"]
        if isinstance(row, dict) and row.get("id")
    }
    ai = AIService(timeout=120, attempts=2)
    pdf_dir = Path(store.path).parent / "course_resume_pdfs"
    for snapshot_id in ids:
        item = store.get("job_snapshots", snapshot_id) or {}
        title = item.get("title") or f"岗位 {snapshot_id}"
        jd = str(item.get("jd") or "").strip()
        stage = "planning"
        if contains_instruction_injection(jd):
            yield {
                "type": "token",
                "text": _boundary_refusal_text(
                    "input_instruction_detected",
                    (
                        "岗位描述包含疑似覆盖既有约束的指令文本。"
                        "该内容只会被当作不可信岗位文本处理，请清理后重新生成。"
                    ),
                    title=title,
                ),
            }
            continue
        yield {"type": "status", "text": f"正在为「{title}」拆解岗位要求并筛选证据…"}
        try:
            plan = ai.plan_resume(facts, jd, title)
            checks = [boundary_check("generation_passed")]
            selected_fact_ids = sorted(
                {
                    fact_id
                    for row in plan.get("evidence_map") or []
                    for fact_id in row.get("fact_ids") or []
                }
            )
            stage = "writing"
            yield {"type": "status", "text": f"正在按照岗位策略撰写「{title}」…"}
            draft = ai.write_resume(facts, jd, title, plan=plan)
            yield {"type": "status", "text": f"「{title}」正在进行事实、数字与日期校验…"}
            spec = validate_resume_spec(draft, facts)
            checks.append(boundary_check("writer_passed"))
            stage = "artifact"
            yield {"type": "status", "text": f"「{title}」正在生成 A4 PDF…"}
            pdf = render_resume_pdf(
                spec,
                delivery["data"],
                pdf_dir,
                avatar=profile_data["avatar"],
            )
            checks.append(
                boundary_check(
                    "artifact_rendered",
                    evidence_refs=(str(pdf["pdf_sha256"]),),
                )
            )
            stage = "audit"
            yield {"type": "status", "text": f"「{title}」正在由独立审计角色逐项核验…"}
            audit_raw = ai.audit_resume(facts, jd, title, spec)
            audit = validate_resume_audit(audit_raw, known_fact_ids)
            checks.append(
                boundary_check(
                    "audit_review_required" if not audit["approved"] else "audit_passed",
                    evidence_refs=selected_fact_ids,
                )
            )
            body = resume_plaintext(spec, delivery["data"])
            content_hash = resume_content_hash(spec)
            boundary = evaluate_boundaries(
                checks,
                fact_objects=fact_objects,
                subject={
                    "snapshot_id": snapshot_id,
                    "resume_version_id": resume_version_id,
                    "profile_version_id": profile_version_id,
                    "delivery_profile_id": delivery["id"],
                    "fact_count": len(facts["facts"]),
                    "selected_fact_count": len(selected_fact_ids),
                    "fact_ids": selected_fact_ids,
                    "content_sha256": content_hash,
                    "pdf_sha256": pdf["pdf_sha256"],
                },
            )
            record_id = store.save_tailored_resume(
                snapshot_id=snapshot_id,
                resume_id=resume_id,
                title=f"{title} · 针对性简历",
                body=body,
                resume_version_id=resume_version_id,
                profile_version_id=profile_version_id,
                delivery_profile_id=delivery["id"],
                document=spec,
                audit=audit,
                plan=plan,
                boundary=boundary.to_dict(),
                pdf_path=pdf["path"],
                pdf_sha256=pdf["pdf_sha256"],
                content_sha256=content_hash,
                template_version=pdf["template_version"],
                renderer_version=pdf["renderer_version"],
                page_count=pdf["page_count"],
                artifact_status="audit_pass" if audit["approved"] else "audit_issue",
            )
        except ResumeValidationError as exc:
            message = str(exc).strip() or type(exc).__name__
            code = exc.boundary_code or {
                "planning": "plan_generation_failed",
                "writing": "writer_generation_failed",
                "artifact": "pdf_render_failed",
                "audit": "audit_structure_invalid",
            }[stage]
            yield {"type": "token", "text": _boundary_refusal_text(code, message, title=title)}
            continue
        except (AIError, ResumeRenderError, ValueError) as exc:
            message = str(exc).strip() or type(exc).__name__
            code = {
                "planning": "plan_generation_failed",
                "writing": "writer_generation_failed",
                "artifact": "pdf_render_failed",
                "audit": "audit_structure_invalid",
            }[stage]
            yield {"type": "token", "text": _boundary_refusal_text(code, message, title=title)}
            continue
        except Exception as exc:
            code = {
                "planning": "plan_generation_failed",
                "writing": "writer_generation_failed",
                "artifact": "pdf_render_failed",
                "audit": "audit_structure_invalid",
            }[stage]
            yield {
                "type": "token",
                "text": _boundary_refusal_text(
                    code,
                    f"{type(exc).__name__}：{str(exc).strip() or '未知错误'}",
                    title=title,
                ),
            }
            continue

        audit_label = "独立审计通过" if audit["approved"] else "独立审计发现问题"
        refusal = boundary.level
        issue_lines = [
            f"- {row['title']}：{row['detail']}"
            for row in audit["findings"]
            if row["status"] == "issue"
        ]
        report = [
            f"\n\n## {title}\n",
            f"PDF 简历 #{record_id} 已生成，{audit_label}，共 {pdf['page_count']} 页。",
            (
                f"边界结论：{refusal.label}"
                f"（{refusal.value.upper()} · {boundary.policy_version}）"
            ),
            f"审计总结：{audit['summary']}",
        ]
        if issue_lines:
            report.append("需处理：\n" + "\n".join(issue_lines))
        report.append(
            f"查看与下载：/jobs/{snapshot_id}/resumes/{record_id}"
            "（首次下载前需要人工审批当前 PDF 版本。）"
        )
        yield {"type": "token", "text": "\n".join(report)}


def stream_assistant(store: Store, message: str, snapshot_ids: list[int], resume_id: int | None,
                     history: list[dict] | None = None, search_ids: list[int] | None = None):
    """Yield status/token/done/error events for one Deep Agent turn."""
    allowed = {item["snapshot_id"] for item in store.list_favorites()}
    visible = [int(i) for i in snapshot_ids if int(i) in allowed]
    found = [int(i) for i in (search_ids or [])]
    latest_profile = store.latest_profile_version()
    profile_version_id = latest_profile["id"] if latest_profile else None
    versions = store.list("resume_versions", resume_id=int(resume_id)) if resume_id else []
    resume_version_id = versions[0]["id"] if versions else None
    token = _CTX.set({
        "store": store,
        "snapshot_ids": visible,
        "resume_id": resume_id,
        "resume_version_id": resume_version_id,
        "profile_version_id": profile_version_id,
        "search_ids": found,
    })
    os.environ.setdefault("LANGSMITH_TRACING", "false")
    try:
        messages = list(history or [])
        messages.append({"role": "user", "content": message.strip()})
        yield {"type": "status", "text": "助手处理中…"}
        collected: list[str] = []
        last_status = ""
        if _wants_tailored_resumes(message.strip()):
            for event in _generate_tailored_resumes():
                if event.get("type") == "token" and event.get("text"):
                    collected.append(event["text"])
                yield event
            reply = "".join(collected).strip() or "（没有生成结果）"
            yield {"type": "done", "reply": reply, "found": list(_CTX.get().get("search_ids") or found)}
            return
        last_ai = ""
        for item in _agent().stream(
            {"messages": messages},
            {"recursion_limit": 50},
            stream_mode=["messages", "values"],
        ):
            mode, payload = item if isinstance(item, tuple) and len(item) == 2 else ("messages", item)
            if mode == "values":
                msgs = (payload or {}).get("messages") or []
                for row in reversed(msgs):
                    if getattr(row, "type", "") == "ai" and not getattr(row, "tool_calls", None):
                        text = _message_text(row).strip()
                        if text:
                            last_ai = text
                            break
                continue
            if isinstance(payload, tuple) and len(payload) == 2:
                chunk, _metadata = payload
            else:
                chunk = payload
            kind = getattr(chunk, "type", "") or ""
            name = _tool_name(chunk)
            if kind == "tool" or name:
                status = _TOOL_STATUS.get(name, "正在调用工具…")
                if status != last_status:
                    last_status = status
                    yield {"type": "status", "text": status}
                if kind == "tool":
                    text = _message_text(chunk).strip()
                    if text.startswith("已保存针对性简历"):
                        collected.append("\n" + text + "\n")
                        yield {"type": "token", "text": "\n" + text + "\n"}
                continue
            text = _message_text(chunk)
            if not text:
                continue
            collected.append(text)
            yield {"type": "token", "text": text}
        reply = "".join(collected).strip() or last_ai or "（模型没有返回文字）"
        yield {
            "type": "done",
            "reply": reply,
            "found": list(_CTX.get().get("search_ids") or found),
        }
    except AIError as exc:
        yield {"type": "error", "error": str(exc)}
    except Exception as exc:
        yield {"type": "error", "error": f"助手调用失败（{type(exc).__name__}）。请检查模型配置与网络。"}
    finally:
        _CTX.reset(token)


def run_assistant(store: Store, message: str, snapshot_ids: list[int], resume_id: int | None,
                  history: list[dict] | None = None, search_ids: list[int] | None = None) -> tuple[str, list[int]]:
    """Run one Deep Agent turn. Unchecked favorites stay hidden; search hits stay visible."""
    reply = ""
    found: list[int] = list(search_ids or [])
    for event in stream_assistant(store, message, snapshot_ids, resume_id, history, search_ids):
        if event.get("type") == "token":
            reply += event.get("text") or ""
        elif event.get("type") == "done":
            return event.get("reply") or reply or "（模型没有返回文字）", list(event.get("found") or found)
        elif event.get("type") == "error":
            raise AIError(event.get("error") or "助手调用失败。")
    return reply.strip() or "（模型没有返回文字）", found
