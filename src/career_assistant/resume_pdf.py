"""Server-rendered, content-addressed PDF artifacts for structured resumes."""
from __future__ import annotations

import base64
from html import escape
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Iterable

from career_assistant.config import PROJECT_ROOT
from career_assistant.resume_document import TEMPLATE_VERSION


AVATAR_MAX_RENDER_BYTES = 5 * 1024 * 1024
RENDERER_VERSION = "playwright-chromium-a4-v2"


class ResumeRenderError(RuntimeError):
    """The trusted renderer could not produce a valid PDF."""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _period(start: str, end: str) -> str:
    if start and end:
        return f"{start} - {end}"
    if start:
        return f"{start} - 至今"
    return ""


def _items(label: str, values: Iterable[str]) -> str:
    rows = "".join(f"<li>{escape(value)}</li>" for value in values if value)
    return f'<ul aria-label="{escape(label)}">{rows}</ul>' if rows else ""


def _links(values: list[str]) -> str:
    if not values:
        return ""
    rows = []
    for value in values:
        safe = escape(value)
        rows.append(
            f'<a href="{safe}" rel="noopener noreferrer">{safe}</a>'
        )
    return '<span class="contact-links">' + " · ".join(rows) + "</span>"


def _avatar_data_url(avatar: dict | None) -> str:
    if not avatar:
        return ""
    path = Path(str(avatar.get("path") or "")).expanduser().resolve()
    if not path.is_file():
        raise ResumeRenderError("资料版本中的头像文件不存在，请重新上传头像")
    if path.stat().st_size > AVATAR_MAX_RENDER_BYTES:
        raise ResumeRenderError("资料版本中的头像文件超出大小限制，请重新上传头像")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != str(avatar.get("sha256") or ""):
        raise ResumeRenderError("资料版本中的头像文件哈希不一致，请重新上传头像")
    mime_type = str(avatar.get("mime_type") or "")
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ResumeRenderError("资料版本中的头像格式不受支持，请重新上传头像")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _render_skills(groups: list[dict]) -> str:
    if not groups:
        return ""
    rows = []
    for group in groups:
        items = []
        for item in group["items"]:
            items.append(f'<li><span>{escape(item["name"])}</span></li>')
        rows.append(
            '<div class="skill-group">'
            f'<h3>{escape(group["name"])}</h3>'
            f'<ul class="skills">{"".join(items)}</ul>'
            "</div>"
        )
    return '<section class="resume-section"><h2>技能</h2>' + "".join(rows) + "</section>"


def _render_experiences(items: list[dict]) -> str:
    if not items:
        return ""
    rows = []
    for item in items:
        heading = " · ".join(
            value
            for value in (escape(item["role"]), escape(item["organization"]))
            if value
        )
        meta = " · ".join(
            value
            for value in (
                escape(item["location"]),
                escape(_period(item["start_date"], item["end_date"])),
            )
            if value
        )
        rows.append(
            '<article class="timeline-item">'
            f'<div class="item-title"><h3>{heading}</h3>'
            f'<span>{meta}</span></div>'
            + _items("经历要点", (row["text"] for row in item["highlights"]))
            + "</article>"
        )
    return '<section class="resume-section"><h2>经历</h2>' + "".join(rows) + "</section>"


def _render_education(items: list[dict]) -> str:
    if not items:
        return ""
    rows = []
    for item in items:
        heading = " · ".join(
            value
            for value in (escape(item["institution"]), escape(item["degree"]))
            if value
        )
        meta = " · ".join(
            value
            for value in (
                escape(item["location"]),
                escape(_period(item["start_date"], item["end_date"])),
            )
            if value
        )
        rows.append(
            '<article class="timeline-item">'
            f'<div class="item-title"><h3>{heading}</h3>'
            f'<span>{meta}</span></div>'
            + _items("教育详情", (row["text"] for row in item["details"]))
            + "</article>"
        )
    return '<section class="resume-section"><h2>教育</h2>' + "".join(rows) + "</section>"


