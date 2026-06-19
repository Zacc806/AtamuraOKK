"""The web app must register every ORM model at startup.

Regression for appeal writes returning 500: the companion read API runs on raw
SQL over views, so the API process only imported the handful of models it
*writes* (e.g. ``Appeal``). That left the ``appeals.call_id`` -> ``calls.id``
foreign-key target (the ``calls`` table) unregistered in ``Base.metadata``, so
SQLAlchemy could not resolve the FK and every appeal flush raised
``NoReferencedTableError`` for every manager on every call.

This runs in a fresh interpreter on purpose: the rest of the suite imports the
``Call`` model directly (e.g. via ``_seed_scored_call``), which would otherwise
register ``calls`` and mask a missing ``load_all_models()`` call at startup.
"""

import subprocess
import sys

_SNIPPET = """
import asyncio
from AtamuraOKK.db.base import Base
from AtamuraOKK.web.application import get_app

app = get_app()


async def run() -> None:
    async with app.router.lifespan_context(app):
        missing = {"calls", "appeals"} - set(Base.metadata.tables)
        assert not missing, f"models not registered at startup: {missing}"


asyncio.run(run())
"""


def test_web_startup_registers_all_orm_models() -> None:
    """Booting the app's lifespan registers ``calls``/``appeals`` in metadata."""
    proc = subprocess.run(  # noqa: S603 — fixed argv, literal snippet, no input
        [sys.executable, "-c", _SNIPPET],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
