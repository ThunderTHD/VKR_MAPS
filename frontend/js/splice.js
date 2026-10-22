// Редактор рассветки муфты: два кабеля друг против друга, линии — сварки между ними.
// Щелчок по свободному волокну слева, затем по свободному справа — сварка.
// Щелчок по сваренному волокну или линии — подробности сварки (потери, примечание, удаление).
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const closureId = Number(params.get("closure"));

let user;
let sheet;
let left = null;          // ключ кабеля «id:конец» в левой колонке
let right = null;
let pending = null;       // выбранное свободное волокно { side, fiber }
let canEdit = false;
let isAdmin = false;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

const keyOf = (c) => `${c.id}:${c.end}`;
const cableByKey = (k) => sheet.cables.find((c) => keyOf(c) === k);
const endRu = (end) => (end === "a" ? "начало кабеля" : "конец кабеля");

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 4500);
}

async function load({ keepPair = true } = {}) {
  sheet = await api(`/api/closures/${closureId}/sheet`);
  const c = sheet.closure;
  document.title = `Рассветка ${c.number} — ГИС ВОЛС`;
  $("title").textContent = `Муфта ${c.number}`;
  $("subtitle").textContent = [`узел ${c.node.label}`, c.model].filter(Boolean).join(" · ");
  $("btn-map").href = `/#obj=closure:${c.id}`;
  $("btn-csv").href = `/api/closures/${c.id}/sheet.csv`;
  const keys = sheet.cables.map(keyOf);
  if (!keepPair || !keys.includes(left)) left = keys[0] ?? null;
  if (!keepPair || !keys.includes(right) || right === left) right = bestPartner(left) ?? keys.find((k) => k !== left) ?? null;
  pending = null;
  render();
}

/** Кабель, с которым у левого больше всего сварок в этой муфте — его логично показать справа. */
function bestPartner(key) {
  const c = key && cableByKey(key);
  if (!c) return null;
  const counts = {};
  for (const f of c.fibers) {
    if (f.state !== "splice") continue;
    const partner = sheet.cables.find((x) => x.id === f.partner.cable_id && keyOf(x) !== key);
    if (partner) counts[keyOf(partner)] = (counts[keyOf(partner)] || 0) + 1;
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
}

// ---------------------------------------------------------------- боковая панель

function renderSide() {
  $("no-cables").hidden = sheet.cables.length > 0;
  $("cables").replaceChildren(...sheet.cables.map((c) => {
    const k = keyOf(c);
    const side = k === left ? "слева" : k === right ? "справа" : null;
    return el("li", { class: `cable-item ${side ? "on" : ""}` },
      el("button", { type: "button", class: "link", onclick: () => pick(k) },
        el("b", {}, c.name), el("span", { class: "muted small" }, ` · ${endRu(c.end)}`),
        el("div", { class: "muted small" }, `${c.mark} · свободно ${c.free} из ${c.fibers.length}`,
          side ? ` · ${side}` : "")));
  }));

  $("transit-box").hidden = sheet.transit.length === 0;
  $("transit").replaceChildren(...sheet.transit.map((t) => {
    const li = el("li", { class: "cable-item" }, el("b", {}, t.name), el("div", { class: "muted small" }, t.mark));
    if (isAdmin) {
      li.append(el("button", { type: "button", class: "btn btn-small", onclick: () => splitForm(li, t) },
        "Разрезать здесь"));
    }
    return li;
  }));

  $("bulk-box").hidden = !(canEdit && left && right);
  $("issues").replaceChildren(...(sheet.issues.length ? sheet.issues : [{ level: "info", message: "Замечаний нет" }])
    .map((i) => el("li", { class: `w-${i.level}` }, i.message)));
}

function pick(k) {
  if (k === left || k === right) return;
  // Новый кабель встаёт на место того, что выбран раньше; второй остаётся
  right = left;
  left = k;
  if (right === null) right = bestPartner(left);
  pending = null;
  render();
}

function splitForm(li, t) {
  const input = el("input", { value: `${t.name} (от ${sheet.closure.number})`, "aria-label": "Название второй части" });
  const transit = el("input", { type: "checkbox", checked: true });
  const form = el("form", { class: "edit-form compact split-form" },
    el("label", { class: "field" }, "Название части после муфты", input),
    el("label", { class: "toggle" }, transit, el("span", {}, "Соединить все волокна транзитом")),
    el("div", { class: "form-actions" }, el("button", { class: "btn btn-primary btn-small", type: "submit" }, "Разрезать"),
      el("button", { class: "btn btn-small", type: "button", onclick: () => renderSide() }, "Отмена")));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api(`/api/closures/${closureId}/split-cable`, { method: "POST",
        body: { cable_id: t.id, new_name: input.value, transit: transit.checked } });
      toast(`Кабель «${t.name}» разрезан в муфте`);
      await load({ keepPair: false });
    } catch (err) {
      toast(err.message);
    }
  });
  li.append(form);
  input.focus();
}

