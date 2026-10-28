"""Editable semantic instructions, with the response protocol kept fixed.

Task overrides contain only the human-editable body. CLI defaults remain the
project prompt; a task composes and saves one complete prompt before requests.
"""

from __future__ import annotations

import re

from project_resources import DEFAULT_PROMPT


_PROTOCOL_HEADING = re.compile(r"^## 严格 JSON 输出[ \t]*$", re.M)

# These names belong to the retired, evidence-heavy review schema.  Match
# exact field names rather than generic words such as "experience" or
# "evidence", so ordinary task-specific prose is never silently replaced.
_LEGACY_SCHEMA_FIELDS = re.compile(
    r"(?<![\w])(?:target_relevance|experience_assessment|jd_dimensions|"
    r"ability_gaps|evidence_quality|needs_human_review)(?![\w])",
    re.I,
)
_LEGACY_NATURAL_RULE_MARKERS = (
    ("六项核心职责", "原文证据", "能力差距"),
    ("六项职责", "原文证据", "能力差距"),
    ("六个维度", "逐项引用", "能力差距"),
)


def _parts(prompt: str) -> tuple[str, str]:
    prompt = prompt.replace("\r\n", "\n").replace("\r", "\n")
    match = _PROTOCOL_HEADING.search(prompt)
    if match is None:
        raise ValueError("默认复核提示词缺少固定 JSON 输出协议。")
    return prompt[:match.start()].strip(), prompt[match.start():].strip()


def default_review_rules() -> str:
    """Return the editable rules from the current project resource."""
    return _parts(DEFAULT_PROMPT.read_text(encoding="utf-8-sig"))[0]


def is_legacy_complex_review_rules(rules: str) -> bool:
    """Identify only the retired rich-schema rules for explicit AI redo.

    Custom rules are otherwise user-owned and must remain untouched.  The
    former schema used distinctive field names; its prose-only form is
    recognized only when all three specific markers occur together.
    """
    if not isinstance(rules, str) or not rules.strip():
        return False
    if _LEGACY_SCHEMA_FIELDS.search(rules):
        return True
    normalized = re.sub(r"\s+", "", rules)
    return any(all(marker in normalized for marker in group)
               for group in _LEGACY_NATURAL_RULE_MARKERS)


def compose_review_prompt(rules: str, *, default_prompt: str | None = None) -> str:
    """Append the fixed two-field protocol, not historical rich instructions."""
    if not isinstance(rules, str) or not rules.strip():
        raise ValueError("复核规则不能为空。")
    source = default_prompt if default_prompt is not None else DEFAULT_PROMPT.read_text(encoding="utf-8-sig")
    _, protocol = _parts(source)
    body = rules.replace("\r\n", "\n").replace("\r", "\n").strip()
    return body + "\n\n" + protocol + "\n"
