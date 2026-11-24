import { updateNetwork, destroyNetwork, setNetworkConnected } from './network.js';
import { icon } from './icons.js';
import { accountUI } from './account.js';
import { teamUI } from './teams.js';
import { esc, roles, agentIcon, brand, date, seconds, badge, empty, loading, graph, runsTable, timeline, tasks, markdown, questionOptions } from './components.js';


const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const app = $('#app'), modal = $('#modal');
let pendingDocumentFiles = [];
let busyCount = 0;
const activeUploads = new Set();
const nav = [
  ['home', 'grid', '工作台'], ['runs', 'layers', '任务记录'], ['knowledge', 'book', '知识库'], ['chat', 'chat', 'Agent 对话'],
];
const state = {
  user: null, identityEpoch: 0,
  mode: localStorage.getItem('evotask-mode') === 'live' ? 'live' : 'demo', csrf: '',
  workspace: { runs: [], documents: 0, agents: [], job: {} }, route: 'home', run: null, events: [],
  docs: [], bases: [], baseId: 'kb_default', selectedBases: null, channel: 'group', chatRun: '', messages: [], sending: false, files: [], goal: '', memory: true, review: true,
  filter: 'all', query: '', verbose: false, stream: null, generation: 0, initialized: false,
};

function apiPath(path, mode = state.mode) { return `${path}${path.includes('?') ? '&' : '?'}mode=${mode}`; }
async function api(path, { method = 'GET', body, mode = state.mode } = {}) {
  const epoch = state.identityEpoch;
  let response;
  try {
    response = await fetch(apiPath(path, mode), {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: { ...(method === 'GET' ? {} : { 'Content-Type': 'application/json' }), ...(path === '/api/session' ? {} : { 'X-EvoTask-CSRF': state.csrf }) },
      ...(method !== 'GET' ? { body: JSON.stringify(body || {}) } : {}),
    });
  } catch { throw new Error('暂时无法连接服务，请确认后端仍在运行。'); }
  const data = await response.json().catch(() => ({}));
  if (epoch !== state.identityEpoch) throw new Error('登录身份已变化，已忽略旧请求。');
  if (!response.ok) {
    if (['account_expired', 'identity_changed'].includes(data.code)) {
      if (!sessionReset) queueMicrotask(() => changeSession(false));
    }
    if (response.status === 401 && path !== '/api/session/login') toast('会话需要重新验证，请刷新页面。', true);
    throw new Error(typeof data.detail === 'string' ? data.detail : `请求未完成（${response.status}）`);
  }
  return data;
}

let sessionReset = null;
async function changeSession(notify = true) {
  if (sessionReset) return sessionReset;
  sessionReset = (async () => {
    destroyNetwork($('#collaboration-network'));
    state.stream?.close(); state.stream = null; ++state.generation; ++state.identityEpoch;
    activeUploads.forEach(request => request.abort());
    state.initialized = false; closeModal(); pendingDocumentFiles = [];
    Object.assign(state, { user: null, run: null, events: [], docs: [], bases: [], baseId: 'kb_default', selectedBases: null,
      files: [], messages: [], goal: '', query: '', filter: 'all', channel: 'group', chatRun: '', sending: false,
      workspace: { runs: [], agents: [], documents: 0, job: {} } });
    history.replaceState(null, '', location.hash === '#knowledge' ? '#knowledge' : '#home');
    app.innerHTML = loading();
    await start();
    if (notify) localStorage.setItem('evotask-identity-change', String(Date.now()) + Math.random());
  })();
  try { await sessionReset; } finally { sessionReset = null; }
}

async function refreshKnowledge() {
  await refreshWorkspace();
  state.docs = await api('/api/knowledge');
  if (state.route === 'knowledge') knowledgePage();
}

const account = accountUI({ state, api, openModal, closeModal, busy, toast, changed: changeSession,
  refreshKnowledge, isBusy: () => busyCount > 0 || state.sending });

let toastTimer;
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $('#toast-region').innerHTML = `<div class="toast ${error ? 'error' : ''}" role="status">${icon(error ? 'info' : 'check')}<span>${esc(message)}</span></div>`;
  toastTimer = setTimeout(() => { $('#toast-region').innerHTML = ''; }, error ? 6500 : 3500);
}

function openModal(title, content, className = '') {
  modal.className = `modal ${className}`;
  modal.innerHTML = `<div class="modal-header"><h2 id="modal-title">${esc(title)}</h2><button class="icon-btn" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body">${content}</div>`;
  if (!modal.open) modal.showModal();
}
function closeModal() { modal.close(); }
modal.addEventListener('click', event => { if (event.target === modal && !(busyCount && $('#document-form', modal))) { const rect = modal.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModal(); } });

async function busy(button, action) {
  if (button?.disabled) return;
  const original = button?.innerHTML;
  busyCount++;
  if ($('#mode-select')) $('#mode-select').disabled = true;
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner"></span> 处理中'; }
  try { return await action(); }
  catch (error) { toast(error.message, true); }
  finally { if (button?.isConnected) { button.disabled = false; button.innerHTML = original; } busyCount--; if ($('#mode-select') && !busyCount) $('#mode-select').disabled = false; }
}

modal.addEventListener('cancel', event => { if (busyCount && $('#document-form', modal)) event.preventDefault(); });

function sourceFiles(run) {
  if (!run.uploaded_files?.length) return '';
  return `<section class="panel"><div class="panel-header"><span>${icon('attach')}输入资料</span></div><div class="artifact-list">${run.uploaded_files.map(file => `<div class="artifact-item">${icon('file')}<div class="artifact-name"><strong>${esc(file.name)}</strong><small>${fileSize(file.bytes)} · ${file.status === 'stored' ? esc(file.detail) : '已提取正文，大文件可分段读取'}</small></div><a class="icon-btn" href="${apiPath(`/api/uploads/${file.id}/download`)}" download aria-label="下载输入 ${esc(file.name)}">${icon('download')}</a></div>`).join('')}</div></section>`;
}

function shell() {
  app.innerHTML = `<div class="app-shell"><div class="mobile-overlay" data-action="toggle-menu"></div><aside class="sidebar" aria-label="主导航"><a class="brand" href="#home" aria-label="EvoTask 工作台">${brand()}<span class="brand-name">Evo<span>Task</span></span></a><div class="workspace-select"><span class="workspace-avatar">${icon('layers')}</span><span><strong>我的工作空间</strong><small>Personal workspace</small></span>${icon('lock')}</div><p class="nav-label">WORKSPACE</p><nav class="nav">${nav.map(([id, symbol, label]) => `<a href="#${id}" class="nav-item" data-nav="${id}">${icon(symbol)}<span>${label}</span>${id === 'runs' ? '<span class="nav-count" id="run-count">0</span>' : ''}</a>`).join('')}</nav><p class="nav-label">PREFERENCES</p><nav class="nav"><a href="#system" class="nav-item" data-nav="system">${icon('settings')}<span>系统状态</span></a><button class="nav-item" data-action="help">${icon('help')}<span>使用指南</span></button></nav><div class="sidebar-bottom"><div class="environment-card"><div class="env-title"><i class="dot"></i><span id="environment-name">离线演示空间</span></div><p id="environment-note">无需 API 额度，先让想法跑起来。</p></div><div class="profile"><span class="avatar">Y</span><span><strong>${esc(state.user?.display_name || '访客工作空间')}</strong><small>${state.user ? '账号资料跨设备同步' : '登录账号，接续你的资料'}</small></span>${icon('spark')}</div></div></aside><div class="main-shell"><header class="topbar"><div class="breadcrumb"><button class="icon-btn mobile-menu" data-action="toggle-menu" aria-label="展开导航" aria-expanded="false">${icon('menu')}</button><span>我的工作空间</span>${icon('chevron')}<span class="current" id="breadcrumb-current">工作台</span></div><div class="topbar-actions"><button class="btn secondary small account-button" data-action="account" aria-label="账号与同步">${icon('lock')}<span>${esc(state.user?.display_name || '登录同步')}</span></button><button class="command-button" data-action="command" aria-label="搜索页面和任务">${icon('search')}<span>快速查找</span><kbd class="kbd">Ctrl K</kbd></button><label class="mode-picker"><i class="dot"></i><select id="mode-select" aria-label="运行模式"><option value="demo">离线演示</option><option value="live">真实模型</option></select>${icon('down')}</label><button class="icon-btn" data-action="help" aria-label="使用帮助">${icon('help')}</button></div></header><main id="main" class="main" tabindex="-1"></main></div></div>`;
  $('#mode-select').value = state.mode;
  updateEnvironment();
}

function updateEnvironment() {
  $('#environment-name').textContent = state.mode === 'demo' ? '离线演示空间' : '真实模型空间';
  $('#environment-note').textContent = state.mode === 'demo' ? '无需 API 额度，先让想法跑起来。' : '使用服务端模型配置，按实际调用消耗额度。';
  $('#run-count').textContent = state.workspace.runs.length;
}

const footer = () => `<footer class="footer-note"><span>${icon('lock')}${state.user ? '账号资料保存在此服务器，可跨设备登录使用' : '访客资料保存在此浏览器空间；登录后可导入账号'}</span><span>BUILT FOR IDEAS. DESIGNED TO EVOLVE.</span></footer>`;

