// Подложки: загрузка плана и привязка к карте по опорным точкам (план слева, карта справа).
import { api } from "./api.js";
import { SkewedImage } from "./overlay-layer.js";

const $ = (id) => document.getElementById(id);
const fmt = (v) => (v == null ? "—" : `${String(v).replace(".", ",")} м`);

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 5000);
}

function marker(latlng, n) {
  return L.marker(latlng, { icon: L.divIcon({ className: "cp-marker", html: String(n), iconSize: [22, 22] }) });
}

// ---------- карты ----------

const imgMap = L.map("img-map", { crs: L.CRS.Simple, minZoom: -5, maxZoom: 4, zoomSnap: 0.25, attributionControl: false });
const geoMap = L.map("geo-map", { maxZoom: 20 });
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20, maxNativeZoom: 19,
  attribution: "© участники OpenStreetMap" }).addTo(geoMap);
geoMap.attributionControl.setPrefix(false);
const imgMarks = L.layerGroup().addTo(imgMap);
const geoMarks = L.layerGroup().addTo(geoMap);
let imgLayer = null;
let preview = null;
let current = null;
let points = [];          // [{px, py, lon?, lat?}]

let home = null;            // границы сети: карта справа скрыта до выбора подложки, вид ставим при показе

function showHome() {
  geoMap.invalidateSize();
  if (home) geoMap.fitBounds(home);
  else geoMap.setView([55.75, 52.05], 13);
}

async function networkBackground() {
  const ref = await api("/api/map/reference");
  if (ref.extent) {
    const [x1, y1, x2, y2] = ref.extent;
    home = [[y1, x1], [y2, x2]];
  }
  const seg = await api("/api/map/layers/segments");
  L.geoJSON(typeof seg === "string" ? JSON.parse(seg) : seg, { style: { color: "#2a78d6", weight: 2, opacity: 0.7 },
    interactive: false }).addTo(geoMap);
  // Колодцы и другие точки сети: подсказка с кодом, щелчок ставит опорную точку точно в центр колодца
  const nodes = await api("/api/map/layers/nodes");
  L.geoJSON(typeof nodes === "string" ? JSON.parse(nodes) : nodes, {
    pointToLayer: (f, ll) => {
      const m = L.circleMarker(ll, { radius: 4, color: "#14213d", weight: 1.5, fillColor: "#fff", fillOpacity: 1 });
      const label = f.properties.code || f.properties.name;
      if (label) m.bindTooltip(label, { direction: "top", offset: [0, -4] });
      m.on("click", (e) => {
        L.DomEvent.stop(e);
        setGeo(ll);
      });
      return m;
    },
  }).addTo(geoMap);
}

function step() {
  const last = points[points.length - 1];
  $("geo-step").textContent = !last || last.lat != null
    ? `Точка ${points.length + 1}: щёлкните место на плане (слева).`
    : `Точка ${points.length}: теперь то же место на карте (справа).`;
}

