import {esc, roles, agentIcon, labels} from './components.js';
import {AGENTS, RELATION_NAMES, buildNetwork, edgeGeometry} from './network-model.js';

const instances = new WeakMap();
const svgNS = 'http://www.w3.org/2000/svg';
let serial = 0;
const setText = (node, value) => { if (node.textContent !== value) node.textContent = value; };
const setAttr = (node, name, value) => { if (node.getAttribute(name) !== String(value)) node.setAttribute(name, value); };
const roleName = id => roles[id]?.name || id;
const timeText = created => created == null ? '时间未知' : new Date(created * 1000).toLocaleString('zh-CN', {hour12: false});
function svgElement(tag, attributes = {}) {
  const node = document.createElementNS(svgNS, tag);
  Object.entries(attributes).forEach(([k, v]) => node.setAttribute(k, v));
  return node;
}

function mount(host) {
  const uid = `network-${++serial}`;
  host.className = 'panel collaboration-network';
  host.innerHTML = `<header class="network-heading"><strong>协作网络</strong><span class="network-summary" aria-live="polite"></span></header>
    <div class="network-stage"><svg class="network-lines" aria-label="Agent 协作关系"><defs><marker id="${uid}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs><g class="network-paths"></g></svg>
    ${AGENTS.map(id => `<button class="network-node ${id}" data-network-agent="${id}" aria-expanded="false" aria-controls="${uid}-details">${agentIcon(id)}<strong>${roleName(id)}</strong><span class="network-status"></span><small class="network-count"></small></button>`).join('')}</div>
    <div class="network-legend"><span><i class="line-sample planned"></i>规划 / 依赖</span><span><i class="line-sample"></i>执行记录</span><span>箭头表示协作方向</span></div>
    <div class="network-edge-list" aria-label="协作关系列表"></div>
    <div class="network-details" id="${uid}-details" hidden></div>`;
  const record = {host, uid, selected: null, model: null, runId: null, seen: new Set(), paths: new Map(), buttons: new Map(), animations: new Map(), detailSignature: '', frame: null, reduced: matchMedia('(prefers-reduced-motion: reduce)')};
  record.measure = () => {
    if (!host.isConnected || !record.model) return;
    const stage = host.querySelector('.network-stage'), bounds = stage.getBoundingClientRect();
    setAttr(host.querySelector('.network-lines'), 'viewBox', `0 0 ${bounds.width} ${bounds.height}`);
    const rects = new Map(AGENTS.map(id => {
      const r = host.querySelector(`[data-network-agent="${id}"]`).getBoundingClientRect();
      return [id, {x: r.x - bounds.x, y: r.y - bounds.y, width: r.width, height: r.height}];
    }));
    const vertical = getComputedStyle(stage).getPropertyValue('--network-vertical').trim() === '1';
    for (const edge of record.model.edges) {
      const group = record.paths.get(edge.key);
      const d = edgeGeometry(rects.get(edge.from), rects.get(edge.to), vertical, bounds.width);
      group.querySelectorAll('path').forEach(path => setAttr(path, 'd', d));
    }
  };
  record.resize = new ResizeObserver(() => {
    if (record.frame) cancelAnimationFrame(record.frame);
    record.frame = requestAnimationFrame(() => { record.frame = null; record.measure(); });
  });
  record.resize.observe(host.querySelector('.network-stage'));
  record.motionChanged = () => { if (record.reduced.matches) stopPulses(record); };
  record.reduced.addEventListener('change', record.motionChanged);
  record.click = event => {
    const node = event.target.closest('[data-network-agent]');
    const edge = event.target.closest('[data-network-edge]');
    const task = event.target.closest('[data-network-task]');
    if (node || edge) {
      const selection = node ? `node:${node.dataset.networkAgent}` : `edge:${edge.dataset.networkEdge}`;
      record.selected = record.selected === selection ? null : selection;
      renderSelection(record);
    } else if (task) {
      const target = [...document.querySelectorAll('.task-item')].find(el => el.dataset.task === task.dataset.networkTask);
      if (target) { target.open = true; target.scrollIntoView({block: 'center', behavior: record.reduced.matches ? 'auto' : 'smooth'}); target.querySelector('summary').focus({preventScroll: true}); }
    } else if (event.target.closest('[data-network-close]')) { record.selected = null; renderSelection(record); }
  };
  record.key = event => {
    const group = event.target.closest('g[data-network-edge]');
    if (group && ['Enter', ' '].includes(event.key)) { event.preventDefault(); group.dispatchEvent(new MouseEvent('click', {bubbles: true})); }
  };
  host.addEventListener('click', record.click);
  host.addEventListener('keydown', record.key);
  instances.set(host, record);
  return record;
}

