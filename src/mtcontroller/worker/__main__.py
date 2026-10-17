"""Worker: polling, fronta změn, zálohy. Zatím jen kostra smyčky (F0).

Spouští se jako `mtc-worker` (systemd: mtc-worker.service).
"""

from __future__ import annotations

import asyncio
import logging
import signal

log = logging.getLogger("mtc.worker")

TICK_SECONDS = 30


async def tick() -> None:
    # F0: zde přibude kontrola check-inů (offline detekce) a fronta adopcí.
    log.debug("tick")


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker started")
    while not stop.is_set():
        try:
            await tick()
        except Exception:  # jedna chyba nesmí shodit službu
            log.exception("tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("worker stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
