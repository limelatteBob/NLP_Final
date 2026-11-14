// Pure projection: no backend writes, timers, DOM, or inferred execution events.
export const AGENTS = ['coordinator', 'researcher', 'coder', 'verifier'];
export const RELATION_NAMES = {assignment: '任务分配', dependency: '任务依赖', delegation: '子任务委派', handoff: '资料交接', revision: '返修反馈'};
const terminal = new Set(['succeeded', 'failed', 'cancelled']);
const valid = id => AGENTS.includes(id);
export const executionActive = run => !!run.executing && !run.pending && !run.cancelling && !terminal.has(run.status) && run.status !== 'waiting_human';

export function agentState(run, id, assigned, planned) {
  const has = status => [...assigned, ...planned].some(t => t.status === status);
  const planning = id === 'coordinator' && run.status === 'planning';
  if (executionActive(run) && (planning || assigned.some(t => t.status === 'running'))) return {code: 'running', label: planning ? '规划中' : '执行中', symbol: '●'};
  const awaitingReply = run.pending && (assigned.some(t => t.id === run.pending.task_id) || (id === 'coordinator' && !run.pending.task_id));
  if (has('waiting_human') || awaitingReply) return {code: 'waiting_human', label: '待用户确认', symbol: '◷'};
  if (has('waiting_child')) return {code: 'waiting_child', label: '等待子任务', symbol: '◷'};
  if (has('failed')) return {code: 'failed', label: '失败', symbol: '!'};
  if (has('running') || planning) {
    if (run.cancelling) return {code: 'stopping', label: '正在停止', symbol: '■'};
    if (run.pending || run.status === 'waiting_human') return {code: 'paused', label: '已暂停', symbol: 'Ⅱ'};
    if (terminal.has(run.status)) return {code: 'stopped', label: '本次执行已结束', symbol: '■'};
    return {code: 'interrupted', label: '执行已中断', symbol: '!'};
  }
  if (has('pending')) return {code: 'pending', label: assigned.length ? '待执行' : '预定 · 待分配', symbol: '○'};
  if (has('succeeded')) return {code: 'succeeded', label: '已完成', symbol: '✓'};
  if (has('cancelled')) return {code: 'cancelled', label: '已停止', symbol: '■'};
  return {code: 'idle', label: '未参与', symbol: '—'};
}

