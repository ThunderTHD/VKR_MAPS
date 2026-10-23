// Эксперимент для ВКР: запуск, ход расчёта, графики (SVG без библиотек) и таблицы серий E1–E5.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";

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

function s(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  node.append(...children);
  return node;
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 5000);
}

const num = (v, d = 1) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: d }));
const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];

// ---------- графики ----------

/** Сгруппированные столбцы: categories × series. Значения подписаны над столбцами, подсказка — при наведении. */
function groupedBars({ categories, series, unit = "м", height = 260, title }) {
  const W = 760, H = height, padL = 46, padB = 52, padT = 18;
  const max = Math.max(1, ...series.flatMap((sr) => sr.values.filter((v) => v != null)));
  const nice = niceMax(max);
  const plotW = W - padL - 10, plotH = H - padT - padB;
  const groupW = plotW / categories.length;
  const barW = Math.min(36, (groupW - 16) / series.length - 2);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": title });
  for (const t of ticks(nice)) {
    const y = padT + plotH * (1 - t / nice);
    svg.append(s("line", { x1: padL, x2: W - 10, y1: y, y2: y, class: "grid" }),
      s("text", { x: padL - 6, y: y + 4, class: "tick", "text-anchor": "end" }, num(t, 0)));
  }
  svg.append(s("text", { x: 4, y: padT - 6, class: "tick" }, unit));
  categories.forEach((cat, ci) => {
    const gx = padL + ci * groupW + (groupW - series.length * (barW + 2)) / 2;
    series.forEach((sr, si) => {
      const v = sr.values[ci];
      if (v == null) return;
      const h = Math.max(1, plotH * (v / nice));
      const x = gx + si * (barW + 2), y = padT + plotH - h;
      const g = s("g", { class: "bar" });
      g.append(s("path", { d: roundedTop(x, y, barW, h, 4), fill: SERIES[si] }),
        s("title", {}, `${cat}\n${sr.name}: ${num(v)} ${unit}`),
        v > 0 ? s("text", { x: x + barW / 2, y: y - 4, class: "val", "text-anchor": "middle" }, num(v, v < 10 ? 1 : 0)) : "");
      svg.append(g);
    });
    wrapLabel(svg, cat, padL + ci * groupW + groupW / 2, H - padB + 16, groupW - 6);
  });
  svg.append(s("line", { x1: padL, x2: W - 10, y1: padT + plotH, y2: padT + plotH, class: "axis" }));
  return figure(title, svg, series.map((x) => x.name));
}

/** Линии по точкам x (число повреждений и т. п.), подписи значений у последней точки. */
function lineChart({ xs, series, unit = "м", xLabel, title }) {
  const W = 760, H = 270, padL = 46, padB = 40, padT = 18, padR = 120;
  const max = niceMax(Math.max(1, ...series.flatMap((sr) => sr.values)));
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const xMax = Math.max(...xs), xMin = Math.min(...xs);
  const X = (v) => padL + (xMax === xMin ? 0 : (plotW * (v - xMin)) / (xMax - xMin));
  const Y = (v) => padT + plotH * (1 - v / max);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": title });
  for (const t of ticks(max)) {
    svg.append(s("line", { x1: padL, x2: padL + plotW, y1: Y(t), y2: Y(t), class: "grid" }),
      s("text", { x: padL - 6, y: Y(t) + 4, class: "tick", "text-anchor": "end" }, num(t, 0)));
  }
  xs.forEach((x) => svg.append(s("text", { x: X(x), y: H - padB + 16, class: "tick", "text-anchor": "middle" }, String(x))));
  svg.append(s("text", { x: padL + plotW / 2, y: H - 6, class: "tick", "text-anchor": "middle" }, xLabel),
    s("text", { x: 4, y: padT - 6, class: "tick" }, unit),
    s("line", { x1: padL, x2: padL + plotW, y1: padT + plotH, y2: padT + plotH, class: "axis" }));
  series.forEach((sr, si) => {
    const pts = xs.map((x, i) => `${X(x)},${Y(sr.values[i])}`).join(" ");
    svg.append(s("polyline", { points: pts, fill: "none", stroke: SERIES[si], "stroke-width": 2 }));
    xs.forEach((x, i) => {
      const g = s("g", { class: "pt" });
      g.append(s("circle", { cx: X(x), cy: Y(sr.values[i]), r: 4.5, fill: SERIES[si], class: "ring" }),
        s("circle", { cx: X(x), cy: Y(sr.values[i]), r: 12, fill: "transparent" }),
        s("title", {}, `${xLabel}: ${x}\n${sr.name}: ${num(sr.values[i])} ${unit}`));
      svg.append(g);
    });
    const last = sr.values[sr.values.length - 1];
    svg.append(s("text", { x: X(xs[xs.length - 1]) + 8, y: Y(last) + 4, class: "val" }, `${sr.name}: ${num(last)}`));
  });
  return figure(title, svg, series.map((x) => x.name));
}