def _render_certificates(items: list[dict]) -> str:
    if not items:
        return ""
    rows = []
    for item in items:
        date = (
            f'<span class="certificate-date">{escape(item["issued_on"])}</span>'
            if item["issued_on"]
            else ""
        )
        rows.append(
            '<div class="certificate-row">'
            f'<div><strong>{escape(item["name"])}</strong>'
            f'<span>{escape(item["issuer"])}</span></div>{date}</div>'
        )
    return '<section class="resume-section"><h2>证书</h2>' + "".join(rows) + "</section>"


def _render_additional(items: list[dict]) -> str:
    if not items:
        return ""
    rows = []
    for item in items:
        rows.append(
            '<div class="additional-row">'
            f'<h3>{escape(item["title"])}</h3>'
            f'<p>{escape(item["text"])}</p>'
            "</div>"
        )
    return '<section class="resume-section"><h2>补充</h2>' + "".join(rows) + "</section>"


def resume_html(spec: dict, delivery: dict, avatar_data_url: str = "") -> str:
    contact = [
        escape(delivery.get("email") or ""),
        escape(delivery.get("phone") or ""),
        escape(delivery.get("location") or ""),
    ]
    contact_line = " · ".join(value for value in contact if value)
    if delivery.get("links"):
        contact_line += (" · " if contact_line else "") + _links(delivery["links"])
    body = "".join(
        (
            _render_skills(spec["skill_groups"]),
            _render_experiences(spec["experiences"]),
            _render_education(spec["education"]),
            _render_certificates(spec["certificates"]),
            _render_additional(spec["additional"]),
        )
    )
    avatar_class = " has-avatar" if avatar_data_url else ""
    avatar_markup = (
        f'<img class="resume-avatar" src="{escape(avatar_data_url)}" alt="">'
        if avatar_data_url
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<title>{escape(delivery["name"])} - {escape(spec["headline"])}</title>
<style>
@page {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #fff; color: #193243; }}
body {{
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", sans-serif;
  font-size: 10.2pt;
  line-height: 1.55;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}}
.resume-page {{ width: 210mm; min-height: 297mm; padding: 15mm 17mm 17mm; background: #fff; }}
.resume-header {{ border-bottom: 1.5px solid #1f5968; padding-bottom: 10px; }}
.resume-header.has-avatar {{ display: grid; grid-template-columns: minmax(0, 1fr) 24mm; gap: 8mm; align-items: center; }}
.resume-identity {{ min-width: 0; }}
.resume-avatar {{ display: block; width: 24mm; height: 32mm; object-fit: cover; border: 1px solid #b9cbd3; border-radius: 2px; }}
.resume-header h1 {{ margin: 0; color: #123c4a; font-size: 25pt; letter-spacing: 0; line-height: 1.15; }}
.headline {{ margin: 3px 0 0; color: #376170; font-size: 12.5pt; font-weight: 650; }}
.contact {{ margin-top: 7px; color: #4d6370; font-size: 9pt; overflow-wrap: anywhere; }}
.contact-links a {{ color: #1b5c6d; text-decoration: none; }}
.summary {{ margin: 13px 0 4px; color: #2d4656; }}
.resume-section {{ margin-top: 15px; break-inside: auto; }}
.resume-section > h2 {{
  margin: 0 0 7px; padding-bottom: 3px; border-bottom: 1px solid #b9cbd3;
  color: #143f4e; font-size: 11pt; text-transform: uppercase;
}}
.skill-group {{ display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 9px; margin-top: 5px; break-inside: avoid; }}
.skill-group h3, .additional-row h3 {{ margin: 0; color: #355666; font-size: 9.6pt; }}
.skills {{ display: flex; flex-wrap: wrap; gap: 5px 8px; margin: 0; padding: 0; list-style: none; }}
.skills li {{ display: inline-flex; align-items: center; gap: 4px; color: #233f4e; }}
.skills li + li::before {{ content: ""; width: 3px; height: 3px; margin-right: 3px; border-radius: 50%; background: #8aa5b0; }}
.timeline-item {{ padding: 7px 0 8px; border-bottom: 1px solid #e2eaee; break-inside: avoid; }}
.timeline-item:last-child {{ border-bottom: 0; }}
.item-title {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }}
.item-title h3 {{ margin: 0; color: #223f4e; font-size: 10.6pt; }}
.item-title span {{ flex: none; max-width: 43%; color: #647985; font-size: 8.8pt; text-align: right; }}
.timeline-item ul {{ margin: 5px 0 0; padding-left: 17px; color: #304b5b; }}
.timeline-item li {{ margin: 2px 0; }}
.certificate-row {{ display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; border-bottom: 1px solid #edf1f3; break-inside: avoid; }}
.certificate-row>div {{ display: flex; gap: 7px; flex-wrap: wrap; }}
.certificate-row strong {{ color: #263f4e; }}
.certificate-row span {{ color: #627783; }}
.certificate-date {{ flex: none; }}
.additional-row {{ display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 12px; padding: 5px 0; break-inside: avoid; }}
.additional-row p {{ margin: 0; color: #334d5c; }}
a {{ overflow-wrap: anywhere; }}
@media print {{ .timeline-item, .skill-group, .certificate-row, .additional-row {{ break-inside: avoid; }} }}
</style>
</head>
<body>
<main class="resume-page">
  <header class="resume-header{avatar_class}">
    <div class="resume-identity">
      <h1>{escape(delivery["name"])}</h1>
      <p class="headline">{escape(spec["headline"])}</p>
      <div class="contact">{contact_line}</div>
    </div>
    {avatar_markup}
  </header>
  <p class="summary">{escape(spec["summary"]["text"])}</p>
  {body}
</main>
</body>
</html>"""


def _browser_path() -> Path | None:
    root = PROJECT_ROOT / ".playwright"
    matches = sorted(root.glob("chromium-*/chrome-win64/chrome.exe"))
    return matches[-1] if matches else None


def render_resume_pdf(
    spec: dict,
    delivery: dict,
    output_dir: str | Path,
    avatar: dict | None = None,
) -> dict:
    """Render a verified PDF and atomically publish it under its content hash."""
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    browser_path = _browser_path()
    tmp_path: Path | None = None
    try:
        from playwright.sync_api import sync_playwright

        avatar_data_url = _avatar_data_url(avatar)
        with tempfile.NamedTemporaryFile(
            prefix=".resume-",
            suffix=".pdf",
            dir=destination,
            delete=False,
        ) as stream:
            tmp_path = Path(stream.name)
        with sync_playwright() as playwright:
            launch = {"headless": True}
            if browser_path:
                launch["executable_path"] = str(browser_path)
            browser = playwright.chromium.launch(**launch)
            page = browser.new_page(viewport={"width": 1240, "height": 1754})
            page.set_content(resume_html(spec, delivery, avatar_data_url), wait_until="networkidle")
            page.pdf(
                path=str(tmp_path),
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
            )
            browser.close()
        data = tmp_path.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise ResumeRenderError("PDF 渲染结果缺少 PDF 文件头")
        from pypdf import PdfReader

        reader = PdfReader(str(tmp_path))
        page_count = len(reader.pages)
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        if page_count < 1 or len(text) < 20:
            raise ResumeRenderError("PDF 渲染结果为空或无法提取文字")
        if delivery["name"] not in text:
            raise ResumeRenderError("PDF 未正确呈现投递姓名")
        digest = hashlib.sha256(data).hexdigest()
        final_path = destination / f"{digest[:20]}.pdf"
        if final_path.exists():
            if file_sha256(final_path) != digest:
                raise ResumeRenderError("同名 PDF 产物哈希冲突")
        else:
            os.replace(tmp_path, final_path)
            tmp_path = None
            final_path.chmod(0o600)
        if file_sha256(final_path) != digest:
            raise ResumeRenderError("PDF 落盘后哈希校验失败")
        return {
            "path": str(final_path),
            "pdf_sha256": digest,
            "page_count": page_count,
            "template_version": TEMPLATE_VERSION,
            "renderer_version": RENDERER_VERSION,
        }
    except ResumeRenderError:
        raise
    except Exception as exc:
        raise ResumeRenderError(
            "PDF 渲染失败；请确认 Playwright Chromium 已安装且中文字体可用。"
        ) from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
