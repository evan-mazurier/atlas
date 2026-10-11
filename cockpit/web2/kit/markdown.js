/* A tiny, SAFE markdown reader for the weekly report (headings, paragraphs, bullet lists, simple tables).
   parseMarkdown is pure (text in, plain block objects out). renderMarkdown builds DOM with textContent only:
   no innerHTML, no links, no raw HTML ever reaches the page. */

export function inline(s) {
  return String(s == null ? "" : s).replace(/\*\*(.+?)\*\*/g, "$1").replace(/__(.+?)__/g, "$1").replace(/`([^`]+)`/g, "$1").replace(/\[([^\]]+)\]\(\S*\)/g, "$1").trim();
}

function cells(line) {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split("|").map((c) => inline(c));
}

const isRule = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);

export function parseMarkdown(text) {
  const lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    const h = /^(#{1,6})\s+(.*)$/.exec(l);
    if (h) { out.push({ t: "h", level: h[1].length, text: inline(h[2].replace(/\s+#+\s*$/, "")) }); i++; continue; }
    if (l.includes("|") && i + 1 < lines.length && isRule(lines[i + 1]) && lines[i + 1].includes("-")) {
      const head = cells(l);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) { rows.push(cells(lines[i])); i++; }
      out.push({ t: "table", head, rows });
      continue;
    }
    if (/^\s*[-*+]\s+/.test(l)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { items.push(inline(lines[i].replace(/^\s*[-*+]\s+/, ""))); i++; }
      out.push({ t: "ul", items });
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6})\s+/.test(lines[i]) && !/^\s*[-*+]\s+/.test(lines[i])) { para.push(lines[i].trim()); i++; }
    out.push({ t: "p", text: inline(para.join(" ")) });
  }
  return out;
}

function mk(tag, text, parent) {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

export function renderMarkdown(host, text) {
  parseMarkdown(text).forEach((b) => {
    if (b.t === "h") mk("h" + Math.min(6, b.level + 2), b.text, host).className = "md-h";
    else if (b.t === "p") mk("p", b.text, host).className = "md-p";
    else if (b.t === "ul") { const ul = mk("ul", null, host); ul.className = "md-ul"; b.items.forEach((x) => mk("li", x, ul)); }
    else if (b.t === "table") {
      const wrap = mk("div", null, host);
      wrap.className = "tscroll";
      const tb = mk("table", null, wrap);
      const tr = mk("tr", null, mk("thead", null, tb));
      b.head.forEach((c) => mk("th", c, tr));
      const body = mk("tbody", null, tb);
      b.rows.forEach((r) => { const row = mk("tr", null, body); r.forEach((c) => mk("td", c, row)); });
    }
  });
}
