// Редактирование из карточки: формы правки, перенос узла, объединение дублей, удаление,
// назначение типа неклассифицированным, история изменений.
import { api } from "./api.js";
import { getPosition, routeUrl } from "./field.js";

const ACTION_RU = { update: "изменение", move: "перенос", merge: "объединение", delete: "удаление",
  classify: "назначение типа", import: "импорт", create: "создание", login: "вход", reshape: "изменение формы",
  split: "вставка точки / разрез", reroute: "перекладка", splice: "сварка", unsplice: "разварка",
  connect: "подключение портов", disconnect: "отключение портов", attach: "документ добавлен",
  detach: "документ удалён", repair: "устранено, отмечено место" };
const FIELD_RU = { kind: "тип", code: "код", name: "название", owner_id: "владелец", address: "адрес",
  note: "примечание", fact_length_m: "длина, м", length_source: "источник длины", length_note: "комментарий",
  channels_total: "каналов", lon: "долгота", lat: "широта", operator_id: "оператор", twist_factor: "укрутка",
  lay_year: "год прокладки", length_m: "длина, м", vertices: "изгибов", segments: "участков", number: "номер",
  model: "модель", connector: "разъём", port_count: "портов", a: "волокно A", b: "волокно Б", ports: "порты",
  fibers: "волокна", cable: "кабель", filename: "файл", new_name: "новый кабель", title: "название",
  status: "статус", error_m: "ошибка, м", route_error_m: "ошибка по трассе, м" };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined));
  return node;
}

function select(name, options, value, { empty } = {}) {
  const s = el("select", { name });
  if (empty) s.append(new Option(empty, ""));
  for (const [v, label] of options) s.append(new Option(label, v, false, String(v) === String(value ?? "")));
  return s;
}

const DOC_OWNERS = ["node", "segment", "cable", "closure", "cross", "access_node", "incident"];
const DOC_KIND_RU = { splice_scheme: "Схема рассветки", route_scheme: "Схема трассы", passport: "Паспорт",
  act: "Акт", asbuilt: "Исполнительная документация", photo: "Фото", other: "Прочее" };

function field(label, control) {
  return el("label", { class: "field" }, label, control);
}

export class Editor {
  /**
   * @param ctx — { user, ref, map, layers, reload(), reopen(type, id), toast(text), closeCard() }
   */
  constructor(ctx) {
    this.ctx = ctx;
    this.pick = null; // активный режим выбора на карте: { kind: "point"|"node", resolve }
    ctx.map.on("click", (e) => {
      if (this.pick?.kind === "point") this._finishPick({ lon: e.latlng.lng, lat: e.latlng.lat });
    });
    document.addEventListener("keydown", (e) => e.key === "Escape" && this.pick && this._cancelPick());
  }

  get isAdmin() { return this.ctx.user.role === "admin"; }
  get isInstaller() { return this.ctx.user.role === "installer"; }

  /** Перехват выбора объекта на карте в режиме «выберите узел». Вернуть true, если обработано. */
  interceptSelect(type, id) {
    if (this.pick?.kind === "node") {
      if (type !== "node") {
        this.ctx.toast("Выберите узел (колодец, опору, точку)");
        return true;
      }
      this._finishPick({ id });
      return true;
    }
    return this.pick?.kind === "point";
  }

