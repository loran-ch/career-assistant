"""Local Deep Agent for job matching and resume drafts.

The model never receives unselected favorites. Job and resume text is loaded
only through tools that check the current request's selected snapshot IDs.
"""
from __future__ import annotations

import contextvars
import os
import re
import time
import uuid
from urllib.parse import urlparse

from career_assistant.ai import AIError, minimize_personal_data
from career_assistant.store import Store

_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("assistant_ctx")
_AGENT = None

SYSTEM_PROMPT = """你是 Career Assistant 里的求职 Deep Agent。
必须遵守：
1. 收藏岗位只能通过工具读取。用户未点选的收藏岗位你看不到，禁止猜测其 JD。本次搜索返回的岗位可以用 list_found_jobs / read_selected_job 查看。
2. 分析或改写已收藏岗位时，若 list_selected_jobs 为空，请用户先在左侧勾选。
3. 简历只能通过 read_selected_resume 读取；没有简历时请用户上传或在本页选择。
4. 不得编造经历、技能、学历、成果数字、公司或在招职位。缺信息标为未知或省略。
5. 批量生成简历：对每个点选收藏岗位调用 save_tailored_resume。搜索到但未收藏点选的岗位不能保存针对性简历，请用户先收藏并勾选。
6. 用户要找网上岗位或要推荐时：先 read_selected_resume，从简历提炼至多 7 个职位关键词（优先与简历方向一致的英文岗位名），再 search_jobs_online。只根据工具返回的岗位推荐，对照简历说明匹配理由、缺口与优先投递顺序。可建议打开 /jobs 查看完整列表并收藏。
7. 用中文回答，结构清楚。
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
    """读取用户当前选择的简历原文与已保存事实。未选择则无法读取。"""
    resume_id = _CTX.get().get("resume_id")
    store = _store()
    if not resume_id:
        return "用户未选择简历。请先在本页选择或到「我的资料」上传。"
    resume = store.get("resumes", int(resume_id))
    if not resume:
        return "简历不存在。"
    versions = store.list("resume_versions", resume_id=int(resume_id))
    facts = ""
    if versions:
        items = (versions[0].get("facts") or {}).get("facts") or []
        facts = "\n".join(f"- {row.get('text','')}" for row in items if row.get("text"))
    text = (resume.get("text") or "")[:12000]
    return minimize_personal_data(
        f"简历文件：{resume.get('filename')}\n\n事实：\n{facts or '（尚无提取事实）'}\n\n原文摘录：\n{text or '（无文本）'}"
    )


def save_tailored_resume(snapshot_id: int, title: str, body: str) -> str:
    """为已点选的某一个岗位保存一版针对性简历。批量生成时每个岗位各调用一次。保存后可在该岗位详情页查看和下载。"""
    if snapshot_id not in _selected_ids():
        return f"岗位 #{snapshot_id} 未点选，不能保存针对性简历。"
    resume_id = _CTX.get().get("resume_id")
    try:
        record_id = _store().save_tailored_resume(snapshot_id, resume_id, title, body)
    except ValueError as exc:
        return str(exc)
    item = _store().get("job_snapshots", snapshot_id) or {}
    return (
        f"已保存针对性简历 #{record_id}，对应 {item.get('title') or snapshot_id}。"
        f"请到岗位页 /jobs/{snapshot_id} 查看和下载。"
    )


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
        tools = [list_selected_jobs, list_found_jobs, read_selected_job, read_selected_resume,
                 save_tailored_resume, search_jobs_online]
        _AGENT = create_deep_agent(
            model=_model(),
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            subagents=[
                {
                    "name": "match_analyst",
                    "description": "对照点选岗位或搜索结果与简历做匹配度分析。",
                    "system_prompt": "只用工具中的岗位与简历。不得编造。用中文给出匹配、缺口与建议。",
                    "tools": tools,
                },
                {
                    "name": "resume_writer",
                    "description": "按用户要求为每个点选岗位改写并保存针对性简历。",
                    "system_prompt": "只用简历原文里有的经历。每个点选岗位单独生成并调用 save_tailored_resume。不得编造技能或成果。",
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
    "save_tailored_resume": "正在保存针对性简历…",
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


_RESUME_SYS = """你根据对照简历，为单一岗位写针对性简历正文（Markdown）。
只用简历里出现的经历、技能、学历和成果。不得编造公司、数字、职级或项目。
岗位介绍缺失时，按职位名称调整措辞，不要假装见过官方 JD。
直接输出完整简历正文，不要前言后语。中文为主，专有名词可保留英文。"""


def _generate_tailored_resumes():
    ids = sorted(_selected_ids())
    if not ids:
        yield {"type": "token", "text": "请先在左侧勾选收藏岗位，再让我生成针对性简历。未勾选的岗位不会保存。"}
        return
    resume = read_selected_resume()
    if resume.startswith("用户未选择") or resume.startswith("简历不存在"):
        yield {"type": "token", "text": resume + "\n生成针对性简历需要先在本页选择或到「我的资料」上传简历。"}
        return
    from langchain_core.messages import HumanMessage, SystemMessage
    model = _model()
    for snapshot_id in ids:
        item = _store().get("job_snapshots", snapshot_id) or {}
        title = item.get("title") or f"岗位 {snapshot_id}"
        yield {"type": "status", "text": f"正在为「{title}」生成针对性简历…"}
        yield {"type": "token", "text": f"\n\n## {title}\n\n"}
        job = read_selected_job(snapshot_id)
        parts: list[str] = []
        try:
            for chunk in model.stream([
                SystemMessage(content=_RESUME_SYS),
                HumanMessage(content=f"对照简历：\n{resume}\n\n目标岗位：\n{job}\n"),
            ]):
                text = _message_text(chunk)
                if text:
                    parts.append(text)
                    yield {"type": "token", "text": text}
        except Exception as exc:
            yield {"type": "token", "text": f"\n生成失败（{type(exc).__name__}）。\n"}
            continue
        result = save_tailored_resume(snapshot_id, f"{title} · 针对性简历", "".join(parts))
        yield {"type": "token", "text": f"\n\n{result}\n"}


def stream_assistant(store: Store, message: str, snapshot_ids: list[int], resume_id: int | None,
                     history: list[dict] | None = None, search_ids: list[int] | None = None):
    """Yield status/token/done/error events for one Deep Agent turn."""
    allowed = {item["snapshot_id"] for item in store.list_favorites()}
    visible = [int(i) for i in snapshot_ids if int(i) in allowed]
    found = [int(i) for i in (search_ids or [])]
    token = _CTX.set({"store": store, "snapshot_ids": visible, "resume_id": resume_id, "search_ids": found})
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