function home() {
  const demo = state.mode === 'demo';
  $('#main').innerHTML = `<div class="page"><section class="hero-intro"><div><div class="eyebrow"><i class="dot"></i> YOUR MULTI-AGENT WORKSPACE</div><h1>一个想法，<em>一支 AI 团队。</em></h1><p>让智能体分工、探索与验证，把你的下一个想法变成可交付的成果。</p></div><div class="hero-annotation"><span>Less busywork. More possibility.</span><span class="annotation-line"></span></div></section><div class="launch-grid"><form class="launch-card" id="task-form"><div class="composer-heading">${icon('spark')}今天，一起完成什么？<span class="pill">${demo ? 'DEMO WORKFLOW' : 'LIVE WORKFLOW'}</span></div><label class="sr-only" for="goal">任务目标</label><textarea class="goal-input" id="goal" maxlength="12000" ${demo ? 'readonly' : ''} placeholder="描述你的目标，剩下的交给团队。&#10;例如：基于我的资料，研究文本相似度并生成一份有来源的报告。">${demo ? '研究文本相似度，检索证据、协作整理，\n生成一份带来源引用的 Markdown 报告。' : esc(state.goal)}</textarea><div class="attachment-list" id="attachments"></div>${demo ? '' : taskKnowledgeSelector()}<div class="composer-settings"><label class="checkbox-label"><input class="toggle" type="checkbox" id="memory-toggle" ${state.memory ? 'checked' : ''}>经验记忆</label><label class="checkbox-label"><input class="toggle" type="checkbox" id="review-toggle" ${state.review ? 'checked' : ''}>交付前由我确认</label></div><div class="composer-bottom"><button type="button" class="composer-attachment" data-action="attach" ${demo ? 'disabled title="自定义附件需要真实模型模式"' : ''}>${icon('attach')}添加资料 <span>任意格式</span></button><button class="btn primary" type="submit" id="launch-button">${icon(demo ? 'play' : 'spark')}${demo ? '启动协作演示' : '启动任务'}${icon('arrow')}</button></div><span class="composer-footnote">${demo ? '固定教学任务 · 不调用真实模型 · 真实执行检索与文件交接' : '真实模型调用将消耗 API 额度 · Ctrl + Enter 快速启动'}</span><input type="file" id="task-files" multiple hidden></form>${graph()}</div><section class="quick-start"><div class="section-heading"><h2>从一个灵感开始</h2><span>选择场景，快速开始你的第一次协作</span></div><div class="template-grid"><button class="template" data-action="template" data-template="research">${agentIcon('researcher')}<span><h3>有据可循的研究</h3><p>检索资料，生成带引用的报告</p></span>${icon('arrow')}</button><button class="template" data-action="template" data-template="code">${agentIcon('coder')}<span><h3>从想法到代码</h3><p>拆解需求，实现并验证功能</p></span>${icon('arrow')}</button><button class="template" data-action="template" data-template="review">${agentIcon('verifier')}<span><h3>让交付多一份把握</h3><p>追溯证据，由你确认最终成果</p></span>${icon('arrow')}</button></div></section><section class="activity-section"><div class="section-heading"><h2>最近的协作 <small id="home-run-count">${String(state.workspace.runs.length).padStart(2, '0')}</small></h2><a class="text-link" href="#runs">全部任务 ${icon('arrow')}</a></div><div class="panel" id="recent-runs">${runsTable(state.workspace.runs.slice(0, 4))}</div></section><section class="team-strip" aria-label="你的 AI 团队">${['coordinator', 'researcher', 'coder', 'verifier'].map(id => `<button class="team-strip-card" data-action="memory" data-agent="${id}">${agentIcon(id)}<span><strong>${roles[id].name}</strong><small>${roles[id].description}</small></span><span class="memory-link">${state.workspace.agents.find(a => a.id === id)?.memory_count || 0} 条经验</span></button>`).join('')}</section>${footer()}</div>`;
  renderAttachments();
}

function historyPage() {
  $('#main').innerHTML = `<div class="page"><div class="page-title"><div><div class="eyebrow">YOUR COLLABORATION HISTORY</div><h1>每一步，都有迹可循。</h1><p>回到任何一次协作，查看分工、决策与最终交付。</p></div><a class="btn primary" href="#home">${icon('plus')}新建任务</a></div><div class="filters"><div class="tabs" role="group" aria-label="任务状态筛选">${[['all', '全部任务'], ['succeeded', '已完成'], ['waiting_human', '待确认'], ['failed', '未完成']].map(([id, name]) => `<button class="tab ${state.filter === id ? 'active' : ''}" data-action="filter" data-filter="${id}">${name}</button>`).join('')}</div><label class="filter-input">${icon('search')}<input id="run-query" placeholder="搜索任务目标…" aria-label="搜索任务" value="${esc(state.query)}"></label></div><div class="panel" id="history-table"></div>${footer()}</div>`;
  filterRuns();
}
function filterRuns() {
  const runs = state.workspace.runs.filter(r => (state.filter === 'all' || r.status === state.filter) && r.goal.toLowerCase().includes(state.query.toLowerCase()));
  $('#history-table').innerHTML = runsTable(runs, !!state.query || state.filter !== 'all');
  $$('.tab').forEach(el => el.classList.toggle('active', el.dataset.filter === state.filter));
}

function errorDetail(message) {
  if (message?.includes('HTTP 402')) return '模型账户余额不足（HTTP 402）。请为服务端配置有额度的课程密钥，或切换到离线演示。已有记录会保留。';
  if (message?.includes('HTTP 401')) return '模型密钥验证失败（HTTP 401）。请检查服务端 .env 中的密钥配置。';
  if (message?.includes('HTTP 404')) return '模型或接口不可用（HTTP 404）。请检查服务端配置中的模型名称与接口地址。';
  return message;
}

function artifactPanels(run) {
  const groups = run.artifact_groups || {unknown: run.artifacts};
  const names = {deliverable: '交付文件', process: '过程资料', history: '历史版本', unknown: '历史文件／用途未确认'};
  return Object.entries(names).map(([kind, title]) => {
    const files = groups[kind] || [];
    if (!files.length && kind !== 'deliverable') return '';
    const rows = files.map(a => `<div class="artifact-item" data-artifact-id="${esc(a.id)}">${icon('file')}<button class="artifact-name" data-action="artifact" data-id="${esc(a.id)}"><strong>${esc(a.display_name || a.path)}</strong><small>${esc(a.summary || '')}</small><small>原文件：${esc(a.path)} · ${esc(a.task_title || a.task_id)}</small><small>${esc(a.status_label || '用途未确认')} · ${fileSize(a.bytes)}</small><small>${kind === 'deliverable' ? `交付顺序 ${a.delivery_order} · ` : ''}生成时间：${a.created ? date(a.created) : '时间未知'}</small></button><a class="icon-btn" href="${apiPath(`/api/runs/${run.id}/artifacts/${a.id}?download=true`)}" download aria-label="下载 ${esc(a.display_name || a.path)}">${icon('download')}</a></div>`).join('');
    const body = `<div class="artifact-list">${rows || '<div class="artifact-empty">尚无已声明的交付文件；其他文件可在下方查看。</div>'}</div>`;
    if (kind === 'deliverable') return `<section class="panel" data-artifact-group="deliverable"><div class="panel-header"><span>${icon('file')}${title}</span><small>${files.length} FILES</small></div>${body}</section>`;
    return `<details class="panel artifact-section" data-artifact-section="${kind}" data-artifact-group="${kind}"><summary class="panel-header"><span>${icon('arrow', 'artifact-toggle')}${title}</span><small>${files.length} FILES</small></summary>${body}</details>`;
  }).join('');
}

function retryLinks(run) {
  return run.attempt_number > 1 ? `<p class="subtext">第 ${esc(run.attempt_number)} 次运行</p>` : '';
}
function showCurrentAttempt(run) {
  if (!run.replaced_by) return false;
  location.replace(`#task/${run.replaced_by}`);
  return true;
}

const retryingRuns = new Set();

