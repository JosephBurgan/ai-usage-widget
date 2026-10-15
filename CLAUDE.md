# ai-usage-widget — project standards

GitHub: https://github.com/JosephBurgan/ai-usage-widget
The checkout on this PC is `C:\Users\Joseph\ai-usage-widget`.

Standing rules for any agent working in this repo. Read before editing or pushing.

For architecture, file layout, OAuth flow, settings shape, and known sharp edges, see `README.md` and the most recent handoff doc.

## Versioning — non-negotiable

**Every commit that ships to users gets a new version tag. No exceptions.**

- SemVer. `vMAJOR.MINOR.PATCH`.
  - **PATCH** (`v1.0.0` → `v1.0.1`) — bug fix, UX polish, internal refactor, no behavior change for existing users.
  - **MINOR** (`v1.0.x` → `v1.1.0`) — new user-visible feature, added setting, new banner/button.
  - **MAJOR** (`v1.x.x` → `v2.0.0`) — breaking change: settings file shape changes incompatibly, removed feature, requires manual user action.
- Tag is annotated, message = same as commit subject:
  ```
  git tag -a vX.Y.Z -m "<one-line summary>"
  git push origin main --follow-tags
  ```
- Push commit and tag together. A commit without a tag is a bug.
- The widget shows the tag via `git describe --tags`. Untagged commits display as `tag+N` — that's a smell, not a release.
- The self-update flow (`check_for_update`) compares against the remote tag. If you push code without a tag, **users won't get the update**.

Before every push, state the chosen version and bump category to the user and wait for OK if not obvious.

## Push workflow

Hard rule: **never push without explicit user approval**.

Order:
1. Edit `ai-usage-widget\claude_widget.py` (the repo copy).
2. `python -m py_compile` it. No syntax errors before continuing.
3. `Copy-Item` repo file → `C:\Users\Joseph\claude_widget.py` (live runtime).
4. Kill `pythonw*`, relaunch via `wscript` on the `.vbs`.
5. Ask Joseph to verify visually. Screenshots are how he reviews.
6. Only after OK: `git add` (specific files, not `-A`), commit, tag, push.

## Commit style

- One-line, imperative subject. No body unless genuinely needed.
- **No Claude co-author trailer.** House style.
- Match the existing `git log --oneline` voice ("Show semantic version…", "Add one-click…", "Pop settings out…").

## Code style

- Terse. No fluff comments. Comments only when the *why* isn't obvious from the code.
- Joseph reads code and screenshots, not diffs. Explain changes in 3-5 plain-English bullets.
- Minimal blue. Buttons gray (`BTN_BG`/`BTN_BG_HOVER`), white text. No accent colors except the green/orange/red usage bars.
- All UI mutations via `_after_safe()` so destroyed-window callbacks no-op.
- No log files. Nothing on disk beyond `~/.claude_widget_settings.json`.

## Sync target

- Repo: `C:\Users\Joseph\ai-usage-widget\` — edit here.
- Live runtime: `C:\Users\Joseph\claude_widget.py` — keep in sync after every edit.
- VBS launcher: `claude_widget.vbs` (self-locating, no console flash).

## Tools

- PowerShell, not Bash.
- `gh` CLI authenticated as `JosephBurgan`.
- Python 3.13 (Windows Store). Use `python` not `py`.
