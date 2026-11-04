#!/usr/bin/env python3
"""Post-process extracted Markdown to fix extraction artifacts.

Fixes:
  * Hyphenation line-break artifacts: "de $-$ cisions" -> "decisions",
    "landhold- ing" -> "landholding"
  * Stray bullet characters: "" -> "•"
  * LaTeX blank blanks: "$________1. $" -> cleaned
  * Double spaces from column interleaving
  * Stray "$ - $" fragments from hyphenated words split across columns
"""

from __future__ import annotations

import argparse
import re
import sys


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
