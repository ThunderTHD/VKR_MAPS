// Этап 1: запрашиваем /api/health и показываем, что API, база и PostGIS работают.
const box = document.getElementById("status");

const LABELS = {
  status: "Состояние",
  version: "Версия",
  database: "База данных",
  postgis: "PostGIS",
  monitoring_mode: "Мониторинг узлов",
  error: "Ошибка",
};

async function load() {
  try {
    const r = await fetch("/api/health");
    const data = await r.json();
    box.replaceChildren(
      ...Object.entries(LABELS)
        .filter(([key]) => key in data)
        .flatMap(([key, label]) => {
          const dt = document.createElement("dt");
          dt.textContent = label;
          const dd = document.createElement("dd");
          dd.textContent = data[key];
          if (key === "status") dd.className = data[key] === "ok" ? "ok" : "bad";
          return [dt, dd];
        }),
    );
  } catch (e) {
    box.innerHTML = "<dt>Состояние</dt><dd class='bad'>API недоступно</dd>";
  }
}

load();