function taskLink(task) {
  return `<button class="network-task-link" data-network-task="${esc(task.id)}">定位子任务 · ${esc(task.title)}</button>`;
}
function relationDetail(relation) {
  return `<li><strong>${RELATION_NAMES[relation.kind]}${relation.preassigned ? ' · 预定' : ''}${relation.refreshed ? ' · 修订版本' : ''}</strong>
    ${relation.file ? `<p>交接文件：${esc(relation.fileTitle)}<small>原文件：${esc(relation.file)}</small></p>` : ''}
    ${relation.reason ? `<p>${esc(relation.reason)}</p>` : ''}
    ${relation.tasks.map(taskLink).join('')}
    <small>${relation.eventSeq == null ? '依据任务计划' : esc(timeText(relation.created))}</small></li>`;
}
function renderSelection(record) {
  const {host, model, selected, uid} = record;
  if (!model) return;
  let content = '', signature = selected || '';
  if (selected?.startsWith('node:')) {
    const node = model.nodes.find(n => `node:${n.id}` === selected);
    signature += JSON.stringify([node.status, node.tasks.map(t => [t.id, t.title, t.objective, t.status, t.owner]), node.internal, node.coordinationCount]);
    content = `<div class="network-detail-heading"><strong>${roleName(node.id)} · ${node.status.label}</strong><button data-network-close aria-label="收起协作详情">×</button></div>
      ${node.tasks.length ? `<ul>${node.tasks.map(task => `<li><strong>${esc(task.title)}</strong><small>${task.owner ? '' : '预定分工 · '}${esc(labels[task.status] || task.status)}</small><p>${esc(task.objective)}</p>${taskLink(task)}</li>`).join('')}</ul>` : node.coordinationCount ? `<p>已安排 ${node.coordinationCount} 项任务。点击分配箭头查看接收方和任务详情。</p>` : '<p>当前没有分配给该 Agent 的子任务。</p>'}
      ${node.internal.length ? `<h4>内部任务衔接</h4><ul>${node.internal.map(relationDetail).join('')}</ul>` : ''}
      <button class="text-link" data-action="memory" data-agent="${node.id}">查看 ${roleName(node.id)} 经验记忆</button>`;
  } else if (selected?.startsWith('edge:')) {
    const edge = model.edges.find(e => `edge:${e.key}` === selected);
    if (edge) {
      signature += JSON.stringify(edge);
      content = `<div class="network-detail-heading"><strong>${roleName(edge.from)} → ${roleName(edge.to)}</strong><button data-network-close aria-label="收起协作详情">×</button></div><p>${edge.relations.length} 项关系；规划依据与执行记录分别列出。</p><ul>${edge.relations.map(relationDetail).join('')}</ul>`;
    } else { record.selected = null; signature = ''; }
  }
  for (const node of model.nodes) {
    const button = host.querySelector(`[data-network-agent="${node.id}"]`);
    setAttr(button, 'aria-expanded', record.selected === `node:${node.id}`);
    button.classList.toggle('selected', record.selected === `node:${node.id}`);
  }
  for (const edge of model.edges) {
    const chosen = record.selected === `edge:${edge.key}`;
    record.paths.get(edge.key).classList.toggle('selected', chosen);
    setAttr(record.paths.get(edge.key), 'aria-expanded', chosen);
    const button = record.buttons.get(edge.key);
    button.classList.toggle('selected', chosen); setAttr(button, 'aria-expanded', chosen);
    setAttr(button, 'aria-controls', `${uid}-details`);
  }
  const panel = host.querySelector('.network-details');
  if (signature !== record.detailSignature) {
    const top = panel.scrollTop;
    const focused = panel.contains(document.activeElement) ? document.activeElement : null;
    const focusTask = focused?.dataset.networkTask, focusAgent = focused?.dataset.agent;
    panel.innerHTML = content;
    panel.scrollTop = top;
    if (focusTask) [...panel.querySelectorAll('[data-network-task]')].find(el => el.dataset.networkTask === focusTask)?.focus({preventScroll: true});
    if (focusAgent) panel.querySelector('[data-action="memory"]')?.focus({preventScroll: true});
    record.detailSignature = signature;
  }
  panel.hidden = !content;
}
function stopPulses(record) {
  for (const animation of record.animations.values()) animation.cancel();
  record.animations.clear();
}

