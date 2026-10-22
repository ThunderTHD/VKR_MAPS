// Анализ последствий («что отключится»), линейный тракт узла доступа, имитация аварий из карточки.
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

export const STATUS_RU = { up: "доступен", down: "недоступен", unknown: "нет данных" };
const plural = (n, one, few, many) => {
  const a = n % 10, b = n % 100;
  return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 10 || b >= 20) ? few : many;
};

export class ImpactTool {
  /** @param ctx — { map, toast, user, openObject(type, id), reopen(type, id), reload() } */
  constructor(ctx) {
    this.ctx = ctx;
    this.layer = L.layerGroup().addTo(ctx.map);
    this.mode = null;
    api("/api/health").then((h) => { this.mode = h.monitoring_mode; }).catch(() => {});
  }

  get simulate() { return this.mode === "simulate" && this.ctx.user.role === "admin"; }

  clear() { this.layer.clearLayers(); }

  /** Кнопки для карточки (добавляются в блок действий редактора). */
  buttons(card, btn) {
    if (["segment", "cable", "closure", "node", "access_node"].includes(card.type)) {
      btn("Что отключится", () => this.show(card));
    }
    if (card.type === "access_node") btn("Линейный тракт", () => this.path(card));
    if (card.type === "segment" && this.simulate) btn("Имитировать обрыв", () => this.sim(card, { segment_id: card.id }));
    if (card.type === "access_node" && this.simulate) {
      btn("Имитировать отключение", () => this.sim(card, { access_node_id: card.id }));
    }
  }

  async show(card) {
    const body = document.getElementById("card-body");
    body.replaceChildren(el("p", { class: "muted" }, "Считаю…"));
    let r;
    try {
      r = await api(`/api/impact/${card.type}/${card.id}`);
    } catch (e) {
      body.replaceChildren(el("p", { class: "form-error" }, e.message), this._back(card));
      return;
    }
    const title = r.count
      ? `Потеряют связь ${r.count} ${plural(r.count, "узел", "узла", "узлов")} доступа`
      : "Узлы доступа не пострадают";
    const parts = [el("h3", {}, title)];
    if (r.by_operator.length > 1) {
      parts.push(el("p", { class: "chips" }, r.by_operator.map((o) => el("span", { class: "chip" }, `${o.operator}: ${o.count}`))));
    }
    if (r.count) {
      parts.push(el("ul", { class: "impact-list" }, r.access_nodes.map((a) => el("li", {}, this._item(a)))));
      parts.push(el("button", { type: "button", class: "btn btn-small", onclick: () => this.mark(r.access_nodes) },
        "Показать на карте"));
    }
    if (r.paths_incomplete) {
      parts.push(el("p", { class: "muted small" },
        `Внимание: у ${r.paths_incomplete} из ${r.paths_total} узлов тракт в системе не доведён до кросса — ` +
        "для них результат может быть неполным."));
    }
    parts.push(this._back(card));
    body.replaceChildren(el("div", { class: "edit-form" }, ...parts));
    this.mark(r.access_nodes, false);
  }

  _item(a) {
    const meta = [a.operator, a.ip].filter(Boolean).join(" · ");
    return el("button", { type: "button", class: "near-item impact-item",
      onclick: () => this.ctx.openObject("access_node", a.id) },
      el("span", { class: `dot st-${a.status}`, title: STATUS_RU[a.status] }),
      el("span", {}, el("b", {}, a.hostname || `#${a.id}`), meta ? el("span", { class: "muted small" }, ` · ${meta}`) : null,
        el("div", { class: "muted small" }, [a.address || a.node, a.via].filter(Boolean).join(" — "))));
  }

  /** Авария из мониторинга: недоступные узлы и вероятное место обрыва (адрес #alarm=1). */
  async showAlarm() {
    const [mon, cor] = await Promise.all([api("/api/monitoring"), api("/api/monitoring/correlate")]);
    const down = mon.nodes.filter((n) => n.status === "down");
    if (!down.length) {
      this.ctx.toast("Сейчас все узлы доступа на связи");
      return;
    }
    const best = cor.candidates[0];
    if (best) this.highlight(best.geojson);
    for (const n of down.filter((x) => x.lonlat)) {
      L.circleMarker([n.lonlat[1], n.lonlat[0]], { radius: 11, color: "#dc2626", weight: 3, fillOpacity: 0.15,
        interactive: false }).addTo(this.layer);
    }
    const otdr = best?.otdr;
    this.ctx.toast(`Недоступно узлов: ${down.length}.` + (best ? ` Вероятный обрыв: ${best.segments.map((s) => s.label).join(", ")}` : "") +
      (otdr ? `. На рефлектометре с «${otdr.from_port}»: ${otdr.min_m}–${otdr.max_m} м` : ""));
  }

  mark(items, fit = true) {
    this.clear();
    const pts = items.filter((a) => a.lonlat).map((a) => [a.lonlat[1], a.lonlat[0]]);
    for (const p of pts) {
      L.circleMarker(p, { radius: 11, color: "#dc2626", weight: 3, fillOpacity: 0.15, interactive: false })
        .addTo(this.layer);
    }
    if (fit && pts.length) this.ctx.map.fitBounds(pts, { padding: [40, 40], maxZoom: 17 });
  }

  async path(card) {
    try {
      const p = await api(`/api/access-nodes/${card.id}/path`);
      this.clear();
      const g = L.geoJSON(p.geojson, { style: { color: "#7c3aed", weight: 7, opacity: 0.6 }, interactive: false })
        .addTo(this.layer);
      if (g.getBounds().isValid()) this.ctx.map.fitBounds(g.getBounds(), { padding: [40, 40] });
      this.ctx.toast(`Тракт: ${p.segments.length} участков, муфт: ${p.closures.length}, ` +
        `оптическая длина ${Math.round(p.total_opt_m)} м` + (p.far_port ? ` → ${p.far_port}` : " (не доведён до кросса)"));
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  async sim(card, body) {
    try {
      const r = await api("/api/monitoring/simulate", { method: "POST", body });
      const n = r.poll.down;
      this.ctx.toast(`Имитация: недоступно узлов — ${n}. Подробности — меню → Мониторинг`);
      await this.ctx.reload();
      const best = r.correlation.candidates[0];
      if (best) this.highlight(best.geojson);
    } catch (e) {
      this.ctx.toast(e.message);
    }
  }

  highlight(geojson) {
    this.clear();
    const g = L.geoJSON(geojson, { style: { color: "#dc2626", weight: 9, opacity: 0.55, dashArray: "10 8" },
      interactive: false }).addTo(this.layer);
    if (g.getBounds().isValid()) this.ctx.map.fitBounds(g.getBounds(), { padding: [60, 60], maxZoom: 18 });
  }

  _back(card) {
    return el("button", { type: "button", class: "btn btn-small btn-ghost",
      onclick: () => { this.clear(); this.ctx.reopen(card.type, card.id); } }, "Назад к карточке");
  }
}
