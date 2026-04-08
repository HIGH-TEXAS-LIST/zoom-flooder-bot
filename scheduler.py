# -*- coding: utf-8 -*-

"""SQLite-backed raid scheduler — persists across restarts."""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from config import build_config

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raids.db")
POLL_INTERVAL = 15  # seconds


class RaidScheduler:
    """Polls an SQLite database for pending raids and fires them via BotManager."""

    def __init__(self, db_path=DB_PATH, bot_manager=None, on_update=None):
        self._db_path = db_path
        self._manager = bot_manager
        self._on_update = on_update  # callback() when schedule list changes
        self._stop_event = threading.Event()
        self._thread = None
        self._init_db()

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_raids (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id      TEXT NOT NULL,
                    passcode        TEXT NOT NULL DEFAULT '',
                    num_bots        INTEGER NOT NULL DEFAULT 1,
                    thread_count    INTEGER NOT NULL DEFAULT 1,
                    custom_name     TEXT NOT NULL DEFAULT '',
                    chat_message    TEXT NOT NULL DEFAULT '',
                    chat_recipient  TEXT NOT NULL DEFAULT '',
                    reactions       TEXT NOT NULL DEFAULT '[]',
                    reaction_count  INTEGER NOT NULL DEFAULT 0,
                    reaction_delay  REAL NOT NULL DEFAULT 1.0,
                    chat_repeat_count INTEGER NOT NULL DEFAULT 0,
                    chat_repeat_delay REAL NOT NULL DEFAULT 2.0,
                    waiting_room_timeout INTEGER NOT NULL DEFAULT 60,
                    scheduled_time  TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    source          TEXT NOT NULL DEFAULT 'web',
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
        finally:
            conn.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def schedule_raid(self, data, scheduled_time, source="web"):
        """Insert a new pending raid. Returns the row ID.

        *data* is a dict with keys matching build_config() parameters.
        *scheduled_time* is an ISO 8601 string.
        """
        dt = datetime.fromisoformat(scheduled_time)
        if dt.tzinfo is None:
            # Treat naive datetimes as local time
            if dt <= datetime.now():
                raise ValueError("Scheduled time must be in the future.")
        else:
            if dt <= datetime.now(timezone.utc):
                raise ValueError("Scheduled time must be in the future.")

        conn = self._connect()
        try:
            cur = conn.execute("""
                INSERT INTO scheduled_raids
                    (meeting_id, passcode, num_bots, thread_count, custom_name,
                     chat_message, chat_recipient, reactions, reaction_count,
                     reaction_delay, chat_repeat_count, chat_repeat_delay,
                     waiting_room_timeout, scheduled_time, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(data.get("meeting_id", "")).strip(),
                str(data.get("passcode", "")).strip(),
                int(data.get("num_bots", 1)),
                int(data.get("thread_count", 1)),
                str(data.get("custom_name", "")).strip(),
                str(data.get("chat_message", "")).strip(),
                str(data.get("chat_recipient", "")).strip(),
                json.dumps(data.get("reactions", [])),
                int(data.get("reaction_count", 0)),
                float(data.get("reaction_delay", 1.0)),
                int(data.get("chat_repeat_count", 0)),
                float(data.get("chat_repeat_delay", 2.0)),
                int(data.get("waiting_room_timeout", 60)),
                scheduled_time,
                source,
            ))
            conn.commit()
            raid_id = cur.lastrowid
        finally:
            conn.close()

        log.info("Scheduled raid #%d for %s (source: %s).", raid_id, scheduled_time, source)
        self._notify()
        return raid_id

    def cancel_raid(self, raid_id):
        """Cancel a pending raid. Returns True if found and was pending."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE scheduled_raids SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (raid_id,),
            )
            conn.commit()
            changed = cur.rowcount > 0
        finally:
            conn.close()

        if changed:
            log.info("Cancelled scheduled raid #%d.", raid_id)
            self._notify()
        return changed

    def list_pending(self):
        """Return all pending raids, ordered by scheduled_time."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM scheduled_raids WHERE status = 'pending' ORDER BY scheduled_time ASC"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def list_all(self, limit=50):
        """Return recent raids of all statuses."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM scheduled_raids ORDER BY scheduled_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ── Background poll loop ──────────────────────────────────────────────────

    def start(self):
        """Start the background polling thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="raid-scheduler")
        self._thread.start()
        log.info("Raid scheduler started (polling every %ds).", POLL_INTERVAL)

    def stop(self):
        """Signal the polling thread to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=POLL_INTERVAL + 5)
        log.info("Raid scheduler stopped.")

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self._fire_due_raids()
            except Exception:
                log.exception("Scheduler poll error.")
            self._stop_event.wait(POLL_INTERVAL)

    def _fire_due_raids(self):
        now = datetime.now().isoformat()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM scheduled_raids WHERE status = 'pending' AND scheduled_time <= ?",
                (now,),
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            raid_id = row["id"]
            log.info("Firing scheduled raid #%d (meeting %s).", raid_id, row["meeting_id"])
            try:
                cfg = build_config(
                    meeting_id=row["meeting_id"],
                    passcode=row["passcode"],
                    thread_count=row["thread_count"],
                    num_bots=row["num_bots"],
                    custom_name=row["custom_name"],
                    chat_message=row["chat_message"],
                    chat_recipient=row["chat_recipient"],
                    reactions=json.loads(row["reactions"]),
                    reaction_count=row["reaction_count"],
                    reaction_delay=row["reaction_delay"],
                    chat_repeat_count=row["chat_repeat_count"],
                    chat_repeat_delay=row["chat_repeat_delay"],
                    waiting_room_timeout=row["waiting_room_timeout"],
                )
                self._manager.start(cfg)
                self._set_status(raid_id, "fired")
            except Exception as exc:
                log.warning("Scheduled raid #%d failed: %s", raid_id, exc)
                self._set_status(raid_id, "failed")

    def _set_status(self, raid_id, status):
        conn = self._connect()
        try:
            conn.execute("UPDATE scheduled_raids SET status = ? WHERE id = ?", (status, raid_id))
            conn.commit()
        finally:
            conn.close()
        self._notify()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _notify(self):
        if self._on_update:
            try:
                self._on_update()
            except Exception:
                pass

    @staticmethod
    def _row_to_dict(row):
        d = dict(row)
        # Parse reactions JSON for API consumers
        if isinstance(d.get("reactions"), str):
            try:
                d["reactions"] = json.loads(d["reactions"])
            except (json.JSONDecodeError, TypeError):
                d["reactions"] = []
        return d
