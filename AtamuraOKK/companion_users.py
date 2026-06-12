"""Companion cabinet user management: ``python -m AtamuraOKK.companion_users``.

Commands:
  create  issue a personal access key (printed ONCE — only its hash is stored)
  list    show all cabinet users
  revoke  deactivate a user's key (or --activate to restore it)

A ``manager`` user must be linked to a Bitrix user id — that link is what the
read API scopes their data to. A ``head`` (руководитель отдела продаж) sees
every manager and may omit the link; give a head ``--department-id`` (Bitrix
department id) to scope them to one department — an office РОП who sees only
their own roster/rollup. ``--name`` is optional when a Bitrix user id is
given: the display name is pulled from Bitrix (OKK's ``managers`` table, else
a live read-only ``user.get``).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from sqlalchemy import select

from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.enums import CompanionRole
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.web.api.v1.auth import hash_key
from AtamuraOKK.web.api.v1.service import resolve_manager_name


async def _create(
    role: CompanionRole,
    bitrix_user_id: int | None,
    name: str | None,
    department_id: int | None,
) -> None:
    key = secrets.token_urlsafe(24)
    async with session_scope() as session:
        if name is None and bitrix_user_id is not None:
            name = await resolve_manager_name(session, bitrix_user_id)
            if name is None:
                msg = (
                    f"Could not resolve a name for Bitrix user {bitrix_user_id} "
                    "(unknown id, or the webhook lacks the 'user' scope) — "
                    "pass --name explicitly."
                )
                raise SystemExit(msg)
        session.add(
            CompanionUser(
                key_sha256=hash_key(key),
                role=role,
                bitrix_user_id=bitrix_user_id,
                name=name,
                department_id=department_id,
            ),
        )
    scope = f", department_id={department_id}" if department_id is not None else ""
    print(  # noqa: T201
        f"Created {role.value} '{name}' (bitrix_user_id={bitrix_user_id}{scope})",
    )
    print(f"Personal access key (shown once, store it now): {key}")  # noqa: T201


async def _list() -> None:
    async with session_scope() as session:
        users = (
            await session.scalars(select(CompanionUser).order_by(CompanionUser.id))
        ).all()
    if not users:
        print("No companion users yet — issue one with 'create'.")  # noqa: T201
        return
    for u in users:
        state = "active" if u.active else "REVOKED"
        dept = f" dept={u.department_id}" if u.department_id is not None else ""
        print(  # noqa: T201
            f"#{u.id:<4} {u.role:<8} {state:<8} "
            f"bitrix_user_id={u.bitrix_user_id!s:<8} {u.name or ''}{dept}",
        )


async def _set_active(user_id: int, active: bool) -> None:
    async with session_scope() as session:
        user = await session.get(CompanionUser, user_id)
        if user is None:
            print(f"No companion user #{user_id}.")  # noqa: T201
            return
        user.active = active
    verb = "reactivated" if active else "revoked"
    print(f"User #{user_id} ({user.name or user.role}) {verb}.")  # noqa: T201


def _cmd_create(args: argparse.Namespace) -> None:
    role = CompanionRole(args.role)
    if role is CompanionRole.MANAGER and args.bitrix_user_id is None:
        msg = "--bitrix-user-id is required for role 'manager'"
        raise SystemExit(msg)
    if role is not CompanionRole.HEAD and args.department_id is not None:
        msg = "--department-id only applies to role 'head' (an office РОП)"
        raise SystemExit(msg)
    if args.name is None and args.bitrix_user_id is None:
        msg = "--name is required when no --bitrix-user-id is given"
        raise SystemExit(msg)
    asyncio.run(_create(role, args.bitrix_user_id, args.name, args.department_id))


def _cmd_list(_: argparse.Namespace) -> None:
    asyncio.run(_list())


def _cmd_revoke(args: argparse.Namespace) -> None:
    asyncio.run(_set_active(args.id, active=args.activate))


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(
        prog="python -m AtamuraOKK.companion_users",
        description="Issue/revoke personal access keys for the companion cabinet.",
    )
    sub = parser.add_subparsers(required=True)

    create = sub.add_parser("create", help="issue a key (printed once)")
    create.add_argument(
        "--role",
        choices=[r.value for r in CompanionRole],
        required=True,
    )
    create.add_argument(
        "--name",
        default=None,
        help="display name (default: pulled from Bitrix by --bitrix-user-id)",
    )
    create.add_argument(
        "--bitrix-user-id",
        type=int,
        default=None,
        help="Bitrix user id (required for managers; scopes their data)",
    )
    create.add_argument(
        "--department-id",
        type=int,
        default=None,
        help=(
            "Bitrix department id — scopes a 'head' to one department "
            "(office РОП); omit for the global head"
        ),
    )
    create.set_defaults(func=_cmd_create)

    lst = sub.add_parser("list", help="show all cabinet users")
    lst.set_defaults(func=_cmd_list)

    revoke = sub.add_parser("revoke", help="deactivate a user's key")
    revoke.add_argument("--id", type=int, required=True)
    revoke.add_argument(
        "--activate",
        action="store_true",
        help="reactivate instead of revoking",
    )
    revoke.set_defaults(func=_cmd_revoke)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
