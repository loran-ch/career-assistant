"""Project-local resume skills loaded into the fixed generation pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import re


@dataclass(frozen=True)
class ResumeSkill:
    name: str
    description: str
    title: str
    instructions: str


RESUME_PLANNING_SKILLS = (
    "resume-job-brief",
    "resume-evidence-selection",
)
RESUME_WRITING_SKILLS = (
    "resume-evidence-selection",
    "resume-impact-writing",
    "resume-ats-quality",
)
RESUME_AUDIT_SKILLS = ("resume-independent-audit",)
ALL_RESUME_SKILLS = (
    "resume-job-brief",
    "resume-evidence-selection",
    "resume-impact-writing",
    "resume-ats-quality",
    "resume-independent-audit",
)


@lru_cache(maxsize=None)
def load_resume_skill(name: str) -> ResumeSkill:
    """Load and minimally validate one packaged SKILL.md document."""
    if name not in ALL_RESUME_SKILLS:
        raise KeyError(f"未知简历技能：{name}")
    resource = files("career_assistant").joinpath("skills", name, "SKILL.md")
    if not resource.is_file():
        raise RuntimeError(f"简历技能文件缺失：{name}")
    raw = resource.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise RuntimeError(f"简历技能缺少 frontmatter：{name}")
    parts = raw.split("---", 2)
    if len(parts) != 3:
        raise RuntimeError(f"简历技能 frontmatter 不完整：{name}")
    frontmatter, body = parts[1], parts[2].strip()
    declared_name = re.search(r"(?m)^name:\s*(\S+)\s*$", frontmatter)
    description = re.search(r"(?m)^description:\s*(.+?)\s*$", frontmatter)
    title = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    if not declared_name or declared_name.group(1) != name:
        raise RuntimeError(f"简历技能名称不一致：{name}")
    if not description or not title or not body:
        raise RuntimeError(f"简历技能元数据不完整：{name}")
    return ResumeSkill(
        name=name,
        description=description.group(1).strip(),
        title=title.group(1).strip(),
        instructions=body,
    )


def compose_resume_skills(names: tuple[str, ...]) -> str:
    """Render selected project skills for a fixed AI role."""
    sections = ["使用以下简历技能。技能是对本次输出的补充约束，不扩大事实来源。"]
    for index, name in enumerate(names, 1):
        skill = load_resume_skill(name)
        sections.append(
            f"\n## 技能 {index}：{skill.title}\n\n{skill.instructions}"
        )
    return "\n".join(sections)


def resume_skill_catalog() -> list[dict[str, str]]:
    """Return safe display metadata without loading instructions into callers."""
    return [
        {
            "name": name,
            "title": load_resume_skill(name).title,
            "description": load_resume_skill(name).description,
        }
        for name in ALL_RESUME_SKILLS
    ]
