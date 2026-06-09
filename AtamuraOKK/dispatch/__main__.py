"""Run a broker role: ``python -m AtamuraOKK.dispatch <role>``.

Roles: ``dispatcher`` (the beat) or a stage worker (``download`` / ``transcribe``
/ ``score``). Scale a stage by starting more of its workers (e.g.
``docker compose up --scale score=3``).
"""

from __future__ import annotations

import argparse

from arq.worker import run_worker

from AtamuraOKK.dispatch.worker_settings import ROLES


def main() -> None:
    """Parse the role and run the matching arq worker (blocking)."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.dispatch")
    parser.add_argument("role", choices=sorted(ROLES))
    args = parser.parse_args()
    run_worker(ROLES[args.role])  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
