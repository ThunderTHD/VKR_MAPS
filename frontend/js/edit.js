// Редактирование из карточки: формы правки, перенос узла, объединение дублей, удаление,
// назначение типа неклассифицированным, история изменений.
import { api } from "./api.js";

const ACTION_RU = { update: "изменение", move: "перенос", merge: "объединение", delete: "удаление",
  classify: "назначение типа", import: "импорт", create: "создание", login: "вход" };
const FIELD_RU = { kind: "тип", code: "код", name: "название", owner_id: "владелец", address: "адрес",
  note: "примечание", fact_length_m: "длина, м", length_source: "источник длины", length_note: "комментарий",
  channels_total: "каналов", lon: "долгота", lat: "широта" };

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
      btn("Переместить", () => this.moveNode(card));
      btn("Объединить с…", () => this.mergeNode(card));
      btn("Удалить", () => this.deleteNode(card), "btn-danger");
    }
    if (card.type === "segment" && (this.isAdmin || this.isInstaller)) {
      btn(this.isAdmin ? "Изменить" : "Уточнить длину", () => this.segmentForm(card));
      if (this.isAdmin) btn("Удалить", () => this.deleteSegment(card), "btn-danger");
    }
    if (card.type === "unclassified" && this.isAdmin) {
      btn("Назначить тип", () => this.classifyForm(card));
    }
    if (["node", "segment", "unclassified"].includes(card.type)) {
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
      this.pick.banner = banner;
    });
  }

  _finishPick(value) {
    const p = this.pick;
    this.pick = null;
    p.banner.remove();
    document.body.classList.remove("picking");
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
    const entity = { node: "network_node", segment: "route_segment", unclassified: "unclassified_feature" }[card.type];
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
