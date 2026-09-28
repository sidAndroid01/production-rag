"""Bounded, tenant-scoped multi-turn chat history (in memory or PostgreSQL)."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from providers import ChatTurn


class ChatHistoryStore:
    def __init__(self, max_turns: int = 12) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        self.max_messages = max_turns * 2
        self._messages: dict[tuple[str, str, str], list[ChatTurn]] = defaultdict(list)
        self._lock = threading.Lock()

    def get(self, tenant_id: str, user_id: str, session_id: str) -> tuple[ChatTurn, ...]:
        with self._lock:
            return tuple(self._messages[(tenant_id, user_id, session_id)])

    def append(self, tenant_id: str, user_id: str, session_id: str, *turns: ChatTurn) -> None:
        with self._lock:
            messages = self._messages[(tenant_id, user_id, session_id)]
            messages.extend(turns)
            del messages[: -self.max_messages]

    def clear(self, tenant_id: str, user_id: str, session_id: str) -> None:
        with self._lock:
            self._messages.pop((tenant_id, user_id, session_id), None)


class PostgresChatHistory:
    """Same contract as ChatHistoryStore, stored in ``chat_messages`` under RLS."""

    def __init__(self, persistence: Any, max_turns: int = 12) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        self.max_messages = max_turns * 2
        self._persistence = persistence

    def get(self, tenant_id: str, user_id: str, session_id: str) -> tuple[ChatTurn, ...]:
        with self._persistence._connect() as connection, connection.cursor() as cursor:
            self._persistence._set_tenant(cursor, tenant_id)
            cursor.execute(
                "SELECT role, content FROM ("
                "  SELECT id, role, content FROM chat_messages"
                "  WHERE tenant_id = %s AND user_id = %s AND session_id = %s"
                "  ORDER BY id DESC LIMIT %s"
                ") AS recent ORDER BY id",
                (tenant_id, user_id, session_id, self.max_messages),
            )
            return tuple(ChatTurn(role, content) for role, content in cursor.fetchall())

    def append(self, tenant_id: str, user_id: str, session_id: str, *turns: ChatTurn) -> None:
        with self._persistence._connect() as connection, connection.cursor() as cursor:
            self._persistence._set_tenant(cursor, tenant_id)
            cursor.executemany(
                "INSERT INTO chat_messages (tenant_id, user_id, session_id, role, content) "
                "VALUES (%s, %s, %s, %s, %s)",
                [(tenant_id, user_id, session_id, turn.role, turn.content) for turn in turns],
            )
            # Keep storage bounded to the window the model can use.
            cursor.execute(
                "DELETE FROM chat_messages WHERE tenant_id = %s AND user_id = %s "
                "AND session_id = %s AND id NOT IN ("
                "  SELECT id FROM chat_messages WHERE tenant_id = %s AND user_id = %s"
                "  AND session_id = %s ORDER BY id DESC LIMIT %s)",
                (tenant_id, user_id, session_id) * 2 + (self.max_messages,),
            )

    def clear(self, tenant_id: str, user_id: str, session_id: str) -> None:
        with self._persistence._connect() as connection, connection.cursor() as cursor:
            self._persistence._set_tenant(cursor, tenant_id)
            cursor.execute(
                "DELETE FROM chat_messages WHERE tenant_id = %s AND user_id = %s "
                "AND session_id = %s",
                (tenant_id, user_id, session_id),
            )