function detailPage(live = false) {
  const run = state.run;
  if (!run) return;
  const focused = document.activeElement?.id;
  const reply = $('#human-reply')?.value || '';
  const openTasks = $$('details.task-item[open]').map(el => el.dataset.task);
  const openEvents = $$('details[data-event][open]').map(el => el.dataset.event);
  const openArtifacts = $('#main').dataset.artifactRun === run.id ? $$('details[data-artifact-section][open]').map(el => el.dataset.artifactSection) : [];
  const timelineScroll = $('.timeline')?.scrollTop || 0;
  const terminal = ['succeeded', 'failed', 'cancelled'].includes(run.status);
  const approvals = run.pending && ['acceptance', 'tool_approval'].includes(run.pending.kind);
  const review = run.pending ? `<section class="review-card"><div class="review-title">${icon('shield')}这里，需要你的判断</div><p>${esc(run.pending.question)}</p>${run.pending.prepared ? `<details><summary>查看操作对象、版本与范围</summary><pre>${esc(JSON.stringify(run.pending.prepared, null, 2))}</pre></details>` : ''}${approvals ? `<div class="review-buttons"><button class="btn primary small" data-action="approve" data-reply="yes" ${run.executing ? 'disabled' : ''}>${icon('check')}确认通过</button><button class="btn secondary small" data-action="approve" data-reply="no" ${run.executing ? 'disabled' : ''}>暂不接受</button></div>` : `<form id="reply-form">${questionOptions(run.pending)}<label class="sr-only" for="human-reply">补充信息</label><textarea id="human-reply" maxlength="8000" required placeholder="一句话即可，也可点击上方选项…">${esc(reply)}</textarea><button class="btn primary small" type="submit" ${run.executing ? 'disabled' : ''}>提交并继续 ${icon('arrow')}</button></form>`}</section>` : '';
  const markup = `<div class="page task-page" data-run-id="${esc(run.id)}"><div id="task-heading"><a class="text-link" href="#runs">← 返回任务记录</a><div class="detail-heading"><div><div class="detail-meta">${badge(run.status)}<span class="pill">${run.mode === 'demo' ? 'DEMO · 无真实模型调用' : 'LIVE · 真实模型'}</span></div><h1>${esc(run.goal)}</h1><div class="detail-meta"><span class="mono">${esc(run.id)}</span><span>${date(run.created, true)}</span><span>经验记忆${run.memory_enabled ? '已开启' : '已关闭'}</span></div></div><div class="detail-actions">${run.can_retry ? `<button class="btn primary small" data-action="retry-run" data-retry-id="${esc(run.id)}" ${retryingRuns.has(run.id) ? 'disabled' : ''}>${retryingRuns.has(run.id) ? '重新运行中…' : '重新运行'}</button>` : ''}<a class="btn secondary small" href="${apiPath(`/api/runs/${run.id}/report`)}" download>${icon('download')}导出记录</a>${run.can_retry_actions ? '<button class="btn secondary small" data-action="retry-actions">继续失败任务</button>' : ''}${run.can_repair ? '<button class="btn secondary small" data-action="repair-run">修复并复验</button>' : ''}${!terminal && !run.pending && !run.executing ? '<button class="btn primary small" data-action="continue">继续任务</button>' : ''}${!terminal ? `<button class="btn secondary small" data-action="cancel" ${run.cancelling ? 'disabled' : ''}>${icon('stop')}${run.cancelling ? '正在停止…' : '停止'}</button>` : ''}</div></div>${run.failure ? `<div class="notice error run-message">${icon('info')}<p>${esc(errorDetail(run.failure))}</p></div>` : ''}${run.mode === 'demo' ? '<div class="notice run-message">离线演示采用固定的模型决策；检索、文件交接、归档与验收均实际执行。演示结果不代表真实模型性能。</div>' : ''}</div><div class="detail-grid"><div class="detail-main">${retryLinks(run)}${review}${revisionPanel(run)}${sourceFiles(run)}<section class="panel"><div class="metric-bar"><div class="metric"><strong>${run.completed_tasks}<span class="metric-slash">/ ${run.task_count}</span></strong><span>子任务完成</span></div><div class="metric"><strong>${run.deliverable_count ?? 0}</strong><span>交付文件</span></div><div class="metric"><strong>${seconds(run.active_seconds)}</strong><span>累计活跃执行</span></div><div class="metric"><strong>${run.model_calls}</strong><span>${run.mode === 'demo' ? '模拟决策次数' : '模型请求次数'}</span></div></div><div id="task-list">${tasks(run)}</div><div class="detail-tab-note">验收范围为任务声明的检查项；代码测试结果与引用的事实准确性需要分别审阅。</div></section><section class="panel"><div class="panel-header"><span>${icon('activity')}协作轨迹</span><button class="text-link" data-action="verbose">${state.verbose ? '显示关键事件' : '包含全部决策'}</button></div><div id="timeline">${timeline(state.events, state.verbose, run.team_snapshot)}</div></section></div><aside class="detail-side"><section id="collaboration-network"></section><div id="artifact-panels">${artifactPanels(run)}</div></aside></div><div class="stream-status" id="stream-status"><i class="dot"></i>${run.executing ? '正在同步执行状态' : run.pending ? '已暂停，等待你的回复' : terminal ? '记录已保存' : '执行已中断，可继续任务'}</div>${footer()}</div>`;
  const current = $('.task-page');
  if (current?.dataset.runId === run.id && $('#collaboration-network')) {
    // Keep the network and its ancestors connected: SSE must not restart node animations.
    const next = document.createElement('template'); next.innerHTML = markup;
    for (const selector of ['#task-heading', '.detail-main', '#artifact-panels']) {
      const target = $(selector, current), replacement = next.content.querySelector(selector);
      target.replaceChildren(...replacement.childNodes);
    }
    $('#stream-status').replaceChildren(...next.content.querySelector('#stream-status').childNodes);
  } else {
    destroyNetwork($('#collaboration-network'));
    $('#main').innerHTML = markup;
  }
  updateNetwork($('#collaboration-network'), run, state.events, {live});
  openTasks.forEach(id => { const item = $$('details.task-item').find(el => el.dataset.task === id); if (item) item.open = true; });
  $('#main').dataset.artifactRun = run.id;
  openArtifacts.forEach(id => { const item = $$('details[data-artifact-section]').find(el => el.dataset.artifactSection === id); if (item) item.open = true; });
  openEvents.forEach(id => { const item = $$('details[data-event]').find(el => el.dataset.event === id); if (item) item.open = true; });
  if ($('.timeline')) $('.timeline').scrollTop = timelineScroll;
  if (focused === 'human-reply') { $('#human-reply')?.focus({ preventScroll: true }); }
}

function connectRun(runId, generation) {
  state.stream?.close();
  const after = state.events.at(-1)?.seq || 0;
  const source = new EventSource(apiPath(`/api/runs/${runId}/stream?after=${after}`));
  state.stream = source;
  source.addEventListener('snapshot', event => {
    if (generation !== state.generation) { source.close(); return; }
    const snapshot = JSON.parse(event.data);
    if (showCurrentAttempt(snapshot.run)) { source.close(); return; }
    state.run = snapshot.run;
    const seen = new Set(state.events.map(e => e.seq));
    state.events.push(...snapshot.events.filter(e => !seen.has(e.seq)));
    setNetworkConnected($('#collaboration-network'), true);
    detailPage(true);
  });
  source.addEventListener('idle', () => {
    source.close();
    refreshWorkspace().catch(() => {});
  });
  source.addEventListener('expired', () => { source.close(); changeSession(false); });
  source.onerror = () => {
    if (generation === state.generation) setNetworkConnected($('#collaboration-network'), false);
    if (generation === state.generation && $('#stream-status')) $('#stream-status').textContent = '连接暂时中断，正在重新连接…';
  };
}

