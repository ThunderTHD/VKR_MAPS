// Страница импорта KML: загрузка → сопоставление папок → предпросмотр на карте → запись.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
let ref;           // справочники (владельцы, типы)
let preview;       // последний ответ сервера с предпросмотром
let jobId;

const TARGETS = [
  ["node", "Точки сети"], ["segment", "Участки трасс"], ["closure", "Муфты"],
  ["unclassified", "Неклассифицированные"], ["skip", "Пропустить"],
];
const STATUS_RU = { new: "новый", warning: "с замечанием", duplicate: "уже есть", skip: "пропущен",
  auto: "создан в конце линии", update: "дополнен" };

// ---------- Карта предпросмотра ----------

const map = L.map("map", { zoomControl: true }).setView([55.74, 52.4], 12);
// Подпись Leaflet по умолчанию содержит флаг — убираем её, оставляя обязательную ссылку на OpenStreetMap
// (лицензия ODbL требует указывать источник картографических данных).
map.attributionControl.setPrefix(false);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 20, maxNativeZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">участники OpenStreetMap</a>',
}).addTo(map);
const existingLayer = L.geoJSON(null, { style: { color: "#8d99ae", weight: 3, opacity: 0.35 }, interactive: false })
  .addTo(map);
const previewLayer = L.geoJSON(null, {
  style: (f) => ({
    new: { color: "#2a9d8f", weight: 4 },
    warning: { color: "#f77f00", weight: 5 },
    duplicate: { color: "#6c757d", weight: 3, dashArray: "4 6" },
    skip: { color: "#d62828", weight: 3, dashArray: "2 4" },
    unclassified: { color: "#8b5e34", weight: 2, dashArray: "8 6" },
  }[f.properties.status] || { color: "#333" }),
  pointToLayer: (f, ll) => L.circleMarker(ll, {
    radius: f.properties.layer === "unclassified" ? 4 : 6,
    color: "#fff", weight: 2, fillOpacity: 1,
    fillColor: { new: "#1d4ed8", auto: "#f77f00", update: "#7b2cbf", unclassified: "#8b5e34" }[f.properties.status],
  }),
  onEachFeature: (f, l) => {
    const p = f.properties;
    const lines = [`<b>${esc(p.name || p.code || "без названия")}</b>`];
    if (p.layer === "segment") {
      lines.push(`${esc(p.a)} → ${esc(p.b)}, ${p.length_m} м`, `статус: ${STATUS_RU[p.status] || p.status}`);
      (p.warnings || []).forEach((w) => lines.push(`⚠ ${esc(w)}`));
    } else if (p.layer === "node") {
      lines.push(`точка: ${STATUS_RU[p.status] || p.status}`);
    } else {
      lines.push(`папка «${esc(p.folder)}»`);
    }
    l.bindTooltip(lines.join("<br>"), { sticky: true });
  },
}).addTo(map);
let marker;

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.hidden = true), 5000);
}

// ---------- Шаг 1: файл ----------

async function upload(file) {
  const status = $("upload-status");
  status.hidden = false;
  status.textContent = `Разбираю «${file.name}»…`;
  const form = new FormData();
  form.append("file", file);
  if ($("crs").value) form.append("crs_id", $("crs").value);
  try {
    show(await api("/api/imports/kml", { method: "POST", body: form }));
  } catch (e) {
    status.textContent = e.message;
  }
}

$("file").addEventListener("change", (e) => e.target.files[0] && upload(e.target.files[0]));
const drop = $("drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
});

async function loadJobs() {
  const jobs = await api("/api/imports");
  const ul = $("jobs");
  const ST = { preview: "не записан", committed: "записан", discarded: "отменён", failed: "ошибка" };
  ul.replaceChildren(...jobs.map((j) => {
    const li = document.createElement("li");
    const s = j.summary || {};
    li.innerHTML = `<span class="job-name"></span><span class="muted small"></span>`;
    li.firstChild.textContent = `№${j.id} ${j.filename}`;
    li.lastChild.textContent = `${ST[j.status] || j.status}` +
      (s.segments_new !== undefined ? ` · участков: ${s.segments_new}, точек: ${s.nodes_new + s.nodes_auto}` : "");
    if (j.status === "preview") {
      li.classList.add("clickable");
      li.addEventListener("click", async () => show(await api(`/api/imports/${j.id}`)));
    }
    return li;
  }));
  if (!jobs.length) ul.innerHTML = '<li class="muted">Пока не было</li>';
}

// ---------- Шаг 2: сопоставление и предпросмотр ----------

function kindOptions(target) {
  if (target === "node") return Object.entries(ref.node_kinds);
  if (target === "segment") return Object.entries(ref.segment_kinds);
  return [];
}

function folderRow(folder, m) {
  const tr = document.createElement("tr");
  const types = Object.entries(folder.types).map(([t, n]) =>
    `${n} ${{ Point: "точ.", LineString: "лин.", Polygon: "полиг." }[t] || t}`).join(", ");
  tr.innerHTML = `<td class="folder-name"></td><td>${types}</td><td><select class="t"></select></td>
    <td><select class="k"></select></td><td><select class="o"><option value="">—</option></select></td>`;
  tr.querySelector(".folder-name").textContent = folder.name;
  tr.querySelector(".folder-name").title = folder.path;
  const t = tr.querySelector(".t");
  TARGETS.forEach(([v, label]) => t.append(new Option(label, v, false, v === m.target)));
  const k = tr.querySelector(".k");
  const fillKinds = () => {
    const opts = kindOptions(t.value);
    k.replaceChildren(...opts.map(([v, label]) => new Option(label, v)));
    k.disabled = !opts.length;
    const cur = t.value === "node" ? m.node_kind : m.segment_kind;
    if (cur) k.value = cur;
  };
  fillKinds();
  t.addEventListener("change", fillKinds);
  const o = tr.querySelector(".o");
  ref.owners.forEach((ow) => o.append(new Option(ow.name, ow.id, false, ow.id === m.owner_id)));
  tr.dataset.folder = folder.name;
  return tr;
}

