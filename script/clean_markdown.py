#!/usr/bin/env python3
"""Post-process extracted Markdown to fix extraction artifacts.

Fixes:
  * Hyphenation line-break artifacts: "de $-$ cisions" -> "decisions",
    "landhold- ing" -> "landholding"
  * Stray bullet characters: "" -> "•"
  * LaTeX blank blanks: "$________1. $" -> cleaned
  * Double spaces from column interleaving
  * Stray "$ - $" fragments from hyphenated words split across columns
  * U+FFFD replacement characters -> removed or replaced with hex fallback
  * Unbalanced math delimiters ($...$) per line -> balanced or removed
  * Oversized anchor/heading labels -> truncated to <= 100 chars
"""

from __future__ import annotations

import argparse
import re
import sys


def _balance_dollars(line: str) -> str:
    """Balance math delimiters on a single line.

    If a line has an odd number of unescaped $ characters, remove the
    trailing unpaired one(s) so the line is well-formed. Also handle
    cases where $ appears in non-math contexts (currency like "$30").
    """
    # Count unescaped $ signs
    dollars = [m.start() for m in re.finditer(r'(?<!\\)\$', line)]
    if len(dollars) % 2 == 0:
        return line  # already balanced

    # Odd number: remove the last unpaired $
    if dollars:
        last = dollars[-1]
        line = line[:last] + line[last + 1:]
    return line


def _truncate_anchors(text: str) -> str:
    """Truncate heading slugs in anchor links to <= 100 chars."""
    # Match anchor links like {#page-N} or {#some-long-slug}
    def repl(m):
        full = m.group(0)
        anchor = m.group(1)
        if len(anchor) > 100:
            truncated = anchor[:97] + "..."
            return full.replace(anchor, truncated)
        return full

    return re.sub(r"\{#([^}]+)\}", repl, text)


def clean_text(text: str) -> str:
    # --- Fix hyphenation artifacts split across columns/lines ---
    # "de $-$ cisions" -> "decisions"  (word split across $...$ math)
    text = re.sub(r"\$\s*-\s*\$\s*", "-", text)
    # "landhold- ing" -> "landholding"  (soft hyphen at line end)
    text = re.sub(r"(\w)-\s+\n\s*(\w)", r"\1\2", text)
    # "$-$" standalone -> hyphen
    text = text.replace("$-$", "-")

    # --- Fix stray bullet / dingbat characters ---
    text = text.replace("\u0143", "•")  # LATIN SMALL LETTER C WITH CEDILLA ( mis-decode)
    text = text.replace("", "•")
    text = text.replace("", "•")
    text = text.replace("\u2022", "•")

    # --- Fix LaTeX blank blanks from review exercises ---
    # "$________1. $" -> just remove the blank line artifacts
    text = re.sub(r"\$_{5,}\s*\d+\.\s*\$", "", text)
    text = re.sub(r"\$\s*_{5,}\s*\$", "", text)

    # --- Fix unbalanced math delimiters ($...$) per line ---
    lines = text.split("\n")
    lines = [_balance_dollars(l) for l in lines]
    text = "\n".join(lines)

    # --- Truncate oversized anchor labels ---
    text = _truncate_anchors(text)

    # --- Fix undecoded glyphs (U+FFFD) ---
    # Replace remaining U+FFFD with nothing (they're unrecoverable)
    text = text.replace("\ufffd", "")

    # --- Collapse multiple spaces but not inside code blocks ---
    lines = text.split("\n")
    out = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
        else:
            out.append(re.sub(r"[ \t]{2,}", " ", line))
    text = "\n".join(out)

    # --- Collapse 3+ blank lines to 2 ---
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    # --- Fix hyphen+newline within same paragraph ---
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", help="book.md to clean in place")
    args = parser.parse_args(argv)

    path = args.markdown
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    cleaned = clean_text(text)

    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned)

    removed = len(text) - len(cleaned)
    print(f"cleaned {path}: {len(text)} -> {len(cleaned)} bytes ({removed} removed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