// ---------------------------------------------------------------- схема пары кабелей

function fillPickers() {
  for (const [sel, value] of [[$("left"), left], [$("right"), right]]) {
    sel.replaceChildren(...sheet.cables.map((c) => new Option(`${c.name} — ${endRu(c.end)}`, keyOf(c), false,
      keyOf(c) === value)));
    if (!value) sel.prepend(new Option("—", "", true, true));
  }
}

function fiberRow(c, f, side) {
  const other = cableByKey(side === "left" ? right : left);
  let status = "свободно";
  if (f.state === "splice") {
    status = other && f.partner.cable_id === other.id ? "" : `→ ${f.partner.cable}, ОВ ${f.partner.fiber}`;
  } else if (f.state === "port") status = f.label;
  else if (f.state === "elsewhere") status = `в другой муфте узла → ${f.partner.cable}, ОВ ${f.partner.fiber}`;
  const isPending = pending && pending.fiber.id === f.id && pending.side === side;
  const row = el("button", {
    type: "button", class: `fiber-row st-${f.state} ${isPending ? "pending" : ""}`, "data-fiber": f.id,
    title: `${c.name}, ОВ ${f.number}: модуль ${f.module_no} (${f.module_color || "—"}), волокно ${f.color || "—"}`,
    onclick: () => clickFiber(side, c, f),
  },
  el("span", { class: "module", style: `background:${f.module_hex}` }),
  el("span", { class: "dot", style: `background:${f.hex}` }),
  el("span", { class: "num" }, f.number),
  el("span", { class: "fcolor muted small" }, f.color || ""),
  el("span", { class: "fstate small" }, status));
  return row;
}

function renderDiagram() {
  fillPickers();
  const lc = cableByKey(left);
  const rc = cableByKey(right);
  const head = (c, side) => el("div", { class: "col-head" }, c ? el("b", {}, c.name) : "—",
    c ? el("div", { class: "muted small" }, `${endRu(c.end)} · ${side}`) : null);
  $("col-left").replaceChildren(head(lc, "слева"), ...(lc ? lc.fibers.map((f) => fiberRow(lc, f, "left")) : []));
  $("col-right").replaceChildren(head(rc, "справа"), ...(rc ? rc.fibers.map((f) => fiberRow(rc, f, "right")) : []));
  $("hint").textContent = !lc || !rc ? "В муфте нужен хотя бы ещё один кабель, чтобы сваривать."
    : !canEdit ? "Просмотр: сваривать могут монтажник и администратор."
      : pending ? `Выбрано: ${pending.side === "left" ? lc.name : rc.name}, ОВ ${pending.fiber.number}. ` +
        "Теперь щёлкните свободное волокно на другой стороне (Esc — отмена)."
        : "Щёлкните свободное волокно слева, затем свободное справа. Щелчок по линии — подробности сварки.";
  requestAnimationFrame(drawLinks);
}

