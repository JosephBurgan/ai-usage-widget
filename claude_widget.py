"""Usage widget — floating always-on-top window for Claude, Grok, and ChatGPT plan usage.

Claude reads ~/.claude/.credentials.json. Grok reads ~/.grok/auth.json.
Tabs show only when the account is checked in settings. Sign in / Sign out
are in settings; an unchecked account never gets a tab.
"""

from __future__ import annotations

import base64
import bisect
import ctypes
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import colorchooser
import webbrowser
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.exceptions import HTTPError

# ── Paths ────────────────────────────────────────────────────────────────────

HOME          = Path.home()
CLAUDE_DIR    = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
CREDS_FILE    = CLAUDE_DIR / ".credentials.json"
GROK_AUTH     = HOME / ".grok" / "auth.json"
SETTINGS_FILE = HOME / ".claude_widget_settings.json"
CRASH_LOG     = HOME / ".claude_widget_crash.log"
REPO_DIR     = Path(__file__).resolve().parent


# ── Atomic JSON I/O ──────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON via temp-file + os.replace so a crash mid-write can't corrupt the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup; original file is untouched.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Settings ─────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    try:
        return _read_json(SETTINGS_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(s: dict) -> None:
    _write_json_atomic(SETTINGS_FILE, s)


# Rows hidden by default on first launch (user can re-enable from settings).
# "Bot app" repeats the Grok weekly pool, so it starts hidden too.
DEFAULT_HIDDEN = ["7-day Sonnet", "Extra credits", "Bot app"]


# ── OAuth / API ──────────────────────────────────────────────────────────────

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
API_HDRS  = {"anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}

# Refresh the access token when it's within this many ms of expiry.
TOKEN_REFRESH_MARGIN_MS = 5 * 60 * 1000


def _load_creds() -> dict:
    return _read_json(CREDS_FILE)["claudeAiOauth"]


def _save_creds_update(oauth_patch: dict) -> None:
    root = _read_json(CREDS_FILE)
    root["claudeAiOauth"].update(oauth_patch)
    _write_json_atomic(CREDS_FILE, root)


def get_fresh_token() -> str:
    """Return a valid access token, refreshing it if it's near expiry."""
    creds = _load_creds()
    if creds["expiresAt"] - time.time() * 1000 > TOKEN_REFRESH_MARGIN_MS:
        return creds["accessToken"]

    r = requests.post(
        TOKEN_URL,
        json={
            "grant_type":    "refresh_token",
            "client_id":     CLIENT_ID,
            "refresh_token": creds["refreshToken"],
            "scope":         " ".join(creds.get("scopes", [])),
        },
        headers={"Content-Type": "application/json", **API_HDRS},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()

    _save_creds_update({
        "accessToken":  data["access_token"],
        "refreshToken": data.get("refresh_token", creds["refreshToken"]),
        "expiresAt":    int(time.time() * 1000) + data["expires_in"] * 1000,
    })
    return data["access_token"]


def fetch_usage() -> dict:
    token = get_fresh_token()
    r = requests.get(
        USAGE_URL,
        headers={"Authorization": f"Bearer {token}", **API_HDRS},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def is_auth_error(exc: BaseException) -> bool:
    """True if the exception represents an auth/credentials problem (vs network/server)."""
    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, HTTPError) and exc.response is not None:
        return exc.response.status_code in (400, 401, 403)
    return False


def claude_connected() -> bool:
    """True when Claude Code has a saved sign-in. Missing file is not an error."""
    try:
        oauth = _read_json(CREDS_FILE).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError, KeyError):
        return False
    return bool(oauth.get("accessToken") and oauth.get("refreshToken"))


def claude_plan_label() -> str:
    """Short plan name from the saved Claude sign-in, e.g. 'Max 20x'."""
    try:
        oauth = _read_json(CREDS_FILE).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError, KeyError):
        return ""
    tier = str(oauth.get("rateLimitTier") or "").lower()
    kind = str(oauth.get("subscriptionType") or "").lower()
    if "20x" in tier:
        return "Max 20x"
    if "5x" in tier:
        return "Max 5x"
    names = {
        "pro": "Pro",
        "max": "Max",
        "team": "Team",
        "enterprise": "Enterprise",
        "free": "Free",
    }
    if kind in names:
        return names[kind]
    if "pro" in tier:
        return "Pro"
    if "team" in tier:
        return "Team"
    if "enterprise" in tier:
        return "Enterprise"
    return ""


# ── Grok (optional second source) ────────────────────────────────────────────

GROK_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
GROK_BILLING   = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS  = "https://cli-chat-proxy.grok.com/v1/settings"
GROK_AUTH_HDR  = {"x-xai-token-auth": "xai-grok-cli", "Accept": "application/json"}
BOT_USAGE_URL  = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus"
BOT_LABEL      = "Bot app"
GROK_TOKEN_REFRESH_MARGIN_S = 5 * 60

GROK_PRODUCT_LABELS = {
    "GrokChat":    "Chat",
    "GrokBuild":   "Build",
    "GrokImagine": "Imagine",
    "GrokVoice":   "Voice",
    "GrokAPI":     "API",
    "GrokBot":     "Grok Bot",
    "Cursor":      "Cursor",
    "GrokCursor":  "Cursor",
}

# High-contrast hues for the stacked weekly bar. Grok Bot is the main product
# so it gets green; the rest stay off orange/red so those still mean "how full".
GROK_SLICE_COLORS = {
    "Chat":     "#4C9AFF",  # blue
    "Voice":    "#E255D0",  # magenta — far from Chat
    "Grok Bot": "#3DDC84",  # green — the main product
    "Cursor":   "#9B6BFF",  # purple
    "Build":    "#F2C94C",  # yellow
    "Imagine":  "#2ED8C3",  # teal
    "API":      "#8FA3BF",  # light gray-blue — distinct from Chat and Grok Bot
}
GROK_SLICE_FALLBACK = (
    "#FF6B9D", "#5AD0FF", "#C77DFF", "#FFD166", "#3DDC97", "#FF9F1C", "#7A89FF",
)


def grok_connected() -> bool:
    """True when the Grok CLI has a saved sign-in. Missing file is not an error."""
    try:
        _pick_grok_auth(_read_json(GROK_AUTH))
    except (OSError, json.JSONDecodeError, KeyError):
        return False
    return True


CHATGPT_AUTH = HOME / ".chatgpt_widget_auth.json"


def chatgpt_connected() -> bool:
    """True when the widget has a saved ChatGPT sign-in. Not built yet."""
    try:
        data = _read_json(CHATGPT_AUTH)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("accessToken") or data.get("access_token"))


CREDS_WATCH_S = 10 * 60


def open_claude_login() -> bool:
    """Open Claude Code in a new console so the user can log in. False if it isn't installed."""
    exe = shutil.which("claude")
    if not exe:
        return False
    try:
        subprocess.Popen([exe], cwd=str(HOME),
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError:
        return False
    return True


GROK_INSTALL_URL = "https://x.ai/cli/install.ps1"


def find_grok_cli() -> str | None:
    """Path to the Grok CLI, or None when it is not installed.

    The official Windows installer puts grok.exe in ~/.grok/bin and only then
    updates the user PATH, which a widget already running will not see.
    """
    names = ("grok.exe", "grok.cmd", "grok")
    folders: list[Path] = []
    env_dir = os.environ.get("GROK_BIN_DIR")
    if env_dir:
        folders.append(Path(env_dir))
    folders.append(HOME / ".grok" / "bin")
    appdata = os.environ.get("APPDATA")
    if appdata:
        folders.append(Path(appdata) / "npm")
    for folder in folders:
        for name in names:
            path = folder / name
            if path.is_file():
                return str(path)
    return shutil.which("grok")


def grok_auth_mtime() -> int | None:
    try:
        return GROK_AUTH.stat().st_mtime_ns
    except OSError:
        return None


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _grok_console_tail() -> str:
    """Leave the window up so a fast failure can be read, then close on Enter."""
    return (
        "Write-Host ''\n"
        "Write-Host 'Press Enter to close this window.'\n"
        "[void](Read-Host)\n"
        "exit\n"
    )


def _open_visible_powershell(script: str) -> bool:
    """Run script in a new PowerShell window. False if the shell cannot start.

    -Command is wrong here. CreateProcess quotes the script, PowerShell treats
    that as a double-quoted string and expands $variables before it runs, and
    the process exits before Read-Host so the console only flashes.
    -EncodedCommand carries the script unchanged. -NoExit keeps the window
    open when PowerShell itself errors before the script starts; the script
    exits after Enter on the normal path.
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-NoExit",
                "-EncodedCommand", encoded,
            ],
            cwd=str(HOME),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    except OSError:
        return False
    return True


def open_grok_login() -> bool:
    """Open a visible window running `grok login`. False if the CLI is not installed."""
    exe = find_grok_cli()
    if not exe:
        return False
    return _open_visible_powershell(f"& {_ps_quote(exe)} login\n" + _grok_console_tail())


def open_grok_install_and_login() -> bool:
    """Visible PowerShell: official installer, then `grok login` by full path.

    The installer adds grok to the user PATH, which this window does not have yet.
    """
    url = _ps_quote(GROK_INSTALL_URL)
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "  Write-Host 'Installing the Grok CLI...'\n"
        f"  irm {url} | iex\n"
        "  $grok = if ($env:GROK_BIN_DIR) { Join-Path $env:GROK_BIN_DIR 'grok.exe' } "
        "else { Join-Path $env:USERPROFILE '.grok\\bin\\grok.exe' }\n"
        "  if (-not (Test-Path -LiteralPath $grok)) {\n"
        "    throw ('Installer finished but ' + $grok + ' was not found.')\n"
        "  }\n"
        "  Write-Host ''\n"
        "  Write-Host 'Starting Grok sign-in...'\n"
        "  & $grok login\n"
        "} catch {\n"
        "  Write-Host ''\n"
        "  Write-Host ('Could not sign in: ' + $_.Exception.Message)\n"
        "  Write-Host 'Install by hand in PowerShell:'\n"
        "  Write-Host '  irm https://x.ai/cli/install.ps1 | iex'\n"
        "  Write-Host '  grok login'\n"
        "}\n"
        + _grok_console_tail()
    )
    return _open_visible_powershell(script)


def source_has_creds(name: str) -> bool:
    if name == "claude":
        return claude_connected()
    if name == "grok":
        return grok_connected()
    if name == "chatgpt":
        return chatgpt_connected()
    return False


LOGIN_URLS = {
    "claude":  "https://claude.com/product/claude-code",
    "chatgpt": "https://chatgpt.com",
}

LOGIN_HINTS = {
    "claude":  "Reads your Claude Code login,\nnot the claude.ai website.\nSign in opens Claude Code in a\nterminal; run /login there.",
    "grok":    "Sign in opens a terminal and runs\ngrok login. Finish it there and\nthis tab updates on its own.",
    "chatgpt": "Sign in on chatgpt.com.\nUsage meters for ChatGPT\nare not wired up yet.",
}

GROK_INSTALL_HINT = (
    "The Grok CLI isn't installed.\n"
    "Sign in opens PowerShell, installs\n"
    "it from x.ai, then runs grok login."
)

GROK_LOGIN_FAILED = (
    "Couldn't open PowerShell.\n"
    "Install the Grok CLI, then run grok login:\n"
    "irm https://x.ai/cli/install.ps1 | iex"
)


def _parse_expires_at(raw: object) -> float | None:
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) / 1000.0 if raw > 1e12 else float(raw)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pick_grok_auth(root: dict) -> tuple[str, dict]:
    best: tuple[str, dict] | None = None
    best_exp = -1.0
    for key, entry in root.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("key") or not entry.get("refresh_token"):
            continue
        exp = _parse_expires_at(entry.get("expires_at")) or 0.0
        score = exp + (1e12 if "auth.x.ai" in key else 0.0)
        if score > best_exp:
            best_exp = score
            best = (key, entry)
    if best is None:
        raise FileNotFoundError(GROK_AUTH)
    return best


def _grok_token_url(entry: dict) -> str:
    issuer = (entry.get("oidc_issuer") or "https://auth.x.ai").rstrip("/")
    return f"{issuer}/oauth2/token"


def _grok_client_id(entry: dict, map_key: str) -> str:
    if entry.get("oidc_client_id"):
        return str(entry["oidc_client_id"])
    if "::" in map_key:
        return map_key.rsplit("::", 1)[-1]
    return GROK_CLIENT_ID


def _save_grok_auth(map_key: str, patch: dict) -> None:
    root = _read_json(GROK_AUTH)
    if map_key not in root or not isinstance(root[map_key], dict):
        raise FileNotFoundError(GROK_AUTH)
    root[map_key].update(patch)
    _write_json_atomic(GROK_AUTH, root)


def grok_access_token() -> str:
    root = _read_json(GROK_AUTH)
    map_key, entry = _pick_grok_auth(root)
    exp = _parse_expires_at(entry.get("expires_at"))
    if exp is not None and exp - time.time() > GROK_TOKEN_REFRESH_MARGIN_S:
        return str(entry["key"])

    r = requests.post(
        _grok_token_url(entry),
        data={
            "grant_type":    "refresh_token",
            "client_id":     _grok_client_id(entry, map_key),
            "refresh_token": entry["refresh_token"],
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    expires_in = int(data.get("expires_in") or 21600)
    expires_at = datetime.fromtimestamp(
        time.time() + expires_in, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    _save_grok_auth(map_key, {
        "key":           data["access_token"],
        "refresh_token": data.get("refresh_token", entry["refresh_token"]),
        "expires_at":    expires_at,
    })
    return data["access_token"]


def _roaming_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return HOME / "AppData" / "Roaming"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_decrypt(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data)
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _bot_aes_key() -> bytes:
    local_state = _roaming_dir() / "Grok Bot" / "Local State"
    raw = json.loads(local_state.read_text(encoding="utf-8"))
    enc_key = base64.b64decode(raw["os_crypt"]["encrypted_key"])
    if not enc_key.startswith(b"DPAPI"):
        raise ValueError("Unexpected Grok Bot encryption key")
    return _dpapi_decrypt(enc_key[5:])


def _decrypt_v10(value: str, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(value)
    if not raw.startswith(b"v10") or len(raw) < 3 + 12 + 16:
        raise ValueError("Unexpected Grok Bot secret format")
    nonce, ct = raw[3:15], raw[15:]
    return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")


def _open_secret(value: str, key: bytes) -> str:
    if value.startswith("eyJ") and value.count(".") == 2:
        return value
    return _decrypt_v10(value, key)


def fetch_grok_bot_usage() -> dict | None:
    """Best-effort Bot row. No Grok Bot sign-in just omits the row."""
    secrets = _roaming_dir() / "Grok Bot" / "sand-secrets.json"
    if not secrets.is_file():
        return None
    try:
        root = _read_json(secrets)
        accounts_raw = root.get("cursor-accounts")
        accounts = json.loads(accounts_raw) if isinstance(accounts_raw, str) else accounts_raw
        if not isinstance(accounts, dict):
            return None
        active = accounts.get("active")
        account = (accounts.get("accounts") or {}).get(active)
        if not isinstance(account, dict):
            return None
        token = _open_secret(str(account.get("cursor-access-token") or ""), _bot_aes_key())
        r = requests.post(
            BOT_USAGE_URL,
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "x-cursor-client-type": "sand",
                "x-cursor-client-version": "sand-desktop",
                "x-ghost-mode": "true",
            },
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        pct = float(body["usagePercent"])
    except Exception:
        return None
    return {
        "usagePercent": pct,
        "periodStart": body.get("currentPeriodStart") or "",
        "nextReset": body.get("nextResetTimestampUtc") or "",
    }


def fetch_grok_usage() -> dict:
    token = grok_access_token()
    headers = {"Authorization": f"Bearer {token}", **GROK_AUTH_HDR}
    r = requests.get(GROK_BILLING, headers=headers, timeout=15)
    r.raise_for_status()
    payload = r.json()
    try:
        s = requests.get(GROK_SETTINGS, headers=headers, timeout=5)
        if s.ok:
            tier = (s.json() or {}).get("subscription_tier_display")
            if tier:
                payload["_tier_display"] = tier
    except requests.RequestException:
        pass
    bot = fetch_grok_bot_usage()
    if bot:
        payload["_bot"] = bot
    return payload


# ── Self-update via git ──────────────────────────────────────────────────────

def _git(*args: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run `git` in the repo directory, capturing stdout/stderr as text."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        # Hide the console window that would otherwise flash on Windows
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def is_git_checkout() -> bool:
    return (REPO_DIR / ".git").exists()


def current_version() -> str:
    """Human-readable version: the latest tag (e.g. `v1.0.0`), or `<tag>+N` if
    the working tree is N commits past it, or a short SHA if no tags exist."""
    if not is_git_checkout():
        return "unknown"
    try:
        r = _git("describe", "--tags", "--always", "--abbrev=7")
        if r.returncode != 0 or not r.stdout.strip():
            return "unknown"
        desc = r.stdout.strip()
        # `git describe --tags` returns either an exact tag (e.g. `v1.0.0`),
        # `<tag>-<n>-g<sha>` (post-tag commit), or a bare short SHA.
        # Rewrite `<tag>-<n>-g<sha>` to `<tag>+<n>` for a cleaner display.
        parts = desc.rsplit("-", 2)
        if len(parts) == 3 and parts[2].startswith("g"):
            return f"{parts[0]}+{parts[1]}"
        return desc
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def check_for_update() -> tuple[str, str]:
    """Return (status, detail). status ∈ {'available', 'current', 'error'}."""
    if not is_git_checkout():
        return "error", "Not a git checkout — manual update only"
    try:
        fetch = _git("fetch", "--quiet", "origin")
        if fetch.returncode != 0:
            return "error", (fetch.stderr or "git fetch failed").strip()
        local  = _git("rev-parse", "HEAD").stdout.strip()
        remote = _git("rev-parse", "origin/main").stdout.strip()
        if not remote:
            return "error", "Could not resolve origin/main"
        if local == remote:
            return "current", ""
        short = _git("rev-parse", "--short", "origin/main").stdout.strip()
        return "available", short
    except FileNotFoundError:
        return "error", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return "error", "git timed out"


def install_update() -> str | None:
    """Run `git pull --ff-only` then `pip install -r requirements.txt`.

    Returns None on success, error string otherwise.
    """
    try:
        r = _git("pull", "--ff-only", "origin", "main", timeout=30)
    except FileNotFoundError:
        return "git not found on PATH"
    except subprocess.TimeoutExpired:
        return "git pull timed out"
    if r.returncode != 0:
        return (r.stderr or r.stdout or "git pull failed").strip()

    # Sync Python dependencies in case requirements.txt changed.
    req = REPO_DIR / "requirements.txt"
    if req.exists():
        # Use python.exe rather than pythonw.exe so pip has stdio.
        py = Path(sys.executable).with_name("python.exe")
        if not py.exists():
            py = Path(sys.executable)
        try:
            pip = subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet", "--user",
                 "-r", str(req)],
                cwd=REPO_DIR,
                capture_output=True, text=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if pip.returncode != 0:
                err = (pip.stderr or pip.stdout or "?").strip()
                return f"pip install failed: {err[:200]}"
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return f"pip install error: {exc}"
    return None


APP_NAME = "AI Usage Widget"
# Startup-folder shortcut only. A Start Menu launch omits this so the window opens.
STARTUP_ARG = "--from-startup"
STARTUP_LNK_REV = 1


def _startup_dir() -> Path:
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def startup_shortcut_path() -> Path:
    return _startup_dir() / f"{APP_NAME}.lnk"


def _migrate_startup_shortcut() -> None:
    """Rename the old Claude shortcut so 'Launch on startup' stays in sync."""
    old = _startup_dir() / "Claude Usage Widget.lnk"
    new = startup_shortcut_path()
    if old.exists() and not new.exists():
        try:
            old.rename(new)
        except OSError:
            pass


def is_startup_enabled() -> bool:
    _migrate_startup_shortcut()
    return startup_shortcut_path().exists()


def set_startup_enabled(enabled: bool) -> str | None:
    """Create or remove the Startup shortcut. None on success, error string otherwise."""
    path = startup_shortcut_path()
    if not enabled:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            return str(exc)
        return None

    vbs  = REPO_DIR / "claude_widget.vbs"
    icon = REPO_DIR / "claude_widget.ico"
    if not vbs.exists():
        return f"Launcher not found: {vbs}"
    ps = (
        f'$sh = New-Object -ComObject WScript.Shell;'
        f'$lnk = $sh.CreateShortcut("{path}");'
        f'$lnk.TargetPath = "wscript.exe";'
        f"$lnk.Arguments = '\"{vbs}\" {STARTUP_ARG}';"
        f'$lnk.WorkingDirectory = "{REPO_DIR}";'
        + (f'$lnk.IconLocation = "{icon}";' if icon.exists() else "")
        + '$lnk.Description = "Floating AI usage widget";'
          '$lnk.Save()'
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if r.returncode != 0:
        return (r.stderr or "Failed to create shortcut").strip()
    return None


def launched_from_startup() -> bool:
    return STARTUP_ARG in sys.argv


def seed_startup_settings_file(path: Path, shortcut: Path) -> tuple[bool, bool]:
    """Fill launch prefs when they are missing and return (launch, tray_only).

    A new settings file turns Launch on startup on and leaves the window open.
    An existing file keeps whatever the Startup shortcut already says.
    """
    had = path.exists()
    try:
        data = _read_json(path) if had else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    changed = False
    if "launch_on_startup" not in data:
        data["launch_on_startup"] = True if not had else shortcut.exists()
        changed = True
    if "startup_tray_only" not in data:
        data["startup_tray_only"] = False
        changed = True
    if data.get("launch_on_startup") and "startup_lnk_rev" not in data:
        data["startup_lnk_rev"] = STARTUP_LNK_REV
        changed = True
    if changed:
        _write_json_atomic(path, data)
    return bool(data["launch_on_startup"]), bool(data["startup_tray_only"])


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.OpenEventW.restype = wintypes.HANDLE
_k32.SetEvent.argtypes = (wintypes.HANDLE,)
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)

INSTANCE_MUTEX = "Local\\ai-usage-widget-instance"
SHOW_EVENT     = "Local\\ai-usage-widget-show"
SHOW_POLL_MS   = 300
_instance_lock = None


def claim_single_instance() -> bool:
    """Take the one-widget lock. False means another widget already runs."""
    global _instance_lock
    _instance_lock = _k32.CreateMutexW(None, False, INSTANCE_MUTEX)
    return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS


def release_single_instance() -> None:
    global _instance_lock
    if _instance_lock:
        _k32.CloseHandle(_instance_lock)
        _instance_lock = None


def signal_running_instance() -> None:
    """Ask the widget that's already running to show itself."""
    h = _k32.OpenEventW(0x0002, False, SHOW_EVENT)   # EVENT_MODIFY_STATE
    if h:
        _k32.SetEvent(h)
        _k32.CloseHandle(h)


def icon_fingerprint() -> str:
    try:
        return hashlib.sha1((REPO_DIR / "claude_widget.ico").read_bytes()).hexdigest()
    except OSError:
        return ""


def refresh_shell_icons() -> None:
    """Make Explorer redraw our Start menu, taskbar and desktop shortcuts with the current .ico."""
    icon = REPO_DIR / "claude_widget.ico"
    ps = (
        "$sh = New-Object -ComObject WScript.Shell;"
        "$dirs = \"$env:APPDATA\\Microsoft\\Windows\\Start Menu\","
        "\"$env:APPDATA\\Microsoft\\Internet Explorer\\Quick Launch\\User Pinned\\TaskBar\","
        "\"$env:USERPROFILE\\Desktop\";"
        "Get-ChildItem $dirs -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {"
        " $l = $sh.CreateShortcut($_.FullName);"
        f" if ($l.IconLocation -like '{icon}*') {{ $l.Save(); $_.FullName }} }}"
    )
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20, creationflags=no_window,
        )
        notify = ctypes.windll.shell32.SHChangeNotify
        for path in r.stdout.splitlines():
            if path.strip():
                notify(0x00002000, 0x0005, ctypes.c_wchar_p(path.strip()), None)  # UPDATEITEM, PATHW
        notify(0x08000000, 0, None, None)                                          # ASSOCCHANGED
        subprocess.run(["ie4uinit.exe", "-show"], timeout=20, creationflags=no_window)
    except (OSError, subprocess.SubprocessError):
        pass


