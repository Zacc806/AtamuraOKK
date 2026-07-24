"""Reconciled manager roster: ``python -m AtamuraOKK.manager_roster``.

Prints every manager from **both** sources side by side:

- the **CRM** identity (Bitrix ``PORTAL_USER_ID`` + name from ``user.get``) —
  authoritative, the join key;
- the **names spoken on the transcribed calls** — whatever name the manager voiced
  when introducing themselves, extracted by the scorer into
  ``scores.manager_spoken_name`` — supplementary, for verification.

The CRM name is never overwritten; spoken names are shown as evidence (with how
many scored calls voiced each), so a nameless/un-enriched CRM row can be read off
the calls and a mismatch (wrong name spoken) can be spotted. The same data is
served, head-scoped, at ``GET /api/v1/managers/roster``.

Usage:
  python -m AtamuraOKK.manager_roster                 # table, all managers
  python -m AtamuraOKK.manager_roster --spoken-only   # only managers with a spoken name
  python -m AtamuraOKK.manager_roster --json           # machine-readable export
"""

from __future__ import annotations

import argparse
import asyncio
import json

from AtamuraOKK.db.session import session_scope
from AtamuraOKK.web.api.v1.schemas import ManagerRosterEntry, SpokenName
from AtamuraOKK.web.api.v1.service import get_manager_roster


def _format_spoken(entries: list[SpokenName]) -> str:
    """``Айгуль ×12, Aigul ×3`` — spoken names with per-name call counts."""
    return ", ".join(f"{e.name} ×{e.calls}" for e in entries) or "—"


async def _run(*, spoken_only: bool, as_json: bool) -> None:
    async with session_scope() as session:
        roster: list[ManagerRosterEntry] = await get_manager_roster(session)
    if spoken_only:
        roster = [r for r in roster if r.spoken_names]

    if as_json:
        print(  # noqa: T201
            json.dumps([r.model_dump() for r in roster], ensure_ascii=False, indent=2)
        )
        return

    print(  # noqa: T201
        f"{'Bitrix ID':>10}  {'CRM (Bitrix)':<28}  Названо на звонках"
    )
    print("-" * 78)  # noqa: T201
    for r in roster:
        crm = r.crm_name or ("(без имени)" if r.enriched else "(не обогащён)")
        flag = "" if r.active else " [неактивен]"
        print(  # noqa: T201
            f"{r.bitrix_user_id:>10}  {crm[:28]:<28}{flag}  "
            f"{_format_spoken(r.spoken_names)}"
        )
    print(f"\nВсего менеджеров: {len(roster)}")  # noqa: T201


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Reconciled manager roster from CRM + transcribed calls.",
    )
    parser.add_argument(
        "--spoken-only",
        action="store_true",
        help="only managers who voiced a name on at least one scored call",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit JSON instead of a table",
    )
    args = parser.parse_args()
    asyncio.run(_run(spoken_only=args.spoken_only, as_json=args.as_json))


if __name__ == "__main__":
    main()
