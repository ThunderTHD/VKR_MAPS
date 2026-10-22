// Пакеты ИД: загрузка PDF → разбор в фоне → замечания → сопоставление с сетью → подтверждение.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
let user;
let current = null;   // открытый пакет
let pollTimer = null;
const link = { cable: null, portA: null, portB: null };  // выбор пользователя

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

function toast(t) {
  $("toast").textContent = t;
  $("toast").hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => ($("toast").hidden = true), 5000);
}

const STATUS_RU = { processing: "разбирается…", failed: "ошибка", parsed: "ждёт проверки", confirmed: "подтверждён",
  rejected: "отклонён" };
const LEVEL_RU = { error: "Ошибка", warning: "Предупреждение", info: "Инфо" };

// ---------------------------------------------------------------- карта опор

const map = L.map("map", { zoomControl: true }).setView([55.74, 52.4], 12);
map.attributionControl.setPrefix(false);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 20, maxNativeZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">участники OpenStreetMap</a>',
}).addTo(map);
const existing = L.geoJSON(null, { style: { color: "#8d99ae", weight: 3, opacity: 0.5, dashArray: "4 6" },
  interactive: false }).addTo(map);
const poles = L.layerGroup().addTo(map);

async function showPoles(list) {
  poles.clearLayers();
  existing.clearLayers();
  if (!list?.length) return;
  const ll = list.map((p) => [p.lat, p.lon]);
  L.polyline(ll, { color: "#2a9d8f", weight: 4 }).addTo(poles);
  list.forEach((p) => L.circleMarker([p.lat, p.lon], { radius: 6, color: "#fff", weight: 2, fillColor: "#1d4ed8",
    fillOpacity: 1 }).bindTooltip(p.name || `опора ${p.n}`).addTo(poles));
  const b = L.latLngBounds(ll).pad(0.4);
  map.fitBounds(b, { maxZoom: 18 });
  try {
    const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(",");
    const seg = await api("/api/map/layers/segments", { params: { bbox } });
    existing.addData(typeof seg === "string" ? JSON.parse(seg) : seg);
  } catch { /* трассы — только для сравнения, без них страница работает */ }
}

// ---------------------------------------------------------------- список и загрузка

async function loadList() {
  const rows = await api("/api/asbuilt");
  $("packages").replaceChildren(...(rows.length ? rows.map((p) => el("li", {
    class: "clickable", onclick: () => open(p.id) },
  el("b", {}, p.filename || `Пакет №${p.id}`),
  el("span", { class: "muted small" }, [p.volp ? `ВОЛП ${p.volp}` : null, STATUS_RU[p.status],
    p.status === "processing" && p.progress?.total ? `${p.progress.stage} ${p.progress.done}/${p.progress.total}` : null,
    p.errors != null ? `ошибок ${p.errors}, предупреждений ${p.warnings}` : null,
    new Date(p.created_at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })].filter(Boolean).join(" · ")),
  p.status === "failed" && p.error ? el("span", { class: "form-error small" }, p.error) : null))
    : [el("li", { class: "muted" }, "Пакетов пока нет")]));
  clearTimeout(pollTimer);
  if (rows.some((p) => p.status === "processing") && $("step-view").hidden) pollTimer = setTimeout(loadList, 2000);
}

async function upload(file) {
  if (!file) return;
  if (user.role !== "admin") {
    toast("Загружать пакеты может администратор");
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  fd.append("ocr", $("ocr").checked ? "true" : "false");
  $("upload-status").hidden = false;
  $("upload-status").textContent = `Загружаю ${file.name}…`;
  try {
    const p = await api("/api/asbuilt", { method: "POST", body: fd });
    $("upload-status").textContent = "Загружено, идёт разбор — пакет появится в списке со статусом «разбирается»";
    await loadList();
    open(p.id);
  } catch (e) {
    $("upload-status").textContent = e.message;
  }
}

$("file").addEventListener("change", (e) => upload(e.target.files[0]));
const drop = $("drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  upload(e.dataTransfer.files[0]);
});
$("btn-back").addEventListener("click", () => {
  current = null;
  $("step-view").hidden = true;
  $("step-list").hidden = false;
  poles.clearLayers();
  loadList();
});

// ---------------------------------------------------------------- просмотр пакета

