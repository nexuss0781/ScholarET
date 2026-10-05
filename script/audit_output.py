#!/usr/bin/env python3
"""Audit an extracted book.md and report every structural defect we can detect.

This runs in GitHub Actions, never as a local experiment. It exists because the
LaTeX gate can only assert counts (">100 spans"), which tells us nothing about
whether the spans are actually equations or paragraphs that merely contain an
"=". So this measures quality instead: it counts prose that leaked into $...$,
display blocks that hold sentences, broken LaTeX escapes, TOC anchors that
swallowed whole pages, and the like.

Usage: audit_output.py book.md [--sample-lines N]
Exit code is always 0; the report is the product, not the verdict.
"""

import argparse
import re
import sys
import unicodedata
from collections import Counter

REPLACEMENT = "�"

# A $...$ span holding this many words is prose, not an equation.
PROSE_WORDS = 5
# A $$ block holding this many words is prose, not a display equation.
DISPLAY_PROSE_WORDS = 8
# A link label this long is a whole page, not a heading.
ANCHOR_LABEL_CHARS = 200


def words(text):
    return re.findall(r"[^\s]+", text)


def inline_math(text):
    """Return inline $...$ spans, ignoring $$ display delimiters."""
    spans = []
    i = 0
    while i < len(text):
        if text[i] != "$":
            i += 1
            continue
        if text.startswith("$$", i):
            end = text.find("$$", i + 2)
            i = (end + 2) if end != -1 else (i + 2)
            continue
        end = text.find("$", i + 1)
        if end == -1:
            break
        spans.append(text[i + 1 : end])
        i = end + 1
    return spans


def display_blocks(lines):
    """Yield (start_line_no, content_lines) for each $$ ... $$ block."""
    i = 0
    while i < len(lines):
        if lines[i].strip() == "$$":
            body = []
            j = i + 1
            while j < len(lines) and lines[j].strip() != "$$":
                body.append(lines[j])
                j += 1
            yield i + 1, body
            i = j + 1
            continue
        i += 1


def odd_dollar_lines(lines):
    """Lines whose unescaped $ count is odd, i.e. math never closed."""
    bad = []
    for n, line in enumerate(lines, 1):
        s = re.sub(r"\\\$", "", line)
        s = s.replace("$$", "")
        if s.count("$") % 2 == 1:
            bad.append((n, line))
    return bad


def mojibake_hits(text):
    """Latin-1 bytes decoded as UTF-8: a sign of a decoding round-trip bug."""
    hits = []
    for marker in ("â€", "Ã¢", "Ã©", "Ã¼", "Ã¶", "Ã¤", "ï¿½", "Â "):
        if marker in text:
            hits.append((marker, text.count(marker)))
    return hits


