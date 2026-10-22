// Слои карты: как рисуется каждый тип объекта.
import { GridCluster, latLng } from "./cluster.js";

// Порядок отрисовки (снизу вверх) задаётся панелями Leaflet
const PANES = { segments: 410, cables: 420, nodes: 430, closures: 440, access: 450, incidents: 455, highlight: 460 };

export const LAYER_INFO = {
  segments: { title: "Трассы (канализация, опоры)", on: true },
  cables: { title: "Кабели", on: true },
  nodes: { title: "Колодцы, опоры, здания", on: true },
  closures: { title: "Муфты", on: true },
  access_nodes: { title: "Узлы доступа", on: true },
  jumpers: { title: "Перемычки", on: true },
  unclassified: { title: "Неклассифицированные", on: false },
  incidents: { title: "Повреждения", on: true },
};

const NODE_STYLE = {
  well: { radius: 4, color: "#33415c", fillColor: "#ffffff", weight: 2 },
  kks: { radius: 5, color: "#33415c", fillColor: "#dfe7f5", weight: 2 },
  pole: { radius: 3.5, color: "#5c4b33", fillColor: "#c9b18a", weight: 1.5 },
  site: { radius: 8, color: "#14213d", fillColor: "#fca311", weight: 3 },
  building: { radius: 5, color: "#4a4e69", fillColor: "#e9ecef", weight: 2 },
  closure_point: { radius: 5, color: "#33415c", fillColor: "#ffd166", weight: 2 },
  unknown: { radius: 5, color: "#d00000", fillColor: "#ffba08", weight: 2, dashArray: "2 2" },
};

const STATUS_COLOR = { up: "#2a9d8f", down: "#e63946", unknown: "#8d99ae" };

