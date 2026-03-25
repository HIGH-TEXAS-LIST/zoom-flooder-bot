# Zoom Flooder Bot

Automated Zoom meeting joiner with a web-based dashboard. Launches headless Chrome instances that join a meeting, optionally send a chat message to a specific participant, and leave.

## Features

- **Web Dashboard** -- real-time control panel at `http://localhost:5000` with live logs, per-bot status, and start/stop controls.
- **Batch Launching** -- spin up multiple bots in parallel with configurable thread count.
- **Chat Messaging** -- send a message on join, optionally targeting a specific participant via the "To" dropdown.
- **Auto-Restart** -- cycle bots repeatedly on a configurable delay.
- **Proxy Rotation** -- route each bot through a different proxy (`proxies.txt`).
- **Random Names** -- pull from a customizable name list (`names.txt`).
- **Headless Chrome** -- runs in the background using Selenium + ChromeDriver (auto-managed).

## Quick Start

### Prerequisites

- Python 3.9+
- Google Chrome (latest stable)

### Install

```bash
pip install -r requirements.txt
```

Or on Windows, double-click **`start.bat`** -- it will install dependencies automatically and open the dashboard.

### Run

**Web dashboard (recommended):**

```bash
python web_app.py
```

Then open `http://localhost:5000` in your browser.

**CLI mode:**

```bash
python main.py
```

## Configuration

### Dashboard Fields

| Field | Description |
|---|---|
| Meeting ID | Zoom meeting ID (digits only) |
| Passcode | Meeting passcode (leave blank if none) |
| Thread Count | Max simultaneous bot launches |
| Number of Bots | Total bots to launch per cycle |
| Bot Name | Fixed name for all bots, or leave blank for random |
| Chat Recipient | Send DM to this participant name (blank = Everyone) |
| Chat Message | Message to send on join (blank = no message) |
| Use Proxies | Route bots through `proxies.txt` |
| Auto-Restart | Re-launch bots on a loop with configurable delay |

### Optional Files

| File | Purpose |
|---|---|
| `names.txt` | One name per line -- bots pick randomly from this list |
| `proxies.txt` | One proxy per line (`http://host:port`, `socks5://host:port`, or `http://user:pass@host:port`) |
| `default.txt` | Pre-fill defaults: line 1 = thread count, line 2 = meeting ID, line 3 = passcode |

## Project Structure

```
zoom-flooder-bot/
  bot.py            # Core Selenium logic: join, chat, leave
  bot_manager.py    # Thread pool orchestration, lifecycle management
  browser.py        # ChromeDriver setup (headless, proxy, incognito)
  config.py         # Config loading (names, proxies, defaults)
  main.py           # CLI entry point
  web_app.py        # Flask + SocketIO web dashboard
  start.bat         # Windows one-click launcher
  names.txt         # Bot name pool
  requirements.txt  # Python dependencies
  static/
    app.js          # Dashboard frontend JS
    style.css       # Dashboard styles
  templates/
    dashboard.html  # Dashboard HTML template
```

## How It Works

1. **`web_app.py`** (or `main.py`) builds a config dict and passes it to `BotManager`.
2. **`BotManager`** splits bots into batches and launches them in a `ThreadPoolExecutor`.
3. Each bot thread calls **`launch_bot()`** in `bot.py`, which:
   - Creates a headless Chrome instance via `browser.py`
   - Navigates to the Zoom web client join page
   - Fills in name/passcode and clicks Join
   - Dismisses cookie banners and recording notices
   - Optionally opens chat, selects a recipient, and sends a message
   - Clicks the Leave button and quits the browser
4. Status updates stream back to the dashboard via **SocketIO** websockets.

## Troubleshooting

- **ChromeDriver version mismatch** -- `webdriver-manager` handles this automatically. If it fails, make sure Chrome is up to date.
- **Chat button not found** -- Zoom's web UI changes frequently. The bot uses multiple selector strategies and JS fallbacks. Check `screenshots/` for debug images.
- **Bots fail to join** -- Verify the meeting ID/passcode. The host may need to enable "Allow participants to join before host" or disable the waiting room.
- **High RAM usage** -- Each Chrome instance uses ~200 MB. Reduce bot count or thread count if your system is constrained.

## License

MIT -- see [LICENSE](LICENSE).
