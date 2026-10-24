# AI Usage Widget


A small always-on-top floating window for Windows that shows your Claude.ai plan usage (5-hour, 7-day, Sonnet-only, extra credits) and your Grok usage. Switch with the Claude / Grok tabs. Claude and Grok are on by default. A checked account keeps its tab, and a tab that is not signed in shows a sign-in prompt. Uncheck an account in settings to hide its tab. Weekly bars have a notch at each day boundary. Refreshes on a timer (configurable).

![widget](docs/screenshot.png)

## Features

- Always-on-top floating window — drag by the title bar to position
- Live progress bars colored by usage level (green / orange / red)
- Grok's weekly limit can instead be one stacked bar: each product is a colored
  segment, and the same colors repeat in the legend under the bar. **Color-code
  categories** sits at the bottom of the Grok tab (on by default). Uncheck it for
  the usual green / orange / red bars. The Claude tab is unchanged
- Countdown to each limit's reset
- Click the pin (📌) to toggle always-on-top
- Click the gear (⚙) to enter manage mode — hide rows you don't care about
- Hidden-row preferences persist across restarts
- Tray icon shows live usage as a number, a colored ring, or both. Pick the meter with
  the dot next to any row in settings mode; turn it off under ≡ → Tray icon
- Optional taskbar readout (off by default): a ring, the percentage and the
  reset countdown on the taskbar just left of the tray. Turn it on under ≡ → Tray icon →
  "Readout on taskbar". Windows has no official way to do this, so it may hide itself
  after a Windows update; the tray icon keeps working either way
- Launch on startup, under ≡ → Window. While that is checked, **Taskbar icon only**
  sits indented under it. Leave it unchecked to open the widget window at sign-in.
  Check it to start with no floating window: the tray icon stays, and the taskbar
  readout stays too when that is turned on. Click the tray icon to open the window.
  Unchecking Launch on startup hides Taskbar icon only.
- The floating window and Settings stay inside the monitor's work area, a few
  pixels clear of the taskbar. That applies when they open, when their height
  changes, and when a saved position is restored. A taskbar on any edge is
  covered, including on a second monitor. The taskbar readout is the exception:
  it is supposed to sit on the taskbar

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

The setup script installs the Python dependency, creates a Start Menu shortcut
(with the widget icon), and launches the widget. It also follows Launch on startup:
a new install turns that on and leaves Taskbar icon only off, so the widget opens
when you sign in. Run setup again later and it keeps the choices already saved in
`%USERPROFILE%\.claude_widget_settings.json`.

Make sure you've run `claude` at least once and logged in — the widget needs the
credentials file that Claude Code creates at `%USERPROFILE%\.claude\.credentials.json`
(or under `CLAUDE_CONFIG_DIR` if you set it). Being signed in on claude.ai in a browser
or the Claude desktop app is not enough. If the Claude tab says "Not signed in", click
**Sign in**: it opens Claude Code in a terminal; run `/login` there and the widget
picks up the new login on its own.

Grok usage is read from the Grok CLI credentials at `%USERPROFILE%\.grok\auth.json`.
Being signed in on grok.com in a browser is not enough. If the Grok tab says
"Not signed in", click **Sign in**: it opens a PowerShell window and runs
`grok login`. The window stays open through the sign-in and waits for Enter
before it closes, including when something fails. Finish sign-in there and the
widget picks up the new credentials on its own. If the Grok CLI is not installed,
the tab says so and **Sign in** opens PowerShell to install it
(`irm https://x.ai/cli/install.ps1 | iex`) and then runs `grok.exe` by its full
path (`%USERPROFILE%\.grok\bin\grok.exe`).

## Grok rows

Weekly is the whole credit pool for this period (`creditUsagePercent`), with the
reset countdown under it. The products under it (Grok Bot, Cursor, Voice, Build,
Chat, Imagine, API, and any other product the account reports) come from
`productUsage`. They are slices of that same pool: their percents add up to about
the weekly percent (a point or two of difference is rounding).

With **Color-code categories** checked, Weekly is one bar. Each product is a
colored segment, and the dark remainder is still unused. Under the bar, each
product is a legend row: a matching color square, the name, and that product's
percent. Those rows do not have their own bars. Uncheck **Color-code categories**
and every Grok row goes back to a single bar in green, orange, or red, based on
how full it is. Those colors mean the usage level, not which product it is.
The checkbox is on the Grok tab, under the last row and above Refresh. The
choice is saved in `%USERPROFILE%\.claude_widget_settings.json`.

**Bot app** is the Grok Bot desktop app's reading of that same weekly limit
(`GetSandUsageStatus`). When its percent and reset match Weekly, it is not
another slice of the pool. It starts hidden. Show it from manage mode (≡) and
it appears as a short note, "same weekly limit", not as its own bar or segment.
If its reset does not match, it is a separate limit and keeps its own bar and
countdown.

**On-demand** is spend against a separate dollar cap. **Prepaid** is prepaid
balance still left. Neither one is part of the weekly stack.

Hide a row from manage mode (≡) and it drops out of the stack and the legend.
The weekly percent stays the real total. The dot next to a row still picks the
tray meter.

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

Hidden rows, launch on startup, and taskbar-icon-only startup are stored at
`%USERPROFILE%\.claude_widget_settings.json`.

## License

MIT
