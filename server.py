"""Local stdio MCP server for interacting with a personal Telegram account via Telethon."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from client import TelegramService

logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

service = TelegramService()


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[None]:
    """Ensure the Telethon client disconnects cleanly on server shutdown."""
    try:
        yield
    finally:
        await service.disconnect()


mcp = MCPServer(
    name="telegram",
    instructions=(
        "Tools for reading chats and sending/replying to messages on the user's "
        "personal Telegram account via Telethon."
    ),
    log_level="WARNING",
    lifespan=lifespan,
)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Telegram Connection Status",
        read_only_hint=True,
        open_world_hint=True,
    )
)
async def telegram_status() -> dict[str, Any]:
    """Check whether Telegram API credentials and user session are configured and authorized."""
    return await service.status()


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Telegram Dialogs",
        read_only_hint=True,
        open_world_hint=True,
    )
)
async def list_dialogs(
    limit: int = 20,
    unread_only: bool = False,
    search: str | None = None,
) -> dict[str, Any]:
    """List recent personal chats, groups, and channels with chat_id and unread counts."""
    dialogs = await service.list_dialogs(
        limit=limit,
        unread_only=unread_only,
        search=search,
    )
    return {"count": len(dialogs), "dialogs": dialogs}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Resolve Telegram Chat",
        read_only_hint=True,
        open_world_hint=True,
    )
)
async def resolve_chat(query: str, limit: int = 20) -> dict[str, Any]:
    """Find a Telegram chat by name substring, @username, phone number, 'me', or numeric chat_id."""
    matches = await service.resolve_chat(query, limit=limit)
    return {"count": len(matches), "matches": matches}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Telegram Messages",
        read_only_hint=True,
        open_world_hint=True,
    )
)
async def get_messages(
    chat: str,
    limit: int = 20,
    offset_id: int = 0,
    search: str | None = None,
) -> dict[str, Any]:
    """Read messages from a chat (accepts chat_id, @username, phone, or 'me')."""
    return await service.get_messages(
        chat,
        limit=limit,
        offset_id=offset_id,
        search=search,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Global Telegram Messages",
        read_only_hint=True,
        open_world_hint=True,
    )
)
async def search_global_messages(query: str, limit: int = 20) -> dict[str, Any]:
    """Search for messages matching a query across all accessible Telegram chats."""
    messages = await service.search_global_messages(query, limit=limit)
    return {"count": len(messages), "messages": messages}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Send or Reply to Telegram Message",
        read_only_hint=False,
        destructive_hint=False,
        open_world_hint=True,
    )
)
async def send_message(
    chat: str,
    text: str,
    reply_to_msg_id: int | None = None,
    parse_mode: str | None = None,
) -> dict[str, Any]:
    """Send a message to a chat or reply to a specific message_id (parse_mode: 'md', 'html', or omit)."""
    return await service.send_message(
        chat,
        text,
        reply_to_msg_id=reply_to_msg_id,
        parse_mode=parse_mode,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Edit Telegram Message",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def edit_message(
    chat: str,
    message_id: int,
    new_text: str,
    parse_mode: str | None = None,
) -> dict[str, Any]:
    """Edit an existing outgoing Telegram message in the specified chat."""
    return await service.edit_message(
        chat,
        message_id,
        new_text,
        parse_mode=parse_mode,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Mark Telegram Chat as Read",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def mark_as_read(chat: str, max_id: int | None = None) -> dict[str, Any]:
    """Mark messages in a Telegram chat as read (optionally up to max_id)."""
    return await service.mark_as_read(chat, max_id=max_id)


def main() -> None:
    """Start the Telegram MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
