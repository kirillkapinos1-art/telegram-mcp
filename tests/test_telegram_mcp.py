"""Tests for Telegram MCP config, service helpers, tool behavior, and stdio protocol."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from auth import render_qr_login_html
from auto_reply import (
    AutoReplyBot,
    GeminiResponder,
    build_transcript,
    clean_generated_reply,
    load_style_profile,
)
from client import (
    TelegramNotAuthorizedError,
    TelegramService,
    build_telethon_client,
    normalize_parse_mode,
    parse_chat_ref,
)
from config import (
    ConfigError,
    TelegramConfig,
    load_autoreply_config,
    load_config,
    save_env_credentials,
    save_env_values,
)
from google.genai import errors as genai_errors


class FakeTelethonClient:
    """In-memory async stub mimicking Telethon TelegramClient methods."""

    def __init__(self, *, authorized: bool = True, require_dialog_warmup: bool = False) -> None:
        self.connected = False
        self.authorized = authorized
        self.require_dialog_warmup = require_dialog_warmup
        self.warmed_up = not require_dialog_warmup
        self.sent_calls: list[dict[str, Any]] = []
        self.edited_calls: list[dict[str, Any]] = []
        self.read_calls: list[dict[str, Any]] = []

        self.alice = SimpleNamespace(
            id=101, first_name="Alice", last_name="Smith", username="alice", bot=False
        )
        self.team_chat = SimpleNamespace(
            id=202, title="Core Team", username=None, megagroup=True
        )
        self.dialogs = [
            SimpleNamespace(
                id=101,
                name="Alice Smith",
                entity=self.alice,
                unread_count=2,
                date=datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc),
                message=SimpleNamespace(message="Hey, are you free?"),
            ),
            SimpleNamespace(
                id=-100202,
                name="Core Team",
                entity=self.team_chat,
                unread_count=0,
                date=datetime(2026, 11, 15, 18, 30, tzinfo=timezone.utc),
                message=SimpleNamespace(message="Release checklist updated"),
            ),
        ]
        self.messages = [
            SimpleNamespace(
                id=11,
                date=datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc),
                sender_id=101,
                sender=self.alice,
                out=False,
                reply_to=None,
                message="Hey, are you free?",
                media=None,
                chat=self.alice,
                chat_id=101,
            ),
            SimpleNamespace(
                id=10,
                date=datetime(2026, 11, 16, 9, 55, tzinfo=timezone.utc),
                sender_id=1,
                sender=None,
                out=True,
                reply_to=SimpleNamespace(reply_to_msg_id=9),
                message="Morning!",
                media=None,
                chat=self.alice,
                chat_id=101,
                get_sender=self._get_self_sender,
            ),
        ]

    async def _get_self_sender(self) -> Any:
        return SimpleNamespace(id=1, first_name="Kirill", last_name=None, username="kirill")

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> Any:
        return SimpleNamespace(id=1, first_name="Kirill", last_name=None, username="kirill")

    async def get_dialogs(self, limit: int | None = None) -> list[Any]:
        self.warmed_up = True
        return self.dialogs[:limit] if limit else self.dialogs

    async def iter_dialogs(self, limit: int | None = None):
        self.warmed_up = True
        items = self.dialogs[:limit] if limit else self.dialogs
        for item in items:
            yield item

    async def get_entity(self, ref: int | str) -> Any:
        if ref == "me":
            return await self.get_me()
        if ref in ("@alice", "alice", 101):
            if isinstance(ref, int) and not self.warmed_up:
                raise ValueError("Entity not in cache")
            return self.alice
        if ref == -100202:
            if not self.warmed_up:
                raise ValueError("Entity not in cache")
            return self.team_chat
        raise ValueError(f"Unknown peer: {ref}")

    async def iter_messages(
        self,
        entity: Any,
        *,
        limit: int = 20,
        offset_id: int = 0,
        search: str | None = None,
    ):
        count = 0
        for msg in self.messages:
            if offset_id and msg.id >= offset_id:
                continue
            if search and search.lower() not in msg.message.lower():
                continue
            yield msg
            count += 1
            if count >= limit:
                break

    async def send_message(
        self,
        entity: Any,
        text: str,
        *,
        reply_to: int | None = None,
        parse_mode: str | None = None,
    ) -> Any:
        self.sent_calls.append(
            {
                "entity": entity,
                "text": text,
                "reply_to": reply_to,
                "parse_mode": parse_mode,
            }
        )
        return SimpleNamespace(
            id=99,
            date=datetime(2026, 11, 16, 10, 5, tzinfo=timezone.utc),
            sender_id=1,
            out=True,
            reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None,
            message=text,
            media=None,
        )

    async def edit_message(
        self,
        entity: Any,
        message_id: int,
        new_text: str,
        *,
        parse_mode: str | None = None,
    ) -> Any:
        self.edited_calls.append(
            {
                "entity": entity,
                "message_id": message_id,
                "new_text": new_text,
                "parse_mode": parse_mode,
            }
        )
        return SimpleNamespace(
            id=message_id,
            date=datetime(2026, 11, 16, 10, 6, tzinfo=timezone.utc),
            sender_id=1,
            out=True,
            reply_to=None,
            message=new_text,
            media=None,
        )

    async def send_read_acknowledge(self, entity: Any, *, max_id: int | None = None) -> bool:
        self.read_calls.append({"entity": entity, "max_id": max_id})
        return True


def test_load_and_save_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_PATH", raising=False)

    env_file = tmp_path / ".env"
    with pytest.raises(ConfigError):
        load_config(env_file)

    env_file.write_text("TELEGRAM_SESSION_PATH=custom_user\n", encoding="utf-8")
    save_env_credentials(12345, "abc123hash", env_path=env_file)

    cfg = load_config(env_file)
    assert cfg.api_id == 12345
    assert cfg.api_hash == "abc123hash"
    assert cfg.session_path.name == "custom_user.session"

    # Process env overrides .env when both are present.
    monkeypatch.setenv("TELEGRAM_API_ID", "99999")
    cfg_override = load_config(env_file)
    assert cfg_override.api_id == 99999


def test_parse_helpers() -> None:
    assert parse_chat_ref(" me ") == "me"
    assert parse_chat_ref("-100123456") == -100123456
    assert parse_chat_ref("@alice") == "@alice"
    with pytest.raises(ValueError):
        parse_chat_ref("   ")

    assert normalize_parse_mode(None) is None
    assert normalize_parse_mode("plain") is None
    assert normalize_parse_mode("MD") == "md"
    assert normalize_parse_mode("html") == "html"
    with pytest.raises(ValueError):
        normalize_parse_mode("rst")


def test_service_status_and_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)

    unconfigured = TelegramService(env_path=env_file)
    status_missing = asyncio.run(unconfigured.status())
    assert status_missing["configured"] is False
    assert status_missing["authorized"] is False

    save_env_credentials(12345, "hash123", env_path=env_file)
    no_session = TelegramService(env_path=env_file)
    status_no_session = asyncio.run(no_session.status())
    assert status_no_session["configured"] is True
    assert status_no_session["session_exists"] is False
    assert status_no_session["authorized"] is False

    fake = FakeTelethonClient(authorized=True, require_dialog_warmup=True)
    svc = TelegramService(env_path=env_file, client_factory=lambda _: fake)

    async def exercise() -> None:
        st = await svc.status()
        assert st["authorized"] is True
        assert st["user"]["username"] == "kirill"

        unread = await svc.list_dialogs(unread_only=True)
        assert len(unread) == 1
        assert unread[0]["chat_id"] == 101

        resolved = await svc.resolve_chat("Core")
        assert len(resolved) == 1
        assert resolved[0]["chat_id"] == -100202

        # Numeric lookup triggers dialog warmup automatically when uncached.
        msgs = await svc.get_messages("101", limit=10)
        assert msgs["count"] == 2
        assert msgs["messages"][1]["reply_to_msg_id"] == 9
        assert msgs["messages"][1]["sender_name"] == "Kirill"

        found = await svc.search_global_messages("free")
        assert len(found) == 1
        assert found[0]["chat_id"] == 101

        sent = await svc.send_message(
            "@alice", "On my way!", reply_to_msg_id=11, parse_mode="md"
        )
        assert sent["ok"] is True
        assert sent["message"]["message_id"] == 99
        assert fake.sent_calls[0]["reply_to"] == 11
        assert fake.sent_calls[0]["parse_mode"] == "md"

        edited = await svc.edit_message("@alice", 99, "Updated text")
        assert edited["message"]["text"] == "Updated text"

        read = await svc.mark_as_read("@alice", max_id=11)
        assert read["ok"] is True
        assert fake.read_calls[0]["max_id"] == 11

        await svc.disconnect()
        assert fake.connected is False

    asyncio.run(exercise())


def test_unauthorized_raises_clean_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    env_file = tmp_path / ".env"
    save_env_credentials(12345, "hash123", env_path=env_file)

    fake = FakeTelethonClient(authorized=False)
    svc = TelegramService(env_path=env_file, client_factory=lambda _: fake)
    with pytest.raises(TelegramNotAuthorizedError):
        asyncio.run(svc.list_dialogs())


def test_mcp_stdio_roundtrip() -> None:
    async def run_roundtrip() -> None:
        child_env = os.environ.copy()
        child_env["TELEGRAM_API_ID"] = ""
        child_env["TELEGRAM_API_HASH"] = ""
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "server.py")],
            env=child_env,
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                tool_names = {tool.name for tool in tools_resp.tools}
                assert {
                    "telegram_status",
                    "list_dialogs",
                    "resolve_chat",
                    "get_messages",
                    "search_global_messages",
                    "send_message",
                    "edit_message",
                    "mark_as_read",
                }.issubset(tool_names)

                status_resp = await session.call_tool("telegram_status", {})
                assert not status_resp.is_error
                payload = status_resp.structured_content or json.loads(
                    status_resp.content[0].text
                )
                assert payload["authorized"] is False

    asyncio.run(run_roundtrip())


def test_render_qr_login_html_and_client_factory(tmp_path: Path) -> None:
    html_file = tmp_path / "qr_login.html"
    ascii_qr, written_path = render_qr_login_html(
        "tg://login?token=example_token", output_path=html_file
    )
    assert written_path == html_file
    assert len(ascii_qr.strip()) > 0
    content = html_file.read_text(encoding="utf-8")
    assert "<svg" in content
    assert "http-equiv='refresh'" in content

    cfg = TelegramConfig(
        api_id=12345,
        api_hash="hash123",
        session_path=tmp_path / "test.session",
    )
    tg_client = build_telethon_client(cfg)
    try:
        assert tg_client.api_id == 12345
        assert tg_client.api_hash == "hash123"
    finally:
        tg_client.session.close()


def test_autoreply_config_and_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("TELEGRAM_AUTOREPLY_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_AUTOREPLY_DEBOUNCE_SECONDS", raising=False)

    env_file = tmp_path / ".env"
    save_env_credentials(12345, "hash123", env_path=env_file)
    with pytest.raises(ConfigError):
        load_autoreply_config(env_file)

    save_env_values({"GEMINI_API_KEY": "fake_key"}, env_path=env_file)
    cfg = load_autoreply_config(
        env_file,
        chats_override=["101", "@alice"],
        debounce_override=1.5,
        model_override="gemini-3.8-flash",
    )
    assert cfg.gemini_api_key == "fake_key"
    assert cfg.allowed_chats == ("101", "@alice")
    assert cfg.debounce_seconds == 1.5
    assert cfg.models[0] == "gemini-3.8-flash"
    assert "gemini-3.5-flash-lite" in cfg.models
    assert cfg.context_limit == 100

    style_file = tmp_path / "style.txt"
    assert load_style_profile(style_file) == ""
    style_file.write_text("- пиши коротко\n", encoding="utf-8")
    assert load_style_profile(style_file) == "- пиши коротко"

    gemma_responder = GeminiResponder(
        "fake_key", ("gemma-4-26b-a4b-it",), style_profile="коротко", genai_client=SimpleNamespace()
    )
    assert "коротко" in gemma_responder._build_instruction("Соня")

    msgs = [
        SimpleNamespace(id=2, out=False, message="где ты?", media=None),
        SimpleNamespace(id=1, out=True, message="тут", media=None),
    ]
    assert build_transcript(msgs, "Соня") == "[Я]: тут\n[Соня]: где ты?"
    assert clean_generated_reply('[Я]: "ща буду"') == "ща буду"
    assert clean_generated_reply("SKIP") is None
    assert clean_generated_reply("   ") is None


def test_gemini_responder_fallback_and_autoreply_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    save_env_values(
        {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "hash123",
            "GEMINI_API_KEY": "fake_key",
        },
        env_path=env_file,
    )
    cfg = load_autoreply_config(
        env_file,
        chats_override=["101"],
        debounce_override=0.05,
    )

    class StubModels:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
            self.calls.append(model)
            if model == "gemini-3.5-flash":
                raise genai_errors.ServerError(
                    503, {"error": {"message": "busy"}}, None
                )
            return SimpleNamespace(text="[Я]: щя выхожу")

    stub_client = SimpleNamespace(models=StubModels())
    responder = GeminiResponder(
        "fake_key",
        ("gemini-3.5-flash", "gemini-3.8-flash"),
        genai_client=stub_client,
    )
    fake_tg = FakeTelethonClient(authorized=True)
    bot = AutoReplyBot(
        cfg,
        responder=responder,
        client_factory=lambda _: fake_tg,
    )

    async def exercise() -> None:
        bot._client = fake_tg
        await bot._client.connect()
        targets = await bot.setup_targets()
        assert 101 in targets

        # Bursts collapse into a single debounced reply.
        bot.schedule_chat(101)
        bot.schedule_chat(101)
        await asyncio.sleep(0.15)
        assert len(fake_tg.sent_calls) == 1
        assert fake_tg.sent_calls[0]["text"] == "щя выхожу"
        assert fake_tg.read_calls[0]["max_id"] == 11
        assert stub_client.models.calls == ["gemini-3.5-flash", "gemini-3.8-flash"]

        # Same latest message ID is not answered twice.
        assert await bot.process_chat(101) is None
        assert len(fake_tg.sent_calls) == 1

        # Outgoing latest message is skipped even if ID is newer.
        fake_tg.messages.insert(
            0, SimpleNamespace(id=12, out=True, message="я уже тут", media=None)
        )
        assert await bot.process_chat(101) is None
        assert len(fake_tg.sent_calls) == 1

    asyncio.run(exercise())
