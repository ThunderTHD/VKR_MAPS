// Служебная страница: состояние API/базы и сводка по объектам сети.
import { api } from "./api.js";

const LABELS = {
  status: "Состояние",
  version: "Версия",
  database: "База данных",
  postgis: "PostGIS",
  monitoring_mode: "Мониторинг узлов",
  error: "Ошибка",
};

function fill(box, pairs) {
  box.replaceChildren(
    ...pairs.flatMap(([label, value, cls]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      if (cls) dd.className = cls;
      return [dt, dd];
    }),
  );
}

async function loadHealth() {
  const box = document.getElementById("status");
  try {
    const data = await (await fetch("/api/health")).json();
    fill(box, Object.entries(LABELS)
      .filter(([key]) => key in data)
      .map(([key, label]) => [label, data[key], key === "status" ? (data[key] === "ok" ? "ok" : "bad") : ""]));
  } catch {
    fill(box, [["Состояние", "API недоступно", "bad"]]);
  }
}

async function loadStats() {
  const box = document.getElementById("stats");
  try {
    const data = await api("/api/stats");
    const fmt = new Intl.NumberFormat("ru-RU");
    fill(box, Object.entries(data).map(([label, value]) => [label, fmt.format(value)]));
  } catch {
    fill(box, [["Сводка", "недоступна", "bad"]]);
  }
}

loadHealth();
loadStats();
