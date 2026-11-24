"""Application-owned language and clarification policy, independent of role prompts."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


INTERACTION_POLICY = """
LANGUAGE AND HUMAN INTERACTION (application policy; overrides role descriptions and input language):
Always use Simplified Chinese for replies, questions, task titles/objectives, bid reasons, summaries,
review explanations, and the natural-language content of deliverables, including HTML visible text.
Even if the human or a custom role requests English or another language, produce Chinese prose.
Preserve proper names, URLs, code/API identifiers, data keys, literal acceptance strings, and verbatim
source quotations; do not translate machine-readable protocols or falsify original evidence.
Ask only ONE essential decision per turn, in one short Chinese sentence (at most 100 characters).
Do not bundle questions or request a questionnaire, a detailed specification, or repeated information.
First inspect the goal, conversation, previous answers and available inputs. For low-impact reversible
preferences choose a reasonable default, state it briefly and proceed; ask only if missing information
blocks correctness, or authority/critical actions require human consent. Never infer critical consent.
For a choice, supply 2-3 short, distinct Chinese options (each at most 30 characters); put the recommended
choice first and label it （推荐）. Omit options only when a short factual answer or URL is necessary.
Do not invent source URLs, finances or permissions as defaults. Use at most 2 clarification rounds per
run for ordinary preferences; further clarification must be genuinely blocking. Approval is separate.
Chat replies should normally be within 300 characters, with at most one question; avoid long preambles.
"""


def chinese_prose(text: str, field: str) -> None:
    """Reject obviously foreign prose; this is a guard, not a language-quality classifier."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f'{field}：请使用简体中文填写内容。')
    prose = re.sub(r'```.*?```|`[^`]*`|https?://\S+', '', text, flags=re.S)
    prose = re.sub(r'^\s*>.*$', '', prose, flags=re.M)  # Quoted evidence stays verbatim.
    if not prose.strip():
        return
    chinese = len(re.findall(r'[\u3400-\u9fff]', prose))
    words = re.findall(r'[A-Za-z]{2,}', prose)
    if not chinese or (len(words) >= 20 and chinese < len(words)):
        raise ValueError(f'{field}：正文应以简体中文撰写；保留代码、专有名词和来源原文。请改写后重试。')


def clarification(payload: dict, *, chinese: bool = True) -> tuple[str, list[str]]:
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
    if chinese:
        chinese_prose(question, '澄清问题')
        for option in options:
            chinese_prose(option, '回答选项')
    return question.strip(), options


def validate_plan_language(plan: dict) -> None:
    for spec in plan.get('tasks', []) or []:
        if not isinstance(spec, dict):
            continue  # Structural validation owns this error.
        for field in ('title', 'objective'):
            chinese_prose(spec.get(field, ''), field)
        for check in spec.get('checks', []) or []:
            if isinstance(check, dict) and check.get('kind') == 'human':
                chinese_prose(check.get('reason', ''), '验收说明')


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'pre', 'code'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'pre', 'code'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def validate_deliverable(path: Path) -> None:
    # Code/data retain their protocol/identifiers. Source inputs are never translated.
    if path.suffix.lower() not in {'.md', '.txt', '.html'}:
        return
    text = path.read_text(encoding='utf-8')
    if path.suffix.lower() == '.html':
        parser = VisibleText()
        parser.feed(text)
        text = '\n'.join(parser.parts)
    chinese_prose(text, f'交付物 {path.name}')
