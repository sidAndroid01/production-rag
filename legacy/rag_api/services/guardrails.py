import re
from typing import ClassVar


class UnsafeInputError(ValueError):
    pass


class InputGuardrail:
    _patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"ignore (all|any|the) previous instructions", re.I),
        re.compile(r"reveal (the )?(system|developer) prompt", re.I),
        re.compile(r"print.*(api[_ -]?key|secret|password)", re.I),
    ]

    def validate_query(self, query: str, max_length: int) -> str:
        query = query.strip()
        if not query or len(query) > max_length:
            raise UnsafeInputError("Query is empty or exceeds the configured length limit")
        if any(pattern.search(query) for pattern in self._patterns):
            raise UnsafeInputError("Query matched a prompt-injection policy")
        return query

    def sanitize_document(self, text: str) -> str:
        # Documents are untrusted data: neutralize common instruction-shaped content.
        return re.sub(
            r"(?im)^\s*(system|assistant|developer)\s*:\s*",
            "[untrusted-document-label]: ",
            text,
        )