function drawLinks() {
  const svg = $("links");
  svg.replaceChildren();
  const lc = cableByKey(left);
  const rc = cableByKey(right);
  if (!lc || !rc) return;
  const box = svg.getBoundingClientRect();
  const w = box.width;
  const yOf = (colId, fiberId) => {
    const r = document.querySelector(`#${colId} [data-fiber="${fiberId}"]`)?.getBoundingClientRect();
    return r ? r.top + r.height / 2 - box.top : null;
  };
  const ns = "http://www.w3.org/2000/svg";
  for (const f of lc.fibers) {
    if (f.state !== "splice" || f.partner.cable_id !== rc.id) continue;
    const rf = rc.fibers.find((x) => x.number === f.partner.fiber);
    if (!rf || rf.splice_id !== f.splice_id) continue;
    const y1 = yOf("col-left", f.id);
    const y2 = yOf("col-right", rf.id);
    if (y1 === null || y2 === null) continue;
    const d = `M0 ${y1} C ${w * 0.5} ${y1}, ${w * 0.5} ${y2}, ${w} ${y2}`;
    const g = document.createElementNS(ns, "g");
    g.setAttribute("class", "link-line");
    g.addEventListener("click", () => showDetail(f.splice_id));
    for (const [cls, color] of [["casing", null], ["core", f.hex], ["hit", null]]) {
      const p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      p.setAttribute("class", cls);
      if (color) p.setAttribute("stroke", color);
      g.append(p);
    }
    const title = document.createElementNS(ns, "title");
    title.textContent = `${lc.name} ОВ ${f.number} ↔ ${rc.name} ОВ ${rf.number}`;
    g.append(title);
    svg.append(g);
  }
}

async function clickFiber(side, c, f) {
  if (f.state === "splice") {
    showDetail(f.splice_id);
    return;
  }
  if (f.state !== "free") {
    toast(f.state === "port" ? `Волокно заведено на ${f.label}` : "Волокно сварено в другой муфте этого узла");
    return;
  }
  if (!canEdit) return;
  if (!pending || pending.side === side) {
    pending = { side, fiber: f, cable: c };
    renderDiagram();
    return;
  }
  const [a, b] = pending.side === "left" ? [pending, { fiber: f, cable: c }] : [{ fiber: f, cable: c }, pending];
  pending = null;
  try {
    await api(`/api/closures/${closureId}/splices`, { method: "POST", body: {
      fiber_a_id: a.fiber.id, end_a: a.cable.end, fiber_b_id: b.fiber.id, end_b: b.cable.end } });
    await load();
  } catch (err) {
    toast(err.message);
    renderDiagram();
  }
}

// ---------------------------------------------------------------- подробности сварки

