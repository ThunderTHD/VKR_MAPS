// Сеточная кластеризация точек для мелкого масштаба.
//
// Идея: на текущем масштабе переводим каждую точку в экранные пиксели и раскладываем по
// квадратным ячейкам заданного размера. Ячейка с одной точкой рисуется обычным маркером,
// с несколькими — кружком с числом; щелчок по кружку приближает карту к его точкам.
// Сложность O(n) на перерисовку — для десятков тысяч точек сети этого достаточно.

export class GridCluster {
  /**
   * @param {L.Map} map
   * @param {object} opts
   *   makeMarker(feature) → L.Layer — маркер одиночной точки
   *   minZoomPlain — с какого масштаба кластеризация выключается
   *   cellPx — размер ячейки в пикселях
   */
  constructor(map, { makeMarker, minZoomPlain = 16, cellPx = 56 }) {
    this.map = map;
    this.makeMarker = makeMarker;
    this.minZoomPlain = minZoomPlain;
    this.cellPx = cellPx;
    this.group = L.layerGroup();
    this.features = [];
    this.visible = false;
    this._redraw = () => this.redraw();
  }

  setFeatures(features) {
    this.features = features;
    if (this.visible) this.redraw();
  }

  addTo() {
    this.visible = true;
    this.group.addTo(this.map);
    this.map.on("zoomend moveend", this._redraw);
    this.redraw();
    return this;
  }

  remove() {
    this.visible = false;
    this.map.off("zoomend moveend", this._redraw);
    this.group.remove();
  }

  redraw() {
    this.group.clearLayers();
    const zoom = this.map.getZoom();
    const bounds = this.map.getBounds().pad(0.3);
    const inView = this.features.filter((f) => bounds.contains(latLng(f)));

    if (zoom >= this.minZoomPlain) {
      inView.forEach((f) => this.group.addLayer(this.makeMarker(f)));
      return;
    }
    const cells = new Map();
    for (const f of inView) {
      const p = this.map.project(latLng(f), zoom);
      const key = `${Math.floor(p.x / this.cellPx)}:${Math.floor(p.y / this.cellPx)}`;
      if (!cells.has(key)) cells.set(key, []);
      cells.get(key).push(f);
    }
    for (const items of cells.values()) {
      if (items.length === 1) {
        this.group.addLayer(this.makeMarker(items[0]));
        continue;
      }
      const lls = items.map(latLng);
      const center = L.latLngBounds(lls).getCenter();
      const size = items.length < 10 ? 30 : items.length < 100 ? 36 : 44;
      const marker = L.marker(center, {
        icon: L.divIcon({
          className: "cluster",
          html: `<span>${items.length}</span>`,
          iconSize: [size, size],
        }),
        keyboard: false,
        title: `${items.length} объектов — приблизить`,
      });
      marker.on("click", () => this.map.fitBounds(L.latLngBounds(lls), { padding: [40, 40], maxZoom: 18 }));
      this.group.addLayer(marker);
    }
  }
}

export function latLng(feature) {
  const [lon, lat] = feature.geometry.coordinates;
  return L.latLng(lat, lon);
}
