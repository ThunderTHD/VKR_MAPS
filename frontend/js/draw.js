// Рисование на карте (администратор): новые точки, участки трасс с привязкой к узлам,
// изменение формы трассы, вставка колодца в участок, прокладка и перекладка кабеля по трассам.
//
// Привязка: щелчок ближе SNAP_PX экранных пикселей к точке сети считается щелчком по этой точке.
// Сервер дополнительно проверяет расстояние в метрах (3 м), так что дублей рядом не появится.
import { api } from "./api.js";

const SNAP_PX = 15;
const ROUTE_COLOR = "#2563eb";
const CABLE_COLOR = "#9d4edd";

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

function select(name, options, value, { empty } = {}) {
  const s = el("select", { name });
  if (empty) s.append(new Option(empty, ""));
  for (const [v, label] of options) s.append(new Option(label, v, false, String(v) === String(value ?? "")));
  return s;
}

const field = (label, control) => el("label", { class: "field" }, label, control);
const fmtLen = (m) => (m >= 1000 ? `${(m / 1000).toFixed(2).replace(".", ",")} км` : `${Math.round(m)} м`);
const sameLL = (a, b) => a && b && a.lat === b.lat && a.lng === b.lng;

export class DrawTools {
  /** @param ctx — { user, ref, map, layers, toast, reload(), openObject(type, id), closeCard() } */
  constructor(ctx) {
    this.ctx = ctx;
    this.mode = null;
    this.map = ctx.map;
    this.map.createPane("draw").style.zIndex = 470;
    this.svg = L.svg({ pane: "draw" });
    this.group = L.layerGroup().addTo(this.map); // всё, что нарисовано в текущем режиме
    this.cursor = L.layerGroup().addTo(this.map); // «резинка» и кольцо привязки
    this.panel = document.getElementById("draw-panel");
    this.rail = document.getElementById("draw-tools");

    // Кнопки рисования — только администратору (перемычку может нарисовать и монтажник);
    // «Повреждение» обслуживает LocateTool
    this.rail.querySelectorAll(".admin-tool").forEach((b) => {
      const allowed = ctx.user.role === "admin" || (b.dataset.tool === "jumper" && ctx.user.role === "installer");
      if (!allowed) return;
      b.hidden = false;
      b.addEventListener("click", () => this.start(b.dataset.tool));
    });
    this.panel.querySelector("[data-close]")?.addEventListener("click", () => this.stop());
    this.map.on("click", (e) => this.mode && this._onClick(e));
    this.map.on("mousemove", (e) => this.mode && this._onMove(e));
    document.addEventListener("keydown", (e) => this._onKey(e));
  }

  get active() { return this.mode !== null; }

  // ---------------------------------------------------------------- общий каркас режимов

  start(mode, opts = {}) {
    this.stop({ silent: true });
    this.mode = mode;
    this.opts = opts;
    this.points = [];
    this.ctx.closeCard();
    document.getElementById("layers-panel").hidden = true;
    document.querySelector("#locate-panel [data-close]")?.click();
    document.body.classList.add("drawing", "no-hit");
    this.map.doubleClickZoom.disable();
    this.rail.querySelectorAll(".admin-tool").forEach((b) => b.classList.toggle("active", b.dataset.tool === mode));
    ({ point: () => this._pointPanel(), route: () => this._routePanel(), cable: () => this._cablePanel(),
      reshape: () => this._reshapeStart(), split: () => this._splitPanel(), jumper: () => this._jumperPanel() })[mode]();
    if (!this.mode) return; // режим не смог начаться (например, участок не загружен)
    this.panel.hidden = false;
    if (this.map.getZoom() < 16 && mode !== "reshape" && !this.zoomHinted) {
      this.zoomHinted = true;
      this.ctx.toast("Приблизьте карту: на крупном масштабе точнее привязка к колодцам");
    }
  }

