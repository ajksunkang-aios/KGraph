"""
KGraph — SCIP Symbol Name Parser

SCIP uses a structured string format for global symbols:
    <scheme> ' ' <manager> ' ' <name> ' ' <version> ' ' <descriptor>+

The four leading fields are always present (sentinel "." / "$" when empty),
followed by the descriptor grammar. Spaces inside descriptor names are escaped
as double-space.

Real scip-clang emits, e.g.:
    "cxx . . $ file_operations#read_iter."
    "cxx . . $ file_operations#"
    "cxx . . $ ext4_file_read_iter()."

Descriptor suffixes determine kind:
    / → Namespace/Module
    # → Type (struct/class)
    . → Term (variable/field — refined to Field when parent is a Type)
    (). → Method
    (disambiguator). → Method with disambiguation
    ! → Macro
    : → Meta
    [] → TypeParameter
    (name) → Parameter

This parser extracts: short name, kind, enclosing symbol prefix.
"""

from __future__ import annotations

import re

from .models import SymbolKind


# Descriptor suffix → our SymbolKind
_SUFFIX_TO_KIND = {
    "/": SymbolKind.MODULE,
    "#": SymbolKind.STRUCT,
    ".": SymbolKind.GLOBAL_VAR,      # Term — could be field or var, refined by SCIP Kind
    "!": SymbolKind.MACRO,
    ":": SymbolKind.GLOBAL_VAR,      # Meta — rare in C, treat as var
}

# Method suffix patterns: "()." or "(disambiguator)."
_METHOD_RE = re.compile(r"\(([^)]*)\)\.$")
_METHOD_SUFFIX_KIND = SymbolKind.FUNCTION  # methods are functions in our model

# TypeParameter: "[name]"
_TYPE_PARAM_RE = re.compile(r"\[([^]]+)\]$")
_TYPE_PARAM_KIND = SymbolKind.TYPE_PARAMETER

# Parameter: "(name)" — but not method disambiguator form
# This is ambiguous with method suffix, so we rely on context


def parse_scip_symbol(symbol_str: str) -> dict:
    """
    Parse a SCIP symbol string into structured components.

    Returns dict with:
        - scheme: str
        - package: str (manager name version)
        - descriptors: list of (name, suffix, kind) tuples
        - short_name: str  (last descriptor's name)
        - kind: str  (SymbolKind inferred from last descriptor suffix + SCIP Kind)
        - enclosing_symbol: str  (symbol string without last descriptor)

    Handles local symbols: "local <id>" — returns name=id, kind=variable.

    Returns empty dict for malformed/empty strings.
    """
    if not symbol_str:
        return {}

    # Local symbols: "local <id>"
    if symbol_str.startswith("local "):
        local_id = symbol_str[6:]
        return {
            "scheme": "local",
            "package": "",
            "descriptors": [],
            "short_name": local_id,
            "kind": SymbolKind.VARIABLE,
            "enclosing_symbol": "",
        }

    # Split the canonical SCIP header from the descriptor grammar.
    #
    # Canonical format (per the SCIP spec, and as emitted by real scip-clang):
    #     <scheme> <manager> <name> <version> <descriptor>+
    # The four leading fields are always present (sentinel "." / "$" when
    # empty), followed by the descriptor grammar. Spaces inside descriptor
    # names are escaped as double-space, so splitting the header on single
    # spaces is safe — the package fields are identifiers/sentinels and never
    # contain unescaped spaces.
    #
    # Real scip-clang emits e.g. "cxx . . $ file_operations#read_iter."
    # (scheme=cxx, manager=., name=., version=$). Splitting on the first four
    # single-spaces yields the correct descriptor tail "file_operations#read_iter.".
    parts = symbol_str.split(" ", 4)
    if len(parts) < 5:
        # Malformed / truncated header (fewer than 4 leading fields + descriptors)
        return {
            "scheme": parts[0] if len(parts) > 0 else "",
            "package": " ".join(parts[1:]) if len(parts) > 1 else "",
            "descriptors": [],
            "short_name": "",
            "kind": SymbolKind.GLOBAL_VAR,
            "enclosing_symbol": "",
        }

    scheme = parts[0]
    package = " ".join(parts[1:4])  # manager + name + version
    descriptors_str = parts[4]

    # Parse descriptors by walking the string and identifying suffix characters
    descriptors = _parse_descriptors(descriptors_str)

    if not descriptors:
        return {
            "scheme": scheme,
            "package": package,
            "descriptors": [],
            "short_name": "",
            "kind": SymbolKind.GLOBAL_VAR,
            "enclosing_symbol": "",
        }

    # Last descriptor determines the symbol's identity
    last_desc = descriptors[-1]
    short_name = last_desc["name"]
    kind = last_desc["kind"]

    # Build enclosing symbol string (everything except the last descriptor)
    # Reconstruct: scheme + " " + package + " " + all descriptors except last
    if len(descriptors) > 1:
        prefix_descriptors_str = _reconstruct_descriptors(descriptors[:-1])
        enclosing_symbol = f"{scheme} {package} {prefix_descriptors_str}"
    else:
        enclosing_symbol = ""

    return {
        "scheme": scheme,
        "package": package,
        "descriptors": descriptors,
        "short_name": short_name,
        "kind": kind,
        "enclosing_symbol": enclosing_symbol,
    }


