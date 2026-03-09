# -*- coding: utf-8 -*-

"""Thread-safe bot orchestration engine.

Designed to be driven by either the CLI (main.py) or the web dashboard (web_app.py).
Only one launch session is permitted at a time (enforced by a Lock).
"""

import atexit
import concurrent.futures
import enum
import logging
import random
import threading
import time

from bot import launch_bot, init_name_pool

log = logging.getLogger(__name__)


class BotStatus(enum.Enum):
    PENDING = "pending"
    JOINING = "joining"
    JOINED = "joined"
    FAILED = "failed"


class BotManager:
    """Manages the full lifecycle of a bot-launch session."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active_drivers = []
        self._running = False
        self._stop_event = threading.Event()

        # Live stats — guarded by _stats_lock
        self._stats_lock = threading.Lock()
        self._bot_statuses = {}
        self._join_times = []
        self._succeeded = 0
        self._failed = 0
        self._total = 0

        # Callback hooks (set by the consumer: CLI or web layer)
        self.on_bot_update = None      # fn(bot_id, status, elapsed)
        self.on_stats_update = None    # fn(stats_dict)

        atexit.register(self._emergency_cleanup)

    # ── Public read-only snapshot ────────────────────────────────────────────
    def get_stats(self):
        with self._stats_lock:
            jt = list(self._join_times)
            return {
                "total": self._total,
                "succeeded": self._succeeded,
                "failed": self._failed,
                "join_times": jt,
                "avg_time": (sum(jt) / len(jt)) if jt else 0,
                "fastest": min(jt) if jt else 0,
                "slowest": max(jt) if jt else 0,
                "bot_statuses": dict(self._bot_statuses),
                "running": self._running,
            }

    @property
    def is_running(self):
        return self._running

    # ── Start ────────────────────────────────────────────────────────────────
    def start(self, cfg):
        """Begin launching bots.  Returns immediately; work runs in a daemon thread.

        Raises RuntimeError if a session is already active.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A launch session is already active.")

        self._stop_event.clear()
        self._running = True
        self._active_drivers.clear()

        # Reset stats
        with self._stats_lock:
            self._bot_statuses = {i: BotStatus.PENDING for i in range(cfg["num_bots"])}
            self._join_times = []
            self._succeeded = 0
            self._failed = 0
            self._total = cfg["num_bots"]

        self._notify_stats()

        t = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        t.start()

    # ── Background launch loop ───────────────────────────────────────────────
    def _run(self, cfg):
        try:
            num_bots = cfg["num_bots"]
            batch_size = min(cfg["thread_count"], num_bots)
            total_batches = (num_bots + batch_size - 1) // batch_size

            init_name_pool(cfg["names_list"])
            log.info("Launching %d bots in %d batch(es)…", num_bots, total_batches)

            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as pool:
                for batch_idx in range(total_batches):
                    if self._stop_event.is_set():
                        log.info("Launch cancelled by user.")
                        break

                    start = batch_idx * batch_size
                    end = min(start + batch_size, num_bots)

                    log.info(
                        "Batch %d/%d (Bots %d–%d)…",
                        batch_idx + 1, total_batches, start + 1, end,
                    )

                    futures = {}
                    for i in range(start, end):
                        self._set_status(i, BotStatus.JOINING)
                        futures[pool.submit(
                            launch_bot,
                            bot_id=i,
                            meeting_id=cfg["meeting_id"],
                            passcode=cfg["passcode"],
                            names_list=cfg["names_list"],
                            custom_name=cfg["custom_name"],
                        )] = i

                    for future in concurrent.futures.as_completed(futures):
                        bot_id = futures[future]
                        try:
                            driver, elapsed = future.result()
                        except Exception as exc:
                            log.error("Bot %d: Unexpected error: %s", bot_id + 1, exc)
                            driver, elapsed = None, 0.0

                        with self._stats_lock:
                            self._join_times.append(elapsed)
                            if driver:
                                self._active_drivers.append((bot_id + 1, driver))
                                self._succeeded += 1
                                self._bot_statuses[bot_id] = BotStatus.JOINED
                            else:
                                self._failed += 1
                                self._bot_statuses[bot_id] = BotStatus.FAILED

                        self._notify_bot(bot_id, elapsed)
                        self._notify_stats()

                    # Inter-batch delay (interruptible)
                    if batch_idx < total_batches - 1 and not self._stop_event.is_set():
                        delay = random.uniform(1, 2)
                        log.info("Batch done. Waiting %.1fs…", delay)
                        self._stop_event.wait(timeout=delay)

            self._log_summary()
        finally:
            self._running = False
            self._lock.release()
            self._notify_stats()

    # ── Stop / Shutdown ──────────────────────────────────────────────────────
    def stop(self):
        """Signal the launch loop to stop, then shut down all drivers."""
        self._stop_event.set()
        threading.Thread(target=self._shutdown_drivers, daemon=True).start()

    def _shutdown_drivers(self):
        drivers = list(self._active_drivers)
        if not drivers:
            return
        drivers.sort(key=lambda x: x[0])

        def _quit(item):
            bot_id, driver = item
            try:
                log.info("Exiting Bot %d…", bot_id)
                driver.quit()
            except Exception:
                pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(drivers), 1)) as pool:
            list(pool.map(_quit, drivers))

        self._active_drivers.clear()
        log.info("All %d bots exited.", len(drivers))

    # ── Internals ────────────────────────────────────────────────────────────
    def _set_status(self, bot_id, status):
        with self._stats_lock:
            self._bot_statuses[bot_id] = status
        self._notify_bot(bot_id, 0)
        self._notify_stats()

    def _notify_bot(self, bot_id, elapsed):
        if self.on_bot_update:
            try:
                self.on_bot_update(
                    bot_id,
                    self._bot_statuses.get(bot_id, BotStatus.PENDING),
                    elapsed,
                )
            except Exception:
                pass

    def _notify_stats(self):
        if self.on_stats_update:
            try:
                self.on_stats_update(self.get_stats())
            except Exception:
                pass

    def _log_summary(self):
        stats = self.get_stats()
        log.info("─── Results ───")
        log.info("  Succeeded : %d / %d", stats["succeeded"], stats["total"])
        log.info("  Failed    : %d / %d", stats["failed"], stats["total"])
        if stats["join_times"]:
            log.info("  Avg time  : %.1fs", stats["avg_time"])
            log.info("  Fastest   : %.1fs", stats["fastest"])
            log.info("  Slowest   : %.1fs", stats["slowest"])
        log.info("───────────────")

    def _emergency_cleanup(self):
        for _, driver in self._active_drivers:
            try:
                driver.quit()
            except Exception:
                pass