function baseOptions(selected = state.baseId) {
  return state.bases.map(b => `<option value="${esc(b.id)}" ${b.id === selected ? 'selected' : ''}>${esc(b.name)}</option>`).join('');
}
function taskKnowledgeSelector() {
  const chosen = state.selectedBases ?? state.bases.map(b => b.id);
  return `<details class="task-knowledge"><summary>${icon('book')} 使用我的知识库 <span>${chosen.length} 个已选</span></summary><p>仅从选中的知识库检索；取消全部可关闭本次知识检索。</p><div>${state.bases.map(b => `<label><input type="checkbox" data-base="${esc(b.id)}" ${chosen.includes(b.id) ? 'checked' : ''}><span>${esc(b.name)}<small>${b.documents} 份资料 · ${esc(b.description || '自定义资料')}</small></span></label>`).join('')}</div><a class="text-link" href="#knowledge">管理知识库 →</a></details>`;
}
function revisionPanel(run) {
  if (!run.revisions?.length) return '';
  const last = run.revisions.at(-1);
  return `<section class="notice revision-notice">${icon('refresh')}<div><strong>已进入第 ${run.revisions.length} 轮修订</strong><p>退回 ${esc(last.target_task)}，保留原验收条件与测试。修订后重新交接并验证。</p><details><summary>查看返修依据</summary><pre>${esc(last.reason)}</pre></details></div></section>`;
}
function knowledgePage() {
  const current = state.bases.find(b => b.id === state.baseId);
  $('#main').innerHTML = `<div class="page"><div class="page-title"><div><div class="eyebrow">YOUR KNOWLEDGE, YOUR CONTEXT</div><h1>给团队，一点背景。</h1><p>构建自己的资料库，让每次协作从你的知识出发。</p></div><button class="btn secondary" data-action="base-editor">${icon('plus')}新建知识库</button></div><div class="sync-strip">${icon(state.user ? 'check' : 'lock')}<span>${state.user ? '账号空间 · 与服务端同步，页面每 12 秒检查更新' : '当前为访客空间。登录账号，让资料在不同设备间延续。'}</span><button class="text-link" data-action="${state.user ? 'sync-knowledge' : 'account'}">${state.user ? '立即同步' : '登录同步'} ${icon('arrow')}</button></div><section class="base-toolbar panel"><label class="field"><span>我的知识库</span><select id="active-base">${baseOptions()}</select></label><div class="base-description"><strong>${current?.documents || 0} 份资料</strong><p>${esc(current?.description || '上传资料，补充来源，并在任务中选择这个知识库。')}</p></div><button class="icon-btn" data-action="edit-base" aria-label="编辑知识库">${icon('settings')}</button><button class="btn danger small base-delete" data-action="delete-base">${icon('trash')}${state.baseId === 'kb_default' ? '清空知识库' : '删除知识库'}</button></section><div class="knowledge-grid"><section><button class="upload-zone" id="upload-zone" data-action="upload">${icon('upload')}<strong>拖放文件到这里，或点击上传</strong><small>任意格式 · 不设固定大小上限 · 支持多文件上传</small></button><input type="file" id="knowledge-file" multiple hidden><div class="knowledge-document-heading"><h2>库内资料</h2><button class="text-link" data-action="document-editor">${icon('plus')}新建文档</button></div><div class="document-list panel" id="document-list">${documentList()}</div></section><form class="panel search-panel" id="search-form"><h2>试一试，检索你的知识库</h2><p>仅检索当前知识库，再按 Agent 的访问权限筛选。</p><label class="field"><span>检索内容</span><input id="knowledge-query" placeholder="例如：如何处理空文本？" maxlength="2000" required></label><label class="field"><span>以谁的视角检索</span><select id="search-agent">${['researcher', 'coordinator', 'coder', 'verifier'].map(a => `<option value="${a}">${roles[a].name}</option>`).join('')}</select></label><button class="btn primary" type="submit">${icon('search')}检索证据</button><div id="search-results"></div><p class="knowledge-hint">来源 URL 可在文档设置中填写。内部资料无需虚构公开链接。</p></form></div>${footer()}</div>`;
  const drop = $('#upload-zone');
  drop.addEventListener('dragover', event => { event.preventDefault(); drop.classList.add('dragging'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('dragging'));
  drop.addEventListener('drop', event => { event.preventDefault(); drop.classList.remove('dragging'); const files = [...event.dataTransfer.files]; if (files.length) documentEditor(files); });
}
function documentList() {
  const docs = state.docs.filter(d => d.base_id === state.baseId);
  return docs.length ? docs.map(doc => `<div class="document-row">${icon('file')}<div class="doc-info"><strong>${esc(doc.name)}</strong><small>${doc.chunks} 个片段 · ${doc.bytes != null ? fileSize(doc.bytes) + ' · ' : ''}版本 ${esc(doc.version.slice(0, 8))}</small>${doc.description ? `<p class="doc-description">${esc(doc.description)}</p>` : ''}${doc.source_url ? `<a class="text-link source-link" href="${esc(doc.source_url)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : ''}${doc.status === 'stored' ? `<p class="document-warning">${esc(doc.detail)}</p>` : ''}</div><span class="pill ${doc.status === 'stored' ? 'amber' : 'green'}">${doc.status === 'stored' ? '仅保存原件' : '已索引'}</span><span class="pill">${doc.scope === 'shared' ? '团队共享' : esc(roles[doc.scope]?.name || doc.scope)}</span><div class="document-actions"><button class="icon-btn" data-action="edit-document" data-id="${esc(doc.id)}" aria-label="编辑 ${esc(doc.name)}">${icon('settings')}</button>${doc.upload_id ? `<a class="icon-btn" href="${apiPath(`/api/uploads/${doc.upload_id}/download`)}" download aria-label="下载原件 ${esc(doc.name)}">${icon('download')}</a>` : ''}<button class="icon-btn" data-action="delete-document" data-id="${esc(doc.id)}" aria-label="移除 ${esc(doc.name)}">${icon('trash')}</button></div></div>`).join('') : empty('把你的资料，放在一起', '上传文档或手动记录知识，在任务中选择使用这个库。', 'book');
}
function documentFields(doc = {}) {
  return `<label class="field"><span>所属知识库</span><select id="document-base">${baseOptions(doc.base_id || state.baseId)}</select></label><label class="field"><span>访问范围</span><select id="document-scope">${['shared', 'coordinator', 'researcher', 'coder', 'verifier'].map(a => `<option value="${a}" ${(doc.scope || 'shared') === a ? 'selected' : ''}>${a === 'shared' ? '团队共享 · 全部 Agent 可检索' : '仅 ' + roles[a].name}</option>`).join('')}</select></label><label class="field"><span>来源 URL（可选）</span><input id="document-source" type="url" maxlength="2048" placeholder="https://example.com/research" value="${esc(doc.source_url || '')}"></label><label class="field"><span>资料说明（可选）</span><textarea id="document-description" rows="2" maxlength="2000" placeholder="资料的用途、适用范围或可信度说明">${esc(doc.description || '')}</textarea></label>`;
}
function documentEditor(files = []) {
  pendingDocumentFiles = files;
  const uploading = files.length > 0;
  openModal(uploading ? `上传 ${files.length} 份资料` : '添加知识文档', `<form id="document-form">${uploading ? `<div class="upload-file-list">${files.map(file => `<div>${icon('file')}<span>${esc(file.name)}</span><small>${fileSize(file.size)}</small></div>`).join('')}</div>` : '<label class="field"><span>文档名称</span><input id="document-name" placeholder="research-notes.md" maxlength="200" required></label>'}${documentFields()}${uploading ? '<p class="subtext">常见文档自动提取正文；无法解析的文件保留原件。同批资料共用上述来源信息，也可上传后单独编辑。</p>' : '<label class="field"><span>文档内容</span><textarea id="document-content" rows="9" required placeholder="粘贴你的资料…"></textarea></label>'}<p class="subtext">同知识库、同名且同访问范围的文档会更新为新版本。</p><div id="upload-progress" class="upload-progress" role="status" aria-live="polite"></div><div class="modal-actions"><button class="btn secondary" type="button" data-action="close-modal">取消</button><button class="btn primary" type="submit">${icon('upload')}加入知识库</button></div></form>`);
}
function baseEditor(base = null) {
  openModal(base ? '编辑知识库' : '新建知识库', `<form id="base-form" data-id="${esc(base?.id || '')}"><label class="field"><span>知识库名称</span><input id="base-name" maxlength="80" value="${esc(base?.name || '')}" placeholder="例如：TypeMate 市场研究" required></label><label class="field"><span>用途与说明</span><textarea id="base-description" maxlength="1000" rows="4" placeholder="告诉团队这个知识库包含什么…">${esc(base?.description || '')}</textarea></label><p class="subtext">知识库保存在${state.user ? '当前账号，可跨设备登录使用' : '当前浏览器访客空间'}；演示／真实模式各自独立。</p><div class="modal-actions"><button type="button" class="btn secondary" data-action="close-modal">取消</button><button type="submit" class="btn primary">保存知识库</button></div></form>`);
}
async function documentSettings(id) {
  const doc = await api(`/api/knowledge/${id}`);
  openModal('编辑资料 · ' + doc.name, `<form id="document-settings-form" data-id="${esc(id)}" data-revision="${esc(doc.revision)}">${documentFields(doc)}${doc.can_edit_text ? `<label class="field"><span>正文（保存后重新索引）</span><textarea id="edit-document-content" rows="10">${esc(doc.text)}</textarea></label>` : `<details class="document-preview"><summary>查看正文预览${doc.truncated ? '（部分内容）' : ''}</summary><pre>${esc(doc.text || doc.detail || '暂无正文')}</pre></details><p class="subtext">此文档可编辑说明、来源及访问范围；更新完整正文请重新上传原文件。</p>`}<div class="modal-actions"><button type="button" class="btn secondary" data-action="close-modal">取消</button><button type="submit" class="btn primary">保存资料</button></div></form>`);
}

const teams = teamUI({ state, api, apiPath, openModal, closeModal, toast, busy, refreshWorkspace });

function chatPath(channel = state.channel) {
  return `/api/chat?channel=${channel}${channel === 'group' && state.chatRun ? `&run_id=${encodeURIComponent(state.chatRun)}` : ''}`;
}
function chatName(agent) {
  const role = roles[agent];
  return role ? `${role.name}${role.label && agent !== 'group' ? ` · ${role.label}` : ''}` : agent;
}
function chatRunOptions() {
  return '<option value="">全部任务与对话</option>' + state.workspace.runs.map(run => `<option value="${esc(run.id)}" ${state.chatRun === run.id ? 'selected' : ''}>${esc(run.goal.slice(0, 70))} · ${date(run.created)}</option>`).join('');
}
function chatPage() {
  const filtered = state.channel === 'group' && !!state.chatRun;
  $('#main').innerHTML = `<div class="page chat-page">
    <div class="page-title"><div><div class="eyebrow">TEAM CONVERSATIONS</div><h1>和你的团队，聊一聊。</h1><p>跟进任务分工、资料交接与验证反馈，也可以单独与 Agent 交流。</p></div></div>
    ${teams.listBar()}<section class="panel chat-layout">
      <aside class="chat-channels" aria-label="会话列表"><h3>团队会话</h3>${['group', 'coordinator', 'researcher', 'coder', 'verifier'].map(a => `
        <button class="channel-button ${state.channel === a ? 'active' : ''}" data-action="channel" data-channel="${a}" aria-pressed="${state.channel === a}">
          ${agentIcon(a)}<span><strong>${roles[a].name}</strong><small>${a === 'group' ? '任务动态与共同讨论' : roles[a].label + ' · 私聊'}</small></span>
        </button>`).join('')}</aside>
      <div class="chat-room">
        <div class="chat-room-header"><div><strong>${esc(chatName(state.channel))}</strong><small>${state.channel === 'group' ? '4 位 Agent · 协作动态来自实际任务记录' : '仅你与当前 Agent 可见'} · ${state.mode === 'demo' ? 'DEMO' : 'LIVE'}</small></div><span class="chat-status">自动同步</span></div>
        ${state.channel === 'group' ? `<div class="chat-filter"><label for="chat-run-filter">查看</label><select id="chat-run-filter">${chatRunOptions()}</select><span>最近 200 条</span></div>` : ''}
        <div class="chat-feed"><div class="chat-messages" id="chat-messages" role="log" aria-label="对话消息" aria-live="polite" aria-relevant="additions"></div><button class="chat-new-message" data-action="chat-bottom" hidden>查看新消息 ↓</button></div>
        <form class="chat-composer" id="chat-form"><label class="sr-only" for="chat-input">消息</label><textarea id="chat-input" placeholder="${filtered ? '切换到全部任务与对话后发言' : '输入消息，与团队交流…'}" maxlength="8000" required ${state.sending || filtered ? 'disabled' : ''}></textarea><div class="chat-compose-actions"><span>Ctrl + Enter 发送</span><button type="submit" class="btn primary" aria-label="发送消息" ${state.sending || filtered ? 'disabled' : ''}>${state.sending ? '<span class="spinner"></span>' : '发送'}</button></div></form>
        <p class="chat-hint">${filtered ? '当前显示该任务的协作记录；点击消息下方链接可查看任务详情。' : state.mode === 'demo' ? '离线对话使用固定回复；执行动态来自实际发生的演示任务。' : '群聊由统筹 Agent 回复；分工、接收和交接卡片由执行记录生成。'} ${filtered ? '' : '新任务请从工作台发起。'}</p>
      </div>
    </section>${footer()}</div>`;
  renderChatMessages(true);
}
function chatMessages() {
  if (!state.messages.length) return empty(state.channel === 'group' ? '团队已就位' : `${roles[state.channel].name} 已就位`, state.chatRun && state.channel === 'group' ? '该任务暂时没有协作记录。' : '发起任务后，这里将显示分工、接收、交接与完成反馈。也可以先发送一条消息。', 'chat');
  let lastDay = '';
  return state.messages.map((message, index) => {
    const stamp = message.created ? new Date(message.created * 1000) : null;
    const day = stamp ? stamp.toLocaleDateString('zh-CN', {year: 'numeric', month: 'long', day: 'numeric'}) : '时间未知';
    const separator = day !== lastDay ? `<div class="chat-date">${esc(day)}</div>` : '';
    lastDay = day;
    const id = message.id || (message.seq ? `message:${message.seq}` : `pending:${index}`);
    return `${separator}<article class="message ${message.sender === 'human' ? 'human' : ''} ${message.source === 'execution' ? 'execution-message' : ''}" data-message-id="${esc(id)}" data-message-kind="${esc(message.kind || 'chat')}">
      ${agentIcon(message.sender)}<div class="message-body">
        <header class="message-heading"><strong>${esc(chatName(message.sender))}</strong>${message.recipient ? `<span class="message-recipient">→ ${esc(roles[message.recipient]?.name || message.recipient)}</span>` : ''}<time ${stamp ? `datetime="${stamp.toISOString()}" title="${esc(stamp.toLocaleString('zh-CN'))}"` : ''}>${stamp ? stamp.toLocaleTimeString('zh-CN', {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '时间未知'}</time></header>
        <div class="message-bubble">${message.label ? `<span class="message-kind">${esc(message.label)}</span>` : ''}<p>${esc(message.body)}</p>${message.pending ? '<small>正在发送…</small>' : ''}</div>
        ${message.run_id ? `<a class="chat-run-link" href="#task/${esc(message.run_id)}" title="${esc(message.run_goal)}">${icon('file')}查看任务 · ${esc(message.run_goal.slice(0, 55))}</a>` : ''}
      </div></article>`;
  }).join('');
}
function renderChatMessages(forceBottom = false) {
  const list = $('#chat-messages');
  if (!list) return;
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 70;
  const previousTop = list.scrollTop;
  const previousIds = new Set($$('[data-message-id]', list).map(el => el.dataset.messageId));
  const anchor = $$('[data-message-id]', list).find(el => el.getBoundingClientRect().bottom >= list.getBoundingClientRect().top);
  const anchorOffset = anchor ? anchor.getBoundingClientRect().top - list.getBoundingClientRect().top : 0;
  const anchorId = anchor?.dataset.messageId;
  list.innerHTML = chatMessages();
  const button = $('.chat-new-message');
  if (forceBottom || nearBottom) { list.scrollTop = list.scrollHeight; button.hidden = true; }
  else {
    const replacement = $$('[data-message-id]', list).find(el => el.dataset.messageId === anchorId);
    list.scrollTop = replacement ? list.scrollTop + replacement.getBoundingClientRect().top - list.getBoundingClientRect().top - anchorOffset : previousTop;
    if ($$('[data-message-id]', list).some(el => !previousIds.has(el.dataset.messageId))) button.hidden = false;
  }
}

function systemPage(info) {
  $('#main').innerHTML = `<div class="page"><div class="page-title"><div><div class="eyebrow">UNDER THE HOOD</div><h1>一切就绪，各就其位。</h1><p>查看服务配置与运行依赖。密钥始终保留在服务端。</p></div><button class="btn secondary" data-action="refresh-system">${icon('refresh')}刷新状态</button></div><div class="system-grid"><section class="panel system-card"><h2>${icon('spark')}模型与检索</h2>${setting('模型服务', info.provider)}${setting('模型名称', info.model)}${setting('API 密钥', info.api_key_present ? '已配置 · 未验证额度' : '尚未配置')}${setting('单任务调用上限', `${info.max_model_calls} 次`)}${setting('请求超时', `${info.api_timeout} 秒`)}${setting('联网检索', info.search_provider)}</section><section class="panel system-card"><h2>${icon('shield')}执行环境</h2>${setting('Docker 沙箱', info.docker.available ? '可用' : '暂不可用')}${setting('镜像', info.docker.image || '未检测到')}${setting('运行状态', info.docker.available ? 'ready' : info.docker.reason)}${setting('Git', info.git ? '已安装' : '未安装')}${setting('数据空间', state.user ? '当前账号 · 跨设备同步' : '当前浏览器 · 访客空间')}${setting('默认访问', '本地服务 · 同源接口')}</section><div class="notice system-note">${icon('info')}<p>以上检查不发送模型请求，也不验证账户余额。真实任务需要有效额度；代码执行需要 Docker。配置通过服务端 <code>.env</code> 管理，修改后重启 Web 服务生效。离线演示无需模型与 Docker。</p></div></div>${footer()}</div>`;
}
function setting(label, value) { return `<div class="setting-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`; }

async function refreshWorkspace() {
  const mode = state.mode;
  const epoch = state.identityEpoch;
  const data = await api('/api/workspace');
  if (mode !== state.mode || epoch !== state.identityEpoch) return;
  const previousBases = JSON.stringify(state.bases);
  const previousRuns = JSON.stringify(state.workspace.runs.map(r => [r.id, r.goal]));
  state.workspace = data; state.bases = data.knowledge_bases || [];
  const chatFilter = $('#chat-run-filter');
  if (chatFilter && previousRuns !== JSON.stringify(data.runs.map(r => [r.id, r.goal]))) chatFilter.innerHTML = chatRunOptions();
  if (state.selectedBases !== null) state.selectedBases = state.selectedBases.filter(id => state.bases.some(base => base.id === id));
  const selector = $('.task-knowledge');
  if (selector && previousBases !== JSON.stringify(state.bases)) {
    const expanded = selector.open;
    selector.outerHTML = taskKnowledgeSelector();
    $('.task-knowledge').open = expanded;
  }
  if (!state.bases.some(b => b.id === state.baseId)) state.baseId = state.bases[0]?.id || 'kb_default';
  if ($('#run-count')) updateEnvironment();
}

async function route() {
  destroyNetwork($('#collaboration-network'));
  teams.stop();
  const generation = ++state.generation;
  state.stream?.close(); state.stream = null;
  const [name = 'home', id] = location.hash.replace(/^#/, '').split('/');
  state.route = ['home', 'runs', 'knowledge', 'chat', 'system', 'task', 'team'].includes(name) ? name : 'home';
  $$('.nav-item[data-nav]').forEach(el => { const active = el.dataset.nav === (state.route === 'task' ? 'runs' : state.route === 'team' ? 'chat' : state.route); el.classList.toggle('active', active); active ? el.setAttribute('aria-current', 'page') : el.removeAttribute('aria-current'); });
  $('#breadcrumb-current').textContent = state.route === 'task' ? '任务详情' : state.route === 'team' ? '团队群聊' : nav.find(n => n[0] === state.route)?.[2] || '系统状态';
  $('.sidebar').classList.remove('is-open'); $('.mobile-overlay').classList.remove('is-open');
  $('.mobile-menu').setAttribute('aria-expanded', 'false');
  document.title = `${$('#breadcrumb-current').textContent} · EvoTask`;
  if (state.route === 'home') { home(); return; }
  if (state.route === 'runs') { historyPage(); return; }
  $('#main').innerHTML = loading();
  try {
    if (state.route === 'team') {
      await teams.show(id);
    } else if (state.route === 'task') {
      const [run, events] = await Promise.all([api(`/api/runs/${encodeURIComponent(id || '')}`), api(`/api/runs/${encodeURIComponent(id || '')}/events`)]);
      if (generation !== state.generation) return;
      if (showCurrentAttempt(run)) return;
      state.run = run; state.events = events; detailPage(); connectRun(run.id, generation);
    } else if (state.route === 'knowledge') {
      const docs = await api('/api/knowledge'); if (generation !== state.generation) return;
      state.docs = docs; await refreshWorkspace(); knowledgePage();
    } else if (state.route === 'chat') {
      await refreshWorkspace();
      if (generation !== state.generation) return;
      const messages = await api(chatPath()); if (generation !== state.generation) return;
      state.messages = messages; chatPage();
    } else if (state.route === 'system') {
      const info = await api('/api/system'); if (generation !== state.generation) return;
      systemPage(info);
    }
  } catch (error) { if (generation === state.generation) $('#main').innerHTML = empty('暂时无法载入', error.message, 'info', '<button class="btn secondary" data-action="retry">重新连接</button>'); }
}

function help() {
  openModal('从想法到交付，只需四步', `<div class="help-step"><span>01</span><div><strong>先体验一次协作</strong><p>在「离线演示」模式启动固定教学任务，观察研究、委派、检索和生成报告的全过程。无需 API 额度。</p></div></div><div class="help-step"><span>02</span><div><strong>把你的资料交给团队</strong><p>在知识库上传 PDF、Word、Excel、PPT、文本等资料，选择团队共享或指定 Agent 的访问范围。</p></div></div><div class="help-step"><span>03</span><div><strong>连接真实模型，提出你的目标</strong><p>切换顶部「真实模型」。服务端需要配置有效的 DeepSeek 或课程 HKGAI 密钥。任务和对话会消耗额度；不同模式的数据独立保存。</p></div></div><div class="help-step"><span>04</span><div><strong>审阅证据，确认最终交付</strong><p>在任务详情预览、下载产物。开启人工确认后，团队等待你的批准再结束。失败记录和部分产物也会保留。</p></div></div><div class="notice">${icon('command')}<p>Ctrl / ⌘ + K 快速查找 · Ctrl / ⌘ + Enter 提交 · Esc 关闭弹窗<br>点击顶部「登录同步」创建账号；在其他设备打开同一个服务器地址并登录，即可继续使用资料。访客资料可在账号面板导入。</p></div>`);
}

function commandPalette() {
  openModal('快速查找', '<label class="command-search">' + icon('search') + '<input id="command-input" placeholder="跳转页面，或查找一个任务…" autocomplete="off" aria-label="搜索页面和任务"><kbd class="kbd">Esc</kbd></label><div class="command-results" id="command-results"></div>', 'command-modal');
  $('.modal-body', modal).style.padding = '0';
  commandResults(''); $('#command-input').focus();
}
function commandResults(query) {
  const pages = [...nav, ['system', 'settings', '系统状态']].filter(n => n[2].toLowerCase().includes(query.toLowerCase()));
  const runs = state.workspace.runs.filter(r => r.goal.toLowerCase().includes(query.toLowerCase())).slice(0, 8);
  $('#command-results').innerHTML = [...pages.map(([id, symbol, label]) => `<a class="command-result" href="#${id}" data-action="close-modal">${icon(symbol)}${label}<small>页面</small></a>`), ...runs.map(r => `<a class="command-result" href="#task/${esc(r.id)}" data-action="close-modal">${icon('file')}<span>${esc(r.goal.slice(0, 40))}</span><small>任务</small></a>`)].join('') || '<div class="filter-empty">没有找到对应内容</div>';
}

function fileSize(bytes = 0) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index++; }
  return `${bytes.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function uploadFile(file, purpose, scope = 'shared', progress = () => {}, metadata = {}) {
  const epoch = state.identityEpoch;
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    activeUploads.add(request);
    request.onloadend = () => activeUploads.delete(request);
    const params = new URLSearchParams({ name: file.name, purpose, scope, ...metadata });
    request.open('POST', apiPath('/api/uploads?' + params.toString()));
    request.setRequestHeader('X-EvoTask-CSRF', state.csrf);
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.upload.onprogress = event => progress(event.lengthComputable ? Math.round(event.loaded / event.total * 100) : null);
    request.onload = () => {
      if (epoch !== state.identityEpoch) { reject(new Error('登录身份已变化，请重新选择资料。')); return; }
      let result;
      try { result = JSON.parse(request.responseText); }
      catch { reject(new Error('上传服务未返回有效结果，请重试。')); return; }
      if (['account_expired', 'identity_changed'].includes(result.code)) changeSession(false);
      request.status >= 200 && request.status < 300 ? resolve(result) : reject(new Error(result.detail || `上传失败（${request.status}）`));
    };
    request.onerror = () => reject(new Error('上传连接中断，请检查网络后重试。'));
    request.onabort = () => reject(new Error('上传已取消。'));
    request.send(file);
  });
}
function renderAttachments() {
  if ($('#attachments')) $('#attachments').innerHTML = state.files.map((file, index) => `<span class="attachment-chip">${esc(file.name)} · ${fileSize(file.size)}<button type="button" data-action="remove-attachment" data-index="${index}" aria-label="移除附件 ${esc(file.name)}">×</button></span>`).join('');
}

async function refreshRun() {
  const run = state.run, generation = state.generation;
  const updated = await api(`/api/runs/${run.id}`);
  if (generation !== state.generation) return;
  if (showCurrentAttempt(updated)) return;
  state.run = updated; detailPage(); connectRun(run.id, generation);
}

document.addEventListener('click', async event => {
  const row = event.target.closest('[data-run]');
  if (row && !event.target.closest('[data-action],a,button')) { location.hash = `task/${row.dataset.run}`; return; }
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  if (action.startsWith('account')) return account.action(button);
  if (action === 'sync-knowledge') return busy(button, async () => { await refreshKnowledge(); toast('已同步服务端最新资料。'); });
  if (action === 'close-modal') { if (busyCount && $('#document-form', modal)) { toast('正在上传和建立索引，请等待完成。'); return; } return closeModal(); }
  if (action === 'help') return help();
  if (action === 'command') return commandPalette();
  if (action === 'retry' || action === 'refresh-system') return route();
  if (action === 'toggle-menu') { const open = $('.sidebar').classList.toggle('is-open'); $('.mobile-overlay').classList.toggle('is-open', open); $('.mobile-menu').setAttribute('aria-expanded', String(open)); return; }
  if (action === 'filter') { state.filter = button.dataset.filter; filterRuns(); return; }
  if (action === 'verbose') { state.verbose = !state.verbose; detailPage(); return; }
  if (action === 'attach') return $('#task-files').click();
  if (action === 'remove-attachment') { state.files.splice(Number(button.dataset.index), 1); renderAttachments(); return; }
  if (action === 'upload') return $('#knowledge-file').click();
  if (action === 'document-editor') return documentEditor();
  if (action === 'base-editor') return baseEditor();
  if (action === 'edit-base') return baseEditor(state.bases.find(b => b.id === state.baseId));
  if (action === 'edit-document') return busy(button, () => documentSettings(button.dataset.id));
  if (action === 'delete-base') return busy(button, async () => {
    const base = await api(`/api/knowledge/bases/${state.baseId}`);
    const verb = base.is_default ? '清空' : '删除';
    openModal(`${verb}知识库`, `<div class="notice error">${icon('trash')}<p>将${verb}「${esc(base.name)}」，永久移除其中 <strong>${base.documents} 份资料</strong>的原件及检索索引。此操作不可撤销。</p></div>${base.document_names.length ? `<ul class="deletion-preview">${base.document_names.map(name => `<li>${esc(name)}</li>`).join('')}${base.documents > base.document_names.length ? `<li>另有 ${base.documents - base.document_names.length} 份资料</li>` : ''}</ul>` : ''}<p class="subtext">${base.is_default ? '默认知识库本身会保留，清空后可以继续添加资料。' : '知识库将从列表和新任务的可选资料中移除。'}已有任务记录和已归档产物保留。</p><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn danger" data-action="confirm-delete-base" data-id="${esc(base.id)}" data-revision="${esc(base.revision)}" data-mode="${state.mode}">确认${verb}知识库</button></div>`);
  });
  if (action === 'confirm-delete-base') return busy(button, async () => {
    if (button.dataset.mode !== state.mode) throw new Error('运行模式已切换，请重新确认。');
    const result = await api(`/api/knowledge/bases/${button.dataset.id}`, { method: 'DELETE', body: { delete_documents: true, expected_revision: button.dataset.revision } });
    await refreshWorkspace(); await route(); closeModal();
    toast(result.cleanup_pending ? '知识库资料已移除；部分原件暂时被占用，将在下次知识库管理操作时重试清理。' : `${result.cleared ? '已清空默认知识库' : '已删除知识库'}，移除 ${result.deleted_documents} 份资料。`, !!result.cleanup_pending);
  });
  if (action === 'retry-run') {
    const id = button.dataset.retryId || state.run.id;
    if (retryingRuns.has(id)) return;
    retryingRuns.add(id);
    return busy(button, async () => {
      const storageKey = `evotask-retry:${state.mode}:${id}`;
      let key = sessionStorage.getItem(storageKey);
      if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(storageKey, key); }
      try {
        const result = await api(`/api/runs/${id}/retry`, {method: 'POST', body: {request_key: key}});
        sessionStorage.removeItem(storageKey);
        await refreshWorkspace();
        location.hash = `task/${result.id}`;
      } finally { retryingRuns.delete(id); }
    });
  }
  if (action === 'retry-actions') {
    openModal('继续失败任务', `<p class="subtext">从动作格式错误处继续，保留已完成的上游任务、已有文件及验收条件。任务完成后仍按原要求进行人工审阅${state.mode === 'live' ? '；继续调用真实模型会消耗剩余 API 预算' : ''}。</p><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-action="confirm-retry-actions">继续执行</button></div>`); return;
  }
  if (action === 'confirm-retry-actions') return busy(button, async () => { await api(`/api/runs/${state.run.id}/retry-actions`, { method: 'POST' }); closeModal(); await refreshRun(); });
  if (action === 'repair-run') {
    openModal('修复并复验', `<p class="subtext">将失败的验证反馈给原作者，保留报告、测试和验收历史。修订后使用原测试重新验证${state.mode === 'live' ? '，继续调用真实模型并消耗剩余 API 预算' : ''}。每个任务最多两轮返修。</p><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" data-action="confirm-repair">开始修复并复验</button></div>`); return;
  }
  if (action === 'confirm-repair') return busy(button, async () => { await api(`/api/runs/${state.run.id}/repair`, { method: 'POST' }); closeModal(); await refreshRun(); });
  if (action === 'template') {
    const templates = {
      research: '基于知识库资料，研究 cosine similarity 的原理、适用场景与边界条件，生成带来源引用的 research-report.md。',
      code: '实现一个只使用 Python 标准库的 count_words(text) 函数，并在 Docker 中运行测试，覆盖空文本、标点与中英文混合输入，交付代码和 README.md。',
      review: '基于知识库资料生成一份文本相似度方法比较报告，核验引用与边界条件，交付前等待我的人工确认。',
    };
    if (state.mode === 'demo') {
      if (button.dataset.template === 'code') { openModal('使用真实模型构建代码', `<p class="subtext">代码场景需要真实模型 API 与 Docker。切换后会填入示例目标，由你点击启动任务。</p><div class="modal-actions"><button class="btn secondary" data-action="close-modal">留在演示</button><button class="btn primary" data-action="switch-live-code">切换并填入目标</button></div>`); return; }
      state.review = button.dataset.template === 'review'; $('#review-toggle').checked = state.review;
      toast('已选择文本研究演示。点击「启动协作演示」开始。');
    } else { state.goal = templates[button.dataset.template]; $('#goal').value = state.goal; if (button.dataset.template === 'review') { state.review = true; $('#review-toggle').checked = true; } $('#goal').focus(); }
    return;
  }
  if (action === 'switch-live-code') { closeModal(); state.goal = '实现 Python count_words(text) 函数，在 Docker 中运行独立测试，覆盖空输入、标点和重复单词，交付代码与 README.md。'; $('#mode-select').value = 'live'; $('#mode-select').dispatchEvent(new Event('change', { bubbles: true })); return; }
  if (action === 'memory') return busy(button, async () => {
    const agent = button.dataset.agent, mode = state.mode;
    const entries = await api(`/api/agents/${agent}/memory${state.route === 'task' && state.run?.team_id ? '?team_id=' + state.run.team_id : ''}`);
    openModal(`${roles[agent].name} · 经验记忆`, `<div class="notice">${icon('brain')}<p>${mode === 'demo' ? '以下记录来自离线演示。' : ''}经验保留已观察到的结果和证据；历史成功不保证下一次成功。</p></div><div style="margin-top:18px">${entries.length ? entries.slice().reverse().map(entry => `<article class="memory-item"><strong>${esc(entry.trigger)}</strong><p>${esc(entry.lesson)}</p><small>${esc(entry.kind)} · ${esc(entry.run_id)} · ${entry.verified ? '来源于执行记录' : '未验证'}</small></article>`).join('') : empty('经验从第一次实践开始', '完成任务后，团队会保留结果与检查证据，用于后续检索。', 'brain')}</div>`);
  });
  if (action === 'artifact') return busy(button, async () => {
    const runId = state.run.id, mode = state.mode;
    const artifact = await api(`/api/runs/${runId}/artifacts/${button.dataset.id}`, { mode });
    openModal(artifact.download_name || artifact.path, `<div class="artifact-meta">原文件：${esc(artifact.path)}<br>${esc(artifact.status_label || '')}<br>${artifact.verification === 'verified' ? '已通过声明的检查' : artifact.verification === 'superseded' ? '历史版本 · 已被修订替代' : '未验收 · 请审阅'} · ${artifact.bytes} bytes<br>SHA256 ${esc(artifact.sha256)}</div><div class="markdown">${artifact.path.toLowerCase().endsWith('.html') ? `<pre>${esc(artifact.content)}</pre>` : markdown(artifact.content)}</div>${/\.(py|js|ts|toml)$/i.test(artifact.path) ? '<p class="subtext">下载名称已调整；组合运行代码时请还原原文件名及目录结构。</p>' : ''}<div class="modal-actions"><a class="btn primary" href="${apiPath(`/api/runs/${runId}/artifacts/${artifact.id}?download=true`, mode)}" download>${icon('download')}下载文件</a></div>`);
  });
  if (action === 'approve') return busy(button, async () => { await api(`/api/runs/${state.run.id}/resume`, { method: 'POST', body: { request_id: state.run.pending.id, reply: button.dataset.reply } }); toast(button.dataset.reply === 'yes' ? '已确认，团队将继续执行。' : '已拒绝，结果与原因将保留。'); await refreshRun(); });
  if (action === 'continue') return busy(button, async () => { await api(`/api/runs/${state.run.id}/continue`, { method: 'POST' }); await refreshRun(); });
  if (action === 'cancel') return busy(button, async () => { await api(`/api/runs/${state.run.id}/cancel`, { method: 'POST' }); toast('已请求停止；当前请求或工具操作结束后生效。'); await refreshRun(); });
  if (action === 'delete-document') {
    const doc = state.docs.find(d => d.id === button.dataset.id);
    openModal('移除知识文档', `<p class="subtext">将从当前知识库移除「${esc(doc.name)}」。已生成产物和历史引用记录会保留。</p><div class="modal-actions"><button class="btn secondary" data-action="close-modal">取消</button><button class="btn danger" data-action="confirm-delete" data-id="${esc(doc.id)}">${icon('trash')}确认移除</button></div>`); return;
  }
  if (action === 'confirm-delete') return busy(button, async () => { await api(`/api/knowledge/${button.dataset.id}`, { method: 'DELETE' }); closeModal(); state.docs = await api('/api/knowledge'); await refreshWorkspace(); knowledgePage(); toast('文档已从知识库移除。'); });
  if (action === 'chat-bottom') { const list = $('#chat-messages'); list.scrollTop = list.scrollHeight; button.hidden = true; return; }
  if (action === 'channel') {
    state.channel = button.dataset.channel;
    await route();
  }
});

