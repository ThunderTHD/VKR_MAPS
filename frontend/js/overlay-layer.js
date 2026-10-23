// Подложка на карте: картинка, привязанная тремя углами (верх-лево, верх-право, низ-лево).
// Leaflet умеет показывать картинку только «прямоугольником по сторонам света»; повёрнутый и
// перекошенный скан кладём сами — CSS-матрицей, пересчитываемой при каждом сдвиге и масштабе карты.
import { api } from "./api.js";

export class SkewedImage extends L.Layer {
  constructor(url, corners, { opacity = 0.6, size } = {}) {
    super();
    this.url = url;
    this.corners = corners;     // {tl:[lon,lat], tr:[...], bl:[...]}
    this.opacity = opacity;
    this.size = size;           // [ширина, высота] картинки в пикселях
  }

  onAdd(map) {
    this.img = L.DomUtil.create("img", "overlay-img leaflet-zoom-hide");
    this.img.src = this.url;
    this.img.alt = "";
    this.img.style.opacity = this.opacity;
    this.img.addEventListener("load", () => this._reset());
    map.getPanes().overlayPane.appendChild(this.img);
    map.on("zoomend viewreset moveend resize", this._reset, this);
    this._reset();
  }

  onRemove(map) {
    map.off("zoomend viewreset moveend resize", this._reset, this);
    this.img.remove();
  }

  setOpacity(v) {
    this.opacity = v;
    if (this.img) this.img.style.opacity = v;
  }

  _reset() {
    if (!this._map || !this.img) return;
    const [w, h] = this.size || [this.img.naturalWidth, this.img.naturalHeight];
    if (!w || !h) return;
    const p = (c) => this._map.latLngToLayerPoint([c[1], c[0]]);
    const tl = p(this.corners.tl), tr = p(this.corners.tr), bl = p(this.corners.bl);
    const a = (tr.x - tl.x) / w, b = (tr.y - tl.y) / w, c = (bl.x - tl.x) / h, d = (bl.y - tl.y) / h;
    this.img.style.width = `${w}px`;
    this.img.style.height = `${h}px`;
    this.img.style.transformOrigin = "0 0";
    this.img.style.transform = `matrix(${a}, ${b}, ${c}, ${d}, ${tl.x}, ${tl.y})`;
  }
}

/** Подложки в панели слоёв: переключатель и прозрачность у каждой привязанной. */
export async function setupOverlays(map, box, { canEdit, toast }) {
  let list;
  try {
    list = await api("/api/overlays");
  } catch {
    return;
  }
  const ready = list.filter((o) => o.corners);
  box.replaceChildren();
  if (!ready.length && !canEdit) return;
  const head = document.createElement("h3");
  head.textContent = "Подложки (планы, схемы)";
  box.append(head);
  for (const o of ready) {
    const layer = new SkewedImage(o.url, o.corners, { opacity: o.opacity, size: o.size });
    if (o.visible) layer.addTo(map);
    const row = document.createElement("div");
    row.className = "overlay-row";
    const label = document.createElement("label");
    label.className = "toggle";
    label.innerHTML = '<input type="checkbox"> <span></span>';
    label.querySelector("span").textContent = o.name;
    const cb = label.querySelector("input");
    cb.checked = o.visible;
    cb.addEventListener("change", () => {
      if (cb.checked) layer.addTo(map); else layer.remove();
      if (canEdit) api(`/api/overlays/${o.id}`, { method: "PATCH", body: { visible: cb.checked } }).catch((e) => toast(e.message));
    });
    const range = document.createElement("input");
    range.type = "range";
    range.min = "0.1";
    range.max = "1";
    range.step = "0.05";
    range.value = o.opacity;
    range.title = "Прозрачность";
    range.setAttribute("aria-label", `Прозрачность подложки «${o.name}»`);
    range.addEventListener("input", () => layer.setOpacity(Number(range.value)));
    range.addEventListener("change", () => {
      if (canEdit) api(`/api/overlays/${o.id}`, { method: "PATCH", body: { opacity: Number(range.value) } }).catch(() => {});
    });
    row.append(label, range);
    box.append(row);
  }
  if (canEdit) {
    const a = document.createElement("a");
    a.href = "/overlays.html";
    a.className = "small";
    a.textContent = ready.length ? "Управление подложками…" : "Загрузить и привязать план…";
    box.append(a);
  }
}
