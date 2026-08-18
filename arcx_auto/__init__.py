"""Arcx Auto Golden - automated submission, monitoring, QA and rerun for
RC extraction runs driven by Arcx.

Layering (dependencies point one way only: L4 -> L3 -> L2 -> L1 -> L0):

    L4  cli/, web/    interfaces (thin, no business logic)
    L3  daemon/       orchestration (the single writer)
    L2  services/     business logic (unit testable)
    L1  adapters/     the only place with side effects
    L0  domain/       pure data and pure functions, zero I/O

See docs/architecture.md.
"""

__version__ = "0.1.0"