async function open(id) {
  clearTimeout(pollTimer);
  const p = await api(`/api/asbuilt/${id}`);
  current = p;
  $("step-list").hidden = true;
  $("step-view").hidden = false;
  $("pkg-title").textContent = p.filename || `Пакет №${p.id}`;
  const body = $("pkg-body");
  if (p.status === "processing") {
    const pr = p.progress || {};
    const pct = pr.total ? Math.round((100 * pr.done) / pr.total) : 0;
    body.replaceChildren(el("p", {}, `Идёт разбор: ${pr.stage || "очередь"} ${pr.total ? `${pr.done} из ${pr.total}` : ""}`),
      el("div", { class: "progress" }, el("div", { style: `width:${pct}%` })),
      el("p", { class: "muted small" }, "Страница обновится сама. Распознавание сканов — самая долгая часть."));
    pollTimer = setTimeout(() => current?.id === id && open(id), 2000);
    return;
  }
  if (p.status === "failed") {
    body.replaceChildren(el("p", { class: "form-error" }, p.error || "Не удалось разобрать файл"));
    return;
  }
  link.cable = p.suggest?.cable_id ?? p.cable_id ?? null;
  link.portA = p.suggest?.port_a ?? null;
  link.portB = p.suggest?.port_b ?? null;
  body.replaceChildren(summaryBlock(p), findingsBlock(p.findings, p.document_id),
    p.status === "parsed" && user.role === "admin" ? matchBlock(p) : resultBlock(p),
    extractedBlock(p), pagesBlock(p));
  showPoles(p.extracted?.poles?.poles);
}

function pdfLink(docId, page, text) {
  return el("a", { href: `/api/documents/${docId}/file${page ? `#page=${page}` : ""}`, target: "_blank", rel: "noopener" },
    text);
}

function summaryBlock(p) {
  const s = p.summary || {};
  const tile = (v, label, cls = "") => el("div", { class: `sum ${cls}` }, el("b", {}, v ?? "—"), el("span", {}, label));
  return el("div", {},
    el("h2", {}, "Пакет"),
    el("dl", { class: "fields" },
      el("dt", {}, "ВОЛП"), el("dd", {}, s.volp ? `СЛ ${s.volp}` : "—"),
      el("dt", {}, "Объект"), el("dd", {}, s.address || "—"),
      el("dt", {}, "Подрядчик"), el("dd", {}, s.contractor || "—"),
      el("dt", {}, "Дата паспорта"), el("dd", {}, s.doc_date ? new Date(s.doc_date).toLocaleDateString("ru-RU") : "—"),
      el("dt", {}, "Статус"), el("dd", {}, STATUS_RU[p.status]),
      el("dt", {}, "Файл"), el("dd", {}, p.document_id ? pdfLink(p.document_id, null, "открыть PDF") : "—")),
    el("div", { class: "summary" },
      tile(s.pages, "страниц"), tile(s.ocr_pages ?? 0, `распознано сканов из ${s.scans ?? 0}`),
      tile(s.errors, "ошибок", s.errors ? "warn" : "ok"), tile(s.warnings, "предупреждений", s.warnings ? "warn" : "ok")));
}

function findingsBlock(findings, docId, title = "Проверка пакета") {
  const order = { error: 0, warning: 1, info: 2 };
  const items = [...(findings || [])].sort((a, b) => order[a.level] - order[b.level]);
  return el("div", {}, el("h2", {}, title),
    el("ul", { class: "warnings" }, items.length ? items.map((f) => el("li", { class: `w-${f.level}` },
      el("b", {}, `${LEVEL_RU[f.level]}: `), f.message,
      f.pages?.length ? el("span", { class: "muted small" }, " — стр. ",
        f.pages.flatMap((n, i) => [i ? ", " : "", docId ? pdfLink(docId, n, String(n)) : String(n)])) : null))
      : [el("li", { class: "w-info" }, "Замечаний нет")]));
}

function extractedBlock(p) {
  const ex = p.extracted || {};
  const pnr = ex.act_pnr || {};
  const sk = ex.skeleton || {};
  const m = p.suggest?.measurement;
  const rows = [
    pnr.device ? ["Коммутатор", `${pnr.device.hostname || "—"} (${pnr.device.ip || "—"}), ${pnr.model || ""}, кросс ${pnr.device.cross || "—"} порт ${pnr.device.cross_port || "—"}`] : null,
    pnr.uplink ? ["Вышестоящий", `${pnr.uplink.hostname || "—"} ${pnr.uplink.port || ""}, кросс ${pnr.uplink.cross || "—"} порт ${pnr.uplink.cross_port || "—"}`] : null,
    sk.physical_m ? ["Длина кабеля", `физ. ${sk.physical_m} м, опт. ${sk.optical_m} м → укрутка ${((sk.optical_m / sk.physical_m - 1) * 100).toFixed(2)} %`] : null,
    sk.segments?.length ? ["Участки", sk.segments.map((s) => `${s.type} ${s.length_m} м`).join("; ")] : null,
    sk.reserves_m?.length ? ["Запасы", sk.reserves_m.map((r) => `${r} м`).join(", ")] : null,
    m ? ["Измерение тракта", `${m.kind === "otdr" ? "рефлектометр" : "тестер"}: порт ${m.port_a} → ${m.port_b}, ${m.optical_m} м, ${m.loss_ab_db} дБ, ${m.wavelength_nm} нм`] : null,
    ex.poles?.poles?.length ? ["Опоры", `${ex.poles.poles.length} с координатами (на карте справа)`] : null,
  ].filter(Boolean);
  const ins = p.suggest?.instruments || [];
  return el("div", {}, el("h2", {}, "Извлечённые данные"),
    el("dl", { class: "fields" }, rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
    ins.length ? el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, ["Прибор", "Модель", "Зав. №", "Госреестр", "Поверка до"].map((t) => el("th", {}, t)))),
      el("tbody", {}, ins.map((i) => el("tr", {}, el("td", {}, { otdr: "рефлектометр", power_meter: "тестер",
        splicer: "сварочный аппарат" }[i.kind] || i.kind), el("td", {}, i.model || "—"), el("td", {}, i.serial || "—"),
      el("td", {}, i.registry_no || "—"), el("td", {}, i.valid_until || "—")))))) : null,
    p.trace?.length ? el("div", {}, el("h3", {}, "Трасса волокна по протоколам"),
      el("ol", { class: "small trace-steps" }, p.trace.map((t) => el("li", {}, t)))) : null);
}

