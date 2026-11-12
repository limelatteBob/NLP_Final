import { icon } from './icons.js';
import { esc } from './components.js';

const $ = selector => document.querySelector(selector);
const when = seconds => new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false });
function deviceName(agent) {
  const platform = /Android/i.test(agent) ? 'Android' : /iPhone|iPad/i.test(agent) ? 'iOS' : /Windows/i.test(agent) ? 'Windows' : /Mac/i.test(agent) ? 'macOS' : /Linux/i.test(agent) ? 'Linux' : '设备';
  const browser = /Edg/i.test(agent) ? 'Edge' : /Firefox/i.test(agent) ? 'Firefox' : /Chrome|CriOS/i.test(agent) ? 'Chrome' : /Safari/i.test(agent) ? 'Safari' : '浏览器';
  return `${platform} · ${browser}`;
}

export function accountUI({ state, api, openModal, closeModal, busy, toast, changed, refreshKnowledge, isBusy }) {
  function loginForm(register = false) {
    openModal(register ? '创建你的账号' : '登录，接着上次的灵感', `
      <div class="account-intro"><span class="account-orbit">${icon('layers')}</span><div><div class="eyebrow">ONE ACCOUNT. YOUR WORKSPACE.</div><p>换个屏幕，资料依然在。<br>在同一服务地址登录，同步知识库、任务与对话。</p></div></div>
      <div class="tabs account-tabs"><button class="tab ${register ? '' : 'active'}" data-action="account-login-tab">登录账号</button><button class="tab ${register ? 'active' : ''}" data-action="account-register-tab">注册账号</button></div>
      <form id="account-auth-form" data-register="${register}">
        ${register ? '<label class="field"><span>怎么称呼你（可选）</span><input id="account-display" maxlength="60" autocomplete="nickname" placeholder="你的名字"></label>' : ''}
        <label class="field"><span>用户名</span><input id="account-username" required minlength="3" maxlength="40" autocomplete="username" autocapitalize="none" spellcheck="false" pattern="[a-zA-Z0-9][a-zA-Z0-9_.\\-]{2,39}" placeholder="3–40 位字母、数字或 . _ -"></label>
        <label class="field"><span>密码${register ? ' · 至少 12 个字符' : ''}</span><input id="account-password" type="password" required ${register ? 'minlength="12"' : ''} maxlength="128" autocomplete="${register ? 'new-password' : 'current-password'}"></label>
        ${register ? '<label class="field"><span>确认密码</span><input id="account-confirm" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label>' : ''}
        <p class="subtext">${register ? '使用用户名与密码登录，无需邮箱。当前访客资料可在登录后导入。' : '账号属于当前 EvoTask 服务。服务器的访问口令与个人账号密码分别设置。'}</p>
        <div class="modal-actions"><button type="button" class="btn secondary" data-action="close-modal">继续访客体验</button><button class="btn primary" type="submit">${register ? '创建账号并登录' : '登录并同步'} ${icon('arrow')}</button></div>
      </form>`, 'account-modal');
  }

  async function panel() {
    if (!state.user) return loginForm();
    const [devices, guest] = await Promise.all([api('/api/account/devices'), api('/api/account/guest-knowledge')]);
    const user = state.user;
    openModal('账号与同步', `
      <div class="account-summary"><span class="account-avatar">${esc(user.display_name.slice(0, 1).toUpperCase())}</span><div><h3>${esc(user.display_name)}</h3><p>@${esc(user.username)}</p></div><span class="pill green">${icon('check')}账号空间</span></div>
      <div class="account-server"><small>在其他设备打开这个服务地址，再登录此账号</small><code>${esc(location.origin)}</code>${['localhost', '127.0.0.1', '[::1]'].includes(location.hostname) ? '<p>此地址仅当前电脑可用。跨设备请使用同一服务器的局域网地址或 HTTPS 域名。可向服务管理员获取访问地址。</p>' : ''}</div>
      <section class="account-section"><div class="account-section-heading"><h3>导入访客知识库</h3><span class="pill">${state.mode === 'demo' ? '离线演示' : '真实模型'}</span></div>
        <p class="subtext">${guest.already_imported ? '此浏览器当前模式的访客资料已导入，重复操作不会新增副本。' : `此浏览器有 ${guest.bases.length} 个访客知识库、${guest.documents} 份资料。复制后保留访客原件，账号已有资料不会被覆盖。任务与对话不在本次导入范围内。`}</p>
        ${guest.bases.length ? `<div class="import-preview">${guest.bases.map(base => `<span>${esc(base.name)}<small>${base.documents} 份资料</small></span>`).join('')}</div>` : ''}
        <button class="btn secondary small" data-action="account-import" ${guest.already_imported || !guest.bases.length ? 'disabled' : ''}>${icon('book')}导入到我的账号</button>
      </section>
      <section class="account-section"><div class="account-section-heading"><h3>登录设备 <small>${devices.length}</small></h3><button class="text-link" data-action="account-revoke" ${devices.length < 2 ? 'disabled' : ''}>退出其他设备</button></div>
        <div class="account-devices">${devices.map(device => `<div class="account-device">${icon('layers')}<div><strong>${esc(deviceName(device.device))}</strong><small>最近活动 ${esc(when(device.seen))}</small></div><span class="pill ${device.current ? 'green' : ''}">${device.current ? '当前设备' : '已登录'}</span></div>`).join('')}</div>
      </section>
      <details class="account-section"><summary>修改密码</summary><form id="account-password-form"><label class="field"><span>当前密码</span><input id="current-password" type="password" required maxlength="128" autocomplete="current-password"></label><label class="field"><span>新密码 · 至少 12 个字符</span><input id="new-password" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label><label class="field"><span>确认新密码</span><input id="confirm-password" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label><p class="subtext">更新后保持此设备登录，其他设备需使用新密码重新登录。</p><button class="btn primary small" type="submit">更新密码</button></form></details>
      <div class="modal-actions"><button class="btn secondary" data-action="account-logout">退出此设备</button><button class="btn primary" data-action="close-modal">返回工作空间</button></div>`, 'account-modal');
  }

  async function action(button) {
    if (isBusy()) return toast('当前操作完成后即可管理账号。', true);
    const action = button.dataset.action;
    if (action === 'account-login-tab') return loginForm();
    if (action === 'account-register-tab') return loginForm(true);
    return busy(button, async () => {
      if (action === 'account') return panel();
      if (action === 'account-logout') {
        await api('/api/account/logout', { method: 'POST' });
        await changed(); toast('已退出此设备，账号资料保存在服务端。');
      }
      if (action === 'account-revoke') {
        await api('/api/account/logout-others', { method: 'POST' }); await panel(); toast('其他设备的登录已失效。');
      }
      if (action === 'account-import') {
        const result = await api('/api/account/import-guest', { method: 'POST' });
        await refreshKnowledge(); await panel(); toast(result.imported ? `已导入 ${result.bases} 个知识库、${result.documents} 份资料。` : '资料已导入，无需重复操作。');
      }
    });
  }

  async function submit(form, button) {
    return busy(button, async () => {
      if (form.id === 'account-auth-form') {
        const register = form.dataset.register === 'true';
        const body = { username: $('#account-username').value.trim(), password: $('#account-password').value };
        if (register) {
          if (body.password !== $('#account-confirm').value) throw new Error('两次密码输入不一致。');
          body.display_name = $('#account-display').value.trim();
        }
        await api(`/api/account/${register ? 'register' : 'login'}`, { method: 'POST', body });
        await changed(); await panel(); toast(register ? '账号已创建，可以在其他设备登录。' : '已登录，账号资料已同步。');
      } else if (form.id === 'account-password-form') {
        if ($('#new-password').value !== $('#confirm-password').value) throw new Error('两次新密码输入不一致。');
        await api('/api/account/password', { method: 'POST', body: { current_password: $('#current-password').value, new_password: $('#new-password').value } });
        await changed(); await panel(); toast('密码已更新，其他设备已退出。');
      }
    });
  }
  return { action, submit, panel };
}
