"""Format Knowledge Hub YAML ``author`` as quoted Obsidian wikilink(s)."""

from __future__ import annotations

import re

# Strong separators first; a plain comma is last and still requires person-name sides.
_SPLITTERS = (
    re.compile(r"\s*,\s*and\s+", re.I),
    re.compile(r"\s+and\s+", re.I),
    re.compile(r"\s+&\s+"),
    re.compile(r"\s*;\s*"),
    re.compile(r"\s*,\s*"),
)

# 2+ of these tokens (initials allowed) is a "person name" for split decisions.
_NAME_TOKEN = re.compile(
    r"""
    (?:
        [A-Z](?:\.[A-Z])+\.?          # J.R.R. / J.R.R
        | [A-Z]\.                     # W.
        | [A-Z][A-Za-z''\u2019\-]*    # Alice, O'Brien, Anne-Marie
        | (?:Jr|Sr|II|III|IV)\.?      # generational suffix
    )
    $
    """,
    re.VERBOSE,
)

_AUTHOR_HANDLE = re.compile(r"@([^\s]+)")
_ON_TWITTER = re.compile(r"\bon twitter\b", re.I)
_WIKILINK_LIST = re.compile(
    r"^(?:\s*\[\[.*?\]\]\s*,)*\s*\[\[.*?\]\]\s*$"
)


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _nonempty(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _wikilink_target(text: str) -> str | None:
    """Same usable-target rules as ``add_readwise_buffet._wikilink_target``."""
    cleaned = _collapse(text).replace("]]", "")
    cleaned = re.sub(r"[|#^]", "", cleaned)
    return _collapse(cleaned) or None


def _looks_like_tweet_handle_author(text: str) -> bool:
    """True for ``@handle on Twitter`` strings tweet detection already treats as handles."""
    return bool(_AUTHOR_HANDLE.search(text) and _ON_TWITTER.search(text))


def _is_name_token(token: str) -> bool:
    return bool(_NAME_TOKEN.match(token))


def _looks_like_person_name(text: str) -> bool:
    """2+ capitalized name tokens, allowing initials like ``W. Brian Arthur``."""
    tokens = text.split()
    return len(tokens) >= 2 and all(_is_name_token(token) for token in tokens)


def _unwrap_wikilinks(text: str) -> str:
    if "[[" not in text:
        return text
    if _WIKILINK_LIST.match(text):
        return ", ".join(re.findall(r"\[\[(.*?)\]\]", text))
    return text.replace("[[", "").replace("]]", "")


def split_author_names(raw: str) -> list[str]:
    """Split an author/creator string into individual name tokens.

    `` and ``, `` & ``, ``;``, and ``, and `` split only when every resulting
    piece looks like a person name. A plain comma splits only under that same
    rule, so role/org suffixes stay attached:
    ``Mark Zuckerberg, Founder and CEO, Meta`` stays one token.
    """
    text = _collapse(_unwrap_wikilinks(raw))
    if not text:
        return []
    return _split_author_names(text)


def _split_author_names(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    for splitter in _SPLITTERS:
        for match in splitter.finditer(text):
            left = text[: match.start()].strip()
            right = text[match.end() :].strip()
            if not left or not right:
                continue
            left_parts = _split_author_names(left)
            right_parts = _split_author_names(right)
            if (
                left_parts
                and right_parts
                and all(_looks_like_person_name(part) for part in (*left_parts, *right_parts))
            ):
                return left_parts + right_parts
    return [text]


def plain_author_label(raw: object) -> str | None:
    """Author text for a ``Title by Author`` stem or other non-YAML uses.

    Never includes ``[[`` / ``]]``. Wikilink brackets belong only on Knowledge
    Hub YAML ``author``. Does not collapse internal whitespace so the stem
    matches the existing filename sanitizer.
    """
    text = _nonempty(raw)
    if not text:
        return None
    return _unwrap_wikilinks(text).strip() or None


def author_frontmatter_value(raw: object, *, is_tweet: bool = False) -> str | None:
    """Turn an author/creator string into a YAML ``author`` value (unquoted).

    One author: ``[[W. Brian Arthur]]``.
    Several: ``[[Alice Smith]], [[Bob Jones]]`` (same ``author`` key).
    Tweet ``@handle on Twitter`` strings stay plain. Empty/junk returns None.

    Use this only for Knowledge Hub YAML. Buffet / journal / highlight / filename
    stems must stay plain via ``plain_author_label``.
    """
    text = _nonempty(raw)
    if not text:
        return None
    collapsed = _collapse(text)
    if is_tweet or _looks_like_tweet_handle_author(collapsed):
        return collapsed

    links: list[str] = []
    for part in split_author_names(collapsed):
        target = _wikilink_target(part)
        if not target:
            continue
        links.append(f"[[{target}]]")
    if not links:
        return None
    return ", ".join(links)


def quote_yaml_scalar(value: str) -> str:
    """Double-quote a YAML scalar so Obsidian keeps ``[[wikilink]]`` brackets."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def author_yaml_literal(raw: object, *, is_tweet: bool = False) -> str:
    """Value to write after ``author: `` in a hand-built YAML block.

    Quoted wikilink(s), a plain tweet handle, or empty string.
    """
    formatted = author_frontmatter_value(raw, is_tweet=is_tweet)
    if not formatted:
        return ""
    if "[[" in formatted:
        return quote_yaml_scalar(formatted)
    return formatted


def _author_name_key_set(value: object) -> set[str] | None:
    """Comparable set of author names, ignoring wikilink brackets."""
    if isinstance(value, list):
        names: set[str] = set()
        for item in value:
            part = _author_name_key_set(item)
            if part:
                names.update(part)
        return names or None
    text = _nonempty(value)
    if not text:
        return None
    parts = split_author_names(text)
    keys = {_collapse(_unwrap_wikilinks(part)).casefold() for part in parts if _collapse(part)}
    return keys or None


def is_plain_to_wikilink_author_upgrade(existing: object, new: object) -> bool:
    """True when ``existing`` is the same author(s) as plain text and ``new`` adds brackets.

    Does not treat a different existing author as upgradable.
    """
    existing_text = existing if isinstance(existing, str) else None
    new_text = new if isinstance(new, str) else None
    if not existing_text or not new_text:
        return False
    if "[[" in existing_text:
        return False
    if "[[" not in new_text:
        return False
    existing_keys = _author_name_key_set(existing_text)
    new_keys = _author_name_key_set(new_text)
    return bool(existing_keys and existing_keys == new_keys)