def relaunch_widget() -> None:
    """Spawn a fresh widget process via the VBS launcher, then exit this one."""
    # Free the lock first so the new process doesn't think we're still running.
    release_single_instance()
    vbs = REPO_DIR / "claude_widget.vbs"
    if vbs.exists():
        subprocess.Popen(["wscript", str(vbs)], close_fds=True)
    else:
        # Fall back to pythonw on the script directly
        pyw = Path(sys.executable).with_name("pythonw.exe")
        cmd = [str(pyw if pyw.exists() else sys.executable), str(REPO_DIR / "claude_widget.py")]
        subprocess.Popen(cmd, close_fds=True)
    os._exit(0)


def retry_after_seconds(exc: BaseException) -> int | None:
    """If exc is a 429, return how many seconds to wait before retrying.

    Honors the Retry-After header when it's a positive integer; falls back
    otherwise (some servers send an invalid `Retry-After: 0`).
    """
    if not (isinstance(exc, HTTPError) and exc.response is not None
            and exc.response.status_code == 429):
        return None
    raw = exc.response.headers.get("Retry-After", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return RATE_LIMIT_FALLBACK_S


# ── Display helpers ───────────────────────────────────────────────────────────

TIER_LABELS = {
    "five_hour":            "5-hour",
    "seven_day":            "7-day",
    "seven_day_opus":       "7-day Opus",
    "seven_day_sonnet":     "7-day Sonnet",
    "seven_day_oauth_apps": "7-day Apps",
    "seven_day_cowork":     "7-day Cowork",
    "seven_day_omelette":   "7-day Pro",
}

# Window duration per tier label, in seconds. Used to render the time-elapsed
# bar under each row. Labels not in this map omit the time bar.
TIER_WINDOW_S = {
    "5-hour":        5 * 3600,
    "7-day":         7 * 86400,
    "7-day Opus":    7 * 86400,
    "7-day Sonnet":  7 * 86400,
    "7-day Apps":    7 * 86400,
    "7-day Cowork":  7 * 86400,
    "7-day Pro":     7 * 86400,
}


def _time_progress(resets_at_iso: str, window_s: int | None) -> float | None:
    """Return 0..1 = fraction of the window elapsed; None if window unknown."""
    if not window_s or not resets_at_iso:
        return None
    try:
        dt = datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    remaining = (dt - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return 1.0
    if remaining >= window_s:
        return 0.0
    return 1.0 - (remaining / window_s)


def _week_notches(window_s: int | None) -> int | None:
    """7 when the bar is a weekly window, so the time bar can mark each day."""
    if not window_s:
        return None
    days = window_s / 86400
    if 6.5 <= days <= 7.5:
        return 7
    return None


def _fmt_countdown(resets_at_iso: str) -> str:
    try:
        dt   = datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
        diff = int((dt - datetime.now(timezone.utc)).total_seconds())
    except ValueError:
        return ""
    if diff <= 0:
        return "resetting…"
    d, rem = divmod(diff, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"resets in {d}d {h}h"
    return f"resets in {h}h {m}m" if h else f"resets in {m}m"


def _fmt_reset_clock(resets_at_iso: str) -> str:
    """Local weekday and clock, e.g. 'Friday 5:00AM'."""
    try:
        dt = datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    local = dt.astimezone()
    hour24 = local.hour
    suffix = "AM" if hour24 < 12 else "PM"
    hour = hour24 % 12 or 12
    return f"{local.strftime('%A')} {hour}:{local.minute:02d}{suffix}"


def _weekly_reset_clock(resets_at_iso: str, window_s: int | None) -> str:
    if _week_notches(window_s) != 7 or not resets_at_iso:
        return ""
    return _fmt_reset_clock(resets_at_iso)


Row = tuple[str, float, str, float | None, int | None, str]
# (label, utilization 0..1, sublabel, time_progress, day notches, weekly reset clock)
# Grok rows may add a 7th field: pool, slice, bot, or bot-same.


def row_kind(row: tuple) -> str:
    return row[6] if len(row) > 6 else ""


def parse_usage(data: dict) -> list[Row]:
    rows: list[Row] = []

    for key, label in TIER_LABELS.items():
        tier = data.get(key)
        if not tier or tier.get("utilization") is None:
            continue
        resets_at = tier.get("resets_at") or ""
        util      = float(tier["utilization"]) / 100.0
        window    = TIER_WINDOW_S.get(label)
        rows.append((
            label, util, _fmt_countdown(resets_at),
            _time_progress(resets_at, window), _week_notches(window),
            _weekly_reset_clock(resets_at, window),
        ))

    extra = data.get("extra_usage")
    if extra and extra.get("is_enabled"):
        used  = float(extra.get("used_credits") or 0)
        limit = float(extra.get("monthly_limit") or 0)
        util  = used / limit if limit > 0 else 0.0
        cur   = extra.get("currency", "USD")
        rows.append(("Extra credits", util, f"{cur} {used:.2f} / {limit:.2f}", None, None, ""))

    return rows


def _period_window_s(period_type: str | None, start: str, end: str) -> int | None:
    t = (period_type or "").upper()
    if "WEEK" in t:
        return 7 * 86400
    if "MONTH" in t:
        return 30 * 86400
    if start and end:
        try:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            secs = int((e - s).total_seconds())
            return secs if secs > 0 else None
        except ValueError:
            return None
    return None


def _period_label(period_type: str | None, window_s: int | None) -> str:
    t = (period_type or "").upper()
    if "WEEK" in t:
        return "Weekly"
    if "MONTH" in t:
        return "Monthly"
    if window_s is not None:
        days = window_s / 86400
        if 4 <= days <= 12:
            return "Weekly"
        if 20 <= days <= 45:
            return "Monthly"
    return "Credits"


def _cents_to_dollars(val_obj: object) -> float:
    if isinstance(val_obj, dict):
        raw = val_obj.get("val", 0)
    else:
        raw = val_obj or 0
    try:
        return float(raw) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _iso_within(a: str, b: str, slack_s: int) -> bool:
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return False
    return abs((da - db).total_seconds()) <= slack_s


def bot_matches_pool(pool_pct: float, pool_end: str, bot: dict) -> bool:
    """True when Bot app is the same weekly pool, not its own limit.

    The desktop app's GetSandUsageStatus reports the account weekly limit:
    same percent and the same reset. A different reset means a separate limit.
    """
    try:
        bot_pct = float(bot["usagePercent"])
    except (KeyError, TypeError, ValueError):
        return False
    if abs(bot_pct - float(pool_pct)) > 1.0:
        return False
    bot_end = str(bot.get("nextReset") or "")
    if pool_end and bot_end:
        return _iso_within(pool_end, bot_end, 180)
    return True


def parse_grok_usage(data: dict) -> tuple[list[Row], str]:
    rows: list[Row] = []
    cfg = data.get("config") or data
    period = cfg.get("currentPeriod") or {}
    start = period.get("start") or cfg.get("billingPeriodStart") or ""
    end   = period.get("end") or cfg.get("billingPeriodEnd") or ""
    window_s = _period_window_s(period.get("type"), start, end)
    label = _period_label(period.get("type"), window_s)

    used_pct = cfg.get("creditUsagePercent")
    if used_pct is None and cfg.get("onDemandCap"):
        cap = _cents_to_dollars(cfg.get("onDemandCap"))
        used = _cents_to_dollars(cfg.get("onDemandUsed"))
        used_pct = (used / cap * 100.0) if cap > 0 else 0.0
    if used_pct is None:
        used_pct = 0.0

    countdown = _fmt_countdown(end) if end else ""
    # 7th field marks the weekly pool. productUsage entries are slices of it.
    rows.append((
        label, float(used_pct) / 100.0, countdown,
        _time_progress(end, window_s) if end else None, _week_notches(window_s),
        _weekly_reset_clock(end, window_s),
        "pool",
    ))

    for item in cfg.get("productUsage") or []:
        pct = item.get("usagePercent")
        if pct is None:
            continue
        product = item.get("product") or ""
        rows.append((
            GROK_PRODUCT_LABELS.get(product, product or "Product"),
            float(pct) / 100.0, "", None, None, "",
            "slice",
        ))

    bot = data.get("_bot") or {}
    if bot.get("usagePercent") is not None:
        bot_end = str(bot.get("nextReset") or "")
        bot_start = str(bot.get("periodStart") or "")
        bot_window = _period_window_s(None, bot_start, bot_end)
        kind = "bot-same" if bot_matches_pool(float(used_pct), end, bot) else "bot"
        rows.append((
            BOT_LABEL, float(bot["usagePercent"]) / 100.0,
            _fmt_countdown(bot_end) if bot_end else "",
            _time_progress(bot_end, bot_window) if bot_end else None,
            _week_notches(bot_window),
            _weekly_reset_clock(bot_end, bot_window),
            kind,
        ))

    on_cap = _cents_to_dollars(cfg.get("onDemandCap"))
    on_used = _cents_to_dollars(cfg.get("onDemandUsed"))
    if on_cap > 0:
        rows.append(("On-demand", min(on_used / on_cap, 1.0), f"$ {on_used:.2f} / {on_cap:.2f}", None, None, ""))

    prepaid = _cents_to_dollars(cfg.get("prepaidBalance"))
    if prepaid > 0:
        rows.append(("Prepaid", 0.0, f"$ {prepaid:.2f} remaining", None, None, ""))

    return rows, str(data.get("_tier_display") or "")


# ── Widget ────────────────────────────────────────────────────────────────────

BG           = "#1a1a1a"
FG           = "#e0e0e0"
FG_DIM       = "#888888"
BAR_BG       = "#333333"
BAR_LOW      = "#4caf50"
BAR_MED      = "#ff9800"
BAR_HIGH     = "#f44336"
ACCENT       = "#7c7c7c"
ACCENT_OFF   = "#444444"
SEPARATOR    = "#333333"
BTN_BG       = "#2a2a2a"
BTN_BG_HOVER = "#3a3a3a"
TIME_BAR_BG  = "#262626"   # slightly darker than BAR_BG
TIME_BAR_FG  = "#999999"   # quiet light gray; day notches use this too
BAR_TICK_FG  = "#c8c8c8"   # tick on top of the split bar being filled
READOUT_TRACK = "#535353"  # bars readout track, lighter than BAR_BG
READOUT_TIME_FG = "#cfcdc8"  # bars readout reset fill, a lighter take on the taskbar's running-app line
BAR_H        = 6
STACK_H      = 8            # Grok weekly bar, tall enough to read category colors
TIME_BAR_H   = 2            # slim accent under the usage bar
TICK_DROP    = 2            # hangs 2px under the time bar (50% taller than 1px)
WIDTH        = 220
SOURCE_ORDER = ("claude", "grok", "chatgpt")
SOURCE_TITLE = {"claude": "Claude", "grok": "Grok", "chatgpt": "ChatGPT"}
# ChatGPT starts off. Claude/Grok stay on for existing installs.
SOURCE_SHOW_DEFAULT = {"claude": True, "grok": True, "chatgpt": False}

# Discrete refresh-timer presets, in minutes.
MINUTE_PRESETS      = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 25, 30, 45, 60]
REFRESH_DEFAULT_MIN = 5
RATE_LIMIT_FALLBACK_S = 120  # Used when 429 response omits Retry-After.


def bar_color(util: float) -> str:
    if util >= 0.9:  return BAR_HIGH
    if util >= 0.6:  return BAR_MED
    return BAR_LOW


# Product slices within this many points of the weekly total are the same pool
# (28+6+2+2+2 = 40 vs a weekly 41). Farther apart, keep each slice's own width.
SLICE_MATCH_TOLERANCE = 0.05


def stack_fractions(parts: list[float], pool: float, *, complete: bool) -> list[float]:
    """Segment widths as fractions of the weekly limit (the full bar).

    When every product is included and the parts add up to about the pool,
    scale them so the stack ends on the weekly percent. Otherwise each part
    keeps its own percent. The painted width never passes 100%.
    """
    clean = [max(0.0, float(p)) for p in parts]
    total = sum(clean)
    pool = min(max(float(pool), 0.0), 1.0)
    if total <= 0 or not clean:
        return [0.0 for _ in clean]
    if complete and pool > 0 and abs(total - pool) <= SLICE_MATCH_TOLERANCE:
        scale = pool / total
    elif total > 1.0:
        scale = 1.0 / total
    else:
        scale = 1.0
    return [p * scale for p in clean]


def grok_slice_color(label: str, used: set[str],
                     custom: dict[str, str] | None = None) -> str:
    override = (custom or {}).get(label)
    if isinstance(override, str) and override.startswith("#") and len(override) in (4, 7):
        used.add(override)
        return override
    color = GROK_SLICE_COLORS.get(label)
    if color is None or color in used:
        for candidate in GROK_SLICE_FALLBACK:
            if candidate not in used:
                color = candidate
                break
        else:
            color = GROK_SLICE_FALLBACK[len(used) % len(GROK_SLICE_FALLBACK)]
    used.add(color)
    return color


# ── Dynamic tray icon ────────────────────────────────────────────────────────

TRAY_PX      = 64
TRAY_SS      = 4            # supersample; PIL draws arcs without anti-aliasing
TRAY_STYLES  = (("percent", "Number"), ("ring", "Ring"), ("both", "Both"), ("bars", "Bars"))
TRAY_BAR_GAP = 0.06         # gap between bars, as a fraction of the icon
TRAY_TIME_H  = 0.16         # reset bar under the split bars, as a fraction of the icon
TRAY_DEFAULT_METER = ["claude", "5-hour"]
TRAY_DOT     = 10           # px, meter-picker dot in settings mode


def _tray_font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype("segoeuib.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def _fit_tray_text(draw, text: str, room: int, start: int):
    size = start
    while size > 8:
        font = _tray_font(size)
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        if r - l <= room and b - t <= room:
            return font
        size -= 2
    return _tray_font(size)


def _draw_centered(draw, n: int, text: str, font, fill: str) -> None:
    """Center on the glyphs' own box so '4' and '13' sit in the same spot."""
    l, t, r, b = draw.textbbox((0, 0), text, font=font, anchor="lt")
    draw.text(((n - (r - l)) / 2 - l, (n - (b - t)) / 2 - t), text,
              fill=fill, font=font, anchor="lt")


TRAY_SIGN_SCALE = 0.5      # % at half height leaves more width for the digits
TRAY_SIGN_GAP   = 0.02


def _draw_number_with_sign(draw, n: int, text: str, sizing: str, fill: str) -> None:
    def widths(size: int):
        big, small = _tray_font(size), _tray_font(max(8, int(size * TRAY_SIGN_SCALE)))
        l, t, r, b = draw.textbbox((0, 0), sizing, font=big, anchor="ls")
        sl, st, sr, sb = draw.textbbox((0, 0), "%", font=small, anchor="ls")
        return big, small, (r - l) + n * TRAY_SIGN_GAP + (sr - sl), b - t

    size = int(n * 1.2)
    while size > 8:
        big, small, total_w, h = widths(size)
        if total_w <= n and h <= n:
            break
        size -= 2

    l, t, r, b = draw.textbbox((0, 0), text, font=big, anchor="ls")
    sl, st, sr, sb = draw.textbbox((0, 0), "%", font=small, anchor="ls")
    w = (r - l) + n * TRAY_SIGN_GAP + (sr - sl)
    x = (n - w) / 2 - l
    base = (n + (b - t)) / 2 - b       # digits' own box centered vertically
    draw.text((x, base), text, fill=fill, font=big, anchor="ls")
    # Sign sits on the digits' top line, like a superscript.
    draw.text((x + l + (r - l) + n * TRAY_SIGN_GAP - sl, base + t - st), "%",
              fill=fill, font=small, anchor="ls")


# ── Taskbar readout ──────────────────────────────────────────────────────────
# Windows 11 has no API for putting UI on the taskbar. A child window of
# Shell_TrayWnd is drawn under the taskbar's XAML layer, so instead this is a
# borderless window owned by the taskbar (owned windows always stack above
# their owner), parked just left of the tray. Anything unexpected and it hides.

READOUT_POLL_MS  = 500    # also how fast the readout gets out of a fullscreen game's way
READOUT_GAP      = 0
READOUT_HOVER_MS = 100
READOUT_FRAME_MS = 16     # pointer poll while over the readout, dragging or sliding
VK_LBUTTON       = 0x01
GWLP_HWNDPARENT  = -8
HWND_TOPMOST     = -1
GW_HWNDPREV      = 3
GWL_EXSTYLE      = -20
WS_EX_TOPMOST    = 0x0008
SWP_NOACTIVATE   = 0x0010
SWP_NOZORDER     = 0x0004
SWP_SHOWWINDOW   = 0x0040

# Keep floating windows off the taskbar. rcWork is the monitor minus the taskbar,
# whichever edge it sits on. Screen size includes the taskbar, so it is the wrong clamp.
WORK_MARGIN = 8
SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONULL = 0
MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def clamp_to_work_area(
    x: int, y: int, w: int, h: int,
    area: tuple[int, int, int, int],
    margin: int = WORK_MARGIN,
) -> tuple[int, int, int, int]:
    """Fit a window inside area (left, top, right, bottom), inset by margin.

    Shrinks width and height only when they would cross the inset edges.
    """
    left, top, right, bottom = (int(v) for v in area)
    left += margin
    top += margin
    right -= margin
    bottom -= margin
    if right < left:
        left = right
    if bottom < top:
        top = bottom
    avail_w = max(1, right - left)
    avail_h = max(1, bottom - top)
    w = min(max(1, int(w)), avail_w)
    h = min(max(1, int(h)), avail_h)
    x = min(max(int(x), left), right - w)
    y = min(max(int(y), top), bottom - h)
    return x, y, w, h


def _monitor_from_rect(x: int, y: int, w: int, h: int, flags: int):
    """Monitor handle for the rect, or None when the API isn't there."""
    if sys.platform != "win32":
        return None
    u32 = ctypes.windll.user32
    if not hasattr(u32, "MonitorFromRect"):
        return None
    rect = wintypes.RECT(int(x), int(y), int(x) + max(int(w), 1), int(y) + max(int(h), 1))
    try:
        u32.MonitorFromRect.argtypes = (ctypes.POINTER(wintypes.RECT), wintypes.DWORD)
        u32.MonitorFromRect.restype = ctypes.c_void_p
        return u32.MonitorFromRect(ctypes.byref(rect), flags) or None
    except (AttributeError, OSError, ctypes.ArgumentError):
        return None


def _primary_work_area() -> tuple[int, int, int, int] | None:
    if sys.platform != "win32":
        return None
    rect = wintypes.RECT()
    u32 = ctypes.windll.user32
    try:
        u32.SystemParametersInfoW.argtypes = (
            wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
        )
        u32.SystemParametersInfoW.restype = wintypes.BOOL
        ok = u32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    except (AttributeError, OSError, ctypes.ArgumentError):
        return None
    if not ok or rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _monitor_info(x: int, y: int, w: int, h: int) -> _MONITORINFO | None:
    """Full-monitor and work-area rects of the monitor nearest this rect."""
    hmon = _monitor_from_rect(x, y, w, h, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    u32 = ctypes.windll.user32
    try:
        u32.GetMonitorInfoW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        u32.GetMonitorInfoW.restype = wintypes.BOOL
        ok = u32.GetMonitorInfoW(hmon, ctypes.byref(info))
    except (AttributeError, OSError, ctypes.ArgumentError):
        ok = False
    return info if ok else None


def work_area_for_rect(x: int, y: int, w: int, h: int) -> tuple[int, int, int, int] | None:
    """Work area of the monitor under this rect. Falls back to the primary."""
    info = _monitor_info(x, y, w, h)
    if info:
        r = info.rcWork
        if r.right > r.left and r.bottom > r.top:
            return int(r.left), int(r.top), int(r.right), int(r.bottom)
    return _primary_work_area()


_SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")


def fullscreen_app_on(x: int, y: int, w: int, h: int) -> bool:
    """True when the foreground window (a game, a video) covers the whole monitor this rect is on."""
    if sys.platform != "win32":
        return False
    u32 = ctypes.windll.user32
    u32.GetForegroundWindow.restype = wintypes.HWND
    fg = u32.GetForegroundWindow()
    if not fg:
        return False
    cls = ctypes.create_unicode_buffer(64)
    u32.GetClassNameW(fg, cls, 64)
    if cls.value in _SHELL_CLASSES:
        return False
    win = _win_rect(fg)
    info = _monitor_info(x, y, w, h)
    if not win or not info:
        return False
    m = info.rcMonitor
    return win[0] <= m.left and win[1] <= m.top and win[2] >= m.right and win[3] >= m.bottom


def rect_on_monitor(x: int, y: int, w: int, h: int) -> bool | None:
    """True when the rect hits a monitor. None if we can't ask Windows."""
    if sys.platform != "win32" or not hasattr(ctypes.windll.user32, "MonitorFromRect"):
        return None
    return _monitor_from_rect(x, y, w, h, MONITOR_DEFAULTTONULL) is not None


def _win_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    r = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def _taskbar_slot(width: int) -> tuple[int, tuple[int, int, int, int]] | None:
    """(taskbar hwnd, screen x/y/w/h) for a box left of the tray, or None."""
    u32 = ctypes.windll.user32
    u32.FindWindowW.restype = wintypes.HWND
    u32.FindWindowExW.restype = wintypes.HWND
    bar = u32.FindWindowW("Shell_TrayWnd", None)
    if not bar:
        return None
    notify = u32.FindWindowExW(bar, None, "TrayNotifyWnd", None)
    bar_r, notify_r = _win_rect(bar), _win_rect(notify) if notify else None
    if not bar_r or not notify_r:
        return None
    bl, bt, br, bb = bar_r
    if br - bl < bb - bt:          # vertical taskbar: no room, skip
        return None
    x = notify_r[0] - width - READOUT_GAP
    if x < bl:
        return None
    return bar, (x, bt, width, bb - bt)


def _taskbar_bg(x: int, y: int) -> str:
    """Sample the taskbar just left of the slot so the readout blends in."""
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    hdc = u32.GetDC(None)
    try:
        c = g32.GetPixel(hdc, max(0, x - 3), y + 2)
    finally:
        u32.ReleaseDC(None, hdc)
    if c == -1 or c == 0xFFFFFFFF:
        return "#1c1c1c"
    return f"#{c & 0xFF:02x}{(c >> 8) & 0xFF:02x}{(c >> 16) & 0xFF:02x}"


def _is_light(hex_color: str) -> bool:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b > 140


READOUT_SUB_PX   = 10
READOUT_TEXT_GAP = 1
READOUT_STYLES   = (("ring", "Ring"), ("bars", "Bars"))
READOUT_BARS_PAD    = 4     # px margin on every side of the bars readout
READOUT_BARS_SPACE  = 2     # px between neighbouring vertical bars
READOUT_BARS_GAP    = 1     # px between the vertical bars and their reset bar
READOUT_BAR_TICK    = 0     # px tick on top of the bar being filled; 0 = off
READOUT_TIME_TICK   = 0     # px tick at the far end of each reset bar; 0 = off
READOUT_STACK_GAP   = 3     # px between the 5-hour and 7-day stacks
READOUT_STACK_TIME_H = 2    # px, reset bar height when stacked
READOUT_STACK       = ("5-hour", "7-day")
READOUT_BAR_METERS  = (("5-hour", "5-hour"), ("7-day", "7-day"), ("both", "Both"))


def _readout_bars_inner_w(segment_counts: list[int], h: int) -> int:
    """Width near the content height where every stack's bars come out whole and equal."""
    target = max(8, h - 2 * READOUT_BARS_PAD)
    gap = READOUT_BARS_SPACE
    for delta in sorted(range(-6, 13), key=lambda d: (abs(d), d < 0)):
        w = target + delta
        if all((w - gap * (n - 1)) % n == 0 and w > gap * (n - 1) for n in segment_counts):
            return w
    return target


def readout_bars_width(segment_counts: list[int], h: int) -> int:
    return _readout_bars_inner_w(segment_counts, h) + 2 * READOUT_BARS_PAD


def _readout_sub(label: str, sub: str) -> str:
    return sub.removeprefix("resets in ") if sub else label


def _readout_ring(h: int) -> int:
    return max(12, h - READOUT_SUB_PX - READOUT_TEXT_GAP - 4)


def readout_width(util: float, label: str, sub: str, h: int) -> int:
    """Just wide enough for the ring or the text under it, so it hugs the tray."""
    from PIL import Image, ImageDraw

    s = TRAY_SS
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    sub_w = d.textlength(_readout_sub(label, sub), font=_tray_font(READOUT_SUB_PX * s))
    return max(_readout_ring(h), int(sub_w / s + 0.99))


def render_readout(util: float, label: str, sub: str, w: int, h: int, bg: str):
    from PIL import Image, ImageDraw

    s = TRAY_SS
    img = Image.new("RGB", (w * s, h * s), bg)
    d = ImageDraw.Draw(img)
    util = min(max(util, 0.0), 1.0)
    color = bar_color(util)
    track = "#c8c8c8" if _is_light(bg) else BAR_BG
    text_fg = "#1a1a1a" if _is_light(bg) else FG
    dim_fg = "#5a5a5a" if _is_light(bg) else FG_DIM

    # Ring with the number in its hole, reset countdown centered under it.
    ring = _readout_ring(h) * s
    stroke = max(2, ring * 14 // 100)
    small = _tray_font(READOUT_SUB_PX * s)
    text = _readout_sub(label, sub)
    _, st, _, sb = d.textbbox((0, 0), text, font=small, anchor="lt")
    top = (h * s - (ring + READOUT_TEXT_GAP * s + sb - st)) // 2
    left = (w * s - ring) // 2
    box = (left, top, left + ring, top + ring)
    d.ellipse(box, outline=track, width=stroke)
    if util > 0:
        d.arc(box, -90, -90 + 360 * util, fill=color, width=stroke)

    pct = min(int(util * 100), 100)
    hole = ring - 2 * stroke
    big = _fit_tray_text(d, "100" if pct == 100 else "88", int(hole * 0.86), hole)
    digits = str(pct)
    l, t, r, b = d.textbbox((0, 0), digits, font=big, anchor="lt")
    cx, cy = left + ring / 2, top + ring / 2
    d.text((cx - (r - l) / 2 - l, cy - (b - t) / 2 - t), digits, fill=color,
           font=big, anchor="lt")
    d.text((w * s / 2, top + ring + READOUT_TEXT_GAP * s - st), text, fill=dim_fg,
           font=small, anchor="mt")
    return img.resize((w, h), Image.LANCZOS)


def render_readout_bars(rows: list[Row], w: int, h: int, bg: str):
    """Split bars per row, stacked top to bottom, as tall as the taskbar allows."""
    from PIL import Image, ImageDraw

    s, pad = TRAY_SS, READOUT_BARS_PAD
    img = Image.new("RGB", (w * s, h * s), bg)
    d = ImageDraw.Draw(img)
    stacked = len(rows) > 1
    ch = h - 2 * pad
    gaps = READOUT_STACK_GAP * (len(rows) - 1)
    sh = (ch - gaps) // len(rows)
    y = pad + (ch - gaps - sh * len(rows)) // 2
    for row in rows:
        _draw_split_bars(d, pad, y, w - 2 * pad, sh, row[1], tray_segments(row), row[3],
                         gap=READOUT_BARS_SPACE,
                         time_h=READOUT_STACK_TIME_H if stacked else None,
                         time_gap=READOUT_BARS_GAP, bar_tick=READOUT_BAR_TICK,
                         time_tick=READOUT_TIME_TICK, track=READOUT_TRACK,
                         time_fg=READOUT_TIME_FG, rounded=False,
                         track_fill=bg)   # see-through, like the perf tiles
        y += sh + READOUT_STACK_GAP
    return img.resize((w, h), Image.LANCZOS)


class TaskbarReadout:
    """Owns the reparented window. All calls come from the Tk thread."""

    def __init__(self, root: tk.Tk, on_click, on_right_click, tip_for, on_reorder) -> None:
        self._root = root
        self._on_reorder = on_reorder  # (graph ids, left to right) after a drag
        self._args: tuple | None = None
        self._tile_x: dict[str, float] = {}   # where each tile is drawn, mid-slide
        self._order: list[str] = []           # graph ids as last drawn
        self._sliding = False
        self._down = False
        self._press: tuple[str | None, int] | None = None   # (region, screen x)
        self._drag: dict | None = None        # id, grab (px into tile), x (strip px)
        self._on_right_click = on_right_click
        self._win: tk.Toplevel | None = None
        self._lbl: tk.Label | None = None
        self._photo = None
        self._bar = 0
        self._slot: tuple[int, int, int, int] | None = None
        self._bg = "#1c1c1c"
        self._data: tuple | None = None
        self._on_click = on_click
        self._tip_for = tip_for       # key -> (title, body) or None
        self._regions: list[tuple[int, int, str]] = []
        self._hover: str | None = None
        self._tip: tk.Toplevel | None = None
        self._hover_after: str | None = None

    def _hwnd(self) -> int:
        return ctypes.windll.user32.GetParent(self._win.winfo_id())

    def _ensure(self, width_for) -> bool:
        found = _taskbar_slot(0)
        if not found:
            self.hide()
            return False
        bar, (right, y, _, h) = found
        w = width_for(h)
        if right - w < 0:
            self.hide()
            return False
        slot = (right - w, y, w, h)
        # The readout is topmost to stay above the taskbar, which would also put
        # it over a fullscreen game. Step aside while one has the screen.
        if fullscreen_app_on(*slot):
            self.hide()
            return False
        if self._win is None or not self._win.winfo_exists():
            self._win = tk.Toplevel(self._root)
            self._win.overrideredirect(True)
            self._win.configure(bg=self._bg)
            # Left clicks and drags are read in _poll_pointer, which also sees
            # the see-through pixels.
            self._lbl = tk.Label(self._win, bd=0, bg=self._bg, cursor="hand2")
            self._lbl.bind("<Button-3>", lambda e: self._on_right_click(e.x_root, e.y_root))
            self._win.update_idletasks()
            self._poll_pointer()
            # The main window may be withdrawn at sign-in. Map this window on
            # its own so the taskbar readout still shows.
            try:
                self._win.deiconify()
            except tk.TclError:
                pass
            self._bar = 0
        hwnd = self._hwnd()
        u32 = ctypes.windll.user32
        if self._bar != bar:
            # Owned by the taskbar, so Windows keeps it stacked above the taskbar.
            u32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
            u32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, bar)
            self._bar = bar
        if slot != self._slot:
            x, y, w, h = slot
            self._bg = _taskbar_bg(x, y)
            self._win.geometry(f"{w}x{h}+{x}+{y}")
            self._lbl.place(x=0, y=0, width=w, height=h)
            self._win.update_idletasks()
            self._slot = slot
            self._data_drawn = None
        # Tk doesn't always move a reowned window, and clicking the taskbar can
        # raise it over us, so pin the spot and z-order every poll.
        # A withdrawn main window must not take this readout with it, and a
        # minimize (screen-control tools do this) would leave a gray "tk" stub.
        try:
            if str(self._win.state()) in ("withdrawn", "iconic"):
                self._win.deiconify()
        except tk.TclError:
            pass
        self._place(u32, hwnd)
        return True

    def _place(self, u32, hwnd: int) -> None:
        """Pin the spot and sit directly above the taskbar, not at the very top.
        Jumping to the top fights other topmost overlays (Wispr Flow's tour dims
        the whole screen); each re-raise flips the readout lit/dim."""
        u32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT)
        u32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        u32.GetWindow.restype = wintypes.HWND
        x, y, w, h = self._slot
        flags = SWP_NOACTIVATE | SWP_SHOWWINDOW
        u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        if not u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST:
            u32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, flags)
        above_bar = u32.GetWindow(self._bar, GW_HWNDPREV)
        if above_bar == hwnd:
            flags |= SWP_NOZORDER
        u32.SetWindowPos(hwnd, above_bar or HWND_TOPMOST, x, y, w, h, flags)

    def show(self, rows: list[Row], style: str, graphs: list[tuple] = ()) -> None:
        """Perf graphs on the left, then usage. Ring shows rows[0]; bars stack every row.

        graphs: (id, color, series) per tile, in saved order. Either side may be empty.
        """
        self._args = (rows, style, list(graphs))
        self._draw()

    def _layout(self, ids: list[str], tile: int) -> dict[str, float]:
        """Drawn x per tile. The dragged tile follows the mouse; the rest slide
        toward their slots a share of the way each frame, like taskbar icons."""
        step = tile + PERF_TILE_GAP
        drag = self._drag if self._drag and self._drag["id"] in ids else None
        if drag:
            ids = [i for i in ids if i != drag["id"]]
            ids.insert(min(max(round(drag["x"] / step), 0), len(ids)), drag["id"])
        self._order = ids
        xs, sliding = {}, False
        for i, gid in enumerate(ids):
            target = float(i * step)
            if drag and gid == drag["id"]:
                xs[gid] = drag["x"]
                continue
            cur = self._tile_x.get(gid, target)
            cur += (target - cur) * PERF_SLIDE
            if abs(target - cur) < 0.5:
                cur = target
            else:
                sliding = True
            xs[gid] = cur
        self._tile_x, self._sliding = xs, sliding
        return xs

    def _draw(self) -> None:
        if self._args is None:
            return
        rows, style, graphs = self._args
        if not rows and not graphs:
            self.hide()
            return
        bars = style == "bars"
        try:
            counts = [tray_segments(r) for r in rows]

            def usage_w(h: int) -> int:
                if not rows:
                    return 0
                if bars:
                    return readout_bars_width(counts, h)
                return readout_width(rows[0][1], rows[0][0], rows[0][2], h)

            if not self._ensure(lambda h: perf_strip_width(len(graphs), h) + usage_w(h)):
                return
            _, _, w, h = self._slot
            gw = perf_strip_width(len(graphs), h)
            tile = round((h - 2 * READOUT_BARS_PAD) * PERF_TILE_RATIO)
            xs = self._layout([g[0] for g in graphs], tile)
            lifted = self._drag["id"] if self._drag else None
            data = (style, self._slot, self._bg, lifted,
                    tuple((g, round(x * TRAY_SS)) for g, x in xs.items()), tuple(
                (r[0], round(r[1], 3), r[2], c, None if r[3] is None else round(r[3], 3))
                for r, c in zip(rows, counts)), tuple(
                (gid, color, tuple((tuple(round(v, 3) for v in line), filled, style)
                                   for line, filled, style in series))
                for gid, color, series in graphs))
            if data == getattr(self, "_data_drawn", None):
                self._refresh_tip()
                return
            from PIL import Image, ImageTk
            image = Image.new("RGB", (w, h), self._bg)
            regions = []
            if graphs:
                # The dragged tile is drawn last so it rides over the others.
                tiles = sorted(graphs, key=lambda g: g[0] == lifted)
                image.paste(render_perf_strip(
                    [(xs[gid], c, s, gid == lifted) for gid, c, s in tiles], gw, h, self._bg))
                for i, gid in enumerate(self._order):
                    x = i * (tile + PERF_TILE_GAP)
                    regions.append((x, x + tile, gid))
            if rows:
                uw = w - gw
                if bars:
                    part = render_readout_bars(rows, uw, h, self._bg)
                else:
                    part = render_readout(rows[0][1], rows[0][0], rows[0][2], uw, h, self._bg)
                image.paste(part, (gw, 0))
                regions.append((gw, w, "usage"))
            self._regions = regions
            self._photo = ImageTk.PhotoImage(image)
            self._lbl.config(image=self._photo, bg=self._bg)
            self._win.configure(bg=self._bg)
            # The sampled color becomes see-through, so the real taskbar (acrylic,
            # top highlight) shows; edges stay anti-aliased against that color.
            self._win.attributes("-transparentcolor", self._bg)
            self._data_drawn = data
            self._refresh_tip()
        except Exception:  # noqa: BLE001 — a readout failure must not take the widget down
            self.hide()

    # ── hover tooltip ──

    def _poll_pointer(self) -> None:
        """Hover, click and drag, polled rather than bound: see-through pixels
        pass the mouse straight to the taskbar, so Tk never hears about them."""
        self._hover_after = None
        if self._win is None:
            return
        u32 = ctypes.windll.user32
        self._keep_above_taskbar(u32)
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        down = bool(u32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        key, inside, sx = None, False, 0
        if self._slot:
            sx, sy, w, h = self._slot
            inside = sx <= pt.x < sx + w and sy <= pt.y < sy + h
            if inside:
                key = next((k for x0, x1, k in self._regions if x0 <= pt.x - sx < x1), None)

        if down and not self._down:
            self._press = (key, pt.x) if inside else None
        elif down and self._press:
            pressed, px = self._press
            if (self._drag is None and pressed not in (None, "usage")
                    and abs(pt.x - px) > PERF_DRAG_PX):
                start = next((r[0] for r in self._regions if r[2] == pressed), 0)
                self._drag = {"id": pressed, "grab": px - sx - start, "x": float(start)}
                self._set_hover(None)
            if self._drag and self._slot:
                tile = round((self._slot[3] - 2 * READOUT_BARS_PAD) * PERF_TILE_RATIO)
                last = (len(self._order) - 1) * (tile + PERF_TILE_GAP)
                self._drag["x"] = float(min(max(pt.x - sx - self._drag["grab"], 0), last))
                self._draw()
        elif not down and self._down:
            press, self._press = self._press, None
            if self._drag:
                self._drag = None
                self._on_reorder(list(self._order))
                self._draw()
            elif press and press[0] and inside and abs(pt.x - press[1]) <= PERF_DRAG_PX:
                self._on_click()
        self._down = down

        if not self._drag:
            self._set_hover(key)
            if self._sliding:
                self._draw()
        fast = inside or self._down or self._sliding
        try:
            self._hover_after = self._root.after(
                READOUT_FRAME_MS if fast else READOUT_HOVER_MS, self._poll_pointer)
        except tk.TclError:
            pass

    def _keep_above_taskbar(self, u32) -> None:
        """The taskbar raises itself (clicks, notifications) and its acrylic then
        dims the readout until the next 500 ms tick. Catch it sooner."""
        if not self._bar or not self._slot:
            return
        try:
            hwnd = self._hwnd()
        except tk.TclError:
            return
        u32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        u32.GetWindow.restype = wintypes.HWND
        above = hwnd
        for _ in range(64):
            above = u32.GetWindow(above, GW_HWNDPREV)
            if not above:
                return
            if above == self._bar:
                self._place(u32, hwnd)
                return

    def _set_hover(self, key: str | None) -> None:
        if key != self._hover:
            self._hover = key
            self._refresh_tip()

    def _refresh_tip(self) -> None:
        """Live: called on every redraw, so the numbers tick while hovering."""
        tip = self._tip_for(self._hover) if self._hover and self._slot else None
        region = next((r for r in self._regions if r[2] == self._hover), None)
        if not tip or region is None:
            self._hide_tip()
            return
        title, body = tip
        if self._tip is None or not self._tip.winfo_exists():
            t = tk.Toplevel(self._root)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            t.configure(bg=SEPARATOR)
            inner = tk.Frame(t, bg=BG)
            inner.pack(padx=1, pady=1)
            self._tip_title = tk.Label(inner, bg=BG, fg=FG, font=("Segoe UI", 9, "bold"),
                                       justify="left", bd=0)
            self._tip_title.pack(anchor="w", padx=8, pady=(5, 0))
            self._tip_body = tk.Label(inner, bg=BG, fg=FG, font=("Segoe UI", 8),
                                      justify="left", bd=0)
            self._tip_body.pack(anchor="w", padx=8, pady=(1, 6))
            self._tip = t
            try:
                t.deiconify()
            except tk.TclError:
                pass
        if self._tip_title.cget("text") == title and self._tip_body.cget("text") == body:
            return
        self._tip_title.config(text=title)
        self._tip_body.config(text=body)
        self._tip.update_idletasks()
        tw, th = self._tip.winfo_reqwidth(), self._tip.winfo_reqheight()
        sx, sy, _, _ = self._slot
        x = sx + (region[0] + region[1]) // 2 - tw // 2
        bar = _win_rect(self._bar) if self._bar else None
        if bar:
            x = max(bar[0] + 4, min(x, bar[2] - tw - 4))
        self._tip.geometry(f"+{x}+{sy - th - 6}")

    def _hide_tip(self) -> None:
        tip, self._tip = self._tip, None
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass

    def hide(self) -> None:
        self._hover, self._regions = None, []
        self._drag, self._press, self._tile_x = None, None, {}
        self._hide_tip()
        if self._hover_after is not None:
            try:
                self._root.after_cancel(self._hover_after)
            except tk.TclError:
                pass
            self._hover_after = None
        win, self._win = self._win, None
        self._bar, self._slot, self._data_drawn = 0, None, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass


def tray_segments(row: Row) -> int:
    """One bar per day for weekly windows, per hour for '5-hour' style labels."""
    if row[4]:
        return row[4]
    head, _, unit = row[0].partition("-")
    if unit.startswith("hour") and head.isdigit() and 1 < int(head) <= 12:
        return int(head)
    return 5


def _split_bars_grid(w: int, segments: int, gap: int) -> tuple[int, int]:
    """(bar width, total width) in px. Every bar gets the same whole width."""
    bw = max(1, (w - gap * (segments - 1)) // segments)
    return bw, bw * segments + gap * (segments - 1)


def _tint(hex_color: str, amount: float) -> str:
    """Blend toward white by amount (0..1)."""
    rgb = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(c + (255 - c) * amount):02x}" for c in rgb)


def _draw_split_bars(d, x: int, y: int, w: int, h: int, util: float, segments: int,
                     time_progress: float | None, gap: int | None = None,
                     time_h: int | None = None, time_gap: int = 0,
                     bar_tick: int = 0, time_tick: int = 0, track: str = BAR_BG,
                     time_fg: str = TIME_BAR_FG, rounded: bool = True,
                     track_fill: str | None = None) -> None:
    """Split bars in a w×h px box at (x, y) on a TRAY_SS-supersampled canvas.

    Fills left to right; the bar in progress fills bottom-up. Edges sit on whole
    output pixels so small sizes stay crisp. time_gap is px between the bars and
    the reset bar. Ticks mark each bar's full end, so the gap to them shows
    what's left: bar_tick px of light gray on top of the bar being filled,
    time_tick px of white at the reset bar's far end.
    """
    s = TRAY_SS
    util = min(max(util, 0.0), 1.0)
    color = bar_color(util)
    if gap is None:
        gap = max(1, round(h * TRAY_BAR_GAP))
    bw, gw = _split_bars_grid(w, segments, gap)
    x += (w - gw) // 2
    r = int(bw * s * 0.25) if rounded else 0
    # Solid track, or a 1px inside stroke of the track color around track_fill.
    track_ink = ({"fill": track} if track_fill is None
                 else {"fill": track_fill, "outline": track, "width": s})
    bars_h = h
    sits = time_progress is not None and time_gap == 0
    if time_progress is not None:
        # Reset countdown runs along the bottom, under the usage bars.
        th = time_h or max(1, round(h * TRAY_TIME_H))
        ty, bottom = (y + h - th) * s, (y + h) * s - 1
        # Pillow needs radius <= (span - 3) / 2 when two corners share a side.
        tr = max(0, min(r, (th * s - 3) // 2))
        d.rounded_rectangle((x * s, ty, (x + gw) * s - 1, bottom), radius=tr,
                            corners=(not sits, not sits, True, True), **track_ink)
        tp = min(max(time_progress, 0.0), 1.0)
        end = round(gw * tp)
        if end > 0:
            d.rounded_rectangle((x * s, ty, (x + end) * s - 1, bottom),
                                radius=max(0, min(tr, (end * s - 3) // 2)), fill=time_fg,
                                corners=(not sits, not sits and tp >= 1, tp >= 1, True))
        if time_tick:
            mx = x + gw - time_tick
            d.rectangle((mx * s, ty, (mx + time_tick) * s - 1, bottom), fill="#ffffff")
        bars_h = h - th - time_gap
    top, bottom = y * s, (y + bars_h) * s - 1
    # Square bottoms when the bars sit on the reset bar.
    corners = (True, True, not sits, not sits)
    filled = util * segments
    active = min(int(filled), segments - 1)
    for i in range(segments):
        x0 = (x + i * (bw + gap)) * s
        x1 = x0 + bw * s - 1
        d.rounded_rectangle((x0, top, x1, bottom), radius=r, corners=corners, **track_ink)
        part = min(max(filled - i, 0.0), 1.0)
        fill_h = round(bars_h * part)
        if fill_h > 0:
            y0 = (y + bars_h - fill_h) * s
            d.rounded_rectangle((x0, y0, x1, bottom), radius=max(0, min(r, (bottom - y0 - 2) // 2)),
                                fill=color, corners=corners)
        if bar_tick and i == active:
            d.rectangle((x0, top, x1, top + bar_tick * s - 1), fill=BAR_TICK_FG)


def render_tray_icon(util: float, style: str, segments: int = 5,
                     time_progress: float | None = None):
    """Usage as a number, a ring, both, or split bars, colored like the bars."""
    from PIL import Image, ImageDraw

    n = TRAY_PX * TRAY_SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    util = min(max(util, 0.0), 1.0)
    color = bar_color(util)
    room = n

    if style == "bars":
        _draw_split_bars(d, 0, 0, TRAY_PX, TRAY_PX, util, segments, time_progress)

    if style in ("ring", "both"):
        w = n * (22 if style == "ring" else 14) // 100
        box = (0, 0, n - 1, n - 1)
        d.ellipse(box, outline=BAR_BG, width=w)
        if util > 0:
            d.arc(box, -90, -90 + 360 * util, fill=color, width=w)
        room = int((n - 2 * w) * 0.72)   # square that fits inside the hole

    if style in ("percent", "both"):
        pct = min(int(util * 100), 100)
        text = str(pct)
        # One size for every 1-2 digit value; only 100 gets its own fit.
        sizing = text if pct == 100 else "88"
        if style == "percent":
            _draw_number_with_sign(d, n, text, sizing, color)
        else:
            font = _fit_tray_text(d, sizing, room, int(room * 1.6))
            _draw_centered(d, n, text, font, color)

    return img.resize((TRAY_PX, TRAY_PX), Image.LANCZOS)


# ── Performance graphs ───────────────────────────────────────────────────────
# Task Manager's Performance tiles, read from the same PDH counters it uses.

PERF_SAMPLE_S  = 1.0
PERF_HISTORY   = 60          # one sample a second, like Task Manager's 60 seconds
PERF_TILE_RATIO = 1.0        # tile width per px of height
PERF_TILE_GAP  = 4
PERF_NET_FLOOR = 1_000       # bits/s, lowest graph top; Task Manager goes this low too
PERF_NET_FILL  = "#4d192c"   # Task Manager's shade under the receive line
PERF_COLORS = {             # Task Manager's own where it has one
    "cpu":  "#39b8e3",
    "mem":  "#3d7eff",
    "disk": "#4cc35a",
    "net":  "#f5628e",
    "gpu":  "#de68f2",
}
PERF_SLIDE     = 0.3         # share of the gap a sliding tile closes each frame
PERF_DRAG_PX   = 4           # movement before a press becomes a drag
PERF_POPOUT_POLL_MS = 150
PERF_POPOUT_SLACK   = 8      # px the mouse may stray past the graph picker
PERF_COUNTERS = {
    "cpu":      r"\Processor Information(_Total)\% Processor Utility",
    "cpu_perf": r"\Processor Information(_Total)\% Processor Performance",
    "cpu_freq": r"\Processor Information(_Total)\Processor Frequency",
    "commit":   r"\Memory\Committed Bytes",
    "commit_limit": r"\Memory\Commit Limit",
    "standby_n": r"\Memory\Standby Cache Normal Priority Bytes",
    "standby_r": r"\Memory\Standby Cache Reserve Bytes",
    "standby_c": r"\Memory\Standby Cache Core Bytes",
    "modified": r"\Memory\Modified Page List Bytes",
    "paged":    r"\Memory\Pool Paged Bytes",
    "nonpaged": r"\Memory\Pool Nonpaged Bytes",
    "disk_idle": r"\PhysicalDisk(*)\% Idle Time",
    "disk_read": r"\PhysicalDisk(*)\Disk Read Bytes/sec",
    "disk_write": r"\PhysicalDisk(*)\Disk Write Bytes/sec",
    "net_sent": r"\Network Interface(*)\Bytes Sent/sec",
    "net_recv": r"\Network Interface(*)\Bytes Received/sec",
    "net_bw":   r"\Network Interface(*)\Current Bandwidth",
    "gpu_eng":  r"\GPU Engine(*)\Utilization Percentage",
    "gpu_mem":  r"\GPU Adapter Memory(*)\Dedicated Usage",
}
PDH_FMT_DOUBLE   = 0x200
PDH_FMT_NOCAP100 = 0x8000
PDH_MORE_DATA    = 0x800007D2 - (1 << 32)
CREATE_NO_WINDOW = 0x08000000
# Disk types and adapter names only come from PowerShell. Slow (~4s), so once
# at start and again when a disk or adapter comes or goes.
PERF_DISCOVER_PS = (
    "$d=@(Get-PhysicalDisk|Select-Object DeviceId,MediaType,BusType);"
    "$n=@(Get-NetAdapter|Where-Object Status -eq Up|"
    "Select-Object Name,InterfaceDescription,PhysicalMediaType,HardwareInterface);"
    "@{disks=$d;nets=$n}|ConvertTo-Json -Depth 3 -Compress"
)


class _PdhValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _PdhItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _PdhValue)]


class _LUID(ctypes.Structure):
    _fields_ = [("Low", wintypes.DWORD), ("High", ctypes.c_long)]


class _DxgiAdapterDesc1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", wintypes.UINT),
                ("DeviceId", wintypes.UINT), ("SubSysId", wintypes.UINT),
                ("Revision", wintypes.UINT), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", _LUID),
                ("Flags", wintypes.UINT)]


class _KmtOpenLuid(ctypes.Structure):
    _fields_ = [("AdapterLuid", _LUID), ("hAdapter", wintypes.UINT)]


class _KmtQuery(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Type", ctypes.c_int),
                ("pPrivateDriverData", ctypes.c_void_p), ("PrivateDriverDataSize", wintypes.UINT)]


class _KmtPerfData(ctypes.Structure):
    _fields_ = [("PhysicalAdapterIndex", wintypes.UINT), ("MemoryFrequency", ctypes.c_uint64),
                ("MaxMemoryFrequency", ctypes.c_uint64), ("MaxMemoryFrequencyOC", ctypes.c_uint64),
                ("MemoryBandwidth", ctypes.c_uint64), ("PCIEBandwidth", ctypes.c_uint64),
                ("FanRPM", wintypes.ULONG), ("Power", wintypes.ULONG),
                ("Temperature", wintypes.ULONG), ("PowerStateOverride", ctypes.c_ubyte)]


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64)]


KMTQAITYPE_ADAPTERPERFDATA = 62
DXGI_ADAPTER_FLAG_SOFTWARE = 2


def _com_call(obj, idx: int, *args, argtypes=()) -> int:
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[idx])(obj, *args)


def _luid_key(luid: _LUID) -> str:
    return f"luid_0x{luid.High & 0xFFFFFFFF:08x}_0x{luid.Low:08x}"


def dxgi_adapters() -> list[tuple[str, int, _LUID]]:
    """(name, dedicated VRAM bytes, LUID) for each hardware GPU, in Task Manager order."""
    import uuid
    iid = (ctypes.c_byte * 16).from_buffer_copy(
        uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)   # IDXGIFactory1
    factory = ctypes.c_void_p()
    if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
        return []
    out = []
    try:
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if _com_call(factory, 12, i, ctypes.byref(adapter),          # EnumAdapters1
                         argtypes=(wintypes.UINT, ctypes.c_void_p)) != 0:
                break
            desc = _DxgiAdapterDesc1()
            _com_call(adapter, 10, ctypes.byref(desc), argtypes=(ctypes.c_void_p,))  # GetDesc1
            _com_call(adapter, 2)                                         # Release
            if not desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE:
                luid = _LUID(desc.AdapterLuid.Low, desc.AdapterLuid.High)
                out.append((desc.Description, int(desc.DedicatedVideoMemory), luid))
            i += 1
    finally:
        _com_call(factory, 2)
    return out


def gpu_temperature(luid: _LUID) -> float | None:
    """°C from the driver, the same source as Task Manager. None if not reported."""
    g = ctypes.windll.gdi32
    opened = _KmtOpenLuid(luid, 0)
    if g.D3DKMTOpenAdapterFromLuid(ctypes.byref(opened)) != 0:
        return None
    try:
        data = _KmtPerfData()
        query = _KmtQuery(opened.hAdapter, KMTQAITYPE_ADAPTERPERFDATA,
                          ctypes.cast(ctypes.byref(data), ctypes.c_void_p), ctypes.sizeof(data))
        if g.D3DKMTQueryAdapterInfo(ctypes.byref(query)) != 0 or not data.Temperature:
            return None
        return data.Temperature / 10
    finally:
        g.D3DKMTCloseAdapter(ctypes.byref(wintypes.UINT(opened.hAdapter)))


def cpu_name() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except OSError:
        return ""


def _pdh_instance(desc: str) -> str:
    """Adapter description as PDH spells its instance name."""
    return desc.replace("(", "[").replace(")", "]").replace("#", "_").replace("/", "_")


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _fmt_gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f}"


def _fmt_bits(bps: float) -> str:
    """Task Manager style: 0 Kbps, 48.0 Kbps, 1.2 Mbps."""
    for unit, size in (("Gbps", 1e9), ("Mbps", 1e6)):
        if bps >= size:
            return f"{bps / size:.3g} {unit}"
    kbps = bps / 1e3
    return f"{kbps:.0f} Kbps" if kbps >= 100 or kbps == 0 else f"{kbps:.1f} Kbps"


def _disk_kind(media: str, bus: str) -> str:
    if bus == "USB":
        return "USB (USB)"
    media = media if media in ("SSD", "HDD") else ""
    return f"{media} ({bus})" if media and bus else media or bus


def perf_default_on(dev: dict) -> bool:
    if dev["kind"] in ("cpu", "mem"):
        return True
    if dev["kind"] == "net":
        return bool(dev.get("hw"))
    return dev["id"] == "gpu:0"


class PerfMonitor:
    """Samples every device once a second on its own thread.

    Devices are dicts: id, kind, title, sub. Tk reads snapshot() only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._devices: list[dict] = []
        self._history: dict[str, list] = {}
        self._tips: dict[str, tuple[str, str]] = {}
        self._discovered: dict | None = None
        self._discovering = False
        self._instances: tuple = ()
        self.devices_version = 0
        self.seq = 0

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running() or sys.platform != "win32":
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
        with self._lock:
            self._history.clear()

    def snapshot(self) -> tuple[list[dict], dict[str, list], dict[str, tuple[str, str]]]:
        with self._lock:
            return (list(self._devices), {k: list(v) for k, v in self._history.items()},
                    dict(self._tips))

    def _discover(self) -> None:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", PERF_DISCOVER_PS],
                capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
            raw = json.loads(out.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            raw = {}
        disks = {str(d.get("DeviceId")): _disk_kind(str(d.get("MediaType") or ""),
                                                    str(d.get("BusType") or ""))
                 for d in raw.get("disks") or [] if isinstance(d, dict)}
        nets = {}
        for n in raw.get("nets") or []:
            if isinstance(n, dict) and n.get("InterfaceDescription"):
                nets[_pdh_instance(str(n["InterfaceDescription"]))] = n
        self._discovered = {"disks": disks, "nets": nets, "gpus": dxgi_adapters()}
        self._discovering = False

    def _rediscover(self) -> None:
        if not self._discovering:
            self._discovering = True
            threading.Thread(target=self._discover, daemon=True).start()

    def _run(self, stop: threading.Event) -> None:
        pdh = ctypes.windll.pdh
        pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
        query = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
            return
        counters = {}
        for key, path in PERF_COUNTERS.items():
            h = ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(query, path, None, ctypes.byref(h)) == 0:
                counters[key] = h
        cpu = cpu_name()

        def read(key: str) -> dict[str, float]:
            h = counters.get(key)
            if h is None:
                return {}
            size, count = wintypes.DWORD(0), wintypes.DWORD(0)
            fmt = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
            if pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count),
                                                None) != PDH_MORE_DATA:
                return {}
            buf = (ctypes.c_byte * size.value)()
            if pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count),
                                                buf) != 0:
                return {}
            items = ctypes.cast(buf, ctypes.POINTER(_PdhItem))
            return {items[i].szName: items[i].FmtValue.doubleValue
                    for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)}

        def one(key: str) -> float:
            vals = read(key)
            return next(iter(vals.values()), 0.0)

        try:
            pdh.PdhCollectQueryData(query)
            while not stop.wait(PERF_SAMPLE_S):
                if pdh.PdhCollectQueryData(query) != 0:
                    continue
                try:
                    self._sample(read, one, cpu)
                except Exception:  # noqa: BLE001 — one bad sample must not end the graphs
                    pass
        finally:
            pdh.PdhCloseQuery(query)

    def _sample(self, read, one, cpu: str) -> None:
        devices: list[dict] = []
        values: dict[str, object] = {}
        tips: dict[str, tuple[str, str]] = {}
        found = self._discovered or {}

        # CPU
        util = min(one("cpu"), 100.0)
        ghz = one("cpu_freq") * one("cpu_perf") / 100 / 1000
        devices.append({"id": "cpu", "kind": "cpu", "title": "CPU", "sub": cpu})
        values["cpu"] = util / 100
        tips["cpu"] = ("CPU", "\n".join(filter(None, [
            cpu, f"{util:.0f}%  {ghz:.2f} GHz" if ghz else f"{util:.0f}%"])))

        # Memory
        ms = _MemStatus()
        ms.dwLength = ctypes.sizeof(ms)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        total, used = ms.ullTotalPhys, ms.ullTotalPhys - ms.ullAvailPhys
        pct = used / total if total else 0.0
        cached = sum(one(k) for k in ("standby_n", "standby_r", "standby_c", "modified"))
        devices.append({"id": "mem", "kind": "mem", "title": "Memory", "sub": ""})
        values["mem"] = pct
        tips["mem"] = ("Memory", "\n".join([
            f"{_fmt_gb(used)}/{_fmt_gb(total)} GB ({pct * 100:.0f}%)",
            f"Available  {_fmt_gb(ms.ullAvailPhys)} GB",
            f"Committed  {_fmt_gb(one('commit'))}/{_fmt_gb(one('commit_limit'))} GB",
            f"Cached  {_fmt_gb(cached)} GB",
            f"Paged pool  {_fmt_gb(one('paged'))} GB   Non-paged  {_fmt_gb(one('nonpaged'))} GB",
        ]))

        # Disks: instances look like "3 C:" or "0 D: E:"
        idle, rd, wr = read("disk_idle"), read("disk_read"), read("disk_write")
        disks = sorted((k for k in idle if k != "_Total" and k.split()[0].isdigit()),
                       key=lambda k: int(k.split()[0]))
        for inst in disks:
            num, *letters = inst.split()
            did = f"disk:{num}"
            title = f"Disk {num} ({', '.join(letters)})" if letters else f"Disk {num}"
            kind = found.get("disks", {}).get(num, "")
            active = min(max(100 - idle[inst], 0.0), 100.0)
            devices.append({"id": did, "kind": "disk", "title": title, "sub": kind})
            values[did] = active / 100
            tips[did] = (title, "\n".join(filter(None, [
                kind, f"Active time  {active:.0f}%",
                f"Read  {_fmt_bytes(rd.get(inst, 0))}/s   Write  {_fmt_bytes(wr.get(inst, 0))}/s"])))

        # Network: only adapters Windows says are up, like Task Manager.
        sent, recv, bw = read("net_sent"), read("net_recv"), read("net_bw")
        for inst in sorted(sent):
            nic = found.get("nets", {}).get(inst)
            if not nic:
                continue
            name = str(nic.get("Name") or inst)
            media = str(nic.get("PhysicalMediaType") or "")
            title = "Wi-Fi" if "802.11" in media or "Wireless" in media else "Ethernet"
            nid = f"net:{name}"
            s, r = sent[inst] * 8, recv.get(inst, 0.0) * 8
            devices.append({"id": nid, "kind": "net", "title": title, "sub": name,
                            "hw": bool(nic.get("HardwareInterface"))})
            values[nid] = (s, r)
            link = bw.get(inst, 0.0)
            tips[nid] = (title, "\n".join(filter(None, [
                name, f"S: {_fmt_bits(s)}   R: {_fmt_bits(r)}",
                str(nic.get("InterfaceDescription") or ""),
                f"Link speed  {_fmt_bits(link)}" if link else ""])))

        # GPU: Task Manager's number is the busiest engine type, summed over processes.
        engines: dict[str, dict[str, float]] = {}
        for inst, v in read("gpu_eng").items():
            luid_at, eng_at = inst.find("luid_"), inst.find("engtype_")
            if luid_at < 0 or eng_at < 0:
                continue
            luid = inst[luid_at:luid_at + 26].lower()
            eng = inst[eng_at + 8:]
            per = engines.setdefault(luid, {})
            per[eng] = per.get(eng, 0.0) + v
        vram_used = {k[:26].lower(): v for k, v in read("gpu_mem").items()}
        for idx, (name, vram, luid) in enumerate(
                g for g in found.get("gpus", []) if _luid_key(g[2]) in vram_used):
            key = _luid_key(luid)
            gid = f"gpu:{idx}"
            util = min(max(engines.get(key, {}).values(), default=0.0), 100.0)
            temp = gpu_temperature(luid)
            title = f"GPU {idx}"
            devices.append({"id": gid, "kind": "gpu", "title": title, "sub": name})
            values[gid] = util / 100
            tips[gid] = (title, "\n".join(filter(None, [
                name, f"{util:.0f}%" + (f" ({temp:.0f} °C)" if temp is not None else ""),
                f"Dedicated memory  {_fmt_gb(vram_used.get(key, 0))}/{_fmt_gb(vram)} GB"
                if vram else ""])))

        instances = (tuple(disks), tuple(sorted(sent)), tuple(sorted(vram_used)))
        if self._discovered is None or instances != self._instances:
            self._instances = instances
            self._rediscover()

        with self._lock:
            if [d["id"] for d in devices] != [d["id"] for d in self._devices]:
                self.devices_version += 1
            self._devices = devices
            self._tips = tips
            for did, v in values.items():
                hist = self._history.setdefault(did, [])
                hist.append(v)
                del hist[:-PERF_HISTORY]
            for did in set(self._history) - set(values):
                del self._history[did]
            self.seq += 1


def _net_scale(peak: float) -> float:
    """Task Manager's graph top: the next 1 or 5 step at or above the peak
    (1, 5, 10, 50 Kbps ... 1, 5 Mbps), so spikes keep headroom."""
    top = PERF_NET_FLOOR
    while top < peak:
        top *= 5 if str(int(top))[0] == "1" else 2
    return float(top)


# Task Manager's flat line, (graph top in bits/s, height), measured off its
# thumbnail. It only moves when the scale does, sinking as the scale grows.
PERF_NET_REF = ((5e5, 0.920), (1e6, 0.807), (5e6, 0.761), (1e7, 0.716),
                (5e7, 0.602), (1e8, 0.511), (5e8, 0.466), (1e9, 0.398))


def _net_ref(top: float) -> float:
    """Height of Task Manager's flat line on a graph topped at top, between
    the measured points by log scale, continuing their slope past either end."""
    pts = [(math.log10(t), h) for t, h in PERF_NET_REF]
    x = math.log10(max(top, 1.0))
    i = min(max(bisect.bisect_right([p[0] for p in pts], x) - 1, 0), len(pts) - 2)
    (ax, ah), (bx, bh) = pts[i], pts[i + 1]
    return min(max(ah + (bh - ah) * (x - ax) / (bx - ax), 0.0), 1.0)


def perf_series(kind: str, hist: list) -> list[tuple[tuple[float, ...], bool, str]]:
    """(values 0..1, filled, style) per line. style "" is a plain 1 px line;
    "solid" and "dashed" are anti-aliased like Task Manager's network graph.

    Network matches Task Manager: receive solid over a fill, send dashed one
    px on, one off, both on one scale, plus its flat reference line.
    """
    if kind != "net":
        return [(tuple(min(max(float(v), 0.0), 1.0) for v in hist), True, "")]
    top = _net_scale(max([0.0] + [max(s, r) for s, r in hist]))
    return [(tuple(r / top for _s, r in hist), True, "solid"),
            (tuple(s / top for s, _r in hist), False, "dashed"),
            ((_net_ref(top),) * PERF_HISTORY, False, "solid")]


def _dashes(pts: list[tuple[float, float]], on: float = 1.0, off: float = 1.0):
    """Split a path into its dashes, measured along the path from its start."""
    run = [0.0]
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        run.append(run[-1] + math.hypot(bx - ax, by - ay))

    def at(s: float) -> tuple[float, float]:
        i = min(max(bisect.bisect_right(run, s) - 1, 0), len(pts) - 2)
        seg = run[i + 1] - run[i]
        t = (s - run[i]) / seg if seg else 0.0
        return (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
                pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t)

    out, a = [], 0.0
    while a < run[-1]:
        b = min(a + on, run[-1])
        inner = [pts[i] for i in range(len(pts)) if a < run[i] < b]
        out.append([at(a)] + inner + [at(b)])
        a += on + off
    return out


def _aa_path(img, box: tuple[int, int, int, int], pts: list[tuple[float, float]],
             color: str, dashed: bool) -> None:
    """A 1 px anti-aliased line clipped to box, the way Direct2D draws Task
    Manager's network lines. The dash phase walks with the path, so a scrolling
    send line flickers between dotted and an even half-bright line, as there."""
    from PIL import Image, ImageDraw

    k = 8
    bx0, by0, bx1, by1 = box
    mask = Image.new("L", ((bx1 - bx0) * k, (by1 - by0) * k), 0)
    d = ImageDraw.Draw(mask)
    # Each segment is filled as its own 1 px band. PIL fills polygon edges
    # inclusively, so pulling the band in half a subpixel gives true coverage.
    trim = 0.5 / k
    for part in (_dashes(pts) if dashed else [pts]):
        for (ax, ay), (bx, by) in zip(part, part[1:]):
            seg = math.hypot(bx - ax, by - ay)
            if seg < 1e-6:
                continue
            ux, uy = (bx - ax) / seg, (by - ay) / seg
            nx, ny = -uy * (0.5 - trim), ux * (0.5 - trim)
            ex, ey = bx - ux * trim, by - uy * trim
            d.polygon([((x - bx0) * k, (y - by0) * k) for x, y in (
                (ax + nx, ay + ny), (ex + nx, ey + ny), (ex - nx, ey - ny), (ax - nx, ay - ny))],
                fill=255)
    # Direct2D's anti-aliasing has 32 levels.
    cover = mask.resize((bx1 - bx0, by1 - by0), Image.BOX).point(
        lambda v: round(v * 32 / 255) * 255 // 32)
    img.paste(color, box, cover)


def perf_strip_width(count: int, h: int) -> int:
    if not count:
        return 0
    tile = round((h - 2 * READOUT_BARS_PAD) * PERF_TILE_RATIO)
    return count * tile + (count - 1) * PERF_TILE_GAP + READOUT_BARS_PAD


def _mix(a: str, b: str, amount: float) -> str:
    """Blend a toward b by amount (0..1)."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(pa, pb))


def render_perf_strip(graphs: list[tuple], w: int, h: int, bg: str):
    """Task Manager tiles, each (x, color, series, lifted). Newest sample on the right edge.

    Only the graph is colored. The tile is see-through (bg is the readout's
    transparent color) inside a faint neutral outline. x is fractional px so
    tiles can slide; a lifted tile (being dragged) gets a brighter outline.
    """
    from PIL import Image, ImageDraw

    s, pad = TRAY_SS, READOUT_BARS_PAD
    img = Image.new("RGB", (w * s, h * s), bg)
    d = ImageDraw.Draw(img)
    ch = h - 2 * pad
    tile = round(ch * PERF_TILE_RATIO)
    shade = _mix(bg, "#000000", 0.3)
    outline = _mix(shade, "#ffffff", 0.16)
    # Lines go on after the shrink: crisp 1px like Task Manager, no smear.
    lines: list[tuple[list[tuple[float, float]], str, str, tuple[int, int, int, int]]] = []
    for x, color, series, lifted in graphs:
        x0, y0 = round(x * s), pad * s
        x1, y1 = x0 + tile * s - 1, (pad + ch) * s - 1
        if lifted:
            d.rectangle((x0, y0, x1, y1), fill=shade)   # covers the tile it rides over
        step = (x1 - x0) / (PERF_HISTORY - 1)
        # Inside the outline. Task Manager puts the top on the centre of the top
        # row and 0 a px below the bottom, so idle lines vanish; its
        # oldest/newest points sit a quarter px in, and a flat line on one row.
        box = (math.ceil(x0 / s) + 1, pad + 1, (x1 + 1) // s - 1, pad + ch - 1)
        span = (box[2] - box[0] - 0.5) / (PERF_HISTORY - 1)
        for line, filled, style in series:
            if len(line) < 2:
                continue
            if style:
                ys = [box[3] + 1 - min(max(v, 0.0), 1.0) * (box[3] - box[1] + 0.5)
                      for v in line]
                if style == "solid" and not filled and len(set(line)) == 1:
                    ys = [round(box[3] - 0.5 - line[0] * (box[3] - box[1])) + 0.5] * len(line)
                xs = [box[2] - 0.25 - (len(line) - 1 - i) * span for i in range(len(line))]
                if len(set(line)) > 1:
                    xs = [math.floor(x) + 0.5 for x in xs]   # samples on pixel centres
                pts = list(zip(xs, ys))
                if filled:
                    d.polygon([(px * s, py * s) for px, py in pts]
                              + [(box[2] * s, pts[-1][1] * s), (box[2] * s, box[3] * s),
                                 (pts[0][0] * s, box[3] * s)],
                              fill=PERF_NET_FILL)
                lines.append((pts, color, style, box))
                continue
            pts = [(x1 - (len(line) - 1 - i) * step, y1 - v * (y1 - y0))
                   for i, v in enumerate(line)]
            if filled:
                d.polygon(pts + [(pts[-1][0], y1), (pts[0][0], y1)],
                          fill=_mix(shade if lifted else bg, color, 0.3))
            left, right = x0 / s + 1, (x1 + 1) / s - 2         # just inside the outline
            bottom = (y1 + 1) / s - 2
            lines.append(([(min(max(px / s, left), right), min(max(py / s, pad + 1), bottom))
                           for px, py in pts], color, "", box))
        d.rectangle((x0, y0, x1, y1), outline=_tint(outline, 0.45) if lifted else outline,
                    width=s)
    img = img.resize((w, h), Image.BOX)
    d = ImageDraw.Draw(img)
    for pts, color, style, box in lines:
        if style:
            _aa_path(img, box, pts, color, style == "dashed")
        else:
            d.line(pts, fill=color, width=1)
    return img


class UsageWidget(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.93)
        self.configure(bg=BG)
        self.resizable(False, False)

        self._on_top      = True
        self._drag_x      = 0
        self._drag_y      = 0
        self._dropped_off: set[str] = set()   # windows the user left hanging off-screen
        self._settings    = load_settings()
        self._pin_old_tray_defaults()
        self._login_notice = ""
        self._sync_startup_pref()
        self._seed_hidden()
        self._migrate_grok_hide_category_rows()
        self._settings_placed = False
        self._refresh_min = self._init_refresh_minutes()
        self._manage_mode = False
        self._last_rows: list[Row] = []
        self._plan = ""
        self._panels: dict[str, tuple] = {}
        self._source = ""
        self._destroyed   = False
        self._fetch_in_flight = False
        self._next_after_id: str | None = None

        # Self-update state
        self._version            = current_version()
        self._update_state       = "idle"   # idle, checking, current, available, installing, error
        self._update_detail      = ""        # SHA when available, error msg when error
        self._update_frame: tk.Frame | None = None

        # Settings popup (created on demand when ≡ pressed)
        self._settings_win: tk.Toplevel | None = None
        self._accounts_frame: tk.Frame | None = None
        self._perf_popout: tk.Toplevel | None = None
        self._perf_popout_body: tk.Frame | None = None

        # Tray state
        self._tray = None  # set by _setup_tray if pystray is available
        self._deps_missing      = not self._has_tray_deps()
        self._deps_install_state = "idle"   # idle, installing, error
        self._deps_install_error = ""

        self._build_ui()
        self._apply_icon()
        self.update_idletasks()
        # Header sets the width. A later row (the Grok plan line) must not
        # stretch the window past the Claude tab.
        self._chrome_w = self.winfo_reqwidth()
        self.minsize(self._chrome_w, 1)
        self.maxsize(self._chrome_w, self.winfo_screenheight())
        self._place_initial()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_tray()
        self._perf = PerfMonitor()
        self._perf_devices_drawn = -1
        self._readout_rows: list[Row] = []
        self._readout = TaskbarReadout(self, self._show_and_raise, self._readout_menu,
                                       self._readout_tip, self._perf_reorder)
        self._readout_tick()
        self._show_event = _k32.CreateEventW(None, False, False, SHOW_EVENT)
        self._show_event_tick()
        self._schedule_refresh()
        self._refresh_icons_if_changed()
        if self._start_tray_only():
            # Withdraw before the window is mapped so sign-in doesn't flash it.
            self.withdraw()
            self._update_tray_icon()
        else:
            self.lift()

        # Auto-check for updates a few seconds after startup so we don't
        # delay first paint.
        self.after(5000, self._auto_check_for_update)

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        hdr = tk.Frame(self, bg=BG, cursor="fleur")
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        self._bind_drag(hdr)

        title = tk.Label(hdr, text="AI Usage", bg=BG, fg=FG,
                         font=("Segoe UI", 9, "bold"), cursor="fleur")
        # The old Grok widget was sized by the words "Grok Usage". Pad the
        # shorter title so this window stays that same width.
        bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        title_pad = max(0, bold.measure("Grok Usage") - bold.measure("AI Usage"))
        title.pack(side="left", padx=(0, title_pad))
        self._bind_drag(title)

        # Header buttons (right-to-left order in the layout).
        close = self._make_icon_btn(hdr, "✕", self._on_close, size=11)
        close.pack(side="right", padx=(0, 4))

        self._gear_btn = self._make_icon_btn(hdr, "≡", self._toggle_manage, size=12)
        self._gear_btn.pack(side="right", padx=(0, 6))

        self._pin_btn = self._make_icon_btn(hdr, "📌", self._toggle_topmost, size=9)
        self._pin_btn.pack(side="right", padx=(12, 6))
        # Pin starts pinned (white) since the window initializes topmost
        self._pin_btn.config(fg="white")

        tk.Frame(self, bg=SEPARATOR, height=1).pack(fill="x", padx=6, pady=(0, 2))

        # Second row, only when both accounts are signed in. Kept out of the
        # title row so the window stays as narrow as the old Grok widget.
        self._tab_bar = tk.Frame(self, bg=BG)
        self._body = tk.Frame(self, bg=BG)
        self._body.pack(fill="x", padx=8, pady=(0, 4))
        self._sync_tabs()
        tk.Label(self._body, text="Loading…", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w")

        self._refresh_btn = self._make_refresh_button(self)
        self._refresh_btn.pack(fill="x", padx=8, pady=(4, 2))
        self._build_grok_color_toggle()

        self._ts_lbl = tk.Label(self, bg=BG, fg=FG_DIM, font=("Segoe UI", 7))
        self._ts_lbl.pack(anchor="e", padx=8, pady=(0, 4))

    def _make_icon_btn(self, parent: tk.Misc, text: str, cb, size: int = 11) -> tk.Label:
        lbl = tk.Label(parent, text=text, bg=BG, fg=ACCENT,
                       font=("Segoe UI", size), cursor="hand2",
                       bd=0, padx=0, pady=0)
        lbl.bind("<Button-1>", lambda _e: cb())
        return lbl

    def _bind_drag(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>",   self._drag_start)
        widget.bind("<B1-Motion>",       self._drag_move)
        widget.bind("<ButtonRelease-1>", lambda _e: (self._drop(self), self._save_position()))

    def _apply_icon(self) -> None:
        icon = REPO_DIR / "claude_widget.ico"
        if not icon.exists():
            return
        try:
            self.iconbitmap(default=str(icon))
        except tk.TclError:
            pass

    def _pin_old_tray_defaults(self) -> None:
        """Tray usage used to default on, in the Number style. New installs get
        off and Ring; an install from before keeps what it showed."""
        s = self._settings
        if not s or ("tray_dynamic" in s and "tray_style" in s):
            return
        s.setdefault("tray_dynamic", True)
        s.setdefault("tray_style", "percent")
        try:
            save_settings(s)
        except OSError:
            pass

    def _seed_hidden(self) -> None:
        """Default hidden rows, plus a one-time hide of Bot app on existing installs."""
        changed = False
        if "hidden" not in self._settings:
            self._settings["hidden"] = list(DEFAULT_HIDDEN)
            changed = True
        hidden = set(self._settings.get("hidden") or [])
        # Bot app matches the weekly pool. Hide it once; if the user shows it
        # again, leave that choice alone.
        if not self._settings.get("bot_app_default_hidden"):
            hidden.add(BOT_LABEL)
            self._settings["bot_app_default_hidden"] = True
            changed = True
        self._settings["hidden"] = sorted(hidden)
        self._hidden = hidden
        if changed:
            try:
                save_settings(self._settings)
            except OSError:
                pass

    def _place_bottom_right(self) -> None:
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{sw - WIDTH - 20}+{sh - 220}")
        self._fit_window(self)

    def _saved_position_visible(self, x: int, y: int) -> bool:
        w = max(self.winfo_reqwidth(), WIDTH)
        h = max(self.winfo_reqheight(), 1)
        on = rect_on_monitor(x, y, w, h)
        if on is not None:
            return on
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        return -100 < x < sw - 50 and -100 < y < sh - 50

    def _place_initial(self) -> None:
        """Restore last-known position if the user opted in, else bottom-right."""
        if self._settings.get("remember_position", True):
            x = self._settings.get("window_x")
            y = self._settings.get("window_y")
            if isinstance(x, int) and isinstance(y, int) and self._saved_position_visible(x, y):
                self.geometry(f"+{x}+{y}")
                # Left hanging off-screen last time: reopen there, and don't let the
                # first refresh yank it back. Reset position is the way out.
                self.update_idletasks()
                # Rows haven't loaded yet, so use the height it had when saved.
                saved_h = self._settings.get("window_h")
                w = max(self.winfo_reqwidth(), 1)
                h = max(self.winfo_reqheight(), saved_h if isinstance(saved_h, int) else 1)
                area = work_area_for_rect(x, y, w, h)
                if area and clamp_to_work_area(x, y, w, h, area) != (x, y, w, h):
                    self._dropped_off.add(str(self))
                return
        self._place_bottom_right()

    def _save_position(self) -> None:
        if not self._settings.get("remember_position", True):
            return
        try:
            self._settings["window_x"] = int(self.winfo_x())
            self._settings["window_y"] = int(self.winfo_y())
            self._settings["window_h"] = int(self.winfo_height())
            save_settings(self._settings)
        except (OSError, tk.TclError):
            pass

    def _fit_window(self, win: tk.Misc | None, *, refresh: bool = True) -> None:
        """Pull a window inside the monitor work area, with a margin.

        Uses the monitor under the window so a left, top, or side taskbar and
        a second monitor are all covered. The taskbar readout is not passed here.
        """
        if win is None or str(win) in self._dropped_off:
            return
        fitting = getattr(self, "_fitting_ids", None)
        if fitting is None:
            fitting = set()
            self._fitting_ids = fitting
        key = str(win)
        if key in fitting:
            return
        fitting.add(key)
        try:
            if not win.winfo_exists():
                return
            if refresh:
                win.update_idletasks()
                w = max(int(win.winfo_reqwidth()), 1)
                h = max(int(win.winfo_reqheight()), 1)
            else:
                w = int(win.winfo_width())
                h = int(win.winfo_height())
                if w < 20 or h < 20:
                    return
            x, y = int(win.winfo_x()), int(win.winfo_y())
            area = work_area_for_rect(x, y, w, h)
            if area is None:
                area = (0, 0, int(win.winfo_screenwidth()), int(win.winfo_screenheight()))
            nx, ny, nw, nh = clamp_to_work_area(x, y, w, h, area)
            if (nx, ny, nw, nh) == (x, y, w, h):
                return
            win.geometry(f"{nw}x{nh}+{nx}+{ny}")
        except tk.TclError:
            return
        finally:
            fitting.discard(key)

    def _fit_settings(self) -> None:
        if not self._settings_placed:
            return
        # Opened at a fixed height; grow or shrink with rows shown later.
        pop = self._settings_win
        if pop is not None and pop.winfo_exists():
            pop.update_idletasks()
            pop.geometry(f"{WIDTH}x{pop.winfo_reqheight()}")
        self._fit_window(pop)

    def _finish_layout(self) -> None:
        if self._destroyed:
            return
        self._sync_grok_color_row()
        self._fit_window(self)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        """Close button. If the user opted in (and the tray is up), hide; else quit."""
        if self._tray is not None and self._settings.get("minimize_on_close", False):
            self._hide_window()
        else:
            self._quit_app()

    def _quit_app(self) -> None:
        """Fully exit: stop tray loop, destroy window."""
        self._save_position()
        self._destroyed = True
        self._close_settings_window()
        self._readout.hide()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _show_and_raise(self) -> None:
        """Reveal the widget and bring it above other windows."""
        if self.state() == "withdrawn":
            self.deiconify()
        self.lift()
        self._update_tray_icon()
        # Force topmost briefly so we surface over other apps. If the user
        # hasn't pinned us, drop the topmost flag again after a moment so
        # the window doesn't permanently stick.
        self.attributes("-topmost", True)
        if not self._on_top:
            self.after(150, lambda: self.attributes("-topmost", False))

    def _refresh_icons_if_changed(self) -> None:
        """After an update ships a new .ico, Windows keeps showing the cached old one."""
        fp = icon_fingerprint()
        if not fp or fp == self._settings.get("icon_fingerprint"):
            return
        self._settings["icon_fingerprint"] = fp
        save_settings(self._settings)
        threading.Thread(target=refresh_shell_icons, daemon=True).start()

    def _show_event_tick(self) -> None:
        """A second launch signals this event instead of opening another widget."""
        if self._show_event and _k32.WaitForSingleObject(self._show_event, 0) == 0:
            self._show_and_raise()
        self._after_safe(SHOW_POLL_MS, self._show_event_tick)

    def _readout_menu(self, x: int, y: int) -> None:
        menu = tk.Menu(self, tearoff=0, bg=BG, fg=FG, activebackground=BTN_BG_HOVER,
                       activeforeground=FG, bd=0)
        menu.add_command(label="Reset position", command=self._reset_position)
        menu.add_command(label="Refresh", command=self._manual_refresh)
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _reset_position(self) -> None:
        """Bring the widget back to the bottom-right corner, fully on-screen."""
        self._show_and_raise()
        self._dropped_off.discard(str(self))
        self.update_idletasks()
        w = max(self.winfo_width(), self.winfo_reqwidth(), 1)
        h = max(self.winfo_height(), self.winfo_reqheight(), 1)
        area = _primary_work_area() or (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())
        # Clamping a far-off point lands it in the work area's bottom-right corner.
        x, y, _, _ = clamp_to_work_area(10**6, 10**6, w, h, area)
        self.geometry(f"+{x}+{y}")
        self._save_position()

    def _hide_window(self) -> None:
        self._close_settings_window()
        if self._manage_mode:
            self._manage_mode = False
            self._gear_btn.config(fg=ACCENT)
            if self._last_rows:
                self._render(self._last_rows)
        self.withdraw()
        self._update_tray_icon()

    def _sync_startup_pref(self) -> None:
        """Keep the settings file and the Startup shortcut on the same choice."""
        changed = False
        if "launch_on_startup" not in self._settings:
            self._settings["launch_on_startup"] = is_startup_enabled()
            changed = True
        if "startup_tray_only" not in self._settings:
            self._settings["startup_tray_only"] = False
            changed = True
        want = bool(self._settings.get("launch_on_startup"))
        stale = want and self._settings.get("startup_lnk_rev") != STARTUP_LNK_REV
        if want != is_startup_enabled() or stale:
            err = set_startup_enabled(want)
            if err:
                if changed:
                    try:
                        save_settings(self._settings)
                    except OSError:
                        pass
                return
            if want:
                self._settings["startup_lnk_rev"] = STARTUP_LNK_REV
            else:
                self._settings.pop("startup_lnk_rev", None)
            changed = True
        if changed:
            try:
                save_settings(self._settings)
            except OSError:
                pass

    def _start_tray_only(self) -> bool:
        """True when this process was launched at sign-in and should stay hidden."""
        return (
            launched_from_startup()
            and bool(self._settings.get("launch_on_startup"))
            and bool(self._settings.get("startup_tray_only"))
            and self._tray is not None
        )

    # ── tray ──────────────────────────────────────────────────────────────────

    def _setup_tray(self) -> None:
        """Create a system-tray icon. No-op if pystray or Pillow isn't installed."""
        try:
            import pystray
            from PIL import Image
        except ImportError:
            return

        png = REPO_DIR / "claude_widget.png"
        if not png.exists():
            return
        try:
            image = Image.open(png)
        except Exception:
            return

        # All tray-thread callbacks marshal back to the UI thread via after().
        def show(icon=None, item=None):
            self._after_safe(0, self._show_and_raise)

        def hide(icon=None, item=None):
            self._after_safe(0, self._hide_window)

        def refresh_now(icon=None, item=None):
            self._after_safe(0, self._manual_refresh)

        def quit_app(icon=None, item=None):
            self._after_safe(0, self._quit_app)

        def reset_position(icon=None, item=None):
            self._after_safe(0, self._reset_position)

        menu = pystray.Menu(
            pystray.MenuItem("Show", show, default=True),
            pystray.MenuItem("Reset position", reset_position),
            pystray.MenuItem("Hide", hide),
            pystray.MenuItem("Refresh now", refresh_now),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        )
        self._tray_base = image
        self._tray = pystray.Icon("claude-usage-widget", image,
                                  "AI Usage", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _tray_dynamic(self) -> bool:
        return bool(self._settings.get("tray_dynamic", False))

    def _tray_style(self) -> str:
        style = self._settings.get("tray_style")
        return style if style in dict(TRAY_STYLES) else "ring"

    def _tray_meter(self) -> tuple[str, str]:
        raw = self._settings.get("tray_meter")
        if isinstance(raw, list) and len(raw) == 2:
            return str(raw[0]), str(raw[1])
        return TRAY_DEFAULT_METER[0], TRAY_DEFAULT_METER[1]

    def _tray_row(self) -> Row | None:
        source, label = self._tray_meter()
        panel = self._panels.get(source)
        if not panel or panel[0] != "ok":
            return None
        for row in panel[1]:
            if row[0] == label:
                return row
        return None

    def _tray_util(self) -> float | None:
        row = self._tray_row()
        return row[1] if row else None

    def _readout_on(self) -> bool:
        return bool(self._settings.get("taskbar_readout", False))

    def _readout_style(self) -> str:
        style = self._settings.get("readout_style")
        return style if style in dict(READOUT_STYLES) else "ring"

    def _readout_bar_meters(self) -> str:
        meters = self._settings.get("readout_bar_meters")
        return meters if meters in dict(READOUT_BAR_METERS) else "both"

    def _picker_on(self) -> bool:
        return self._tray is not None and (self._tray_dynamic() or self._readout_on())

    def _update_readout(self) -> None:
        row = self._tray_row() if self._readout_on() else None
        style = self._readout_style()
        rows = [row] if row else []
        if row and style == "bars":
            panel = self._panels.get(self._tray_meter()[0])
            by_label = {r[0]: r for r in panel[1]} if panel and panel[0] == "ok" else {}
            meters = self._readout_bar_meters()
            labels = READOUT_STACK if meters == "both" else (meters,)
            if all(label in by_label for label in labels):
                rows = [by_label[label] for label in labels]
        self._readout_rows = rows
        self._readout.show(rows, style, self._perf_graphs())

    def _readout_tick(self) -> None:
        if self._destroyed:
            return
        if self._perf_on():
            self._perf.start()
        elif self._perf.running():
            self._perf.stop()
        if self._perf.devices_version != self._perf_devices_drawn:
            self._render_perf_devices()
        self._update_readout()
        self._after_safe(READOUT_POLL_MS, self._readout_tick)

    def _readout_tip(self, key: str) -> tuple[str, str] | None:
        if key != "usage":
            return self._perf.snapshot()[2].get(key)
        rows = self._readout_rows
        if not rows:
            return None
        source = SOURCE_TITLE.get(self._tray_meter()[0], "")
        lines = []
        for r in rows:
            when = r[2]
            if len(r) > 5 and r[5]:
                when = f"{when} ({r[5]})" if when else r[5]
            lines.append(f"{r[0]}  {min(int(r[1] * 100), 100)}%" + (f"\n{when}" if when else ""))
        return source, "\n".join(lines)

    # ── performance graphs ──

    def _perf_on(self) -> bool:
        return bool(self._settings.get("perf_graphs", True))

    def _perf_picked(self, dev: dict) -> bool:
        picks = self._settings.get("perf_picks")
        picks = picks if isinstance(picks, dict) else {}
        return bool(picks.get(dev["id"], perf_default_on(dev)))

    def _perf_graphs(self) -> list[tuple]:
        if not self._perf_on() or not self._perf.running():
            return []
        devices, history, _ = self._perf.snapshot()
        order = self._settings.get("perf_order")
        rank = {gid: i for i, gid in enumerate(order if isinstance(order, list) else [])}
        # Graphs never dragged keep Task Manager's order, after the dragged ones.
        picked = sorted((d for d in devices if self._perf_picked(d)),
                        key=lambda d: rank.get(d["id"], len(rank)))
        return [(d["id"], PERF_COLORS[d["kind"]], perf_series(d["kind"], history.get(d["id"], [])))
                for d in picked]

    def _perf_reorder(self, ids: list[str]) -> None:
        """Save a dragged order. Shown graphs trade places among their own saved
        slots; unshown or not-yet-found ones keep theirs."""
        old = self._settings.get("perf_order")
        old = [i for i in old if isinstance(i, str)] if isinstance(old, list) else []
        shown = set(ids)
        merged = old + [i for i in ids if i not in old]
        dragged = iter(ids)
        self._set_tray_setting("perf_order", [next(dragged) if i in shown else i for i in merged])

    def _set_perf_on(self, on: bool) -> str | None:
        err = self._set_tray_setting("perf_graphs", bool(on))
        self._sync_perf_devices_row()
        return err

    def _set_perf_pick(self, dev_id: str, on: bool) -> str | None:
        picks = self._settings.get("perf_picks")
        picks = dict(picks) if isinstance(picks, dict) else {}
        picks[dev_id] = bool(on)
        return self._set_tray_setting("perf_picks", picks)

    def _sync_perf_devices_row(self) -> None:
        """The graph picker sits on the on/off row, only while graphs are on."""
        btn = getattr(self, "_perf_pick_btn", None)
        if btn is None or not btn.winfo_exists():
            return
        if self._perf_on():
            if not btn.winfo_manager():
                btn.pack(side="right")
        else:
            btn.pack_forget()
            self._close_perf_popout()
        self._fit_settings()

    def _toggle_perf_popout(self) -> None:
        if self._perf_popout is not None:
            self._close_perf_popout()
        else:
            self._open_perf_popout()

    def _open_perf_popout(self) -> None:
        btn = getattr(self, "_perf_pick_btn", None)
        if btn is None or not btn.winfo_exists():
            return
        pop = tk.Toplevel(self)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=SEPARATOR)
        self._perf_popout = pop
        self._perf_popout_body = tk.Frame(pop, bg=BG, padx=8, pady=6)
        self._perf_popout_body.pack(padx=1, pady=1)
        self._render_perf_devices()
        self._after_safe(PERF_POPOUT_POLL_MS, self._perf_popout_tick)

    def _place_perf_popout(self) -> None:
        """Hang it from the picker button, kept inside the work area."""
        pop, btn = self._perf_popout, self._perf_pick_btn
        pop.update_idletasks()
        w, h = pop.winfo_reqwidth(), pop.winfo_reqheight()
        x = btn.winfo_rootx() + btn.winfo_width() - w      # right edges line up
        y = btn.winfo_rooty() + btn.winfo_height()
        area = work_area_for_rect(x, y, w, h) or (0, 0, pop.winfo_screenwidth(),
                                                    pop.winfo_screenheight())
        x, y, w, h = clamp_to_work_area(x, y, w, h, area)
        pop.geometry(f"+{x}+{y}")

    def _perf_popout_tick(self) -> None:
        """Close once the mouse is off both the pop-out and its button."""
        pop, btn = self._perf_popout, getattr(self, "_perf_pick_btn", None)
        if pop is None or not pop.winfo_exists() or btn is None or not btn.winfo_exists():
            self._close_perf_popout()
            return
        px, py = self.winfo_pointerxy()
        m = PERF_POPOUT_SLACK

        def over(w: tk.Misc) -> bool:
            x, y = w.winfo_rootx(), w.winfo_rooty()
            return x - m <= px < x + w.winfo_width() + m and y - m <= py < y + w.winfo_height() + m

        if not over(pop) and not over(btn):
            self._close_perf_popout()
            return
        self._after_safe(PERF_POPOUT_POLL_MS, self._perf_popout_tick)

    def _close_perf_popout(self) -> None:
        pop, self._perf_popout = self._perf_popout, None
        self._perf_popout_body = None
        if pop is not None:
            try:
                pop.destroy()
            except tk.TclError:
                pass

    def _render_perf_devices(self) -> None:
        body = self._perf_popout_body
        if body is None or not body.winfo_exists():
            return
        self._perf_devices_drawn = self._perf.devices_version
        for child in body.winfo_children():
            child.destroy()
        devices = self._perf.snapshot()[0]
        if not devices:
            tk.Label(body, text="Finding devices…", bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 7), bd=0, padx=0, pady=0).pack(anchor="w")
        for dev in devices:
            label = dev["title"]
            if dev["sub"] and dev["kind"] in ("net", "gpu"):
                label = f"{label}  ·  {dev['sub']}"
            self._make_checkbox(
                body, label,
                get_state=lambda d=dev: self._perf_picked(d),
                set_state=lambda on, i=dev["id"]: self._set_perf_pick(i, on),
                swatch=PERF_COLORS[dev["kind"]],
            )
        self._place_perf_popout()

    def _update_tray_icon(self) -> None:
        self._update_readout()
        if self._tray is None:
            return
        row = self._tray_row() if self._tray_dynamic() else None
        if row is None:
            image, title = self._tray_base, "AI Usage"
        else:
            source, label = self._tray_meter()
            util = row[1]
            image = render_tray_icon(util, self._tray_style(), tray_segments(row), row[3])
            title = f"{SOURCE_TITLE.get(source, source)} {label}: {min(int(util * 100), 100)}%"
        try:
            if self.state() == "withdrawn":
                title = f"{title} — click to open"
            self._tray.icon = image
            self._tray.title = title
        except Exception:  # noqa: BLE001 — tray thread may be gone at shutdown
            pass

    def _set_tray_setting(self, key: str, value) -> str | None:
        self._settings[key] = value
        try:
            save_settings(self._settings)
        except OSError as exc:
            return str(exc)
        self._update_tray_icon()
        if self._manage_mode and self._last_rows:
            self._render(self._last_rows)
        return None

    def _after_safe(self, *args, remember_id: bool = False, **kwargs) -> None:
        """`self.after` that silently no-ops if the window has been destroyed.

        If remember_id=True, stores the returned after-id so it can be cancelled
        (used for the next-poll timer so a manual refresh can supersede it).
        """
        if self._destroyed:
            return
        try:
            aid = self.after(*args, **kwargs)
        except tk.TclError:
            return
        if remember_id:
            self._next_after_id = aid

    # ── drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, e: tk.Event) -> None:
        self._drag_x = e.x_root - self.winfo_x()
        self._drag_y = e.y_root - self.winfo_y()

    def _drag_move(self, e: tk.Event) -> None:
        # No clamping mid-drag: fighting the mouse every motion event is what jittered.
        self.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    def _drop(self, win: tk.Misc) -> None:
        """A window dropped partly off-screen stays put until it is next opened."""
        try:
            x, y = int(win.winfo_x()), int(win.winfo_y())
            w, h = int(win.winfo_width()), int(win.winfo_height())
        except tk.TclError:
            return
        area = work_area_for_rect(x, y, w, h) or (
            0, 0, int(win.winfo_screenwidth()), int(win.winfo_screenheight()))
        if clamp_to_work_area(x, y, w, h, area) == (x, y, w, h):
            self._dropped_off.discard(str(win))
        else:
            self._dropped_off.add(str(win))

    # ── pin ───────────────────────────────────────────────────────────────────

    def _toggle_topmost(self) -> None:
        self._on_top = not self._on_top
        self.attributes("-topmost", self._on_top)
        self._pin_btn.config(fg="white" if self._on_top else ACCENT)

    # ── refresh loop ──────────────────────────────────────────────────────────

    def _schedule_refresh(self) -> None:
        """Kick off a fetch in a worker thread, dropping any pending timer."""
        if self._destroyed or self._fetch_in_flight:
            return
        self._next_after_id = None
        self._fetch_in_flight = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        next_delay_s = self._refresh_min * 60
        panels: dict[str, tuple] = {}
        for name in self._enabled_sources():
            if not self._source_signed_in(name):
                panels[name] = ("auth",)
                continue
            try:
                if name == "claude":
                    panels[name] = ("ok", parse_usage(fetch_usage()), claude_plan_label())
                elif name == "grok":
                    rows, plan = parse_grok_usage(fetch_grok_usage())
                    panels[name] = ("ok", rows, plan)
                else:
                    panels[name] = ("auth",)
            except Exception as exc:  # noqa: BLE001 — one source must not blank the other
                if is_auth_error(exc) or isinstance(exc, FileNotFoundError):
                    panels[name] = ("auth",)
                else:
                    panels[name] = ("error", str(exc))
                backoff = retry_after_seconds(exc)
                if backoff is not None:
                    next_delay_s = max(next_delay_s, backoff)
        self._after_safe(0, self._on_fetch_success, panels)
        self._fetch_in_flight = False
        self._after_safe(next_delay_s * 1000, self._schedule_refresh,
                         remember_id=True)

    def _on_fetch_success(self, panels: dict[str, tuple]) -> None:
        self._panels = panels
        grok = panels.get("grok")
        if grok and grok[0] == "ok":
            self._login_notice = ""
        self._show_active()
        self._update_tray_icon()
        self._ts_lbl.config(text=time.strftime("Updated %H:%M:%S"))

    def _show_source(self, name: str) -> bool:
        default = SOURCE_SHOW_DEFAULT.get(name, False)
        val = self._settings.get(f"show_{name}")
        if val is None:
            return default
        return bool(val)

    def _signed_out_set(self) -> set[str]:
        raw = self._settings.get("signed_out") or []
        if isinstance(raw, list):
            return set(raw)
        return set()

    def _source_signed_in(self, name: str) -> bool:
        if name in self._signed_out_set():
            return False
        return source_has_creds(name)

    def _enabled_sources(self) -> list[str]:
        """Accounts the user checked in settings. Sign-in is not required to show a tab."""
        return [name for name in SOURCE_ORDER if self._show_source(name)]

    def _sync_tabs(self) -> None:
        for child in self._tab_bar.winfo_children():
            child.destroy()
        sources = self._enabled_sources()
        if self._source not in sources:
            saved = self._settings.get("source")
            self._source = saved if saved in sources else (sources[0] if sources else "")
        if len(sources) < 2:
            self._tab_bar.pack_forget()
            return
        if not self._tab_bar.winfo_ismapped():
            self._tab_bar.pack(fill="x", padx=8, pady=(0, 2), before=self._body)
        row = tk.Frame(self._tab_bar, bg=BG)
        row.pack(fill="x")
        for name in sources:
            self._add_tab(row, name, name == self._source)
        tk.Frame(self._tab_bar, bg=SEPARATOR, height=1).pack(fill="x")

    def _add_tab(self, parent: tk.Misc, name: str, active: bool) -> None:
        bg = BTN_BG if active else BG
        bd = FG_DIM if active else ACCENT_OFF
        wrap = tk.Frame(
            parent, bg=bg, highlightthickness=1,
            highlightbackground=bd, highlightcolor=bd, cursor="hand2",
        )
        wrap.pack(side="left")
        many = len(self._enabled_sources()) >= 3
        lbl = tk.Label(
            wrap, text=SOURCE_TITLE[name], bg=bg,
            fg=FG if active else FG_DIM,
            font=("Segoe UI", 8, "bold" if active else "normal"),
            cursor="hand2", bd=0, padx=4 if many else 6, pady=1,
        )
        lbl.pack()
        handler = lambda _e, n=name: self._select_source(n)
        wrap.bind("<Button-1>", handler)
        lbl.bind("<Button-1>", handler)

    def _select_source(self, name: str) -> None:
        if name == self._source:
            return
        self._source = name
        self._settings["source"] = name
        try:
            save_settings(self._settings)
        except OSError:
            pass
        self._show_active()

    def _show_active(self) -> None:
        self._sync_tabs()
        if not self._enabled_sources():
            self._last_rows = []
            self._plan = ""
            self._reset_body()
            tk.Label(self._body, text="No accounts enabled.\nCheck one in ≡ settings.",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 8), justify="left",
                     wraplength=self._inner_w()).pack(anchor="w")
            self._finish_layout()
            return
        panel = self._panels.get(self._source)
        if not panel:
            self._last_rows = []
            self._plan = ""
            self._reset_body()
            self._finish_layout()
            return
        if panel[0] == "ok":
            self._plan = panel[2] if len(panel) > 2 else ""
            self._render(panel[1])
        elif panel[0] == "auth":
            self._show_login()
        else:
            self._show_error(panel[1] if len(panel) > 1 else "Error")

    def _set_show_source(self, name: str, enabled: bool) -> str | None:
        self._settings[f"show_{name}"] = bool(enabled)
        try:
            save_settings(self._settings)
        except OSError as exc:
            return str(exc)
        if enabled and not self._source:
            self._source = name
        if enabled and self._source not in self._enabled_sources():
            self._source = name
        self._show_active()
        if enabled and name not in self._panels:
            self._manual_refresh()
        return None

    def _sign_in(self, name: str) -> None:
        was_local = name in self._signed_out_set()
        gone = self._signed_out_set()
        gone.discard(name)
        self._settings["signed_out"] = sorted(gone)
        try:
            save_settings(self._settings)
        except OSError:
            pass
        has_creds = source_has_creds(name)
        panel = self._panels.get(name)
        # A local Sign out keeps the credential file. Signing back in reconnects
        # it. A missing file, or tokens the API rejected, has to go through login.
        rejected = (
            name == "grok" and has_creds and not was_local
            and bool(panel) and panel[0] == "auth"
        )
        if name == "claude":
            if not has_creds:
                if open_claude_login():
                    self._watch_for_creds(name, time.time() + CREDS_WATCH_S)
                else:
                    url = LOGIN_URLS.get("claude")
                    if url:
                        webbrowser.open(url)
        elif name == "grok" and (not has_creds or rejected):
            self._login_notice = ""
            since = grok_auth_mtime()
            started = open_grok_login() if find_grok_cli() else open_grok_install_and_login()
            if started:
                self._watch_for_creds(name, time.time() + CREDS_WATCH_S, since)
            else:
                self._login_notice = GROK_LOGIN_FAILED
        elif not has_creds:
            url = LOGIN_URLS.get(name)
            if url:
                webbrowser.open(url)
        self._refresh_account_rows()
        if self._show_source(name):
            self._source = name
        self._show_active()
        self._manual_refresh()

    def _watch_for_creds(self, name: str, deadline: float, since: int | None = None) -> None:
        """Refresh as soon as the terminal login writes credentials.

        `since` is the previous auth-file mtime. Pass it when a rejected file
        is already on disk so the watch waits for `grok login` to replace it.
        """
        if self._destroyed or time.time() > deadline:
            return
        ready = source_has_creds(name)
        if since is not None:
            ready = ready and grok_auth_mtime() != since
        if ready:
            self._login_notice = ""
            self._refresh_account_rows()
            self._manual_refresh()
            if since is None:
                return
            since = grok_auth_mtime()
        self._after_safe(2000, self._watch_for_creds, name, deadline, since)

    def _sign_out(self, name: str) -> None:
        gone = self._signed_out_set()
        gone.add(name)
        self._settings["signed_out"] = sorted(gone)
        try:
            save_settings(self._settings)
        except OSError:
            pass
        self._refresh_account_rows()
        self._show_active()
        self._manual_refresh()

    def _manual_refresh(self) -> None:
        """User-initiated refresh from the settings panel; debounced via in-flight flag."""
        if self._fetch_in_flight:
            return
        if self._next_after_id is not None:
            try:
                self.after_cancel(self._next_after_id)
            except tk.TclError:
                pass
            self._next_after_id = None
        self._schedule_refresh()

    # ── render ────────────────────────────────────────────────────────────────

    def _render(self, rows: list[Row]) -> None:
        self._last_rows = rows
        for w in self._body.winfo_children():
            w.destroy()

        self._add_plan_header(self._plan)

        if self._manage_mode:
            # Settings live in a separate popup; main body just shows every row
            # with its hide-checkbox so the user can toggle visibility.
            visible = rows
        else:
            if self._update_state in ("available", "installing", "error"):
                self._add_update_banner()
            elif self._deps_missing:
                self._add_deps_banner()
            visible = [r for r in rows if r[0] not in self._hidden]

        if not visible and not self._manage_mode:
            tk.Label(self._body, text="All rows hidden — click ≡ to show some",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 8),
                     wraplength=self._inner_w()).pack(anchor="w")
        elif (
            self._source == "grok"
            and self._grok_color_on()
            and any(row_kind(r) == "slice" for r in rows)
        ):
            self._render_grok_colored(rows, visible)
        else:
            for row in visible:
                self._add_plain_row(row)

        self._finish_layout()

    def _add_plan_header(self, plan: str) -> None:
        """Plan name at the top of the tab, above every meter."""
        if not plan:
            return
        tk.Label(
            self._body, text=plan, bg=BG, fg=FG_DIM,
            font=("Segoe UI", 7), bd=0, padx=0, pady=0,
            wraplength=self._inner_w(), justify="left",
        ).pack(anchor="w", pady=(0, 2))

    def _add_update_banner(self) -> None:
        s = self._update_state
        if s == "installing":
            text, clickable, action = "Downloading update… please wait", False, None
        elif s == "error":
            text, clickable, action = "Update failed — Click to retry", True, self._on_install_update
        else:
            text, clickable, action = "Download Update", True, self._on_install_update

        btn = tk.Label(
            self._body, text=text,
            bg=BTN_BG, fg=FG_DIM if not clickable else "white",
            font=("Segoe UI", 8, "bold"),
            cursor="arrow" if not clickable else "hand2",
            pady=4, bd=0, padx=0,
        )
        btn.pack(fill="x", pady=(0, 6))
        if clickable and action is not None:
            btn.bind("<Button-1>", lambda _e: action())
            btn.bind("<Enter>",   lambda _e: btn.config(bg=BTN_BG_HOVER))
            btn.bind("<Leave>",   lambda _e: btn.config(bg=BTN_BG))

        if s == "error" and self._update_detail:
            tk.Label(self._body, text=self._update_detail,
                     bg=BG, fg=BAR_HIGH, font=("Segoe UI", 7),
                     wraplength=self._inner_w(), justify="left",
                     bd=0, padx=0, pady=0).pack(anchor="w", pady=(0, 4))

    @staticmethod
    def _has_tray_deps() -> bool:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
            return True
        except ImportError:
            return False

    def _add_deps_banner(self) -> None:
        s = self._deps_install_state
        if s == "installing":
            text, clickable = "Installing…", False
        elif s == "error":
            text, clickable = "Install failed — Click to retry", True
        else:
            text, clickable = "Install missing tray icon", True

        btn = tk.Label(
            self._body, text=text,
            bg=BTN_BG, fg="white",
            font=("Segoe UI", 8, "bold"),
            cursor="hand2" if clickable else "watch",
            pady=4, bd=0, padx=0,
        )
        btn.pack(fill="x", pady=(0, 6))
        if clickable:
            btn.bind("<Button-1>", lambda _e: self._on_install_deps())
            btn.bind("<Enter>",   lambda _e: btn.config(bg=BTN_BG_HOVER))
            btn.bind("<Leave>",   lambda _e: btn.config(bg=BTN_BG))

        if s == "error" and self._deps_install_error:
            tk.Label(self._body, text=self._deps_install_error,
                     bg=BG, fg=BAR_HIGH, font=("Segoe UI", 7),
                     wraplength=self._inner_w(), justify="left",
                     bd=0, padx=0, pady=0).pack(anchor="w", pady=(0, 4))

    def _on_install_deps(self) -> None:
        if self._deps_install_state == "installing":
            return
        self._deps_install_state = "installing"
        self._deps_install_error = ""
        if self._last_rows:
            self._render(self._last_rows)
        threading.Thread(target=self._do_install_deps, daemon=True).start()

    def _do_install_deps(self) -> None:
        req = REPO_DIR / "requirements.txt"
        py  = Path(sys.executable).with_name("python.exe")
        if not py.exists():
            py = Path(sys.executable)
        try:
            r = subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet", "--user",
                 "-r", str(req)],
                cwd=REPO_DIR,
                capture_output=True, text=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._after_safe(0, self._deps_install_done, str(exc))
            return
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "?").strip()[:200]
            self._after_safe(0, self._deps_install_done, err)
            return
        # Success — relaunch so the new modules can be imported.
        self._after_safe(0, self._do_relaunch)

    def _deps_install_done(self, error: str) -> None:
        self._deps_install_state = "error"
        self._deps_install_error = error
        if self._last_rows:
            self._render(self._last_rows)

    def _populate_settings(self, parent: tk.Misc) -> None:
        panel = tk.Frame(parent, bg=BG)
        panel.pack(fill="x", pady=(0, 4))

        self._update_frame = tk.Frame(panel, bg=BG)
        self._update_frame.pack(fill="x", pady=(0, 8))
        self._render_update_section()

        self._refresh_lbl = tk.Label(
            panel, text=self._fmt_refresh_label(self._refresh_min),
            bg=BG, fg=FG, font=("Segoe UI", 8),
            bd=0, padx=0, pady=0)
        self._refresh_lbl.pack(anchor="w")

        scale = tk.Scale(
            panel, from_=0, to=len(MINUTE_PRESETS) - 1, resolution=1,
            orient="horizontal", showvalue=False,
            bg=BG, fg=FG, troughcolor=BAR_BG, highlightthickness=0,
            activebackground=ACCENT, sliderrelief="flat", borderwidth=0,
            command=lambda v: self._set_refresh_index(int(float(v))))
        scale.set(MINUTE_PRESETS.index(self._refresh_min))
        scale.pack(fill="x")

        # ── boolean options ──
        opts = tk.Frame(panel, bg=BG)
        opts.pack(fill="x", pady=(6, 0))

        self._add_section_header(opts, "Accounts", first=True)
        self._accounts_frame = tk.Frame(opts, bg=BG)
        self._accounts_frame.pack(fill="x")
        self._refresh_account_rows()

        self._add_section_header(opts, "Window")
        startup_block = tk.Frame(opts, bg=BG)
        startup_block.pack(fill="x")
        self._make_checkbox(
            startup_block, "Launch on startup",
            get_state=lambda: bool(self._settings.get("launch_on_startup", False)),
            set_state=self._set_launch_on_startup,
        )
        self._startup_tray_row = tk.Frame(startup_block, bg=BG)
        self._make_checkbox(
            self._startup_tray_row, "Taskbar icon only",
            get_state=lambda: bool(self._settings.get("startup_tray_only", False)),
            set_state=self._set_startup_tray_only,
            indent=14,
        )
        self._sync_startup_tray_row()
        self._make_checkbox(
            opts, "Keep minimized on close",
            get_state=lambda: self._settings.get("minimize_on_close", False),
            set_state=self._set_minimize_on_close,
        )
        self._make_checkbox(
            opts, "Re-open widget in same position",
            get_state=lambda: self._settings.get("remember_position", True),
            set_state=self._set_remember_position,
        )

        if self._tray is not None:
            self._add_section_header(opts, "Tray icon")
            self._make_checkbox(
                opts, "Show usage in tray icon",
                get_state=self._tray_dynamic,
                set_state=lambda on: self._set_tray_setting("tray_dynamic", bool(on)),
            )
            self._tray_style_frame = tk.Frame(opts, bg=BG)
            self._tray_style_frame.pack(fill="x", pady=(4, 0))
            self._render_tray_styles()
            self._make_checkbox(
                opts, "Readout on taskbar",
                get_state=self._readout_on,
                set_state=lambda on: self._set_tray_setting("taskbar_readout", bool(on)),
            )
            self._readout_style_frame = tk.Frame(opts, bg=BG)
            self._readout_style_frame.pack(fill="x", pady=(4, 0))
            self._render_readout_styles()
            tk.Label(opts, text="Pick the meter with ○ next to a row.",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 7),
                     bd=0, padx=0, pady=0).pack(anchor="w", pady=(3, 0))

            self._add_section_header(opts, "Performance graphs")
            perf_row = self._make_checkbox(
                opts, "Graphs on taskbar",
                get_state=self._perf_on,
                set_state=self._set_perf_on,
            )
            self._perf_pick_btn = self._make_dropdown_button(
                perf_row, "Graphs", self._toggle_perf_popout)
            self._sync_perf_devices_row()

    def _add_section_header(self, parent: tk.Misc, text: str, first: bool = False) -> None:
        if not first:
            tk.Frame(parent, bg=SEPARATOR, height=1).pack(fill="x", pady=(8, 4))
        tk.Label(parent, text=text, bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8), bd=0, padx=0, pady=0).pack(anchor="w")

    def _render_tray_styles(self) -> None:
        self._render_style_buttons(self._tray_style_frame, TRAY_STYLES,
                                   self._tray_style(), self._pick_tray_style)

    def _render_readout_styles(self) -> None:
        frame = self._readout_style_frame
        for child in frame.winfo_children():
            child.destroy()
        styles = tk.Frame(frame, bg=BG)
        styles.pack(fill="x")
        self._render_style_buttons(styles, READOUT_STYLES,
                                   self._readout_style(), self._pick_readout_style)
        if self._readout_style() == "bars":
            meters = tk.Frame(frame, bg=BG)
            meters.pack(fill="x", pady=(4, 0))
            self._render_style_buttons(meters, READOUT_BAR_METERS,
                                       self._readout_bar_meters(), self._pick_readout_bar_meters)
        self._fit_settings()

    def _render_style_buttons(self, frame: tk.Frame, styles, current: str, pick) -> None:
        for child in frame.winfo_children():
            child.destroy()
        for key, title in styles:
            active = key == current
            btn = tk.Label(
                frame, text=title, bg=BTN_BG if active else BG,
                fg=FG if active else FG_DIM,
                font=("Segoe UI", 8, "bold" if active else "normal"),
                highlightthickness=1,
                highlightbackground=FG_DIM if active else ACCENT_OFF,
                cursor="hand2", bd=0, padx=6, pady=1,
            )
            btn.pack(side="left", padx=(0, 4))
            btn.bind("<Button-1>", lambda _e, k=key: pick(k))

    def _pick_tray_style(self, key: str) -> None:
        self._set_tray_setting("tray_style", key)
        self._render_tray_styles()

    def _pick_readout_style(self, key: str) -> None:
        self._set_tray_setting("readout_style", key)
        self._render_readout_styles()

    def _pick_readout_bar_meters(self, key: str) -> None:
        self._set_tray_setting("readout_bar_meters", key)
        self._render_readout_styles()

    def _set_launch_on_startup(self, enabled: bool) -> str | None:
        err = set_startup_enabled(enabled)
        if err:
            return err
        self._settings["launch_on_startup"] = bool(enabled)
        if enabled:
            self._settings["startup_lnk_rev"] = STARTUP_LNK_REV
        else:
            self._settings.pop("startup_lnk_rev", None)
        try:
            save_settings(self._settings)
        except OSError as exc:
            return str(exc)
        self._sync_startup_tray_row()
        return None

    def _set_startup_tray_only(self, enabled: bool) -> str | None:
        if enabled and self._tray is None:
            return "Install the tray icon first."
        self._settings["startup_tray_only"] = bool(enabled)
        try:
            save_settings(self._settings)
        except OSError as exc:
            return str(exc)
        return None

    def _sync_startup_tray_row(self) -> None:
        """Taskbar icon only is a child of Launch on startup."""
        row = getattr(self, "_startup_tray_row", None)
        if row is None or not row.winfo_exists():
            return
        if self._settings.get("launch_on_startup"):
            if not row.winfo_ismapped():
                row.pack(fill="x")
        else:
            row.pack_forget()
        self._fit_settings()

    def _set_minimize_on_close(self, enabled: bool) -> str | None:
        self._settings["minimize_on_close"] = bool(enabled)
        try:
            save_settings(self._settings)
        except OSError as exc:
            return str(exc)
        return None

    def _set_remember_position(self, enabled: bool) -> str | None:
        self._settings["remember_position"] = bool(enabled)
        if enabled:
            # Capture the current position right now so a later restart
            # uses it even if the user never drags the window again.
            self._settings["window_x"] = int(self.winfo_x())
            self._settings["window_y"] = int(self.winfo_y())
        try:
            save_settings(self._settings)
        except OSError as exc:
            return str(exc)
        return None

    def _make_checkbox(self, parent: tk.Misc, label: str,
                        get_state, set_state, *, indent: int = 0,
                        swatch: str | None = None) -> tk.Frame:
        """Settings-row checkbox styled to match the row hide checkboxes.

        get_state: () -> bool
        set_state: (new_value: bool) -> str | None   (returns error or None)
        indent: left inset for a child of the row above.
        swatch: color of a small square before the label.
        """
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(2, 0), padx=(indent, 0))

        state = {"on": bool(get_state())}

        box = tk.Label(row, text="☑" if state["on"] else "☐",
                       bg=BG, fg=FG, font=("Segoe UI", 9),
                       cursor="hand2", bd=0, padx=0, pady=0)
        box.pack(side="left", padx=(0, 4))

        if swatch:
            tk.Frame(row, bg=swatch, width=7, height=7).pack(side="left", padx=(0, 5))

        lbl = tk.Label(row, text=label, bg=BG, fg=FG,
                       font=("Segoe UI", 8), cursor="hand2",
                       bd=0, padx=0, pady=0)
        lbl.pack(side="left")

        err_lbl: list[tk.Label] = []

        def toggle(_e=None):
            new = not state["on"]
            err = set_state(new)
            if err:
                if not err_lbl:
                    e = tk.Label(row, text=err, bg=BG, fg=BAR_HIGH,
                                 font=("Segoe UI", 7),
                                 wraplength=WIDTH - 30, justify="left",
                                 bd=0, padx=0, pady=0)
                    e.pack(anchor="w")
                    err_lbl.append(e)
                else:
                    err_lbl[0].config(text=err)
                self._fit_settings()
                return
            state["on"] = new
            box.config(text="☑" if new else "☐")
            if err_lbl:
                err_lbl[0].destroy()
                err_lbl.clear()
            self._fit_settings()

        box.bind("<Button-1>", toggle)
        lbl.bind("<Button-1>", toggle)
        return row

    def _make_action_button(self, parent: tk.Misc, text: str, cb) -> tk.Label:
        btn = tk.Label(parent, text=text,
                       bg=BTN_BG, fg=FG, font=("Segoe UI", 8),
                       cursor="hand2", pady=3,
                       bd=0, padx=0)
        btn.bind("<Button-1>", lambda _e: cb())
        btn.bind("<Enter>",   lambda _e: btn.config(bg=BTN_BG_HOVER))
        btn.bind("<Leave>",   lambda _e: btn.config(bg=BTN_BG))
        return btn

    def _make_dropdown_button(self, parent: tk.Misc, text: str, cb) -> tk.Frame:
        """Gray button with a full-size ▾; the arrow is its own label so it can be bigger."""
        btn = tk.Frame(parent, bg=BTN_BG, cursor="hand2")
        tk.Label(btn, text=text, bg=BTN_BG, fg=FG, font=("Segoe UI", 8),
                 bd=0, padx=0, pady=0).pack(side="left", padx=(8, 3))
        tk.Label(btn, text="▾", bg=BTN_BG, fg=FG, font=("Segoe UI", 11),
                 bd=0, padx=0, pady=0).pack(side="left", padx=(0, 6))
        parts = [btn, *btn.winfo_children()]

        def paint(bg: str) -> None:
            for p in parts:
                p.config(bg=bg)

        for p in parts:
            p.bind("<Button-1>", lambda _e: cb())
            p.bind("<Enter>", lambda _e: paint(BTN_BG_HOVER))
            p.bind("<Leave>", lambda _e: paint(BTN_BG))
        return btn

    def _make_refresh_button(self, parent: tk.Misc) -> tk.Label:
        return self._make_action_button(parent, "↻  Refresh", self._manual_refresh)

    def _toggle_manage(self) -> None:
        self._manage_mode = not self._manage_mode
        self._gear_btn.config(fg="white" if self._manage_mode else ACCENT)
        if self._manage_mode:
            self._open_settings_window()
        else:
            self._close_settings_window()
        if self._last_rows:
            self._render(self._last_rows)
        else:
            self._finish_layout()

    def _open_settings_window(self) -> None:
        """Pop a separate Toplevel with the settings UI, clamped on-screen."""
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return

        pop = tk.Toplevel(self)
        pop.overrideredirect(True)
        pop.configure(bg=BG)
        pop.attributes("-topmost", True)
        pop.attributes("-alpha", 0.93)

        hdr = tk.Frame(pop, bg=BG, cursor="fleur")
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        title = tk.Label(hdr, text="Settings", bg=BG, fg=FG,
                         font=("Segoe UI", 9, "bold"), cursor="fleur")
        title.pack(side="left")
        close = self._make_icon_btn(hdr, "✕", self._toggle_manage, size=11)
        close.pack(side="right", padx=(0, 4))

        drag = {"x": 0, "y": 0}
        def _ds(e: tk.Event) -> None:
            drag["x"] = e.x_root - pop.winfo_x()
            drag["y"] = e.y_root - pop.winfo_y()
        def _dm(e: tk.Event) -> None:
            pop.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")
        for w in (hdr, title):
            w.bind("<ButtonPress-1>", _ds)
            w.bind("<B1-Motion>", _dm)
            w.bind("<ButtonRelease-1>", lambda _e: self._drop(pop))

        tk.Frame(pop, bg=SEPARATOR, height=1).pack(fill="x", padx=6, pady=(0, 4))

        body = tk.Frame(pop, bg=BG)
        body.pack(fill="x", padx=8, pady=(0, 8))

        self._settings_win = pop
        self._populate_settings(body)

        pop.update_idletasks()
        pop.geometry(f"{WIDTH}x{pop.winfo_reqheight()}")
        pop.update_idletasks()
        self._position_settings_window()
        self._settings_placed = True
        self._fit_settings()

    def _position_settings_window(self) -> None:
        """Place popup adjacent to widget, inside the monitor work area."""
        pop = self._settings_win
        if pop is None or not pop.winfo_exists():
            return
        pop.update_idletasks()
        pw = max(pop.winfo_width(), pop.winfo_reqwidth(), 1)
        ph = max(pop.winfo_height(), pop.winfo_reqheight(), 1)
        wx, wy = self.winfo_x(), self.winfo_y()
        ww = max(self.winfo_width(), 1)
        wh = max(self.winfo_height(), 1)
        area = work_area_for_rect(wx, wy, ww, wh)
        if area is None:
            area = (0, 0, pop.winfo_screenwidth(), pop.winfo_screenheight())
        gap = 4
        left, top, right, bottom = area
        m = WORK_MARGIN

        candidates = [
            (wx - pw - gap, wy),           # left of widget (widget is usually bottom-right)
            (wx + ww + gap, wy),           # right of widget
            (wx, wy - ph - gap),           # above widget
            (wx, wy + wh + gap),           # below widget
        ]
        for x, y in candidates:
            if (x >= left + m and y >= top + m
                    and x + pw <= right - m and y + ph <= bottom - m):
                pop.geometry(f"{pw}x{ph}+{x}+{y}")
                return
        # No side fits cleanly — keep the left-of spot, pulled into the work area.
        x, y, pw, ph = clamp_to_work_area(*candidates[0], pw, ph, area)
        pop.geometry(f"{pw}x{ph}+{x}+{y}")

    def _close_settings_window(self) -> None:
        self._close_perf_popout()
        pop = self._settings_win
        self._settings_win = None
        self._settings_placed = False
        if pop is not None:
            self._dropped_off.discard(str(pop))
            try:
                pop.destroy()
            except tk.TclError:
                pass

    def _toggle_hidden(self, label: str) -> None:
        self._hidden ^= {label}
        self._settings["hidden"] = sorted(self._hidden)
        save_settings(self._settings)
        if self._last_rows:
            self._render(self._last_rows)

    def _init_refresh_minutes(self) -> int:
        """Load refresh interval (minutes), migrating legacy seconds key if present."""
        raw = self._settings.get("refresh_minutes")
        if raw is None and "refresh_seconds" in self._settings:
            try:
                raw = max(1, round(int(self._settings["refresh_seconds"]) / 60))
            except (TypeError, ValueError):
                raw = REFRESH_DEFAULT_MIN
            del self._settings["refresh_seconds"]
        minutes = self._snap_to_preset(raw if raw is not None else REFRESH_DEFAULT_MIN)
        if self._settings.get("refresh_minutes") != minutes:
            self._settings["refresh_minutes"] = minutes
            save_settings(self._settings)
        return minutes

    @staticmethod
    def _snap_to_preset(value: object) -> int:
        """Round any number to the nearest MINUTE_PRESETS entry."""
        try:
            n = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return REFRESH_DEFAULT_MIN
        return min(MINUTE_PRESETS, key=lambda m: abs(m - n))

    def _set_refresh_index(self, idx: int) -> None:
        if not 0 <= idx < len(MINUTE_PRESETS):
            return
        minutes = MINUTE_PRESETS[idx]
        if minutes == self._refresh_min:
            return
        self._refresh_min = minutes
        self._settings["refresh_minutes"] = minutes
        save_settings(self._settings)
        if hasattr(self, "_refresh_lbl"):
            try:
                self._refresh_lbl.config(text=self._fmt_refresh_label(minutes))
            except tk.TclError:
                pass

    # ── self-update UI ──

    def _render_update_section(self) -> None:
        frame = self._update_frame
        if frame is None or not frame.winfo_exists():
            return
        for w in frame.winfo_children():
            w.destroy()

        tk.Label(frame, text=f"Version: {self._version}",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 7),
                 bd=0, padx=0, pady=0).pack(anchor="w")

        state, detail = self._update_state, self._update_detail

        # Button: label and action change based on state. Always present.
        if state == "checking":
            label  = "Checking… please wait"
            action = None
        elif state == "installing":
            label  = "Downloading update… please wait"
            action = None
        elif state == "available":
            label  = "Download Update"
            action = self._on_install_update
        else:  # idle / current / error
            label  = "Check for updates"
            action = self._on_check_update

        btn = self._make_action_button(frame, label, action or (lambda: None))
        if action is None:
            # Disabled-looking button while a worker is running.
            btn.config(fg=FG_DIM, cursor="arrow")
            btn.unbind("<Button-1>")
            btn.unbind("<Enter>")
            btn.unbind("<Leave>")
        btn.pack(fill="x", pady=(2, 0))

        # Status text below the button.
        status_text, status_fg, status_font_size = "", FG_DIM, 8
        if state == "current":
            status_text, status_fg = "✓ Up to date", BAR_LOW
        elif state == "error":
            status_text, status_fg, status_font_size = detail, BAR_HIGH, 7

        if status_text:
            tk.Label(frame, text=status_text, bg=BG, fg=status_fg,
                     font=("Segoe UI", status_font_size),
                     wraplength=WIDTH - 20, justify="left",
                     bd=0, padx=0, pady=0).pack(anchor="w", pady=(2, 0))
        self._fit_settings()

    def _set_update_state(self, state: str, detail: str = "") -> None:
        self._update_state  = state
        self._update_detail = detail
        self._render_update_section()
        if not self._manage_mode:
            self._render(self._last_rows)

    def _auto_check_for_update(self) -> None:
        """Background update check fired once at startup; silent unless an update is found."""
        if self._update_state != "idle":
            return
        threading.Thread(target=self._do_auto_check, daemon=True).start()

    def _do_auto_check(self) -> None:
        status, detail = check_for_update()
        if status != "available":
            return   # stay silent on error/up-to-date
        def apply():
            self._update_state  = "available"
            self._update_detail = detail
            self._render_update_section()    # in case settings panel is open
            if self._last_rows:               # re-render body so banner shows
                self._render(self._last_rows)
        self._after_safe(0, apply)

    def _on_check_update(self) -> None:
        if self._update_state in ("checking", "installing"):
            return
        self._set_update_state("checking")
        threading.Thread(target=self._do_check_update, daemon=True).start()

    def _do_check_update(self) -> None:
        status, detail = check_for_update()
        self._after_safe(0, self._set_update_state, status, detail)

    def _on_install_update(self) -> None:
        if self._update_state in ("checking", "installing"):
            return
        self._set_update_state("installing")
        threading.Thread(target=self._do_install_update, daemon=True).start()

    def _do_install_update(self) -> None:
        err = install_update()
        if err:
            self._after_safe(0, self._set_update_state, "error", err)
            return
        # Pull succeeded — relaunch so the new code takes effect.
        self._after_safe(0, self._do_relaunch)

    def _do_relaunch(self) -> None:
        self._destroyed = True
        self._close_settings_window()
        self._readout.hide()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        try:
            self.destroy()
        except tk.TclError:
            pass
        relaunch_widget()

    @staticmethod
    def _fmt_refresh_label(minutes: int) -> str:
        if minutes >= 60:
            hours = minutes // 60
            return f"Refresh timer: {hours} hr" if hours == 1 else f"Refresh timer: {hours} hrs"
        unit = "min" if minutes == 1 else "min"
        return f"Refresh timer: {minutes} {unit}"

    def _ui_font(self, size: int, bold: bool = False) -> tkfont.Font:
        cache = getattr(self, "_ui_fonts", None)
        if cache is None:
            cache = {}
            self._ui_fonts = cache
        key = (size, bold)
        font = cache.get(key)
        if font is None:
            font = tkfont.Font(
                family="Segoe UI", size=size,
                weight="bold" if bold else "normal",
            )
            cache[key] = font
        return font

    def _inner_w(self) -> int:
        chrome = getattr(self, "_chrome_w", 0) or WIDTH
        return max(40, chrome - 16)

    @staticmethod
    def _split_plan(sub: str) -> tuple[str, str]:
        mark = " · "
        if mark in sub:
            tier, rest = sub.split(mark, 1)
            return tier, rest
        return "", sub

    def _fit_clock(self, left: str, right: str) -> str:
        """Use a 3-letter weekday when the full name would widen the window."""
        if not right:
            return right
        font = self._ui_font(7)
        inner = self._inner_w()
        if font.measure(left) + font.measure(right) <= inner:
            return right
        day, _, rest = right.partition(" ")
        if rest and len(day) > 3:
            return f"{day[:3]} {rest}"
        return right

    def _grok_color_on(self) -> bool:
        return bool(self._settings.get("grok_color_categories", True))

    def _grok_hide_category_rows(self) -> bool:
        return bool(self._settings.get("grok_hide_category_rows", False))

    def _migrate_grok_hide_category_rows(self) -> None:
        """Invert grok_show_category_rows once: show=False becomes hide=True."""
        if "grok_hide_category_rows" in self._settings:
            return
        if "grok_show_category_rows" not in self._settings:
            return
        show = bool(self._settings.get("grok_show_category_rows"))
        self._settings["grok_hide_category_rows"] = not show
        del self._settings["grok_show_category_rows"]
        try:
            save_settings(self._settings)
        except OSError:
            pass

    def _add_grok_manage_check(self, parent: tk.Misc, text: str, on_toggle,
                               *, pady: tuple[int, int] = (0, 0)) -> tk.Label:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=pady)
        box = tk.Label(row, text="☑", bg=BG, fg=FG, font=("Segoe UI", 9),
                       cursor="hand2", bd=0, padx=0, pady=0)
        box.pack(side="left", padx=(0, 4))
        lbl = tk.Label(row, text=text, bg=BG, fg=FG,
                       font=("Segoe UI", 8), cursor="hand2",
                       bd=0, padx=0, pady=0)
        lbl.pack(side="left")
        box.bind("<Button-1>", on_toggle)
        lbl.bind("<Button-1>", on_toggle)
        return box

    def _build_grok_color_toggle(self) -> None:
        """Manage-mode checkboxes under the Grok rows, above Refresh."""
        wrap = tk.Frame(self, bg=BG)
        self._grok_color_row = wrap
        self._grok_color_box = self._add_grok_manage_check(
            wrap, "Color-code categories", self._toggle_grok_color)
        self._grok_cat_box = self._add_grok_manage_check(
            wrap, "Hide category rows", self._toggle_grok_cat_rows, pady=(2, 0))
        self._sync_grok_color_box()
        self._sync_grok_cat_box()

    def _sync_check_box(self, box: tk.Label | None, on: bool) -> None:
        if box is None:
            return
        try:
            box.config(text="☑" if on else "☐")
        except tk.TclError:
            pass

    def _sync_grok_color_box(self) -> None:
        self._sync_check_box(getattr(self, "_grok_color_box", None), self._grok_color_on())

    def _sync_grok_cat_box(self) -> None:
        self._sync_check_box(getattr(self, "_grok_cat_box", None), self._grok_hide_category_rows())

    def _sync_grok_color_row(self) -> None:
        row = getattr(self, "_grok_color_row", None)
        if row is None or not row.winfo_exists():
            return
        panel = self._panels.get(self._source)
        show = (
            self._manage_mode
            and self._source == "grok"
            and bool(panel) and panel[0] == "ok"
            and bool(self._last_rows)
        )
        if show:
            self._sync_grok_color_box()
            self._sync_grok_cat_box()
            # winfo_ismapped is false while the window is withdrawn (tray-only
            # startup), so use the packer state to show and hide the row.
            if not row.winfo_manager():
                row.pack(fill="x", padx=8, pady=(2, 0), before=self._refresh_btn)
        elif row.winfo_manager():
            row.pack_forget()

    def _persist_grok_flag(self, key: str, value: bool) -> None:
        self._settings[key] = value
        try:
            save_settings(self._settings)
        except OSError:
            pass
        if self._source == "grok" and self._last_rows:
            self._render(self._last_rows)

    def _toggle_grok_color(self, _e: tk.Event | None = None) -> None:
        self._persist_grok_flag("grok_color_categories", not self._grok_color_on())
        self._sync_grok_color_box()

    def _toggle_grok_cat_rows(self, _e: tk.Event | None = None) -> None:
        self._persist_grok_flag("grok_hide_category_rows", not self._grok_hide_category_rows())
        self._sync_grok_cat_box()

    def _pick_grok_category_color(self, label: str, current: str) -> None:
        result = colorchooser.askcolor(
            color=current, title=f"{label} color", parent=self)
        if not result or not result[1]:
            return
        chosen = result[1]
        colors = self._settings.get("grok_category_colors")
        if not isinstance(colors, dict):
            colors = {}
        else:
            colors = dict(colors)
        colors[label] = chosen
        self._settings["grok_category_colors"] = colors
        try:
            save_settings(self._settings)
        except OSError:
            pass
        if self._source == "grok" and self._last_rows:
            self._render(self._last_rows)

    def _add_plain_row(self, row: tuple) -> None:
        if row_kind(row) == "bot-same":
            self._add_bot_note(row)
            return
        self._add_row(*row[:6])

    def _render_grok_colored(self, rows: list, visible: list) -> None:
        """Weekly pool as one stacked bar, products as a color legend under it."""
        shown = {r[0] for r in visible}
        pool = next((r for r in rows if row_kind(r) == "pool"), None)
        slices = [r for r in rows if row_kind(r) == "slice"]
        bots = [r for r in rows if row_kind(r) in ("bot", "bot-same")]
        others = [r for r in rows if row_kind(r) not in ("pool", "slice", "bot", "bot-same")]

        used: set[str] = set()
        custom = self._settings.get("grok_category_colors") or {}
        if not isinstance(custom, dict):
            custom = {}
        color_of = {r[0]: grok_slice_color(r[0], used, custom) for r in slices}
        stack_rows = [r for r in slices if r[0] not in self._hidden]
        if pool is not None and pool[0] in shown:
            hidden_pool = pool[0] in self._hidden
            if stack_rows:
                widths = stack_fractions(
                    [r[1] for r in stack_rows], pool[1],
                    complete=len(stack_rows) == len(slices),
                )
                segments = [(w, color_of[r[0]]) for r, w in zip(stack_rows, widths)]
                if hidden_pool:
                    segments = [(w, FG_DIM) for w, _c in segments]
            else:
                # Every category is hidden: one status-colored fill so the
                # weekly percent and the bar still describe the same total.
                fill = FG_DIM if hidden_pool else bar_color(pool[1])
                segments = [(min(max(pool[1], 0.0), 1.0), fill)]
            self._add_grok_pool(pool, segments)

        # Hide only folds the legend while manage mode is closed. The stacked
        # weekly bar always keeps every (non-hidden) category segment.
        if self._manage_mode or not self._grok_hide_category_rows():
            for row in slices:
                if row[0] in shown:
                    self._add_legend_row(row, color_of[row[0]])
            for row in bots:
                if row[0] in shown:
                    self._add_plain_row(row)
        for row in others:
            if row[0] in shown:
                self._add_plain_row(row)

    def _add_grok_pool(self, row: tuple, segments: list[tuple[float, str]]) -> None:
        label, util, sub, time_progress, day_notches, reset_when = row[:6]
        hidden = label in self._hidden
        pct = min(int(util * 100), 100)
        frame = tk.Frame(self._body, bg=BG)
        frame.pack(fill="x", pady=(4, 0))
        self._pack_row_title(
            frame, label, f"{pct}%",
            FG_DIM if hidden else bar_color(util),
        )
        self._mount_stacked_bar(frame, segments)
        if time_progress is not None:
            self._mount_time_bar(
                frame, time_progress,
                FG_DIM if hidden else TIME_BAR_FG, day_notches,
            )
        self._pack_detail(frame, sub, reset_when)

    def _add_legend_row(self, row: tuple, color: str) -> None:
        label, util = row[0], row[1]
        hidden = label in self._hidden
        pct = min(int(util * 100), 100)
        frame = tk.Frame(self._body, bg=BG)
        frame.pack(fill="x", pady=(2, 0))
        self._pack_row_title(
            frame, label, f"{pct}%",
            FG_DIM if hidden else FG,
            swatch=color,
        )

    def _add_bot_note(self, row: tuple) -> None:
        """Bot app reading of the same weekly limit. Not a stack segment."""
        frame = tk.Frame(self._body, bg=BG)
        frame.pack(fill="x", pady=(2, 0))
        self._pack_row_title(
            frame, row[0], "", FG_DIM,
            label_fg=FG_DIM, suffix="same weekly limit",
        )

    def _pack_row_title(self, parent: tk.Misc, label: str, right_text: str, right_fg: str,
                        *, swatch: str | None = None, label_fg: str | None = None,
                        suffix: str = "") -> None:
        hidden = label in self._hidden
        active_fg = FG_DIM if hidden else (label_fg or FG)
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x")

        if self._manage_mode:
            box = tk.Label(top, text="☐" if hidden else "☑", bg=BG, fg=active_fg,
                           font=("Segoe UI", 9), cursor="hand2",
                           bd=0, padx=0, pady=0)
            box.pack(side="left", padx=(0, 4))
            box.bind("<Button-1>", lambda _e, l=label: self._toggle_hidden(l))

        if swatch:
            size = 8
            fill = FG_DIM if hidden else swatch
            sq = tk.Canvas(top, width=size, height=size, bg=BG,
                           highlightthickness=0, bd=0)
            sq.create_rectangle(0, 0, size, size, fill=fill, width=0)
            sq.pack(side="left", padx=(0, 6))
            if self._manage_mode:
                sq.configure(cursor="hand2")
                sq.bind("<Button-1>", lambda _e, l=label, c=swatch: self._pick_grok_category_color(l, c))

        tk.Label(top, text=label, bg=BG, fg=active_fg,
                 font=("Segoe UI", 8), bd=0, padx=0, pady=0).pack(side="left")
        if suffix:
            tk.Label(top, text=suffix, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 7), bd=0, padx=0, pady=0).pack(side="left", padx=(6, 0))
        if right_text:
            tk.Label(top, text=right_text, bg=BG, fg=right_fg,
                     font=("Segoe UI", 8, "bold"),
                     bd=0, padx=0, pady=0).pack(side="right")

        if self._manage_mode and self._picker_on():
            picked = self._tray_meter() == (self._source, label)
            # Drawn, not a glyph: ○ and ● render at different sizes.
            dot = tk.Canvas(top, width=TRAY_DOT, height=TRAY_DOT, bg=BG,
                            highlightthickness=0, bd=0, cursor="hand2")
            dot.create_oval(1, 1, TRAY_DOT - 2, TRAY_DOT - 2, outline=FG,
                            width=1, fill=FG if picked else BG)
            dot.pack(side="right", padx=(0, 6))
            dot.bind("<Button-1>", lambda _e, l=label: self._set_tray_setting(
                "tray_meter", [self._source, l]))

    def _pack_detail(self, parent: tk.Misc, sub: str, reset_when: str) -> None:
        if not sub and not reset_when:
            return
        tier, detail = self._split_plan(sub)
        if tier:
            tk.Label(parent, text=tier, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 7), bd=0, padx=0, pady=0,
                     wraplength=self._inner_w(), justify="left",
                     ).pack(anchor="w")
        clock = self._fit_clock(detail, reset_when)
        if detail or clock:
            line = tk.Frame(parent, bg=BG)
            line.pack(fill="x")
            if clock:
                tk.Label(line, text=clock, bg=BG, fg=FG_DIM,
                         font=("Segoe UI", 7),
                         bd=0, padx=0, pady=0).pack(side="right")
            if detail:
                room = self._inner_w()
                if clock:
                    room = max(40, room - self._ui_font(7).measure(clock) - 4)
                tk.Label(line, text=detail, bg=BG, fg=FG_DIM,
                         font=("Segoe UI", 7),
                         bd=0, padx=0, pady=0,
                         wraplength=room, justify="left").pack(side="left")

    def _mount_stacked_bar(self, parent: tk.Misc, segments: list[tuple[float, str]]) -> None:
        """One bar. Each segment is a product's share of the weekly limit; the rest stays dark."""
        canvas = tk.Canvas(
            parent, width=1, height=STACK_H, bg=BAR_BG, highlightthickness=0, bd=0,
        )
        canvas.pack(fill="x", pady=(2, 0))

        def redraw(_event: tk.Event | None = None) -> None:
            canvas.delete("all")
            width = canvas.winfo_width()
            if width < 2:
                return
            canvas.create_rectangle(0, 0, width, STACK_H, fill=BAR_BG, width=0)
            x = 0
            for frac, color in segments:
                span = round(width * max(frac, 0.0))
                if span <= 0 or x >= width:
                    continue
                x1 = min(width, x + span)
                canvas.create_rectangle(x, 0, x1, STACK_H, fill=color, width=0)
                x = x1

        canvas.bind("<Configure>", redraw)

    def _add_row(self, label: str, util: float, sub: str,
                 time_progress: float | None, day_notches: int | None = None,
                 reset_when: str = "") -> None:
        pct = min(int(util * 100), 100)
        hidden = label in self._hidden
        active_bar = FG_DIM if hidden else bar_color(util)
        time_fg = FG_DIM if hidden else TIME_BAR_FG

        row = tk.Frame(self._body, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        self._pack_row_title(row, label, f"{pct}%", active_bar)

        usage_bar = tk.Frame(row, bg=BAR_BG, height=BAR_H)
        usage_bar.pack(fill="x", pady=(2, 0))
        usage_bar.pack_propagate(False)
        tk.Frame(usage_bar, bg=active_bar, height=BAR_H).place(
            relx=0, rely=0, relwidth=min(max(util, 0.0), 1.0), relheight=1.0)

        if time_progress is not None:
            self._mount_time_bar(row, time_progress, time_fg, day_notches)
        self._pack_detail(row, sub, reset_when)

    def _mount_time_bar(self, parent: tk.Misc, progress: float, fill: str,
                        days: int | None) -> None:
        """Light bar under the usage bar. Weekly bars get a notch per day."""
        drop = TICK_DROP if days else 0
        height = TIME_BAR_H + drop
        # width=1: Tk's canvas default is ~10cm and was stretching the window.
        canvas = tk.Canvas(
            parent, width=1, height=height, bg=BG, highlightthickness=0, bd=0,
        )
        canvas.pack(fill="x")
        tick = fill

        def redraw(_event: tk.Event | None = None) -> None:
            canvas.delete("all")
            width = canvas.winfo_width()
            if width < 2:
                return
            prog = min(max(progress, 0.0), 1.0)
            canvas.create_rectangle(0, 0, width, TIME_BAR_H, fill=TIME_BAR_BG, width=0)
            canvas.create_rectangle(
                0, 0, max(1, round(width * prog)), TIME_BAR_H, fill=fill, width=0,
            )
            if not days or days < 2:
                return
            # Start, end, and one notch at each day boundary between them.
            last = width - 1
            for i in range(days + 1):
                x = round(i * last / days)
                canvas.create_rectangle(x, 0, x + 1, height, fill=tick, width=0)

        canvas.bind("<Configure>", redraw)

    def _reset_body(self) -> None:
        for w in self._body.winfo_children():
            w.destroy()

    def _login_hint(self, name: str) -> str:
        if name == "grok" and self._login_notice:
            return self._login_notice
        if name == "grok" and not find_grok_cli():
            return GROK_INSTALL_HINT
        return LOGIN_HINTS.get(name, "Sign in to connect this account.")

    def _login_button_label(self, name: str) -> str:
        if name == "grok" and not find_grok_cli():
            return "Install and sign in"
        return "Sign in"

    def _show_login(self) -> None:
        self._reset_body()
        name = self._source
        tk.Label(self._body, text="Not signed in", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w")
        hint = self._login_hint(name)
        tk.Label(self._body, text=hint,
                 bg=BG, fg=FG, font=("Segoe UI", 8), justify="left",
                 wraplength=self._inner_w()).pack(anchor="w", pady=(4, 0))
        btn = self._make_action_button(
            self._body, self._login_button_label(name), lambda n=name: self._sign_in(n),
        )
        btn.pack(fill="x", pady=(8, 0))
        self._ts_lbl.config(text="")
        self._finish_layout()

    def _refresh_account_rows(self) -> None:
        frame = getattr(self, "_accounts_frame", None)
        if frame is None or not frame.winfo_exists():
            return
        for child in frame.winfo_children():
            child.destroy()
        for name in SOURCE_ORDER:
            self._add_account_row(frame, name)
        self._fit_settings()

    def _add_account_row(self, parent: tk.Misc, name: str) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(2, 0))

        signed = self._source_signed_in(name)
        action = "Sign out" if signed else "Sign in"
        cb = (lambda n=name: self._sign_out(n)) if signed else (lambda n=name: self._sign_in(n))
        btn = tk.Label(
            row, text=action, bg=BTN_BG, fg=FG, font=("Segoe UI", 7),
            cursor="hand2", bd=0, padx=6, pady=1,
            width=8, anchor="center",
        )
        btn.pack(side="right")
        btn.bind("<Button-1>", lambda _e, c=cb: c())
        btn.bind("<Enter>", lambda _e, b=btn: b.config(bg=BTN_BG_HOVER))
        btn.bind("<Leave>", lambda _e, b=btn: b.config(bg=BTN_BG))

        state = {"on": self._show_source(name)}
        box = tk.Label(row, text="☑" if state["on"] else "☐",
                       bg=BG, fg=FG, font=("Segoe UI", 9),
                       cursor="hand2", bd=0, padx=0, pady=0)
        box.pack(side="left", padx=(0, 4))
        lbl = tk.Label(row, text=SOURCE_TITLE[name], bg=BG, fg=FG,
                       font=("Segoe UI", 8), cursor="hand2",
                       bd=0, padx=0, pady=0)
        lbl.pack(side="left")

        def toggle(_e=None, n=name, st=state, bx=box):
            new = not st["on"]
            err = self._set_show_source(n, new)
            if err:
                return
            st["on"] = new
            bx.config(text="☑" if new else "☐")

        box.bind("<Button-1>", toggle)
        lbl.bind("<Button-1>", toggle)

    def _show_error(self, msg: str) -> None:
        self._reset_body()
        tk.Label(self._body, text=f"Error: {msg}", bg=BG, fg=BAR_HIGH,
                 font=("Segoe UI", 7), wraplength=self._inner_w(),
                 justify="left").pack(anchor="w")
        self._ts_lbl.config(text=time.strftime("Failed %H:%M:%S"))
        self._finish_layout()


def log_crash(what: str, exc: BaseException | None = None) -> None:
    import traceback
    text = "".join(traceback.format_exception(exc)) if exc else ""
    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {what}\n{text}\n")
    except OSError:
        pass


def install_crash_log() -> None:
    """pythonw has no console, so a crash vanishes silently. Write why to CRASH_LOG."""
    import faulthandler
    try:
        if CRASH_LOG.exists() and CRASH_LOG.stat().st_size > 256_000:
            CRASH_LOG.write_text("", encoding="utf-8")
        # Hard crashes (access violations in Tk or ctypes) skip Python entirely;
        # faulthandler dumps every thread's stack to this file when one hits.
        global _fault_file
        _fault_file = open(CRASH_LOG, "a", encoding="utf-8")
        faulthandler.enable(_fault_file, all_threads=True)
    except OSError:
        pass
    sys.excepthook = lambda t, e, tb: log_crash("Unhandled error, widget closed", e)
    threading.excepthook = lambda a: log_crash(f"Unhandled error in thread {a.thread.name}",
                                               a.exc_value)


_EXIT_WATCH = r"""
$p = Get-Process -Id {pid} -ErrorAction Stop; $null = $p.Handle; $p.WaitForExit()
$c = $p.ExitCode
if ($c -eq 0) {{ exit }}
$hex = '0x{{0:X8}}' -f $c
$why = switch ($hex) {{
  '0x00000001' {{ 'killed by taskkill /F or a script (exit 1)' }}
  '0xFFFFFFFF' {{ 'killed by Stop-Process or Task Manager End task' }}
  '0xC0000005' {{ 'crashed: access violation (Tk, Pillow or a Windows call)' }}
  '0xC00000FD' {{ 'crashed: stack overflow' }}
  '0xC0000409' {{ 'crashed: fast-fail / buffer check inside a native library' }}
  '0xC000013A' {{ 'stopped by Ctrl+C / Ctrl+Break' }}
  '0x40010004' {{ 'ended by Windows sign-out or shutdown' }}
  default {{ 'unknown exit code' }}
}}
$all = Get-CimInstance Win32_Process
$names = @{{}}; $all | ForEach-Object {{ $names[[int]$_.ProcessId] = $_.Name }}
$since = (Get-Date).AddMinutes(-2)
$recent = $all | Where-Object {{ $_.CreationDate -gt $since -and $_.ProcessId -ne $PID }} |
  Sort-Object CreationDate | ForEach-Object {{
    $l = "$($_.CommandLine)"; if ($l.Length -gt 300) {{ $l = $l.Substring(0, 300) + '...' }}
    "  $($_.CreationDate.ToString('HH:mm:ss')) $($_.Name) [$($_.ProcessId)] from $($names[[int]$_.ParentProcessId]): $l" }}
if (-not $recent) {{ $recent = @('  (nothing new started; likely Windows itself, Task Manager, or a long-running app)') }}
$t = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$out = "[$t] Widget (pid {pid}) ended without warning: $why, code $hex`nStarted in the 2 minutes before:`n" + ($recent -join "`n") + "`n`n"
[IO.File]::AppendAllText('{log}', $out)
"""


def start_exit_watch() -> None:
    """A kill can't be logged from inside. A hidden PowerShell outlives us, reads the
    exit code and notes which scripts were running, which usually names the killer.
    PowerShell, not pythonw, so 'kill every pythonw' doesn't take the watcher too."""
    if sys.platform != "win32":
        return
    script = _EXIT_WATCH.format(pid=os.getpid(), log=str(CRASH_LOG).replace("'", "''"))
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()],
            creationflags=CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def main() -> None:
    if not claim_single_instance():
        signal_running_instance()
        return
    install_crash_log()
    # A start with no matching end below it means something killed the process outright.
    log_crash(f"Started (pid {os.getpid()})")
    start_exit_watch()
    app = UsageWidget()
    app.report_callback_exception = lambda t, e, tb: log_crash("Error in a UI callback", e)
    app.protocol("WM_SAVE_YOURSELF", lambda: log_crash("Windows is signing out or shutting down"))
    try:
        app.mainloop()
    except BaseException as e:
        log_crash("Main loop died, widget closed", e)
        raise
    if getattr(app, "_destroyed", False):
        log_crash(f"Quit normally (pid {os.getpid()})")
    else:
        log_crash("Main loop ended without Quit (window destroyed by something else)")


if __name__ == "__main__":
    main()
