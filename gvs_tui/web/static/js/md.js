// Tiny markdown-lite renderer + light Python highlighting.
// Input is untrusted model output; everything is escaped before formatting.

export function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const PY_KW = /\b(def|class|return|if|elif|else|for|while|in|not|and|or|import|from|as|with|try|except|finally|raise|pass|break|continue|lambda|yield|global|nonlocal|assert|del|is|None|True|False|print|input|len|range|open|str|int|float|list|dict|set|tuple|sum|abs|min|max|sorted|enumerate|zip|map|filter)\b/;

export function highlightPy(code) {
  let s = esc(code);
  s = s.replace(
    /(#.*$)|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(\b\d+\.?\d*\b)|(\b(def|class|return|if|elif|else|for|while|in|not|and|or|import|from|as|with|try|except|finally|raise|pass|break|continue|lambda|yield|global|nonlocal|assert|del|is|None|True|False)\b)/gm,
    (m, com, str, num, kw) => {
      if (com) return `<span class="tok-com">${com}</span>`;
      if (str) return `<span class="tok-str">${str}</span>`;
      if (num) return `<span class="tok-num">${num}</span>`;
      if (kw) return `<span class="tok-kw">${kw}</span>`;
      return m;
    }
  );
  return s;
}

function inline(s) {
  return s
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, '<code class="inline">$1</code>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}

export function md(text) {
  if (text == null || !String(text).trim()) return '<div class="empty">—</div>';
  const blocks = [];
  let src = String(text).replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push({ lang, code });
    return `\u0000B${blocks.length - 1}\u0000`;
  });
  const lines = esc(src).split("\n");
  let html = "";
  let inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (const line of lines) {
    const bm = line.match(/^\u0000B(\d+)\u0000\s*$/);
    if (bm) {
      closeList();
      const b = blocks[Number(bm[1])];
      const hl = (b.lang === "python" || b.lang === "py") ? highlightPy(b.code) : esc(b.code);
      html += `<pre class="code"><code>${hl}</code></pre>`;
      continue;
    }
    const h = line.match(/^#{1,4}\s+(.*)/);
    if (h) { closeList(); html += `<h4>${inline(h[1])}</h4>`; continue; }
    if (/^\s*[-*]\s+/.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`;
      continue;
    }
    if (line.trim() === "") { closeList(); continue; }
    closeList();
    html += `<p>${inline(line)}</p>`;
  }
  closeList();
  return html;
}

// Extract "### Heading" sections from a model response.
export function sections(text) {
  const out = [];
  if (!text) return out;
  const re = /^###\s+(.+)$/gm;
  let m;
  const marks = [];
  while ((m = re.exec(text))) marks.push({ at: m.index, name: m[1].trim() });
  for (let i = 0; i < marks.length; i++) {
    const start = marks[i].at + marks[i].name.length + 4;
    const end = i + 1 < marks.length ? marks[i + 1].at : text.length;
    out.push({ name: marks[i].name, body: text.slice(start, end).trim() });
  }
  return out;
}
