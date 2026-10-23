// Работа в поле (телефон монтажника): моё местоположение, что рядом, маршрут, отметка по GPS.
import { api } from "./api.js";

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

const fmt = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1).replace(".", ",")} км` : `${Math.round(m)} м`);

/** Текущие координаты телефона. Геолокация в браузере работает только по HTTPS (или на localhost). */
export function getPosition({ timeout = 15000 } = {}) {
  return new Promise((resolve, reject) => {
    if (!window.isSecureContext) {
      reject(new Error("Геолокация работает только по защищённому адресу https://… — откройте систему " +
        "по ссылке со страницы «Вход с телефона» (phone.bat на ноутбуке)"));
      return;
    }
    if (!navigator.geolocation) {
      reject(new Error("Браузер не поддерживает определение местоположения"));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lon: p.coords.longitude, lat: p.coords.latitude, accuracy: p.coords.accuracy }),
      (e) => reject(new Error(e.code === 1 ? "Доступ к местоположению запрещён — разрешите его в настройках браузера"
        : "Не удалось определить местоположение. Выйдите на открытое место и попробуйте ещё раз")),
      { enableHighAccuracy: true, timeout, maximumAge: 10000 });
  });
}

/** Ссылка на маршрут в Яндекс Картах (на телефоне откроется приложение, если оно установлено). */
export function routeUrl(to, from = null) {
  const [lon, lat] = to;
  const a = from ? `${from.lat},${from.lon}` : "";
  return `https://yandex.ru/maps/?rtext=${a}~${lat},${lon}&rtt=auto`;
}

export class FieldTools {
  /** @param ctx — { map, toast, openObject(type, id) } */
  constructor(ctx) {
    this.ctx = ctx;
    this.panel = document.getElementById("nearby-panel");
    this.body = this.panel.querySelector(".panel-body");
    this.tab = "objects";
    this.me = null;
    this.layer = L.layerGroup().addTo(ctx.map);
    this.panel.querySelector("[data-close]").addEventListener("click", () => this.close());
    document.getElementById("btn-nearby").addEventListener("click", () => this.open());
  }

  /** Открыть карточку объекта: панель прячем (на телефоне она перекрыла бы карточку), отметку «я здесь» оставляем. */
  show(type, id) {
    this.panel.hidden = true;
    this.ctx.openObject(type, id);
  }

  close() {
    this.panel.hidden = true;
    this.layer.clearLayers();
  }

  async open() {
    for (const id of ["draw-panel", "locate-panel", "layers-panel"]) {
      const p = document.getElementById(id);
      if (p) p.hidden = true;
    }
    this.ctx.closeCard?.();
    this.panel.hidden = false;
    this.body.replaceChildren(el("p", { class: "muted" }, "Определяю местоположение…"));
    try {
      this.me = await getPosition();
    } catch (e) {
      this.body.replaceChildren(el("p", { class: "form-error" }, e.message),
        el("button", { type: "button", class: "btn btn-small", onclick: () => this.open() }, "Ещё раз"));
      return;
    }
    await this.load();
  }

  async load(radius = 300) {
    const data = await api("/api/nearby", { params: { lon: this.me.lon, lat: this.me.lat, radius } });
    this.data = data;
    this.radius = radius;
    this.drawMe();
    this.render();
  }

  drawMe() {
    this.layer.clearLayers();
    const ll = [this.me.lat, this.me.lon];
    L.circle(ll, { radius: this.me.accuracy, color: "#1d4ed8", weight: 1, fillOpacity: 0.08, interactive: false })
      .addTo(this.layer);
    L.circleMarker(ll, { radius: 8, color: "#fff", weight: 3, fillColor: "#1d4ed8", fillOpacity: 1, interactive: false })
      .addTo(this.layer);
    this.ctx.map.setView(ll, Math.max(this.ctx.map.getZoom(), 17));
  }

  render() {
    const d = this.data;
    const tabs = el("div", { class: "seg-tabs", role: "tablist" },
      ...[["objects", `Рядом (${d.objects.length})`], ["incidents", `Повреждения (${d.incidents.length})`]].map(([k, t]) =>
        el("button", { type: "button", role: "tab", class: k === this.tab ? "on" : "", "aria-selected": String(k === this.tab),
          onclick: () => { this.tab = k; this.render(); } }, t)));
    const acc = el("p", { class: "muted small" }, `Точность GPS ± ${Math.round(this.me.accuracy)} м · радиус ${fmt(this.radius)}`,
      el("button", { type: "button", class: "link inline", onclick: () => this.open() }, " обновить"));
    let list;
    if (this.tab === "objects") {
      list = d.objects.length ? el("ul", { class: "near-list" }, d.objects.map((o) => el("li", {},
        el("button", { type: "button", class: "near-item", onclick: () => this.show(o.type, o.id) },
          el("span", { class: "near-dist" }, fmt(o.distance_m)),
          el("span", {}, el("b", {}, o.label), el("span", { class: "muted small" }, ` · ${o.kind_ru}`,
            o.closures ? ` · муфт: ${o.closures}` : ""), o.address ? el("div", { class: "muted small" }, o.address) : null)))))
        : el("div", {}, el("p", { class: "muted" }, `В радиусе ${fmt(this.radius)} точек сети нет.`),
          this.radius < 2000 ? el("button", { type: "button", class: "btn btn-small", onclick: () => this.load(this.radius * 4) },
            `Искать в ${fmt(this.radius * 4)}`) : null);
    } else {
      list = d.incidents.length ? el("ul", { class: "near-list" }, d.incidents.map((i) => el("li", {},
        el("div", { class: "near-item" },
          el("span", { class: "near-dist" }, fmt(i.distance_m)),
          el("span", {}, el("b", {}, i.title || `Повреждение №${i.id}`),
            el("div", { class: "muted small" }, i.half_width_m ? `прогноз ± ${Math.round(i.half_width_m)} м` : ""),
            el("div", { class: "near-actions" },
              el("button", { type: "button", class: "btn btn-small", onclick: () => this.show("incident", i.id) },
                "Открыть"),
              el("a", { class: "btn btn-small btn-primary", href: routeUrl(i.lonlat, this.me), target: "_blank",
                rel: "noopener" }, "Маршрут")))))))
        : el("p", { class: "muted" }, "Повреждений в работе нет.");
    }
    this.body.replaceChildren(tabs, acc, list);
  }
}

/** «Муфта по фото»: фото муфты → координаты с фото → муфта на ближайшей точке/трассе (монтажник, администратор). */
export function setupClosurePhoto({ user, toast, reload, openObject }) {
  const btn = document.getElementById("btn-closure-photo");
  const input = document.getElementById("closure-photo-file");
  if (user.role === "viewer") return;
  btn.hidden = false;
  btn.addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    const f = input.files[0];
    input.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f, f.name || "муфта.jpg");
    btn.disabled = true;
    toast("Читаю координаты с фото…");
    try {
      const r = await api("/api/photos/closure", { method: "POST", body: fd });
      await reload();
      openObject("closure", r.closure_id);
      const src = r.source === "exif" ? "из данных фото" : "по подписи на кадре";
      toast(`${r.message}. Координаты ${src}` + (r.accuracy_m ? `, точность ± ${Math.round(r.accuracy_m)} м` : "") + ".");
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
    }
  });
}