def _parse_descriptors(desc_str: str) -> list[dict]:
    """
    Parse descriptor string into list of descriptor dicts.

    Each descriptor dict has: name, suffix, kind.

    The descriptor string is a concatenation of:
        name + suffix_char
    where suffix_char is one of: / # . ! : or () for methods, [] for type params.

    Escaped identifiers are wrapped in backticks: `name with spaces`.

    This is a simplified parser — handles the common C kernel patterns.
    """
    descriptors = []
    pos = 0
    length = len(desc_str)

    while pos < length:
        # Extract name (possibly escaped)
        name, new_pos = _extract_name(desc_str, pos)
        pos = new_pos

        if pos >= length:
            # No suffix — shouldn't happen in valid SCIP, but handle gracefully
            descriptors.append({
                "name": name,
                "suffix": "",
                "kind": SymbolKind.GLOBAL_VAR,
            })
            break

        # Determine suffix and kind from next character(s)
        ch = desc_str[pos]

        if ch == "(":
            # Method descriptor: name(disambiguator). or name().
            # Find matching closing paren + "."
            paren_end = desc_str.find(").", pos)
            if paren_end >= 0:
                disambiguator = desc_str[pos + 1:paren_end]
                suffix = desc_str[pos:paren_end + 2]  # e.g. "(+1)."
                pos = paren_end + 2
                descriptors.append({
                    "name": name,
                    "suffix": suffix,
                    "kind": SymbolKind.FUNCTION,
                })
            else:
                # Parameter: (name) without trailing "."
                paren_close = desc_str.find(")", pos)
                if paren_close >= 0:
                    suffix = desc_str[pos:paren_close + 1]
                    pos = paren_close + 1
                    descriptors.append({
                        "name": name,
                        "suffix": suffix,
                        "kind": SymbolKind.PARAMETER,
                    })
                else:
                    # Malformed — skip
                    pos = length

        elif ch == "[":
            # TypeParameter: [name]
            bracket_close = desc_str.find("]", pos)
            if bracket_close >= 0:
                suffix = desc_str[pos:bracket_close + 1]
                pos = bracket_close + 1
                descriptors.append({
                    "name": name,
                    "suffix": suffix,
                    "kind": SymbolKind.TYPE_PARAMETER,
                })
            else:
                pos = length

        elif ch in _SUFFIX_TO_KIND:
            kind = _SUFFIX_TO_KIND[ch]
            # Special: "." suffix for Term — if the symbol has a "#" (Type) descriptor
            # before it, then "." means Field, not global_var.
            # We'll refine this later based on SCIP Kind enum.
            suffix = ch
            pos += 1
            descriptors.append({
                "name": name,
                "suffix": suffix,
                "kind": kind,
            })
        else:
            # Unknown suffix — treat as part of name
            descriptors.append({
                "name": name + ch,
                "suffix": "",
                "kind": SymbolKind.GLOBAL_VAR,
            })
            pos += 1

    return descriptors


def _extract_name(desc_str: str, pos: int) -> tuple[str, int]:
    """
    Extract a descriptor name starting at position pos.

    Names can be:
    - Simple: sequence of identifier chars [_a-zA-Z0-9+-$.]
    - Escaped: `name with special chars` (backtick-wrapped)
    """
    if pos >= len(desc_str):
        return "", pos

    if desc_str[pos] == "`":
        # Escaped identifier
        # Find closing backtick (double-backtick escapes a literal backtick inside)
        end = pos + 1
        while end < len(desc_str):
            if desc_str[end] == "`":
                if end + 1 < len(desc_str) and desc_str[end + 1] == "`":
                    end += 2  # escaped backtick
                else:
                    return desc_str[pos + 1:end].replace("``", "`"), end + 1
            else:
                end += 1
        # No closing backtick found
        return desc_str[pos + 1:end].replace("``", "`"), end

    else:
        # Simple identifier: [a-zA-Z0-9_+-$.]
        end = pos
        while end < len(desc_str) and _is_identifier_char(desc_str[end]):
            end += 1
        return desc_str[pos:end], end


def _is_identifier_char(ch: str) -> bool:
    """Check if char is a valid SCIP simple-identifier character."""
    return ch.isalnum() or ch in ("_", "+", "-", "$")


def _reconstruct_descriptors(descriptors: list[dict]) -> str:
    """
    Reconstruct descriptor string from parsed descriptor list.
    Used to build the enclosing_symbol string.
    """
    parts = []
    for d in descriptors:
        name = d["name"]
        suffix = d["suffix"]
        # Escaped names need backticks if they contain non-identifier chars
        needs_escape = any(not _is_identifier_char(c) and c != "`" for c in name)
        if needs_escape:
            escaped_name = name.replace("`", "``")
            parts.append(f"`{escaped_name}`{suffix}")
        else:
            parts.append(f"{name}{suffix}")
    return "".join(parts)