function pagesBlock(p) {
  return el("details", { class: "trace-block" }, el("summary", {}, `Страницы (${p.pages?.length || 0})`),
    el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, ["Стр.", "Тип", "Текст", "Использование"].map((t) => el("th", {}, t)))),
      el("tbody", {}, (p.pages || []).map((pg) => el("tr", {},
        el("td", {}, p.document_id ? pdfLink(p.document_id, pg.page, String(pg.page)) : pg.page), el("td", {}, pg.title),
        el("td", {}, pg.ocr ? "OCR" : pg.has_text ? "есть" : "нет"), el("td", {}, pg.use)))))));
}

// ---------------------------------------------------------------- сопоставление и подтверждение

function portPicker(label, initial, onPick) {
  const box = el("div", { class: "edit-form compact port-picker" });
  const info = el("div", { class: "muted small" }, initial ? `${initial.node} · ${initial.cross}, порт ${initial.port_no}` : "не выбран");
  const search = el("input", { type: "search", placeholder: "кросс: узел, адрес, название" });
  const list = el("select", { size: 4, class: "pick-list", hidden: true });
  const ports = el("select", { hidden: true });
  let t;
  search.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const rows = await api("/api/crosses", { params: { q: search.value.trim() } });
      list.replaceChildren(...rows.map((x) => new Option(`${x.node} · ${x.name} (${x.used}/${x.port_count})`, x.id)));
      list.hidden = !rows.length;
    }, 250);
  });
  list.addEventListener("change", async () => {
    const opt = list.selectedOptions[0];
    const rows = await api(`/api/crosses/${opt.value}/ports`);
    ports.replaceChildren(new Option("— порт —", ""), ...rows.map((p) => new Option(
      `${p.port_no}${p.cable ? ` — ${p.cable}, ОВ ${p.fiber}` : " — свободен"}`, p.id)));
    ports.hidden = false;
    ports.onchange = () => {
      const port = rows.find((r) => String(r.id) === ports.value);
      onPick(port ? { id: port.id, cross: opt.text, port_no: port.port_no, node: "" } : null);
      info.textContent = port ? `${opt.text}, порт ${port.port_no}` : "не выбран";
    };
  });
  box.append(el("label", { class: "field" }, label, info, search), list, ports);
  return box;
}

