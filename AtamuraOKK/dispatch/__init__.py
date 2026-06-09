"""Distributed-worker dispatch layer.

Postgres stays the single source of truth (the ``calls.status`` column); this
package adds race-safe work-claiming (``claim``) and an optional broker-based
fan-out (``dispatcher``/``tasks``/``worker_settings``) so the pipeline can run
as several cooperating processes instead of one APScheduler worker.
"""
