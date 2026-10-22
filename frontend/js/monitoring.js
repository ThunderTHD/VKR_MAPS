// Мониторинг узлов доступа: сводка, авария с вероятным местом обрыва, имитация, таблица, события.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
const ST = { up: "доступен", down: "недоступен", unknown: "нет данных" };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

const dt = (s) => (s ? new Date(s).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—");
const tile = (value, label, cls = "") => el("div", { class: `sum ${cls}` }, el("b", {}, value ?? "—"), el("span", {}, label));

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 5000);
}

let me = null;
let data = null;

function renderAlarm(cor) {
  const box = $("alarm");
  const down = data.nodes.filter((n) => n.status === "down");
  box.hidden = !down.length;
  if (!down.length) return;
  const parts = [el("h2", {}, `Авария: недоступно узлов — ${down.length}`)];
  if (cor.uplink) parts.push(el("p", { class: "alarm-main" }, cor.uplink.message));
  const best = cor.candidates[0];
  if (best) {
    const seg = best.segments.map((s) => s.label).join(", ");
    parts.push(el("p", { class: "alarm-main" },
      el("b", {}, "Вероятное место обрыва: "), `участок ${seg} `,
      el("span", { class: `badge conf-${best.confidence}` }, `уверенность ${best.confidence}`)),
    el("p", { class: "muted small" }, `Через этот участок проходят тракты ${best.down_through} из ${best.down_total} ` +
      `недоступных узлов` + (best.alive_through ? `, но и ${best.alive_through} работающих — возможно, причина другая` :
        " и ни одного работающего")));
    if (best.otdr) {
      parts.push(el("p", {}, el("b", {}, "Рефлектометр: "),
        `подключите на «${best.otdr.from_port}» (тракт узла ${best.otdr.via_node}) — обрыв ожидается на `,
        el("b", {}, `${best.otdr.min_m}–${best.otdr.max_m} м`), ` (${best.otdr.cables.join("; ")}).`));
    }
    const alt = cor.candidates.slice(1);
    if (alt.length) {
      parts.push(el("details", {}, el("summary", {}, "Другие варианты"), el("ul", { class: "plain-list" },
        alt.map((c) => el("li", {}, `${c.segments.map((s) => s.label).join(", ")} — ${c.down_through} из ${c.down_total}` +
          ` недоступных, уверенность ${c.confidence}`)))));
    }
  } else if (cor.untraced.length) {
    parts.push(el("p", { class: "muted" }, "У недоступных узлов в системе не заведены линейные тракты (порт кросса) — " +
      "место обрыва по схеме не определить."));
  }
  parts.push(el("div", { class: "toolbar" },
    el("a", { class: "btn btn-small btn-primary", href: "/#alarm=1" }, "Показать на карте"),
    best ? el("a", { class: "btn btn-small", href: `/#obj=segment:${best.segment_ids[0]}` }, "Карточка участка") : null));
  box.replaceChildren(...parts);
}

function renderSim() {
  const show = data.mode === "simulate" && me.role === "admin";
  $("sim").hidden = !show;
  if (!show) return;
  $("faults").replaceChildren(...(data.sim_faults.length ? data.sim_faults.map((f) => el("li", {},
    f.kind === "cut" ? `Обрыв участка ${f.segment}` : `Нет питания: ${f.hostname}`,
    el("span", { class: "muted small" }, ` · ${f.auto ? "самопроизвольная авария имитатора" : f.note}, с ${dt(f.created_at)}` +
      (f.expires_at ? ` до ${dt(f.expires_at)}` : "")))) : [el("li", { class: "muted" }, "Активных аварий нет")]));
}

function renderTable() {
  const f = $("filter").value;
  const rows = data.nodes.filter((n) => !f || n.status === f);
  $("rows").replaceChildren(...(rows.length ? rows.map((n) => el("tr", {
    class: "clickable", onclick: () => { location.href = `/#obj=access_node:${n.id}`; } },
  el("td", {}, n.hostname || `#${n.id}`), el("td", {}, n.operator || "—"), el("td", {}, n.ip || "—"),
  el("td", {}, el("span", { class: `st st-${n.status}` }, ST[n.status]),
    n.status === "up" && n.fail_count ? el("span", { class: "muted small" }, ` (пропусков: ${n.fail_count})`) : null),
  el("td", {}, n.last_rtt_ms != null ? `${n.last_rtt_ms} мс` : "—"), el("td", {}, dt(n.changed_at)),
  el("td", {}, n.address || "—"))) : [el("tr", {}, el("td", { colspan: 7, class: "muted" }, "Нет узлов"))]));
}

function renderEvents() {
  $("events").replaceChildren(...(data.events.length ? data.events.map((e) => el("li", {},
    el("span", { class: "muted small" }, dt(e.ts)), " ", el("a", { href: `/#obj=access_node:${e.id}` }, e.hostname),
    " — ", el("span", { class: `st st-${e.status}` }, ST[e.status]))) : [el("li", { class: "muted" }, "Событий пока нет")]));
}

async function load() {
  const [d, cor] = await Promise.all([api("/api/monitoring"), api("/api/monitoring/correlate")]);
  data = d;
  const modeRu = { simulate: "имитация (нет доступа к сети узлов)", ping: "опрос ping", off: "выключен" }[d.mode];
  $("mode").textContent = `Режим: ${modeRu} · опрос каждые ${d.interval_min} мин · ` +
    `«недоступен» после ${d.threshold} пропусков подряд · последний опрос: ${dt(d.last_poll_at)}`;
  $("summary").replaceChildren(
    tile(d.counts.up, "доступны", "ok"), tile(d.counts.down, "недоступны", d.counts.down ? "bad" : ""),
    tile(d.counts.unknown, "нет данных"), tile(d.nodes.length, "всего узлов"));
  renderAlarm(cor);
  renderSim();
  renderTable();
  renderEvents();
}

async function act(fn, msg) {
  try {
    await fn();
    if (msg) toast(msg);
    await load();
  } catch (e) {
    toast(e.message);
  }
}

async function main() {
  me = await api("/api/auth/me");
  $("btn-poll").hidden = me.role === "viewer";
  $("btn-poll").addEventListener("click", () => act(() => api("/api/monitoring/poll", { method: "POST" }), "Опрос выполнен"));
  $("btn-random").addEventListener("click", () => act(
    () => api("/api/monitoring/simulate", { method: "POST", body: { random_cut: true } }), "Имитирован обрыв кабеля"));
  $("btn-clear").addEventListener("click", () => act(
    () => api("/api/monitoring/simulate", { method: "DELETE" }), "Аварии сняты, связь восстановлена"));
  $("filter").addEventListener("change", renderTable);
  await load();
  setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 30_000);
}

main().catch((e) => toast(e.message));
