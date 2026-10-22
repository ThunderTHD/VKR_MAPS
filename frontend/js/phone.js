// Страница «Вход с телефона»: QR-код с HTTPS-адресом и подсказки по сертификату.
import qrcode from "../vendor/qrcode/qrcode.js";

const box = document.getElementById("phone-body");

function qrSvg(text) {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  return qr.createSvgTag({ cellSize: 6, margin: 3, scalable: true, alt: text });
}

function step(html) {
  const li = document.createElement("li");
  li.innerHTML = html;
  return li;
}

async function main() {
  const info = await fetch("/api/phone/info").then((r) => r.json());
  if (!info.url) {
    box.innerHTML = `<p class="form-error">Адрес для телефона ещё не настроен.</p>
      <p>На ноутбуке запустите <b>phone.bat</b> (рядом со start.bat): он найдёт адрес ноутбука в Wi-Fi,
      откроет порт ${info.https_port} и перезапустит систему с HTTPS. На сервере — укажите PHONE_HOST в файле .env.</p>`;
    return;
  }
  const qr = document.createElement("div");
  qr.className = "phone-qr";
  qr.innerHTML = qrSvg(info.url);
  const url = document.createElement("p");
  url.className = "phone-url";
  url.textContent = info.url;
  const ol = document.createElement("ol");
  ol.className = "phone-steps";
  ol.append(
    step("Подключите телефон к <b>той же Wi-Fi-сети</b>, что и ноутбук."),
    step("Наведите камеру телефона на QR-код и откройте ссылку."),
    step("Браузер предупредит, что сертификат не проверен — это нормально для локальной сети: " +
      "нажмите «Дополнительно» → «Перейти на сайт». Чтобы предупреждения не было, установите на телефон " +
      `<a href="${info.url}api/phone/root.crt">корневой сертификат системы</a>` +
      " (Android: Настройки → Безопасность → Шифрование и учётные данные → Установить сертификат → Сертификат ЦС; " +
      "iPhone: установите профиль, затем Основные → Об этом устройстве → Доверие сертификатам)."),
    step("Войдите под своей учётной записью монтажника и разрешите доступ к местоположению и камере."),
  );
  box.replaceChildren(qr, url, ol);
}

main().catch((e) => { box.textContent = `Ошибка: ${e.message}`; });