document.addEventListener('submit', async event => {
  const form = event.target;
  event.preventDefault();
  const button = event.submitter || $('button[type="submit"]', form);
  if (form.id.startsWith('account-')) return account.submit(form, button);
  if (form.id === 'base-form') return busy(button, async () => {
    const id = form.dataset.id;
    const base = await api('/api/knowledge/bases' + (id ? '/' + id : ''), { method: id ? 'PATCH' : 'POST', body: { name: $('#base-name').value.trim(), description: $('#base-description').value.trim() } });
    state.baseId = base.id; await refreshWorkspace(); await route(); closeModal(); toast('知识库已保存。');
  });
  if (form.id === 'document-settings-form') return busy(button, async () => {
    const body = { expected_revision: form.dataset.revision, base_id: $('#document-base').value, scope: $('#document-scope').value, source_url: $('#document-source').value.trim(), description: $('#document-description').value.trim() };
    if ($('#edit-document-content')) body.content = $('#edit-document-content').value;
    await api('/api/knowledge/' + form.dataset.id, { method: 'PATCH', body });
    state.baseId = body.base_id; await refreshWorkspace(); await route(); closeModal(); toast('资料已更新，检索将使用最新内容。');
  });
  if (form.id === 'task-form') return busy(button, async () => {
    const mode = state.mode;
    if (mode === 'live' && !$('#goal').value.trim()) throw new Error('先描述一下你想完成的目标。');
    state.goal = mode === 'live' ? $('#goal').value.trim() : state.goal;
    state.memory = $('#memory-toggle').checked; state.review = $('#review-toggle').checked;
    const uploadIds = [];
    if (mode === 'live') {
      for (const [index, file] of state.files.entries()) {
        button.innerHTML = `<span class="spinner"></span>上传 ${index + 1}/${state.files.length}`;
        const result = await uploadFile(file, 'task', 'shared', percent => {
          if (button.isConnected) button.innerHTML = `<span class="spinner"></span>${percent === 100 ? '提取正文…' : `上传 ${percent ?? '…'}%`}`;
        });
        uploadIds.push(result.id);
        if (result.status === 'stored') toast(`${file.name}：${result.detail}`, true);
      }
    }
    const run = await api('/api/runs', { method: 'POST', body: { goal: mode === 'demo' ? '' : state.goal, memory: state.memory, review: state.review, upload_ids: uploadIds, knowledge_base_ids: mode === 'live' ? (state.selectedBases ?? state.bases.map(b => b.id)) : null } });
    if (mode !== state.mode) return;
    state.files = [];
    await refreshWorkspace(); location.hash = `task/${run.id}`;
  });
  if (form.id === 'reply-form') return busy(button, async () => { await api(`/api/runs/${state.run.id}/resume`, { method: 'POST', body: { request_id: state.run.pending.id, reply: button.name === 'reply' ? button.value : $('#human-reply').value.trim() } }); toast('补充信息已提交。'); await refreshRun(); });
  if (form.id === 'document-form') return busy(button, async () => {
    const scope = $('#document-scope').value;
    const metadata = { base_id: $('#document-base').value, source_url: $('#document-source').value.trim(), description: $('#document-description').value.trim() };
    state.baseId = metadata.base_id;
    const files = pendingDocumentFiles.length ? [...pendingDocumentFiles] : [new File([$('#document-content').value], $('#document-name').value.trim(), { type: 'text/plain' })];
    let stored = 0;
    for (const [index, file] of files.entries()) {
      const result = await uploadFile(file, 'knowledge', scope, percent => {
        const progress = $('#upload-progress');
        if (progress) progress.textContent = `${index + 1}/${files.length} · ${file.name} · ${percent === 100 ? '已上传，正在提取正文并建立索引…' : `已上传 ${percent ?? '…'}%`}`;
      }, metadata);
      if (result.status === 'stored') stored++;
    }
    pendingDocumentFiles = [];
    state.docs = await api('/api/knowledge'); await refreshWorkspace(); if (state.route === 'knowledge') knowledgePage(); closeModal();
    toast(stored ? `已保存 ${files.length} 份文件，其中 ${stored} 份无法提取正文；请查看文件状态。` : `已上传并索引 ${files.length} 份文档。`, stored > 0);
  });
  if (form.id === 'search-form') return busy(button, async () => {
    const generation = state.generation;
    const passages = await api(`/api/knowledge/search?query=${encodeURIComponent($('#knowledge-query').value)}&agent=${$('#search-agent').value}&base_id=${encodeURIComponent(state.baseId)}`);
    if (generation !== state.generation) return;
    $('#search-results').innerHTML = passages.length ? passages.map(p => `<article class="search-result"><strong>${esc(p.source)}</strong><small>第 ${p.start}–${p.end} 行 · BM25 ${p.score}</small>${p.source_url ? `<a class="text-link source-link" href="${esc(p.source_url)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : ''}<p tabindex="0" aria-label="检索片段正文">${esc(p.text)}</p><span class="citation">${esc(p.citation)}</span></article>`).join('') : '<div class="artifact-empty">未找到匹配片段。试试不同关键词，或检查文档的访问范围。</div>';
  });
  if (form.id === 'chat-form') {
    if (state.sending) return;
    const message = $('#chat-input').value.trim(); if (!message) return;
    const channel = state.channel, mode = state.mode;
    state.sending = true; state.messages.push({ sender: 'human', body: message, created: Date.now() / 1000, pending: true }); chatPage();
    try {
      await api('/api/chat', { method: 'POST', mode, body: { message, channel } });
    } catch (error) { toast(errorDetail(error.message), true); }
    finally {
      state.sending = false;
      if (state.route === 'chat' && state.channel === channel && state.mode === mode) {
        try { state.messages = await api(chatPath(channel), { mode }); chatPage(); $('#chat-input')?.focus(); }
        catch (error) { toast(error.message, true); chatPage(); }
      }
    }
  }
  if (form.id === 'login-form') return busy(button, async () => { await api('/api/session/login', { method: 'POST', body: { token: $('#access-token').value } }); await start(); });
});

document.addEventListener('input', event => {
  if (event.target.id === 'goal') state.goal = event.target.value;
  if (event.target.id === 'run-query') { state.query = event.target.value; filterRuns(); }
  if (event.target.id === 'command-input') commandResults(event.target.value);
});
document.addEventListener('change', async event => {
  const target = event.target;
  if (target.id === 'chat-run-filter') { state.chatRun = target.value; await route(); return; }
  if (target.id === 'active-base') { state.baseId = target.value; knowledgePage(); }
  if (target.matches('[data-base]')) { state.selectedBases = $$('.task-knowledge [data-base]:checked').map(input => input.dataset.base); $('.task-knowledge summary span').textContent = `${state.selectedBases.length} 个已选`; }
  if (target.id === 'memory-toggle') state.memory = target.checked;
  if (target.id === 'review-toggle') state.review = target.checked;
  if (target.id === 'mode-select') {
    state.stream?.close(); ++state.generation;
    state.chatRun = ''; state.messages = [];
    state.mode = target.value; localStorage.setItem('evotask-mode', state.mode);
    state.workspace = { runs: [], agents: [], job: {}, documents: 0 }; state.selectedBases = null; state.bases = []; state.baseId = 'kb_default'; state.run = null; state.events = []; state.files = [];
    updateEnvironment();
    try { await refreshWorkspace(); if (state.route === 'task') location.hash = 'home'; else await route(); toast(state.mode === 'demo' ? '已进入独立的离线演示空间。' : '已进入真实模型空间，执行任务会消耗 API 额度。'); }
    catch (error) { toast(error.message, true); }
  }
  if (target.id === 'task-files') {
    try { state.files.push(...target.files); renderAttachments(); }
    catch (error) { toast(error.message, true); }
    target.value = '';
  }
  if (target.id === 'knowledge-file' && target.files[0]) {
    try { documentEditor([...target.files]); }
    catch (error) { toast(error.message, true); }
    target.value = '';
  }
});

document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && state.initialized) { event.preventDefault(); commandPalette(); }
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    const form = document.activeElement?.closest('form') || $('#task-form');
    if (form && (!modal.open || modal.contains(form))) { event.preventDefault(); form.requestSubmit(); }
  }
  if (event.key === 'Enter' && event.target.matches('[data-run]')) event.target.click();
  if (modal.open && $('#command-input')) {
    const entries = $$('.command-result', modal), index = entries.indexOf(document.activeElement);
    if (event.key === 'ArrowDown') { event.preventDefault(); entries[Math.min(index + 1, entries.length - 1)]?.focus(); }
    if (event.key === 'ArrowUp') { event.preventDefault(); (index <= 0 ? $('#command-input') : entries[index - 1])?.focus(); }
    if (event.key === 'Enter' && event.target.id === 'command-input') { event.preventDefault(); entries[0]?.click(); }
  }
});

