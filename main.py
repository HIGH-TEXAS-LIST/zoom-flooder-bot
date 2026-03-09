# -*- coding: utf-8 -*-

"""Zoom Flooder Bot — CLI entry point."""

import logging
import os
import time

try:
    import keyboard
    _HAS_KEYBOARD = True
except ImportError:
    _HAS_KEYBOARD = False

from config import get_user_config
from bot_manager import BotManager

# ── Logging setup ────────────────────────────────────────────────────────────
LOG_FILE = "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

for name in ("selenium", "urllib3", "webdriver_manager"):
    logging.getLogger(name).setLevel(logging.WARNING)


# ── Helpers ──────────────────────────────────────────────────────────────────
def wait_for_exit():
    """Block until the exit hotkey or Ctrl+C."""
    if _HAS_KEYBOARD:
        exit_event = keyboard.add_hotkey("alt+ctrl+shift+e", lambda: None)
        log.info("Press Alt+Ctrl+Shift+E to exit all bots.")
        try:
            keyboard.wait("alt+ctrl+shift+e")
        except KeyboardInterrupt:
            pass
        finally:
            keyboard.remove_hotkey(exit_event)
    else:
        log.info("Press Enter to exit all bots (keyboard module not installed).")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    os.system("cls" if os.name == "nt" else "clear")
    print("Zoom Flooder Bot V1\n")

    cfg = get_user_config()
    manager = BotManager()

    manager.start(cfg)

    # Wait for the launch to finish
    while manager.is_running:
        time.sleep(0.5)

    wait_for_exit()
    manager.stop()


if __name__ == "__main__":
    main()
