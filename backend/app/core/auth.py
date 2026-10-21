"""Авторизация: токен сессии в cookie и проверка ролей.

После входа сервер выдаёт подписанный токен (JWT) и кладёт его в cookie с флагом
HttpOnly: JavaScript страницы не может его прочитать, поэтому токен не украсть через
внедрённый скрипт. Браузер сам отправляет cookie с каждым запросом к API.

Защита от подделки запросов с чужих сайтов (CSRF): cookie выставляется с
SameSite=Lax, а изменяющие запросы (POST/PUT/PATCH/DELETE) дополнительно должны
нести заголовок X-Requested-With — чужой сайт не может добавить его в запрос без
разрешения CORS, которого мы не даём.
"""
from datetime import UTC, datetime, timedelta

import jwt
import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.models import AppUser, Role

COOKIE_NAME = "vols_session"
ALGORITHM = "HS256"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Иерархия ролей: у каждой роли есть права всех ролей ниже
ROLE_LEVEL = {Role.viewer: 1, Role.installer: 2, Role.admin: 3}


def create_token(user: AppUser) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {"sub": str(user.id), "role": user.role.value, "iat": now,
               "exp": now + timedelta(minutes=s.access_token_minutes)}
    return jwt.encode(payload, s.secret_key, algorithm=ALGORITHM)


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    s = get_settings()
    response.set_cookie(
        COOKIE_NAME, token, max_age=s.access_token_minutes * 60, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _unauthorized(detail: str = "Требуется вход в систему") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail)


def current_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    """Зависимость FastAPI: пользователь по cookie сессии, иначе 401."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise _unauthorized()
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Сессия истекла, войдите снова") from None
    except jwt.InvalidTokenError:
        raise _unauthorized() from None
    user = db.scalar(sa.select(AppUser).where(AppUser.id == int(payload["sub"])))
    if user is None or not user.active:
        raise _unauthorized()
    if request.method in UNSAFE_METHODS and request.headers.get("X-Requested-With") != "fetch":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Запрос отклонён: нет заголовка X-Requested-With")
    return user


def require_role(minimum: Role):
    """Зависимость: пользователь с ролью не ниже указанной, иначе 403."""

    def checker(user: AppUser = Depends(current_user)) -> AppUser:
        if ROLE_LEVEL[user.role] < ROLE_LEVEL[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
        return user

    return checker