function figure(title, svg, names) {
  const legend = names.length > 1 ? el("div", { class: "viz-legend" }, names.map((n, i) =>
    el("span", { class: "viz-key" }, el("i", { style: `background:${SERIES[i]}` }), n))) : null;
  // На телефоне график не сжимается до нечитаемого — прокручивается по горизонтали
  return el("figure", { class: "chart-fig" }, el("figcaption", {}, title), legend, el("div", { class: "chart-scroll" }, svg));
}

function niceMax(v) {
  const p = 10 ** Math.floor(Math.log10(v));
  // Множители, у которых четверти — «круглые» числа (подписи 0, ¼, ½, ¾, 1)
  return [1, 1.2, 1.6, 2, 2.4, 3, 4, 5, 6, 8, 10].map((m) => m * p).find((m) => m >= v) || v;
}
function ticks(max) { return [0, 0.25, 0.5, 0.75, 1].map((k) => k * max); }
function roundedTop(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}
function wrapLabel(svg, text, cx, y, width) {
  const words = text.split(" ");
  const lines = [];
  let cur = "";
  const maxChars = Math.max(8, Math.floor(width / 6.5));
  for (const w of words) {
    if ((cur + " " + w).trim().length > maxChars && cur) { lines.push(cur); cur = w; } else cur = (cur + " " + w).trim();
  }
  lines.push(cur);
  lines.slice(0, 3).forEach((ln, i) => svg.append(s("text", { x: cx, y: y + i * 13, class: "tick", "text-anchor": "middle" }, ln)));
}

// ---------- таблицы ----------

function table(head, rows) {
  return el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
    el("thead", {}, el("tr", {}, head.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, c)))))));
}

function card(title, ...children) {
  return el("section", { class: "page-card" }, el("h2", {}, title), ...children);
}

// ---------- серии ----------

function renderE1(r, runId) {
  const rows = r.rows;
  return card(r.title,
    el("p", { class: "muted small" }, "Одни и те же смоделированные повреждения, четыре способа найти место. Ручные способы — " +
      "как делают без системы: отмерить показание рефлектометра по линии на карте или по длинам из документации."),
    groupedBars({ title: "Ошибка места повреждения по трассе, м (медиана и 90 % случаев)", unit: "м",
      categories: rows.map((x) => x.title),
      series: [{ name: "медиана", values: rows.map((x) => x.median_m) }, { name: "90 % случаев не больше", values: rows.map((x) => x.p90_m) }] }),
    table(["Способ", "Медиана, м", "Среднее, м", "90 %, м", "Макс., м", "≤ 10 м", "≤ 25 м", "Интервал ±, м", "Попадание в интервал", "Лучше «по карте»"],
      rows.map((x) => [x.title, num(x.median_m), num(x.mean_m), num(x.p90_m), num(x.max_m), `${num(x.within_10m_pct)} %`,
        `${num(x.within_25m_pct)} %`, x.half_width_mean_m != null ? num(x.half_width_mean_m) : "—",
        x.coverage_pct != null ? `${num(x.coverage_pct)} %` : "—", `${num(x.vs_map_pct)} %`])),
    histogram(r.hist, rows), csvLink(runId, "e1"));
}

