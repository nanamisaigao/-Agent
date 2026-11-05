/**
 * 前端入口。
 *
 * 分四块，从上往下看就行：
 *   1. 状态与工具函数
 *   2. 对话区（SSE 事件 → DOM）
 *   3. 侧栏（工作区 / 表 / 模型，走 REST）
 *   4. 主题、演示模式、启动
 *
 * 一条原则：**对话区不认识数据库，侧栏不认识模型调用**。
 * 两边只通过 state 和 /api/* 交换信息。
 */

import { esc, markdown } from './markdown.js';

/* ------------------------------------------------------------------ */
/* 1. 状态与工具                                                       */
/* ------------------------------------------------------------------ */

const state = {
  workspaces: [],
  activeWs: null,
  tables: [],
  models: { items: [], active: null, fallback: null },
  providers: [],
  providersChecked: '',
  selectedProvider: null,
  sessions: [],
  activeSession: null,
  pendingContext: null,
  busy: false,
};

const $ = (id) => document.getElementById(id);
const logEl = $('log');
const inputEl = $('q');
const sendBtn = $('send');
const statusEl = $('status');

const FAKE_MODE = new URLSearchParams(location.search).get('fake') === '1';
const DEMO_MODE = new URLSearchParams(location.search).get('demo') === '1';

/** 空状态里的示例问题来自当前工作区的数据字典，不是写死的 */
function emptyHtml() {
  const workspace = state.workspaces.find((w) => w.id === state.activeWs);
  const suggestions = (workspace && workspace.suggestions) || [];
  const items = suggestions.length
    ? suggestions.map((q) => `<li>${esc(q)}</li>`).join('')
    : '<li>从左侧的表开始，或者直接描述你想看什么</li>';

  return `
  <div class="empty" id="empty">
    <p>问点什么试试</p>
    <ul>${items}</ul>
    <p class="tip">每一步的 SQL 和查询结果都会展开给你看</p>
  </div>`;
}

function clearLog() {
  logEl.innerHTML = emptyHtml();
}

/** 工作区数据加载完之后，如果还停在空状态就重画一次 */
function refreshEmpty() {
  if ($('empty')) clearLog();
}