export function updateNetwork(host, run, events = [], {live = false} = {}) {
  if (!host) return;
  let record = instances.get(host);
  if (record && record.runId !== run.id) { destroyNetwork(host); record = null; }
  const first = !record;
  record ||= mount(host);
  record.runId = run.id;
  const previousEdge = record.model?.edges.find(e => `edge:${e.key}` === record.selected);
  record.model = buildNetwork(run, events);
  if (previousEdge && !record.model.edges.some(e => e.key === previousEdge.key)) {
    const ids = new Set(previousEdge.relations.map(r => r.id));
    const replacement = record.model.edges.find(e => e.relations.some(r => ids.has(r.id)));
    const internal = record.model.nodes.find(n => n.internal.some(r => ids.has(r.id)));
    record.selected = replacement ? `edge:${replacement.key}` : internal ? `node:${internal.id}` : null;
  }
  const {model} = record;
  setText(host.querySelector('.network-summary'), model.summary);
  setAttr(host, 'data-run-id', run.id);
  for (const node of model.nodes) {
    const button = host.querySelector(`[data-network-agent="${node.id}"]`);
    setAttr(button, 'data-status', node.status.code);
    setAttr(button, 'aria-label', `${roleName(node.id)}，${node.status.label}，${node.tasks.length} 项分工，查看详情`);
    setText(button.querySelector('.network-status'), `${node.status.symbol} ${node.status.label}`);
    setText(button.querySelector('.network-count'), node.id === 'coordinator' && !node.tasks.length && node.coordinationCount ? `已分配 ${node.coordinationCount} 项任务` : `${node.tasks.length} 项分工${node.planned.length ? ` · ${node.planned.length} 项预定` : ''}`);
  }
  const keys = new Set(model.edges.map(e => e.key));
  for (const [key, group] of record.paths) if (!keys.has(key)) { group.remove(); record.paths.delete(key); record.buttons.get(key)?.remove(); record.buttons.delete(key); record.animations.get(key)?.cancel(); record.animations.delete(key); }
  const list = host.querySelector('.network-edge-list');
  list.querySelector('.network-empty')?.remove();
  for (const edge of model.edges) {
    if (!record.paths.has(edge.key)) {
      const group = svgElement('g', {'data-network-edge': edge.key, role: 'button', tabindex: '0', 'aria-controls': `${record.uid}-details`});
      group.append(svgElement('title'), svgElement('path', {class: 'relation-base', 'marker-end': `url(#${record.uid}-arrow)`}), svgElement('path', {class: 'relation-pulse'}), svgElement('path', {class: 'relation-hit'}));
      host.querySelector('.network-paths').append(group); record.paths.set(edge.key, group);
      const button = document.createElement('button'); button.dataset.networkEdge = edge.key;
      button.className = 'network-edge-button'; list.append(button); record.buttons.set(edge.key, button);
    }
    const group = record.paths.get(edge.key);
    group.classList.toggle('planned', edge.plannedOnly);
    const label = `${roleName(edge.from)} → ${roleName(edge.to)}，${edge.relations.length} 项关系：${edge.kinds.map(k => RELATION_NAMES[k]).join('、')}`;
    setAttr(group, 'aria-label', label); setText(group.querySelector('title'), label);
    const button = record.buttons.get(edge.key);
    setAttr(button, 'aria-label', label);
    const content = `<span>${roleName(edge.from)} → ${roleName(edge.to)}</span><small>${edge.relations.length} 项 · ${edge.kinds.map(k => RELATION_NAMES[k]).join(' / ')}</small>`;
    if (button.innerHTML !== content) button.innerHTML = content;
  }
  if (!model.edges.length) { const empty = document.createElement('p'); empty.className = 'network-empty'; empty.textContent = run.tasks?.length ? '暂未记录跨 Agent 协作关系。点击节点查看分工。' : '正在等待任务计划，尚无协作关系。'; list.append(empty); }
  record.measure();
  if (!model.active || record.reduced.matches) stopPulses(record);
  const now = Date.now() / 1000;
  for (const edge of model.edges) {
    const fresh = edge.relations.some(r => !r.planned && r.eventSeq != null && !record.seen.has(r.eventSeq) && ['assignment', 'handoff', 'revision'].includes(r.kind) && r.created != null && now - r.created >= 0 && now - r.created <= 5);
    if (!first && live && model.active && !record.reduced.matches && fresh) {
      record.animations.get(edge.key)?.cancel();
      const pulse = record.paths.get(edge.key).querySelector('.relation-pulse');
      const animation = pulse.animate([{strokeDashoffset: 36, opacity: 0.9}, {strokeDashoffset: 0, opacity: 0}], {duration: 1300, easing: 'linear'});
      record.animations.set(edge.key, animation);
      animation.onfinish = () => { if (record.animations.get(edge.key) === animation) record.animations.delete(edge.key); };
    }
  }
  events.forEach(e => record.seen.add(e.seq));
  renderSelection(record);
}

export function destroyNetwork(host) {
  const record = instances.get(host);
  if (!record) return;
  record.resize.disconnect();
  if (record.frame) cancelAnimationFrame(record.frame);
  stopPulses(record);
  record.reduced.removeEventListener('change', record.motionChanged);
  host.removeEventListener('click', record.click); host.removeEventListener('keydown', record.key);
  instances.delete(host);
}

export function setNetworkConnected(host, connected) {
  const record = instances.get(host);
  if (!record) return;
  host.classList.toggle('network-stale', !connected);
  if (!connected) stopPulses(record);
  setText(host.querySelector('.network-summary'), connected ? record.model.summary : '连接中断 · 等待同步');
}
