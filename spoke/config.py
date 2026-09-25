"""Config (~/.spoke/config.toml) and API-key storage (OS keychain via keyring).

The config file is created with comments on first run. Unknown keys are ignored with a
warning; missing keys fall back to DEFAULTS, so old config files keep working after upgrades.
The API key is NEVER read from or written to the config file.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEYRING_SERVICE = "spoke"
KEYRING_USER = "groq_api_key"
ENV_KEY = "GROQ_API_KEY"


def spoke_cmd(args: str = "") -> str:
    """The exact command to run Spoke with THIS interpreter (the venv's python), so hints
    never point at a system `python` that doesn't have Spoke's dependencies."""
    exe = sys.executable or "python"
    if " " in exe:
        exe = f'"{exe}"'
    return f"{exe} -m spoke" + (f" {args}" if args else "")


def spoke_home() -> Path:
    """~/.spoke, overridable with SPOKE_HOME (used by tests and for portable installs)."""
    return Path(os.environ.get("SPOKE_HOME") or Path.home() / ".spoke")


def config_path() -> Path:
    return spoke_home() / "config.toml"


SEED_VOCAB = [
    "Lumeo", "TigerBeetle", "Soroban", "Stellar", "NestJS", "Prisma", "Next.js", "FastAPI",
    "Turborepo", "pnpm", "BullMQ", "Supabase", "Setu", "FIRA", "ITR-4", "ERI",
    "Krishnendu", "Kolkata",
]

# Spoken / mis-heard form -> canonical form. Matched case-insensitively on word boundaries,
# applied AFTER cleanup. Keys are lowercase.
SEED_REPLACEMENTS = {
    "lumeo": "Lumeo",
    "lumio": "Lumeo",
    "tiger beetle": "TigerBeetle",
    "tigerbeetle": "TigerBeetle",
    "soroban": "Soroban",
    "nest js": "NestJS",
    "nestjs": "NestJS",
    "prisma": "Prisma",
    "next js": "Next.js",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    "fast api": "FastAPI",
    "fastapi": "FastAPI",
    "turbo repo": "Turborepo",
    "turborepo": "Turborepo",
    "p npm": "pnpm",
    "bull mq": "BullMQ",
    "bullmq": "BullMQ",
    "supabase": "Supabase",
    "super base": "Supabase",
    "supa base": "Supabase",
    "sopabase": "Supabase",  # heard on a real Mac test, 2026-09-25
    "tiger beatle": "TigerBeetle",
    "setu": "Setu",
    "fira": "FIRA",
    "itr 4": "ITR-4",
    "itr-4": "ITR-4",
    "itr four": "ITR-4",
    "eri": "ERI",
    "krishnendu": "Krishnendu",
    "kolkata": "Kolkata",
}

# Whisper output that shows up on silent / near-silent audio. Deliberately excludes phrases
# you might really dictate on their own ("Thank you.", "Okay.") -- add them if they bite you. Matched against the WHOLE
# transcript after lowercasing and stripping punctuation. Prefix an entry with "re:" to make
# it a regex searched anywhere in the transcript instead.
SEED_BLOCKLIST = [
    "thank you for watching",
    "thanks for watching",
    "thank you so much for watching",
    "you",
    "please subscribe",
    "like and subscribe",
    "re:^\\W*(subtitles?|captions?)( are)? (by|created by|provided by)",
    "re:amara\\.org",
    "re:^\\W*(transcribed|translated|transcription) by\\b",
    "re:\\bmbc\\b.*news",
]

DEFAULT_TERMINAL_CLASSES = [
    # xterm / urxvt / st have no Ctrl+Shift+V clipboard paste by default: use paste_method = "type".
    "gnome-terminal", "gnome-terminal-server", "konsole", "alacritty",
    "kitty", "terminator", "tilix", "xfce4-terminal", "wezterm",
    "org.wezfurlong.wezterm", "foot", "ghostty", "com.mitchellh.ghostty", "terminology",
    "lxterminal", "mate-terminal", "qterminal", "sakura", "guake", "yakuake",
]


@dataclass
class Config:
    hotkey: str = "auto"
    mode: str = "hold"
    toggle_double_tap_ms: int = 400
    language: str = "en"
    stt_backend: str = "groq"
    stt_model: str = "whisper-large-v3-turbo"
    stt_timeout_seconds: float = 10.0
    local_only: bool = False
    local_model_size: str = "base"
    cleanup: bool = True
    # "auto" = first of cleanup.PREFERRED_CLEANUP_MODELS that the key can use. A named model
    # that the key can't use also falls back to that list instead of failing every utterance.
    cleanup_model: str = "auto"
    cleanup_timeout_seconds: float = 1.0
    cleanup_min_words: int = 4
    sounds: bool = True
    notifications: bool = True
    tray: bool = True
    max_seconds: float = 300.0
    min_seconds: float = 0.3
    silence_rms_threshold: float = 150.0
    keep_mic_open: bool = False
    input_device: str = ""
    paste_method: str = "clipboard"
    paste_restore_delay_ms: int = 150
    trailing_space: bool = True
    trailing_space_window_seconds: float = 30.0
    linux_terminal_classes: list[str] = field(default_factory=lambda: list(DEFAULT_TERMINAL_CLASSES))
    history: bool = True
    history_max_mb: float = 5.0
    log_level: str = "INFO"
    debug_save_audio: bool = False
    hallucination_blocklist: list[str] = field(default_factory=lambda: list(SEED_BLOCKLIST))
    vocab: list[str] = field(default_factory=lambda: list(SEED_VOCAB))
    replacements: dict[str, str] = field(default_factory=lambda: dict(SEED_REPLACEMENTS))

    @property
    def effective_stt_backend(self) -> str:
        return "local" if self.local_only else self.stt_backend

    @property
    def effective_cleanup(self) -> bool:
        # local_only never sends text to the cloud LLM.
        return self.cleanup and not self.local_only

    def validate(self) -> list[str]:
        problems = []
        if self.mode not in ("hold", "toggle"):
            problems.append(f"mode must be 'hold' or 'toggle', got {self.mode!r}")
        if self.stt_backend not in ("groq", "local"):
            problems.append(f"stt_backend must be 'groq' or 'local', got {self.stt_backend!r}")
        if self.paste_method not in ("clipboard", "type"):
            problems.append(f"paste_method must be 'clipboard' or 'type', got {self.paste_method!r}")
        if self.max_seconds <= 0:
            problems.append("max_seconds must be > 0")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            problems.append(f"log_level must be DEBUG/INFO/WARNING/ERROR, got {self.log_level!r}")
        return problems


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_list(items: list[str], indent: str = "  ") -> str:
    return "[\n" + "".join(f"{indent}{_toml_str(i)},\n" for i in items) + "]"


def default_config_text() -> str:
    d = Config()
    repl = "\n".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in d.replacements.items())
    return f'''# Spoke config. Edit and restart Spoke (or re-run `python -m spoke doctor`) to apply.
# Your Groq API key is NOT stored here -- it lives in the OS keychain (`python -m spoke setup`)
# or the GROQ_API_KEY environment variable.

# --- Hotkey -------------------------------------------------------------------------------
# "auto" = Right Option on macOS, Right Ctrl on Windows/Linux.
# Other values: right_option, right_ctrl, right_cmd, right_shift, left_ctrl, caps_lock,
# f13..f20, or any pynput Key name (e.g. "alt_r", "ctrl_r", "f19").
hotkey = "auto"
# "hold"   = hold to talk, release to transcribe (primary).
# "toggle" = double-tap to start, single tap to stop (long dictation).
mode = "hold"
toggle_double_tap_ms = 400

# --- Speech to text -----------------------------------------------------------------------
# ISO-639-1 code ("en", "hi", "bn") or "auto" to let Whisper detect it (use for mixed speech).
language = "en"
# "groq" (cloud, fast) or "local" (faster-whisper on CPU; pip install -r requirements-local.txt)
stt_backend = "groq"
stt_model = "whisper-large-v3-turbo"
stt_timeout_seconds = 10.0
# true = audio never leaves this machine: forces stt_backend = "local" and disables cleanup.
local_only = false
# faster-whisper model: tiny, base, small, medium, large-v3. "base" is the fast CPU default.
local_model_size = "base"

# --- Cleanup LLM pass ---------------------------------------------------------------------
cleanup = true
# "auto" picks the fastest model your key can use, in this order: llama-3.1-8b-instant,
# openai/gpt-oss-20b, qwen/qwen3.8-27b, qwen/qwen3-32b, llama-3.3-70b-versatile,
# openai/gpt-oss-120b. Or name one (`doctor` lists what your key can use); if it isn't
# available Spoke falls back to the same list. Reasoning models (gpt-oss, qwen3) are sent
# with thinking set to low/off and hidden -- only the cleaned text is ever pasted.
cleanup_model = "{d.cleanup_model}"
# Cleanup slower than this falls back to the raw transcript.
cleanup_timeout_seconds = 1.0
# Utterances shorter than this many words skip cleanup (saves ~200 ms).
cleanup_min_words = 4

# --- Recording ----------------------------------------------------------------------------
sounds = true
# Desktop notifications for errors / auto-stop.
notifications = true
tray = true
# Auto-stop (with a notification) after this many seconds.
max_seconds = {d.max_seconds}
# Recordings shorter than this are discarded without calling the API.
min_seconds = {d.min_seconds}
# int16 RMS below which a recording counts as silence and is discarded.
# Run `python -m spoke test-mic` in a quiet room and while speaking to calibrate.
silence_rms_threshold = {d.silence_rms_threshold}
# true = keep the mic stream open between dictations (instant start, but the OS mic
# indicator stays on). false = open on key-down (Spoke logs how long that takes).
keep_mic_open = false
# Input device name or index as a string; "" = system default.
input_device = ""

# --- Pasting ------------------------------------------------------------------------------
# "clipboard" = save clipboard, paste, restore (fast, default).
# "type"      = simulate typing (slower, never touches the clipboard).
paste_method = "clipboard"
paste_restore_delay_ms = 150
# Insert a space before the new text if the last paste went to the same app less than
# trailing_space_window_seconds ago, so consecutive dictations don't run together.
trailing_space = true
trailing_space_window_seconds = 30.0
# Linux only: window classes that get Ctrl+Shift+V instead of Ctrl+V.
linux_terminal_classes = {_toml_list(d.linux_terminal_classes)}

# --- History & logs -----------------------------------------------------------------------
# Text-only JSONL at ~/.spoke/history.jsonl (never audio). Rotates at history_max_mb.
history = true
history_max_mb = {d.history_max_mb}
# DEBUG logs per-stage latency (record -> STT -> cleanup -> paste) to ~/.spoke/spoke.log.
log_level = "INFO"
# true = also write each recording as WAV into ~/.spoke/debug_audio/ (off by default).
debug_save_audio = false

# Whole-transcript matches (case/punctuation-insensitive) are dropped as Whisper
# hallucinations. "re:" entries are regexes searched anywhere in the transcript.
hallucination_blocklist = {_toml_list(d.hallucination_blocklist)}

# Custom vocabulary, most important first. Sent to Whisper as its prompt
# (auto-truncated to fit Whisper's 224-token prompt limit).
vocab = {_toml_list(d.vocab)}

# Applied after cleanup, case-insensitive, whole words: "what you say" = "what you want".
[replacements]
{repl}
'''


def _coerce(name: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be true/false")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        return float(value)
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise TypeError(f"{name} must be a list of strings")
        return list(value)
    if isinstance(default, dict):
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise TypeError(f"{name} must be a table of strings")
        return {str(k).lower(): v for k, v in value.items()}
    return value


def from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    known = {f.name for f in fields(Config)}
    for key, value in data.items():
        if key not in known:
            log.warning("Ignoring unknown config key %r", key)
            continue
        setattr(cfg, key, _coerce(key, value, getattr(cfg, key)))
    return cfg


def load(path: Path | None = None, create: bool = True) -> Config:
    path = path or config_path()
    if not path.exists():
        if not create:
            return Config()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # history/logs live here too
        path.write_text(default_config_text(), encoding="utf-8")
        log.info("Created default config at %s", path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    cfg = from_dict(data)
    problems = cfg.validate()
    if problems:
        raise ValueError(f"Invalid config {path}: " + "; ".join(problems))
    return cfg


# --- API key ------------------------------------------------------------------------------

def get_api_key() -> tuple[str | None, str]:
    """Return (key, source). Keychain first, then GROQ_API_KEY."""
    try:
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if key:
            return key, "keychain"
    except Exception as e:  # no backend (headless Linux), locked keychain, etc.
        log.debug("keyring unavailable: %s", type(e).__name__)
    key = os.environ.get(ENV_KEY)
    if key:
        return key.strip(), "env"
    return None, "missing"


def set_api_key(key: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())


def delete_api_key() -> None:
    import keyring

    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.PasswordDeleteError:
        pass
