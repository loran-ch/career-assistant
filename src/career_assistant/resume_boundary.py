"""Fact objects, policy rules, and action-scoped refusal for resumes."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Iterable, Mapping


BOUNDARY_POLICY_VERSION = "resume-boundary-v2"


class RefusalLevel(str, Enum):
    """Ordered refusal levels shared by every resume rule."""

    ALLOW = "allow"
    WARN = "warn"
    REVIEW = "review"
    BLOCK = "block"

    @property
    def rank(self) -> int:
        return {
            self.ALLOW: 0,
            self.WARN: 1,
            self.REVIEW: 2,
            self.BLOCK: 3,
        }[self]

    @property
    def label(self) -> str:
        return {
            self.ALLOW: "允许",
            self.WARN: "提醒",
            self.REVIEW: "需人工复核",
            self.BLOCK: "禁止",
        }[self]

    @classmethod
    def highest(cls, levels: Iterable["RefusalLevel"]) -> "RefusalLevel":
        return max(levels, key=lambda level: level.rank, default=cls.ALLOW)


class BoundaryStage(str, Enum):
    PREFLIGHT = "preflight"
    PLANNING = "planning"
    WRITING = "writing"
    AUDIT = "audit"
    ARTIFACT = "artifact"
    APPROVAL = "approval"
    EXPORT = "export"

    @property
    def label(self) -> str:
        return {
            self.PREFLIGHT: "生成前检查",
            self.PLANNING: "岗位规划",
            self.WRITING: "简历撰写",
            self.AUDIT: "独立审计",
            self.ARTIFACT: "文件产物",
            self.APPROVAL: "人工审批",
            self.EXPORT: "导出下载",
        }[self]


class BoundaryAction(str, Enum):
    GENERATE = "generate"
    RENDER = "render"
    SAVE = "save"
    PREVIEW = "preview"
    APPROVE = "approve"
    EXPORT = "export"

    @property
    def label(self) -> str:
        return {
            self.GENERATE: "生成草稿",
            self.RENDER: "渲染文件",
            self.SAVE: "保存版本",
            self.PREVIEW: "预览文件",
            self.APPROVE: "人工审批",
            self.EXPORT: "导出下载",
        }[self]


class RuleGroup(str, Enum):
    FACT = "fact"
    SEMANTIC = "semantic"
    PERMISSION = "permission"
    FORMAT = "format"
    INPUT = "input"
    SYSTEM = "system"

    @property
    def label(self) -> str:
        return {
            self.FACT: "事实真实性",
            self.SEMANTIC: "语义与职责",
            self.PERMISSION: "权限审批",
            self.FORMAT: "格式完整性",
            self.INPUT: "输入污染",
            self.SYSTEM: "系统校验",
        }[self]


class FactCriticality(str, Enum):
    CRITICAL = "critical"
    MATERIAL = "material"
    SUPPORTING = "supporting"


class FactTrust(str, Enum):
    USER_CONFIRMED = "user_confirmed"
    IMPORTED = "imported"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class FactObject:
    """Minimal provenance record for one fact from the career vault."""

    fact_id: str
    claim_type: str
    criticality: FactCriticality
    source: str
    trust: FactTrust
    valid_from: str = ""
    valid_to: str = ""

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "claim_type": self.claim_type,
            "criticality": self.criticality.value,
            "source": self.source,
            "trust": self.trust.value,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "FactObject":
        if not isinstance(value, dict):
            raise ValueError("事实对象必须是对象")
        fact_id = str(value.get("fact_id") or "").strip()
        claim_type = str(value.get("claim_type") or "").strip()
        source = str(value.get("source") or "").strip()
        if not fact_id or not claim_type or not source:
            raise ValueError("事实对象字段无效")
        try:
            criticality = FactCriticality(value.get("criticality"))
            trust = FactTrust(value.get("trust"))
        except ValueError as exc:
            raise ValueError("事实对象等级无效") from exc
        return cls(
            fact_id=fact_id,
            claim_type=claim_type,
            criticality=criticality,
            source=source,
            trust=trust,
            valid_from=str(value.get("valid_from") or ""),
            valid_to=str(value.get("valid_to") or ""),
        )


_FACT_TYPE_ALIASES = {
    "profile_skill": "skill",
    "skill": "skill",
    "profile_experience": "experience",
    "experience": "experience",
    "work": "experience",
    "internship": "experience",
    "project": "project",
    "profile_education": "education",
    "education": "education",
    "profile_certificate": "certificate",
    "certificate": "certificate",
    "profile_additional": "additional",
    "additional": "additional",
    "user_provided": "additional",
}
_CRITICAL_FACT_TYPES = {"experience", "education", "certificate"}
_MATERIAL_FACT_TYPES = {"project", "skill", "additional"}
_PERIOD_PATTERN = re.compile(
    r"(?<!\d)(?:19|20)\d{2}-(?:0[1-9]|1[0-2])(?:-\d{2})?(?!\d)"
)
_INJECTION_PATTERNS = (
    re.compile(r"忽略(?:之前|以上|前面).{0,12}(?:指令|要求|规则)", re.IGNORECASE),
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above).{0,20}instructions", re.IGNORECASE),
    re.compile(r"(?:system|developer)\s+prompt", re.IGNORECASE),
    re.compile(r"不要(?:遵守|执行).{0,12}(?:指令|要求|规则)"),
)


def _fact_kind(row: Mapping[str, Any]) -> str:
    raw = str(row.get("category") or row.get("type") or "").strip().lower()
    return _FACT_TYPE_ALIASES.get(raw, raw or "additional")


def _fact_trust(row: Mapping[str, Any]) -> FactTrust:
    if row.get("verified") is True:
        return FactTrust.USER_CONFIRMED
    origin = str(row.get("origin") or "").strip().lower()
    category = str(row.get("category") or "").strip().lower()
    if origin in {"profile", "user_provided"} or category == "user_provided":
        return FactTrust.USER_CONFIRMED
    if origin in {"resume", "imported", "upload"}:
        return FactTrust.IMPORTED
    return FactTrust.UNVERIFIED


def _fact_period(row: Mapping[str, Any]) -> tuple[str, str]:
    valid_from = str(row.get("start_date") or row.get("issued_on") or "").strip()
    valid_to = str(row.get("end_date") or row.get("expires_on") or "").strip()
    if valid_from or valid_to:
        return valid_from, valid_to
    text = str(row.get("text") or row.get("evidence") or "")
    matches = _PERIOD_PATTERN.findall(text)
    if not matches:
        return "", ""
    return matches[0], matches[1] if len(matches) > 1 else ""


def fact_objects_from_facts(value: Any) -> tuple[FactObject, ...]:
    """Build lightweight provenance objects without duplicating fact text."""

    rows = value.get("facts") if isinstance(value, Mapping) else value
    if not isinstance(rows, list):
        return ()
    result: list[FactObject] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        fact_id = str(row.get("id") or "").strip()
        if not fact_id or fact_id in seen:
            continue
        claim_type = _fact_kind(row)
        criticality = (
            FactCriticality.CRITICAL
            if claim_type in _CRITICAL_FACT_TYPES
            else FactCriticality.MATERIAL
            if claim_type in _MATERIAL_FACT_TYPES
            else FactCriticality.SUPPORTING
        )
        valid_from, valid_to = _fact_period(row)
        result.append(
            FactObject(
                fact_id=fact_id,
                claim_type=claim_type,
                criticality=criticality,
                source=str(
                    row.get("origin")
                    or row.get("source")
                    or row.get("category")
                    or "career_profile"
                ),
                trust=_fact_trust(row),
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )
        seen.add(fact_id)
    return tuple(result)


def contains_instruction_injection(value: Any) -> bool:
    """Detect instruction-like text that must remain untrusted job context."""

    text = str(value or "")
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


@dataclass(frozen=True)
class BoundaryRule:
    code: str
    group: RuleGroup
    stage: BoundaryStage
    level: RefusalLevel
    summary: str
    affected_actions: frozenset[BoundaryAction]
    recoverable: bool
    remediation: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "affected_actions",
            frozenset(self.affected_actions),
        )


def _rule(
    code: str,
    group: RuleGroup,
    stage: BoundaryStage,
    level: RefusalLevel,
    summary: str,
    affected_actions: Iterable[BoundaryAction],
    recoverable: bool,
    remediation: str,
) -> BoundaryRule:
    return BoundaryRule(
        code=code,
        group=group,
        stage=stage,
        level=level,
        summary=summary,
        affected_actions=frozenset(affected_actions),
        recoverable=recoverable,
        remediation=remediation,
    )


BOUNDARY_RULES: dict[str, BoundaryRule] = {
    rule.code: rule
    for rule in (
        _rule(
            "preflight_jobs_missing",
            RuleGroup.INPUT,
            BoundaryStage.PREFLIGHT,
            RefusalLevel.BLOCK,
            "没有选择任何已收藏岗位",
            (BoundaryAction.GENERATE,),
            True,
            "先勾选至少一个收藏岗位。",
        ),
        _rule(
            "preflight_delivery_missing",
            RuleGroup.INPUT,
            BoundaryStage.PREFLIGHT,
            RefusalLevel.BLOCK,
            "尚未确认投递信息",
            (BoundaryAction.GENERATE,),
            True,
            "到「我的资料 -> 投递信息」确认姓名和联系方式。",
        ),
        _rule(
            "preflight_facts_missing",
            RuleGroup.FACT,
            BoundaryStage.PREFLIGHT,
            RefusalLevel.BLOCK,
            "职业资料库没有可用于生成简历的事实",
            (BoundaryAction.GENERATE,),
            True,
            "上传简历或补充技能、经历、证书等职业事实。",
        ),
        _rule(
            "input_instruction_detected",
            RuleGroup.INPUT,
            BoundaryStage.PLANNING,
            RefusalLevel.BLOCK,
            "岗位描述包含疑似指令注入内容",
            (BoundaryAction.GENERATE,),
            True,
            "该段岗位描述只可按普通文本引用，请清理后重新生成。",
        ),
        _rule(
            "plan_generation_failed",
            RuleGroup.SYSTEM,
            BoundaryStage.PLANNING,
            RefusalLevel.BLOCK,
            "岗位规划生成或确定性校验未通过",
            (BoundaryAction.GENERATE,),
            True,
            "检查岗位描述和职业事实后重新生成岗位规划。",
        ),
        _rule(
            "writer_generation_failed",
            RuleGroup.SYSTEM,
            BoundaryStage.WRITING,
            RefusalLevel.BLOCK,
            "结构化简历生成或确定性校验未通过",
            (
                BoundaryAction.RENDER,
                BoundaryAction.SAVE,
                BoundaryAction.APPROVE,
                BoundaryAction.EXPORT,
            ),
            True,
            "按校验错误修正事实引用、数字、日期或职责表述后重新生成。",
        ),
        _rule(
            "unsupported_fact_reference",
            RuleGroup.FACT,
            BoundaryStage.WRITING,
            RefusalLevel.BLOCK,
            "简历引用了职业资料库中不存在的事实",
            (
                BoundaryAction.RENDER,
                BoundaryAction.SAVE,
                BoundaryAction.APPROVE,
                BoundaryAction.EXPORT,
            ),
            True,
            "删除未知事实引用，或先在职业资料库中补充该事实。",
        ),
        _rule(
            "unsupported_numeric_claim",
            RuleGroup.FACT,
            BoundaryStage.WRITING,
            RefusalLevel.BLOCK,
            "简历包含职业资料库无法支撑的数字或日期",
            (
                BoundaryAction.RENDER,
                BoundaryAction.SAVE,
                BoundaryAction.APPROVE,
                BoundaryAction.EXPORT,
            ),
            True,
            "恢复职业资料库原值，或先补充可核实的数字和日期。",
        ),
        _rule(
            "ownership_overclaim",
            RuleGroup.SEMANTIC,
            BoundaryStage.WRITING,
            RefusalLevel.BLOCK,
            "简历把参与性职责扩大为独立完成或主导",
            (
                BoundaryAction.RENDER,
                BoundaryAction.SAVE,
                BoundaryAction.APPROVE,
                BoundaryAction.EXPORT,
            ),
            True,
            "把职责表述降回事实库支持的范围，或补充新的授权事实。",
        ),
        _rule(
            "pdf_render_failed",
            RuleGroup.FORMAT,
            BoundaryStage.ARTIFACT,
            RefusalLevel.BLOCK,
            "PDF 渲染失败",
            (BoundaryAction.SAVE,),
            True,
            "检查模板和渲染器错误后重新渲染。",
        ),
        _rule(
            "ats_layout_invalid",
            RuleGroup.FORMAT,
            BoundaryStage.ARTIFACT,
            RefusalLevel.BLOCK,
            "简历布局不满足可解析格式要求",
            (BoundaryAction.RENDER, BoundaryAction.SAVE),
            True,
            "改用标准单栏模板并移除不可解析结构。",
        ),
        _rule(
            "audit_structure_invalid",
            RuleGroup.SYSTEM,
            BoundaryStage.AUDIT,
            RefusalLevel.BLOCK,
            "独立审计结构或结论一致性校验未通过",
            (
                BoundaryAction.SAVE,
                BoundaryAction.APPROVE,
                BoundaryAction.EXPORT,
            ),
            True,
            "重新执行独立审计，直到逐项结果和总结论一致。",
        ),
        _rule(
            "boundary_record_invalid",
            RuleGroup.SYSTEM,
            BoundaryStage.AUDIT,
            RefusalLevel.BLOCK,
            "保存的边界评估无法解析",
            (
                BoundaryAction.PREVIEW,
                BoundaryAction.APPROVE,
                BoundaryAction.EXPORT,
            ),
            False,
            "重新生成该版本的边界记录。",
        ),
        _rule(
            "audit_review_required",
            RuleGroup.SEMANTIC,
            BoundaryStage.AUDIT,
            RefusalLevel.REVIEW,
            "独立审计发现需要人工判断的问题",
            (BoundaryAction.EXPORT,),
            True,
            "用户核对审计问题并显式审批当前文件版本后才可导出。",
        ),
        _rule(
            "material_unreviewed",
            RuleGroup.SEMANTIC,
            BoundaryStage.AUDIT,
            RefusalLevel.REVIEW,
            "求职材料尚未完成 AI 审阅或人工确认",
            (BoundaryAction.EXPORT,),
            True,
            "完成审阅并人工审批当前内容后才能导出。",
        ),
        _rule(
            "generation_passed",
            RuleGroup.FACT,
            BoundaryStage.PLANNING,
            RefusalLevel.ALLOW,
            "岗位规划通过事实引用检查",
            (),
            False,
            "",
        ),
        _rule(
            "writer_passed",
            RuleGroup.FACT,
            BoundaryStage.WRITING,
            RefusalLevel.ALLOW,
            "结构化简历通过事实、数字与职责检查",
            (),
            False,
            "",
        ),
        _rule(
            "artifact_rendered",
            RuleGroup.FORMAT,
            BoundaryStage.ARTIFACT,
            RefusalLevel.ALLOW,
            "PDF 已生成并记录内容与文件指纹",
            (),
            False,
            "",
        ),
        _rule(
            "audit_passed",
            RuleGroup.SEMANTIC,
            BoundaryStage.AUDIT,
            RefusalLevel.ALLOW,
            "独立审计逐项通过",
            (),
            False,
            "",
        ),
        _rule(
            "legacy_unaudited",
            RuleGroup.SEMANTIC,
            BoundaryStage.AUDIT,
            RefusalLevel.REVIEW,
            "历史文件没有独立审计或结构化事实映射",
            (BoundaryAction.EXPORT,),
            False,
            "仅可人工审查后导出；该记录不能证明事实准确性。",
        ),
        _rule(
            "artifact_integrity_failed",
            RuleGroup.FORMAT,
            BoundaryStage.ARTIFACT,
            RefusalLevel.BLOCK,
            "PDF 路径、文件或指纹完整性检查未通过",
            (
                BoundaryAction.PREVIEW,
                BoundaryAction.APPROVE,
                BoundaryAction.EXPORT,
            ),
            False,
            "不要使用当前文件，重新生成或恢复受控 PDF 产物。",
        ),
        _rule(
            "approval_not_confirmed",
            RuleGroup.PERMISSION,
            BoundaryStage.APPROVAL,
            RefusalLevel.BLOCK,
            "人工审批未进行显式确认",
            (BoundaryAction.APPROVE,),
            True,
            "核对当前版本、来源和审计结果后勾选确认。",
        ),
        _rule(
            "approval_hash_mismatch",
            RuleGroup.PERMISSION,
            BoundaryStage.APPROVAL,
            RefusalLevel.BLOCK,
            "提交审批的文件指纹与当前版本不一致",
            (BoundaryAction.APPROVE, BoundaryAction.EXPORT),
            True,
            "刷新审批页并重新核对当前文件版本。",
        ),
        _rule(
            "export_not_approved",
            RuleGroup.PERMISSION,
            BoundaryStage.EXPORT,
            RefusalLevel.BLOCK,
            "当前文件版本尚未完成人工审批",
            (BoundaryAction.EXPORT,),
            True,
            "先完成当前文件版本的人工审批。",
        ),
        _rule(
            "export_hash_mismatch",
            RuleGroup.PERMISSION,
            BoundaryStage.EXPORT,
            RefusalLevel.BLOCK,
            "导出前文件指纹与审批记录不一致",
            (BoundaryAction.EXPORT,),
            True,
            "重新审批当前文件版本。",
        ),
    )
}


@dataclass(frozen=True)
class BoundaryCheck:
    code: str
    group: RuleGroup
    stage: BoundaryStage
    level: RefusalLevel
    summary: str
    affected_actions: frozenset[BoundaryAction]
    recoverable: bool
    remediation: str
    fact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "group": self.group.value,
            "group_label": self.group.label,
            "stage": self.stage.value,
            "stage_label": self.stage.label,
            "level": self.level.value,
            "level_label": self.level.label,
            "summary": self.summary,
            "affected_actions": sorted(
                action.value for action in self.affected_actions
            ),
            "affected_action_labels": [
                action.label
                for action in BoundaryAction
                if action in self.affected_actions
            ],
            "recoverable": self.recoverable,
            "remediation": self.remediation,
            "fact_refs": list(self.fact_refs),
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "BoundaryCheck":
        if not isinstance(value, dict):
            raise ValueError("边界检查必须是对象")
        code = str(value.get("code") or "").strip()
        rule = BOUNDARY_RULES.get(code)
        if rule is None:
            raise ValueError(f"未知边界规则：{code or '空值'}")
        try:
            group = RuleGroup(value.get("group") or rule.group.value)
            stage = BoundaryStage(value.get("stage") or rule.stage.value)
            level = RefusalLevel(value.get("level") or rule.level.value)
        except ValueError as exc:
            raise ValueError("边界检查等级无效") from exc
        raw_actions = value.get("affected_actions")
        if raw_actions is None:
            actions = rule.affected_actions
        elif isinstance(raw_actions, list):
            try:
                actions = frozenset(BoundaryAction(item) for item in raw_actions)
            except (TypeError, ValueError) as exc:
                raise ValueError("边界检查动作无效") from exc
        else:
            raise ValueError("边界检查动作必须为列表")
        return cls(
            code=rule.code,
            group=group,
            stage=stage,
            level=level,
            summary=str(value.get("summary") or rule.summary),
            affected_actions=actions,
            recoverable=bool(value.get("recoverable", rule.recoverable)),
            remediation=str(value.get("remediation") or rule.remediation),
            fact_refs=tuple(str(item) for item in value.get("fact_refs") or []),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or []),
        )


@dataclass(frozen=True)
class BoundaryDecision:
    action: BoundaryAction
    level: RefusalLevel
    code: str
    summary: str
    allowed: bool
    remediation: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "level": self.level.value,
            "code": self.code,
            "summary": self.summary,
            "allowed": self.allowed,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class BoundaryEvaluation:
    checks: tuple[BoundaryCheck, ...]
    fact_objects: tuple[FactObject, ...] = ()
    subject: Mapping[str, Any] = field(default_factory=dict)
    policy_version: str = BOUNDARY_POLICY_VERSION

    @property
    def level(self) -> RefusalLevel:
        return RefusalLevel.highest(check.level for check in self.checks)

    @property
    def blocking_checks(self) -> tuple[BoundaryCheck, ...]:
        return tuple(
            check for check in self.checks if check.level is RefusalLevel.BLOCK
        )

    def checks_for(
        self,
        action: BoundaryAction | str,
    ) -> tuple[BoundaryCheck, ...]:
        action = BoundaryAction(action)
        return tuple(
            check
            for check in self.checks
            if action in check.affected_actions
        )

    def level_for(self, action: BoundaryAction | str) -> RefusalLevel:
        return RefusalLevel.highest(
            check.level for check in self.checks_for(action)
        )

    def requires_review(self, action: BoundaryAction | str) -> bool:
        return self.level_for(action) is RefusalLevel.REVIEW

    def permission(self, action: BoundaryAction | str) -> bool:
        action = BoundaryAction(action)
        level = self.level_for(action)
        if level is RefusalLevel.BLOCK:
            return False
        if level is RefusalLevel.REVIEW and action in {
            BoundaryAction.GENERATE,
            BoundaryAction.RENDER,
        }:
            return False
        return True

    def decide(
        self,
        action: BoundaryAction | str,
        *,
        approved: bool = False,
        confirmed: bool = False,
        hash_matches: bool = True,
    ) -> BoundaryDecision:
        action = BoundaryAction(action)
        relevant = self.checks_for(action)
        blocking = tuple(
            check for check in relevant if check.level is RefusalLevel.BLOCK
        )
        if blocking:
            check = max(blocking, key=lambda item: item.level.rank)
            return BoundaryDecision(
                action,
                check.level,
                check.code,
                check.summary,
                False,
                check.remediation,
            )
        if action is BoundaryAction.APPROVE and not confirmed:
            return _decision_for_rule(action, "approval_not_confirmed")
        if action is BoundaryAction.APPROVE and not hash_matches:
            return _decision_for_rule(action, "approval_hash_mismatch")
        if action is BoundaryAction.EXPORT and not approved:
            return _decision_for_rule(action, "export_not_approved")
        if action is BoundaryAction.EXPORT and not hash_matches:
            return _decision_for_rule(action, "export_hash_mismatch")
        level = self.level_for(action)
        if level is RefusalLevel.REVIEW and action in {
            BoundaryAction.GENERATE,
            BoundaryAction.RENDER,
        }:
            check = max(relevant, key=lambda item: item.level.rank)
            return BoundaryDecision(
                action,
                check.level,
                check.code,
                check.summary,
                False,
                check.remediation,
            )
        return BoundaryDecision(
            action,
            level,
            "boundary_allows_action",
            "边界评估允许执行该动作",
            True,
        )

    def to_dict(self) -> dict:
        level = self.level
        action_states: dict[str, dict] = {}
        for action in BoundaryAction:
            action_level = self.level_for(action)
            action_states[action.value] = {
                "action_label": action.label,
                "level": action_level.value,
                "level_label": action_level.label,
                "allowed": self.permission(action),
                "requires_review": action_level is RefusalLevel.REVIEW,
                "rule_codes": [
                    check.code for check in self.checks_for(action)
                ],
            }
        return {
            "policy_version": self.policy_version,
            "level": level.value,
            "label": level.label,
            "subject": dict(self.subject),
            "permissions": {
                action.value: self.permission(action)
                for action in BoundaryAction
            },
            "actions": action_states,
            "fact_objects": [
                fact.to_dict() for fact in self.fact_objects
            ],
            "checks": [check.to_dict() for check in self.checks],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "BoundaryEvaluation":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("边界评估必须是对象")
        raw_checks = value.get("checks", value.get("rules"))
        if not isinstance(raw_checks, list):
            raise ValueError("边界评估缺少规则结果列表")
        if not raw_checks:
            raise ValueError("边界评估必须至少包含一项规则结果")
        checks = tuple(BoundaryCheck.from_dict(raw) for raw in raw_checks)
        raw_facts = value.get("fact_objects", value.get("facts", []))
        if raw_facts is None:
            raw_facts = []
        if not isinstance(raw_facts, list):
            raise ValueError("边界事实对象必须为列表")
        facts: list[FactObject] = []
        for raw in raw_facts:
            try:
                facts.append(FactObject.from_dict(raw))
            except ValueError:
                continue
        subject = value.get("subject")
        if subject is not None and not isinstance(subject, dict):
            raise ValueError("边界评估 subject 必须是对象")
        return cls(
            checks=checks,
            fact_objects=tuple(facts),
            subject=dict(subject or {}),
            policy_version=str(
                value.get("policy_version") or BOUNDARY_POLICY_VERSION
            ),
        )


def _decision_for_rule(
    action: BoundaryAction,
    code: str,
) -> BoundaryDecision:
    rule = BOUNDARY_RULES[code]
    return BoundaryDecision(
        action,
        rule.level,
        rule.code,
        rule.summary,
        False,
        rule.remediation,
    )


def boundary_check(
    code: str,
    *,
    fact_refs: Iterable[str] = (),
    evidence_refs: Iterable[str] = (),
) -> BoundaryCheck:
    rule = BOUNDARY_RULES.get(code)
    if rule is None:
        raise ValueError(f"未知边界规则：{code}")
    return BoundaryCheck(
        code=rule.code,
        group=rule.group,
        stage=rule.stage,
        level=rule.level,
        summary=rule.summary,
        affected_actions=rule.affected_actions,
        recoverable=rule.recoverable,
        remediation=rule.remediation,
        fact_refs=tuple(str(item) for item in fact_refs),
        evidence_refs=tuple(str(item) for item in evidence_refs),
    )


def evaluate_boundaries(
    checks: Iterable[BoundaryCheck],
    *,
    fact_objects: Iterable[FactObject] = (),
    subject: Mapping[str, Any] | None = None,
) -> BoundaryEvaluation:
    return BoundaryEvaluation(
        checks=tuple(checks),
        fact_objects=tuple(fact_objects),
        subject=dict(subject or {}),
    )


def evaluation_from_audit(
    audit: Mapping[str, Any] | None,
    *,
    legacy: bool = False,
    fact_objects: Iterable[FactObject] = (),
    evidence_refs: Iterable[str] = (),
) -> BoundaryEvaluation:
    """Derive a rule evaluation for records without a stored v2 boundary."""

    if legacy:
        return evaluate_boundaries(
            [boundary_check("legacy_unaudited", evidence_refs=evidence_refs)],
            fact_objects=fact_objects,
            subject={"legacy": True},
        )
    if (
        not isinstance(audit, Mapping)
        or type(audit.get("approved")) is not bool
        or not isinstance(audit.get("findings"), list)
        or not 1 <= len(audit["findings"]) <= 40
        or any(
            not isinstance(item, Mapping)
            or item.get("status") not in {"pass", "issue"}
            for item in audit["findings"]
        )
    ):
        return evaluate_boundaries(
            [boundary_check("audit_structure_invalid", evidence_refs=evidence_refs)],
            fact_objects=fact_objects,
            subject={"legacy": False},
        )
    issue_refs = tuple(
        dict.fromkeys(
            str(fact_id)
            for item in audit["findings"]
            if isinstance(item, Mapping) and item.get("status") == "issue"
            for fact_id in item.get("fact_ids") or []
            if str(fact_id).strip()
        )
    )
    has_issue = bool(issue_refs) or any(
        isinstance(item, Mapping) and item.get("status") == "issue"
        for item in audit["findings"]
    )
    if bool(audit.get("approved")) == has_issue:
        return evaluate_boundaries(
            [boundary_check("audit_structure_invalid", evidence_refs=evidence_refs)],
            fact_objects=fact_objects,
            subject={"legacy": False},
        )
    check = boundary_check(
        "audit_review_required" if has_issue else "audit_passed",
        fact_refs=issue_refs,
        evidence_refs=evidence_refs,
    )
    return evaluate_boundaries(
        [check],
        fact_objects=fact_objects,
        subject={"audit_approved": bool(audit.get("approved"))},
    )


def with_runtime_integrity(
    evaluation: BoundaryEvaluation,
    integrity_error: str,
) -> BoundaryEvaluation:
    if not integrity_error:
        return evaluation
    return evaluate_boundaries(
        (*evaluation.checks, boundary_check("artifact_integrity_failed")),
        fact_objects=evaluation.fact_objects,
        subject=evaluation.subject,
    )
