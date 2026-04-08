# -*- coding: utf-8 -*-

"""Discord slash-command integration for the Zoom flooder bot."""

import asyncio
import logging
import threading
from datetime import datetime

import discord
from discord import app_commands

log = logging.getLogger(__name__)


class RaidBotClient(discord.Client):
    """Minimal Discord client with an app-command tree."""

    def __init__(self, bot_manager, scheduler, guild_id=None, allowed_channels=None):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._manager = bot_manager
        self._scheduler = scheduler
        self._guild_id = guild_id
        self._allowed_channels = set(allowed_channels) if allowed_channels else None
        self._register_commands()

    async def setup_hook(self):
        if self._guild_id:
            guild = discord.Object(id=self._guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Discord slash commands synced to guild %s.", self._guild_id)
        else:
            await self.tree.sync()
            log.info("Discord slash commands synced globally (may take up to 1 hour).")

    async def on_ready(self):
        log.info("Discord bot logged in as %s (ID: %s).", self.user, self.user.id)

    def _check_channel(self, interaction: discord.Interaction) -> bool:
        """Return True if the channel is allowed (or no restriction set)."""
        if self._allowed_channels is None:
            return True
        return interaction.channel_id in self._allowed_channels

    def _register_commands(self):
        manager = self._manager
        scheduler = self._scheduler
        check_channel = self._check_channel

        @self.tree.command(name="raid", description="Start a raid immediately")
        @app_commands.describe(
            meeting_id="Zoom meeting ID",
            passcode="Meeting passcode (optional)",
            num_bots="Number of bots (default 1)",
            thread_count="Concurrent threads (default 1)",
            custom_name="Bot display name (blank for random)",
            chat_message="Message to send in chat (optional)",
        )
        async def cmd_raid(
            interaction: discord.Interaction,
            meeting_id: str,
            passcode: str = "",
            num_bots: int = 1,
            thread_count: int = 1,
            custom_name: str = "",
            chat_message: str = "",
        ):
            if not check_channel(interaction):
                await interaction.response.send_message("Not allowed in this channel.", ephemeral=True)
                return
            await interaction.response.defer()
            try:
                from config import build_config
                cfg = build_config(
                    meeting_id=meeting_id,
                    passcode=passcode,
                    num_bots=num_bots,
                    thread_count=thread_count,
                    custom_name=custom_name,
                    chat_message=chat_message,
                )
                await asyncio.to_thread(manager.start, cfg)
                embed = discord.Embed(
                    title="Raid Started",
                    description=f"Launching **{num_bots}** bot(s) into meeting `{meeting_id}`.",
                    color=discord.Color.green(),
                )
                await interaction.followup.send(embed=embed)
            except RuntimeError as exc:
                await interaction.followup.send(f"Could not start: {exc}")
            except Exception as exc:
                await interaction.followup.send(f"Error: {exc}")

        @self.tree.command(name="stop", description="Stop the current raid")
        async def cmd_stop(interaction: discord.Interaction):
            if not check_channel(interaction):
                await interaction.response.send_message("Not allowed in this channel.", ephemeral=True)
                return
            try:
                manager.stop()
                await interaction.response.send_message("Stop signal sent.")
            except Exception as exc:
                await interaction.response.send_message(f"Error: {exc}")

        @self.tree.command(name="status", description="Check current raid status")
        async def cmd_status(interaction: discord.Interaction):
            if not check_channel(interaction):
                await interaction.response.send_message("Not allowed in this channel.", ephemeral=True)
                return
            stats = manager.get_stats()
            running = "Yes" if manager.is_running else "No"
            embed = discord.Embed(title="Raid Status", color=discord.Color.blue())
            embed.add_field(name="Running", value=running, inline=True)
            embed.add_field(name="Succeeded", value=str(stats.get("succeeded", 0)), inline=True)
            embed.add_field(name="Failed", value=str(stats.get("failed", 0)), inline=True)
            jt = stats.get("join_times", [])
            if jt:
                embed.add_field(name="Avg Time", value=f"{sum(jt)/len(jt):.1f}s", inline=True)
                embed.add_field(name="Fastest", value=f"{min(jt):.1f}s", inline=True)
                embed.add_field(name="Slowest", value=f"{max(jt):.1f}s", inline=True)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="schedule", description="Schedule a raid for later")
        @app_commands.describe(
            meeting_id="Zoom meeting ID",
            time="When to start (YYYY-MM-DD HH:MM)",
            passcode="Meeting passcode (optional)",
            num_bots="Number of bots (default 1)",
        )
        async def cmd_schedule(
            interaction: discord.Interaction,
            meeting_id: str,
            time: str,
            passcode: str = "",
            num_bots: int = 1,
        ):
            if not check_channel(interaction):
                await interaction.response.send_message("Not allowed in this channel.", ephemeral=True)
                return
            try:
                # Parse flexible datetime formats
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%m/%d/%Y %H:%M"):
                    try:
                        dt = datetime.strptime(time, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    await interaction.response.send_message(
                        "Invalid time format. Use `YYYY-MM-DD HH:MM`.", ephemeral=True
                    )
                    return

                raid_id = scheduler.schedule_raid(
                    {"meeting_id": meeting_id, "passcode": passcode, "num_bots": num_bots},
                    dt.isoformat(),
                    source="discord",
                )
                embed = discord.Embed(
                    title="Raid Scheduled",
                    description=f"Raid **#{raid_id}** → meeting `{meeting_id}` at `{dt}`.",
                    color=discord.Color.gold(),
                )
                await interaction.response.send_message(embed=embed)
            except ValueError as exc:
                await interaction.response.send_message(f"Error: {exc}", ephemeral=True)
            except Exception as exc:
                await interaction.response.send_message(f"Error: {exc}")

        @self.tree.command(name="schedules", description="List pending scheduled raids")
        async def cmd_schedules(interaction: discord.Interaction):
            if not check_channel(interaction):
                await interaction.response.send_message("Not allowed in this channel.", ephemeral=True)
                return
            pending = scheduler.list_pending()
            if not pending:
                await interaction.response.send_message("No pending scheduled raids.")
                return
            lines = []
            for r in pending:
                lines.append(f"**#{r['id']}** — `{r['meeting_id']}` at `{r['scheduled_time']}` ({r['num_bots']} bots)")
            embed = discord.Embed(
                title=f"Pending Raids ({len(pending)})",
                description="\n".join(lines),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="cancel", description="Cancel a scheduled raid")
        @app_commands.describe(raid_id="ID of the scheduled raid")
        async def cmd_cancel(interaction: discord.Interaction, raid_id: int):
            if not check_channel(interaction):
                await interaction.response.send_message("Not allowed in this channel.", ephemeral=True)
                return
            if scheduler.cancel_raid(raid_id):
                await interaction.response.send_message(f"Raid #{raid_id} cancelled.")
            else:
                await interaction.response.send_message("Raid not found or already fired.", ephemeral=True)


def start_discord_bot(token, bot_manager, scheduler, guild_id=None, allowed_channels=None):
    """Launch the Discord bot in a background daemon thread.

    Returns the thread (already started).
    """
    client = RaidBotClient(
        bot_manager=bot_manager,
        scheduler=scheduler,
        guild_id=guild_id,
        allowed_channels=allowed_channels,
    )

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(client.start(token))
        except Exception:
            log.exception("Discord bot crashed.")
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name="discord-bot")
    t.start()
    return t
