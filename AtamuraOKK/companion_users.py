"""Companion cabinet user management: ``python -m AtamuraOKK.companion_users``.

Commands:
  create         issue a personal access key (printed ONCE — only its hash is stored)
  list           show all cabinet users
  revoke         deactivate a user's key (or --activate to restore it)
  import-roster  bulk-issue keys for whole departments from a roster JSON

A ``manager`` user must be linked to a Bitrix user id — that link is what the
read API scopes their data to. A ``head`` (руководитель отдела продаж) sees
every manager and may omit the link; give a head ``--department-id`` (Bitrix
department id) to scope them to one department — an office РОП who sees only
their own roster/rollup. ``--name`` is optional when a Bitrix user id is
given: the display name is pulled from Bitrix (OKK's ``managers`` table, else
a live read-only ``user.get``).

The cabinet (``/api/v1/users``) covers day-to-day issuance — heads mint
manager keys, the global head also mints department-scoped head keys. This
CLI remains the fallback and the only way to create a *global* (dept-less)
head row or reactivate a revoked key.

``import-roster`` onboards whole departments at once from a JSON roster
(``AtamuraOKK/data/companion_roster.json`` by default): it pulls every
department from Bitrix (``department.get``) to verify each roster department
id exists and capture its real name, then issues one key per person — the РОП
of each office as a department-scoped ``head``, everyone else as a ``manager``
tied to that office's department. The run is idempotent (re-running skips
people who already hold an active key; ``--force`` revokes-and-reissues).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.enums import CompanionRole
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.web.api.v1.auth import hash_key
from AtamuraOKK.web.api.v1.service import (
    assign_manager_department,
    resolve_manager_name,
)

_DEFAULT_ROSTER_PATH = str(Path(__file__).parent / "data" / "companion_roster.json")
_HEAD_LABELS = {"роп", "head"}


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


@dataclass(frozen=True)
class _RosterPerson:
    bitrix_user_id: int
    role_label: str
    cabinet_role: CompanionRole
    name: str | None


@dataclass(frozen=True)
class _RosterDept:
    bitrix_department_id: int
    label: str | None
    staff: list[_RosterPerson]


@dataclass
class _Result:
    name: str | None
    role_label: str
    cabinet_role: CompanionRole
    department_id: int
    department_name: str | None
    status: str
    key: str | None = None


def _classify_role(label: str) -> CompanionRole:
    if label.strip().casefold() in _HEAD_LABELS:
        return CompanionRole.HEAD
    return CompanionRole.MANAGER


def _parse_person(person: object, dept_id: int) -> _RosterPerson:
    if not isinstance(person, dict):
        raise SystemExit(f"Department {dept_id}: each staffer must be an object.")
    uid = person.get("bitrix_user_id")
    role = person.get("role")
    if not isinstance(uid, int) or not isinstance(role, str):
        msg = (
            f"Department {dept_id}: each staffer needs int "
            "'bitrix_user_id' and str 'role'."
        )
        raise SystemExit(msg)
    name = person.get("name")
    return _RosterPerson(
        bitrix_user_id=uid,
        role_label=role,
        cabinet_role=_classify_role(role),
        name=name if isinstance(name, str) and name else None,
    )


def _parse_dept(dept: object, seen_users: dict[int, int]) -> _RosterDept:
    if not isinstance(dept, dict):
        raise SystemExit("Each entry in 'departments' must be an object.")
    dept_id = dept.get("bitrix_department_id")
    if not isinstance(dept_id, int):
        raise SystemExit("Each department needs an int 'bitrix_department_id'.")
    staff_raw = dept.get("staff")
    if not isinstance(staff_raw, list) or not staff_raw:
        raise SystemExit(f"Department {dept_id} has no 'staff' list.")
    staff: list[_RosterPerson] = []
    for raw_person in staff_raw:
        person = _parse_person(raw_person, dept_id)
        if person.bitrix_user_id in seen_users:
            msg = (
                f"Bitrix user id {person.bitrix_user_id} appears in both "
                f"department {seen_users[person.bitrix_user_id]} and {dept_id}."
            )
            raise SystemExit(msg)
        seen_users[person.bitrix_user_id] = dept_id
        staff.append(person)
    if sum(p.cabinet_role is CompanionRole.HEAD for p in staff) > 1:
        raise SystemExit(f"Department {dept_id} has more than one РОП (head).")
    label = dept.get("label")
    return _RosterDept(
        bitrix_department_id=dept_id,
        label=label if isinstance(label, str) and label else None,
        staff=staff,
    )


def _load_roster(path: str) -> list[_RosterDept]:
    """Parse and validate the roster JSON (raises SystemExit on any problem)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Roster file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Roster file is not valid JSON ({path}): {exc}") from None

    departments = raw.get("departments") if isinstance(raw, dict) else None
    if not isinstance(departments, list) or not departments:
        raise SystemExit("Roster must have a non-empty 'departments' list.")

    seen_users: dict[int, int] = {}
    return [_parse_dept(dept, seen_users) for dept in departments]


async def _pull_bitrix_departments() -> dict[int, str]:
    """All Bitrix departments as ``{id: name}``; fatal on any Bitrix failure.

    Unlike ``service._bitrix_department_names`` (which degrades to ``{}``),
    verification is the whole point here, so a missing scope / unreachable
    webhook / empty result aborts the run.
    """
    names: dict[int, str] = {}
    try:
        async with BitrixClient() as bx:
            async for row in bx.list("department.get"):
                dept_id, name = row.get("ID"), row.get("NAME")
                if dept_id is not None and name:
                    names[int(dept_id)] = str(name)
    except (BitrixError, ValueError) as exc:
        msg = f"Bitrix department.get failed — cannot verify roster: {exc}"
        raise SystemExit(msg) from exc
    if not names:
        raise SystemExit(
            "Bitrix department.get returned no departments — cannot verify roster.",
        )
    return names


