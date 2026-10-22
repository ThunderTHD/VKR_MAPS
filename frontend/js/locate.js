// Поиск места повреждения: точка измерения (порт кросса или волокно кабеля) + расстояние
// с рефлектометра → место на карте, интервал погрешности, колодцы, которые стоит проверить.
import { api } from "./api.js";

const PATH_COLOR = "#7b2cbf";
const FAULT_COLOR = "#d00000";

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

const field = (label, control, hint) => el("label", { class: "field" }, label, control,
  hint ? el("span", { class: "muted small" }, hint) : null);
const fmt = (m) => (m >= 1000 ? `${(m / 1000).toFixed(3).replace(".", ",")} км` : `${Math.round(m)} м`);
const latlng = ([lon, lat]) => [lat, lon];

export class LocateTool {
  /** @param ctx — { user, map, toast, openObject(type, id), closeCard() } */
  constructor(ctx) {
    this.ctx = ctx;
    this.map = ctx.map;
    this.map.createPane("locate").style.zIndex = 465;
    this.svg = L.svg({ pane: "locate" });
    this.layer = L.layerGroup().addTo(this.map);
    this.panel = document.getElementById("locate-panel");
    this.body = this.panel.querySelector(".panel-body");
    this.panel.querySelector("[data-close]").addEventListener("click", () => this.close());
    document.querySelector("[data-tool=locate]").addEventListener("click", () => this.open());
    this.mode = "port";
    this.start = {};
  }

  get canSave() { return ["admin", "installer"].includes(this.ctx.user.role); }

  /** Открыть инструмент; preset — { cross: {id, label} } или { cable: {id, name} }. */
  open(preset = {}) {
    document.getElementById("draw-panel").hidden = true;
    document.getElementById("layers-panel").hidden = true;
    if (preset.cross) {
      this.mode = "port";
      this.start = { cross: preset.cross };
    } else if (preset.cable) {
      this.mode = "fiber";
      this.start = { cable: preset.cable, fiber: 1, end: "a" };
    }
    this.panel.hidden = false;
    document.querySelector("[data-tool=locate]").classList.add("active");
    this.renderForm();
  }

  close() {
    this.panel.hidden = true;
    this.layer.clearLayers();
    document.querySelector("[data-tool=locate]").classList.remove("active");
  }

  // ---------------------------------------------------------------- форма

