"""Format Knowledge Hub YAML ``author`` as quoted Obsidian wikilink(s)."""

from __future__ import annotations

import re
import unicodedata

# Strong separators first; a plain comma is last and still requires person-name sides.
_SPLITTERS = (
    re.compile(r"\s*,\s*and\s+", re.I),
    re.compile(r"\s+and\s+", re.I),
    re.compile(r"\s+&\s+"),
    re.compile(r"\s*;\s*"),
    re.compile(r"\s*,\s*"),
)

# 2+ of these tokens (initials allowed) is a "person name" for split decisions.
# Accented letters (Rubén, Martínez) count; spelling is not ASCII-folded.
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
_SINGLE_WIKILINK = re.compile(r"^\[\[[^\]]+\]\]$")
_WIKILINK_FIND = re.compile(r"\[\[.*?\]\]")


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _nonempty(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
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


def _is_unicode_name_letter(char: str) -> bool:
    """Letter or combining mark. Does not ASCII-fold."""
    category = unicodedata.category(char)
    return category.startswith("L") or category in {"Mn", "Mc", "Lm"}


def _is_unicode_name_token(token: str) -> bool:
    """Capitalized name token that may include accented letters (``Rubén``)."""
    if not token or unicodedata.category(token[0]) != "Lu":
        return False
    return all(
        _is_unicode_name_letter(char) or char in "'’\u2019-"
        for char in token[1:]
    )


def _is_name_token(token: str) -> bool:
    return bool(_NAME_TOKEN.match(token)) or _is_unicode_name_token(token)


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
    Accented letters count as name tokens (``Rubén Martínez``); spelling is
    preserved.
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


def _author_text_from_raw(raw: object) -> str | None:
    """Flatten a string or list of author pieces into one comparable string."""
    if isinstance(raw, list):
        pieces = []
        for item in raw:
            text = _author_text_from_raw(item)
            if text:
                pieces.append(text)
        return ", ".join(pieces) or None
    return _nonempty(raw)


def plain_author_label(raw: object) -> str | None:
    """Author text for a ``Title by Author`` stem or other non-YAML uses.

    Never includes ``[[`` / ``]]``. Wikilink brackets belong only on Knowledge
    Hub YAML ``author``. Does not collapse internal whitespace so the stem
    matches the existing filename sanitizer.
    """
    text = _author_text_from_raw(raw)
    if not text:
        return None
    return _unwrap_wikilinks(text).strip() or None


def _wikilink_author_links(raw: object) -> list[str]:
    """Build ``[[Name]]`` links from a string or list. Empty when junk."""
    text = _author_text_from_raw(raw)
    if not text:
        return []
    links: list[str] = []
    for part in split_author_names(_collapse(text)):
        target = _wikilink_target(part)
        if not target:
            continue
        links.append(f"[[{target}]]")
    return links


def author_frontmatter_value(
    raw: object, *, is_tweet: bool = False
) -> str | list[str] | None:
    """Turn an author/creator string into a YAML ``author`` value (unquoted).

    One author: ``[[W. Brian Arthur]]`` (string).
    Several: ``["[[Alice Smith]]", "[[Bob Jones]]"]`` (YAML list, same shape
    as People). Never a comma-joined ``"[[A]], [[B]]"`` scalar.
    Tweet ``@handle on Twitter`` strings stay plain. Empty/junk returns None.

    Use this only for Knowledge Hub YAML. Buffet / journal / highlight / filename
    stems must stay plain via ``plain_author_label``.
    """
    text = _author_text_from_raw(raw)
    if not text:
        return None
    collapsed = _collapse(text)
    if is_tweet or _looks_like_tweet_handle_author(collapsed):
        return collapsed

    links = _wikilink_author_links(collapsed)
    if not links:
        return None
    if len(links) == 1:
        return links[0]
    return links


def quote_yaml_scalar(value: str) -> str:
    """Double-quote a YAML scalar so Obsidian keeps ``[[wikilink]]`` brackets."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def author_yaml_literal(raw: object, *, is_tweet: bool = False) -> str:
    """Value to write after ``author: `` in a hand-built YAML block.

    Quoted wikilink, a YAML list of quoted wikilinks (leading newline so
    ``author:{literal}`` matches People), a plain tweet handle, or empty
    string.
    """
    formatted = author_frontmatter_value(raw, is_tweet=is_tweet)
    if not formatted:
        return ""
    if isinstance(formatted, list):
        items = "\n".join(f"  - {quote_yaml_scalar(item)}" for item in formatted)
        return f"\n{items}"
    if "[[" in formatted:
        return quote_yaml_scalar(formatted)
    return formatted


def author_yaml_field(raw: object, *, is_tweet: bool = False, key: str = "author") -> str:
    """Full ``author: ...`` field for a hand-built YAML block.

    Several authors omit the space after the colon so the list matches People:
    ``author:\\n  - "[[A]]"``. One author stays ``author: "[[A]]"``.
    """
    literal = author_yaml_literal(raw, is_tweet=is_tweet)
    if literal.startswith("\n"):
        return f"{key}:{literal}"
    return f"{key}: {literal}"


def _wikilink_count(value: object) -> int:
    if isinstance(value, list):
        return sum(_wikilink_count(item) for item in value)
    text = _nonempty(value)
    if not text:
        return 0
    return len(_WIKILINK_FIND.findall(text))


def _is_single_wikilink(value: object) -> bool:
    text = _nonempty(value)
    return bool(text and _SINGLE_WIKILINK.match(_collapse(text)))


def _is_canonical_wikilink_author(value: object) -> bool:
    """True when ``author`` is already the new Obsidian-safe form.

    One person: a single quoted-style ``[[Name]]`` string.
    Several: a list of ``[[Name]]`` strings. A comma-joined
    ``"[[A]], [[B]]"`` scalar is the broken old form, not canonical.
    """
    if isinstance(value, list):
        return bool(value) and all(_is_single_wikilink(item) for item in value)
    return _is_single_wikilink(value)


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
    """True when ``existing`` is the same author(s) in an older form than ``new``.

    Accepts a plain-text scalar, the old comma-joined ``"[[A]], [[B]]"``
    string, or the new list of wikilinks. Upgrades the broken string to a
    list when the names match. Does not treat a different existing author
    as upgradable, and does not rewrite an already-canonical value.
    """
    existing_keys = _author_name_key_set(existing)
    new_keys = _author_name_key_set(new)
    if not existing_keys or existing_keys != new_keys:
        return False
    if _is_canonical_wikilink_author(existing):
        return False
    if _is_canonical_wikilink_author(new):
        return True
    return _wikilink_count(new) > _wikilink_count(existing)
