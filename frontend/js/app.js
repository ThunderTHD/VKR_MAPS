// Главный модуль карты: загрузка справочников и слоёв, фильтры, поиск, карточки, состояние в адресе.
import { api } from "./api.js";
import { renderCard } from "./card.js";
import { DrawTools } from "./draw.js";
import { Editor } from "./edit.js";
import { LAYER_INFO, MapLayers } from "./layers.js";
import { FieldTools, setupClosurePhoto } from "./field.js";
import { ImpactTool } from "./impact.js";
import { LocateTool } from "./locate.js";

const $ = (id) => document.getElementById(id);
const state = { operator_id: "", owner_id: "", obj: null };
const PREFS_KEY = "vols.layers";

// ---------- Карта ----------

const map = L.map("map", { zoomControl: false, preferCanvas: true, minZoom: 5, maxZoom: 20 });
// Подпись Leaflet по умолчанию содержит флаг — убираем её, оставляя обязательную ссылку на OpenStreetMap
// (лицензия ODbL требует указывать источник картографических данных).
map.attributionControl.setPrefix(false);
L.control.zoom({ position: "bottomright" }).addTo(map);
L.control.scale({ imperial: false, position: "bottomleft" }).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 20,
  maxNativeZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">участники OpenStreetMap</a>',
}).addTo(map);
map.setView([55.74, 52.4], 12); // Набережные Челны — до загрузки границ сети

let layers;
let editor;
let draw;

// ---------- Состояние в адресной строке: #op=1&owner=2&obj=cable:5 ----------

function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  state.operator_id = h.get("op") || "";
  state.owner_id = h.get("owner") || "";
  const obj = h.get("obj");
  state.obj = obj ? { type: obj.split(":")[0], id: Number(obj.split(":")[1]) } : null;
}

function writeHash() {
  const h = new URLSearchParams();
  if (state.operator_id) h.set("op", state.operator_id);
  if (state.owner_id) h.set("owner", state.owner_id);
  if (state.obj) h.set("obj", `${state.obj.type}:${state.obj.id}`);
  history.replaceState(null, "", h.toString() ? `#${h}` : location.pathname);
}

// ---------- Данные слоёв ----------

async function loadLayers() {
  const params = { operator_id: state.operator_id, owner_id: state.owner_id };
  document.body.classList.add("loading");
  try {
    const names = Object.keys(LAYER_INFO);
    const results = await Promise.all(names.map((n) => api(`/api/map/layers/${n}`, { params })));
    names.forEach((n, i) => layers.setData(n, typeof results[i] === "string" ? JSON.parse(results[i]) : results[i]));
  } finally {
    document.body.classList.remove("loading");
  }
  updateFilterChip();
}

// ---------- Карточки ----------

async function openObject(type, id, { fly = true } = {}) {
  state.obj = { type, id };
  writeHash();
  const card = $("card");
  card.classList.remove("expanded");
  try {
    const data = await api(`/api/objects/${type}/${id}`);
    renderCard(card, data, (t, i) => openObject(t, i), editor.actions(data));
    editor.documents(data);
    const bounds = layers.highlightObject(type, id);
    if (fly) {
      if (bounds && bounds.isValid()) {
        map.flyToBounds(bounds, { maxZoom: 17, padding: [60, 60], duration: 0.6 });
      } else if (data.lonlat) {
        map.flyTo([data.lonlat[1], data.lonlat[0]], Math.max(map.getZoom(), 17), { duration: 0.6 });
      }
    }
  } catch (e) {
    toast(e.message);
  }
}

function closeCard() {
  $("card").hidden = true;
  layers.clearHighlight();
  editor?.ctx.impact?.clear();
  state.obj = null;
  writeHash();
}

// ---------- Фильтры и слои ----------

function fillSelect(select, items) {
  for (const it of items) select.append(new Option(it.name, it.id));
}