async function start() {
  try {
    const session = await api('/api/session'); state.csrf = session.csrf; state.user = session.user;
    if (!session.authenticated) {
      app.innerHTML = `<div class="login-screen"><form class="login-card" id="login-form"><div class="brand">${brand()}<span class="brand-name">Evo<span>Task</span></span></div><h1>你的团队，在此等候。</h1><p>输入管理员提供的工作台访问口令，进入独立的协作空间。</p><label class="field"><span>访问口令</span><input type="password" id="access-token" autocomplete="current-password" required maxlength="512" placeholder="输入工作台口令"></label><button type="submit" class="btn primary">进入工作空间 ${icon('arrow')}</button></form></div>`; return;
    }
    shell(); await refreshWorkspace(); state.initialized = true; await route();
    if (session.account_expired) { await account.panel(); toast('账号登录已失效，请重新登录。', true); }
  } catch (error) { app.innerHTML = `<div class="login-screen"><div class="login-card">${brand()}<h1 style="margin-top:25px">暂时无法连接</h1><p>${esc(error.message)}</p><button class="btn primary" id="reconnect">重新连接</button></div></div>`; $('#reconnect').addEventListener('click', start); }
}

window.addEventListener('hashchange', () => { if (state.initialized) route(); });
window.addEventListener('beforeunload', () => state.stream?.close());
window.addEventListener('storage', event => { if (event.key === 'evotask-identity-change') changeSession(false); });
document.addEventListener('pointermove', event => {
  const visual = event.target.closest('.team-visual');
  if (!visual || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const bounds = visual.getBoundingClientRect();
  visual.style.setProperty('--pointer-x', `${event.clientX - bounds.left}px`);
  visual.style.setProperty('--pointer-y', `${event.clientY - bounds.top}px`);
});
setInterval(async () => {
  if (!state.initialized || document.hidden || busyCount || modal.open || state.sending) return;
  try {
    const session = await api('/api/session');
    if (session.csrf !== state.csrf || session.user?.id !== state.user?.id || !session.authenticated) {
      const expired = session.account_expired;
      await changeSession(false);
      if (expired) { await account.panel(); toast('账号登录已失效，请重新登录。', true); }
      return;
    }
    if (state.route === 'task') return;
    const generation = state.generation;
    const previousBases = JSON.stringify(state.bases);
    await refreshWorkspace();
    if (generation !== state.generation) return;
    if (state.route === 'home' && $('#recent-runs')) { $('#recent-runs').innerHTML = runsTable(state.workspace.runs.slice(0, 4)); $('#home-run-count').textContent = String(state.workspace.runs.length).padStart(2, '0'); }
    if (state.route === 'runs') filterRuns();
    if (state.route === 'knowledge' && !$('#search-form')?.contains(document.activeElement)) {
      const docs = await api('/api/knowledge');
      if (generation !== state.generation || modal.open || busyCount) return;
      if (JSON.stringify(docs) !== JSON.stringify(state.docs) || previousBases !== JSON.stringify(state.bases)) { state.docs = docs; knowledgePage(); }
    }
  } catch { /* Explicit operations show connection errors; do not flood idle visitors. */ }
}, 12000);
let refreshingChat = false;
setInterval(async () => {
  if (!state.initialized || state.route !== 'chat' || document.hidden || state.sending || refreshingChat) return;
  refreshingChat = true;
  const generation = state.generation, path = chatPath();
  try {
    const messages = await api(path);
    if (generation !== state.generation || state.sending || path !== chatPath()) return;
    if (JSON.stringify(messages) !== JSON.stringify(state.messages)) { state.messages = messages; renderChatMessages(); }
    if ($('.chat-status')) $('.chat-status').textContent = '自动同步';
  } catch {
    if (generation === state.generation && $('.chat-status')) $('.chat-status').textContent = '正在重连…';
  } finally { refreshingChat = false; }
}, 2500);
start();