  renderForm() {
    const tabs = el("div", { class: "seg-tabs", role: "tablist" },
      ...[["port", "Порт кросса"], ["fiber", "Волокно кабеля"]].map(([m, label]) => el("button", {
        type: "button", role: "tab", class: m === this.mode ? "on" : "", "aria-selected": String(m === this.mode),
        onclick: () => { this.mode = m; this.renderForm(); } }, label)));
    this.startBox = el("div", { class: "edit-form compact" });
    this.distance = el("input", { type: "number", min: "0.1", step: "0.1", name: "distance", required: true,
      placeholder: "например 1834,5", value: this.lastDistance ?? "" });
    const adv = el("details", { class: "advanced" }, el("summary", {}, "Дополнительно"),
      el("div", { class: "edit-form compact" },
        field("Измерительный шнур, м", el("input", { type: "number", name: "launch", min: "0", step: "0.1",
          value: this.adv?.launch ?? "" }), "вычитается из показания"),
        el("div", { class: "field-row" },
          field("n в приборе", el("input", { type: "number", name: "ior_set", step: "0.0001", min: "1.3", max: "1.6",
            value: this.adv?.ior_set ?? "", placeholder: "1,4680" })),
          field("n волокна", el("input", { type: "number", name: "ior_fiber", step: "0.0001", min: "1.3", max: "1.6",
            value: this.adv?.ior_fiber ?? "", placeholder: "1,4683" }))),
        field("Показание с другого конца, м", el("input", { type: "number", name: "far", min: "0.1", step: "0.1",
          value: this.adv?.far ?? "" }), "если меряли с обеих сторон — интервал станет уже")));
    this.error = el("p", { class: "form-error", role: "alert", hidden: true });
    const form = el("form", { class: "edit-form" }, tabs, this.startBox,
      field("Расстояние по рефлектометру, м", this.distance), adv, this.error,
      el("div", { class: "form-actions wrap" },
        el("button", { type: "submit", class: "btn btn-primary btn-small" }, "Найти место"),
        el("button", { type: "button", class: "btn btn-small", onclick: () => this.showTrace() }, "Показать трассу")));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      this.adv = { launch: fd.get("launch"), ior_set: fd.get("ior_set"), ior_fiber: fd.get("ior_fiber"),
        far: fd.get("far") };
      this.lastDistance = fd.get("distance");
      this.locate();
    });
    this.result = el("div", { class: "locate-result" });
    this.panel.querySelector("h2").textContent = "Поиск повреждения";
    this.body.replaceChildren(
      el("p", { class: "muted small" }, "Откуда меряли рефлектометром и какое расстояние до события он показал. ",
        "Система пройдёт по сваркам, учтёт укрутку и запасы и покажет место с интервалом погрешности."),
      form, this.result);
    this.mode === "port" ? this.renderPortPicker() : this.renderFiberPicker();
  }

  async renderPortPicker() {
    const search = el("input", { type: "search", placeholder: "Кросс: узел, адрес, название", autocomplete: "off",
      value: this.start.cross?.label ?? "" });
    const list = el("select", { size: 5, class: "pick-list", "aria-label": "Кроссы" });
    const ports = el("select", { name: "port", required: true });
    this.startBox.replaceChildren(field("Кросс", search), list, field("Порт", ports));
    const loadPorts = async (crossId) => {
      const rows = await api(`/api/crosses/${crossId}/ports`);
      ports.replaceChildren(...rows.map((p) => new Option(p.cable ? `${p.port_no} — ${p.cable}, ОВ ${p.fiber}`
        : `${p.port_no} — свободен`, p.id, false, p.id === this.start.port_id)));
      [...ports.options].forEach((o, i) => { o.disabled = !rows[i].cable; });
      if (!this.start.port_id) ports.value = rows.find((p) => p.cable)?.id ?? "";
      this.start.port_id = Number(ports.value) || null;
    };
    ports.addEventListener("change", () => { this.start.port_id = Number(ports.value); });
    const fill = async () => {
      const rows = await api("/api/crosses", { params: { q: search.value.trim() } });
      list.replaceChildren(...rows.map((x) => new Option(`${x.node} · ${x.name}${x.address ? ` · ${x.address}` : ""}` +
        ` (${x.used}/${x.port_count})`, x.id, false, x.id === this.start.cross?.id)));
      list.hidden = rows.length === 0;
    };
    let t;
    search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(fill, 250); });
    list.addEventListener("change", async () => {
      const opt = list.selectedOptions[0];
      this.start = { cross: { id: Number(opt.value), label: opt.text } };
      await loadPorts(this.start.cross.id);
    });
    await fill();
    if (this.start.cross) await loadPorts(this.start.cross.id);
  }

  renderFiberPicker() {
    const s = this.start;
    const search = el("input", { type: "search", placeholder: "Название кабеля", value: s.cable?.name ?? "",
      autocomplete: "off" });
    const list = el("select", { size: 4, class: "pick-list", hidden: true, "aria-label": "Кабели" });
    const fiber = el("input", { type: "number", min: "1", value: s.fiber ?? 1 });
    const end = el("select", {}, new Option("с начала кабеля", "a", false, s.end !== "b"),
      new Option("с конца кабеля", "b", false, s.end === "b"));
    fiber.addEventListener("change", () => { s.fiber = Number(fiber.value); });
    end.addEventListener("change", () => { s.end = end.value; });
    let t;
    search.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        const rows = (await api("/api/search", { params: { q: search.value.trim() } })).filter((r) => r.type === "cable");
        list.replaceChildren(...rows.map((r) => new Option(r.label, r.id)));
        list.hidden = rows.length === 0;
      }, 250);
    });
    list.addEventListener("change", () => {
      const o = list.selectedOptions[0];
      s.cable = { id: Number(o.value), name: o.text };
      search.value = o.text;
      list.hidden = true;
    });
    this.startBox.replaceChildren(field("Кабель", search), list,
      el("div", { class: "field-row" }, field("Волокно №", fiber), field("Откуда", end)));
    s.fiber ??= 1;
    s.end ??= "a";
  }

  _startBody() {
    if (this.mode === "port") {
      if (!this.start.port_id) throw new Error("Выберите кросс и порт, к которому подключён рефлектометр");
      return { port_id: this.start.port_id };
    }
    if (!this.start.cable) throw new Error("Выберите кабель");
    return { cable_id: this.start.cable.id, fiber: this.start.fiber, end: this.start.end };
  }

  // ---------------------------------------------------------------- трасса и результат

  async showTrace() {
    this.error.hidden = true;
    try {
      const tr = await api("/api/trace", { method: "POST", body: this._startBody() });
      this.layer.clearLayers();
      this.drawPath(tr);
      this.result.replaceChildren(this.traceBlock(tr));
    } catch (e) {
      this.fail(e);
    }
  }

  async locate(save = false, title = null) {
    this.error.hidden = true;
    const a = this.adv || {};
    const num = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
    try {
      const r = await api("/api/locate", { method: "POST", body: {
        ...this._startBody(), distance_m: Number(this.lastDistance), launch_m: num(a.launch) || 0,
        ior_set: num(a.ior_set), ior_fiber: num(a.ior_fiber), far_distance_m: num(a.far), save, title } });
      this.show(r);
      if (r.incident_id) this.ctx.toast(`Повреждение сохранено (№${r.incident_id})`);
    } catch (e) {
      this.fail(e);
    }
  }

  /** Отступы при подгонке карты: панель слева (ПК) или снизу (телефон) не должна закрывать результат. */
  _pad() {
    const mobile = window.matchMedia("(max-width: 760px)").matches;
    return mobile
      ? { paddingTopLeft: [20, 80], paddingBottomRight: [20, this.panel.offsetHeight + 20] }
      : { paddingTopLeft: [this.panel.offsetWidth + 40, 80], paddingBottomRight: [40, 40] };
  }

  fail(e) {
    this.error.textContent = e.message;
    this.error.hidden = false;
  }

  drawPath(tr) {
    const g = L.geoJSON(tr.geojson, {
      pane: "locate",
      renderer: this.svg,
      filter: (f) => f.geometry.type === "LineString",
      style: { color: PATH_COLOR, weight: 4, opacity: 0.8, dashArray: "8 6" },
      interactive: false,
    }).addTo(this.layer);
    for (const f of tr.geojson.features) {
      if (f.geometry.type !== "Point") continue;
      const p = f.properties;
      L.circleMarker(latlng(f.geometry.coordinates), { pane: "locate", renderer: this.svg, radius: 6, weight: 2,
        color: "#fff", fillColor: p.event === "splice" ? "#ffb703" : PATH_COLOR, fillOpacity: 1 })
        .bindTooltip(`${fmt(p.opt_m)} — ${p.label}`, { direction: "top" }).addTo(this.layer);
    }
    const b = g.getBounds();
    if (b.isValid()) this.map.fitBounds(b, { ...this._pad(), maxZoom: 17 });
  }

  show(r) {
    this.layer.clearLayers();
    this.drawPath(r.trace);
    if (r.interval.line.length > 1) {
      L.polyline(r.interval.line.map(latlng), { pane: "locate", renderer: this.svg, color: FAULT_COLOR, weight: 12,
        opacity: 0.35, lineCap: "round", interactive: false }).addTo(this.layer);
    }
    for (const w of r.wells) {
      if (!w.point) continue;
      L.circleMarker(latlng(w.point), { pane: "locate", renderer: this.svg, radius: 8, color: FAULT_COLOR, weight: 2,
        fillColor: "#fff", fillOpacity: 1 })
        .bindTooltip(`${w.label}: ${w.offset_m > 0 ? "+" : ""}${Math.round(w.offset_m)} м`, { direction: "right" })
        .on("click", () => this.ctx.openObject("node", w.node_id)).addTo(this.layer);
    }
    const pt = latlng(r.point);
    L.marker(pt, { pane: "locate", icon: L.divIcon({ className: "fault-icon", html: "<span></span>",
      iconSize: [30, 30] }), interactive: false }).addTo(this.layer);
    const bounds = L.latLngBounds([pt]);
    if (r.interval.line.length) r.interval.line.forEach((c) => bounds.extend(latlng(c)));
    this.map.fitBounds(bounds.pad(0.6), { ...this._pad(), maxZoom: 18 });
    this.result.replaceChildren(this.resultBlock(r));
    this.result.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  resultBlock(r) {
    const p = r.place;
    const parts = [
      el("div", { class: "fault-card" },
        el("div", { class: "fault-where" }, p.text),
        el("div", { class: "fault-acc" }, `± ${Math.round(r.half_width_m)} м`,
          el("span", { class: "muted small" }, " (95 %)")),
        el("div", { class: "muted small" }, `${fmt(r.position_m)} по трассе от точки измерения · интервал `,
          `${fmt(r.interval.from_m)} – ${fmt(r.interval.to_m)}`),
        el("div", { class: "muted small" }, `Координаты: ${r.point[1].toFixed(6)}, ${r.point[0].toFixed(6)}`)),
    ];
    for (const w of r.warnings) parts.push(el("p", { class: "warn-line" }, w));
    if (r.two_sided) {
      const t = r.two_sided;
      parts.push(el("p", { class: "muted small" }, `С начала: ${fmt(t.from_start_m)} ± ${Math.round(t.from_start_half_width_m)} м; `,
        `с конца: ${fmt(t.from_end_m)} ± ${Math.round(t.from_end_half_width_m)} м; расхождение ${Math.round(t.disagreement_m)} м.`));
    }
    if (r.wells.length) {
      parts.push(el("h3", {}, "Проверить в первую очередь"), el("ul", { class: "links" }, r.wells.slice(0, 8).map((w) =>
        el("li", {}, el("button", { type: "button", class: "link", onclick: () => this.ctx.openObject("node", w.node_id) },
          el("b", {}, w.label), el("span", { class: "muted small" },
            ` ${w.offset_m >= 0 ? "дальше" : "ближе"} на ${Math.abs(Math.round(w.offset_m))} м`))))));
    }
    parts.push(el("h3", {}, "Из чего складывается погрешность"), el("ul", { class: "contrib" }, r.contributions.map((c) =>
      el("li", {}, el("div", { class: "bar", style: `width:${Math.max(2, c.share_pct)}%` }),
        el("span", {}, c.source), el("span", { class: "muted small" }, ` ${c.share_pct} %`)))));
    if (this.canSave) {
      const title = el("input", { placeholder: "Название повреждения (необязательно)" });
      parts.push(el("div", { class: "edit-form compact save-box" }, title,
        el("button", { type: "button", class: "btn btn-small btn-primary", onclick: () => this.locate(true, title.value) },
          "Сохранить повреждение")));
    }
    parts.push(this.traceBlock(r.trace));
    return el("div", {}, ...parts);
  }

  traceBlock(tr) {
    const state = { port: "тракт заканчивается портом кросса", open: "конец волокна никуда не подключён",
      loop: "в рассветке петля" }[tr.end_state];
    return el("details", { class: "trace-block", open: !this.result?.childElementCount },
      el("summary", {}, `Трасса волокна: ${fmt(tr.total_opt_m)} оптически, ${tr.legs.length} кабел(я/ей)`),
      el("p", { class: "muted small" }, `Физическая длина ${fmt(tr.total_phys_m)}; ${state}.`),
      el("table", { class: "data-table events" },
        el("thead", {}, el("tr", {}, el("th", {}, "м (опт.)"), el("th", {}, "Событие"))),
        el("tbody", {}, tr.events.map((e) => el("tr", {}, el("td", {}, Math.round(e.opt_m)),
          el("td", {}, e.label, e.node ? el("span", { class: "muted small" }, ` · ${e.node}`) : null))))));
  }
}
