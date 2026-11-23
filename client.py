"""Telethon service wrapper and shared formatting/validation helpers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telethon import TelegramClient, utils
from telethon.tl import types

from config import ConfigError, TelegramConfig, load_config

_VALID_PARSE_MODES = {"md", "markdown", "html"}
_INT_PATTERN = re.compile(r"-?\d+")


class TelegramNotAuthorizedError(RuntimeError):
    """Raised when the Telegram session does not exist or is not signed in."""


def clamp_limit(limit: int, maximum: int = 100) -> int:
    """Clamp requested page size into [1, maximum]."""
    return max(1, min(int(limit), maximum))


def parse_chat_ref(chat: str) -> int | str:
    """Parse a chat reference into a marked integer ID, 'me', or username/phone."""
    cleaned = chat.strip()
    if not cleaned:
        raise ValueError("Chat identifier cannot be empty.")
    if cleaned.lower() == "me":
        return "me"
    if _INT_PATTERN.fullmatch(cleaned):
        return int(cleaned)
    return cleaned


def normalize_parse_mode(parse_mode: str | None) -> str | None:
    """Validate and normalize Telethon parse_mode ('md', 'html', or None)."""
    if parse_mode is None:
        return None
    cleaned = parse_mode.strip().lower()
    if cleaned in ("", "none", "plain"):
        return None
    if cleaned in _VALID_PARSE_MODES:
        return cleaned
    raise ValueError(
        f"Unsupported parse_mode '{parse_mode}'. Use 'md', 'html', or omit it."
    )


def safe_peer_id(entity: Any) -> int | None:
    """Return canonical marked peer ID (-100... for channels, -... for chats, + for users)."""
    if entity is None:
        return None
    try:
        return int(utils.get_peer_id(entity))
    except (TypeError, ValueError):
        raw_id = getattr(entity, "id", None)
        return int(raw_id) if isinstance(raw_id, int) else None


def entity_display_name(entity: Any) -> str:
    """Extract a readable display name for a user, group, or channel entity."""
    if entity is None:
        return "Unknown"
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [
        str(part).strip()
        for part in (
            getattr(entity, "first_name", None),
            getattr(entity, "last_name", None),
        )
        if part and str(part).strip()
    ]
    if parts:
        return " ".join(parts)
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    peer_id = safe_peer_id(entity)
    return str(peer_id) if peer_id is not None else "Unknown"


def entity_kind(entity: Any) -> str:
    """Return a normalized chat kind ('user', 'bot', 'group', 'supergroup', 'channel')."""
    if isinstance(entity, types.Channel):
        return "supergroup" if getattr(entity, "megagroup", False) else "channel"
    if isinstance(entity, types.Chat):
        return "group"
    if isinstance(entity, types.User):
        return "bot" if getattr(entity, "bot", False) else "user"
    # Duck-typed fallback for unit tests and lightweight entity objects.
    if getattr(entity, "title", None):
        return "supergroup" if getattr(entity, "megagroup", False) else "group"
    if getattr(entity, "bot", False):
        return "bot"
    return "user"


def format_entity_summary(entity: Any) -> dict[str, Any]:
    """Build a compact metadata dictionary for a chat or user entity."""
    return {
        "chat_id": safe_peer_id(entity),
        "title": entity_display_name(entity),
        "type": entity_kind(entity),
        "username": getattr(entity, "username", None),
    }


def format_message(
    msg: Any,
    *,
    sender_name: str | None = None,
    chat_id: int | None = None,
    chat_title: str | None = None,
) -> dict[str, Any]:
    """Serialize a Telethon Message into a JSON-friendly dictionary."""
    reply_to = getattr(msg, "reply_to", None)
    reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)
    media = getattr(msg, "media", None)
    msg_date = getattr(msg, "date", None)
    text = getattr(msg, "message", None)
    if text is None:
        text = getattr(msg, "text", None)

    payload: dict[str, Any] = {
        "message_id": getattr(msg, "id", None),
        "date": msg_date.isoformat() if msg_date is not None else None,
        "sender_id": getattr(msg, "sender_id", None),
        "sender_name": sender_name,
        "is_outgoing": bool(getattr(msg, "out", False)),
        "reply_to_msg_id": reply_to_msg_id,
        "text": text or "",
        "media_type": type(media).__name__ if media is not None else None,
    }
    if chat_id is not None:
        payload["chat_id"] = chat_id
    if chat_title is not None:
        payload["chat_title"] = chat_title
    return payload


def _dialog_matches_query(dialog: Any, lowered_query: str) -> bool:
    entity = getattr(dialog, "entity", None)
    candidates = (
        getattr(dialog, "name", None),
        entity_display_name(entity),
        getattr(entity, "username", None),
        str(getattr(dialog, "id", "")),
    )
    return any(
        candidate and lowered_query in str(candidate).lower()
        for candidate in candidates
    )


def _format_dialog(dialog: Any) -> dict[str, Any]:
    entity = getattr(dialog, "entity", None)
    last_msg = getattr(dialog, "message", None)
    last_text = getattr(last_msg, "message", None) or ""
    preview = last_text[:200] + ("..." if len(last_text) > 200 else "")
    dialog_date = getattr(dialog, "date", None)

    return {
        "chat_id": getattr(dialog, "id", None) or safe_peer_id(entity),
        "title": getattr(dialog, "name", None) or entity_display_name(entity),
        "type": entity_kind(entity),
        "username": getattr(entity, "username", None),
        "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
        "last_message_date": (
            dialog_date.isoformat() if dialog_date is not None else None
        ),
        "last_message_preview": preview,
    }


async def _resolve_sender_name(msg: Any) -> str | None:
    sender = getattr(msg, "sender", None)
    if sender is None and getattr(msg, "sender_id", None) is not None:
        get_sender = getattr(msg, "get_sender", None)
        if callable(get_sender):
            try:
                sender = await get_sender()
            except Exception:
                sender = None
    return entity_display_name(sender) if sender is not None else None


async def resolve_entity(client: Any, chat: str) -> Any:
    """Resolve a chat ID, 'me', @username, or phone number into a Telethon entity."""
    ref = parse_chat_ref(chat)
    try:
        return await client.get_entity(ref)
    except ValueError as first_err:
        # Numeric peer IDs may fail before Telethon's local entity cache is populated.
        if isinstance(ref, int):
            await client.get_dialogs(limit=200)
            try:
                return await client.get_entity(ref)
            except ValueError as second_err:
                raise ValueError(
                    f"Could not find chat '{chat}'. Verify the chat_id or call list_dialogs first."
                ) from second_err
        raise ValueError(
            f"Could not resolve chat '{chat}'. Verify the @username, phone number, or chat_id."
        ) from first_err


def build_telethon_client(cfg: TelegramConfig) -> TelegramClient:
    """Create a Telethon client with desktop metadata for reliable auth and sessions."""
    return TelegramClient(
        str(cfg.session_path),
        cfg.api_id,
        cfg.api_hash,
        device_model="Desktop",
        system_version="Windows 11",
        app_version="1.0.0",
    )


class TelegramService:
    """Manages an authenticated Telethon client and exposes chat operations."""

    def __init__(
        self,
        *,
        env_path: Path | None = None,
        client_factory: Callable[[TelegramConfig], Any] | None = None,
    ) -> None:
        self._env_path = env_path
        self._client_factory = client_factory
        self._client: Any | None = None
        self._lock = asyncio.Lock()

    def _build_client(self, cfg: TelegramConfig) -> Any:
        if self._client_factory is not None:
            return self._client_factory(cfg)
        return build_telethon_client(cfg)

    async def _ensure_connected(self, cfg: TelegramConfig) -> Any:
        if self._client is None:
            self._client = self._build_client(cfg)
        if not self._client.is_connected():
            await self._client.connect()
        return self._client

    async def get_client(self) -> Any:
        """Return a connected, authorized TelegramClient or raise a descriptive error."""
        async with self._lock:
            cfg = load_config(self._env_path)
            # Avoid creating an empty .session SQLite file before the user runs auth.py.
            if self._client_factory is None and not cfg.session_path.exists():
                raise TelegramNotAuthorizedError(
                    f"Telegram session file not found at '{cfg.session_path}'. "
                    "Run auth.py in a terminal to authorize your account."
                )
            client = await self._ensure_connected(cfg)
            if not await client.is_user_authorized():
                raise TelegramNotAuthorizedError(
                    "Telegram session is not authorized. Run auth.py in a terminal to sign in."
                )
            return client

    async def disconnect(self) -> None:
        """Disconnect the underlying Telethon client if active."""
        async with self._lock:
            if self._client is not None and self._client.is_connected():
                await self._client.disconnect()

    async def status(self) -> dict[str, Any]:
        """Return non-throwing connection and authorization status for diagnostics."""
        try:
            cfg = load_config(self._env_path)
        except ConfigError as exc:
            return {
                "configured": False,
                "session_exists": False,
                "authorized": False,
                "user": None,
                "message": str(exc),
            }

        session_exists = (
            True if self._client_factory is not None else cfg.session_path.exists()
        )
        if not session_exists:
            return {
                "configured": True,
                "session_exists": False,
                "authorized": False,
                "session_path": str(cfg.session_path),
                "user": None,
                "message": "Credentials configured, but session is not authorized yet. Run auth.py in a terminal.",
            }

        async with self._lock:
            client = await self._ensure_connected(cfg)
            authorized = await client.is_user_authorized()
            if not authorized:
                return {
                    "configured": True,
                    "session_exists": True,
                    "authorized": False,
                    "session_path": str(cfg.session_path),
                    "user": None,
                    "message": "Session file exists but is not authorized. Run auth.py in a terminal.",
                }
            me = await client.get_me()
            return {
                "configured": True,
                "session_exists": True,
                "authorized": True,
                "session_path": str(cfg.session_path),
                "user": {
                    "id": getattr(me, "id", None),
                    "name": entity_display_name(me),
                    "username": getattr(me, "username", None),
                },
                "message": "Connected and authorized.",
            }

    async def list_dialogs(
        self,
        *,
        limit: int = 20,
        unread_only: bool = False,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent Telegram chats with optional unread and text filters."""
        client = await self.get_client()
        capped = clamp_limit(limit)
        lowered_query = search.strip().lower() if search and search.strip() else None
        # Fetch extra dialogs when filtering client-side so we still fill the page.
        fetch_limit = None if (unread_only or lowered_query) else capped

        results: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=fetch_limit):
            if unread_only and not (getattr(dialog, "unread_count", 0) or 0):
                continue
            if lowered_query and not _dialog_matches_query(dialog, lowered_query):
                continue
            results.append(_format_dialog(dialog))
            if len(results) >= capped:
                break
        return results

    async def resolve_chat(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Find matching chats by @username, chat ID, 'me', or title substring."""
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("Search query cannot be empty.")

        client = await self.get_client()
        matches: list[dict[str, Any]] = []
        seen_ids: set[int] = set()

        looks_direct = (
            cleaned.lower() == "me"
            or cleaned.startswith(("@", "+"))
            or bool(_INT_PATTERN.fullmatch(cleaned))
        )
        if looks_direct:
            try:
                entity = await resolve_entity(client, cleaned)
                summary = format_entity_summary(entity)
                if summary["chat_id"] is not None:
                    seen_ids.add(summary["chat_id"])
                matches.append(summary)
            except ValueError:
                pass

        for dialog in await self.list_dialogs(limit=limit, search=cleaned):
            chat_id = dialog.get("chat_id")
            if chat_id in seen_ids:
                continue
            if chat_id is not None:
                seen_ids.add(chat_id)
            matches.append(
                {
                    "chat_id": chat_id,
                    "title": dialog.get("title"),
                    "type": dialog.get("type"),
                    "username": dialog.get("username"),
                }
            )
            if len(matches) >= clamp_limit(limit):
                break
        return matches

    async def get_messages(
        self,
        chat: str,
        *,
        limit: int = 20,
        offset_id: int = 0,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Fetch messages from a specific chat with optional search and pagination."""
        client = await self.get_client()
        entity = await resolve_entity(client, chat)
        capped = clamp_limit(limit)
        cleaned_search = search.strip() if search and search.strip() else None

        messages: list[dict[str, Any]] = []
        async for msg in client.iter_messages(
            entity,
            limit=capped,
            offset_id=max(0, int(offset_id)),
            search=cleaned_search,
        ):
            sender_name = await _resolve_sender_name(msg)
            messages.append(format_message(msg, sender_name=sender_name))

        return {
            "chat": format_entity_summary(entity),
            "count": len(messages),
            "messages": messages,
        }

    async def search_global_messages(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search messages across all Telegram chats."""
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("Search query cannot be empty.")

        client = await self.get_client()
        results: list[dict[str, Any]] = []
        async for msg in client.iter_messages(
            None,
            search=cleaned,
            limit=clamp_limit(limit),
        ):
            chat_obj = getattr(msg, "chat", None)
            sender_name = await _resolve_sender_name(msg)
            results.append(
                format_message(
                    msg,
                    sender_name=sender_name,
                    chat_id=getattr(msg, "chat_id", None) or safe_peer_id(chat_obj),
                    chat_title=entity_display_name(chat_obj)
                    if chat_obj is not None
                    else None,
                )
            )
        return results

    async def send_message(
        self,
        chat: str,
        text: str,
        *,
        reply_to_msg_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Send a text message or reply to a specific message in a Telegram chat."""
        if not text or not text.strip():
            raise ValueError("Message text cannot be empty.")
        if reply_to_msg_id is not None and int(reply_to_msg_id) <= 0:
            raise ValueError("reply_to_msg_id must be a positive integer.")

        mode = normalize_parse_mode(parse_mode)
        client = await self.get_client()
        entity = await resolve_entity(client, chat)
        sent = await client.send_message(
            entity,
            text,
            reply_to=int(reply_to_msg_id) if reply_to_msg_id is not None else None,
            parse_mode=mode,
        )
        return {
            "ok": True,
            "chat": format_entity_summary(entity),
            "message": format_message(sent),
        }

    async def edit_message(
        self,
        chat: str,
        message_id: int,
        new_text: str,
        *,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Edit an existing outgoing message in a Telegram chat."""
        if int(message_id) <= 0:
            raise ValueError("message_id must be a positive integer.")
        if not new_text or not new_text.strip():
            raise ValueError("new_text cannot be empty.")

        mode = normalize_parse_mode(parse_mode)
        client = await self.get_client()
        entity = await resolve_entity(client, chat)
        edited = await client.edit_message(
            entity,
            int(message_id),
            new_text,
            parse_mode=mode,
        )
        return {
            "ok": True,
            "chat": format_entity_summary(entity),
            "message": format_message(edited),
        }

    async def mark_as_read(
        self,
        chat: str,
        *,
        max_id: int | None = None,
    ) -> dict[str, Any]:
        """Mark messages in a chat as read up to max_id (or all unread if omitted)."""
        if max_id is not None and int(max_id) <= 0:
            raise ValueError("max_id must be a positive integer.")

        client = await self.get_client()
        entity = await resolve_entity(client, chat)
        await client.send_read_acknowledge(
            entity,
            max_id=int(max_id) if max_id is not None else None,
        )
        return {
            "ok": True,
            "chat": format_entity_summary(entity),
            "max_id": int(max_id) if max_id is not None else None,
        }
