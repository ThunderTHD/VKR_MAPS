// Точки сети по фото: загрузка пачки, координаты съёмки, привязка к точкам или новые точки.
import { api } from "./api.js";

const $ = (id) => document.getElementById(id);

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

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 6000);
}

let rows = [];   // { item, file, select, code }

function actionSelect(item) {
  const s = el("select", {});
  if (item.nearest) {
    s.append(el("option", { value: "attach" }, `прикрепить к ${item.nearest.label}`));
  }
  if (item.coords) s.append(el("option", { value: "create" }, "новая точка здесь"));
  s.append(el("option", { value: "skip" }, "пропустить"));
  s.value = item.suggest;
  return s;
}

function render() {
  $("result").hidden = !rows.length;
  $("rows").replaceChildren(...rows.map((r) => {
    const { item } = r;
    const c = item.coords;
    const where = c
      ? el("div", {}, `${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}`,
        el("div", { class: "muted small" }, c.source === "exif" ? "из EXIF" : "по подписи на кадре"),
        item.taken_at ? el("div", { class: "muted small" }, new Date(item.taken_at).toLocaleString("ru-RU")) : null)
      : el("span", { class: "form-error" }, "координаты не найдены");
    const near = item.nearest
      ? el("div", {}, el("a", { href: `/#obj=node:${item.nearest.id}`, target: "_blank" }, item.nearest.label),
        el("div", { class: "muted small" }, `${item.nearest.kind_ru}, ${Math.round(item.nearest.distance_m)} м`))
      : el("span", { class: "muted" }, c ? "в 200 м точек нет" : "—");
    const toggleCode = () => { r.code.hidden = r.select.value !== "create"; };
    r.select.addEventListener("change", toggleCode);
    toggleCode();
    return el("tr", {}, el("td", {}, el("img", { src: URL.createObjectURL(r.file), alt: item.filename }),
      el("div", { class: "muted small" }, item.filename)), el("td", {}, where), el("td", {}, near),
    el("td", {}, r.select, r.code));
  }));
}

async function analyze(files) {
  if (!files.length) return;
  $("progress").textContent = `Загружаю ${files.length} фото и ищу координаты… (распознавание подписи может занять до минуты)`;
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  try {
    const items = await api("/api/photos/analyze", { method: "POST", body: fd });
    rows = items.map((item, i) => ({ item, file: files[i], select: actionSelect(item),
      code: el("input", { type: "text", placeholder: "номер (необязательно)", class: "small" }) }));
    const found = items.filter((i) => i.coords).length;
    $("progress").textContent = `Координаты найдены у ${found} из ${items.length} фото`;
    render();
  } catch (e) {
    $("progress").textContent = "";
    toast(e.message);
  }
}

async function commit() {
  const kind = $("kind-all").value;
  const items = rows.map((r) => ({ doc_id: r.item.doc_id, action: r.select.value,
    node_id: r.select.value === "attach" ? r.item.nearest?.id : null,
    kind: r.select.value === "create" ? kind : null, code: r.code.value || null }));
  try {
    const res = await api("/api/photos/commit", { method: "POST", body: { items } });
    rows = [];
    render();
    $("progress").replaceChildren(`Готово: прикреплено фото — ${res.attached}, новых точек — ${res.created}, ` +
      `пропущено — ${res.skipped}. `, res.nodes.length ? el("a", { href: `/#obj=node:${res.nodes[0]}` }, "Показать на карте") : "");
  } catch (e) {
    toast(e.message);
  }
}

$("files").addEventListener("change", (e) => analyze([...e.target.files]));
$("btn-commit").addEventListener("click", commit);
api("/api/auth/me").then((me) => {
  if (me.role === "viewer") {
    $("files").disabled = true;
    toast("Загружать фото могут монтажник и администратор");
  }
}).catch(() => {});
