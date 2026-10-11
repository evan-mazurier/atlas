// Run: node --test cockpit/test_web2_markdown.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { parseMarkdown, inline } from "./web2/kit/markdown.js";

test("headings, paragraphs and bullets", () => {
  const b = parseMarkdown("# Week 41\n\nSome **bold** text\nsecond line.\n\n- one\n- `two`\n* three\n\n## Next");
  assert.deepEqual(b, [
    { t: "h", level: 1, text: "Week 41" },
    { t: "p", text: "Some bold text second line." },
    { t: "ul", items: ["one", "two", "three"] },
    { t: "h", level: 2, text: "Next" },
  ]);
});

test("simple tables", () => {
  const b = parseMarkdown("| Area | Hours |\n| --- | ---: |\n| Atlas | 4 |\n| MD | 2 |\n\nafter");
  assert.deepEqual(b[0], { t: "table", head: ["Area", "Hours"], rows: [["Atlas", "4"], ["MD", "2"]] });
  assert.deepEqual(b[1], { t: "p", text: "after" });
});

test("raw html and links stay plain text, never markup", () => {
  const b = parseMarkdown("<img src=x onerror=alert(1)>\n\n[click](javascript:alert(1))");
  assert.equal(b[0].text, "<img src=x onerror=alert(1)>");   // rendered with textContent: shown literally
  assert.equal(b[1].text, "click");
  assert.equal(inline(null), "");
});

test("empty input", () => { assert.deepEqual(parseMarkdown(""), []); });