function formatRows(n) {
  if (!n) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(0)}K`;
  return Number(n).toLocaleString();
}

function setStatus(text, cls = '') {
  statusEl.textContent = text;
  statusEl.className = `status ${cls}`.trim();
}

async function api(path, { method = 'GET', body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (data.detail) detail = data.detail;
    } catch (err) { /* 非 JSON 响应就保留状态码 */ }
    throw new Error(detail);
  }
  return response.json();
}

function autosize() {
  inputEl.style.height = 'auto';
  inputEl.style.height = `${Math.min(inputEl.scrollHeight, 160)}px`;
}

function scrollToBottom() {
  logEl.scrollTop = logEl.scrollHeight;
}

function append(className, html) {
  const empty = $('empty');
  if (empty) empty.remove();

  const div = document.createElement('div');
  div.className = `msg ${className}`;
  div.innerHTML = html;
  logEl.appendChild(div);
  scrollToBottom();
  return div;
}

/* ------------------------------------------------------------------ */
/* 2. 对话区                                                          */
/* ------------------------------------------------------------------ */

let toolCards = new Map();

function buildToolCard(id, name, args) {
  const empty = $('empty');
  if (empty) empty.remove();

  const sqlText = args && typeof args === 'object' && 'sql' in args
    ? args.sql
    : JSON.stringify(args ?? {}, null, 2);

  const card = document.createElement('div');
  card.className = 'msg tool';
  card.innerHTML = `
    <div class="tool-head">
      <span class="pill">${esc(name)}</span>
      <span class="meta" data-role="meta">运行中…</span>
    </div>
    <div class="sql-block">
      <div class="sql-label">SQL</div>
      <pre></pre>
    </div>`;
  card.querySelector('pre').textContent = sqlText || '（这次调用没有 SQL 参数）';

  logEl.appendChild(card);
  toolCards.set(id, card);
  scrollToBottom();
  return card;
}

function addToolCall(event) {
  buildToolCard(event.id, event.name, event.args);
}

function fillToolResult(event) {
  const card = toolCards.get(event.id);
  if (!card) return;

  const meta = card.querySelector('[data-role="meta"]');
  const result = event.result || {};

  if (!result.ok) {
    meta.textContent = '失败';
    const box = document.createElement('div');
    box.className = 'errbox';
    box.textContent = [result.error || '未知错误', result.hint].filter(Boolean).join('\n');
    card.appendChild(box);
    scrollToBottom();
    return;
  }

  const parts = [`${result.returned} 行`, `${Math.round(result.elapsed_ms)} ms`];
  if (result.truncated) parts.push('已截断');
  meta.textContent = parts.join(' · ');

  if (result.columns && result.columns.length) {
    const numericType = (type) => type === 'int' || type === 'float';
    const types = result.types || [];
    const head = result.columns
      .map((name, index) => `<th class="${numericType(types[index]) ? 'num' : ''}">${esc(name)}</th>`)
      .join('');
    const body = (result.rows || []).map((row) => {
      const tds = row.map((value, index) => {
        const isNull = value === null || value === undefined;
        const numeric = typeof value === 'number' || numericType(types[index]);
        return `<td class="${numeric ? 'num' : ''}">${esc(isNull ? 'NULL' : value)}</td>`;
      }).join('');
      return `<tr>${tds}</tr>`;
    }).join('');

    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    wrap.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    card.appendChild(wrap);
  }

  if (result.hint) {
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = result.hint;
    card.appendChild(hint);
  }

  scrollToBottom();
}

function handleEvent(event) {
  switch (event.type) {
    case 'context':
      state.pendingContext = event;
      break;
    case 'run_start': {
      append('user', esc(event.question));
      if (event.session_id) state.activeSession = event.session_id;
      const ctx = state.pendingContext;
      if (ctx && (ctx.shrunk || ctx.dropped)) {
        append('usage', `上下文已压缩：${ctx.shrunk || 0} 轮精简 · `
          + `${ctx.dropped || 0} 轮省略（约 ${ctx.tokens || 0} tokens）`);
      }
      state.pendingContext = null;
      setStatus('查询中…', 'busy');
      break;
    }
    case 'text':
      append('narration', esc(event.text));
      break;
    case 'tool_call':
      addToolCall(event);
      break;
    case 'tool_result':
      fillToolResult(event);
      break;
    case 'answer':
      append('answer', markdown(event.text || ''));
      break;
    case 'error':
      append('answer', `<div class="errbox">${esc(event.message)}</div>`);
      setStatus('出错', 'error');
      break;
    case 'run_end': {
      const usage = event.usage || {};
      append('usage', `完成 · ${event.steps} 步 · prompt ${usage.prompt_tokens || 0}`
        + ` / completion ${usage.completion_tokens || 0} tokens`);
      setStatus('就绪', '');
      break;
    }
    default:
      console.debug('未处理的事件', event.type, event);
  }
}

async function ask(question) {
  state.busy = true;
  sendBtn.disabled = true;
  inputEl.disabled = true;
  toolCards = new Map();
  setStatus('查询中…', 'busy');

  try {
    const response = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question,
        fake: FAKE_MODE,
        workspace: state.activeWs || undefined,
        session_id: state.activeSession || undefined,
      }),
    });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, boundary).trim();
        buffer = buffer.slice(boundary + 2);

        if (!frame.startsWith('data:')) continue;
        const payload = frame.slice(5).trim();
        if (payload === '[DONE]') continue;

        try {
          handleEvent(JSON.parse(payload));
        } catch (err) {
          console.error('事件解析失败', payload, err);
        }
      }
    }

    if (!statusEl.classList.contains('error')) setStatus('就绪', '');
  } catch (err) {
    append('answer', `<div class="errbox">请求失败：${esc(err.message)}</div>`);
    setStatus('连接失败', 'error');
  } finally {
    state.busy = false;
    sendBtn.disabled = false;
    inputEl.disabled = false;
    inputEl.focus();
    // 这一轮可能产生了新会话或改了标题，刷新一下列表
    loadSessions().catch(() => {});
  }
}

/* ------------------------------------------------------------------ */
/* 3. 侧栏                                                            */
/* ------------------------------------------------------------------ */

function renderWorkspaces() {
  const box = $('ws-list');
  box.innerHTML = '';

  if (!state.workspaces.length) {
    box.innerHTML = '<p class="side-empty">还没有工作区，点 ＋ 添加一个数据库</p>';
  }

  for (const workspace of state.workspaces) {
    const item = document.createElement('button');
    item.className = `ws-item${workspace.id === state.activeWs ? ' active' : ''}`;
    item.innerHTML = `
      <span class="ws-name">${esc(workspace.name)}</span>
      <span class="ws-meta">${workspace.tables} 表 · ${formatRows(workspace.rows)} 行</span>`;
    item.onclick = () => switchWorkspace(workspace.id);
    box.appendChild(item);
  }

  const active = state.workspaces.find((w) => w.id === state.activeWs);
  $('ws-title').textContent = active ? active.name : '未选择工作区';
  $('ws-sub').textContent = active ? `${active.tables} 张表 · ${formatRows(active.rows)} 行` : '';
  document.title = active ? `${active.name} · da_agent` : 'da_agent';
}

function renderTables() {
  const box = $('table-list');
  box.innerHTML = '';
  $('table-count').textContent = state.tables.length ? String(state.tables.length) : '';

  for (const table of state.tables) {
    const item = document.createElement('button');
    item.className = 'table-item';
    item.title = table.note || '';
    item.innerHTML = `<span class="t-name">${esc(table.name)}</span>`
      + `<span class="t-rows">${formatRows(table.rows)}</span>`;
    item.onclick = () => insertTableTemplate(table.name);
    box.appendChild(item);
  }
}

function insertTableTemplate(name) {
  inputEl.value = `SELECT * FROM ${name} LIMIT 10`;
  inputEl.focus();
  autosize();
}

/* ---------------------- 会话：列表 / 打开 / 回放 ---------------------- */

function shortTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  if (date.toDateString() === now.toDateString()) {
    return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch (err) {
    return { raw: String(text) };
  }
}

function renderSessions() {
  const box = $('session-list');
  box.innerHTML = '';

  if (!state.sessions.length) {
    box.innerHTML = '<p class="side-empty">还没有对话</p>';
    return;
  }

  for (const session of state.sessions) {
    const item = document.createElement('button');
    item.className = `session-item${session.id === state.activeSession ? ' active' : ''}`;
    item.innerHTML = `<span class="ss-title">${esc(session.title)}</span>`
      + `<span class="ss-meta">${session.turns} 轮 · ${shortTime(session.updated_at)}</span>`;
    item.onclick = () => openSession(session.id);
    box.appendChild(item);
  }
}

async function loadSessions() {
  if (!state.activeWs) {
    state.sessions = [];
    renderSessions();
    return;
  }
  state.sessions = await api(`/api/sessions?workspace=${encodeURIComponent(state.activeWs)}`);
  renderSessions();
}

async function openSession(sessionId) {
  if (state.busy) return;
  try {
    const data = await api(
      `/api/sessions/${encodeURIComponent(sessionId)}?workspace=${encodeURIComponent(state.activeWs)}`);
    state.activeSession = sessionId;
    clearLog();
    renderHistory(data.messages || []);
    renderSessions();
    closeSidebar();
  } catch (err) {
    setStatus(`打开会话失败：${err.message}`, 'error');
  }
}

function newSession() {
  if (state.busy) return;
  state.activeSession = null;
  clearLog();
  renderSessions();
  inputEl.focus();
  closeSidebar();
}

/** 把存下来的 messages 还原成界面。和实时渲染共用同一套卡片函数。 */
function renderHistory(messages) {
  toolCards = new Map();
  for (const message of messages) {
    if (message.role === 'user') {
      append('user', esc(message.content || ''));
    } else if (message.role === 'assistant') {
      const calls = message.tool_calls || [];
      for (const call of calls) {
        buildToolCard(call.id, call.function?.name || 'tool',
          safeJson(call.function?.arguments));
      }
      if (message.content) append('answer', markdown(message.content));
    } else if (message.role === 'tool') {
      fillToolResult({ id: message.tool_call_id, result: safeJson(message.content) });
    }
  }
  // 渲染完高度会再变一次（字体、表格布局），滚动两次才稳
  scrollToBottom();
  requestAnimationFrame(scrollToBottom);
}

function renderModelChip() {
  const { items, active, fallback } = state.models;
  const profile = items.find((item) => item.id === active);
  let label;
  if (profile) {
    label = `${profile.name} · ${profile.model}`;
  } else if (fallback) {
    label = `环境变量 · ${fallback.model}`;
  } else {
    label = '未配置模型';
  }
  $('model-label').textContent = label;

  const ready = Boolean(profile) || Boolean(fallback && fallback.has_key);
  $('btn-model').classList.toggle('warn', !ready);
}

function renderModelList() {
  const box = $('model-list');
  box.innerHTML = '';
  const { items, active, fallback } = state.models;

  if (!items.length) {
    const note = fallback
      ? `还没有保存的模型。当前回落到 .env：${fallback.model} @ ${fallback.base_url}`
      : '还没有保存的模型，先添加一个。';
    box.innerHTML = `<p class="empty-note">${esc(note)}</p>`;
    return;
  }

  for (const item of items) {
    const row = document.createElement('div');
    row.className = `model-row${item.id === active ? ' active' : ''}`;
    row.innerHTML = `
      <button class="model-pick" data-act="use">
        <span class="mr-name">${esc(item.name)}</span>
        <span class="mr-meta">${esc(item.model)} · ${esc(item.base_url)}</span>
        <span class="mr-key">${esc(item.key_hint)}</span>
      </button>
      <button class="mr-del" data-act="del" title="删除">✕</button>`;

    row.querySelector('[data-act="use"]').onclick = () => activateModel(item.id);
    row.querySelector('[data-act="del"]').onclick = () => deleteModel(item.id);
    box.appendChild(row);
  }
}

async function loadWorkspaces() {
  const data = await api('/api/workspaces');
  state.workspaces = data.items;
  state.activeWs = data.active;
  renderWorkspaces();
}

async function loadTables() {
  if (!state.activeWs) {
    state.tables = [];
    renderTables();
    return;
  }
  state.tables = await api(`/api/tables?workspace=${encodeURIComponent(state.activeWs)}`);
  renderTables();
}

async function loadModels() {
  state.models = await api('/api/models');
  renderModelChip();
  renderModelList();
}

async function switchWorkspace(id) {
  if (id === state.activeWs || state.busy) return;
  try {
    await api('/api/workspaces/activate', { method: 'POST', body: { id } });
    state.activeWs = id;
    state.activeSession = null;
    renderWorkspaces();
    await loadTables();
    await loadSessions();
    clearLog();
    closeSidebar();
  } catch (err) {
    setStatus(`切换失败：${err.message}`, 'error');
  }
}

async function addWorkspace(path, name) {
  const result = await api('/api/workspaces', { method: 'POST', body: { path, name: name || null } });
  await loadWorkspaces();
  await loadTables();
  clearLog();
  return result;
}

async function activateModel(id) {
  const { state: next } = await api('/api/models/activate', { method: 'POST', body: { id } });
  state.models = next;
  renderModelChip();
  renderModelList();
}

async function deleteModel(id) {
  const { state: next } = await api(`/api/models/${encodeURIComponent(id)}`, { method: 'DELETE' });
  state.models = next;
  renderModelChip();
  renderModelList();
}

/* ------------------------------------------------------------------ */
/* 4. 弹窗                                                             */
/* ------------------------------------------------------------------ */

function openModal(id) {
  $(id).hidden = false;
}

function closeModal(element) {
  const modal = element.closest('.modal');
  if (modal) modal.hidden = true;
}

document.querySelectorAll('[data-close]').forEach((btn) => {
  btn.addEventListener('click', () => closeModal(btn));
});

document.querySelectorAll('.modal').forEach((modal) => {
  modal.addEventListener('click', (event) => {
    if (event.target === modal) modal.hidden = true;
  });
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    document.querySelectorAll('.modal:not([hidden])').forEach((modal) => { modal.hidden = true; });
  }
});

$('btn-new-session').addEventListener('click', newSession);

/* 添加工作区 */
$('btn-add-ws').addEventListener('click', () => {
  $('ws-error').hidden = true;
  $('ws-path').value = '';
  $('ws-name').value = '';
  openModal('modal-ws');
  $('ws-path').focus();
});

$('ws-submit').addEventListener('click', async () => {
  const path = $('ws-path').value.trim();
  const name = $('ws-name').value.trim();
  const errorEl = $('ws-error');
  const submit = $('ws-submit');

  if (!path) {
    errorEl.textContent = '请填数据库文件路径';
    errorEl.hidden = false;
    return;
  }

  submit.disabled = true;
  submit.textContent = '分析中…';
  errorEl.hidden = true;

  try {
    const workspace = await addWorkspace(path, name);
    $('modal-ws').hidden = true;
    setStatus(`已添加 ${workspace.name}`, '');
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = '开始分析';
  }
});

/* 模型设置 */
$('btn-model').addEventListener('click', () => {
  $('m-error').hidden = true;
  $('m-ok').hidden = true;
  renderModelList();
  renderProviderGrid();
  if (!state.selectedProvider && state.providers.length) {
    applyProvider(state.providers[0].id);
  }
  openModal('modal-model');
});

function modelProvider() {
  return state.providers.find((p) => p.id === state.selectedProvider) || null;
}

function showModelError(text) {
  $('m-ok').hidden = true;
  $('m-error').textContent = text;
  $('m-error').hidden = false;
}

function setModelOk(text) {
  $('m-error').hidden = true;
  $('m-ok').textContent = text;
  $('m-ok').hidden = false;
}

function renderProviderGrid() {
  const box = $('provider-grid');
  box.innerHTML = '';
  for (const provider of state.providers) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `provider-card${provider.id === state.selectedProvider ? ' active' : ''}`;
    card.title = provider.base_url ? `${provider.note || ''}\n${provider.base_url}` : (provider.note || '');
    card.innerHTML = `<span class="pc-name">${esc(provider.name)}</span>`
      + `<span class="pc-note">${esc(provider.note || provider.base_url)}</span>`;
    card.onclick = () => applyProvider(provider.id);
    box.appendChild(card);
  }
}

function applyProvider(id) {
  const provider = state.providers.find((p) => p.id === id);
  if (!provider) return;

  state.selectedProvider = id;
  renderProviderGrid();

  if (provider.id === 'custom') {
    $('m-base').value = '';
    $('m-model').value = '';
    $('model-options').innerHTML = '';
  } else {
    $('m-base').value = provider.base_url;
    $('model-options').innerHTML = provider.models
      .map((name) => `<option value="${esc(name)}"></option>`).join('');
    $('m-model').value = provider.models[0] || '';
  }
  $('m-name').value = provider.id === 'custom' ? '' : provider.name;
  $('m-hint').textContent = provider.key_url
    ? `API Key 申请：${provider.key_url}`
    : (provider.note || '');

  $('m-error').hidden = true;
  $('m-ok').hidden = true;
  $('m-key').focus();
}

async function loadProviders() {
  try {
    const data = await api('/api/providers');
    state.providers = data.items || [];
    state.providersChecked = data.checked;
    renderProviderGrid();
  } catch (err) {
    console.warn('服务商预设加载失败', err);
  }
}

$('m-discover').addEventListener('click', async () => {
  const baseUrl = $('m-base').value.trim();
  const apiKey = $('m-key').value.trim();
  const button = $('m-discover');

  if (!baseUrl || !apiKey) {
    showModelError('先填 Base URL 和 API Key，再拉列表');
    return;
  }

  button.disabled = true;
  button.textContent = '获取中…';
  try {
    const data = await api('/api/models/discover',
      { method: 'POST', body: { base_url: baseUrl, api_key: apiKey } });
    $('model-options').innerHTML = data.models
      .map((name) => `<option value="${esc(name)}"></option>`).join('');
    if (!data.models.length) {
      showModelError('服务端返回了空列表，检查权限或地址');
    } else {
      if (!$('m-model').value) $('m-model').value = data.models[0];
      setModelOk(`找到 ${data.models.length} 个模型，点模型名输入框可以挑`);
    }
  } catch (err) {
    showModelError(err.message);
  } finally {
    button.disabled = false;
    button.textContent = '获取列表';
  }
});

$('m-test').addEventListener('click', async () => {
  const baseUrl = $('m-base').value.trim();
  const model = $('m-model').value.trim();
  const apiKey = $('m-key').value.trim();
  const button = $('m-test');

  if (!baseUrl || !model || !apiKey) {
    showModelError('Base URL、模型名、API Key 三个都要填');
    return;
  }

  button.disabled = true;
  button.textContent = '测试中…';
  try {
    const provider = modelProvider();
    const data = await api('/api/models/test', {
      method: 'POST',
      body: {
        base_url: baseUrl,
        model,
        api_key: apiKey,
        thinking: provider ? provider.thinking : null,
      },
    });
    if (data.ok) setModelOk(`${data.message}（${data.latency_ms} ms）`);
    else showModelError(data.message);
  } catch (err) {
    showModelError(err.message);
  } finally {
    button.disabled = false;
    button.textContent = '测试连接';
  }
});

$('m-submit').addEventListener('click', async () => {
  const provider = modelProvider();
  const payload = {
    name: $('m-name').value.trim() || $('m-model').value.trim(),
    base_url: $('m-base').value.trim(),
    model: $('m-model').value.trim(),
    api_key: $('m-key').value.trim(),
    thinking: provider ? provider.thinking : null,
  };

  if (!payload.base_url || !payload.model) {
    showModelError('Base URL 和模型名不能为空');
    return;
  }
  if (!payload.api_key) {
    showModelError('请填 API Key');
    return;
  }

  const submit = $('m-submit');
  submit.disabled = true;
  submit.textContent = '保存中…';

  try {
    const result = await api('/api/models', { method: 'POST', body: payload });
    state.models = result.state;
    renderModelChip();
    renderModelList();
    $('m-key').value = '';
    $('m-name').value = '';
    setStatus(`当前模型：${payload.name}`, '');
    setModelOk('已保存并切换');
  } catch (err) {
    showModelError(err.message);
  } finally {
    submit.disabled = false;
    submit.textContent = '保存并启用';
  }
});

/* ------------------------------------------------------------------ */
/* 5. 输入、主题、启动                                                 */
/* ------------------------------------------------------------------ */

function submit() {
  const question = inputEl.value.trim();
  if (!question || state.busy) return;
  if (!state.activeWs) {
    setStatus('先选择一个工作区', 'error');
    return;
  }
  inputEl.value = '';
  autosize();
  ask(question);
}

sendBtn.addEventListener('click', submit);

inputEl.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});

inputEl.addEventListener('input', autosize);

/* 窄屏侧栏开关 */
$('btn-menu').addEventListener('click', () => {
  $('sidebar').classList.toggle('open');
});
function closeSidebar() {
  $('sidebar').classList.remove('open');
}

/* 主题 */
const themeBtn = $('theme');

function applyTheme(name) {
  document.documentElement.dataset.theme = name;
  themeBtn.textContent = name === 'dark' ? '☀' : '☾';
  themeBtn.title = name === 'dark' ? '切换到浅色' : '切换到深色';
  try {
    localStorage.setItem('da-theme', name);
  } catch (err) { /* 隐私模式忽略 */ }
}

let savedTheme = 'light';
try {
  savedTheme = localStorage.getItem('da-theme') || 'light';
} catch (err) { savedTheme = 'light'; }

const urlTheme = new URLSearchParams(location.search).get('theme');
if (urlTheme === 'dark' || urlTheme === 'light') savedTheme = urlTheme;
applyTheme(savedTheme);

themeBtn.addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});

/* 演示模式：?demo=1 渲染预置对话，不发任何请求 */
const DEMO_EVENTS = [
  { type: 'run_start', question: '2017 年哪个州的订单数最多？' },
  { type: 'text', text: '我按 2017 年整年、客户所在州统计订单数。' },
  {
    type: 'tool_call', id: 'demo-1', name: 'run_sql',
    args: {
      sql: 'SELECT c.customer_state, COUNT(DISTINCT o.order_id) AS orders\n'
         + 'FROM orders o\n'
         + 'JOIN customers c ON c.customer_id = o.customer_id\n'
         + "WHERE o.order_purchase_timestamp >= '2017-01-01'\n"
         + "  AND o.order_purchase_timestamp < '2018-01-01'\n"
         + 'GROUP BY c.customer_state\n'
         + 'ORDER BY orders DESC\n'
         + 'LIMIT 6',
    },
  },
  {
    type: 'tool_result', id: 'demo-1',
    result: {
      ok: true,
      columns: ['customer_state', 'orders'],
      types: ['text', 'int'],
      rows: [['SP', 17760], ['RJ', 6225], ['MG', 5414], ['RS', 2661], ['PR', 2270], ['SC', 1720]],
      returned: 6, truncated: false, elapsed_ms: 1643,
    },
  },
  {
    type: 'answer',
    text: '**结论**：2017 年订单数最多的是 SP（圣保罗州），17,760 单。\n\n'
        + '| 排名 | 州 | 订单数 |\n|---|---|---|\n'
        + '| 1 | SP | 17,760 |\n| 2 | RJ | 6,225 |\n| 3 | MG | 5,414 |\n\n'
        + 'SP 约占全部订单的 40%，是第二名 RJ 的 2.85 倍。\n\n'
        + '口径：订单数 = COUNT(DISTINCT order_id)，州取客户所在州。',
  },
  { type: 'run_end', ok: true, steps: 2, usage: { prompt_tokens: 3563, completion_tokens: 348 } },
];

async function boot() {
  clearLog();
  try {
    await loadWorkspaces();
    refreshEmpty();          // 工作区数据到了才知道该推荐什么问题
    await loadModels();
    await loadProviders();
    await loadTables();
    await loadSessions();
    setStatus('就绪', '');
  } catch (err) {
    setStatus(`初始化失败：${err.message}`, 'error');
    setReady(false);
  }

  if (DEMO_MODE) {
    DEMO_EVENTS.forEach(handleEvent);
    if (new URLSearchParams(location.search).get('panel') === 'model') {
      openModal('modal-model');
    }
    return;
  }

  // ?ws=<id> 先切到指定工作区，再处理 ?session=<id>
  const params = new URLSearchParams(location.search);
  const urlWs = params.get('ws');
  if (urlWs && urlWs !== state.activeWs) {
    try {
      await api('/api/workspaces/activate', { method: 'POST', body: { id: urlWs } });
      state.activeWs = urlWs;
      state.activeSession = null;
      renderWorkspaces();
      await loadTables();
      await loadSessions();
    } catch (err) {
      setStatus(`打开工作区失败：${err.message}`, 'error');
    }
  }

  // ?panel=model 直接打开模型设置（截图和排查用）
  if (params.get('panel') === 'model') {
    $('btn-model').click();
  }

  const urlSession = params.get('session');
  if (urlSession) {
    await openSession(urlSession);
  }
}

function setReady(ready) {
  sendBtn.disabled = !ready;
  inputEl.disabled = !ready;
}

boot();
inputEl.focus();