def control_chars(text):
    bad = Counter()
    for ch in text:
        if unicodedata.category(ch) == "Cc" and ch not in "\n\r\t":
            bad[repr(ch)] += 1
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--sample-lines", type=int, default=6)
    args = ap.parse_args()

    raw = open(args.path, encoding="utf-8", errors="replace").read()
    lines = raw.split("\n")
    n = args.sample_lines
    out = []
    p = lambda s="": out.append(s)

    p("=" * 72)
    p("SCHOLARET-AUDIT  " + args.path)
    p("=" * 72)
    p(f"bytes            {len(raw.encode('utf-8')):,}")
    p(f"lines            {len(lines):,}")
    p(f"headings         {sum(1 for l in lines if l.startswith('#')):,}")

    findings = []

    # ---------------------------------------------------------------- U+FFFD
    repl_total = raw.count(REPLACEMENT)
    repl_lines = [i for i, l in enumerate(lines, 1) if REPLACEMENT in l]
    p()
    p("-- undecoded glyphs (U+FFFD) " + "-" * 45)
    p(f"count            {repl_total}")
    p(f"lines affected   {len(repl_lines)}")
    for ln in repl_lines[:n]:
        text = lines[ln - 1]
        i = text.index(REPLACEMENT)
        p(f"  L{ln}: ...{text[max(0, i - 45):i + 45]}...")
    findings.append(("U+FFFD glyphs", repl_total, True))

    # ------------------------------------------------- prose inside $...$
    p()
    p("-- prose inside inline math " + "-" * 45)
    spans = []
    for i, l in enumerate(lines, 1):
        if l.strip() == "$$":
            continue
        for s in inline_math(l):
            spans.append((i, s))
    long_spans = [(i, s) for i, s in spans if len(words(s)) >= PROSE_WORDS]
    p(f"inline spans     {len(spans):,}")
    p(f"spans >={PROSE_WORDS} words  {len(long_spans):,}  ({100.0 * len(long_spans) / max(1, len(spans)):.1f}%)")
    for i, s in long_spans[:n]:
        p(f"  L{i}: ${s[:100]}$")
    findings.append((f"inline spans with >={PROSE_WORDS} words", len(long_spans), False))

    longest = sorted(spans, key=lambda t: -len(words(t[1])))[:n]
    for i, s in longest:
        p(f"  longest L{i} ({len(words(s))} words): ${s[:120]}$")

    # --------------------------------------------- prose inside $$ ... $$
    p()
    p("-- display math blocks " + "-" * 45)
    blocks = list(display_blocks(lines))
    prose_blocks = [(ln, b) for ln, b in blocks if len(words(" ".join(b))) >= DISPLAY_PROSE_WORDS]
    empty_blocks = [(ln, b) for ln, b in blocks if not " ".join(b).strip()]
    p(f"blocks           {len(blocks):,}")
    p(f"prose blocks     {len(prose_blocks):,}  (>={DISPLAY_PROSE_WORDS} words)")
    p(f"empty blocks     {len(empty_blocks):,}")
    for ln, b in prose_blocks[:n]:
        p(f"  L{ln}: {' '.join(b)[:140]}")
    for ln, _ in empty_blocks[:3]:
        p(f"  empty block at L{ln}")
    findings.append(("display blocks holding prose", len(prose_blocks), False))

    # ------------------------------------------------------- broken LaTeX
    p()
    p("-- LaTeX syntax " + "-" * 49)
    p(f"double backslash {raw.count(chr(92) + chr(92)):,}   (\\times written as \\\\times)")
    escaped_parens = len(re.findall(r"\\\(", raw))
    p(f"escaped parens   {escaped_parens:,}   (a literal backslash-paren, which is not math)")
    empty_scripts = len(re.findall(r"[\^_]\{\}", raw))
    p(f"empty scripts    {empty_scripts:,}   (a superscript or subscript with no body)")
    cmds = Counter(re.findall(r"\\([A-Za-z]+)", raw))
    p(f"commands used    {dict(cmds.most_common(12))}")
    for name in ("times", "sqrt", "cdot", "pm", "leq", "geq", "neq", "approx", "int", "partial"):
        p(f"  \\{name:<10} {cmds.get(name, 0):,}")
    p(f"superscripts     {len(re.findall(chr(94) + r'[{][^{}]{0,12}[}]', raw)):,}")
    p(f"subscripts       {len(re.findall(chr(95) + r'[{][^{}]{0,12}[}]', raw)):,}")
    sqrt_groups = len(re.findall(r"sqrt\{", raw))
    p(f"sqrt groups      {sqrt_groups:,}")
    findings.append(("double backslashes", raw.count(chr(92) + chr(92)), True))

    # --------------------------------------------------------- unbalanced $
    p()
    p("-- delimiter balance " + "-" * 49)
    odd = odd_dollar_lines(lines)
    p(f"lines with odd $ {len(odd):,}")
    for ln, l in odd[:n]:
        p(f"  L{ln}: {l[:110]}")
    findings.append(("lines with unbalanced $", len(odd), True))

    # --------------------------------------------------- TOC anchor blowup
    p()
    p("-- link and anchor labels " + "-" * 43)
    big = []
    for i, l in enumerate(lines, 1):
        for m in re.finditer(r"\[([^\]]*)\]\(([^)]*)\)", l):
            if len(m.group(1)) >= ANCHOR_LABEL_CHARS:
                big.append((i, len(m.group(1)), m.group(1)[:90]))
    p(f"anchors >={ANCHOR_LABEL_CHARS} chars  {len(big):,}")
    for ln, ln_len, lbl in big[:n]:
        p(f"  L{ln} ({ln_len} chars): [{lbl}...")
    findings.append(("oversized anchor labels", len(big), True))

    # ----------------------------------------------------- page furniture
    p()
    p("-- page furniture " + "-" * 51)
    for marker in ("Ethiofetena.com", "Ethiopian No 1 Educational Website"):
        c = raw.count(marker)
        p(f"{marker[:34]:<34} {c:,}")
        findings.append((marker, c, False))
    only_num = sum(1 for l in lines if re.fullmatch(r"\s*[-–—•]?\s*\d{1,4}\s*", l))
    p(f"lines that are only a number   {only_num:,}")
    findings.append(("numeric-only lines", only_num, False))

    # ------------------------------------------------- structural spacing
    p()
    p("-- structure " + "-" * 57)
    p(f"bare bullet lines   {sum(1 for l in lines if re.fullmatch(r'\\s*[-*+]\\s+', l)):,}")
    image_refs = len(re.findall(r"!\[[^\]]*\]\([^)]*\)", raw))
    p(f"image refs          {image_refs:,}")
    p(f"heading lines       {sum(1 for l in lines if re.fullmatch(r'#{1,6} .+', l)):,}")
    longest_line = max(range(len(lines)), key=lambda i: len(lines[i]))
    p(f"longest line        L{longest_line + 1} ({len(lines[longest_line]):,} chars)")
    p(f"  {lines[longest_line][:120]}")

    # ------------------------------------------------------- encoding
    p()
    p("-- encoding " + "-" * 56)
    moji = mojibake_hits(raw)
    p(f"mojibake markers    {moji if moji else 'none'}")
    cc = control_chars(raw)
    p(f"control characters  {dict(cc) if cc else 'none'}")
    p(f"non-ascii chars     {sum(1 for ch in raw if ord(ch) > 127):,}")

    # ---------------------------------------------------------- verdict
    p()
    p("=" * 72)
    p("SUMMARY (count, expected = 0 unless noted)")
    for name, count, must_be_zero in findings:
        flag = "" if not must_be_zero else ("  OK" if count == 0 else "  <-- DEFECT")
        note = "" if must_be_zero else "  (informational)"
        p(f"  {name:<42} {count:>8,}{flag}{note}")
    p("=" * 72)

    print("\n".join(out))


if __name__ == "__main__":
    sys.exit(main())