function histogram(hist, rows) {
  const total = rows[0].n;
  return groupedBars({ title: "Распределение ошибок: доля повреждений по диапазонам ошибки, %", unit: "%",
    categories: hist.bins.map((b) => `${b} м`), height: 240,
    series: rows.map((x) => ({ name: x.title, values: hist.counts[x.method].map((c) => (100 * c) / total) })) });
}

function renderE2(r, runId) {
  const keys = [["map", "По карте"], ["docs", "По документации"], ["system", "Система"], ["calibrated", "После калибровки"]];
  return card(r.title,
    el("p", { class: "muted small" }, "Сети с разной долей участков, длина которых известна (измерена или есть в ИД). " +
      "Остальные — только линия на карте."),
    groupedBars({ title: "Медиана ошибки, м", categories: r.rows.map((x) => x.title),
      series: keys.map(([k, n]) => ({ name: n, values: r.rows.map((x) => x[`${k}_median_m`]) })) }),
    table(["Данные", ...keys.map(([, n]) => `${n}, м`), "Интервал ±, м", "Попадание"],
      r.rows.map((x) => [x.title, ...keys.map(([k]) => num(x[`${k}_median_m`])), num(x.half_width_mean_m), `${num(x.coverage_pct)} %`])),
    csvLink(runId, "e2"));
}

function renderE3(r, runId) {
  const xs = r.rows.map((x) => x.incidents);
  return card(r.title,
    el("p", { class: "muted small" }, "Сеть, где длины участков известны только по карте. Монтажники отмечают фактическое " +
      "место устранённых повреждений — калибровка уточняет «растяжение» карты и укрутку."),
    lineChart({ title: "Ошибка и интервал в зависимости от числа отмеченных повреждений", xLabel: "устранённых повреждений",
      xs, series: [{ name: "медиана", values: r.rows.map((x) => x.median_m) }, { name: "90 %", values: r.rows.map((x) => x.p90_m) },
        { name: "интервал ±", values: r.rows.map((x) => x.half_width_mean_m) }] }),
    table(["Устранённых повреждений", "Медиана, м", "90 %, м", "Интервал ±, м", "Попадание"],
      r.rows.map((x) => [x.incidents, num(x.median_m), num(x.p90_m), num(x.half_width_mean_m), `${num(x.coverage_pct)} %`])),
    csvLink(runId, "e3"));
}

function renderE4(r, runId) {
  return card(r.title,
    el("p", { class: "muted small" }, "Случайный обрыв участка → узлы доступа за ним недоступны → система по статусам и " +
      "трактам указывает отрезок трассы. «Попадание» — настоящий участок обрыва внутри указанного отрезка."),
    groupedBars({ title: "Настоящий участок в указанном отрезке, %", unit: "%", categories: r.rows.map((x) => x.title),
      series: [{ name: "в лучшем отрезке", values: r.rows.map((x) => x.hit_pct) }, { name: "в трёх лучших", values: r.rows.map((x) => x.hit3_pct) }] }),
    table(["Условия", "Обрывов", "В лучшем отрезке", "В трёх лучших", "Длина отрезка, м", "Участков в отрезке"],
      r.rows.map((x) => [x.title, x.cuts, `${num(x.hit_pct)} %`, `${num(x.hit3_pct)} %`, num(x.group_len_median_m, 0), num(x.group_segments_median)])),
    csvLink(runId, "e4"));
}

