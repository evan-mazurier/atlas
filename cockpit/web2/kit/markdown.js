/* A tiny, SAFE markdown reader for the weekly report (headings, paragraphs, bullet and numbered lists with one
   nested level, tables, **bold** and `code`). HTML comments are dropped.
   parseMarkdown is pure (text in, plain block objects out). renderMarkdown builds DOM with textContent / text
   nodes only: no innerHTML, no links, no raw HTML ever reaches the page. */

/** Plain text of an inline string: bold, code and links reduced to their words. */
export function inline(s) {
  return String(s == null ? "" : s).replace(/\*\*(.+?)\*\*/g, "$1").replace(/__(.+?)__/g, "$1").replace(/`([^`]+)`/g, "$1").replace(/\[([^\]]+)\]\(\S*\)/g, "$1").trim();
}

/** Inline runs: [{text}, {text, b:true}, {text, code:true}]. Links reduce to their label; nothing else is markup. */
export function spans(s) {
  const src = String(s == null ? "" : s).replace(/\[([^\]]+)\]\(\S*\)/g, "$1").trim();
  const out = [];
  const re = /\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`/g;
  let last = 0, m;
  const plain = (t) => { if (t) out.push({ text: t }); };
  while ((m = re.exec(src))) {
    plain(src.slice(last, m.index));
    if (m[3] != null) out.push({ text: m[3], code: true });
    else out.push({ text: (m[1] != null ? m[1] : m[2]).replace(/`([^`]+)`/g, "$1"), b: true });
    last = m.index + m[0].length;
  }
  plain(src.slice(last));
  return out;
}

const NUM = /^[+\-−]?\d[\d,.]*\s?%?$/;
const isNum = (t) => NUM.test(String(t).trim());

function cell(raw) {
  const text = inline(raw);
  return { text, runs: spans(raw), num: isNum(text) };
}

function cells(line) {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split("|").map((c) => c.trim());
}

const isRule = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
const ITEM = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
const item = (raw) => ({ text: inline(raw), runs: spans(raw) });

export function parseMarkdown(text) {
  const src = String(text == null ? "" : text).replace(/\r\n?/g, "\n").replace(/<!--[\s\S]*?-->/g, "");
  const lines = src.split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    const h = /^(#{1,6})\s+(.*)$/.exec(l);
    if (h) { out.push({ t: "h", level: h[1].length, text: inline(h[2].replace(/\s+#+\s*$/, "")) }); i++; continue; }
    if (l.includes("|") && i + 1 < lines.length && isRule(lines[i + 1]) && lines[i + 1].includes("-")) {
      const head = cells(l).map(inline);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) { rows.push(cells(lines[i]).map(cell)); i++; }
      out.push({ t: "table", head, rows });
      continue;
    }
    const li = ITEM.exec(l);
    if (li) {
      const ordered = /^\d/.test(li[2]);
      const base = li[1].length;
      const items = [];
      while (i < lines.length) {
        const m = ITEM.exec(lines[i]);
        if (m) {
          if (m[1].length <= base) {
            if (/^\d/.test(m[2]) !== ordered) break;
            items.push({ raw: m[3], subs: [] });
          } else if (items.length) items[items.length - 1].subs.push(m[3]);
          else break;
          i++;
        } else if (lines[i].trim() && /^\s{2,}\S/.test(lines[i]) && items.length) {
          const last = items[items.length - 1];
          if (last.subs.length) last.subs[last.subs.length - 1] += " " + lines[i].trim(); else last.raw += " " + lines[i].trim();
          i++;
        } else break;
      }
      out.push({ t: ordered ? "ol" : "ul", items: items.map((x) => Object.assign(item(x.raw), { sub: x.subs.map(item) })) });
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6})\s+/.test(lines[i]) && !ITEM.test(lines[i])) { para.push(lines[i].trim()); i++; }
    const raw = para.join(" ");
    out.push({ t: "p", text: inline(raw), runs: spans(raw) });
  }
  return out;
}

function mk(tag, text, parent) {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

function putRuns(parent, runs) {
  (runs || []).forEach((r) => {
    if (r.b) mk("strong", r.text, parent);
    else if (r.code) mk("code", r.text, parent);
    else parent.appendChild(document.createTextNode(r.text));
  });
}

function putList(host, tag, items) {
  const list = mk(tag, null, host);
  list.className = "md-" + tag;
  items.forEach((x) => {
    const li = mk("li", null, list);
    putRuns(li, x.runs);
    if (x.sub && x.sub.length) {
      const ul = mk("ul", null, li);
      ul.className = "md-ul md-sub";
      x.sub.forEach((s) => putRuns(mk("li", null, ul), s.runs));
    }
  });
}

const WIDE_COLS = 4;        // a table with this many columns spans the full row
const CAPTION_MAX = 60;    // a one-line paragraph up to this long, right before a table, is its caption
const LONG_ROWS = 12;      // a table with more body rows than this is cut to SHOW_ROWS until expanded
const SHOW_ROWS = 10;

const isCaption = (b, next) => !!next && next.t === "table" && b.t === "p" && b.text.length <= CAPTION_MAX && !/[.;]$/.test(b.text);

function isWide(blocks) {
  return blocks.some((b) =>
    (b.t === "table" && b.head.length >= WIDE_COLS) ||
    (b.t === "p" && b.text.length > 100) ||
    ((b.t === "ul" || b.t === "ol") && (b.items.length > 3 || b.items.some((x) => x.text.length > 60))));
}

/** Pure grouping step for the grid layout. Every "###" subsection (its heading and the blocks up to the next heading of
    level 1 to 3) becomes a grid item inside {t:"grid", items:[{wide, blocks}]}; a "###" with captioned tables (a short
    one-line paragraph directly followed by a table) becomes its heading as a wide item, then one item per caption+table
    pair and one wide item per other run of blocks. Wide items (4+ column tables, long text or lists) span the full row. */
export function groupBlocks(blocks) {
  const out = [];
  let i = 0;
  while (i < blocks.length) {
    const b = blocks[i];
    if (!(b.t === "h" && b.level === 3)) { out.push(b); i++; continue; }
    const items = [];
    while (i < blocks.length && blocks[i].t === "h" && blocks[i].level === 3) {
      const head = blocks[i++];
      const body = [];
      while (i < blocks.length && !(blocks[i].t === "h" && blocks[i].level <= 3)) body.push(blocks[i++]);
      const hasCaption = body.some((x, k) => isCaption(x, body[k + 1]));
      if (!hasCaption) { items.push({ wide: isWide(body), blocks: [head].concat(body) }); continue; }
      items.push({ wide: true, blocks: [head] });
      let run = [];
      const flush = () => { if (run.length) items.push({ wide: true, blocks: run }); run = []; };
      for (let k = 0; k < body.length; k++) {
        if (isCaption(body[k], body[k + 1])) {
          flush();
          const pair = [body[k], body[k + 1]];
          items.push({ wide: isWide([body[k + 1]]), blocks: pair });
          k++;
        } else run.push(body[k]);
      }
      flush();
    }
    out.push({ t: "grid", items });
  }
  return out;
}

function putTable(host, b, opts) {
  const wrap = mk("div", null, host);
  wrap.className = "tscroll md-tbl";
  const tb = mk("table", null, wrap);
  tb.className = "tbl";
  const tr = mk("tr", null, mk("thead", null, tb));
  b.head.forEach((c, k) => {
    const th = mk("th", c, tr);
    if (b.rows.length && b.rows.every((r) => r[k] && r[k].num)) th.className = "num";
  });
  const body = mk("tbody", null, tb);
  const long = b.rows.length > LONG_ROWS;
  const extra = [];
  b.rows.forEach((r, n) => {
    const row = mk("tr", null, body);
    r.forEach((c) => { const td = mk("td", null, row); if (c.num) td.className = "num"; putRuns(td, c.runs); });
    if (long && n >= SHOW_ROWS) { row.hidden = true; extra.push(row); }
  });
  if (long && opts && typeof opts.button === "function") {
    const more = mk("div", null, host);
    more.className = "md-more";
    opts.button(more, { label: "Show all " + b.rows.length + " rows", onClick: () => { extra.forEach((r) => { r.hidden = false; }); more.remove(); } });
  } else if (long) extra.forEach((r) => { r.hidden = false; });
}

function putBlock(host, b, opts) {
  if (b.t === "h") mk("h" + Math.min(6, b.level + 2), b.text, host).className = "md-h md-h" + b.level;
  else if (b.t === "p") { const p = mk("p", null, host); p.className = "md-p"; putRuns(p, b.runs); }
  else if (b.t === "ul" || b.t === "ol") putList(host, b.t, b.items);
  else if (b.t === "table") putTable(host, b, opts);
  else if (b.t === "grid") {
    const g = mk("div", null, host);
    g.className = "md-grid";
    b.items.forEach((it) => {
      const cell = mk("div", null, g);
      cell.className = "md-gi" + (it.wide ? " md-wide" : "");
      it.blocks.forEach((x) => putBlock(cell, x, opts));
    });
  }
}

/** renderMarkdown(host, text, {grid?, button?}): `grid` lays "###" subsections out in a responsive grid;
    `button` is the themed button factory (kit/ui.js) used for "Show all N rows" on long tables. */
export function renderMarkdown(host, text, opts) {
  const blocks = parseMarkdown(text);
  (opts && opts.grid ? groupBlocks(blocks) : blocks).forEach((b) => putBlock(host, b, opts));
}
