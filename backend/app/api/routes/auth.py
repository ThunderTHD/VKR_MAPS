"""Вход, выход, текущий пользователь."""
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import clear_session_cookie, create_token, current_user, set_session_cookie
from app.core.db import get_db
from app.core.security import verify_password
from app.models import AppUser, AuditLog

router = APIRouter(prefix="/auth", tags=["Авторизация"])


class LoginIn(BaseModel):
    login: str
    password: str


class UserOut(BaseModel):
    id: int
    login: str
    full_name: str
    role: str


def _user_out(u: AppUser) -> UserOut:
    return UserOut(id=u.id, login=u.login, full_name=u.full_name, role=u.role.value)


@router.post("/login", summary="Вход по логину и паролю")
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> UserOut:
    user = db.scalar(sa.select(AppUser).where(AppUser.login == data.login.strip()))
    # Одинаковое сообщение для «нет пользователя» и «неверный пароль» — не подсказываем, какие логины есть
    if user is None or not user.active or not verify_password(data.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")
    set_session_cookie(response, create_token(user), request)
    db.add(AuditLog(user_id=user.id, action="login", entity_type="app_user", entity_id=user.id))
    db.commit()
    return _user_out(user)


@router.post("/logout", summary="Выход")
def logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", summary="Текущий пользователь")
def me(user: AppUser = Depends(current_user)) -> UserOut:
    return _user_out(user)
