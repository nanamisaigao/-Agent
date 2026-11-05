/**
 * 迷你 markdown 渲染：只支持模型真会用到的那几种语法。
 * 没有依赖，也不引第三方库——你需要知道的规则都在下面 60 行里。
 *
 * 注意：先 esc() 再替换，模型输出里如果带 HTML 也不会被执行。
 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

export function inlineMd(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function cells(row) {
  return row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

/** 渲染表格。整列都是数字才右对齐，表头跟着一起对齐。 */
export function renderTable(lines) {
  const isNumeric = (value) => /^-?[\d,]+(\.\d+)?$/.test(String(value ?? '').trim());

  const head = cells(lines[0]);
  const body = lines.slice(1)
    .filter((row) => !/^[\s|:-]+$/.test(row))
    .map(cells);

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

export function markdown(text) {
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
