"""Application-owned language and clarification policy, independent of role prompts."""
from __future__ import annotations

import re


INTERACTION_POLICY = """
LANGUAGE AND HUMAN INTERACTION (application policy):
For a task, use the user's explicit conversation-language preference or the language of task_goal for
questions, options, plans, bids, summaries and review explanations. An English task uses English for
these fields by default. For ordinary chat, use the user's requested language or their latest message.
Use Simplified Chinese when the language is unclear. Earlier messages, role descriptions and examples
do not set the language for the current task.
For deliverables, follow the language requested by the user in the task or clarifications, including
per-file and multilingual requests. Otherwise use the language of task_goal. Apply this to newly written
prose, including documentation, code comments, docstrings, UI labels, user-facing strings and visible
HTML text. Use the task language as a default, without adding it as a user requirement in the plan.
Preserve proper names, URLs, code/API identifiers, data keys, literal acceptance strings, and verbatim
source quotations; do not translate machine-readable protocols or falsify original evidence.
Ask only ONE essential decision per turn, in one short sentence (at most 100 characters).
Do not bundle questions or request a questionnaire, a detailed specification, or repeated information.
First inspect the goal, conversation, previous answers and available inputs. For low-impact reversible
preferences choose a reasonable default, state it briefly and proceed; ask only if missing information
blocks correctness, or authority/critical actions require human consent. Never infer critical consent.
For a choice, supply 2-3 short, distinct options (each at most 30 characters). Put the recommended
choice first and label it in the conversation language. Omit options only when a short factual answer
or URL is necessary.
Do not invent source URLs, finances or permissions as defaults. Use at most 2 clarification rounds per
run for ordinary preferences; further clarification must be genuinely blocking. Approval is separate.
Chat replies should normally be within 300 characters, with at most one question; avoid long preambles.
"""


def require_prose(text: object, field: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f'{field} must be a nonempty string')


def task_locale(goal: str) -> str:
    """Select an English or Chinese template for generated system updates."""
    prose = re.sub(r'```.*?```|`[^`]*`|https?://\S+|^ *>[^\n]*', '', goal, flags=re.S | re.M)
    han = len(re.findall(r'[\u3400-\u9fff]', prose))
    words = len(re.findall(r'[A-Za-z]{2,}', prose))
    return 'en' if words > han else 'zh'


def task_text(run: dict, chinese: str, english: str) -> str:
    return english if run.get('interaction_locale') == 'en' else chinese


def clarification(payload: dict) -> tuple[str, list[str]]:
    question, options = payload.get('question'), payload.get('options', [])
    if (not isinstance(question, str) or not question.strip() or len(question.strip()) > 100
            or '\n' in question.strip() or len(re.findall(r'[?？]', question)) > 1
            or re.search(r'(?:^|\s)[1-9][.、)]', question)):
        raise ValueError('每次只问一个关键问题，用不超过 100 字的一句话表达，禁止罗列多个问题。')
    if not isinstance(options, list) or (options and not 2 <= len(options) <= 3):
        raise ValueError('请提供 2–3 个简短选项；需要填写事实或 URL 时可省略选项。')
    if any(not isinstance(o, str) or not o.strip() or len(o.strip()) > 30 or '\n' in o for o in options):
        raise ValueError('每个选项须为不超过 30 字的一行文字。')
    options = [o.strip() for o in options]
    if len(set(options)) != len(options):
        raise ValueError('回答选项不能重复。')
    return question.strip(), options
