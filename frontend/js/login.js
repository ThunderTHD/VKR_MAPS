import { api } from "./api.js";

const form = document.getElementById("login");
const error = document.getElementById("error");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  error.hidden = true;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: { login: form.login.value, password: form.password.value },
    });
    const next = new URLSearchParams(location.search).get("next");
    // Переходим только внутри своего сайта (защита от подмены адреса в ссылке)
    location.href = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
    button.disabled = false;
  }
});
