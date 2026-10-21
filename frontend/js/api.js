// Обращения к API. Cookie сессии браузер отправляет сам; на 401 — переход на страницу входа.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = "GET", body, params } = {}) {
  const url = new URL(path, location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, v);
    }
  }
  const headers = { "X-Requested-With": "fetch" }; // защита от CSRF (см. app/core/auth.py)
  const isForm = body instanceof FormData; // загрузка файла: заголовок с границей ставит браузер
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  const r = await fetch(url, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
  });
  if (r.status === 401 && !path.startsWith("/api/auth/login")) {
    location.href = "/login.html?next=" + encodeURIComponent(location.pathname + location.hash);
    throw new ApiError(401, "Требуется вход");
  }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    // FastAPI отдаёт ошибки проверки данных списком — показываем первую понятную строку
    const detail = Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join("; ") : data?.detail;
    throw new ApiError(r.status, detail || r.statusText);
  }
  return data;
}
