// Калибровка: расчёт, просмотр результата «до/после», применение и откат.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
let runs = [];

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

const pct = (v, d = 2) => `${(100 * v).toFixed(d).replace(".", ",")} %`;
const m = (v) => (v === null || v === undefined ? "—" : `${String(v).replace(".", ",")} м`);

function toast(t) {
  $("toast").textContent = t;
  $("toast").hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => ($("toast").hidden = true), 5000);
}

function state(r) {
  if (r.reverted) return "отменён";
  if (r.applied_at) return "применён";
  return "рассчитан";
}

/** Пара «до → после» с полосками, чтобы разница была видна сразу. */
function compare(label, before, after, unit = " м", lowerBetter = true) {
  const max = Math.max(before ?? 0, after ?? 0) || 1;
  const better = lowerBetter ? after < before : after > before;
  return el("div", { class: "cmp" }, el("div", { class: "cmp-label" }, label),
    el("div", { class: "cmp-bars" },
      el("div", { class: "cmp-row" }, el("span", { class: "muted small" }, "до"),
        el("div", { class: "cmp-bar before", style: `width:${(100 * (before ?? 0)) / max}%` }),
        el("b", {}, `${before ?? "—"}${unit}`)),
      el("div", { class: "cmp-row" }, el("span", { class: "muted small" }, "после"),
        el("div", { class: `cmp-bar after ${better ? "good" : ""}`, style: `width:${(100 * (after ?? 0)) / max}%` }),
        el("b", {}, `${after ?? "—"}${unit}`))));
}

function show(r) {
  const p = r.params;
  const box = $("result");
  const h = p.holdout;
  const parts = [
    el("h2", { class: "sec-title" }, `Прогон №${r.id} — ${state(r)}`),
    el("div", { class: "summary" },
      el("div", { class: "sum" }, el("b", {}, p.observations), el("span", {}, `наблюдений: измерений ИД ${p.by_kind.measurement}, повреждений ${p.by_kind.incident}`)),
      el("div", { class: "sum" }, el("b", {}, `+${pct(p.map_stretch, 1)}`), el("span", {}, `реальная трасса длиннее нарисованной (± ${pct(p.map_stretch_err_2s, 1)})`)),
      el("div", { class: "sum" }, el("b", {}, p.geo_err_pct ? `${p.geo_err_pct} %` : "—"), el("span", {}, "погрешность длины «по карте» после поправки (было 25 %)"))),
    el("h3", {}, "Проверка на отложенных данных"),
    h.before.n ? el("div", { class: "cmps" },
      compare(`Ошибка предсказания длины (RMSE), ${h.before.n} наблюдений`, h.before.rmse_m, h.after.rmse_m),
      compare("Систематический сдвиг (среднее)", Math.abs(h.before.bias_m), Math.abs(h.after.bias_m)))
      : el("p", { class: "muted" }, "Данных слишком мало, чтобы отложить часть для проверки."),
  ];
  if (p.simulation) {
    const s = p.simulation;
    parts.push(el("h3", {}, "Моделирование повреждений на тестовой сети (200 случаев)"),
      el("div", { class: "cmps" },
        compare("Средняя ошибка места", s.before.error_mean_m, s.after.error_mean_m),
        compare("Медиана ошибки", s.before.error_median_m, s.after.error_median_m),
        compare("90 % ошибок не больше", s.before.error_p90_m, s.after.error_p90_m),
        compare("Средняя полуширина интервала", s.before.half_width_mean_m, s.after.half_width_mean_m),
        compare("Попадание в интервал", s.before.coverage_pct, s.after.coverage_pct, " %", false)));
  }
  parts.push(el("h3", {}, "Укрутка по маркам кабеля"),
    r.proposals.length ? el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, ["Марка", "Было", "Стало", "Погрешность (95 %)", "Наблюдений", "Кабелей"].map((t) => el("th", {}, t)))),
      el("tbody", {}, r.proposals.map((x) => el("tr", {}, el("td", {}, x.mark), el("td", {}, pct(x.prior)),
        el("td", {}, el("b", {}, pct(x.new))), el("td", {}, `± ${pct(x.err_2s)}`), el("td", {}, x.n_obs),
        el("td", {}, x.cables.length))))))
      : el("p", { class: "muted" }, "По маркам кабеля данных недостаточно — укрутка не меняется."));
  if (p.skipped?.length) {
    parts.push(el("details", {}, el("summary", {}, `Пропущено данных: ${p.skipped.length}`),
      el("ul", { class: "small" }, p.skipped.map((s) => el("li", {}, s)))));
  }
  const actions = el("div", { class: "form-actions" });
  if (!r.applied_at) {
    actions.append(el("button", { class: "btn btn-primary", type: "button", onclick: () => act(r.id, "apply") }, "Применить"));
  } else if (!r.reverted) {
    actions.append(el("button", { class: "btn btn-danger", type: "button", onclick: () => act(r.id, "revert") }, "Отменить калибровку"));
  }
  parts.push(actions);
  box.replaceChildren(...parts);
  box.hidden = false;
}

async function act(id, what) {
  if (what === "revert" && !confirm("Вернуть укрутку и поправку карты к значениям до этой калибровки?")) return;
  try {
    const r = await api(`/api/calibration/runs/${id}/${what}`, { method: "POST" });
    toast(what === "apply" ? "Калибровка применена — локализация использует новые параметры" : "Калибровка отменена");
    await loadRuns();
    show(r);
  } catch (e) {
    toast(e.message);
  }
}

async function loadRuns() {
  runs = await api("/api/calibration/runs");
  $("runs").replaceChildren(...(runs.length ? runs.map((r) => el("tr", { class: "clickable", onclick: () => show(r) },
    el("td", {}, r.id), el("td", {}, new Date(r.created_at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })),
    el("td", {}, r.params.observations), el("td", {}, `+${pct(r.params.map_stretch, 1)}`), el("td", {}, r.proposals.length),
    el("td", {}, r.params.holdout.before.n ? `${m(r.params.holdout.before.rmse_m)} → ${m(r.params.holdout.after.rmse_m)}` : "—"),
    el("td", {}, state(r))))
    : [el("tr", {}, el("td", { colspan: 7, class: "muted" }, "Калибровка ещё не выполнялась"))]));
}

$("btn-run").addEventListener("click", async () => {
  const btn = $("btn-run");
  btn.disabled = true;
  $("run-status").textContent = "Считаю… на тестовой сети с моделированием это занимает до полуминуты";
  try {
    const r = await api("/api/calibration/runs", { method: "POST", body: {} });
    await loadRuns();
    show(r);
    $("run-status").textContent = "";
  } catch (e) {
    $("run-status").textContent = "";
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
});

loadRuns().then(() => runs[0] && show(runs[0])).catch((e) => toast(e.message));
