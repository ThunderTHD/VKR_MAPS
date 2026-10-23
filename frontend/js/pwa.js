// Установка как приложение (PWA) и работа без связи: регистрация service worker (sw.js),
// плашка «нет связи» и пункт меню «Установить приложение».

if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost"
    || location.hostname === "127.0.0.1")) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("service worker:", e));
  });
}

function offlineBadge() {
  let el = document.getElementById("offline-badge");
  if (!el) {
    el = document.createElement("div");
    el.id = "offline-badge";
    el.className = "offline-badge";
    el.setAttribute("role", "status");
    el.textContent = "Нет связи — показаны данные последней загрузки, изменения сохранить нельзя";
    document.body.append(el);
  }
  el.hidden = navigator.onLine;
}
window.addEventListener("online", offlineBadge);
window.addEventListener("offline", offlineBadge);
document.addEventListener("DOMContentLoaded", offlineBadge);
if (document.readyState !== "loading") offlineBadge();

// Кнопка установки: браузер присылает событие, когда приложение можно установить
let deferred = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferred = e;
  const btn = document.getElementById("menu-install");
  if (btn) btn.hidden = false;
});
window.addEventListener("appinstalled", () => {
  deferred = null;
  const btn = document.getElementById("menu-install");
  if (btn) btn.hidden = true;
});
document.addEventListener("click", async (e) => {
  if (!e.target.closest?.("#menu-install") || !deferred) return;
  deferred.prompt();
  await deferred.userChoice;
  deferred = null;
  e.target.closest("#menu-install").hidden = true;
});
