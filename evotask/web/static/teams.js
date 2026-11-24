import { esc, badge, date, questionOptions } from './components.js';

const slots = { coordinator: '统筹位置', researcher: '协作位置 A', coder: '协作位置 B', verifier: '独立审查位置' };
const $ = s => document.querySelector(s);

export function teamUI({ state, api, apiPath, openModal, closeModal, toast, busy, refreshWorkspace }) {
  let current = null, timer = null, generation = 0, channel = 'group', imported = null, importVersion = 0;
  function stop() { clearTimeout(timer); timer = null; current = null; channel = 'group'; generation++; }
  function listBar() {
    return `<section class="panel team-list"><div class="panel-header"><strong>我的团队</strong><button class="btn primary small" data-team-action="create">新建团队</button></div><div class="team-links">${(state.workspace.teams || []).map(t => `<a class="team-link" href="#team/${esc(t.id)}"><strong>${esc(t.name)}</strong><small>${Object.values(t.roles).map(r => esc(r.name)).join(' · ')}</small></a>`).join('') || '<p class="subtext">为每个项目创建团队，让角色、对话与经验持续积累。</p>'}</div></section>`;
  }
  function editor() {
    imported = null; importVersion++;
    openModal('新建团队', `<form id="team-create-form"><div class="team-import-tabs" role="group" aria-label="团队信息输入方式"><button type="button" class="btn primary small" data-team-action="manual" aria-pressed="true">手动填写</button><button type="button" class="btn secondary small" data-team-action="upload" aria-pressed="false">上传 Markdown</button></div><section id="team-import-panel" hidden><label class="field"><span>团队说明文档（.md / .markdown，UTF-8，最多 128 KB）</span><input id="team-document" type="file" accept=".md,.markdown"></label><p class="subtext">支持“## 成员名称”后跟职责正文，或“角色 / 职责”表格。识别后可调整成员位置、名称与职责。</p><button type="button" class="btn secondary small" data-team-action="parse-document">识别成员与职责</button><p id="team-import-status" role="status" aria-live="polite"></p></section><label class="field"><span>团队名称</span><input id="team-name" maxlength="80" required></label><p class="subtext">填写四位成员的名称与职责。第一位负责统筹；请将独立审查、测试或风险评估角色放在最后一个位置。团队保存后，历史任务使用启动时的角色配置。回复和交付正文统一使用中文，角色职责可用任意语言填写。</p>${Object.entries(slots).map(([id, title]) => `<fieldset class="team-role-field"><legend>${title}</legend><label class="field team-import-choice" hidden><span>从文档选择成员</span><select data-import-slot="${id}" aria-label="${title}候选成员"></select></label><label class="field"><span>角色名称</span><input name="${id}-name" maxlength="80" required></label><label class="field"><span>职责描述</span><textarea name="${id}-instructions" maxlength="5000" rows="3" required></textarea></label></fieldset>`).join('')}<div class="modal-actions"><button type="button" class="btn secondary" data-action="close-modal">取消</button><button class="btn primary" type="submit">创建团队</button></div></form>`);
  }
  function pending(run) {
    const p = run.pending;
    if (!p) return '';
    const approval = ['tool_approval', 'acceptance', 'test_revision', 'check_revision'].includes(p.kind);
    return `<form class="team-reply panel" data-team-reply="${esc(run.id)}" data-request="${esc(p.id)}"><strong>${approval ? '等待确认' : '等你回答'}</strong><p>${esc(p.question)}</p>${p.prepared ? `<details><summary>查看文件、操作与验收要求</summary><pre>${esc(JSON.stringify(p.prepared, null, 2))}</pre></details>` : ''}${approval ? '<div class="team-actions"><button class="btn primary small" name="reply" value="yes">确认通过</button><button class="btn secondary small" name="reply" value="no">拒绝</button></div>' : `${questionOptions(p)}<label class="field"><span>也可填写简短回答</span><textarea name="reply" rows="2" maxlength="8000" placeholder="一句话即可…" required></textarea></label><button class="btn primary small" type="submit">回复并继续</button>`}${['acceptance','test_revision','check_revision'].includes(p.kind) ? `<label>修改对象<select name=quality-target>${run.tasks.filter(t => t.id === p.task_id || t.status === 'succeeded' && run.tasks.some(q => q.role === 'verifier' && q.depends_on.includes(t.id))).map(t => `<option value="${esc(t.id)}" ${t.id === p.task_id ? 'selected' : ''}>${esc(t.title)}</option>`).join('')}</select></label><label class="field"><span>需要修改的具体问题</span><textarea name="quality-feedback" rows="2" maxlength="7000"></textarea></label><button class="btn secondary small" name="reply" value="revise">提出修改并继续</button>` : ''}</form>`;
  }
  function cards(data) {
    return data.runs.map(run => `<article class="panel team-run" data-team-run="${esc(run.id)}"><div class="team-actions">${badge(run.status)}<span class="mono">${date(run.display_created || run.created, true)}</span><a class="text-link" href="#task/${esc(run.id)}">查看任务详情 →</a></div><h3>${esc(run.goal)}</h3><p class="subtext">${run.completed_tasks} / ${run.task_count} 子任务 · ${run.model_calls} 次${run.mode === 'live' ? '模型请求' : '模拟决策'} · ${run.executing ? '正在执行' : '状态已保存'}</p>${run.failure ? `<p class="notice error">${esc(run.failure)}</p>` : ''}<div class="team-task-list">${run.tasks.map(t => `<div>${badge(t.status)} <strong>${esc(run.team_snapshot?.roles[t.owner]?.name || t.owner || '待分派')}</strong> · ${esc(t.title)}${t.summary ? `<p>${esc(t.summary)}</p>` : ''}</div>`).join('')}</div>${pending(run)}<div class="team-artifacts">${run.artifacts.filter(a => a.verification !== 'superseded').map(a => `<a class="text-link" href="${apiPath(`/api/runs/${run.id}/artifacts/${a.id}?download=true`)}" download>${esc(a.path)}${a.verification === 'verified' ? '' : '（待审阅）'} ↓</a>`).join('')}</div>${run.status === 'succeeded' && run.artifacts.length ? `<button class="btn secondary small" data-team-action="inherit" data-run="${esc(run.id)}">基于这些文件发起新任务</button>` : ''}${['failed', 'cancelled'].includes(run.status) ? `<button class="btn secondary small" data-team-action="rerun" data-run="${esc(run.id)}">重新运行</button>` : ''}</article>`).join('') || '<div class="panel team-run"><p class="subtext">团队准备好了。在下方描述目标，点击「发起任务」开始协作。</p></div>';
  }
  function messages(data) {
    const name = id => id === 'human' ? '你' : data.team.roles[id]?.name || id || '团队';
    return data.messages.map(m => {
      const timestamp = m.created ? new Date(m.created * 1000) : null;
      return `<article class="team-chat-message ${m.sender === 'human' ? 'is-human' : ''}" data-message-id="${esc(m.id || `message:${m.seq}`)}"><span class="team-chat-avatar" aria-hidden="true">${esc([...name(m.sender)][0])}</span><div class="team-chat-body"><div class="team-chat-heading"><strong>${esc(name(m.sender))}</strong>${m.recipient ? `<span>→ ${esc(name(m.recipient))}</span>` : ''}<time ${timestamp ? `datetime="${timestamp.toISOString()}"` : ''}>${timestamp ? esc(timestamp.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})) : '时间未知'}</time></div><div class="team-chat-bubble">${m.label ? `<span class="team-chat-kind">执行动态 · ${esc(m.label)}</span>` : ''}<p>${esc(m.body)}</p></div>${m.run_id ? `<a class="team-chat-context" href="#task/${esc(m.run_id)}">${esc(m.task_title || m.run_goal || '查看任务')}</a>` : ''}</div></article>`;
    }).join('') || '<p class="subtext">想法、问题与进展，都留在这个团队里。</p>';
  }
  function updateMessages(data, initial = false) {
    const feed = $('#team-messages'); if (!feed) return;
    const html = messages(data); if (feed._markup === html) return;
    const atBottom = initial || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
    const anchor = [...feed.querySelectorAll('[data-message-id]')].find(el => el.offsetTop >= feed.scrollTop);
    const anchorId = anchor?.dataset.messageId, offset = anchor ? anchor.offsetTop - feed.scrollTop : 0;
    const previous = feed.scrollTop;
    feed.innerHTML = html; feed._markup = html;
    if (atBottom) feed.scrollTop = feed.scrollHeight;
    else {
      const next = [...feed.querySelectorAll('[data-message-id]')].find(el => el.dataset.messageId === anchorId);
      feed.scrollTop = next ? next.offsetTop - offset : previous;
    }
    const indicator = $('#team-new-messages'); if (indicator) indicator.hidden = atBottom;
  }
  function selectMember(slot, index) {
    const form = $('#team-create-form'), member = imported?.members[index];
    if (!form) return;
    form.elements[`${slot}-name`].value = member?.name || '';
    form.elements[`${slot}-instructions`].value = member?.instructions || '';
  }
  async function parseDocument() {
    const form = $('#team-create-form'), input = $('#team-document'), file = input?.files[0];
    const status = $('#team-import-status'), version = ++importVersion;
    try {
      if (!file || !/\.(md|markdown)$/i.test(file.name)) throw new Error('请选择 .md 或 .markdown 文档。');
      if (file.size > 131072) throw new Error('文档不能超过 128 KB。');
      status.textContent = '正在识别成员与职责…';
      const content = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer());
      const data = await api('/api/teams/preview', {method:'POST', body:{filename:file.name, content}});
      if (version !== importVersion || form !== $('#team-create-form')) return;
      imported = data; $('#team-name').value = data.name;
      for (const slot of Object.keys(slots)) {
        const select = form.querySelector(`[data-import-slot="${slot}"]`);
        select.innerHTML = '<option value="">手动补充</option>' + data.members.map((m, i) => `<option value="${i}">${esc(m.name)}</option>`).join('');
        select.value = data.assignments[slot] ?? ''; select.parentElement.hidden = false;
        selectMember(slot, select.value);
      }
      status.textContent = `已识别 ${data.members.length} 位成员。${data.warnings.join(' ')}`;
    } catch (e) {
      if (version === importVersion && form === $('#team-create-form')) status.textContent = e instanceof TypeError ? '无法读取文档，请使用 UTF-8 编码保存。' : e.message;
    }
  }
  async function refresh() {
    const id = current?.team.id, token = generation;
    if (!id || state.route !== 'team') return;
    try {
      const data = await api(`/api/teams/${id}?channel=${channel}`);
      if (token !== generation) return;
      current = data;
      // Preserve an answer being typed and any expanded preview during polling.
      const drafts = [...document.querySelectorAll('#team-runs [data-team-reply]')].map(form => ({request:form.dataset.request, fields:[...form.querySelectorAll('textarea,select')].map(el => ({name:el.name,value:el.value}))}));
      if (!($('#team-runs')?.contains(document.activeElement) && document.activeElement.matches('textarea,input,select'))) {
        $('#team-runs').innerHTML = cards(data);
        for (const draft of drafts) {
          const form = [...document.querySelectorAll('#team-runs [data-team-reply]')].find(el => el.dataset.request === draft.request);
          if (form) for (const field of draft.fields) { const el = form.elements[field.name]; if (el) el.value = field.value; }
        }
      }
      updateMessages(data);
      $('#team-state').textContent = data.job.busy ? '团队正在执行，进展自动同步' : '已同步 · 对话与任务均已保存';
    } catch (e) { if (token === generation && $('#team-state')) $('#team-state').textContent = e.message; }
    finally { if (token === generation) { clearTimeout(timer); timer = setTimeout(refresh, 2500); } }
  }
  async function show(id) {
    if (current?.team.id !== id) channel = 'group';
    const token = ++generation;
    const data = await api(`/api/teams/${encodeURIComponent(id)}?channel=${channel}`);
    if (token !== generation || state.route !== 'team') return;
    current = data;
    $('#main').innerHTML = `<div class="page team-page"><a class="text-link" href="#chat">← 返回 Agent 对话</a><div class="page-title"><div><div class="eyebrow">A TEAM, A SHARED HISTORY</div><h1>${esc(data.team.name)}</h1><p>从第一轮交付，继续下一轮想法。</p></div><div class="team-actions team-exports"><a class="btn primary small" href="${apiPath(`/api/teams/${id}/evaluation.zip`)}" title="包含对话、审计记录、所有归档产物及版本清单" download>导出评测包 ZIP</a><a class="btn secondary small" href="${apiPath(`/api/teams/${id}/export`)}" download>导出完整团队记录</a></div></div>${state.mode === 'demo' ? '<p class="notice">离线演示使用固定决策，仅展示协作流程。自定义业务任务请切换真实模型。</p>' : ''}<div class="team-roster">${Object.entries(data.team.roles).map(([slot, r]) => `<details class="panel team-member"><summary><small>${slots[slot]}</small><strong>${esc(r.name)}</strong></summary><p>${esc(r.instructions)}</p><small>${data.memory[slot].length} 条经验</small></details>`).join('')}</div><div class="team-room"><section><h2>协作任务</h2><div id="team-runs">${cards(data)}</div></section><aside><section class="panel team-conversation"><label class="field"><span>对话频道</span><select id="team-channel">${['group', ...Object.keys(slots)].map(slot => `<option value="${slot}" ${channel === slot ? 'selected' : ''}>${slot === 'group' ? '团队群聊' : `私聊 · ${esc(data.team.roles[slot].name)}`}</option>`).join('')}</select></label><p class="subtext">${channel === 'group' ? '团队共享讨论；执行进展在群聊同步。' : '仅所选 Agent 读取此私聊；协作任务仍在群聊执行。'}</p><div id="team-messages" role="log" aria-label="团队对话" aria-live="off"></div><button type="button" id="team-new-messages" class="btn secondary small" data-team-action="latest-messages" hidden>查看新消息 ↓</button></section><form class="panel team-composer" id="team-message-form"><label class="field"><span>${channel === 'group' ? '告诉团队你的想法' : `告诉 ${esc(data.team.roles[channel].name)} 你的想法`}</span><textarea id="team-message" rows="5" maxlength="8000" placeholder="聊聊需求，或直接描述要交付的目标…" required></textarea></label><div class="team-actions"><button type="submit" class="btn secondary small">发送消息</button><button type="button" class="btn primary small" data-team-action="start" ${channel !== 'group' ? 'disabled' : ''}>发起任务</button></div><small>发送消息用于讨论；发起任务会执行工具并产出文件${state.mode === 'live' ? '，消耗模型额度' : ''}。</small></form><p class="subtext" id="team-state">已同步</p></aside></div></div>`;
    updateMessages(data, true);
    clearTimeout(timer); timer = setTimeout(refresh, 2500);
  }
  function inheritance(run) {
    openModal('基于交付文件发起新任务', `<form id="team-inherit-form" data-run="${esc(run.id)}"><p class="subtext">所选归档将按原始路径与字节复制到新任务的只读输入目录。请选择每个路径的一个版本；旧交付不会被修改。</p><div class="team-file-picker">${run.artifacts.map(a => `<label><input type="checkbox" name="artifact" value="${esc(a.id)}" ${a.verification === 'verified' && run.tasks.some(t => t.artifact_ids.includes(a.id)) && run.artifacts.filter(b => b.path === a.path && run.tasks.some(t => t.artifact_ids.includes(b.id))).length === 1 ? 'checked' : ''}><span>${esc(a.path)}<small>${esc(a.verification)} · ${esc(a.sha256.slice(0, 12))} · ${esc(a.id)}</small></span></label>`).join('')}</div><label class="field"><span>下一轮任务</span><textarea id="team-next-goal" rows="6" maxlength="12000" required></textarea></label><div class="modal-actions"><button type="button" class="btn secondary" data-action="close-modal">取消</button><button type="submit" class="btn primary">创建并启动新任务</button></div></form>`);
  }
  document.addEventListener('click', e => {
    const button = e.target.closest('[data-team-action]'); if (!button || button.disabled) return;
    const action = button.dataset.teamAction;
    if (action === 'create') return editor();
    if (action === 'inherit') return inheritance(current.runs.find(r => r.id === button.dataset.run));
    if (action === 'manual' || action === 'upload') {
      importVersion++;
      $('#team-import-panel').hidden = action === 'manual';
      document.querySelectorAll('.team-import-tabs button').forEach(el => {
        const selected = el.dataset.teamAction === action;
        el.setAttribute('aria-pressed', selected); el.classList.toggle('primary', selected); el.classList.toggle('secondary', !selected);
      });
      return;
    }
    if (action === 'parse-document') return busy(button, parseDocument);
    if (action === 'latest-messages') { $('#team-messages').scrollTop = $('#team-messages').scrollHeight; button.hidden = true; return; }
    if (action === 'rerun') return busy(button, async () => {
      const keyName = `team-rerun:${state.mode}:${button.dataset.run}`;
      const request_key = sessionStorage.getItem(keyName) || crypto.randomUUID();
      sessionStorage.setItem(keyName, request_key);
      await api(`/api/runs/${button.dataset.run}/rerun`, {method:'POST', body:{request_key}});
      sessionStorage.removeItem(keyName);
      await refresh(); await refreshWorkspace();
    });
    if (action === 'start') return busy(button, async () => {
      if (channel !== 'group') throw new Error('请切换到团队群聊发起协作任务。');
      const input = $('#team-message'), goal = input.value.trim(); if (!goal) throw new Error('请先填写任务目标。');
      await api('/api/runs', { method: 'POST', body: { goal, team_id: current.team.id, review: true } });
      input.value = ''; await refresh(); await refreshWorkspace();
    });
  });
  document.addEventListener('change', e => {
    if (e.target.id === 'team-document') { importVersion++; $('#team-import-status').textContent = ''; return; }
    if (e.target.dataset.importSlot) return selectMember(e.target.dataset.importSlot, e.target.value);
    if (e.target.id !== 'team-channel' || !current) return;
    const draft = $('#team-message')?.value || '';
    channel = e.target.value;
    show(current.team.id).then(() => { if ($('#team-message')) $('#team-message').value = draft; }).catch(e => toast(e.message, true));
  });
  document.addEventListener('submit', e => {
    const form = e.target;
    if (!['team-create-form', 'team-message-form', 'team-inherit-form'].includes(form.id) && !form.dataset.teamReply) return;
    e.preventDefault(); const button = e.submitter || form.querySelector('button[type=submit]');
    busy(button, async () => {
      if (form.id === 'team-create-form') {
        const roles = Object.fromEntries(Object.keys(slots).map(id => [id, {name: form.elements[`${id}-name`].value, instructions: form.elements[`${id}-instructions`].value}]));
        const team = await api('/api/teams', {method: 'POST', body: {name: $('#team-name').value, roles}});
        closeModal(); await refreshWorkspace(); location.hash = `team/${team.id}`; return;
      }
      if (form.id === 'team-message-form') {
        await api('/api/chat', {method: 'POST', body: {team_id: current.team.id, channel, message: $('#team-message').value}});
        $('#team-message').value = '';
      } else if (form.id === 'team-inherit-form') {
        const artifact_ids = [...form.querySelectorAll('[name=artifact]:checked')].map(el => el.value);
        if (!artifact_ids.length) throw new Error('请至少选择一个归档文件。');
        await api('/api/runs', {method: 'POST', body: {team_id: current.team.id, goal: $('#team-next-goal').value, source_run_id: form.dataset.run, artifact_ids, review: true}});
        closeModal();
      } else {
        const reply = button.value === 'revise' ? 'revise:' + form.querySelector('[name=quality-target]').value + '|' + form.querySelector('[name=quality-feedback]').value.trim() : button.name === 'reply' ? button.value : form.querySelector('textarea[name=reply]').value.trim();
        await api(`/api/runs/${form.dataset.teamReply}/resume`, {method: 'POST', body: {team_id: current.team.id, request_id: form.dataset.request, reply}});
        form.replaceChildren(); // Allow the refresh to replace a now-answered prompt.
      }
      await refresh(); await refreshWorkspace();
    });
  });
  return { listBar, show, stop };
}
