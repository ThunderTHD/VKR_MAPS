// Системы координат: добавление (файл .prj или текст), список, удаление.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 5000);
}

async function load() {
  const rows = await api("/api/crs");
  $("list").replaceChildren(...(rows.length ? rows.map((c) => {
    const li = document.createElement("li");
    const b = document.createElement("b");
    b.textContent = c.name;
    const meta = document.createElement("span");
    meta.className = "muted small";
    meta.textContent = ` · ${c.geographic ? "географическая" : "плоская (метры)"}${c.show_in_cards ? " · в карточках" : ""}`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-small btn-ghost";
    del.textContent = "Удалить";
    del.addEventListener("click", async () => {
      if (!confirm(`Удалить систему «${c.name}»?`)) return;
      await api(`/api/crs/${c.id}`, { method: "DELETE" });
      load();
    });
    li.append(b, meta, " ", del);
    return li;
  }) : [Object.assign(document.createElement("li"), { className: "muted", textContent: "Пока нет — сеть показывается только в WGS 84" })]));
}

$("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const fd = new FormData();
  if (f.name.value.trim()) fd.append("name", f.name.value.trim());
  if (f.prj.files[0]) fd.append("prj", f.prj.files[0]);
  else fd.append("definition", f.definition.value);
  fd.append("show_in_cards", f.show_in_cards.checked ? "true" : "false");
  $("err").hidden = true;
  try {
    const r = await api("/api/crs", { method: "POST", body: fd });
    toast(`Добавлена: ${r.name}`);
    f.reset();
    load();
  } catch (err) {
    $("err").textContent = err.message;
    $("err").hidden = false;
  }
});

api("/api/auth/me").then((me) => { if (me.role !== "admin") $("add").hidden = true; });
load().catch((e) => toast(e.message));
