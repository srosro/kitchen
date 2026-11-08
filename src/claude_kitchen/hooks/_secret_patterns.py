#!/usr/bin/env python3
"""Shared secret-shape registry for code-config hooks and transcript mining."""
from __future__ import annotations

import re


_ASSIGN_KEY = r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)"
# A complete mask is ellipsis + exactly three retained characters. Sentence
# punctuation belongs to surrounding prose when followed by whitespace/end.
_CANONICAL_MASK = r"…[^\s]{3}(?=$|[\s'\"&;]|[,.!?):\]}](?=\s|$))"
_PEM_END = r"-----END (?:[A-Z]+ )?PRIVATE KEY-----"
_SINGLE_QUOTED_END = r"(?:'|(?=[\r\n]|$))"
_DOUBLE_QUOTED_END = r'(?:"|(?=[\r\n]|$))'

# Kept separate because redacting consumers replace the whole block with a
# canonical placeholder; generic patterns preserve their surrounding context.
PEM_PATTERN = re.compile(
    rf"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----\s*([\s\S]*?\S|)\s*(?:{_PEM_END}|$)"
)

# Every expression captures the secret value in group 1 so redacting consumers
# can retain safe surrounding context. Keep distinctive token forms first.
SECRET_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b([sr]k_(?:live|test)_[A-Za-z0-9]{16,})\b"),
    re.compile(r"\b(gh[oprsu]_[A-Za-z0-9_]{30,})\b"),
    re.compile(r"\b(github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(r"\b(xox[abporst]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"\b((?:AKIA|ASIA)[A-Z0-9]{16,})\b"),
    re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"),
    re.compile(rf"(?im)(?:\b|_){_ASSIGN_KEY}['\"]?\s*[=:]\s*\$?'(?!…[^\s]{{3}}{_SINGLE_QUOTED_END})((?:\\.|[^'\r\n])+){_SINGLE_QUOTED_END}"),
    re.compile(rf'(?im)(?:\b|_){_ASSIGN_KEY}[\'\"]?\s*[=:]\s*\$?"(?!…[^\s]{{3}}{_DOUBLE_QUOTED_END})((?:\\.|[^"\r\n])+){_DOUBLE_QUOTED_END}'),
    re.compile(rf"(?i)(?:\b|_){_ASSIGN_KEY}['\"]?\s*[=:]\s*(?!\\?['\"]|{_CANONICAL_MASK})([^\s'\"&;]+)"),
    re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{8,})"),
    re.compile(r'''(?i)["']?\bauthorization["']?\s*:\s*["']?bearer\s+([A-Za-z0-9._~+/=-]+)'''),
    re.compile(r'''(?i)["']?\bauthorization["']?\s*:\s*["']?basic\s+([A-Za-z0-9+/]{4,}={0,2}|[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)(?=["'\s;,]|$)'''),
    re.compile(r"://[^/\s:@?#]*:(?!…[^@\s/?#]{3}@)([^@\s/?#]+)@"),
)


def mask_secret(value: str) -> str:
    """Retain a useful tail only when it does not reveal most of the value."""
    return f"…{value[-3:]}" if len(value) >= 8 else "…***"


def contains_secret(text: str) -> bool:
    """Return whether text contains any registered credential shape."""
    return bool(PEM_PATTERN.search(text)) or any(pattern.search(text) for pattern in SECRET_PATTERNS)
