"""Bounded, local Markdown role extraction. Document text never changes permissions."""
from pathlib import Path
import re

SLOTS = ('coordinator', 'researcher', 'coder', 'verifier')


def clean(value):
    return re.sub(r'[`*_]', '', value).strip()


def parse_team_document(filename: str, content: str) -> dict:
    if Path(filename).suffix.lower() not in {'.md', '.markdown'}:
        raise ValueError('请上传 .md 或 .markdown 文档')
    if len(content.encode('utf-8')) > 131072 or '\x00' in content:
        raise ValueError('Markdown 文档须为 UTF-8 文本，且不超过 128 KB')
    content = content.lstrip('\ufeff')
    members, warnings = [], []
    title = re.search(r'^#\s+(.+)', content, re.M)
    name = clean(title[1]) if title else Path(filename).stem
    name = re.sub(r'^Scenario\s+\d+\s*[:：]\s*', '', name, flags=re.I)
    if len(name) > 80:
        name = ''
        warnings.append('文档标题超过 80 字，请填写团队名称。')

    def add(label, instructions):
        label = re.sub(r'^(?:成员|角色|Agent)\s*\d*\s*[:：]\s*', '', clean(label), flags=re.I)
        # Remove a wrapping Markdown code fence, preserving the actual instructions.
        instructions = re.sub(r'^\s*(```|~~~)[^\n]*\n', '', instructions.strip())
        instructions = re.sub(r'\n\s*(```|~~~)\s*$', '', instructions).strip()
        if not label or not instructions:
            return
        if len(label) > 80 or len(instructions) > 5000:
            raise ValueError('成员名称不得超过 80 字，职责不得超过 5000 字；请拆分文档后重试')
        existing = next((m for m in members if m['name'] == label), None)
        if existing:
            if existing['instructions'] != instructions:
                raise ValueError(f'“{label}”有多份不同的职责，请合并后重新上传')
            return
        members.append({'name': label, 'instructions': instructions})

    # Tables must explicitly identify a name column AND a responsibilities column.
    lines = content.splitlines()
    table_lines = set()
    for i, line in enumerate(lines[:-1]):
        cells = [clean(c).lower() for c in line.strip().strip('|').split('|')]
        if '|' not in line or not re.fullmatch(r'[\s|:\-]+', lines[i + 1]):
            continue
        names = [j for j, c in enumerate(cells) if c in {'name', 'role', 'member', 'agent', '角色', '名称', '成员', '成员名称', '角色名称'}]
        duties = [j for j, c in enumerate(cells) if c in {'responsibilities', 'responsibility', 'instructions', 'duties', '职责', '职责描述', '分工'}]
        if not names or not duties:
            continue
        j = i + 2
        while j < len(lines) and '|' in lines[j] and lines[j].strip():
            values = [v.strip() for v in lines[j].strip().strip('|').split('|')]
            if max(names[0], duties[0]) < len(values):
                add(values[names[0]], re.sub(r'<br\s*/?>', '\n', values[duties[0]], flags=re.I))
            j += 1
        table_lines.update(range(i, j))

    # Headings outside fenced blocks; nested responsibility headings belong to the role.
    headings, fence = [], None
    for i, line in enumerate(lines):
        f = re.match(r'^\s*(`{3,}|~{3,})', line)
        if f:
            if fence is None:
                fence = f[1][0]
            elif f[1][0] == fence:
                fence = None
            continue
        h = re.match(r'^(#{2,6})\s+(.+?)\s*#*$', line)
        if h and fence is None:
            headings.append((i, len(h[1]), h[2]))
    containers = {'团队', '团队成员', '成员', '角色', '角色初始化', 'roles', 'members', 'agents', 'team members', '职责', '职责描述', 'responsibilities', 'instructions', '任务', '任务目标', '说明', '背景', 'task', 'tasks', 'workflow', '工作流程'}
    for index, (line, level, label) in enumerate(headings):
        if clean(label).lower() in containers:
            continue
        end = next((other[0] for other in headings[index + 1:] if other[1] <= level), len(lines))
        body = '\n'.join(v for j, v in enumerate(lines[line + 1:end], line + 1) if j not in table_lines).strip()
        # A container with nested role headings is not itself a member.
        if any(line < other[0] < end and clean(other[2]).lower() not in containers for other in headings):
            continue
        body = re.sub(r'^#{2,6}\s+(?:职责|职责描述|Responsibilities|Instructions)\s*$', '', body, flags=re.M | re.I).strip()
        add(label, body)
    if not members:
        raise ValueError('未识别到成员及职责。请使用“## 成员名称”后跟职责正文，或包含“角色／职责”的表格。')
    if len(members) > 24:
        raise ValueError('最多识别 24 位候选成员，请精简文档')
    assignments, used = {}, set()
    hints = {
        'coordinator': r'coordinator|technical lead|team lead|chief|fund manager|统筹|负责人|组长|协调|主编',
        'verifier': r'verifier|reviewer|test engineer|\bqa\b|risk|fact.check|审查|审核|测试|风控|核查',
        'coder': r'coder|developer|programmer|开发|程序员',
        'researcher': r'research|analyst|研究|分析',
    }
    for slot, hint in hints.items():
        candidates = [i for i, m in enumerate(members) if i not in used and re.search(hint, m['name'], re.I)]
        if len(candidates) == 1:
            assignments[slot] = candidates[0]
            used.add(candidates[0])
    for slot in SLOTS:
        if slot not in assignments:
            remaining = [i for i in range(len(members)) if i not in used]
            assignments[slot] = remaining[0] if remaining else None
            used.add(assignments[slot])
    warnings.append('位置为规则匹配建议，请核对统筹和独立审查角色；名称与职责均可编辑。')
    if len(members) != 4:
        warnings.append(f'识别到 {len(members)} 位成员；系统使用四个位置，请补全或选择需要的四位成员。所有候选成员均保留在下拉框中。')
    return {'name': name, 'members': members, 'assignments': assignments, 'warnings': warnings}