export function buildNetwork(run, inputEvents = []) {
  const tasks = run.tasks || [], byTask = new Map(tasks.map(t => [t.id, t]));
  const artifacts = new Map((run.artifacts || []).map(a => [a.id, a]));
  const events = [...new Map(inputEvents.filter(e => !e.run_id || e.run_id === run.id).map(e => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq);
  const assignments = events.filter(e => e.kind === 'task_assigned');
  const ownerAt = (taskId, seq) => assignments.findLast(e => e.data.task_id === taskId && e.seq <= seq)?.agent;
  const nodes = AGENTS.map(id => {
    const assigned = tasks.filter(t => t.owner === id);
    const planned = tasks.filter(t => !t.owner && t.role === id);
    return {id, assigned, planned, tasks: [...assigned, ...planned], status: agentState(run, id, assigned, planned), internal: []};
  });
  const edges = new Map(), relations = [], seen = new Set();
  function add(relation) {
    if (!valid(relation.from) || !valid(relation.to) || seen.has(relation.id)) return;
    seen.add(relation.id);
    relation.taskIds = [...new Set((relation.taskIds || []).filter(id => byTask.has(id)))];
    relation.tasks = relation.taskIds.map(id => ({id, title: byTask.get(id).title}));
    relations.push(relation);
    if (relation.from === relation.to) { nodes.find(n => n.id === relation.from).internal.push(relation); return; }
    const key = `${relation.from}:${relation.to}`;
    if (!edges.has(key)) edges.set(key, {key, from: relation.from, to: relation.to, relations: []});
    edges.get(key).relations.push(relation);
  }
  for (const task of tasks) {
    for (const dependency of task.depends_on || []) {
      const upstream = byTask.get(dependency);
      if (upstream) add({id: `dependency:${dependency}:${task.id}`, kind: 'dependency',
        from: upstream.owner || upstream.role, to: task.owner || task.role,
        planned: true, preassigned: !upstream.owner || !task.owner, taskIds: [dependency, task.id], created: null});
    }
  }
  for (const event of events) {
    const data = event.data || {};
    const common = {id: `event:${event.seq}`, eventSeq: event.seq, created: event.created ?? null, planned: false};
    if (event.kind === 'task_assigned') {
      add({...common, kind: 'assignment', from: 'coordinator', to: event.agent, taskIds: [data.task_id]});
    } else if (event.kind === 'delegated') {
      // Resolve the first actual assignment for this delegation, not a later reassignment.
      const assigned = assignments.find(e => e.data.task_id === data.to_task && e.seq > event.seq);
      const recipient = assigned?.agent || byTask.get(data.to_task)?.owner;
      add({...common, kind: 'delegation', from: event.agent, to: recipient || data.role,
        planned: !recipient, preassigned: !recipient, taskIds: [data.from_task, data.to_task]});
    } else if (['artifact_handoff', 'artifact_refreshed'].includes(event.kind)) {
      const artifact = artifacts.get(data.artifact_id);
      if (artifact) {
        const recipients = tasks.filter(t => t.owner === event.agent && (t.inputs || []).includes(data.path)).map(t => t.id);
        add({...common, kind: 'handoff', from: artifact.owner, to: event.agent,
          taskIds: [artifact.task_id, data.task_id, ...recipients], file: artifact.path,
          fileTitle: artifact.title || artifact.path, refreshed: event.kind === 'artifact_refreshed'});
      }
    } else if (event.kind === 'revision_requested') {
      // Missing historical ownership is not guessed from the current, possibly revised plan.
      add({...common, kind: 'revision', from: event.agent,
        to: data.target_owner || ownerAt(data.target_task, event.seq),
        taskIds: [data.from_task, data.target_task], reason: data.reason || '', round: data.round});
    }
  }
  const coordinator = nodes.find(n => n.id === 'coordinator');
  coordinator.coordinationCount = new Set(assignments.map(e => e.data.task_id).filter(Boolean)).size;
  if (coordinator.status.code === 'idle' && events.some(e => ['plan_created', 'task_assigned'].includes(e.kind))) {
    coordinator.status = {code: 'succeeded', label: coordinator.coordinationCount ? '已安排分工' : '已完成规划', symbol: '✓'};
  }
  for (const edge of edges.values()) {
    edge.plannedOnly = edge.relations.every(r => r.planned);
    edge.kinds = [...new Set(edge.relations.map(r => r.kind))];
  }
  return {nodes, edges: [...edges.values()], relations, active: executionActive(run),
    summary: terminal.has(run.status) ? {succeeded: '任务已完成', failed: '任务未完成', cancelled: '任务已停止'}[run.status] :
      run.pending || run.status === 'waiting_human' ? '等待用户确认' : run.cancelling ? '正在停止' :
      run.executing ? (run.status === 'planning' ? '正在规划' : '实时协作') : '执行已中断'};
}

// Nodes and paths use the same measured coordinate space, including responsive layouts.
export function edgeGeometry(from, to, vertical, width) {
  const center = r => ({x: r.x + r.width / 2, y: r.y + r.height / 2});
  const a = center(from), b = center(to);
  if (vertical) {
    const sign = b.y > a.y ? 1 : -1;
    const start = {x: a.x + sign * (from.width / 2 + 3), y: a.y};
    const end = {x: b.x + sign * (to.width / 2 + 5), y: b.y};
    const lane = sign > 0 ? width - 5 : 5;
    return `M ${start.x} ${start.y} C ${lane} ${start.y}, ${lane} ${end.y}, ${end.x} ${end.y}`;
  }
  const dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy) || 1;
  const control = {x: (a.x + b.x) / 2 - dy / length * 28, y: (a.y + b.y) / 2 + dx / length * 28};
  function boundary(r, c, target, gap) {
    const x = target.x - c.x, y = target.y - c.y;
    const scale = Math.min(x ? (r.width / 2 + gap) / Math.abs(x) : Infinity, y ? (r.height / 2 + gap) / Math.abs(y) : Infinity);
    return {x: c.x + x * scale, y: c.y + y * scale};
  }
  const start = boundary(from, a, control, 3), end = boundary(to, b, control, 5);
  return `M ${start.x} ${start.y} Q ${control.x} ${control.y}, ${end.x} ${end.y}`;
}
