"""本机岗位数据：读取 data/sample_data_analyst_jobs.json，不打开浏览器。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from career_assistant.config import DATA_DIR, PROJECT_ROOT
from career_assistant.models import JobPosting, parse_iso
from career_assistant.scrapers.base import BaseScraper

SAMPLE_NAME = "sample_data_analyst_jobs.json"


def sample_paths() -> list[Path]:
    return [DATA_DIR / SAMPLE_NAME, PROJECT_ROOT / "data" / SAMPLE_NAME]


class LocalScraper(BaseScraper):
    """本地样例岗位，供无网络或外部来源失败时仍能列出结果。"""

    source_name = "local"
    pages_per_keyword = 0

    def _do_fetch(self, last_seen_cutoff: datetime) -> list[JobPosting]:
        path = next((p for p in sample_paths() if p.is_file()), None)
        if path is None:
            raise RuntimeError(
                f"未找到本地岗位文件 {SAMPLE_NAME}；请放在 data/ 目录后重试。"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise RuntimeError(f"{path} 应为岗位对象数组。")
        jobs: list[JobPosting] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            job = self._from_record(item)
            if job is None or job.external_id in seen:
                continue
            seen.add(job.external_id)
            jobs.append(job)
        logger.info(f"[local] 从 {path.name} 读取 {len(jobs)} 个岗位")
        return jobs

    def _from_record(self, item: dict) -> Optional[JobPosting]:
        title = str(item.get("title") or "").strip()
        external_id = str(item.get("external_id") or "").strip()
        if not title or not external_id:
            return None
        url = str(item.get("url") or "").strip()
        normalized = str(item.get("url_normalized") or url).strip()
        return JobPosting(
            source=self.source_name,
            external_id=external_id,
            title=title,
            company=str(item.get("company") or "（公司未标注）").strip() or "（公司未标注）",
            url=url,
            url_normalized=normalized or f"local:{external_id}",
            salary_text=str(item.get("salary_text") or ""),
            location=str(item.get("location") or self.config.location),
            posted_at=None,
            description_snippet=str(item.get("description_snippet") or "")[:500],
            keyword_matched=str(item.get("keyword_matched") or "local"),
            direct_apply=bool(item.get("direct_apply")),
        )

    def _extract_posted_at(self, raw: str) -> Optional[datetime]:
        return parse_iso(raw)
