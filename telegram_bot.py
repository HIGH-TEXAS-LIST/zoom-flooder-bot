# -*- coding: utf-8 -*-

"""Telegram bot integration for the Zoom flooder bot."""

import asyncio
import logging
import threading
from datetime import datetime

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

log = logging.getLogger(__name__)


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Zoom Raid Bot ready.\n"
        "Use /help to see available commands."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/raid <meeting_id> [passcode] [num_bots] — Start a raid now\n"
        "/stop — Stop the current raid\n"
        "/status — Check current raid status\n"
        "/schedule <meeting_id> <YYYY-MM-DD HH:MM> [passcode] [num_bots] — Schedule a raid\n"
        "/schedules — List pending scheduled raids\n"
        "/cancel <raid_id> — Cancel a scheduled raid\n"
        "/help — Show this message"
    )


async def cmd_raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manager = context.bot_data["manager"]
    args = context.args or []

    if not args:
        await update.message.reply_text("Usage: /raid <meeting_id> [passcode] [num_bots]")
        return

    meeting_id = args[0]
    passcode = args[1] if len(args) > 1 else ""
    try:
        num_bots = int(args[2]) if len(args) > 2 else 1
    except ValueError:
        await update.message.reply_text("num_bots must be a number.")
        return

    try:
        from config import build_config
        cfg = build_config(meeting_id=meeting_id, passcode=passcode,
                           num_bots=num_bots, thread_count=1)
        await asyncio.to_thread(manager.start, cfg)
        await update.message.reply_text(
            f"Raid started: {num_bots} bot(s) → meeting {meeting_id}"
        )
    except RuntimeError as exc:
        await update.message.reply_text(f"Could not start: {exc}")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manager = context.bot_data["manager"]
    try:
        manager.stop()
        await update.message.reply_text("Stop signal sent.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manager = context.bot_data["manager"]
    stats = manager.get_stats()
    running = "Yes" if manager.is_running else "No"
    jt = stats.get("join_times", [])
    lines = [
        f"Running: {running}",
        f"Succeeded: {stats.get('succeeded', 0)}",
        f"Failed: {stats.get('failed', 0)}",
    ]
    if jt:
        lines.append(f"Avg: {sum(jt)/len(jt):.1f}s | Fast: {min(jt):.1f}s | Slow: {max(jt):.1f}s")
    await update.message.reply_text("\n".join(lines))


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scheduler = context.bot_data["scheduler"]
    args = context.args or []

    # /schedule <meeting_id> <YYYY-MM-DD> <HH:MM> [passcode] [num_bots]
    # Or: /schedule <meeting_id> <YYYY-MM-DD HH:MM> [passcode] [num_bots]
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /schedule <meeting_id> <YYYY-MM-DD HH:MM> [passcode] [num_bots]"
        )
        return

    meeting_id = args[0]

    # Try to parse date+time (may be split across 2 args)
    dt = None
    time_args_used = 0
    for fmt, count in [("%Y-%m-%d %H:%M", 2), ("%Y-%m-%dT%H:%M", 1), ("%Y-%m-%d %H:%M", 1)]:
        try:
            time_str = " ".join(args[1:1 + count])
            dt = datetime.strptime(time_str, fmt)
            time_args_used = count
            break
        except (ValueError, IndexError):
            continue

    if dt is None:
        await update.message.reply_text("Invalid time. Use YYYY-MM-DD HH:MM")
        return

    remaining = args[1 + time_args_used:]
    passcode = remaining[0] if len(remaining) > 0 else ""
    try:
        num_bots = int(remaining[1]) if len(remaining) > 1 else 1
    except ValueError:
        num_bots = 1

    try:
        raid_id = scheduler.schedule_raid(
            {"meeting_id": meeting_id, "passcode": passcode, "num_bots": num_bots},
            dt.isoformat(),
            source="telegram",
        )
        await update.message.reply_text(
            f"Raid #{raid_id} scheduled: {num_bots} bot(s) → {meeting_id} at {dt}"
        )
    except ValueError as exc:
        await update.message.reply_text(f"Error: {exc}")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scheduler = context.bot_data["scheduler"]
    pending = scheduler.list_pending()
    if not pending:
        await update.message.reply_text("No pending scheduled raids.")
        return
    lines = []
    for r in pending:
        lines.append(f"#{r['id']} — {r['meeting_id']} at {r['scheduled_time']} ({r['num_bots']} bots)")
    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scheduler = context.bot_data["scheduler"]
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /cancel <raid_id>")
        return
    try:
        raid_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Raid ID must be a number.")
        return
    if scheduler.cancel_raid(raid_id):
        await update.message.reply_text(f"Raid #{raid_id} cancelled.")
    else:
        await update.message.reply_text("Raid not found or already fired.")


# ── Startup ──────────────────────────────────────────────────────────────────

def start_telegram_bot(token, bot_manager, scheduler, allowed_users=None):
    """Launch the Telegram bot in a background daemon thread.

    Returns the thread (already started).
    """
    allowed = set(allowed_users) if allowed_users else None

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        app = Application.builder().token(token).build()

        # Store shared instances for handlers
        app.bot_data["manager"] = bot_manager
        app.bot_data["scheduler"] = scheduler
        app.bot_data["allowed_users"] = allowed

        # Register commands
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("raid", cmd_raid))
        app.add_handler(CommandHandler("stop", cmd_stop))
        app.add_handler(CommandHandler("status", cmd_status))
        app.add_handler(CommandHandler("schedule", cmd_schedule))
        app.add_handler(CommandHandler("schedules", cmd_schedules))
        app.add_handler(CommandHandler("cancel", cmd_cancel))

        # Set bot command list for Telegram's UI menu
        async def _post_init(application):
            await application.bot.set_my_commands([
                BotCommand("raid", "Start a raid now"),
                BotCommand("stop", "Stop current raid"),
                BotCommand("status", "Check raid status"),
                BotCommand("schedule", "Schedule a future raid"),
                BotCommand("schedules", "List pending raids"),
                BotCommand("cancel", "Cancel a scheduled raid"),
                BotCommand("help", "Show help"),
            ])

        app.post_init = _post_init

        try:
            loop.run_until_complete(app.initialize())
            loop.run_until_complete(app.start())
            loop.run_until_complete(app.updater.start_polling(drop_pending_updates=True))
            log.info("Telegram bot started polling.")
            loop.run_forever()
        except Exception:
            log.exception("Telegram bot crashed.")
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name="telegram-bot")
    t.start()
    return t
