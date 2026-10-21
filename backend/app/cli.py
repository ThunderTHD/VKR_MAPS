"""Команды обслуживания системы.

Запуск внутри контейнера:  docker compose exec app python -m app.cli <команда>

  generate      сгенерировать тестовую сеть (удаляет текущие данные сети)
  create-user   создать пользователя
  demo-users    создать демонстрационных пользователей (admin, montazhnik, viewer)
  stats         показать, сколько объектов в базе
"""
import argparse
import sys

import sqlalchemy as sa

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import AppUser, Role
from app.services.generator import GeneratorParams, generate_network
from app.services.stats import collect_stats

DEMO_USERS = [
    ("admin", "Администратор", Role.admin),
    ("montazhnik", "Монтажник (демо)", Role.installer),
    ("viewer", "Просмотр (демо)", Role.viewer),
]
DEMO_PASSWORD = "Vols-2026"


def cmd_generate(args: argparse.Namespace) -> None:
    params = GeneratorParams(nodes=args.nodes, seed=args.seed)
    with SessionLocal() as db:
        report = generate_network(db, params, reset=True)
    print(f"Сеть сгенерирована (seed={args.seed}):")
    for key, value in sorted(report.counts.items()):
        print(f"  {key:<28} {value}")


def _create_user(db, login: str, full_name: str, role: Role, password: str) -> bool:
    if db.scalar(sa.select(AppUser).where(AppUser.login == login)):
        return False
    db.add(AppUser(login=login, full_name=full_name, role=role, password_hash=hash_password(password)))
    return True


def cmd_create_user(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        if not _create_user(db, args.login, args.name or args.login, Role(args.role), args.password):
            sys.exit(f"Пользователь {args.login} уже существует")
        db.commit()
    print(f"Создан пользователь {args.login} ({args.role})")


def cmd_demo_users(_: argparse.Namespace) -> None:
    with SessionLocal() as db:
        created = [login for login, name, role in DEMO_USERS if _create_user(db, login, name, role, DEMO_PASSWORD)]
        db.commit()
    if created:
        print(f"Созданы пользователи: {', '.join(created)}; пароль: {DEMO_PASSWORD}")
        print("Это демонстрационные учётные записи — на сервере компании смените пароли.")
    else:
        print("Демонстрационные пользователи уже есть")


def cmd_stats(_: argparse.Namespace) -> None:
    with SessionLocal() as db:
        for key, value in collect_stats(db).items():
            print(f"  {key:<24} {value}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Обслуживание ГИС ВОЛС")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="сгенерировать тестовую сеть (текущие данные сети удаляются)")
    g.add_argument("--nodes", type=int, default=300, help="примерное число точек сети (по умолчанию 300)")
    g.add_argument("--seed", type=int, default=42, help="зерно случайности: одинаковое зерно — одинаковая сеть")
    g.set_defaults(func=cmd_generate)

    u = sub.add_parser("create-user", help="создать пользователя")
    u.add_argument("login")
    u.add_argument("password")
    u.add_argument("--role", choices=[r.value for r in Role], default=Role.viewer.value)
    u.add_argument("--name")
    u.set_defaults(func=cmd_create_user)

    sub.add_parser("demo-users", help="создать демонстрационных пользователей").set_defaults(func=cmd_demo_users)
    sub.add_parser("stats", help="сколько объектов в базе").set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
