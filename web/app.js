/**
 * 前端渲染器。
 *
 * 它的全部工作就是：读 SSE 事件流 → 按 type 分发给对应的渲染函数。
 * 不认识模型、不认识 SQL、不认识数据库——换掉后端只要事件协议不变，这里一行都不用改。
 */

const logEl = document.getElementById('log');
const inputEl = document.getElementById('q');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');

let toolCards = new Map();
let busy = false;

// 调试用：地址加 ?fake=1 走离线假模型，不花一分钱就能验证界面渲染
const FAKE_MODE = new URLSearchParams(location.search).get('fake') === '1';
// ?demo=1 直接渲染一段预置对话，用来单独调样式（不发请求）
const DEMO_MODE = new URLSearchParams(location.search).get('demo') === '1';

/* ------------------------------------------------------------------ */
/* 工具函数                                                            */
/* ------------------------------------------------------------------ */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

function scrollToBottom() {
  logEl.scrollTop = logEl.scrollHeight;
}

function clearEmpty() {
  const empty = document.getElementById('empty');
  if (empty) empty.remove();
}

function append(className, html) {
  clearEmpty();
  const div = document.createElement('div');
  div.className = `msg ${className}`;
  div.innerHTML = html;
  logEl.appendChild(div);
  scrollToBottom();
  return div;
}

function setStatus(text, cls = '') {
  statusEl.textContent = text;
  statusEl.className = `status ${cls}`.trim();
}

/* ------------------------------------------------------------------ */
/* 迷你 markdown：只支持模型真会用到的那几种                           */
/* ------------------------------------------------------------------ */

function inlineMd(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function renderTable(lines) {
  const cells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '')
    .split('|').map((cell) => cell.trim());
  const isNumeric = (value) => /^-?[\d,]+(\.\d+)?$/.test(String(value ?? '').trim());

  const head = cells(lines[0]);
  const body = lines.slice(1)
    .filter((row) => !/^[\s|:-]+$/.test(row))          // 去掉 |---|---| 分隔行
    .map(cells);

  // 整列都是数字才右对齐——表头跟着一起对齐，否则就是"错位"
  const numeric = head.map((_, index) => body.length > 0
    && body.every((row) => isNumeric(row[index])));

  const th = head
    .map((cell, index) => `<th class="${numeric[index] ? 'num' : ''}">${inlineMd(cell)}</th>`)
    .join('');
  const trs = body.map((row) => {
    const tds = row.map((cell, index) => (
      `<td class="${numeric[index] ? 'num' : ''}">${inlineMd(cell)}</td>`
    )).join('');
    return `<tr>${tds}</tr>`;
  }).join('');

  return `<div class="table-wrap"><table class="md"><thead><tr>${th}</tr></thead>`
       + `<tbody>${trs}</tbody></table></div>`;
}

function markdown(text) {
  const lines = String(text).replace(/\r/g, '').split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.startsWith('```')) {
      const code = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith('```')) code.push(lines[i++]);
      i += 1;
      out.push(`<pre class="code"><code>${esc(code.join('\n'))}</code></pre>`);
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      out.push(renderTable(rows));
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${inlineMd(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i++].replace(/^\s*[-*]\s+/, ''));
      }
      out.push(`<ul>${items.map((t) => `<li>${inlineMd(t)}</li>`).join('')}</ul>`);
      continue;
    }

    if (line.trim() === '') { i += 1; continue; }

    const para = [];
    while (i < lines.length && lines[i].trim() !== ''
           && !/^\s*\|/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i])
           && !lines[i].startsWith('```')) {
      para.push(lines[i++]);
    }
    out.push(`<p>${inlineMd(para.join(' '))}</p>`);
  }

  return out.join('');
}

/* ------------------------------------------------------------------ */
/* 工具卡片                                                            */
/* ------------------------------------------------------------------ */

