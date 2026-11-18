// Run: node --test cockpit/test_web2_markdown.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { parseMarkdown, groupBlocks, inline, spans } from "./web2/kit/markdown.js";

test("headings, paragraphs and bullets", () => {
  const b = parseMarkdown("# Week 41\n\nSome **bold** text\nsecond line.\n\n- one\n- `two`\n* three\n\n## Next");
  assert.deepEqual(b.map((x) => x.t), ["h", "p", "ul", "h"]);
  assert.equal(b[0].text, "Week 41");
  assert.equal(b[1].text, "Some bold text second line.");
  assert.deepEqual(b[2].items.map((x) => x.text), ["one", "two", "three"]);
  assert.equal(b[3].level, 2);
});

test("html comments are dropped, single and multi-line", () => {
  const b = parseMarkdown("<!-- numbers:start -->\n\n## A\n\n<!-- x\ny -->\ntext\n\n<!-- numbers:end -->");
  assert.deepEqual(b.map((x) => x.t), ["h", "p"]);
  assert.equal(b[1].text, "text");
});

test("bold and code become runs, links reduce to their label", () => {
  assert.deepEqual(spans("a **b** `c` [d](javascript:x) e"), [{ text: "a " }, { text: "b", b: true }, { text: " " }, { text: "c", code: true }, { text: " d e" }]);
  assert.deepEqual(spans(""), []);
});

test("ordered list and nested bullets", () => {
  const b = parseMarkdown("1. **First.** body\n2. Second\n   continued\n\n- top\n  - nested a\n  - nested b\n- next");
  assert.equal(b[0].t, "ol");
  assert.equal(b[0].items.length, 2);
  assert.deepEqual(b[0].items[0].runs[0], { text: "First.", b: true });
  assert.equal(b[0].items[1].text, "Second continued");
  assert.equal(b[1].t, "ul");
  assert.equal(b[1].items.length, 2);
  assert.deepEqual(b[1].items[0].sub.map((x) => x.text), ["nested a", "nested b"]);
});

test("simple tables, numeric cells detected", () => {
  const b = parseMarkdown("| Area | Hours | Share | Note |\n| --- | ---: | --- | --- |\n| Atlas | 4,219.5 | 36.8 % | **ok** |\n| MD | -2 | +5% | 3 tasks |\n\nafter");
  assert.equal(b[0].t, "table");
  assert.deepEqual(b[0].head, ["Area", "Hours", "Share", "Note"]);
  assert.deepEqual(b[0].rows.map((r) => r.map((c) => c.num)), [[false, true, true, false], [false, true, true, false]]);
  assert.deepEqual(b[0].rows[0][3].runs, [{ text: "ok", b: true }]);
  assert.equal(b[1].text, "after");
});

test("raw html and links stay plain text, never markup", () => {
  const b = parseMarkdown("<img src=x onerror=alert(1)>\n\n[click](javascript:alert(1))");
  assert.equal(b[0].text, "<img src=x onerror=alert(1)>");   // rendered with textContent: shown literally
  assert.equal(b[1].text, "click");
  assert.equal(inline(null), "");
});

test("empty input", () => { assert.deepEqual(parseMarkdown(""), []); });

const T2 = "| A | B |\n| --- | --- |\n| x | 1 |\n";
const T5 = "| A | B | C | D | E |\n| --- | --- | --- | --- | --- |\n| x | 1 | 2 | 3 | 4 |\n";

test("groupBlocks: ### subsections become grid items, short tables narrow, wide ones full row", () => {
  const g = groupBlocks(parseMarkdown("## Numbers\n\nintro\n\n### Time by area\n\n" + T2 + "\n### Time by type\n\n" + T2 + "\n### Wide\n\n" + T5 + "\n## Lessons\n\n1. one\n"));
  assert.deepEqual(g.map((b) => b.t), ["h", "p", "grid", "h", "ol"]);
  assert.deepEqual(g[2].items.map((i) => i.wide), [false, false, true]);
  assert.deepEqual(g[2].items[0].blocks.map((b) => b.t), ["h", "table"]);
});

test("groupBlocks: a short paragraph before a table is its caption; the heading and other runs span the row", () => {
  const g = groupBlocks(parseMarkdown("### Usage (tasks 7)\n\nBy area\n\n" + T2 + "\nBy type\n\n" + T2 + "\nBy all\n\n" + T5 + "\n- a bullet\n"));
  const items = g[0].items;
  assert.deepEqual(items.map((i) => i.blocks.map((b) => b.t).join("+")), ["h", "p+table", "p+table", "p+table", "ul"]);
  assert.deepEqual(items.map((i) => i.wide), [true, false, false, true, true]);
});

test("groupBlocks: long bullets or paragraphs make the item wide; a ### with 4+ column table plus prose stays one wide item", () => {
  const g = groupBlocks(parseMarkdown("### Earlier changes\n\n" + T5 + "\n" + "Long paragraph. ".repeat(10) + "\n\n1. **Do it.** now\n"));
  assert.equal(g[0].items.length, 1);
  assert.equal(g[0].items[0].wide, true);
  assert.equal(groupBlocks(parseMarkdown("### B\n\n- " + "x".repeat(70) + "\n"))[0].items[0].wide, true);
});