  stop({ silent = false, reopen = true } = {}) {
    if (!this.mode) return;
    const back = this.opts?.back;
    this.mode = null;
    this.group.clearLayers();
    this.cursor.clearLayers();
    this.panel.hidden = true;
    document.body.classList.remove("drawing", "no-hit");
    this.map.doubleClickZoom.enable();
    this.rail.querySelectorAll(".admin-tool").forEach((b) => b.classList.remove("active"));
    if (!silent && reopen && back) this.ctx.openObject(back.type, back.id);
  }

  _setPanel(title, ...content) {
    this.panel.querySelector("h2").textContent = title;
    this.panel.querySelector(".panel-body").replaceChildren(...content.flat().filter(Boolean));
  }

  _onKey(e) {
    if (!this.mode) return;
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
    if (e.key === "Escape") this.stop();
    else if (!typing && e.key === "Backspace") {
      e.preventDefault();
      this._undo();
    } else if (!typing && e.key === "Enter") {
      e.preventDefault();
      this._finish?.();
    }
  }

  /** Ближайшая точка сети к щелчку (в пикселях экрана) — из загруженного слоя узлов и уже поставленных новых. */
  _snap(latlng, { includePending = true } = {}) {
    const p = this.map.latLngToContainerPoint(latlng);
    let best = null;
    let bestD = SNAP_PX;
    for (const f of this.ctx.layers.data.nodes?.features || []) {
      const [lon, lat] = f.geometry.coordinates;
      const d = p.distanceTo(this.map.latLngToContainerPoint([lat, lon]));
      if (d < bestD) {
        bestD = d;
        best = { node_id: f.properties.id, latlng: L.latLng(lat, lon),
          label: f.properties.code || f.properties.name || this.ctx.ref.node_kinds[f.properties.kind] };
      }
    }
    if (includePending) {
      for (const pt of this.points || []) {
        if (!pt.end || pt.node_id) continue;
        const d = p.distanceTo(this.map.latLngToContainerPoint(pt.latlng));
        if (d < bestD) {
          bestD = d;
          best = { node_id: null, latlng: pt.latlng, label: "новая точка", pending: true };
        }
      }
    }
    return best;
  }

  _onMove(e) {
    this.cursor.clearLayers();
    if (this.mode === "reshape") return;
    const snap = this._snap(e.latlng);
    const to = snap ? snap.latlng : e.latlng;
    if (snap) {
      L.circleMarker(to, { pane: "draw", renderer: this.svg, radius: 11, color: "#ffb703", weight: 3, fill: false,
        interactive: false }).bindTooltip(snap.label || "", { permanent: true, direction: "top", offset: [0, -10],
        className: "snap-tip" }).addTo(this.cursor);
    }
    const last = this.points?.at(-1);
    if (this.mode === "route" && last) {
      L.polyline([last.latlng, to], { pane: "draw", renderer: this.svg, color: ROUTE_COLOR, weight: 2,
        dashArray: "4 6", interactive: false }).addTo(this.cursor);
    }
  }

  _onClick(e) {
    const handler = { point: this._pointClick, route: this._routeClick, cable: this._cableClick,
      split: this._splitClick, jumper: this._jumperClick }[this.mode];
    handler?.call(this, e);
  }

  _undo() {
    if (this.mode === "route" || this.mode === "cable") {
      this.points.pop();
      this.mode === "route" ? this._routeRedraw() : this._cableUpdate();
    }
  }

  _actions(...buttons) {
    return el("div", { class: "form-actions wrap" }, ...buttons);
  }

  _btn(label, onclick, cls = "") {
    return el("button", { type: "button", class: `btn btn-small ${cls}`, onclick }, label);
  }

  // ---------------------------------------------------------------- точка

  // ---------------------------------------------------------------- перемычка

  _jumperPanel() {
    this.jumper = { a: null, b: null };
    this._jumperRender();
  }

