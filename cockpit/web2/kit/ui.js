/* Themed controls (docs/v2/contracts.md §8.3). No native select, checkbox, radio, date or time picker.
   Every control returns a small handle; values live in the caller's module state, never in the DOM. */
import { H } from "./charts.js";

/** segmented(host, {options: [{value, label}], value, onChange}) -> {set(value)} */
export function segmented(host, { options, value, onChange }) {
  const wrap = H("div", "ui-seg", null, host);
  wrap.setAttribute("role", "group");
  const btns = options.map((o) => {
    const b = H("button", null, o.label, wrap);
    b.type = "button";
    b.addEventListener("click", () => { set(o.value); if (onChange) onChange(o.value); });
    return b;
  });
  function set(v) {
    value = v;
    btns.forEach((b, i) => {
      const on = options[i].value === v;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  set(value);
  return { set, el: wrap };
}

/** stepper(host, {value, min, max, step, onChange}) -> {set(value)} */
export function stepper(host, { value, min = -Infinity, max = Infinity, step = 1, onChange, format }) {
  const wrap = H("div", "ui-step", null, host);
  const dec = H("button", null, "−", wrap);
  const out = H("output", null, null, wrap);
  const inc = H("button", null, "+", wrap);
  dec.type = inc.type = "button";
  dec.setAttribute("aria-label", "Decrease");
  inc.setAttribute("aria-label", "Increase");
  function set(v) {
    value = Math.max(min, Math.min(max, v));
    out.textContent = format ? format(value) : String(value);
    dec.disabled = value <= min;
    inc.disabled = value >= max;
  }
  dec.addEventListener("click", () => { set(value - step); if (onChange) onChange(value); });
  inc.addEventListener("click", () => { set(value + step); if (onChange) onChange(value); });
  set(value);
  return { set, el: wrap };
}

/** dropdown(host, {items: [{value, label}], value, onChange}) -> {set(value), setItems(items)} */
export function dropdown(host, { items, value, onChange }) {
  const wrap = H("div", "ui-dd", null, host);
  const btn = H("button", null, null, wrap);
  btn.type = "button";
  btn.setAttribute("aria-haspopup", "listbox");
  const pop = H("div", "pop", null, wrap);
  pop.setAttribute("role", "listbox");
  function label() { const it = items.find((i) => i.value === value); return it ? it.label : ""; }
  function close() { wrap.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); document.removeEventListener("pointerdown", away); }
  function paint() {
    pop.textContent = "";
    items.forEach((it) => {
      const b = H("button", it.value === value ? "on" : null, it.label, pop);
      b.type = "button";
      b.setAttribute("role", "option");
      b.addEventListener("click", () => { value = it.value; btn.textContent = label(); paint(); close(); if (onChange) onChange(value); });
    });
    btn.textContent = label();
  }
  // The outside-click listener exists only while the popup is open, so nothing accumulates per repaint or visit.
  const away = (ev) => { if (!wrap.isConnected || !wrap.contains(ev.target)) close(); };
  btn.addEventListener("click", () => {
    const open = !wrap.classList.contains("open");
    document.querySelectorAll(".ui-dd.open").forEach((d) => d.classList.remove("open"));
    wrap.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    document.removeEventListener("pointerdown", away);
    if (open) document.addEventListener("pointerdown", away);
  });
  wrap.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { close(); btn.focus(); } });
  paint();
  return { set(v) { value = v; paint(); }, setItems(next) { items = next; paint(); }, el: wrap };
}

/** button(host, {label, onClick, disabled?, reason?}) -> {setDisabled(flag, reason?)}. The reason is visible text. */
export function button(host, { label, onClick, disabled = false, reason = "" }) {
  const wrap = H("span", "ui-btn-wrap", null, host);
  const b = H("button", "ui-btn", label, wrap);
  b.type = "button";
  const why = H("span", "ui-reason", null, wrap);
  function setDisabled(flag, r) {
    b.disabled = !!flag;
    why.textContent = flag ? (r != null ? r : reason) : "";
  }
  b.addEventListener("click", () => { if (!b.disabled && onClick) onClick(); });
  setDisabled(disabled, reason);
  return { setDisabled, el: b };
}

/** toast(text, kind) with kind ok | warn | err (default ok). */
export function toast(text, kind = "ok") {
  let box = document.getElementById("toasts");
  if (!box) { box = H("div", null, null, document.body); box.id = "toasts"; }
  const t = H("div", "toast " + kind, text, box);
  t.setAttribute("role", "status");
  setTimeout(() => t.remove(), 4000);
  return t;
}

/** field(host, {label, value, onInput}) -> {set(value), get()}; a themed text input. */
export function field(host, { label, value = "", onInput, placeholder = "" }) {
  const wrap = H("label", "ui-field", null, host);
  H("span", null, label, wrap);
  const inp = H("input", null, null, wrap);
  inp.type = "text";
  inp.value = value;
  inp.placeholder = placeholder;
  inp.addEventListener("input", () => { if (onInput) onInput(inp.value); });
  return { set(v) { inp.value = v; }, get() { return inp.value; }, el: inp };
}
