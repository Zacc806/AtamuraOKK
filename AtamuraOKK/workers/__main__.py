"""Worker entrypoint: ``python -m AtamuraOKK.workers``."""

from __future__ import annotations

import asyncio

from AtamuraOKK.workers.runner import run_forever


def main() -> None:
    """Run the worker scheduler until interrupted."""
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