  /** Кнопки действий под заголовком карточки. */
  actions(card) {
    const box = el("div", { class: "card-actions" });
    const btn = (label, fn, cls = "") => box.append(el("button", { type: "button", class: `btn btn-small ${cls}`,
      onclick: fn }, label));
    if (card.type === "node" && this.isAdmin) {
      btn("Изменить", () => this.nodeForm(card));
      btn("Поставить муфту", () => this.closureForm(card, null));
      btn("Поставить кросс", () => this.crossForm(card, null));
      btn("Переместить", () => this.moveNode(card));
      btn("Объединить с…", () => this.mergeNode(card));
      btn("Удалить", () => this.deleteNode(card), "btn-danger");
    }
    if (card.type === "segment" && (this.isAdmin || this.isInstaller)) {
      btn(this.isAdmin ? "Изменить" : "Уточнить длину", () => this.segmentForm(card));
      if (this.isAdmin) {
        btn("Форма трассы", () => this.ctx.draw.reshape(card));
        btn("Вставить колодец", () => this.ctx.draw.split(card));
        btn("Удалить", () => this.deleteSegment(card), "btn-danger");
      }
    }
    if (card.type === "cable") {
      btn("Найти повреждение", () => this.ctx.locate.open({ cable: { id: card.id, name: card.title } }));
    }
    if (card.type === "cable" && this.isAdmin) {
      btn("Изменить", () => this.cableForm(card));
      btn("Перепроложить", () => this.ctx.draw.reroute(card));
      btn("Удалить", () => this.deleteCable(card), "btn-danger");
    }
    if (card.type === "closure") {
      box.append(el("a", { class: "btn btn-small btn-primary", href: `/splice.html?closure=${card.id}` }, "Рассветка"));
      if (this.isAdmin) {
        btn("Изменить", () => this.closureForm(card, card));
        btn("Удалить", () => this.deleteClosure(card), "btn-danger");
      }
    }
    if (card.type === "cross") {
      btn("Найти повреждение", () => this.ctx.locate.open({ cross: { id: card.id, label: card.title } }));
      if (this.isAdmin || this.isInstaller) btn("Подключить волокна", () => this.portsForm(card));
      if (this.isAdmin) {
        btn("Изменить", () => this.crossForm(card, card));
        btn("Удалить", () => this.deleteCross(card), "btn-danger");
      }
    }
    if (card.type === "jumper" && (this.isAdmin || this.isInstaller)) {
      btn("Изменить подпись", async () => {
        const label = prompt("Подпись перемычки:", card.edit?.label || "");
        if (label === null) return;
        try {
          await api(`/api/jumpers/${card.id}`, { method: "PATCH", body: { label } });
          await this.ctx.reload();
          this.ctx.reopen("jumper", card.id);
        } catch (e) {
          this.ctx.toast(e.message);
        }
      });
      if (this.isAdmin) {
        btn("Удалить", async () => {
          if (!confirm("Удалить перемычку?")) return;
          await api(`/api/jumpers/${card.id}`, { method: "DELETE" });
          this.ctx.closeCard();
          await this.ctx.reload();
        }, "btn-danger");
      }
    }
    if (card.type === "unclassified" && this.isAdmin) {
      btn("Назначить тип", () => this.classifyForm(card));
    }
    if (card.type === "incident" && card.lonlat && card.status !== "repaired") {
      box.append(el("a", { class: "btn btn-small", href: routeUrl(card.lonlat), target: "_blank", rel: "noopener" },
        "Маршрут"));
    }
    if (card.type === "incident" && (this.isAdmin || this.isInstaller)) {
      if (card.status === "localized" || card.status === "open") {
        btn("Отметить фактическое место", () => this.markActual(card), "btn-primary");
      }
      btn("Изменить", () => this.incidentForm(card));
      if (card.status === "cancelled" || card.status === "repaired") {
        btn("Вернуть в работу", () => this.incidentStatus(card, "localized"));
      } else {
        btn("Отменить", () => this.incidentStatus(card, "cancelled"), "btn-danger");
      }
      if (this.isAdmin) btn("Удалить", () => this.deleteIncident(card), "btn-danger");
    }
    this.ctx.impact?.buttons(card, btn);
    if (["node", "segment", "unclassified", "cable", "closure", "cross", "incident"].includes(card.type)) {
      btn("История", () => this.history(card), "btn-ghost");
    }
    return box.childElementCount ? box : null;
  }

  // ---------- формы ----------

