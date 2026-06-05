"""Provision the QA questions + dashboards in Metabase via its API.

Turns the SQL files in ``metabase/queries/`` into saved native questions, then
assembles them into the five dashboards (department roll-up, per-manager
scorecard, call drill-down, flagged queue, pipeline). Idempotent: re-running
updates the existing collection / cards / dashboards in place rather than
duplicating them.

    METABASE_ADMIN_EMAIL=you@atamura.kz \
    METABASE_ADMIN_PASSWORD='Str0ng-Passw0rd!' \
    uv run python metabase/provision_dashboards.py

Run ``metabase/bootstrap.py`` first (creates the admin + connects the data
source). Metabase reaches Postgres on the internal host ``AtamuraOKK-db:5432``.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

MB_URL = os.environ.get("METABASE_URL", "http://localhost:3000")
ADMIN_EMAIL = os.environ.get("METABASE_ADMIN_EMAIL", "admin@atamura.kz")
ADMIN_PASSWORD = os.environ.get("METABASE_ADMIN_PASSWORD", "")
DB_NAME = os.environ.get("METABASE_DB_DISPLAY_NAME", "Atamura QA")
# The actual Postgres database (dbname) holding the QA views, used to pick the
# right Metabase data source when its display name isn't "Atamura QA".
DB_DBNAME = os.environ.get("METABASE_DB_NAME", "AtamuraOKK")
# Hard override: set METABASE_DB_ID to skip auto-detection entirely.
DB_ID_OVERRIDE = os.environ.get("METABASE_DB_ID")
# A view that must be present for a data source to be the right one.
MARKER_TABLE = "call_scores_latest"
COLLECTION_NAME = "Atamura QA"
QUERIES_DIR = Path(__file__).parent / "queries"

# The Metabase dashboard grid is 24 columns wide.
GRID = 24


# --- Card (question) definitions -------------------------------------------
# Each maps a SQL file to a display type + visualization settings. The names are
# the idempotency key (looked up within the collection on re-run).
CARDS: list[dict[str, Any]] = [
    {
        "file": "01_per_manager_scorecard.sql",
        "name": "01 — Per-manager scorecard",
        "display": "table",
        "viz": {
            "table.column_formatting": [
                {
                    "columns": ["avg_percent"],
                    "type": "single",
                    "operator": ">=",
                    "value": 75,
                    "color": "#84BB4C",
                },
                {
                    "columns": ["avg_percent"],
                    "type": "single",
                    "operator": "<",
                    "value": 75,
                    "color": "#ED6E6E",
                },
            ],
        },
    },
    {
        "file": "02_department_rollup.sql",
        "name": "02 — Department roll-up",
        "display": "bar",
        "viz": {"graph.dimensions": ["department"], "graph.metrics": ["avg_percent"]},
    },
    {
        "file": "03_manager_trend_weekly.sql",
        "name": "03 — Manager weekly trend",
        "display": "line",
        "viz": {
            "graph.dimensions": ["week", "manager_name"],
            "graph.metrics": ["avg_percent"],
        },
    },
    {
        "file": "04_zone_distribution.sql",
        "name": "04 — Zone distribution",
        "display": "pie",
        "viz": {"pie.dimension": "zone", "pie.metric": "calls"},
    },
    {
        "file": "05_score_histogram.sql",
        "name": "05 — Score histogram",
        "display": "bar",
        "viz": {
            "graph.dimensions": ["bucket_start"],
            "graph.metrics": ["calls"],
            "graph.x_axis.title_text": "Балл (10-балльные интервалы)",
        },
    },
    {
        "file": "06_team_weakest_criteria.sql",
        "name": "06 — Weakest criteria",
        "display": "row",
        "viz": {
            "graph.dimensions": ["criterion_text"],
            "graph.metrics": ["avg_pct_of_max"],
        },
    },
    {
        "file": "07_block_distribution.sql",
        "name": "07 — Block distribution",
        "display": "bar",
        "viz": {
            "graph.dimensions": ["block_name"],
            "graph.metrics": ["avg_pct_of_max"],
        },
    },
    {
        "file": "08_flagged_calls_queue.sql",
        "name": "08 — Flagged-calls queue",
        "display": "table",
        "viz": {
            "table.column_formatting": [
                {
                    "columns": ["percent"],
                    "type": "single",
                    "operator": "<",
                    "value": 75,
                    "color": "#ED6E6E",
                },
            ],
        },
    },
    {
        "file": "09_call_drilldown.sql",
        "name": "09 — Call drill-down (header)",
        "display": "table",
        "viz": {},
    },
    {
        "file": "10_call_criteria_drilldown.sql",
        "name": "10 — Call criteria breakdown",
        "display": "table",
        "viz": {},
    },
    {
        "file": "11_pipeline_funnel.sql",
        "name": "11 — Pipeline funnel",
        "display": "funnel",
        "viz": {"funnel.dimension": "status", "funnel.metric": "calls"},
    },
]

# --- Dashboard definitions --------------------------------------------------
# placements: (card name, col, row, size_x, size_y). A "filter" of "department"
# adds a Text dashboard filter wired to every placed card that declares the
# {{department_name}} variable; "call_id" adds a required Number filter wired to
# cards declaring {{call_id}}.
DASHBOARDS: list[dict[str, Any]] = [
    {
        "name": "Atamura QA — Сводка по отделу",
        "filter": "department",
        "cards": [
            ("02 — Department roll-up", 0, 0, GRID, 5),
            ("04 — Zone distribution", 0, 5, 8, 6),
            ("05 — Score histogram", 8, 5, 8, 6),
            ("07 — Block distribution", 16, 5, 8, 6),
            ("06 — Weakest criteria", 0, 11, GRID, 9),
        ],
    },
    {
        "name": "Atamura QA — Менеджеры",
        "filter": "department",
        "cards": [
            ("01 — Per-manager scorecard", 0, 0, GRID, 7),
            ("03 — Manager weekly trend", 0, 7, GRID, 8),
        ],
    },
    {
        "name": "Atamura QA — Разбор звонка",
        "filter": "call_id",
        "cards": [
            ("09 — Call drill-down (header)", 0, 0, GRID, 8),
            ("10 — Call criteria breakdown", 0, 8, GRID, 10),
        ],
    },
    {
        "name": "Atamura QA — Очередь на разбор",
        "filter": "department",
        "cards": [
            ("08 — Flagged-calls queue", 0, 0, GRID, 12),
        ],
    },
    {
        "name": "Atamura QA — Конвейер",
        "filter": None,
        "cards": [
            ("11 — Pipeline funnel", 0, 0, GRID, 8),
        ],
    },
]


class MB:
    """Thin authenticated Metabase API client."""

    def __init__(self, client: httpx.Client, session_id: str) -> None:
        self.c = client
        self.h = {"X-Metabase-Session": session_id}

    def get(self, path: str) -> Any:
        """GET ``path`` and return parsed JSON."""
        r = self.c.get(path, headers=self.h)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json: Any) -> Any:
        """POST ``json`` to ``path`` and return parsed JSON."""
        r = self.c.post(path, headers=self.h, json=json)
        r.raise_for_status()
        return r.json()

    def put(self, path: str, json: Any) -> Any:
        """PUT ``json`` to ``path`` and return parsed JSON."""
        r = self.c.put(path, headers=self.h, json=json)
        r.raise_for_status()
        return r.json()


def _login(client: httpx.Client) -> str:
    login = client.post(
        "/api/session",
        json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if login.status_code >= 400:
        print(
            "Login failed — set METABASE_ADMIN_EMAIL/PASSWORD and run bootstrap first.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return str(login.json()["id"])


def _has_marker(mb: MB, db_id: int) -> bool:
    """True if the data source exposes the QA marker view."""
    meta = mb.get(f"/api/database/{db_id}/metadata")
    return any(t.get("name") == MARKER_TABLE for t in meta.get("tables", []))


def _database_id(mb: MB) -> int:
    """Find the Metabase data source that actually holds the QA views.

    Order: explicit METABASE_DB_ID > exact display-name match > the Postgres
    source whose dbname matches and which exposes ``call_scores_latest``. The
    last rule disambiguates duplicate / mis-pointed sources sharing a name.
    """
    if DB_ID_OVERRIDE:
        return int(DB_ID_OVERRIDE)

    data = mb.get("/api/database")
    rows = data.get("data", data) if isinstance(data, dict) else data

    by_name = [d for d in rows if d.get("name") == DB_NAME]
    if len(by_name) == 1:
        return int(by_name[0]["id"])

    # Candidates: Postgres sources pointing at the right dbname.
    pg = [
        d
        for d in rows
        if d.get("engine") == "postgres"
        and (d.get("details") or {}).get("dbname") == DB_DBNAME
    ]
    candidates = pg or [d for d in rows if d.get("engine") == "postgres"]
    for d in candidates:
        if _has_marker(mb, int(d["id"])):
            print(f"Using data source {d['name']!r} (id={d['id']}).")
            return int(d["id"])

    names = ", ".join(f"{d.get('name')!r}(id={d.get('id')})" for d in rows)
    print(
        f"No data source exposes {MARKER_TABLE!r}. Found: {names}. "
        "Run bootstrap.py, or set METABASE_DB_ID to the right one.",
    )
    raise SystemExit(1)


def _ensure_collection(mb: MB) -> int:
    for col in mb.get("/api/collection"):
        if col.get("name") == COLLECTION_NAME and not col.get("archived"):
            return int(col["id"])
    created = mb.post("/api/collection", {"name": COLLECTION_NAME})
    print(f"Created collection {COLLECTION_NAME!r}.")
    return int(created["id"])


def _template_tags(sql: str) -> dict[str, Any]:
    """Build Metabase template-tags for every {{var}} found in the SQL."""
    tags: dict[str, Any] = {}
    for var in dict.fromkeys(re.findall(r"\{\{\s*(\w+)\s*\}\}", sql)):
        is_call_id = var == "call_id"
        tags[var] = {
            "id": str(uuid.uuid4()),
            "name": var,
            "display-name": var.replace("_", " ").title(),
            "type": "number" if is_call_id else "text",
            "required": is_call_id,
        }
    return tags


def _collection_items(mb: MB, collection_id: int, model: str) -> list[dict[str, Any]]:
    """Items of one model in a collection (the endpoint wraps them in 'data')."""
    resp = mb.get(f"/api/collection/{collection_id}/items?models={model}")
    items = resp.get("data", resp) if isinstance(resp, dict) else resp
    return [it for it in items if it.get("model") == model]


def _existing_cards(mb: MB, collection_id: int) -> dict[str, int]:
    return {
        c["name"]: int(c["id"]) for c in _collection_items(mb, collection_id, "card")
    }


def _upsert_cards(mb: MB, collection_id: int, db_id: int) -> dict[str, int]:
    existing = _existing_cards(mb, collection_id)
    name_to_id: dict[str, int] = {}
    for spec in CARDS:
        sql = (QUERIES_DIR / spec["file"]).read_text(encoding="utf-8")
        body = {
            "name": spec["name"],
            "dataset_query": {
                "type": "native",
                "native": {"query": sql, "template-tags": _template_tags(sql)},
                "database": db_id,
            },
            "display": spec["display"],
            "visualization_settings": spec["viz"],
            "collection_id": collection_id,
        }
        if spec["name"] in existing:
            cid = existing[spec["name"]]
            mb.put(f"/api/card/{cid}", body)
            action = "updated"
        else:
            cid = int(mb.post("/api/card", body)["id"])
            action = "created"
        name_to_id[spec["name"]] = cid
        print(f"  card {action}: {spec['name']}")
    return name_to_id


def _card_has_var(spec_file: str, var: str) -> bool:
    sql = (QUERIES_DIR / spec_file).read_text(encoding="utf-8")
    return bool(re.search(r"\{\{\s*" + re.escape(var) + r"\s*\}\}", sql))


def _file_for(card_name: str) -> str:
    for spec in CARDS:
        if spec["name"] == card_name:
            return str(spec["file"])
    raise KeyError(card_name)


def _existing_dashboards(mb: MB, collection_id: int) -> dict[str, int]:
    return {
        d["name"]: int(d["id"])
        for d in _collection_items(mb, collection_id, "dashboard")
    }


def _filter_meta(kind: str) -> dict[str, Any]:
    """Dashboard-parameter definition + the SQL variable it maps to."""
    if kind == "department":
        return {
            "var": "department_name",
            "param": {
                "id": "dept_filter",
                "name": "Отдел",
                "slug": "department_name",
                "type": "string/=",
                "sectionId": "string",
            },
        }
    return {
        "var": "call_id",
        "param": {
            "id": "callid_filter",
            "name": "Call ID",
            "slug": "call_id",
            "type": "number/=",
            "sectionId": "number",
        },
    }


def _upsert_dashboards(
    mb: MB, collection_id: int, cards: dict[str, int]
) -> dict[str, dict[str, Any]]:
    existing = _existing_dashboards(mb, collection_id)
    result: dict[str, dict[str, Any]] = {}
    for dash in DASHBOARDS:
        name = dash["name"]
        if name in existing:
            dash_id = existing[name]
        else:
            new = mb.post(
                "/api/dashboard",
                {"name": name, "collection_id": collection_id},
            )
            dash_id = int(new["id"])

        fmeta = _filter_meta(dash["filter"]) if dash["filter"] else None
        parameters = [fmeta["param"]] if fmeta else []

        dashcards = []
        for i, (card_name, col, row, sx, sy) in enumerate(dash["cards"]):
            mappings = []
            if fmeta and _card_has_var(_file_for(card_name), fmeta["var"]):
                mappings.append(
                    {
                        "parameter_id": fmeta["param"]["id"],
                        "card_id": cards[card_name],
                        "target": ["variable", ["template-tag", fmeta["var"]]],
                    }
                )
            dashcards.append(
                {
                    "id": -(i + 1),
                    "card_id": cards[card_name],
                    "col": col,
                    "row": row,
                    "size_x": sx,
                    "size_y": sy,
                    "parameter_mappings": mappings,
                    "visualization_settings": {},
                }
            )

        mb.put(
            f"/api/dashboard/{dash_id}",
            {"name": name, "parameters": parameters, "dashcards": dashcards},
        )
        result[name] = {"id": dash_id, "param": fmeta["param"]["id"] if fmeta else None}
        print(f"  dashboard provisioned: {name} ({len(dashcards)} cards)")
    return result


def _wire_drilldown_clicks(
    mb: MB, cards: dict[str, int], dashboards: dict[str, dict[str, Any]]
) -> None:
    """Make the call_id column on the queue/scorecard open the drill-down dash."""
    drill = dashboards.get("Atamura QA — Разбор звонка")
    if not drill or not drill.get("param"):
        return
    target_id, param_id = drill["id"], drill["param"]
    click = {
        "type": "link",
        "linkType": "dashboard",
        "targetId": target_id,
        "parameterMapping": {
            param_id: {
                "id": param_id,
                "source": {"type": "column", "id": "call_id", "name": "call_id"},
                "target": {"type": "parameter", "id": param_id},
            },
        },
    }
    col_settings = {'["name","call_id"]': {"click_behavior": click}}
    for dash_name in ("Atamura QA — Очередь на разбор", "Atamura QA — Менеджеры"):
        meta = dashboards.get(dash_name)
        if not meta:
            continue
        full = mb.get(f"/api/dashboard/{meta['id']}")
        changed = False
        for dc in full.get("dashcards", []):
            if dc.get("card_id") in cards.values():
                vs = dict(dc.get("visualization_settings") or {})
                cs = dict(vs.get("column_settings") or {})
                cs.update(col_settings)
                vs["column_settings"] = cs
                dc["visualization_settings"] = vs
                changed = True
        if changed:
            mb.put(
                f"/api/dashboard/{meta['id']}",
                {"dashcards": full["dashcards"]},
            )
            print(f"  click-through wired on: {dash_name}")


def main() -> int:
    """Provision the collection, questions and dashboards. Idempotent."""
    if not ADMIN_PASSWORD:
        print("Set METABASE_ADMIN_PASSWORD.", file=sys.stderr)
        return 2
    with httpx.Client(base_url=MB_URL, timeout=120.0) as client:
        mb = MB(client, _login(client))
        db_id = _database_id(mb)
        collection_id = _ensure_collection(mb)
        print("Provisioning questions…")
        cards = _upsert_cards(mb, collection_id, db_id)
        print("Provisioning dashboards…")
        dashboards = _upsert_dashboards(mb, collection_id, cards)
        try:
            _wire_drilldown_clicks(mb, cards, dashboards)
        except httpx.HTTPError as exc:  # click-through is best-effort
            print(f"  (click-through wiring skipped: {exc})")
    print(f"\nDone. Open {MB_URL} → collection {COLLECTION_NAME!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
