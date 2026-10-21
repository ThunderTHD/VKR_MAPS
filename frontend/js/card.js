// Карточка объекта: поля, связанные объекты (ссылки), таблицы (рассветка, порты).

const TYPE_RU = {
  node: "Узел", segment: "Участок трассы", cable: "Кабель", closure: "Муфта", cross: "Кросс",
  access_node: "Узел доступа",
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/**
 * @param {HTMLElement} root — элемент #card
 * @param {object} data — ответ /api/objects/{type}/{id}
 * @param {(type, id) => void} onOpen — открыть связанный объект
 */
export function renderCard(root, data, onOpen) {
  root.querySelector("#card-title").textContent = data.title;
  root.querySelector("#card-subtitle").textContent = data.subtitle || TYPE_RU[data.type] || "";
  const body = root.querySelector("#card-body");
  body.replaceChildren();

  const fields = data.fields.filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (fields.length) {
    body.append(el("dl", { class: "fields" }, fields.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])));
  }

  for (const section of data.sections || []) {
    if (!section.items.length) continue;
    body.append(
      el("h3", {}, section.title),
      el("ul", { class: "links" }, section.items.map((it) =>
        el("li", {}, el("button", { type: "button", class: "link", onclick: () => onOpen(it.type, it.id) },
          el("span", { class: `badge badge-${it.type}` }, TYPE_RU[it.type] || it.type), it.label)))),
    );
  }

  // Таблица (рассветка, порты) — последней: она длинная, ссылки должны быть видны сразу
  if (data.table && data.table.rows.length) {
    body.append(
      el("h3", {}, `${data.table.title} (${data.table.rows.length})`),
      el("div", { class: "table-scroll" },
        el("table", { class: "data-table" },
          el("thead", {}, el("tr", {}, data.table.columns.map((c) => el("th", {}, c)))),
          el("tbody", {}, data.table.rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, c ?? "—"))))))),
    );
  }
  root.hidden = false;
}