function showDetail(spliceId) {
  const s = sheet.splices.find((x) => x.id === spliceId);
  if (!s) return;
  const box = $("detail");
  const side = (x) => el("div", { class: "detail-end" }, el("span", { class: "dot", style: `background:${x.hex}` }),
    el("b", {}, x.cable), ` · ОВ ${x.fiber}`, el("span", { class: "muted small" }, ` (${x.color || "—"}, модуль ${x.module_no})`));
  const num = (name, value) => el("input", { name, type: "number", step: "0.01", value: value ?? "",
    disabled: !canEdit, placeholder: "—" });
  const form = el("form", { class: "edit-form compact" },
    side(s.a), el("div", { class: "muted small swap" }, "↕"), side(s.b),
    el("label", { class: "field" }, "Потери по сварочному аппарату, дБ", num("loss_splicer_db", s.loss_db)),
    el("div", { class: "field-row" },
      el("label", { class: "field" }, "Рефлектометр A→Б", num("loss_otdr_ab_db", s.loss_otdr_ab_db)),
      el("label", { class: "field" }, "Б→A", num("loss_otdr_ba_db", s.loss_otdr_ba_db))),
    el("label", { class: "field" }, "Примечание", el("input", { name: "note", value: s.note ?? "", disabled: !canEdit })),
    canEdit ? el("div", { class: "form-actions" },
      el("button", { class: "btn btn-primary btn-small", type: "submit" }, "Сохранить"),
      el("button", { class: "btn btn-small btn-danger", type: "button", onclick: () => removeSplice(s) }, "Разварить"),
      el("button", { class: "btn btn-small btn-ghost", type: "button", onclick: hideDetail }, "Закрыть"))
      : el("button", { class: "btn btn-small", type: "button", onclick: hideDetail }, "Закрыть"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const n = (k) => (fd.get(k) === "" ? null : Number(fd.get(k)));
    try {
      await api(`/api/splices/${s.id}`, { method: "PATCH", body: { loss_splicer_db: n("loss_splicer_db"),
        loss_otdr_ab_db: n("loss_otdr_ab_db"), loss_otdr_ba_db: n("loss_otdr_ba_db"), note: fd.get("note") } });
      toast("Сохранено");
      await load();
      showDetail(s.id);
    } catch (err) {
      toast(err.message);
    }
  });
  box.replaceChildren(form);
  $("detail-box").hidden = false;
  document.querySelectorAll(".splice-table tr").forEach((tr) => tr.classList.toggle("on", tr.dataset.id === String(s.id)));
  $("detail-box").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function hideDetail() {
  $("detail-box").hidden = true;
  document.querySelectorAll(".splice-table tr.on").forEach((tr) => tr.classList.remove("on"));
}

async function removeSplice(s) {
  if (!confirm(`Разварить ${s.a.cable} ОВ ${s.a.fiber} ↔ ${s.b.cable} ОВ ${s.b.fiber}?`)) return;
  try {
    await api(`/api/splices/${s.id}`, { method: "DELETE" });
    hideDetail();
    await load();
  } catch (err) {
    toast(err.message);
  }
}

// ---------------------------------------------------------------- таблица всех сварок

function renderTable() {
  $("splice-count").textContent = `(${sheet.splices.length})`;
  const dot = (x) => el("span", { class: "dot", style: `background:${x.hex}`, title: x.color || "" });
  $("splices").replaceChildren(...(sheet.splices.length ? sheet.splices.map((s) => el("tr", {
    "data-id": s.id, class: "clickable", onclick: () => {
      // Показать на схеме пару кабелей этой сварки
      const ka = sheet.cables.find((c) => c.id === s.a.cable_id && c.end === s.a.end);
      const kb = sheet.cables.find((c) => c.id === s.b.cable_id && c.end === s.b.end);
      if (ka && kb) {
        left = keyOf(ka);
        right = keyOf(kb);
        renderSide();
        renderDiagram();
      }
      showDetail(s.id);
    } },
  el("td", {}, s.a.cable), el("td", {}, s.a.fiber), el("td", {}, dot(s.a)),
  el("td", {}, s.b.cable), el("td", {}, s.b.fiber), el("td", {}, dot(s.b)),
  el("td", { class: s.loss_db > 0.2 ? "bad" : "" }, s.loss_db ?? "—"), el("td", {}, s.note ?? ""),
  el("td", { class: "no-print" }, canEdit ? el("button", { type: "button", class: "btn btn-small btn-ghost",
    title: "Разварить", onclick: (e) => { e.stopPropagation(); removeSplice(s); } }, "✕") : "")))
    : [el("tr", {}, el("td", { colspan: 9, class: "muted" }, "Сварок пока нет"))]));
}

function render() {
  renderSide();
  renderDiagram();
  renderTable();
}

// ---------------------------------------------------------------- запуск

async function main() {
  if (!closureId) {
    $("title").textContent = "Не указана муфта";
    return;
  }
  user = await api("/api/auth/me");
  canEdit = ["admin", "installer"].includes(user.role);
  isAdmin = user.role === "admin";
  $("left").addEventListener("change", (e) => {
    left = e.target.value;
    if (left === right) right = bestPartner(left);
    pending = null;
    render();
  });
  $("right").addEventListener("change", (e) => {
    right = e.target.value;
    if (left === right) left = bestPartner(right);
    pending = null;
    render();
  });
  $("bulk").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const lc = cableByKey(left);
    const rc = cableByKey(right);
    try {
      const r = await api(`/api/closures/${closureId}/splices/range`, { method: "POST", body: {
        cable_a_id: lc.id, end_a: lc.end, from_a: Number(fd.get("from_a")),
        cable_b_id: rc.id, end_b: rc.end, from_b: Number(fd.get("from_b")),
        count: Number(fd.get("count")), note: fd.get("note") || null } });
      toast(`Сварено волокон: ${r.count}`);
      await load();
    } catch (err) {
      toast(err.message);
    }
  });
  $("btn-print").addEventListener("click", () => window.print());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && pending) {
      pending = null;
      renderDiagram();
    }
  });
  window.addEventListener("resize", () => requestAnimationFrame(drawLinks));
  await load({ keepPair: false });
}

main().catch((e) => toast(e.message));
