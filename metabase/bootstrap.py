"""One-shot Metabase provisioning: create the admin user + connect Postgres.

Runs the Metabase setup API so you don't have to click through the wizard.
Idempotent: if Metabase is already set up, it just reports and exits.

    METABASE_ADMIN_EMAIL=you@atamura.kz \
    METABASE_ADMIN_PASSWORD='Str0ng-Passw0rd!' \
    uv run python metabase/bootstrap.py

Note: Metabase (in Docker) reaches Postgres on the INTERNAL hostname
``AtamuraOKK-db:5432`` — not localhost:5433.
"""

from __future__ import annotations

import os
import sys

import httpx

MB_URL = os.environ.get("METABASE_URL", "http://localhost:3000")
ADMIN_EMAIL = os.environ.get("METABASE_ADMIN_EMAIL", "admin@atamura.kz")
ADMIN_PASSWORD = os.environ.get("METABASE_ADMIN_PASSWORD", "")

# Connection Metabase uses to reach our analytics DB (inside the compose network).
DB_DETAILS = {
    "host": os.environ.get("METABASE_DB_HOST", "AtamuraOKK-db"),
    "port": int(os.environ.get("METABASE_DB_PORT", "5432")),
    "dbname": os.environ.get("METABASE_DB_NAME", "AtamuraOKK"),
    "user": os.environ.get("METABASE_DB_USER", "AtamuraOKK"),
    "password": os.environ.get("METABASE_DB_PASS", "AtamuraOKK"),
    "ssl": False,
    "tunnel-enabled": False,
}


def _ensure_database(client: httpx.Client, session_id: str) -> None:
    """Add the 'Atamura QA' Postgres data source if it isn't connected yet."""
    headers = {"X-Metabase-Session": session_id}
    existing = client.get("/api/database", headers=headers).json()
    rows = existing.get("data", existing) if isinstance(existing, dict) else existing
    if any(d.get("name") == "Atamura QA" for d in rows):
        print("Data source 'Atamura QA' already connected.")
        return
    resp = client.post(
        "/api/database",
        headers=headers,
        json={
            "engine": "postgres",
            "name": "Atamura QA",
            "details": DB_DETAILS,
            "is_full_sync": True,
        },
    )
    resp.raise_for_status()
    db_id = resp.json()["id"]
    client.post(f"/api/database/{db_id}/sync_schema", headers=headers)
    print("Connected data source 'Atamura QA' and triggered schema sync.")


def main() -> int:
    """Provision Metabase (admin + Postgres data source); idempotent."""
    if not ADMIN_PASSWORD:
        print(
            "Set METABASE_ADMIN_PASSWORD (Metabase requires a strong password).",
            file=sys.stderr,
        )
        return 2

    with httpx.Client(base_url=MB_URL, timeout=60.0) as client:
        # Login first: works whenever the admin already exists, regardless of
        # whether a stale setup-token is still advertised.
        login = client.post(
            "/api/session",
            json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        if login.status_code < 400:
            session_id = login.json()["id"]
            print("Logged in as existing admin; ensuring data source.")
        else:
            token = client.get("/api/session/properties").json().get("setup-token")
            if not token:
                print("Admin login failed and no setup token — check credentials.")
                return 1
            resp = client.post(
                "/api/setup",
                json={
                    "token": token,
                    "user": {
                        "first_name": "QA",
                        "last_name": "Admin",
                        "email": ADMIN_EMAIL,
                        "password": ADMIN_PASSWORD,
                        "site_name": "Atamura QA",
                    },
                    "prefs": {"site_name": "Atamura QA", "allow_tracking": False},
                },
            )
            if resp.status_code >= 400:
                print(
                    "Setup failed:", resp.status_code, resp.text[:300], file=sys.stderr
                )
                return 1
            session_id = resp.json()["id"]
            print(f"Created admin {ADMIN_EMAIL}.")

        _ensure_database(client, session_id)

    print(f"Done. Open {MB_URL} — build questions from metabase/queries/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