function readMapping() {
  const mapping = {};
  for (const tr of $("folders").querySelectorAll("tr")) {
    const target = tr.querySelector(".t").value;
    const kind = tr.querySelector(".k").value || null;
    mapping[tr.dataset.folder] = {
      target,
      node_kind: target === "node" ? kind : null,
      segment_kind: target === "segment" ? kind : null,
      owner_id: Number(tr.querySelector(".o").value) || null,
    };
  }
  return mapping;
}

function summaryHtml(s) {
  const item = (n, label, cls = "") => `<div class="sum ${cls}"><b>${n}</b><span>${label}</span></div>`;
  return [
    item(s.segments_new, "новых участков", "ok"),
    item(s.segments_duplicate, "уже есть в базе"),
    item(s.nodes_new, "точек из файла"),
    item(s.nodes_auto, "точек в концах линий", s.nodes_auto ? "warn" : ""),
    item(s.nodes_update, "узлов дополнено"),
    item(s.closures, "муфт"),
    item(s.unclassified, "неклассифицированных"),
    item(s.warnings, "замечаний", s.warnings ? "warn" : ""),
  ].join("");
}

function renderWarnings() {
  const showInfo = $("show-info").checked;
  const list = preview.warnings.filter((w) => showInfo || w.level !== "info");
  $("warn-count").textContent = `(${preview.summary.warnings} + ${preview.warnings.length - preview.summary.warnings} инф.)`;
  $("warnings").replaceChildren(...list.map((w) => {
    const li = document.createElement("li");
    li.className = `w-${w.level}`;
    li.textContent = w.message;
    if (w.lonlat) {
      li.tabIndex = 0;
      li.addEventListener("click", () => {
        map.setView([w.lonlat[1], w.lonlat[0]], 18);
        marker?.remove();
        marker = L.circleMarker([w.lonlat[1], w.lonlat[0]], { radius: 14, color: "#d62828", weight: 3, fill: false })
          .addTo(map);
      });
    }
    return li;
  }));
}

function show(data) {
  preview = data;
  jobId = data.job.id;
  $("step-file").hidden = true;
  $("step-done").hidden = true;
  $("step-preview").hidden = false;
  $("file-name").textContent = `№${jobId} · ${data.filename}`;
  $("tolerance").value = data.tolerance_m;
  $("folders").replaceChildren(...data.folders.map((f) => folderRow(f, data.mapping[f.name] || { target: "unclassified" })));
  $("summary").innerHTML = summaryHtml(data.summary);
  $("btn-commit").disabled = data.job.status !== "preview";
  renderWarnings();
  previewLayer.clearLayers();
  previewLayer.addData(data.geojson);
  const b = previewLayer.getBounds();
  if (b.isValid()) map.fitBounds(b, { padding: [30, 30] });
}

$("show-info").addEventListener("change", renderWarnings);

$("btn-refresh").addEventListener("click", async () => {
  try {
    show(await api(`/api/imports/${jobId}/preview`, {
      method: "POST", body: { mapping: readMapping(), tolerance_m: Number($("tolerance").value) },
    }));
    toast("Предпросмотр обновлён");
  } catch (e) {
    toast(e.message);
  }
});

$("btn-commit").addEventListener("click", async () => {
  const btn = $("btn-commit");
  btn.disabled = true;
  try {
    // Сначала сохраняем текущее сопоставление (если его меняли и не обновили предпросмотр)
    await api(`/api/imports/${jobId}/preview`, {
      method: "POST", body: { mapping: readMapping(), tolerance_m: Number($("tolerance").value) },
    });
    const res = await api(`/api/imports/${jobId}/commit`, { method: "POST" });
    const c = res.report.created;
    $("step-preview").hidden = true;
    $("step-done").hidden = false;
    $("done-summary").innerHTML = [
      [c.segments, "участков создано"], [c.nodes, "точек создано"], [c.nodes_updated, "узлов дополнено"],
      [c.closures, "муфт"], [c.unclassified, "неклассифицированных"],
    ].map(([n, l]) => `<div class="sum"><b>${n}</b><span>${l}</span></div>`).join("");
    loadExisting();
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
  }
});

function reset() {
  previewLayer.clearLayers();
  marker?.remove();
  $("step-preview").hidden = true;
  $("step-done").hidden = true;
  $("step-file").hidden = false;
  $("file").value = "";
  $("upload-status").hidden = true;
  loadJobs();
}

$("btn-cancel").addEventListener("click", async () => {
  if (preview?.job.status === "preview") await api(`/api/imports/${jobId}/discard`, { method: "POST" });
  reset();
});
$("btn-again").addEventListener("click", reset);

async function loadExisting() {
  existingLayer.clearLayers();
  existingLayer.addData(await api("/api/map/layers/segments"));
}

async function main() {
  const user = await api("/api/auth/me");
  if (user.role !== "admin") {
    document.body.innerHTML = '<main class="stub"><h1>Импорт</h1><p>Импорт доступен только администратору.</p>' +
      '<p><a href="/">← К карте</a></p></main>';
    return;
  }
  ref = await api("/api/map/reference");
  if (ref.extent) {
    const [x1, y1, x2, y2] = ref.extent;
    map.fitBounds([[y1, x1], [y2, x2]]);
  }
  await Promise.all([loadJobs(), loadExisting()]);
  for (const c of await api("/api/crs")) $("crs").append(new Option(c.name, c.id));
}

main().catch((e) => toast(e.message));