function matchBlock(p) {
  const s = p.suggest || {};
  const cableSel = el("select", {}, new Option("— кабель не выбран —", ""),
    ...(s.cables || []).map((c) => new Option(c.name, c.id, false, c.id === link.cable)));
  const cableSearch = el("input", { type: "search", placeholder: "найти кабель по названию" });
  let t;
  cableSearch.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const rows = (await api("/api/search", { params: { q: cableSearch.value.trim() } })).filter((r) => r.type === "cable");
      cableSel.replaceChildren(new Option("— кабель не выбран —", ""), ...rows.map((r) => new Option(r.label, r.id)));
    }, 250);
  });
  cableSel.addEventListener("change", () => { link.cable = Number(cableSel.value) || null; });
  const sysBox = el("div");
  const opts = [["measurement", "Измерение тракта — эталон для калибровки", !!s.measurement],
    ["twist", `Укрутка кабеля из ИД${s.twist != null ? ` (${(s.twist * 100).toFixed(2)} %)` : ""}`, s.twist != null],
    ["instruments", `Приборы и поверка (${s.instruments?.length || 0})`, !!s.instruments?.length],
    ["access_node", `Узел доступа ${s.access_node?.hostname || ""}${s.access_node?.exists_id ? " (обновить)" : " (создать)"}`,
      !!s.access_node?.hostname],
    ["poles", `Опоры с координатами на карту (${s.poles})`, false],
    ["attach", "Прикрепить PDF к кабелю", true]];
  const checks = Object.fromEntries(opts.map(([k, , on]) => [k, el("input", { type: "checkbox", checked: on })]));
  const err = el("p", { class: "form-error", hidden: true });
  const body = () => ({ cable_id: link.cable, port_a_id: link.portA?.id ?? null, port_b_id: link.portB?.id ?? null });
  return el("div", {}, el("h2", {}, "Сопоставление с сетью"),
    el("p", { class: "muted small" }, "Система предложила кабель по номеру ВОЛП и порты тракта по акту ПНР. ",
      "Проверьте и поправьте, затем сверьте с данными системы."),
    el("div", { class: "edit-form compact" }, el("label", { class: "field" }, "Кабель в системе", cableSel), cableSearch),
    portPicker("Порт A (оборудование клиента)", link.portA, (v) => { link.portA = v; }),
    portPicker("Порт Б (вышестоящий узел)", link.portB, (v) => { link.portB = v; }),
    el("div", { class: "form-actions" }, el("button", { class: "btn btn-small", type: "button", onclick: async () => {
      try {
        const f = await api(`/api/asbuilt/${p.id}/compare`, { method: "POST", body: body() });
        sysBox.replaceChildren(findingsBlock(f, null, "Сверка с системой"));
      } catch (e) {
        toast(e.message);
      }
    } }, "Сверить с системой")),
    sysBox,
    el("h3", {}, "Что записать"),
    el("div", { class: "toggles" }, opts.map(([k, label]) => el("label", { class: "toggle small" }, checks[k], el("span", {}, label)))),
    err,
    el("div", { class: "sticky-actions" },
      el("button", { class: "btn btn-primary", type: "button", onclick: async () => {
        err.hidden = true;
        try {
          const opt = Object.fromEntries(Object.entries(checks).map(([k, c]) => [k, c.checked]));
          const r = await api(`/api/asbuilt/${p.id}/confirm`, { method: "POST", body: { ...body(), ...opt } });
          toast(`Записано: ${Object.keys(r).join(", ") || "ничего"}`);
          open(p.id);
        } catch (e) {
          err.textContent = e.message;
          err.hidden = false;
        }
      } }, "Подтвердить и записать"),
      el("button", { class: "btn btn-small btn-danger", type: "button", style: "margin-top:8px", onclick: async () => {
        const reason = prompt("Причина (например, «вернуть подрядчику: ошибки в длинах»):", "");
        if (reason === null) return;
        await api(`/api/asbuilt/${p.id}/reject`, { method: "POST", body: { reason } });
        open(p.id);
      } }, "Отклонить пакет")));
}

function resultBlock(p) {
  const rep = p.check_report || {};
  const applied = rep.applied || {};
  const names = { measurement_id: "измерение тракта", twist: "укрутка кабеля", instruments: "приборы",
    access_node_id: "узел доступа", poles: "опоры", document_id: "PDF прикреплён к кабелю" };
  return el("div", {},
    p.status === "confirmed" ? el("div", {}, el("h2", {}, "Записано в систему"),
      el("ul", { class: "small" }, Object.keys(applied).map((k) => el("li", {}, names[k] || k,
        typeof applied[k] === "object" ? `: ${JSON.stringify(applied[k])}` : ""))),
      p.cable_id ? el("a", { class: "btn btn-small", href: `/#obj=cable:${p.cable_id}` }, "Кабель на карте") : null) : null,
    p.status === "rejected" ? el("p", { class: "form-error" }, `Пакет отклонён${p.error ? `: ${p.error}` : ""}`) : null,
    p.status === "parsed" ? el("p", { class: "muted" }, "Пакет ждёт проверки администратором.") : null,
    rep.system?.length ? findingsBlock(rep.system, null, "Сверка с системой") : null);
}

// ---------------------------------------------------------------- запуск

async function main() {
  user = await api("/api/auth/me");
  if (user.role === "viewer") {
    $("step-list").replaceChildren(el("p", {}, "Раздел доступен монтажнику и администратору."));
    return;
  }
  if (user.role !== "admin") $("drop").hidden = true;
  const id = Number(new URLSearchParams(location.search).get("id"));
  await loadList();
  if (id) open(id);
}

main().catch((e) => toast(e.message));
