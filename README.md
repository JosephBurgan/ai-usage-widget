# AI Usage Widget


A small always-on-top floating window for Windows that shows your Claude.ai plan usage (5-hour, 7-day, Sonnet-only, extra credits) and, when the Grok CLI is already signed in on this PC, your Grok usage too. Switch with the Claude / Grok tabs. An account that is not signed in does not get a tab and does not get a login prompt. Weekly bars have a notch at each day boundary. Refreshes on a timer (configurable).

![widget](docs/screenshot.png)

## Features

- Always-on-top floating window — drag by the title bar to position
- Live progress bars colored by usage level (green / orange / red)
- Countdown to each limit's reset
- Click the pin (📌) to toggle always-on-top
- Click the gear (⚙) to enter manage mode — hide rows you don't care about
- Hidden-row preferences persist across restarts
- Tray icon shows live usage as a number, a colored ring, or both. Pick the meter with
  the dot next to any bar in settings mode; turn it off under ≡ → Tray icon
- Optional taskbar readout (experimental, off by default): a ring, the percentage and the
  reset countdown on the taskbar just left of the tray. Turn it on under ≡ → Tray icon →
  "Readout on taskbar". Windows has no official way to do this, so it may hide itself
  after a Windows update; the tray icon keeps working either way

## Requirements

- Windows 10 or 11
- Python 3.10+ ([install from python.org](https://www.python.org/downloads/windows/) — make sure "Add to PATH" is checked)
- [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) authenticated at least once
  (the widget reads tokens from `~/.claude/.credentials.json`)

## Install

```powershell
git clone https://github.com/JosephBurgan/ai-usage-widget.git
cd ai-usage-widget
powershell -ExecutionPolicy Bypass -File setup.ps1
```

The setup script installs the Python dependency, creates Start Menu and Startup
shortcuts (with the widget icon), and launches the widget.

Make sure you've run `claude` at least once and logged in — the widget needs the
credentials file that Claude Code creates at `%USERPROFILE%\.claude\.credentials.json`
(or under `CLAUDE_CONFIG_DIR` if you set it). Being signed in on claude.ai in a browser
or the Claude desktop app is not enough. If the Claude tab says "Not signed in", click
**Sign in**: it opens Claude Code in a terminal; run `/login` there and the widget
picks up the new login on its own.

## Update

Click ☰ on the widget → **Check for updates** → if available, click **Update**.
That runs `git pull` and relaunches the widget automatically.

Or manually:

```powershell
cd C:\path\to\ai-usage-widget
git pull
```

## How it works

- Reads the OAuth access + refresh tokens from `~/.claude/.credentials.json`
- Refreshes the access token automatically via `POST https://api.anthropic.com/v1/oauth/token`
  when it's close to expiring
- Polls `GET https://api.anthropic.com/api/oauth/usage` every 30 s with
  `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20`
- Writes refreshed tokens back to the credentials file

If the refresh token ever gets invalidated (e.g. you logged out elsewhere), the widget
will show "Not logged in" — run `claude` once, send any message, and the tokens will
be refreshed.

Per-window hidden-row preferences are stored at
`%USERPROFILE%\.claude_widget_settings.json`.

## License

MIT
