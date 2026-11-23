"""Configuration loading and local .env persistence for the Telegram MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = PROJECT_DIR / ".env"
DEFAULT_SESSION_PATH = PROJECT_DIR / "telegram.session"
DEFAULT_AUTOREPLY_SESSION_PATH = PROJECT_DIR / "telegram_autoreply.session"
DEFAULT_AUTOREPLY_CHATS: tuple[str, ...] = ()
DEFAULT_GEMINI_MODELS = (
    "gemini-3.5-flash-lite",
    "gemma-4-26b-a4b-it",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemma-4-31b-it",
)
DEFAULT_STYLE_PROFILE_PATH = PROJECT_DIR / "style_profile.txt"


class ConfigError(RuntimeError):
    """Raised when required Telegram API credentials are missing or invalid."""


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Validated configuration required to initialize a Telethon user client."""

    api_id: int
    api_hash: str
    session_path: Path


@dataclass(frozen=True, slots=True)
class AutoReplyConfig:
    """Configuration for the real-time Gemini Telegram auto-responder."""

    telegram: TelegramConfig
    gemini_api_key: str
    models: tuple[str, ...]
    allowed_chats: tuple[str, ...]
    debounce_seconds: float
    context_limit: int


def resolve_session_path(
    raw_path: str | Path | None,
    *,
    default: Path = DEFAULT_SESSION_PATH,
) -> Path:
    """Resolve a session file path relative to the project root with .session suffix."""
    if raw_path is None or not str(raw_path).strip():
        return default
    candidate = Path(str(raw_path).strip())
    # Resolve relative paths against the project directory because MCP hosts often
    # spawn stdio servers with an arbitrary working directory (e.g. System32).
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    if candidate.suffix != ".session":
        candidate = candidate.with_name(f"{candidate.name}.session")
    return candidate


def load_config(
    env_path: Path | None = None,
    *,
    session_override: str | Path | None = None,
) -> TelegramConfig:
    """Load Telegram API configuration from process env and optional .env file."""
    target_env = env_path or DEFAULT_ENV_PATH
    if target_env.exists():
        # Keep explicit process environment variables (e.g. MCP config `env`) higher priority.
        load_dotenv(dotenv_path=target_env, override=False)

    raw_api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    raw_api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    raw_session = (
        session_override
        if session_override is not None
        else os.environ.get("TELEGRAM_SESSION_PATH")
    )

    if not raw_api_id:
        raise ConfigError(
            "TELEGRAM_API_ID is not set. Add it to .env or MCP server env, "
            "or run auth.py interactively."
        )
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ConfigError("TELEGRAM_API_ID must be a positive integer.") from exc
    if api_id <= 0:
        raise ConfigError("TELEGRAM_API_ID must be a positive integer.")

    if not raw_api_hash:
        raise ConfigError(
            "TELEGRAM_API_HASH is not set. Add it to .env or MCP server env, "
            "or run auth.py interactively."
        )

    return TelegramConfig(
        api_id=api_id,
        api_hash=raw_api_hash,
        session_path=resolve_session_path(raw_session),
    )


def save_env_values(
    updates: dict[str, str],
    env_path: Path | None = None,
) -> Path:
    """Persist key=value pairs into .env without clobbering existing keys."""
    cleaned_updates: dict[str, str] = {}
    for key, value in updates.items():
        clean_key = key.strip()
        clean_val = value.strip()
        if not clean_key or "=" in clean_key or "\n" in clean_key or "\r" in clean_key:
            raise ValueError(f"Invalid env key: {key!r}")
        if not clean_val or "\n" in clean_val or "\r" in clean_val:
            raise ValueError(f"Env value for {clean_key} must be a single non-empty line.")
        cleaned_updates[clean_key] = clean_val

    target_env = env_path or DEFAULT_ENV_PATH
    existing = dotenv_values(target_env) if target_env.exists() else {}
    merged: dict[str, str] = {
        k: v for k, v in existing.items() if v is not None
    }
    merged.update(cleaned_updates)

    lines = [f"{key}={value}" for key, value in merged.items()]
    target_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target_env


def save_env_credentials(
    api_id: int,
    api_hash: str,
    env_path: Path | None = None,
) -> Path:
    """Persist TELEGRAM_API_ID and TELEGRAM_API_HASH into .env without clobbering other keys."""
    if api_id <= 0:
        raise ValueError("api_id must be a positive integer.")
    return save_env_values(
        {
            "TELEGRAM_API_ID": str(api_id),
            "TELEGRAM_API_HASH": api_hash,
        },
        env_path=env_path,
    )


def load_autoreply_config(
    env_path: Path | None = None,
    *,
    chats_override: list[str] | None = None,
    debounce_override: float | None = None,
    model_override: str | None = None,
) -> AutoReplyConfig:
    """Load configuration for the real-time Telegram auto-responder daemon."""
    target_env = env_path or DEFAULT_ENV_PATH
    if target_env.exists():
        load_dotenv(dotenv_path=target_env, override=False)

    session_raw = (
        os.environ.get("TELEGRAM_AUTOREPLY_SESSION_PATH")
        or str(DEFAULT_AUTOREPLY_SESSION_PATH)
    )
    tg_cfg = load_config(env_path=target_env, session_override=session_raw)

    gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_api_key:
        raise ConfigError(
            "GEMINI_API_KEY is not set. Add it to .env or environment variables."
        )

    preferred_model = (model_override or os.environ.get("GEMINI_MODEL", "")).strip()
    models = (
        tuple(dict.fromkeys([preferred_model, *DEFAULT_GEMINI_MODELS]))
        if preferred_model
        else DEFAULT_GEMINI_MODELS
    )

    if chats_override:
        chats = tuple(c.strip() for c in chats_override if c.strip())
    else:
        raw_chats = os.environ.get("TELEGRAM_AUTOREPLY_CHATS", "").strip()
        chats = (
            tuple(c.strip() for c in raw_chats.split(",") if c.strip())
            if raw_chats
            else DEFAULT_AUTOREPLY_CHATS
        )
    if not chats:
        raise ConfigError(
            "No allowed chats configured. Set TELEGRAM_AUTOREPLY_CHATS or pass --chats."
        )

    raw_debounce = (
        str(debounce_override)
        if debounce_override is not None
        else os.environ.get("TELEGRAM_AUTOREPLY_DEBOUNCE_SECONDS", "4.0")
    )
    try:
        debounce_seconds = max(0.0, float(raw_debounce))
    except ValueError as exc:
        raise ConfigError("TELEGRAM_AUTOREPLY_DEBOUNCE_SECONDS must be a number.") from exc

    raw_limit = os.environ.get("TELEGRAM_AUTOREPLY_CONTEXT_LIMIT", "100")
    try:
        context_limit = max(5, min(int(raw_limit), 100))
    except ValueError as exc:
        raise ConfigError("TELEGRAM_AUTOREPLY_CONTEXT_LIMIT must be an integer.") from exc

    return AutoReplyConfig(
        telegram=tg_cfg,
        gemini_api_key=gemini_api_key,
        models=models,
        allowed_chats=chats,
        debounce_seconds=debounce_seconds,
        context_limit=context_limit,
    )
