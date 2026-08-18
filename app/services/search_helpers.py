"""Shared wildcard matching for BrooksHouse search fields.

Plain text keeps the familiar "contains" behavior. Percent/asterisk match any
number of characters; underscore/question-mark match exactly one character.
A backslash makes the next wildcard character literal.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


def clean_search_term(value: Any) -> str:
    return str(value or "").strip()


def _tokenize(value: Any) -> tuple[str, str, bool]:
    term = clean_search_term(value)
    sql_parts: list[str] = []
    regex_parts: list[str] = []
    has_wildcard = False
    index = 0

    while index < len(term):
        character = term[index]
        if character == "\\" and index + 1 < len(term):
            index += 1
            literal = term[index]
            sql_parts.append("\\" + literal if literal in {"%", "_", "\\"} else literal)
            regex_parts.append(re.escape(literal))
        elif character in {"%", "*"}:
            sql_parts.append("%")
            regex_parts.append(".*")
            has_wildcard = True
        elif character in {"_", "?"}:
            sql_parts.append("_")
            regex_parts.append(".")
            has_wildcard = True
        else:
            sql_parts.append("\\" + character if character in {"%", "_", "\\"} else character)
            regex_parts.append(re.escape(character))
        index += 1

    return "".join(sql_parts), "".join(regex_parts), has_wildcard


def sql_wildcard_pattern(value: Any) -> str:
    """Build a parameterized SQL LIKE/ILIKE pattern."""

    sql_pattern, _, has_wildcard = _tokenize(value)
    return f"%{sql_pattern}%" if sql_pattern and not has_wildcard else sql_pattern


def has_search_wildcard(value: Any) -> bool:
    """Return True when the user supplied an unescaped wildcard."""

    _, _, has_wildcard = _tokenize(value)
    return has_wildcard


def wildcard_match(value: Any, search_term: Any) -> bool:
    """Case-insensitively match one value using BrooksHouse wildcard rules."""

    term = clean_search_term(search_term)
    if not term:
        return True
    _, regex_pattern, has_wildcard = _tokenize(term)
    if not has_wildcard:
        regex_pattern = ".*" + regex_pattern + ".*"
    return re.fullmatch(
        regex_pattern,
        str(value or ""),
        flags=re.IGNORECASE | re.DOTALL,
    ) is not None


def wildcard_matches_any(values: Iterable[Any], search_term: Any) -> bool:
    """Return True when at least one supplied value matches the search term."""

    return any(wildcard_match(value, search_term) for value in values)