export class MapLayers {
  constructor(map, ref, onSelect) {
    this.map = map;
    this.ref = ref;
    this.onSelect = onSelect; // (type, id) => void
    this.ownerColor = Object.fromEntries(ref.owners.map((o) => [o.id, o.color]));
    this.operatorColor = Object.fromEntries(ref.operators.map((o) => [o.id, o.color]));
    for (const [name, z] of Object.entries(PANES)) {
      map.createPane(name).style.zIndex = z;
    }
    // Canvas быстрее SVG для тысяч объектов. У каждой панели свой холст, чтобы сохранить порядок слоёв.
    const canvas = (pane) => L.canvas({ pane, padding: 0.5, tolerance: 6 });
    this.renderers = { segments: canvas("segments"), cables: canvas("cables"), nodes: canvas("nodes") };
    this.layers = {};
    this.data = {};
    this.enabled = Object.fromEntries(Object.entries(LAYER_INFO).map(([k, v]) => [k, v.on]));
    this.highlight = L.layerGroup().addTo(map);

    this.layers.segments = L.geoJSON(null, {
      pane: "segments",
      renderer: this.renderers.segments,
      style: (f) => this.segmentStyle(f),
      onEachFeature: (f, l) => this.bind(l, "segment", f, segmentTooltip(f, this.ref)),
    });
    this.layers.cables = L.geoJSON(null, {
      pane: "cables",
      renderer: this.renderers.cables,
      style: (f) => ({ color: this.operatorColor[f.properties.operator_id] || "#333", weight: 2.5, opacity: 0.95 }),
      onEachFeature: (f, l) => this.bind(l, "cable", f, `${f.properties.name}<br>${f.properties.type}`),
    });
    this.layers.nodes = new GridCluster(map, { makeMarker: (f) => this.nodeMarker(f) });
    this.layers.closures = L.geoJSON(null, {
      pane: "closures",
      pointToLayer: (f, ll) => L.marker(ll, {
        pane: "closures",
        icon: L.divIcon({
          className: "closure-icon",
          html: `<span style="border-color:${this.operatorColor[f.properties.operator_id] || "#14213d"}">М</span>`,
          iconSize: [22, 22],
        }),
      }),
      onEachFeature: (f, l) => this.bind(l, "closure", f,
        `Муфта ${f.properties.number}<br>${f.properties.node_code || ""} · сварок: ${f.properties.splices}`),
    });
    this.layers.access_nodes = L.geoJSON(null, {
      pane: "access",
      pointToLayer: (f, ll) => L.marker(ll, {
        pane: "access",
        icon: L.divIcon({
          className: `access-icon status-${f.properties.status}`,
          html: `<span style="background:${STATUS_COLOR[f.properties.status]}"></span>`,
          iconSize: [14, 14],
          iconAnchor: [-2, 16], // чуть правее и выше здания, чтобы не закрывать его
        }),
      }),
      onEachFeature: (f, l) => this.bind(l, "access_node", f,
        `${f.properties.hostname}<br>${f.properties.ip || ""}`),
    });
    this.layers.jumpers = L.geoJSON(null, {
      pane: "cables",
      style: { color: "#7209b7", weight: 2, dashArray: "6 4" },
      onEachFeature: (f, l) => l.bindTooltip(f.properties.label || "Перемычка", { sticky: true }),
    });
    // Повреждения: прогноз (красный) и, если устранено, фактическое место (зелёный) с линией между ними
    this.layers.incidents = L.geoJSON(null, {
      pane: "incidents",
      pointToLayer: (f, ll) => {
        const p = f.properties;
        const group = L.featureGroup();
        const icon = L.divIcon({ className: `incident-icon st-${p.status}`, html: "<span>!</span>", iconSize: [22, 22] });
        const m = L.marker(ll, { pane: "incidents", icon }).addTo(group);
        if (p.actual) {
          const a = [p.actual.coordinates[1], p.actual.coordinates[0]];
          L.polyline([ll, a], { pane: "incidents", color: "#2a9d8f", weight: 2, dashArray: "4 4", interactive: false })
            .addTo(group);
          L.circleMarker(a, { pane: "incidents", radius: 6, color: "#fff", weight: 2, fillColor: "#2a9d8f",
            fillOpacity: 1 }).addTo(group);
        }
        m.feature = f;
        return group;
      },
      onEachFeature: (f, l) => this.bind(l, "incident", f, `${f.properties.title || "Повреждение"}` +
        (f.properties.error_m != null ? `<br>ошибка ${Math.round(f.properties.error_m)} м` : "") +
        (f.properties.half_width_m ? `<br>прогноз ± ${Math.round(f.properties.half_width_m)} м` : "")),
    });
    this.layers.unclassified = L.geoJSON(null, {
      pane: "segments",
      renderer: this.renderers.segments,
      style: { color: "#fb8500", weight: 2, dashArray: "3 5" },
      pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 4, color: "#fb8500", renderer: this.renderers.segments }),
      onEachFeature: (f, l) => this.bind(l, "unclassified", f,
        `${f.properties.name || "Без названия"}<br><small>${f.properties.folder || ""}</small>`),
    });
  }

  segmentStyle(f) {
    const p = f.properties;
    const aerial = ["aerial", "facade"].includes(p.kind);
    return {
      color: this.ownerColor[p.owner_id] || "#adb5bd",
      weight: p.cables ? 6 : 4,
      opacity: p.cables ? 0.45 : 0.3,
      dashArray: aerial ? "8 6" : null,
      lineCap: "round",
    };
  }

  nodeMarker(f) {
    const style = NODE_STYLE[f.properties.kind] || NODE_STYLE.unknown;
    const m = L.circleMarker(latLng(f), { ...style, fillOpacity: 1, pane: "nodes", renderer: this.renderers.nodes });
    const p = f.properties;
    this.bind(m, "node", f, [p.code, p.name, p.address].filter(Boolean).join("<br>") || this.ref.node_kinds[p.kind]);
    return m;
  }

  bind(layer, type, feature, tooltip) {
    if (tooltip) {
      layer.bindTooltip(tooltip, { sticky: ["segment", "cable", "unclassified"].includes(type), direction: "top" });
    }
    layer.on("click", (e) => {
      L.DomEvent.stopPropagation(e);
      this.onSelect(type, feature.properties.id);
    });
  }

  setData(name, geojson) {
    this.data[name] = geojson;
    const layer = this.layers[name];
    if (layer instanceof GridCluster) {
      layer.setFeatures(geojson.features);
    } else {
      layer.clearLayers();
      layer.addData(geojson);
    }
  }

  setEnabled(name, on) {
    this.enabled[name] = on;
    const layer = this.layers[name];
    if (on) {
      layer instanceof GridCluster ? layer.addTo() : layer.addTo(this.map);
    } else {
      layer.remove();
    }
  }

  applyEnabled() {
    for (const [name, on] of Object.entries(this.enabled)) this.setEnabled(name, on);
  }

  /** Подсветить геометрию объекта из загруженных слоёв; вернуть её границы. */
  highlightObject(type, id) {
    this.highlight.clearLayers();
    const source = { segment: "segments", cable: "cables", node: "nodes", closure: "closures",
      access_node: "access_nodes", unclassified: "unclassified", incident: "incidents" }[type];
    const feature = source && this.data[source]?.features.find((f) => f.properties.id === id);
    if (!feature) return null;
    const shape = L.geoJSON(feature, {
      pane: "highlight",
      style: { color: "#ffd60a", weight: 9, opacity: 0.85 },
      pointToLayer: (f, ll) => L.circleMarker(ll, { pane: "highlight", radius: 13, color: "#ffd60a", weight: 4,
        fill: false }),
      interactive: false,
    }).addTo(this.highlight);
    return shape.getBounds();
  }

  clearHighlight() {
    this.highlight.clearLayers();
  }
}

function segmentTooltip(f, ref) {
  const p = f.properties;
  const kind = ref.segment_kinds[p.kind] || p.kind;
  const len = p.fact_length_m ? `${Math.round(p.fact_length_m)} м` : `≈${Math.round(p.geom_length_m)} м по карте`;
  return `${kind}${p.name ? `<br>${p.name}` : ""}<br>${len} · кабелей: ${p.cables}`;
}
