"""Команды обслуживания системы.

Запуск внутри контейнера:  docker compose exec app python -m app.cli <команда>

  generate      сгенерировать тестовую сеть (удаляет текущие данные сети)
  create-user   создать пользователя
  demo-users    создать демонстрационных пользователей (admin, montazhnik, viewer)
  stats         показать, сколько объектов в базе
  import-kml    импорт KML/KMZ (по умолчанию — только отчёт; --commit — записать)
  clear-generated  удалить тестовую сеть генератора (импортированные данные остаются)
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


def cmd_import_kml(args: argparse.Namespace) -> None:
    from app.services.kml import jobs

    with SessionLocal() as db:
        for path in args.files:
            with open(path, "rb") as fh:
                job = jobs.create_job(db, path.replace("\\", "/").rsplit("/", 1)[-1], fh.read(), None)
            preview = jobs.build_preview(db, job, tolerance_m=args.tolerance)
            print(f"\n{path}")
            for key, value in preview["summary"].items():
                print(f"  {key:<24} {value}")
            for w in preview["warnings"]:
                if w["level"] != "info" or args.verbose:
                    print(f"  [{w['level']}] {w['message']}")
            if args.commit:
                created = jobs.commit_job(db, job, None)["created"]
                db.commit()
                print(f"  записано: {created}")
            else:
                db.rollback()
                print("  (пробный прогон — добавьте --commit, чтобы записать)")


def cmd_clear_generated(args: argparse.Namespace) -> None:
    from app.services.cleanup import clear_generated

    with SessionLocal() as db:
        result = clear_generated(db)
        db.commit()
    print("Удалено:")
    for label, n in result["deleted"].items():
        print(f"  {label:<24} {n}")
    if result["kept_nodes"] or result["kept_segments"]:
        print(f"Оставлено (к ним привязаны настоящие данные): точек {result['kept_nodes']}, "
              f"участков {result['kept_segments']}")


def cmd_localize_experiment(args: argparse.Namespace) -> None:
    """Точность локализации на сгенерированной сети: случайные повреждения, сравнение с истиной."""
    from app.services import experiment

    with SessionLocal() as db:
        res = experiment.run_experiment(db, n=args.n, seed=args.seed)
    print(f"Повреждений смоделировано: {res['trials']} на {res['paths']} трактах")
    print(f"Ошибка, м: средняя {res['error_mean_m']}, медиана {res['error_median_m']}, "
          f"90 % — не больше {res['error_p90_m']}, максимум {res['error_max_m']}")
    print(f"Средняя полуширина интервала: {res['half_width_mean_m']} м")
    print(experiment.sigma_check(res))


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

    sub.add_parser("clear-generated", help="удалить тестовую сеть генератора").set_defaults(func=cmd_clear_generated)

    e = sub.add_parser("localize-experiment", help="точность локализации на сгенерированной сети")
    e.add_argument("--n", type=int, default=300, help="сколько повреждений смоделировать")
    e.add_argument("--seed", type=int, default=1)
    e.set_defaults(func=cmd_localize_experiment)

    k = sub.add_parser("import-kml", help="импорт KML/KMZ")
    k.add_argument("files", nargs="+")
    k.add_argument("--commit", action="store_true", help="записать в базу (иначе только отчёт)")
    k.add_argument("--tolerance", type=float, default=10.0, help="допуск привязки концов, м")
    k.add_argument("-v", "--verbose", action="store_true", help="показывать и информационные сообщения")
    k.set_defaults(func=cmd_import_kml)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