  _jumperRender() {
    const { a, b } = this.jumper;
    const step = !a ? "Щёлкните первый объект: муфту, узел доступа, кросс или колодец."
      : !b ? "Теперь второй объект." : "Подпишите перемычку и сохраните.";
    const row = (title, o) => el("p", { class: "small" }, el("b", {}, `${title}: `), o ? `${o.type_ru} ${o.label}` : "—");
    const parts = [el("p", { class: "muted small" }, "Перемычка — линия с подписью между двумя объектами: патч-корд между " +
      "кроссами, резерв волокон между муфтами. В трассировке не участвует."), el("p", {}, step), row("Откуда", a), row("Куда", b)];
    if (a && b) {
      const error = el("p", { class: "form-error", role: "alert", hidden: true });
      const form = el("form", { class: "edit-form" },
        field("Подпись", el("input", { name: "label", placeholder: "например, Резерв 24 волокна", maxlength: 200 })),
        error, this._actions(el("button", { type: "submit", class: "btn btn-primary btn-small" }, "Сохранить"),
          this._btn("Заново", () => { this.group.clearLayers(); this._jumperPanel(); })));
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        try {
          const r = await api("/api/jumpers", { method: "POST", body: { a_type: a.type, a_id: a.id, b_type: b.type,
            b_id: b.id, label: new FormData(form).get("label") } });
          this.stop({ silent: true });
          await this.ctx.reload();
          this.ctx.openObject("jumper", r.id);
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
        }
      });
      parts.push(form);
      setTimeout(() => form.querySelector("input")?.focus(), 0);
    }
    this._setPanel("Перемычка", ...parts);
  }

  async _jumperClick(e) {
    if (this.jumper.a && this.jumper.b) return;
    // Радиус поиска объекта — около 20 пикселей экрана в метрах на текущем масштабе
    const mpp = (40075016 * Math.cos((e.latlng.lat * Math.PI) / 180)) / 2 ** (this.map.getZoom() + 8);
    const within = Math.min(Math.max(20 * mpp, 5), 150);
    const hit = await api("/api/jumpers/nearest", { params: { lon: e.latlng.lng, lat: e.latlng.lat, within_m: within } });
    if (!hit) {
      this.ctx.toast("Здесь нет муфты, узла, кросса или колодца — щёлкните ближе к объекту");
      return;
    }
    const ll = [hit.lonlat[1], hit.lonlat[0]];
    L.circleMarker(ll, { pane: "draw", renderer: this.svg, radius: 9, color: "#7209b7", weight: 3, fillOpacity: 0.2 })
      .addTo(this.group);
    if (!this.jumper.a) this.jumper.a = hit;
    else {
      if (hit.type === this.jumper.a.type && hit.id === this.jumper.a.id) {
        this.ctx.toast("Это тот же объект — выберите другой");
        return;
      }
      this.jumper.b = hit;
      const a = this.jumper.a.lonlat;
      L.polyline([[a[1], a[0]], ll], { pane: "draw", renderer: this.svg, color: "#7209b7", weight: 3, dashArray: "6 4" })
        .addTo(this.group);
    }
    this._jumperRender();
  }

  _pointPanel() {
    this._setPanel("Новая точка", el("p", { class: "muted small" },
      "Щёлкните на карте место колодца, опоры или здания. Если рядом уже есть точка, откроется она."));
  }

  _pointClick(e) {
    const snap = this._snap(e.latlng, { includePending: false });
    if (snap) {
      this.ctx.toast(`Здесь уже есть точка: ${snap.label}`);
      return;
    }
    this.group.clearLayers();
    L.circleMarker(e.latlng, { pane: "draw", renderer: this.svg, radius: 7, color: "#fff", weight: 3,
      fillColor: "#f77f00", fillOpacity: 1 }).addTo(this.group);
    const owners = this.ctx.ref.owners.map((o) => [o.id, o.name]);
    const error = el("p", { class: "form-error", role: "alert", hidden: true });
    const form = el("form", { class: "edit-form" },
      field("Тип", select("kind", Object.entries(this.ctx.ref.node_kinds).filter(([k]) => k !== "unknown"),
        this.lastNodeKind || "well")),
      field("Код из схемы", el("input", { name: "code", placeholder: "к.№348, 57121" })),
      field("Название", el("input", { name: "name" })),
      field("Владелец", select("owner_id", owners, this.lastOwner, { empty: "не указан" })),
      field("Адрес", el("input", { name: "address" })),
      error,
      this._actions(el("button", { type: "submit", class: "btn btn-primary btn-small" }, "Создать"),
        this._btn("Другое место", () => this._pointPanel())));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(form);
      this.lastNodeKind = fd.get("kind");
      this.lastOwner = fd.get("owner_id");
      try {
        const r = await api("/api/nodes", { method: "POST", body: {
          lon: e.latlng.lng, lat: e.latlng.lat, kind: fd.get("kind"), code: fd.get("code"), name: fd.get("name"),
          owner_id: Number(fd.get("owner_id")) || null, address: fd.get("address") || null } });
        await this.ctx.reload();
        this.ctx.toast("Точка создана — можно ставить следующую");
        this.group.clearLayers();
        this._pointPanel();
        this.created = r.id;
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    });
    this._setPanel("Новая точка", form);
    form.querySelector("input[name=code]").focus();
  }

  // ---------------------------------------------------------------- участки трассы

  _routePanel() {
    const ref = this.ctx.ref;
    const owners = ref.owners.map((o) => [o.id, o.name]);
    const s = this.routeSettings ||= { kind: "duct", owner_id: "", new_kind: "well", bend: false };
    const kind = select("kind", Object.entries(ref.segment_kinds), s.kind);
    const newKind = select("new_kind", Object.entries(ref.node_kinds)
      .filter(([k]) => !["unknown", "site"].includes(k)), s.new_kind);
    const owner = select("owner_id", owners, s.owner_id, { empty: "не указан" });
    const bend = el("input", { type: "checkbox", name: "bend", checked: s.bend });
    kind.addEventListener("change", () => {
      s.kind = kind.value;
      // По опорам ставят опоры, по канализации — колодцы
      s.new_kind = newKind.value = ["aerial"].includes(kind.value) ? "pole" : kind.value === "duct" ? "well"
        : newKind.value;
    });
    newKind.addEventListener("change", () => (s.new_kind = newKind.value));
    owner.addEventListener("change", () => (s.owner_id = owner.value));
    bend.addEventListener("change", () => (s.bend = bend.checked));
    this.routeStatus = el("div", { class: "draw-status" });
    this.routeError = el("p", { class: "form-error", role: "alert", hidden: true });
    this._finish = () => this._routeSave();
    this._setPanel("Трасса",
      el("p", { class: "muted small" }, "Щёлкайте по колодцам по порядку. Щелчок по пустому месту ставит новую точку ",
        "выбранного типа. Изгиб трассы без колодца — с зажатым Shift или с включённой галочкой ниже."),
      el("div", { class: "edit-form" },
        field("Тип участка", kind), field("Владелец", owner), field("Новые точки", newKind),
        el("label", { class: "toggle" }, bend, el("span", {}, "Щелчок по пустому месту — изгиб, а не точка"))),
      this.routeStatus, this.routeError,
      this._actions(el("button", { type: "button", class: "btn btn-primary btn-small", onclick: () => this._routeSave() },
        "Сохранить"), this._btn("↶ Шаг назад", () => this._undo()), this._btn("Отмена", () => this.stop())),
      el("p", { class: "muted small kbd-hint" }, "Enter — сохранить · Backspace — шаг назад · Esc — отмена"));
    this._routeRedraw();
  }

  _routeClick(e) {
    const snap = this._snap(e.latlng);
    const s = this.routeSettings;
    const last = this.points.at(-1);
    let pt;
    if (snap) pt = { latlng: snap.latlng, node_id: snap.node_id, end: true, label: snap.label };
    else if ((s.bend || e.originalEvent.shiftKey) && last) pt = { latlng: e.latlng, end: false };
    else pt = { latlng: e.latlng, node_id: null, end: true };
    if (last && sameLL(last.latlng, pt.latlng)) return; // повторный щелчок в ту же точку
    this.points.push(pt);
    this._routeRedraw();
  }

  /** Разбить поставленные точки на участки: каждый — от точки сети до точки сети, изгибы внутри. */
  _routePieces() {
    const pieces = [];
    let cur = null;
    for (const pt of this.points) {
      if (!cur) {
        cur = { start: pt, vertices: [] };
        continue;
      }
      if (pt.end) {
        pieces.push({ ...cur, end: pt });
        cur = { start: pt, vertices: [] };
      } else {
        cur.vertices.push(pt);
      }
    }
    return { pieces, open: cur && cur.vertices.length ? cur : null };
  }

  _routeRedraw() {
    this.group.clearLayers();
    const { pieces, open } = this._routePieces();
    let total = 0;
    for (const p of pieces) {
      const lls = [p.start.latlng, ...p.vertices.map((v) => v.latlng), p.end.latlng];
      for (let i = 1; i < lls.length; i++) total += lls[i - 1].distanceTo(lls[i]);
      L.polyline(lls, { pane: "draw", renderer: this.svg, color: ROUTE_COLOR, weight: 4, opacity: 0.9,
        interactive: false }).addTo(this.group);
    }
    if (open) {
      L.polyline([open.start.latlng, ...open.vertices.map((v) => v.latlng)], { pane: "draw", renderer: this.svg,
        color: ROUTE_COLOR, weight: 3, dashArray: "6 6", interactive: false }).addTo(this.group);
    }
    for (const pt of this.points) {
      const style = !pt.end ? { radius: 4, color: ROUTE_COLOR, weight: 2, fillColor: "#fff" }
        : pt.node_id ? { radius: 8, color: "#ffb703", weight: 3, fillColor: "#fff", fillOpacity: 0.2 }
          : { radius: 6, color: "#fff", weight: 2, fillColor: "#f77f00" };
      L.circleMarker(pt.latlng, { pane: "draw", renderer: this.svg, fillOpacity: 1, interactive: false, ...style })
        .addTo(this.group);
    }
    const newNodes = new Set(this.points.filter((p) => p.end && !p.node_id).map((p) => `${p.latlng.lat},${p.latlng.lng}`));
    if (this.routeStatus) {
      this.routeStatus.replaceChildren(this.points.length
        ? el("div", {}, el("b", {}, `Участков: ${pieces.length}`), ` · ≈${fmtLen(total)} по карте`,
          newNodes.size ? ` · новых точек: ${newNodes.size}` : "")
        : el("span", { class: "muted" }, "Щёлкните начальный колодец"));
    }
  }

  async _routeSave() {
    // Последний щелчок-изгиб превращаем в конечную точку, иначе участок останется незамкнутым
    const last = this.points.at(-1);
    if (last && !last.end) last.end = true;
    const { pieces } = this._routePieces();
    if (!pieces.length) {
      this.ctx.toast("Нужно минимум две точки");
      return;
    }
    const s = this.routeSettings;
    const end = (pt) => (pt.node_id ? { node_id: pt.node_id } : { lon: pt.latlng.lng, lat: pt.latlng.lat });
    let saved = 0;
    try {
      for (const p of pieces) {
        // Новая точка, созданная предыдущим участком, найдётся сервером по координатам (привязка 3 м)
        await api("/api/segments", { method: "POST", body: {
          start: end(p.start), end: end(p.end), vertices: p.vertices.map((v) => [v.latlng.lng, v.latlng.lat]),
          kind: s.kind, owner_id: Number(s.owner_id) || null, new_node_kind: s.new_kind } });
        saved++;
      }
    } catch (err) {
      this.routeError.textContent = `Сохранено участков: ${saved} из ${pieces.length}. Ошибка: ${err.message}`;
      this.routeError.hidden = false;
      await this.ctx.reload();
      return;
    }
    await this.ctx.reload();
    this.ctx.toast(`Сохранено участков: ${saved}`);
    this.points = [];
    this._routeRedraw();
  }

  // ---------------------------------------------------------------- кабель по трассе

  /** opts.cable — перекладка существующего кабеля {id, title}; иначе — новый кабель. */
  _cablePanel() {
    const ref = this.ctx.ref;
    const reroute = this.opts.cable;
    this.cableInfo = el("div", { class: "draw-status" });
    this.cableError = el("p", { class: "form-error", role: "alert", hidden: true });
    this.cableList = el("ol", { class: "waypoints" });
    const form = el("form", { class: "edit-form" });
    // Поля кабеля появляются, когда маршрут найден: на телефоне сначала видна карта, а не длинная форма
    this.cableFields = el("div", { class: "edit-form", hidden: true });
    if (!reroute) {
      const c = this.cableSettings ||= { operator_id: ref.operators[0]?.id, cable_type_id: ref.cable_types[0]?.id };
      form.append(this.cableFields);
      this.cableFields.append(
        field("Название", el("input", { name: "name", required: true, placeholder: "СЛ 16000149/03, ОК-1…" })),
        field("Оператор", select("operator_id", ref.operators.map((o) => [o.id, o.name]), c.operator_id)),
        field("Марка", select("cable_type_id", ref.cable_types.map((t) => [t.id, `${t.mark} · ${t.fibers} ОВ`]),
          c.cable_type_id)),
        el("div", { class: "field-row" },
          field("Укрутка, %", el("input", { name: "twist", type: "number", step: "0.1", min: "0", max: "19",
            value: "0.5" })),
          field("Год прокладки", el("input", { name: "lay_year", type: "number", min: "1970", max: "2100" }))),
        el("div", { class: "field-row" },
          field("Запас в начале, м", el("input", { name: "res_a", type: "number", step: "1", min: "0" })),
          field("Запас в конце, м", el("input", { name: "res_b", type: "number", step: "1", min: "0" }))),
      );
    }
    form.append(this.cableError, this._actions(
      el("button", { type: "submit", class: "btn btn-primary btn-small" }, reroute ? "Сохранить маршрут" : "Проложить"),
      this._btn("↶ Шаг назад", () => this._undo()), this._btn("Отмена", () => this.stop())));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      this._cableSave(new FormData(form));
    });
    this.cableForm = form;
    this._finish = () => form.requestSubmit();
    this._setPanel(reroute ? `Перекладка: ${reroute.title}` : "Кабель по трассе",
      el("p", { class: "muted small" }, "Щёлкните начало кабеля, затем конец. Путь соберётся сам — кратчайший по ",
        "трассам. Если кабель идёт иначе, щёлкните промежуточные колодцы по порядку."),
      this.cableList, this.cableInfo, form);
    this._cableUpdate();
  }

  _cableClick(e) {
    const snap = this._snap(e.latlng, { includePending: false });
    if (!snap) {
      this.ctx.toast("Щёлкните по точке сети: кабель прокладывается только по существующим трассам");
      return;
    }
    if (this.points.at(-1)?.node_id === snap.node_id) return;
    this.points.push(snap);
    this._cableUpdate();
  }

  async _cableUpdate() {
    this.group.clearLayers();
    this.cableError.hidden = true;
    this.cablePath = null;
    this.cableFields.hidden = true;
    this.cableList.replaceChildren(...this.points.map((p, i) => el("li", {},
      el("span", {}, p.label || `точка ${p.node_id}`),
      el("span", { class: "muted small" }, i === 0 ? " начало" : i === this.points.length - 1 ? " конец" : ""))));
    this.points.forEach((p, i) => L.circleMarker(p.latlng, { pane: "draw", renderer: this.svg, radius: 9,
      color: CABLE_COLOR, weight: 3, fillColor: "#fff", fillOpacity: 1, interactive: false })
      .bindTooltip(String(i + 1), { permanent: true, direction: "center", className: "wp-num" }).addTo(this.group));
    if (this.points.length < 2) {
      this.cableInfo.replaceChildren(el("span", { class: "muted" },
        this.points.length ? "Теперь щёлкните конец кабеля" : "Щёлкните начало кабеля"));
      return;
    }
    const seq = (this.cableSeq = (this.cableSeq || 0) + 1);
    try {
      const r = await api("/api/routing/path", { method: "POST", body: { waypoints: this.points.map((p) => p.node_id) } });
      if (seq !== this.cableSeq || this.mode !== "cable") return; // пользователь уже щёлкнул дальше
      this.cablePath = r;
      this.cableFields.hidden = false;
      L.geoJSON(r.geojson, { pane: "draw", renderer: this.svg, interactive: false,
        style: { color: CABLE_COLOR, weight: 6, opacity: 0.85, lineCap: "round" } }).addTo(this.group);
      this.group.eachLayer((l) => l.bringToFront?.());
      this.cableInfo.replaceChildren(
        el("div", {}, el("b", {}, `Длина по трассе: ${fmtLen(r.length_m)}`), ` · участков: ${r.segments.length}`),
        r.unknown_length_segments
          ? el("div", { class: "muted small" }, `Без фактической длины: ${r.unknown_length_segments} — `,
            "для них взята длина по карте")
          : null,
        ...r.warnings.map((w) => el("div", { class: "form-error small" }, w)));
    } catch (err) {
      if (seq !== this.cableSeq) return;
      this.cableError.textContent = err.message;
      this.cableError.hidden = false;
      this.cableInfo.replaceChildren();
    }
  }

  async _cableSave(fd) {
    if (!this.cablePath) {
      this.cableError.textContent = this.points.length < 2 ? "Укажите начало и конец кабеля" : "Маршрут не найден";
      this.cableError.hidden = false;
      return;
    }
    const waypoints = this.points.map((p) => p.node_id);
    try {
      if (this.opts.cable) {
        await api(`/api/cables/${this.opts.cable.id}/route`, { method: "PUT", body: { waypoints } });
        await this.ctx.reload();
        this.ctx.toast("Маршрут кабеля изменён");
        this.stop();
        return;
      }
      const nodes = this.cablePath.nodes;
      const reserves = [["res_a", nodes[0]], ["res_b", nodes.at(-1)]]
        .filter(([k]) => Number(fd.get(k)) > 0).map(([k, node_id]) => ({ node_id, length_m: Number(fd.get(k)) }));
      this.cableSettings = { operator_id: fd.get("operator_id"), cable_type_id: fd.get("cable_type_id") };
      const r = await api("/api/cables", { method: "POST", body: {
        name: fd.get("name"), operator_id: Number(fd.get("operator_id")), cable_type_id: Number(fd.get("cable_type_id")),
        waypoints, twist_factor: (Number(fd.get("twist")) || 0) / 100, lay_year: Number(fd.get("lay_year")) || null,
        reserves } });
      await this.ctx.reload();
      this.stop({ silent: true });
      this.ctx.openObject("cable", r.id);
      this.ctx.toast("Кабель проложен");
    } catch (err) {
      this.cableError.textContent = err.message;
      this.cableError.hidden = false;
    }
  }

  // ---------------------------------------------------------------- форма участка

  reshape(card) {
    this.start("reshape", { segment: card, back: { type: "segment", id: card.id } });
  }

  _reshapeStart() {
    const card = this.opts.segment;
    const f = this.ctx.layers.data.segments?.features.find((x) => x.properties.id === card.id);
    if (!f) {
      this.ctx.toast("Участок не загружен на карте");
      this.stop({ silent: true });
      return;
    }
    this.coords = f.geometry.coordinates.map(([lon, lat]) => L.latLng(lat, lon));
    this.reshapeInfo = el("div", { class: "draw-status" });
    const error = el("p", { class: "form-error", role: "alert", hidden: true });
    this._finish = async () => {
      try {
        const r = await api(`/api/segments/${card.id}/geometry`, { method: "PUT", body: {
          vertices: this.coords.slice(1, -1).map((ll) => [ll.lng, ll.lat]) } });
        await this.ctx.reload();
        this.ctx.toast(`Форма сохранена, длина по карте ${fmtLen(r.length_m)}`);
        this.stop();
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    };
    this._setPanel(`Форма: ${card.title}`,
      el("p", { class: "muted small" }, "Перетаскивайте белые точки-изгибы. Полупрозрачная точка посередине отрезка ",
        "добавляет новый изгиб. Двойной щелчок по изгибу удаляет его. Концы участка привязаны к точкам сети — чтобы ",
        "сдвинуть конец, переместите сам колодец."),
      this.reshapeInfo, error,
      this._actions(el("button", { type: "button", class: "btn btn-primary btn-small", onclick: () => this._finish() },
        "Сохранить"), this._btn("Отмена", () => this.stop())));
    this.map.fitBounds(L.latLngBounds(this.coords), { maxZoom: 18, padding: [80, 80] });
    this._reshapeDraw();
  }

  _reshapeDraw() {
    this.group.clearLayers();
    const line = L.polyline(this.coords, { pane: "draw", renderer: this.svg, color: ROUTE_COLOR, weight: 4,
      interactive: false }).addTo(this.group);
    const handle = (cls) => L.divIcon({ className: `vertex-handle ${cls}`, iconSize: [16, 16] });
    let length = 0;
    for (let i = 1; i < this.coords.length; i++) length += this.coords[i - 1].distanceTo(this.coords[i]);
    this.reshapeInfo.replaceChildren(el("b", {}, `≈${fmtLen(length)} по карте`), ` · изгибов: ${this.coords.length - 2}`);
    this.coords.forEach((ll, i) => {
      const isEnd = i === 0 || i === this.coords.length - 1;
      const m = L.marker(ll, { pane: "draw", icon: handle(isEnd ? "end" : ""), draggable: !isEnd,
        keyboard: false }).addTo(this.group);
      if (isEnd) return;
      m.on("drag", (e) => {
        this.coords[i] = e.target.getLatLng();
        line.setLatLngs(this.coords);
      });
      m.on("dragend", () => this._reshapeDraw());
      m.on("dblclick", (e) => {
        L.DomEvent.stop(e);
        this.coords.splice(i, 1);
        this._reshapeDraw();
      });
    });
    // Середины отрезков: потянуть — появится новый изгиб
    for (let i = 1; i < this.coords.length; i++) {
      const a = this.coords[i - 1];
      const b = this.coords[i];
      const mid = L.latLng((a.lat + b.lat) / 2, (a.lng + b.lng) / 2);
      const m = L.marker(mid, { pane: "draw", icon: handle("ghost"), draggable: true, keyboard: false })
        .addTo(this.group);
      m.on("dragstart", () => this.coords.splice(i, 0, mid));
      m.on("drag", (e) => {
        this.coords[i] = e.target.getLatLng();
        line.setLatLngs(this.coords);
      });
      m.on("dragend", () => this._reshapeDraw());
      m.on("click", () => {
        this.coords.splice(i, 0, mid);
        this._reshapeDraw();
      });
    }
  }

  // ---------------------------------------------------------------- вставка точки в участок

  split(card) {
    this.start("split", { segment: card, back: { type: "segment", id: card.id } });
  }

  _splitPanel() {
    const kinds = Object.entries(this.ctx.ref.node_kinds).filter(([k]) => !["unknown", "site"].includes(k));
    this.splitKind = select("kind", kinds, "well");
    this._setPanel(`Вставить точку: ${this.opts.segment.title}`,
      el("p", { class: "muted small" }, "Щёлкните на участке место нового колодца или опоры. Участок разделится на два, ",
        "кабели, проходящие по нему, пройдут через новую точку. Известная длина делится пропорционально."),
      el("div", { class: "edit-form" }, field("Тип новой точки", this.splitKind)),
      this._actions(this._btn("Отмена", () => this.stop())));
  }

  async _splitClick(e) {
    try {
      const r = await api(`/api/segments/${this.opts.segment.id}/split`, { method: "POST", body: {
        lon: e.latlng.lng, lat: e.latlng.lat, kind: this.splitKind.value } });
      await this.ctx.reload();
      this.stop({ silent: true });
      this.ctx.openObject("node", r.node_id);
      this.ctx.toast("Точка вставлена — заполните код колодца");
    } catch (err) {
      this.ctx.toast(err.message);
    }
  }

  // ---------------------------------------------------------------- перекладка кабеля

  reroute(card) {
    this.start("cable", { cable: card, back: { type: "cable", id: card.id } });
  }
}