async def _upsert_department(
    session: AsyncSession,
    bitrix_id: int,
    name: str,
) -> None:
    """Get-or-create a department by Bitrix id and set its real name."""
    department = await session.scalar(
        select(Department).where(Department.bitrix_id == bitrix_id),
    )
    if department is None:
        session.add(Department(bitrix_id=bitrix_id, name=name))
    else:
        department.name = name
    await session.flush()


async def _existing_active_code(
    session: AsyncSession,
    *,
    role: CompanionRole,
    bitrix_user_id: int | None,
    department_id: int | None,
) -> CompanionUser | None:
    """An active code that already covers this person (idempotency probe).

    Managers key on ``bitrix_user_id``; heads (whose ``bitrix_user_id`` may be
    NULL) key on the scoped Bitrix ``department_id``.
    """
    query = select(CompanionUser).where(
        CompanionUser.role == role,
        CompanionUser.active.is_(True),
    )
    if role is CompanionRole.HEAD:
        query = query.where(CompanionUser.department_id == department_id)
    else:
        query = query.where(CompanionUser.bitrix_user_id == bitrix_user_id)
    return await session.scalar(query.limit(1))


async def _issue_code(
    session: AsyncSession,
    *,
    role: CompanionRole,
    bitrix_user_id: int | None,
    department_bitrix_id: int | None,
    name: str | None,
) -> str:
    """Add a CompanionUser row and return the raw key (shown once)."""
    key = secrets.token_urlsafe(24)
    session.add(
        CompanionUser(
            key_sha256=hash_key(key),
            role=role,
            bitrix_user_id=bitrix_user_id,
            name=name,
            department_id=department_bitrix_id if role is CompanionRole.HEAD else None,
        ),
    )
    await session.flush()
    return key


async def _import_roster(path: str, *, force: bool) -> None:
    roster = _load_roster(path)
    bitrix_depts = await _pull_bitrix_departments()

    missing = sorted(
        d.bitrix_department_id
        for d in roster
        if d.bitrix_department_id not in bitrix_depts
    )
    if missing:
        ids = ", ".join(str(m) for m in missing)
        raise SystemExit(
            f"Roster department id(s) not found in Bitrix: {ids}. Nothing issued.",
        )

    results: list[_Result] = []
    async with session_scope() as session:
        for dept in roster:
            dept_id = dept.bitrix_department_id
            dept_name = bitrix_depts[dept_id]
            await _upsert_department(session, dept_id, dept_name)
            for person in dept.staff:
                results.append(
                    await _issue_for_person(
                        session,
                        person=person,
                        dept_id=dept_id,
                        dept_name=dept_name,
                        force=force,
                    ),
                )

    _print_results(results)


async def _issue_for_person(
    session: AsyncSession,
    *,
    person: _RosterPerson,
    dept_id: int,
    dept_name: str,
    force: bool,
) -> _Result:
    role = person.cabinet_role
    existing = await _existing_active_code(
        session,
        role=role,
        bitrix_user_id=person.bitrix_user_id,
        department_id=dept_id,
    )
    result = _Result(
        name=person.name,
        role_label=person.role_label,
        cabinet_role=role,
        department_id=dept_id,
        department_name=dept_name,
        status="issued",
    )
    if existing is not None:
        if not force:
            result.status = "skipped (exists)"
            return result
        existing.active = False
        result.status = "reissued"

    name = person.name or await resolve_manager_name(session, person.bitrix_user_id)
    result.name = name
    if role is CompanionRole.MANAGER:
        await assign_manager_department(
            session,
            person.bitrix_user_id,
            dept_id,
            name,
        )
    result.key = await _issue_code(
        session,
        role=role,
        bitrix_user_id=person.bitrix_user_id,
        department_bitrix_id=dept_id,
        name=name,
    )
    return result


def _print_results(results: list[_Result]) -> None:
    issued = reissued = skipped = 0
    print("\nname                          role      dept  key")  # noqa: T201
    print("-" * 88)  # noqa: T201
    for r in results:
        if r.status == "issued":
            issued += 1
        elif r.status == "reissued":
            reissued += 1
        else:
            skipped += 1
        name = (r.name or f"user {r.role_label}")[:28]
        role = f"{r.cabinet_role.value}"
        key = r.key or f"[{r.status}]"
        print(f"{name:<29} {role:<9} {r.department_id:<5} {key}")  # noqa: T201
    print(  # noqa: T201
        f"\nDepartments: {len({r.department_id for r in results})}  "
        f"issued={issued} reissued={reissued} skipped_exists={skipped}",
    )
    if issued or reissued:
        print("Keys are shown once — store them now.")  # noqa: T201


def _cmd_import_roster(args: argparse.Namespace) -> None:
    asyncio.run(_import_roster(args.roster_path, force=args.force))


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

    imp = sub.add_parser(
        "import-roster",
        help="bulk-issue keys for whole departments from a roster JSON",
    )
    imp.add_argument(
        "--roster-path",
        default=_DEFAULT_ROSTER_PATH,
        help="path to roster JSON (default: bundled companion_roster.json)",
    )
    imp.add_argument(
        "--force",
        action="store_true",
        help="revoke-and-reissue even if an active key already exists",
    )
    imp.set_defaults(func=_cmd_import_roster)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
