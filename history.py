"""Bounded, tenant-scoped multi-turn chat history."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Sequence

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
            del messages[:-self.max_messages]

    def clear(self, tenant_id: str, user_id: str, session_id: str) -> None:
        with self._lock:
            self._messages.pop((tenant_id, user_id, session_id), None)
