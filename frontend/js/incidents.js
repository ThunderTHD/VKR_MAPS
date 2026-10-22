// Журнал повреждений: сводка точности прогнозов и список с переходом на карту.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);

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

const m = (v) => (v === null || v === undefined ? "—" : `${Math.round(v)} м`);
const dt = (s) => (s ? new Date(s).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—");

function tile(value, label, cls = "") {
  return el("div", { class: `sum ${cls}` }, el("b", {}, value ?? "—"), el("span", {}, label));
}

async function load() {
  const data = await api("/api/incidents", { params: { status: $("status").value } });
  const s = data.stats;
  $("summary").replaceChildren(
    tile(s.by_status.localized, "в работе (место рассчитано)", s.by_status.localized ? "warn" : ""),
    tile(s.repaired, "устранено"),
    tile(s.repaired ? m(s.route_error_median_m) : "—", "медиана ошибки по трассе"),
    tile(s.repaired ? m(s.route_error_p90_m) : "—", "90 % ошибок не больше"),
    tile(s.repaired ? `± ${Math.round(s.half_width_mean_m)} м` : "—", "средний интервал прогноза"),
    tile(s.repaired ? `${s.within_pct} %` : "—", "факт попал в интервал", s.within_pct >= 90 ? "ok" : "warn"),
  );
  $("sim-note").hidden = !data.items.some((i) => i.simulated);
  $("rows").replaceChildren(...(data.items.length ? data.items.map((i) => el("tr", {
    class: "clickable", onclick: () => { location.href = `/#obj=incident:${i.id}`; } },
  el("td", {}, i.id), el("td", {}, i.title || "—", i.simulated ? el("span", { class: "badge badge-test" }, "тест") : null),
  el("td", {}, el("span", { class: `st st-${i.status}` }, i.status_ru)), el("td", {}, dt(i.detected_at)),
  el("td", {}, i.cable ? `${i.cable}, ОВ ${i.fiber}` : "—"), el("td", {}, i.otdr_distance_m ? `${i.otdr_distance_m} м` : "—"),
  el("td", {}, i.half_width_m ? `± ${Math.round(i.half_width_m)} м` : "—"), el("td", {}, m(i.route_error_m)),
  el("td", {}, i.within_interval === null || i.within_interval === undefined ? "—" : i.within_interval ? "да" : "нет")))
    : [el("tr", {}, el("td", { colspan: 9, class: "muted" }, "Записей нет"))]));
}

$("status").addEventListener("change", load);
load().catch((e) => { $("toast").textContent = e.message; $("toast").hidden = false; });