  _form(card, title, controls, onSubmit) {
    const body = document.getElementById("card-body");
    const error = el("p", { class: "form-error", role: "alert", hidden: true });
    const form = el("form", { class: "edit-form" }, el("h3", {}, title), ...controls, error,
      el("div", { class: "form-actions" },
        el("button", { type: "submit", class: "btn btn-primary btn-small" }, "Сохранить"),
        el("button", { type: "button", class: "btn btn-small", onclick: () => this.ctx.reopen(card.type, card.id) },
          "Отмена")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      error.hidden = true;
      try {
        await onSubmit(new FormData(form));
        await this.ctx.reload();
        this.ctx.reopen(card.type, card.id);
        this.ctx.toast("Сохранено");
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    });
    body.replaceChildren(form);
    form.querySelector("input, select")?.focus();
  }

  nodeForm(card) {
    const e = card.edit;
    const owners = this.ctx.ref.owners.map((o) => [o.id, o.name]);
    this._form(card, "Изменить узел", [
      field("Тип", select("kind", Object.entries(this.ctx.ref.node_kinds), e.kind)),
      field("Код из схемы", el("input", { name: "code", value: e.code ?? "", placeholder: "к.№348, 57121" })),
      field("Название", el("input", { name: "name", value: e.name ?? "", placeholder: "Понятное название узла" })),
      field("Владелец", select("owner_id", owners, e.owner_id, { empty: "не указан" })),
      field("Адрес", el("input", { name: "address", value: e.address ?? "" })),
      field("Примечание", el("textarea", { name: "note", rows: 2 }, e.note ?? "")),
    ], (fd) => api(`/api/nodes/${card.id}`, { method: "PATCH", body: {
      kind: fd.get("kind"), code: fd.get("code"), name: fd.get("name"),
      owner_id: Number(fd.get("owner_id")) || null, address: fd.get("address"), note: fd.get("note"),
    } }));
  }

  segmentForm(card) {
    const e = card.edit;
    const sources = this.ctx.ref.length_sources.filter((s) => s.code !== "geometry")
      .map((s) => [s.code, `${s.name} (±${s.error_pct} %)`]);
    const lengthControls = [
      field("Фактическая длина, м", el("input", { name: "fact_length_m", type: "number", step: "0.1", min: "0",
        value: e.fact_length_m ?? "", placeholder: "пусто — длина неизвестна" })),
      field("Источник длины", select("length_source", sources, e.length_source, { empty: "—" })),
      field("Комментарий к длине", el("input", { name: "length_note", value: e.length_note ?? "",
        placeholder: "кто сообщил, по какому документу" })),
    ];
    const owners = this.ctx.ref.owners.map((o) => [o.id, o.name]);
    const adminControls = this.isAdmin ? [
      field("Тип участка", select("kind", Object.entries(this.ctx.ref.segment_kinds), e.kind)),
      field("Владелец", select("owner_id", owners, e.owner_id, { empty: "не указан" })),
      field("Название", el("input", { name: "name", value: e.name ?? "" })),
      field("Каналов", el("input", { name: "channels_total", type: "number", min: "0", value: e.channels_total ?? "" })),
    ] : [];
    this._form(card, this.isAdmin ? "Изменить участок" : "Уточнить длину участка", [...lengthControls, ...adminControls],
      (fd) => {
        const len = fd.get("fact_length_m");
        const body = { fact_length_m: len === "" ? null : Number(len), length_source: fd.get("length_source") || null,
          length_note: fd.get("length_note") };
        if (this.isAdmin) {
          Object.assign(body, { kind: fd.get("kind"), owner_id: Number(fd.get("owner_id")) || null,
            name: fd.get("name"), channels_total: fd.get("channels_total") === "" ? null : Number(fd.get("channels_total")) });
        }
        return api(`/api/segments/${card.id}`, { method: "PATCH", body });
      });
  }

  cableForm(card) {
    const e = card.edit;
    this._form(card, "Изменить кабель", [
      field("Название", el("input", { name: "name", value: e.name ?? "", required: true })),
      field("Оператор", select("operator_id", this.ctx.ref.operators.map((o) => [o.id, o.name]), e.operator_id)),
      field("Укрутка, %", el("input", { name: "twist", type: "number", step: "0.1", min: "0", max: "19",
        value: Math.round(e.twist_factor * 1000) / 10 })),
      field("Год прокладки", el("input", { name: "lay_year", type: "number", min: "1970", max: "2100",
        value: e.lay_year ?? "" })),
      field("Примечание", el("textarea", { name: "note", rows: 2 }, e.note ?? "")),
      el("p", { class: "muted small" }, "Марку кабеля сменить нельзя: от неё зависят волокна и рассветка. ",
        "Путь кабеля меняется кнопкой «Перепроложить»."),
    ], (fd) => api(`/api/cables/${card.id}`, { method: "PATCH", body: {
      name: fd.get("name"), operator_id: Number(fd.get("operator_id")),
      twist_factor: (Number(fd.get("twist")) || 0) / 100, lay_year: Number(fd.get("lay_year")) || null,
      note: fd.get("note") || null } }));
  }

  async deleteCable(card) {
    if (!confirm(`Удалить кабель «${card.title}» вместе с волокнами и маршрутом?`)) return;
    try {
      await api(`/api/cables/${card.id}`, { method: "DELETE" });
      this.ctx.closeCard();
      await this.ctx.reload();
      this.ctx.toast("Кабель удалён");
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  // ---------- муфты и кроссы ----------

  /** Новая муфта в узле (card — узел) или правка существующей (existing — карточка муфты). */
  closureForm(card, existing) {
    const e = existing?.edit || {};
    const ops = this.ctx.ref.operators.map((o) => [o.id, o.name]);
    this._form(card, existing ? "Изменить муфту" : "Новая муфта в узле", [
      field("Номер", el("input", { name: "number", value: e.number ?? "", required: true, placeholder: "М1, М 17/10" })),
      field("Модель", el("input", { name: "model", value: e.model ?? "", placeholder: "МОГ-У, МТОК…" })),
      field("Оператор", select("operator_id", ops, e.operator_id, { empty: "не указан" })),
      field("Примечание", el("textarea", { name: "note", rows: 2 }, e.note ?? "")),
    ], async (fd) => {
      const body = { number: fd.get("number"), model: fd.get("model") || null,
        operator_id: Number(fd.get("operator_id")) || null, note: fd.get("note") || null };
      if (existing) return api(`/api/closures/${card.id}`, { method: "PATCH", body });
      const r = await api("/api/closures", { method: "POST", body: { ...body, node_id: card.id } });
      card.type = "closure";
      card.id = r.id;
    });
  }

  crossForm(card, existing) {
    const e = existing?.edit || {};
    const ops = this.ctx.ref.operators.map((o) => [o.id, o.name]);
    this._form(card, existing ? "Изменить кросс" : "Новый кросс в узле", [
      field("Название", el("input", { name: "name", value: e.name ?? "ODF-1", required: true })),
      field("Портов", el("input", { name: "port_count", type: "number", min: "1", max: "288", required: true,
        value: e.port_count ?? 24 })),
      field("Разъём", el("input", { name: "connector", value: e.connector ?? "SC/UPC" })),
      field("Модель", el("input", { name: "model", value: e.model ?? "" })),
      field("Оператор", select("operator_id", ops, e.operator_id, { empty: "не указан" })),
      existing ? el("p", { class: "muted small" }, "Уменьшить число портов можно, только если лишние свободны.") : null,
    ].filter(Boolean), async (fd) => {
      const body = { name: fd.get("name"), port_count: Number(fd.get("port_count")),
        connector: fd.get("connector") || null, model: fd.get("model") || null,
        operator_id: Number(fd.get("operator_id")) || null };
      if (existing) return api(`/api/crosses/${card.id}`, { method: "PATCH", body });
      const r = await api("/api/crosses", { method: "POST", body: { ...body, node_id: card.id } });
      card.type = "cross";
      card.id = r.id;
    });
  }

  async portsForm(card) {
    const sheet = await api(`/api/crosses/${card.id}/sheet`);
    const firstFree = (sheet.ports.find((p) => !p.fiber) || {}).port_no || 1;
    if (!sheet.cables.length) {
      this.ctx.toast("В этом узле не начинается и не заканчивается ни один кабель");
      return;
    }
    const cables = sheet.cables.map((c) => [`${c.id}:${c.end}`,
      `${c.name} — ${c.end === "a" ? "начало" : "конец"} (${c.fibers} ОВ)`]);
    this._form(card, "Подключить волокна к портам", [
      field("Кабель", select("cable", cables, cables[0][0])),
      el("div", { class: "field-row" },
        field("С волокна", el("input", { name: "fiber_from", type: "number", min: "1", value: 1, required: true })),
        field("На порт", el("input", { name: "port_from", type: "number", min: "1", max: sheet.cross.port_count,
          value: firstFree, required: true }))),
      field("Сколько подряд", el("input", { name: "count", type: "number", min: "1", value: 1, required: true })),
      el("p", { class: "muted small" }, `Свободно портов: ${sheet.ports.filter((p) => !p.fiber).length} из `,
        `${sheet.cross.port_count}. Отключить порты — ниже.`),
    ], (fd) => {
      const [cableId, end] = fd.get("cable").split(":");
      return api(`/api/crosses/${card.id}/connect`, { method: "POST", body: { cable_id: Number(cableId), end,
        fiber_from: Number(fd.get("fiber_from")), port_from: Number(fd.get("port_from")),
        count: Number(fd.get("count")) } });
    });
    // Отключение — отдельной маленькой формой под основной
    const off = el("form", { class: "edit-form" }, el("h3", {}, "Отключить порты"),
      el("div", { class: "field-row" },
        field("С порта", el("input", { name: "port_from", type: "number", min: "1", value: 1, required: true })),
        field("Сколько", el("input", { name: "count", type: "number", min: "1", value: 1, required: true }))),
      el("div", { class: "form-actions" }, el("button", { type: "submit", class: "btn btn-small btn-danger" }, "Отключить")));
    off.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(off);
      try {
        const r = await api(`/api/crosses/${card.id}/disconnect`, { method: "POST", body: {
          port_from: Number(fd.get("port_from")), count: Number(fd.get("count")) } });
        this.ctx.toast(`Отключено портов: ${r.count}`);
        this.ctx.reopen("cross", card.id);
      } catch (err) {
        this.ctx.toast(err.message);
      }
    });
    document.getElementById("card-body").append(off);
  }

  async deleteClosure(card) {
    const splices = card.fields.find(([k]) => k === "Сварок")?.[1] || 0;
    if (!confirm(`Удалить ${card.title}${splices ? ` вместе со сварками (${splices})` : ""}?`)) return;
    try {
      await api(`/api/closures/${card.id}`, { method: "DELETE" });
      this.ctx.closeCard();
      await this.ctx.reload();
      this.ctx.toast("Муфта удалена");
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  async deleteCross(card) {
    if (!confirm(`Удалить кросс «${card.title}»? Подключённые волокна будут отключены.`)) return;
    try {
      await api(`/api/crosses/${card.id}`, { method: "DELETE" });
      this.ctx.closeCard();
      await this.ctx.reload();
      this.ctx.toast("Кросс удалён");
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  // ---------- повреждения ----------

  /** Где повреждение оказалось на самом деле: по GPS телефона (монтажник на месте) или щелчком на карте. */
  markActual(card) {
    const body = document.getElementById("card-body");
    const status = el("p", { class: "muted small" });
    body.replaceChildren(el("div", { class: "edit-form" }, el("h3", {}, "Фактическое место повреждения"),
      el("p", { class: "muted small" }, "Если вы на месте повреждения — отметьте по GPS. Иначе укажите точку на карте."),
      el("button", { type: "button", class: "btn btn-primary", onclick: async () => {
        status.textContent = "Определяю местоположение…";
        try {
          const p = await getPosition();
          if (p.accuracy > 30 && !confirm(`Точность GPS ± ${Math.round(p.accuracy)} м — это много. Всё равно отметить?`)) {
            status.textContent = "";
            return;
          }
          await this._saveActual(card, { lon: p.lon, lat: p.lat, note: `отмечено по GPS, точность ± ${Math.round(p.accuracy)} м` });
        } catch (e) {
          status.textContent = e.message;
        }
      } }, "Я здесь — отметить по GPS"),
      el("button", { type: "button", class: "btn", onclick: () => this._markOnMap(card) }, "Указать на карте"),
      status,
      el("button", { type: "button", class: "btn btn-small btn-ghost", onclick: () => this.ctx.reopen("incident", card.id) },
        "Отмена")));
  }

  async _markOnMap(card) {
    const pos = await this._startPick("point",
      "Щёлкните на карте место, где повреждение оказалось на самом деле. Ошибка прогноза посчитается сама.");
    if (!pos) {
      this.ctx.reopen("incident", card.id);
      return;
    }
    await this._saveActual(card, pos);
  }

  async _saveActual(card, pos) {
    try {
      const r = await api(`/api/incidents/${card.id}/actual`, { method: "POST", body: pos });
      await this.ctx.reload();
      this.ctx.reopen("incident", card.id);
      const a = r.actual;
      this.ctx.toast(`Отмечено. Ошибка прогноза: ${Math.round(a.route_error_m)} м по трассе` +
        (a.within_interval ? " — в пределах интервала" : " — вне интервала") +
        (a.off_route_m > 50 ? `; точка в ${Math.round(a.off_route_m)} м от трассы` : ""));
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  incidentForm(card) {
    const e = card.edit;
    this._form(card, "Изменить повреждение", [
      field("Название", el("input", { name: "title", value: e.title ?? "" })),
      field("Примечание", el("textarea", { name: "note", rows: 3 }, e.note ?? "")),
    ], (fd) => api(`/api/incidents/${card.id}`, { method: "PATCH",
      body: { title: fd.get("title"), note: fd.get("note") } }));
  }

  async incidentStatus(card, status) {
    if (status === "cancelled" && !confirm("Отменить повреждение (например, ложная тревога)?")) return;
    try {
      await api(`/api/incidents/${card.id}`, { method: "PATCH", body: { status } });
      await this.ctx.reload();
      this.ctx.reopen("incident", card.id);
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  async deleteIncident(card) {
    if (!confirm(`Удалить «${card.title}» из журнала?`)) return;
    try {
      await api(`/api/incidents/${card.id}`, { method: "DELETE" });
      this.ctx.closeCard();
      await this.ctx.reload();
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  // ---------- документы ----------

  async documents(card) {
    const box = document.getElementById("card-docs");
    if (!box || !DOC_OWNERS.includes(card.type)) return;
    let docs;
    try {
      docs = await api("/api/documents", { params: { owner_type: card.type, owner_id: card.id } });
    } catch {
      return;
    }
    if (document.getElementById("card-docs") !== box) return; // карточку уже сменили
    const canUpload = this.isAdmin || this.isInstaller;
    if (!docs.length && !canUpload) return;
    const size = (b) => (b > 1048576 ? `${(b / 1048576).toFixed(1)} МБ` : `${Math.max(1, Math.round(b / 1024))} КБ`);
    const photos = docs.filter((d) => d.inline && d.kind === "photo");
    const gallery = photos.length ? el("div", { class: "photo-strip" }, photos.map((d) =>
      el("a", { href: `/api/documents/${d.id}/file`, target: "_blank", rel: "noopener", title: d.note || d.filename },
        el("img", { src: `/api/documents/${d.id}/file`, alt: d.note || "фото", loading: "lazy" })))) : null;
    const list = el("ul", { class: "docs" }, docs.map((d) => el("li", {},
      el("a", { href: `/api/documents/${d.id}/file`, target: "_blank", rel: "noopener" }, d.filename),
      el("span", { class: "meta" }, [d.kind_ru, size(d.size), new Date(d.created_at).toLocaleDateString("ru-RU"),
        d.user, d.note].filter(Boolean).join(" · ")),
      d.lonlat ? el("span", { class: "photo-geo" }, `📍 снято ${d.coord_source === "ocr" ? "(по подписи на кадре) " : ""}` +
        (d.distance_m !== null ? `в ${Math.round(d.distance_m)} м от объекта` : "") +
        (d.taken_at ? ` · ${new Date(d.taken_at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })}` : ""))
        : null,
      this.isAdmin ? el("button", { type: "button", class: "btn btn-small btn-ghost", title: "Удалить документ",
        onclick: async () => {
          if (!confirm(`Удалить документ «${d.filename}»?`)) return;
          try {
            await api(`/api/documents/${d.id}`, { method: "DELETE" });
            this.documents(card);
          } catch (e) {
            this.ctx.toast(e.message);
          }
        } }, "✕") : null)));
    const parts = [el("h3", {}, `Документы и фото${docs.length ? ` (${docs.length})` : ""}`), gallery,
      docs.length ? list : null];
    if (canUpload) {
      // Фото с места: на телефоне сразу открывается камера
      const cam = el("input", { type: "file", accept: "image/*", capture: "environment", hidden: true });
      cam.addEventListener("change", async () => {
        const f = cam.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append("file", f, f.name || "фото.jpg");
        fd.append("owner_type", card.type);
        fd.append("owner_id", card.id);
        fd.append("kind", "photo");
        this.ctx.toast("Загружаю фото…");
        try {
          const r = await api("/api/documents", { method: "POST", body: fd });
          this.ctx.toast(r.coord_source ? "Фото прикреплено, место съёмки определено" : "Фото прикреплено");
          this.documents(card);
        } catch (err) {
          this.ctx.toast(err.message);
        }
      });
      parts.push(el("button", { type: "button", class: "btn btn-small photo-btn", onclick: () => cam.click() },
        "📷 Сфотографировать"), cam);
      const file = el("input", { type: "file", name: "file", required: true,
        accept: ".pdf,.png,.jpg,.jpeg,.gif,.webp,.heic,.doc,.docx,.xls,.xlsx,.odt,.ods,.txt,.csv,.sor,.kml,.kmz,.dwg,.dxf,.zip,.7z,.rar" });
      const kinds = Object.entries(DOC_KIND_RU);
      const form = el("form", { class: "doc-upload" }, file,
        select("kind", kinds, card.type === "closure" ? "splice_scheme" : "other"),
        el("input", { name: "note", placeholder: "Комментарий (необязательно)", class: "doc-note" }),
        el("button", { type: "submit", class: "btn btn-small" }, "Прикрепить файл"));
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        fd.append("owner_type", card.type);
        fd.append("owner_id", card.id);
        try {
          await api("/api/documents", { method: "POST", body: fd });
          this.ctx.toast("Файл прикреплён");
          this.documents(card);
        } catch (err) {
          this.ctx.toast(err.message);
        }
      });
      parts.push(form);
    }
    box.replaceChildren(...parts.filter(Boolean));
  }

  classifyForm(card) {
    const isPoint = card.geom_type === "POINT";
    const owners = this.ctx.ref.owners.map((o) => [o.id, o.name]);
    const kinds = isPoint ? this.ctx.ref.node_kinds : this.ctx.ref.segment_kinds;
    this._form(card, isPoint ? "Сделать точкой сети" : "Сделать участком трассы", [
      field("Тип", select("kind", Object.entries(kinds), isPoint ? "unknown" : "duct")),
      field("Владелец", select("owner_id", owners, null, { empty: "не указан" })),
      isPoint ? null : el("p", { class: "muted small" },
        "Концы линии привяжутся к ближайшим узлам (до 10 м), иначе будут созданы новые точки."),
    ].filter(Boolean), async (fd) => {
      const res = await api(`/api/unclassified/${card.id}/classify`, { method: "POST", body: {
        target: isPoint ? "node" : "segment",
        node_kind: isPoint ? fd.get("kind") : null, segment_kind: isPoint ? null : fd.get("kind"),
        owner_id: Number(fd.get("owner_id")) || null,
      } });
      // Объекта больше нет — открываем то, во что он превратился
      card.type = isPoint ? "node" : "segment";
      card.id = isPoint ? res.node_id : res.segment_id;
    });
  }

  // ---------- действия на карте ----------

  _startPick(kind, hint) {
    this._cancelPick();
    return new Promise((resolve) => {
      this.pick = { kind, resolve };
      const banner = el("div", { class: "pick-banner", role: "status" }, hint,
        el("button", { type: "button", class: "btn btn-small", onclick: () => this._cancelPick() }, "Отмена"));
      document.body.append(banner);
      document.body.classList.add("picking");
      // Выбор места: щелчки по объектам слоёв должны доходить до карты (см. .no-hit в css)
      if (kind === "point") document.body.classList.add("no-hit");
      this.pick.banner = banner;
    });
  }

  _finishPick(value) {
    const p = this.pick;
    this.pick = null;
    p.banner.remove();
    document.body.classList.remove("picking");
    if (!this.ctx.draw?.active) document.body.classList.remove("no-hit");
    p.resolve(value);
  }

  _cancelPick() {
    if (this.pick) this._finishPick(null);
  }

  async moveNode(card) {
    const pos = await this._startPick("point", "Щёлкните на карте новое место узла. Концы участков переместятся вместе с ним.");
    if (!pos) return;
    try {
      await api(`/api/nodes/${card.id}/move`, { method: "POST", body: pos });
      await this.ctx.reload();
      this.ctx.reopen("node", card.id);
      this.ctx.toast("Узел перенесён");
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  async mergeNode(card) {
    const picked = await this._startPick("node",
      `Выберите на карте дубль узла «${card.title}». Его связи перейдут к этому узлу, а дубль будет удалён.`);
    if (!picked) return;
    if (picked.id === card.id) {
      this.ctx.toast("Это тот же узел");
      return;
    }
    const other = await api(`/api/objects/node/${picked.id}`);
    if (!confirm(`Оставить «${card.title}» и удалить «${other.title}», перенеся к нему все участки, муфты и узлы доступа?`)) {
      return;
    }
    try {
      const r = await api("/api/nodes/merge", { method: "POST", body: { keep_id: card.id, remove_id: picked.id } });
      await this.ctx.reload();
      this.ctx.reopen("node", card.id);
      this.ctx.toast(`Объединено: перепривязано участков — ${r.segments_relinked}`);
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  async deleteNode(card) {
    if (!confirm(`Удалить «${card.title}»?`)) return;
    try {
      await api(`/api/nodes/${card.id}`, { method: "DELETE" });
      this.ctx.closeCard();
      await this.ctx.reload();
      this.ctx.toast("Узел удалён");
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  async deleteSegment(card) {
    if (!confirm(`Удалить участок «${card.title}»?`)) return;
    try {
      await api(`/api/segments/${card.id}`, { method: "DELETE" });
      this.ctx.closeCard();
      await this.ctx.reload();
      this.ctx.toast("Участок удалён");
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  // ---------- история ----------

  async history(card) {
    const entity = { node: "network_node", segment: "route_segment", unclassified: "unclassified_feature",
      cable: "cable", closure: "closure", cross: "optical_cross", incident: "incident" }[card.type];
    const rows = await api("/api/audit", { params: { entity_type: entity, entity_id: card.id } });
    const body = document.getElementById("card-body");
    const list = rows.length ? rows.map((r) => {
      const changes = [];
      for (const [k, v] of Object.entries(r.after || {})) {
        const before = r.before?.[k];
        if (typeof v !== "object" && before !== v) {
          changes.push(`${FIELD_RU[k] || k}: ${before ?? "—"} → ${v ?? "—"}`);
        }
      }
      return el("li", {},
        el("div", {}, el("b", {}, ACTION_RU[r.action] || r.action), " · ",
          new Date(r.ts).toLocaleString("ru-RU"), " · ", r.user || "система"),
        changes.length ? el("div", { class: "muted small" }, changes.join("; ")) : null);
    }) : [el("li", { class: "muted" }, "Изменений не было")];
    body.replaceChildren(el("h3", {}, "История изменений"), el("ul", { class: "history" }, list),
      el("button", { type: "button", class: "btn btn-small", onclick: () => this.ctx.reopen(card.type, card.id) },
        "← К карточке"));
  }
}