function legend(ul, items, kind) {
  ul.replaceChildren(...items.map((it) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="swatch swatch-${kind}"></span>`;
    li.firstChild.style.background = it.color;
    li.append(it.name);
    return li;
  }));
}

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY)) || {};
  } catch {
    return {};
  }
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(layers.enabled));
  } catch {
    /* хранилище недоступно (приватный режим) — настройки слоёв просто не запомнятся */
  }
}

function buildToggles() {
  const prefs = loadPrefs();
  const box = $("layer-toggles");
  for (const [name, info] of Object.entries(LAYER_INFO)) {
    const on = name in prefs ? prefs[name] : info.on;
    layers.enabled[name] = on;
    const label = document.createElement("label");
    label.className = "toggle";
    label.innerHTML = `<input type="checkbox"> <span></span>`;
    label.querySelector("span").textContent = info.title;
    const cb = label.querySelector("input");
    cb.checked = on;
    cb.addEventListener("change", () => {
      layers.setEnabled(name, cb.checked);
      savePrefs();
    });
    box.append(label);
  }
}

function updateFilterChip() {
  // Экспорт сети учитывает фильтр по оператору
  for (const fmt of ["kml", "geojson"]) {
    $(`menu-export-${fmt}`).href = `/api/export/network.${fmt}` + (state.operator_id ? `?operator_id=${state.operator_id}` : "");
  }
  const parts = [];
  if (state.operator_id) parts.push($("filter-operator").selectedOptions[0].text);
  if (state.owner_id) parts.push(`трассы: ${$("filter-owner").selectedOptions[0].text}`);
  $("filter-chip").hidden = parts.length === 0;
  $("filter-chip-text").textContent = parts.join(" · ");
}

async function applyFilters() {
  state.operator_id = $("filter-operator").value;
  state.owner_id = $("filter-owner").value;
  writeHash();
  await loadLayers();
}

// ---------- Поиск ----------

let searchTimer;
let searchSeq = 0;

function setupSearch() {
  const input = $("search");
  const list = $("search-results");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = input.value.trim();
      if (q.length < 2) {
        list.hidden = true;
        return;
      }
      const seq = ++searchSeq; // ответы на старые запросы игнорируем
      const results = await api("/api/search", { params: { q } });
      if (seq !== searchSeq) return;
      list.replaceChildren(...(results.length ? results : [{ empty: true }]).map((r) => {
        const li = document.createElement("li");
        if (r.empty) {
          li.className = "empty";
          li.textContent = "Ничего не найдено";
          return li;
        }
        li.setAttribute("role", "option");
        li.tabIndex = 0;
        li.innerHTML = `<span class="badge badge-${r.type}"></span><span class="label"></span>`;
        li.firstChild.textContent = { node: "Узел", closure: "Муфта", cable: "Кабель",
          access_node: "Узел доступа" }[r.type];
        li.lastChild.textContent = r.label;
        const choose = () => {
          list.hidden = true;
          input.blur();
          openObject(r.type, r.id);
        };
        li.addEventListener("click", choose);
        li.addEventListener("keydown", (e) => e.key === "Enter" && choose());
        return li;
      }));
      list.hidden = false;
    }, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") list.hidden = true;
    if (e.key === "ArrowDown") list.querySelector("li[tabindex]")?.focus();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search")) list.hidden = true;
  });
}

// ---------- Прочее ----------

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), Math.max(4000, text.length * 60));
}

function togglePanel(id, btn) {
  const panel = $(id);
  panel.hidden = !panel.hidden;
  btn?.setAttribute("aria-expanded", String(!panel.hidden));
}

function setupUi(user) {
  $("user-name").textContent = `${user.full_name} (${{ admin: "администратор", installer: "монтажник",
    viewer: "просмотр" }[user.role]})`;
  if (user.role !== "viewer") {
    $("menu-asbuilt").hidden = false;
    $("menu-photos").hidden = false;
  }
  if (user.role === "admin") {
    $("menu-import").hidden = false;
    $("menu-calibration").hidden = false;
    $("menu-export-kml").hidden = false;
    $("menu-export-geojson").hidden = false;
  }
  $("btn-user").addEventListener("click", () => togglePanel("user-menu", $("btn-user")));
  $("btn-logout").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" });
    location.href = "/login.html";
  });
  $("btn-layers").addEventListener("click", () => togglePanel("layers-panel", $("btn-layers")));
  document.querySelectorAll("[data-close]:not([data-close=draw-panel]):not([data-close=locate-panel]):not([data-close=nearby-panel])").forEach((b) =>
    b.addEventListener("click", () => (b.dataset.close === "card" ? closeCard() : ($(b.dataset.close).hidden = true))));
  $("filter-operator").addEventListener("change", applyFilters);
  $("filter-owner").addEventListener("change", applyFilters);
  $("filter-chip-clear").addEventListener("click", () => {
    $("filter-operator").value = "";
    $("filter-owner").value = "";
    applyFilters();
  });
  // Шторка на телефоне: касание ручки разворачивает/сворачивает карточку
  $("sheet-handle").addEventListener("click", () => $("card").classList.toggle("expanded"));

  $("btn-locate").addEventListener("click", () => {
    map.locate({ setView: true, maxZoom: 17, enableHighAccuracy: true });
  });
  let me;
  map.on("locationfound", (e) => {
    me?.remove();
    me = L.layerGroup([
      L.circle(e.latlng, { radius: e.accuracy, color: "#1d4ed8", weight: 1, fillOpacity: 0.08 }),
      L.circleMarker(e.latlng, { radius: 7, color: "#fff", weight: 3, fillColor: "#1d4ed8", fillOpacity: 1 }),
    ]).addTo(map);
  });
  map.on("locationerror", (e) =>
    toast(location.protocol === "https:" || location.hostname === "localhost"
      ? `Не удалось определить местоположение: ${e.message}`
      : "Геолокация работает только по HTTPS — на сервере компании нужен сертификат"));
  document.addEventListener("keydown", (e) => e.key === "Escape" && !draw.active && !$("card").hidden && closeCard());
}

// ---------- Запуск ----------

async function main() {
  const user = await api("/api/auth/me");
  const ref = await api("/api/map/reference");
  layers = new MapLayers(map, ref, (type, id) => {
    if (draw.active) return; // в режиме рисования щелчки обрабатывает DrawTools
    if (!editor.interceptSelect(type, id)) openObject(type, id, { fly: false });
  });
  editor = new Editor({
    user, ref, map, layers, toast, closeCard,
    reload: loadLayers,
    reopen: (type, id) => openObject(type, id, { fly: false }),
  });
  draw = new DrawTools({ user, ref, map, layers, toast, closeCard, reload: loadLayers,
    openObject: (type, id) => openObject(type, id, { fly: false }) });
  editor.ctx.draw = draw;
  editor.ctx.locate = new LocateTool({ user, map, toast, closeCard, reload: loadLayers,
    openObject: (type, id) => openObject(type, id) });
  editor.ctx.impact = new ImpactTool({ map, toast, user, reload: loadLayers,
    openObject: (type, id) => openObject(type, id),
    reopen: (type, id) => openObject(type, id, { fly: false }) });
  editor.ctx.field = new FieldTools({ map, toast, closeCard, openObject: (type, id) => openObject(type, id) });
  setupClosurePhoto({ user, toast, reload: loadLayers, openObject: (type, id) => openObject(type, id) });
  window.vols = { map, layers, draw }; // для отладки из консоли браузера и проверок интерфейса
  fillSelect($("filter-operator"), ref.operators);
  fillSelect($("filter-owner"), ref.owners);
  legend($("legend-owners"), ref.owners, "line");
  legend($("legend-operators"), ref.operators, "line");
  buildToggles();
  setupUi(user);
  setupSearch();

  readHash();
  $("filter-operator").value = state.operator_id;
  $("filter-owner").value = state.owner_id;
  if (ref.extent) {
    const [x1, y1, x2, y2] = ref.extent;
    map.fitBounds([[y1, x1], [y2, x2]], { padding: [20, 20] });
  }
  await loadLayers();
  layers.applyEnabled();
  if (state.obj) openObject(state.obj.type, state.obj.id);
  if (new URLSearchParams(location.hash.slice(1)).get("alarm")) editor.ctx.impact.showAlarm().catch((e) => toast(e.message));
  // Статусы узлов доступа меняет фоновый опрос — раз в минуту обновляем слои (если вкладка видна и не идёт рисование)
  setInterval(() => {
    if (!document.hidden && !draw.active && !editor.pick) loadLayers().catch(() => {});
  }, 60_000);
}

main().catch((e) => toast(e.message));