function addToolCall(event) {
  clearEmpty();
  const args = event.args || {};
  const sql = args && typeof args === 'object' && 'sql' in args
    ? args.sql
    : JSON.stringify(args, null, 2);

  const card = document.createElement('div');
  card.className = 'msg tool';
  // SQL 常显，不用 <details>：折叠控件容易误触，且不同浏览器渲染差异大
  card.innerHTML = `
    <div class="tool-head">
      <span class="pill">${esc(event.name)}</span>
      <span class="meta" data-role="meta">运行中…</span>
    </div>
    <div class="sql-block">
      <div class="sql-label">SQL</div>
      <pre></pre>
    </div>`;
  card.querySelector('pre').textContent = sql || '（这次调用没有 SQL 参数）';

  logEl.appendChild(card);
  toolCards.set(event.id, card);
  scrollToBottom();
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
    // 表头按列类型对齐：数字列右对齐，和下面的数值对得上
    const numericType = (type) => type === 'int' || type === 'float';
    const types = result.types || [];
    const head = result.columns
      .map((name, index) => `<th class="${numericType(types[index]) ? 'num' : ''}">${esc(name)}</th>`)
      .join('');
    const body = (result.rows || []).map((row) => {
      const tds = row.map((value, index) => {
        const isNull = value === null || value === undefined;
        const numeric = typeof value === 'number';
        return `<td class="${numeric || numericType(types[index]) ? 'num' : ''}">`
             + `${esc(isNull ? 'NULL' : value)}</td>`;
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

/* ------------------------------------------------------------------ */
/* 事件分发                                                            */
/* ------------------------------------------------------------------ */

function handleEvent(event) {
  switch (event.type) {
    case 'run_start':
      append('user', esc(event.question));
      setStatus('查询中…', 'busy');
      break;
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

/* ------------------------------------------------------------------ */
/* 请求 + SSE 解析                                                     */
/* ------------------------------------------------------------------ */

async function ask(question) {
  busy = true;
  sendBtn.disabled = true;
  inputEl.disabled = true;
  toolCards = new Map();
  setStatus('查询中…', 'busy');

  try {
    const response = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, fake: FAKE_MODE }),
    });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE 帧以空行分隔
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
    busy = false;
    sendBtn.disabled = false;
    inputEl.disabled = false;
    inputEl.focus();
  }
}

/* ------------------------------------------------------------------ */
/* 输入交互                                                            */
/* ------------------------------------------------------------------ */

function submit() {
  const question = inputEl.value.trim();
  if (!question || busy) return;
  inputEl.value = '';
  inputEl.style.height = 'auto';
  ask(question);
}

sendBtn.addEventListener('click', submit);

inputEl.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = `${Math.min(inputEl.scrollHeight, 160)}px`;
});

inputEl.focus();

/* ------------------------------------------------------------------ */
/* 主题切换：默认浅色，选择记在 localStorage                           */
/* ------------------------------------------------------------------ */

const themeBtn = document.getElementById('theme');

function applyTheme(name) {
  document.documentElement.dataset.theme = name;
  themeBtn.textContent = name === 'dark' ? '☀' : '☾';
  themeBtn.title = name === 'dark' ? '切换到浅色' : '切换到深色';
  try {
    localStorage.setItem('da-theme', name);
  } catch (err) {
    // 隐私模式下 localStorage 不可用，忽略即可
  }
}

let savedTheme = 'light';
try {
  savedTheme = localStorage.getItem('da-theme') || 'light';
} catch (err) {
  savedTheme = 'light';
}
// ?theme=dark 便于截图和分享，优先级高于本地记录
const urlTheme = new URLSearchParams(location.search).get('theme');
if (urlTheme === 'dark' || urlTheme === 'light') savedTheme = urlTheme;
applyTheme(savedTheme);

themeBtn.addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});

/* ------------------------------------------------------------------ */
/* 演示模式：?demo=1 渲染一段预置对话，不发任何请求                     */
/* ------------------------------------------------------------------ */

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

if (DEMO_MODE) {
  DEMO_EVENTS.forEach(handleEvent);
}