function renderE5(r, runId) {
  return card(r.title,
    el("p", { class: "muted small" }, "Время операций на сетях разного размера (сервер, где идёт расчёт; мс — миллисекунды)."),
    table(["Точек", "Участков", "Кабелей", "Узлов доступа", "Генерация, с", "Трассировка, мс", "Локализация, мс",
      "Пути всех узлов, мс", "Из кеша, мс", "Что отключится, мс", "Корреляция, мс"],
    r.rows.map((x) => [x.nodes, x.segments, x.cables, x.access_nodes, num(x.generate_s), num(x.trace_ms), num(x.localize_ms, 2),
      num(x.paths_all_ms, 0), num(x.paths_cached_ms), num(x.impact_ms), num(x.correlate_ms)])),
    csvLink(runId, "e5"));
}

function csvLink(runId, series) {
  return el("p", { class: "small" }, el("a", { href: `/api/experiments/${runId}/${series}.csv` }, "Таблица в Excel (CSV)"));
}

const RENDER = { e1: renderE1, e2: renderE2, e3: renderE3, e4: renderE4, e5: renderE5 };

// ---------- прогоны ----------

let timer = null;

async function loadRuns(selectId) {
  const runs = await api("/api/experiments");
  const sel = $("runs");
  sel.replaceChildren(...runs.map((r) => new Option(
    `№${r.id} · ${r.suite === "full" ? "полный" : "быстрый"} · ${new Date(r.created_at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })} · ` +
    { running: "идёт", done: "готов", failed: "ошибка" }[r.status], r.id)));
  if (!runs.length) {
    $("results").replaceChildren(el("section", { class: "page-card" }, el("p", { class: "muted" }, "Прогонов ещё нет — запустите эксперимент.")));
    return;
  }
  const id = selectId ?? (runs.find((r) => r.status === "done") || runs[0]).id;
  sel.value = id;
  await show(id);
}

async function show(id) {
  clearTimeout(timer);
  const r = await api(`/api/experiments/${id}`);
  const busy = r.status === "running";
  $("progress").hidden = !busy;
  $("btn-quick").disabled = $("btn-full").disabled = busy;
  if (busy) {
    $("progress-text").textContent = `Идёт расчёт: ${r.progress?.stage || "…"}`;
    $("progress-bar").style.width = `${Math.round(100 * (r.progress?.frac || 0))}%`;
    $("results").replaceChildren();
    timer = setTimeout(() => show(id), 3000);
    return;
  }
  if (r.status === "failed") {
    $("results").replaceChildren(el("section", { class: "page-card" }, el("p", { class: "form-error" }, r.error || "Ошибка")));
    return;
  }
  const res = r.results;
  const cfg = res.config;
  $("results").replaceChildren(
    el("section", { class: "page-card" }, el("h2", {}, `Прогон №${r.id}`),
      el("p", { class: "muted small" }, `Сети: ${cfg.seeds.length} (≈${cfg.nodes} точек), повреждений на сеть: ${cfg.faults}, ` +
        `для калибровки: ${cfg.incidents}. Время расчёта: ${Object.values(res.timing_s).reduce((a, b) => a + b, 0).toFixed(0)} с.`),
      el("a", { class: "btn btn-small", href: `/api/experiments/${r.id}/report.md` }, "Отчёт для ВКР (Markdown)")),
    ...Object.keys(RENDER).filter((k) => res.results[k]).map((k) => RENDER[k](res.results[k], r.id)));
}

async function start(suite) {
  try {
    const r = await api("/api/experiments", { method: "POST", body: { suite } });
    await loadRuns(r.id);
  } catch (e) {
    toast(e.message);
  }
}

$("btn-quick").addEventListener("click", () => start("quick"));
$("btn-full").addEventListener("click", () => {
  if (confirm("Полный эксперимент считается 10–30 минут. Запустить?")) start("full");
});
$("runs").addEventListener("change", () => show(Number($("runs").value)));
loadRuns().catch((e) => toast(e.message));
