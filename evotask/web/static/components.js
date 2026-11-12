import { icon } from './icons.js';

export const esc = (value = '') => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const roles = {
  coordinator: { name: 'Coordinator', label: '统筹规划', description: '拆解目标 · 协调分工', icon: 'branch' },
  researcher: { name: 'Researcher', label: '资料研究', description: '检索资料 · 追溯来源', icon: 'search' },
  coder: { name: 'Coder', label: '构建交付', description: '生成代码 · 整理产物', icon: 'code' },
  verifier: { name: 'Verifier', label: '验证审查', description: '核验成果 · 反馈改进', icon: 'shield' },
  group: { name: '团队群聊', label: '共同讨论', icon: 'layers' },
  human: { name: '你', icon: 'human' },
};
export const agentIcon = (id, mini = false) => `<span class="agent-icon ${esc(id)} ${mini ? 'mini' : ''}">${id === 'human' ? 'YOU' : icon(roles[id]?.icon || 'spark')}</span>`;
export const brand = () => '<div class="brand-symbol" aria-hidden="true"><i></i><i></i><i></i></div>';
export const date = (stamp, full = false) => new Date(stamp * 1000).toLocaleString('zh-CN', full ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' } : { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
export const seconds = n => Number(n || 0) < 60 ? `${Number(n || 0).toFixed(1)}s` : `${Math.floor(n / 60)}m ${Math.floor(n % 60)}s`;
export const labels = { planning: '规划中', running: '执行中', waiting_human: '待你确认', waiting_child: '等待委派', pending: '待执行', succeeded: '已完成', failed: '未完成', cancelled: '已停止' };
export function badge(status) {
  const color = { succeeded: 'green', waiting_human: 'amber', failed: 'red', running: 'blue', planning: 'blue' }[status] || '';
  return `<span class="pill ${color}"><i class="dot"></i>${labels[status] || esc(status)}</span>`;
}
export const empty = (title, text = '', type = 'layers', action = '') => `<div class="empty">${icon(type)}<h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
export const loading = () => '<div class="loading-line" role="progressbar" aria-label="正在加载"></div>';

export function graph(run = null) {
  const active = run?.tasks.filter(t => t.status === 'running').map(t => t.owner) || [];
  const nodes = ['coordinator', 'researcher', 'coder', 'verifier'].map(id => {
    const working = active.includes(id) || (id === 'coordinator' && run?.status === 'planning');
    const done = run?.tasks.some(t => t.owner === id && t.status === 'succeeded');
    return `<button class="team-node ${id} ${working ? 'working' : ''}" data-action="memory" data-agent="${id}" aria-label="查看 ${roles[id].name} 经验">${agentIcon(id)}<span><strong>${roles[id].name}</strong><small>${working ? '正在执行任务' : done ? '已完成分工' : roles[id].label}</small></span></button>`;
  }).join('');
  return `<div class="team-visual ${run?.executing ? 'executing' : ''}"><div class="team-visual-heading"><span>${icon('activity')}协作网络</span><small><i class="dot"></i>${run ? '任务状态' : '4 AGENTS READY'}</small></div><div class="constellation"><svg class="connections" viewBox="0 0 380 202" preserveAspectRatio="none" aria-hidden="true"><ellipse class="orbit" cx="190" cy="102" rx="130" ry="74"/><ellipse class="orbit dashed" cx="190" cy="102" rx="153" ry="93"/><circle class="core-halo" cx="190" cy="101" r="39"/><path class="edge" d="M190 48V80M190 124v35M103 111h62M216 111h62"/><path class="edge" d="M190 48Q77 40 61 99M190 48Q303 40 319 99M61 126Q90 177 190 177M319 126Q285 177 190 177"/></svg><div class="core">${brand()}</div>${nodes}</div><div class="graph-caption"><span><i></i>独立记忆</span><span class="caption-line"></span><span>按能力分工</span><span class="caption-line"></span><span>可追溯交接</span></div></div>`;
}

export function runsTable(runs, filtered = false) {
  if (!runs.length) return filtered ? '<div class="filter-empty">没有符合条件的任务。试试其他关键词。</div>' : `<div class="empty-table">${icon('layers')}<div><h3>第一个想法，从这里开始</h3><p>启动一次演示，观察你的 AI 团队如何分工与协作。</p></div></div>`;
  return `<div class="table-wrap"><table class="runs-table"><thead><tr><th>任务名称</th><th>状态</th><th>创建时间</th><th>交付产物</th><th><span class="sr-only">查看</span></th></tr></thead><tbody>${runs.map(run => `<tr class="table-row" data-run="${esc(run.id)}" tabindex="0" role="link" aria-label="查看任务：${esc(run.goal)}"><td><div class="run-name">${icon('file')}<span title="${esc(run.goal)}">${esc(run.goal)}</span></div></td><td>${badge(run.status)}</td><td class="mono">${date(run.created)}</td><td class="mono">${run.artifact_count} files</td><td>${icon('arrow', 'row-arrow')}</td></tr>`).join('')}</tbody></table></div>`;
}

const eventLabels = {
  action_normalized: '已适配工具调用格式', action_retry_requested: '继续格式错误处的任务',
  revision_requested: '退回作者修订', artifact_refreshed: '已交接修订版本', revision_unavailable: '返修暂不可用',
  run_created: '任务已创建', model_request: '请求模型决策', plan_created: '已生成任务计划',
  capability_bid: '评估任务匹配度', task_assigned: '接受任务分工', delegated: '发起子任务委派',
  artifact_handoff: '交接授权产物', tool_result: '工具执行完成', task_succeeded: '通过验收检查',
  attempt_failed: '记录失败与已有产物', memory_recalled: '检索历史经验', waiting_human: '等待你的确认',
  human_reply: '收到人工回复', run_finished: '本次任务已结束', run_cancelled: '已停止任务',
  service_error: '执行服务中断', tool_started: '开始执行工具',
};
export function timeline(events, verbose = false) {
  const meaningful = verbose ? events : events.filter(e => !['model_request', 'capability_bid'].includes(e.kind));
  if (!meaningful.length) return '<div class="artifact-empty">任务开始后，协作过程将在这里实时出现。</div>';
  return `<div class="timeline">${meaningful.slice(-80).reverse().map(event => {
    const data = event.data;
    const detail = data.question || data.title || data.observed_error || data.message || data.reason || data.task_id || (data.status ? labels[data.status] : '') || '';
    return `<div class="timeline-item ${event.kind.includes('fail') || event.kind.includes('error') ? 'error' : ''}"><details data-event="${event.seq}"><summary><strong>${roles[event.agent]?.name || esc(event.agent)} · ${eventLabels[event.kind] || esc(event.kind)}</strong>${detail ? `<p>${esc(detail)}</p>` : ''}<time>${new Date(event.created * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</time></summary><pre class="event-data">${esc(JSON.stringify(data, null, 2))}</pre></details></div>`;
  }).join('')}</div>`;
}

export function tasks(run) {
  if (!run.tasks.length) return empty('正在拆解你的目标', '协调 Agent 将分析目标、确定分工并制定验收条件。', 'branch');
  return `<div class="task-list">${run.tasks.map(task => `<details class="task-item" data-task="${esc(task.id)}"><summary class="task-summary">${agentIcon(task.owner || task.role)}<div class="task-text"><strong>${esc(task.title)}</strong><small>${task.parent ? '↳ 委派任务 · ' : ''}${roles[task.owner]?.name || '等待分配'} · ${task.steps} 步操作</small></div>${badge(task.status)}${icon('chevron')}</summary><div class="task-details"><p>${esc(task.objective)}</p>${task.summary ? `<p>${esc(task.summary)}</p>` : ''}${task.depends_on.length ? `<p class="dependency">前置任务：${esc(task.depends_on.join(', '))}</p>` : ''}<div>验收条件</div>${task.checks.map(check => `<div class="check-line">${icon(task.status === 'succeeded' ? 'check' : 'shield')}<span>${esc(check.kind)} · ${esc(check.reason || check.path)}${check.text ? ` 包含「${esc(check.text)}」` : ''}</span></div>`).join('')}${task.memory_hits.length ? `<p class="dependency">本次召回 ${task.memory_hits.length} 条历史经验</p>` : ''}</div></details>`).join('')}</div>`;
}

export function markdown(source) {
  // Intentionally small safe renderer: never inserts raw HTML or external embeds.
  const lines = String(source).split('\n');
  let code = null, output = '';
  const inline = s => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  for (const line of lines) {
    if (line.startsWith('```')) {
      if (code !== null) { output += `<pre><code>${esc(code.join('\n'))}</code></pre>`; code = null; }
      else code = [];
    } else if (code !== null) code.push(line);
    else if (/^#{1,3} /.test(line)) { const n = line.indexOf(' '); output += `<h${n}>${inline(line.slice(n + 1))}</h${n}>`; }
    else if (/^[-*] /.test(line)) output += `<div class="markdown-li">${inline(line.slice(2))}</div>`;
    else if (line.startsWith('> ')) output += `<blockquote>${inline(line.slice(2))}</blockquote>`;
    else if (line.trim()) output += `<p>${inline(line)}</p>`;
  }
  if (code !== null) output += `<pre><code>${esc(code.join('\n'))}</code></pre>`;
  return output;
}