function render() {
  imgMarks.clearLayers();
  geoMarks.clearLayers();
  points.forEach((p, i) => {
    marker([-p.py, p.px], i + 1).addTo(imgMarks);
    if (p.lat != null) marker([p.lat, p.lon], i + 1).addTo(geoMarks);
  });
  $("cp").replaceChildren(...points.map((p, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="cp-tag">${i + 1}</span><span></span>`;
    li.lastChild.textContent = `план (${Math.round(p.px)}, ${Math.round(p.py)}) → ` +
      (p.lat != null ? `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}` : "отметьте на карте") +
      (p.residual != null ? ` · невязка ${fmt(p.residual)}` : "");
    return li;
  }));
  step();
  refit();
}

async function refit() {
  const done = points.filter((p) => p.lat != null);
  $("btn-save").disabled = done.length < 3;
  preview?.remove();
  preview = null;
  if (done.length < 3) {
    $("fit").textContent = done.length ? `Отмечено пар: ${done.length} из минимум 3` : "";
    return;
  }
  try {
    const q = new URLSearchParams();
    current.size.forEach((v) => q.append("size", v));
    const r = await api(`/api/overlays/fit?${q}`, { method: "POST", body: { points: done } });
    done.forEach((p, i) => { p.residual = r.residuals_m[i]; });
    $("fit").textContent = `Масштаб плана: ${r.m_per_px.toString().replace(".", ",")} м на пиксель` +
      (r.rms_m != null ? ` · средняя невязка ${fmt(r.rms_m)}` : " · добавьте 4-ю точку, чтобы увидеть невязку");
    preview = new SkewedImage(current.url, r.corners, { opacity: Number($("preview-opacity").value), size: current.size })
      .addTo(geoMap);
    $("cp").querySelectorAll("li").forEach((li, i) => {
      const p = points[i];
      if (p?.residual != null && !li.textContent.includes("невязка")) li.lastChild.textContent += ` · невязка ${fmt(p.residual)}`;
    });
  } catch (e) {
    $("fit").textContent = e.message;
  }
}

imgMap.on("click", (e) => {
  if (!current) return;
  const [w, h] = current.size;
  if (e.latlng.lng < 0 || e.latlng.lng > w || -e.latlng.lat < 0 || -e.latlng.lat > h) {
    toast("Щёлкните внутри плана");
    return;
  }
  const last = points[points.length - 1];
  if (last && last.lat == null) last.px = e.latlng.lng, last.py = -e.latlng.lat;   // переставить ещё не связанную точку
  else points.push({ px: e.latlng.lng, py: -e.latlng.lat });
  render();
});
function setGeo(latlng) {
  const last = points[points.length - 1];
  if (!last) {
    toast("Сначала отметьте точку на плане (слева)");
    return;
  }
  const ll = L.latLng(latlng).wrap();
  last.lon = ll.lng;
  last.lat = ll.lat;
  delete last.residual;
  render();
}
geoMap.on("click", (e) => setGeo(e.latlng));
$("btn-undo").addEventListener("click", () => { points.pop(); render(); });
$("preview-opacity").addEventListener("input", () => preview?.setOpacity(Number($("preview-opacity").value)));
$("btn-save").addEventListener("click", async () => {
  try {
    const done = points.filter((p) => p.lat != null).map(({ px, py, lon, lat }) => ({ px, py, lon, lat }));
    await api(`/api/overlays/${current.id}/points`, { method: "PUT", body: { points: done } });
    await api(`/api/overlays/${current.id}`, { method: "PATCH", body: { opacity: Number($("preview-opacity").value) } });
    toast("Привязка сохранена — подложка видна на карте (панель «Слои»)");
    load();
  } catch (e) {
    toast(e.message);
  }
});

// ---------- список ----------

function edit(o) {
  current = o;
  $("geo").hidden = false;
  $("geo-title").textContent = `Привязка: ${o.name}`;
  imgLayer?.remove();
  const [w, h] = o.size;
  const bounds = [[-h, 0], [0, w]];
  imgLayer = L.imageOverlay(o.url, bounds).addTo(imgMap);
  points = (o.points || []).map((p) => ({ ...p }));
  $("preview-opacity").value = o.opacity;
  imgMap.invalidateSize();
  imgMap.fitBounds(bounds);
  imgMap.setMaxBounds(L.latLngBounds(bounds).pad(0.5));
  if (o.corners) {
    geoMap.invalidateSize();
    const c = Object.values(o.corners);
    geoMap.fitBounds(c.map(([lon, lat]) => [lat, lon]), { padding: [20, 20] });
  } else showHome();
  render();
  $("geo").scrollIntoView();
}

async function load() {
  const list = await api("/api/overlays");
  $("list").replaceChildren(...list.map((o) => {
    const li = document.createElement("li");
    const b = document.createElement("b");
    b.textContent = o.name;
    const meta = document.createElement("span");
    meta.className = "muted small";
    meta.textContent = o.corners ? ` · привязана по ${o.points.length} точкам${o.rms_m != null ? `, невязка ${fmt(o.rms_m)}` : ""}`
      : " · не привязана";
    const go = Object.assign(document.createElement("button"), { type: "button", className: "btn btn-small",
      textContent: o.corners ? "Изменить привязку" : "Привязать" });
    go.addEventListener("click", () => edit(o));
    const del = Object.assign(document.createElement("button"), { type: "button", className: "btn btn-small btn-ghost",
      textContent: "Удалить" });
    del.addEventListener("click", async () => {
      if (!confirm(`Удалить подложку «${o.name}»?`)) return;
      await api(`/api/overlays/${o.id}`, { method: "DELETE" });
      if (current?.id === o.id) $("geo").hidden = true;
      load();
    });
    li.append(b, meta, " ", go, " ", del);
    return li;
  }));
  return list;
}

$("upload").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  toast("Загружаю…");
  try {
    const o = await api("/api/overlays", { method: "POST", body: fd });
    e.target.reset();
    await load();
    edit(o);
  } catch (err) {
    toast(err.message);
  }
});

networkBackground().catch(() => {});
load().catch((e) => toast(e.message));
