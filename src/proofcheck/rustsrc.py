"""A small, conservative Rust lexer for locating test code in ``.rs`` files.

proofcheck never compiles or interprets Rust itself; it only needs to know
*where* things are:

* ``#[cfg(test)] mod <name> { ... }`` blocks (inline unit tests), so that a
  pull request's changed test module can be transplanted into the base
  commit without also transplanting the fix;
* test functions (``#[test]``, ``#[tokio::test]``, ``#[rstest]``, ...), so
  that only the tests a diff touched count as evidence.

Brace matching is only sound if comments, string literals and character
literals are skipped correctly, so the lexer understands line comments,
nested block comments, (byte/C) string literals with escapes, raw strings
(``r#"..."#``, ``br"..."``, ``cr#"..."#``), raw identifiers (``r#type``),
byte/char literals and lifetimes/labels. Anything it cannot classify with
certainty raises :class:`RustLexError`; callers then skip the file with a
clear note instead of guessing.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

#: Attribute names (last path segment) that mark a test function.
TEST_ATTRS = frozenset({"test", "rstest", "test_case", "quickcheck", "wasm_bindgen_test"})

_OPEN = {"{": "}", "(": ")", "[": "]"}
_CLOSE = {v: k for k, v in _OPEN.items()}


class RustLexError(ValueError):
    """The source could not be tokenized (or bracket-matched) unambiguously."""


@dataclass
class Token:
    kind: str  # ident | punct | literal | lifetime
    text: str
    start: int  # offset of the first character
    end: int  # offset after the last character


@dataclass
class Item:
    """A located item: a test module or a test function."""

    name: str
    start: int  # offset (first attribute included)
    end: int  # offset after the closing brace
    start_line: int  # 1-based
    end_line: int  # 1-based, inclusive
    #: Names of the enclosing inline modules within the file (outermost first).
    parents: list[str] = field(default_factory=list)

    def lines(self) -> range:
        return range(self.start_line, self.end_line + 1)

    @property
    def qualname(self) -> str:
        """Module path within the file, e.g. ``inner::tests``."""
        return "::".join(self.parents + [self.name])


def _is_ident_start(c: str) -> bool:
    return c == "_" or c.isalpha() or (ord(c) > 127 and c.isidentifier())


def _is_ident_char(c: str) -> bool:
    return c == "_" or c.isalnum() or (ord(c) > 127 and ("a" + c).isidentifier())


def tokenize(src: str) -> list[Token]:
    """Split Rust source into tokens, dropping whitespace and comments.

    Raises :class:`RustLexError` for unterminated comments/literals and for
    quote characters that are neither a char literal nor a lifetime.
    """
    toks: list[Token] = []
    n = len(src)
    i = 0
    if src.startswith("#!") and not src.startswith("#!["):
        # shebang line
        nl = src.find("\n")
        i = n if nl < 0 else nl
    while i < n:
        c = src[i]
        if c in " \t\r\n\f\v﻿":
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise RustLexError(f"unterminated block comment at offset {i}")
            i = j
            continue
        if _is_ident_start(c):
            j = i + 1
            while j < n and _is_ident_char(src[j]):
                j += 1
            word = src[i:j]
            if word in ("r", "br", "cr") and j < n and src[j] in "#\"":
                k = j
                while k < n and src[k] == "#":
                    k += 1
                hashes = k - j
                if k < n and src[k] == '"':
                    close = '"' + "#" * hashes
                    end = src.find(close, k + 1)
                    if end < 0:
                        raise RustLexError(f"unterminated raw string at offset {i}")
                    toks.append(Token("literal", src[i:end + len(close)], i, end + len(close)))
                    i = end + len(close)
                    continue
                if word == "r" and hashes == 1 and k < n and _is_ident_start(src[k]):
                    m = k + 1
                    while m < n and _is_ident_char(src[m]):
                        m += 1
                    toks.append(Token("ident", src[k:m], i, m))  # raw identifier r#name
                    i = m
                    continue
                if hashes:
                    raise RustLexError(f"ambiguous '{word}#' at offset {i}")
            if word in ("b", "c") and j < n and src[j] == '"':
                end = _string_end(src, j)
                toks.append(Token("literal", src[i:end], i, end))
                i = end
                continue
            if word == "b" and j < n and src[j] == "'":
                end = _char_end(src, j)
                if end is None:
                    raise RustLexError(f"invalid byte literal at offset {i}")
                toks.append(Token("literal", src[i:end], i, end))
                i = end
                continue
            toks.append(Token("ident", word, i, j))
            i = j
            continue
        if c.isdigit():
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] == "_" or (src[j] == "." and j + 1 < n and src[j + 1].isdigit())):
                j += 1
            toks.append(Token("literal", src[i:j], i, j))
            i = j
            continue
        if c == '"':
            end = _string_end(src, i)
            toks.append(Token("literal", src[i:end], i, end))
            i = end
            continue
        if c == "'":
            end = _char_end(src, i)
            if end is not None:
                toks.append(Token("literal", src[i:end], i, end))
                i = end
                continue
            if i + 1 < n and _is_ident_start(src[i + 1]):
                j = i + 2
                while j < n and _is_ident_char(src[j]):
                    j += 1
                if j < n and src[j] == "'":
                    raise RustLexError(f"ambiguous quote at offset {i} (neither a char literal nor a lifetime)")
                toks.append(Token("lifetime", src[i:j], i, j))
                i = j
                continue
            raise RustLexError(f"ambiguous quote at offset {i}")
        toks.append(Token("punct", c, i, i + 1))
        i += 1
    return toks


def _string_end(src: str, i: int) -> int:
    """Offset after the string literal whose opening quote is at ``i``."""
    n = len(src)
    j = i + 1
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        j += 1
    raise RustLexError(f"unterminated string literal at offset {i}")


def _char_end(src: str, i: int) -> int | None:
    """Offset after a char literal starting with the quote at ``i``, or None if it is not one."""
    n = len(src)
    if i + 1 >= n:
        return None
    c = src[i + 1]
    if c == "\\":
        if i + 2 >= n:
            raise RustLexError(f"unterminated char literal at offset {i}")
        # '\n', '\'', '\\', '\x7f', '\u{1F600}'
        j = i + 3
        limit = min(n, i + 14)
        while j < limit and src[j] != "'":
            if src[j] == "\n":
                break
            j += 1
        if j < n and src[j] == "'":
            return j + 1
        raise RustLexError(f"unterminated char literal at offset {i}")
    if c in "'\n":
        return None
    if i + 2 < n and src[i + 2] == "'":
        return i + 3
    return None


def match_brackets(toks: list[Token]) -> dict[int, int]:
    """Map the index of every opening bracket token to its closing token (and back)."""
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for k, t in enumerate(toks):
        if t.kind != "punct":
            continue
        if t.text in _OPEN:
            stack.append(k)
        elif t.text in _CLOSE:
            if not stack or toks[stack[-1]].text != _CLOSE[t.text]:
                raise RustLexError(f"unbalanced {t.text!r} at offset {t.start}")
            o = stack.pop()
            pairs[o] = k
            pairs[k] = o
    if stack:
        raise RustLexError(f"unclosed {toks[stack[-1]].text!r} at offset {toks[stack[-1]].start}")
    return pairs


class _Source:
    def __init__(self, src: str) -> None:
        self.src = src
        self.toks = tokenize(src)
        self.pairs = match_brackets(self.toks)
        self._line_starts = [0] + [k + 1 for k, ch in enumerate(src) if ch == "\n"]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self._line_starts, offset)

    def is_punct(self, k: int, text: str) -> bool:
        return k < len(self.toks) and self.toks[k].kind == "punct" and self.toks[k].text == text

    def is_ident(self, k: int, text: str | None = None) -> bool:
        return k < len(self.toks) and self.toks[k].kind == "ident" and (text is None or self.toks[k].text == text)

    def attributes(self, k: int) -> tuple[list[tuple[int, int]], int]:
        """Outer attributes starting at token ``k``: ``([(hash_idx, close_idx)], next_idx)``."""
        attrs = []
        while self.is_punct(k, "#") and self.is_punct(k + 1, "["):
            close = self.pairs[k + 1]
            attrs.append((k, close))
            k = close + 1
        return attrs, k

    def attr_tokens(self, attr: tuple[int, int]) -> list[Token]:
        return self.toks[attr[0] + 2:attr[1]]

    def skip_visibility(self, k: int) -> int:
        if self.is_ident(k, "pub"):
            k += 1
            if self.is_punct(k, "("):
                k = self.pairs[k] + 1
        return k

    def item(self, name: str, first: int, last: int, parents: list[str]) -> Item:
        start, end = self.toks[first].start, self.toks[last].end
        return Item(name, start, end, self.line(start), self.line(end - 1), list(parents))


def _is_cfg_test(toks: list[Token]) -> bool:
    """``cfg(test)`` or ``cfg(all(test, ...))``."""
    texts = [t.text for t in toks]
    if texts == ["cfg", "(", "test", ")"]:
        return True
    return texts[:5] == ["cfg", "(", "all", "(", "test"] and len(texts) > 5 and texts[5] in (",", ")")


def _attr_name(toks: list[Token]) -> str:
    """Last path segment of an attribute (``tokio::test`` -> ``test``)."""
    name = ""
    for t in toks:
        if t.kind == "ident":
            name = t.text
        elif t.text == ":":
            continue
        else:
            break
    return name


def _walk(s: _Source, lo: int, hi: int, parents: list[str], mods: list[Item], fns: list[Item], in_test_mod: bool) -> None:
    k = lo
    while k < hi:
        attrs, nxt = s.attributes(k)
        j = s.skip_visibility(nxt)
        if s.is_ident(j, "mod") and s.is_ident(j + 1) and s.is_punct(j + 2, "{"):
            name = s.toks[j + 1].text
            close = s.pairs[j + 2]
            first = attrs[0][0] if attrs else nxt
            is_test = any(_is_cfg_test(s.attr_tokens(a)) for a in attrs)
            if is_test and not in_test_mod:
                mods.append(s.item(name, first, close, parents))
            _walk(s, j + 3, close, parents + [name], mods, fns, in_test_mod or is_test)
            k = close + 1
            continue
        if attrs:
            names = {_attr_name(s.attr_tokens(a)) for a in attrs}
            q = j
            while s.is_ident(q) and s.toks[q].text in ("async", "const", "unsafe", "extern", "default"):
                q += 1
                if s.toks[q - 1].text == "extern" and q < len(s.toks) and s.toks[q].kind == "literal":
                    q += 1
            if s.is_ident(q, "fn") and s.is_ident(q + 1) and names & TEST_ATTRS:
                body = _fn_body(s, q + 2, hi)
                if body is not None:
                    fns.append(s.item(s.toks[q + 1].text, attrs[0][0], s.pairs[body], parents))
                    k = s.pairs[body] + 1
                    continue
            k = nxt if nxt > k else k + 1
            continue
        t = s.toks[k]
        if t.kind == "punct" and t.text in _OPEN:
            # Descend into blocks (impl bodies, fn bodies, macro bodies such as proptest! { ... }).
            close = s.pairs[k]
            _walk(s, k + 1, close, parents, mods, fns, in_test_mod)
            k = close + 1
            continue
        k += 1


def _fn_body(s: _Source, k: int, hi: int) -> int | None:
    """Index of the ``{`` opening the body of the fn whose signature starts at ``k``."""
    while k < hi:
        t = s.toks[k]
        if t.kind == "punct":
            if t.text == "{":
                return k
            if t.text == ";":
                return None
            if t.text in ("(", "["):
                k = s.pairs[k] + 1
                continue
        k += 1
    return None


@dataclass
class RustTestLayout:
    """Test modules and test functions of one Rust source file."""

    test_modules: list[Item]
    test_functions: list[Item]


def analyze(src: str) -> RustTestLayout:
    """Locate ``#[cfg(test)]`` modules and test functions. Raises :class:`RustLexError`."""
    s = _Source(src)
    mods: list[Item] = []
    fns: list[Item] = []
    _walk(s, 0, len(s.toks), [], mods, fns, False)
    return RustTestLayout(mods, fns)


def touched_test_functions(src: str, changed_lines: set[int] | None) -> set[str]:
    """Names of test functions whose span (attributes included) intersects ``changed_lines``.

    ``changed_lines`` of ``None`` means the whole file is new: every test is touched.
    """
    layout = analyze(src)
    return {f.name for f in layout.test_functions if changed_lines is None or any(ln in changed_lines for ln in f.lines())}


@dataclass
class Transplant:
    """Result of moving changed test modules from the head version of a file into the base version."""

    text: str  # the new base text
    modules: list[str]  # module paths within the file (``tests``, ``inner::tests``) of the transplanted modules
    touched: set[str]  # test functions touched by the diff inside those modules


def transplant_test_modules(base_src: str, head_src: str, changed_lines: set[int]) -> Transplant | None:
    """Replace (or append) every ``#[cfg(test)]`` module that the diff changed.

    Returns ``None`` when no test module was changed. Raises
    :class:`RustLexError` when either version cannot be analysed.
    """
    head = analyze(head_src)
    base = analyze(base_src)
    changed = [m for m in head.test_modules if any(ln in changed_lines for ln in m.lines())]
    if not changed:
        return None
    names = [m.qualname for m in changed]
    if len({m.qualname for m in head.test_modules}) != len(head.test_modules) \
            or len({m.qualname for m in base.test_modules}) != len(base.test_modules):
        raise RustLexError("several #[cfg(test)] modules share a name")
    base_by_name = {m.qualname: m for m in base.test_modules}
    text = base_src
    appended: list[str] = []
    # Replace from the end of the file backwards so earlier offsets stay valid.
    replacements = []
    for m in changed:
        block = head_src[m.start:m.end]
        if m.qualname in base_by_name:
            b = base_by_name[m.qualname]
            replacements.append((b.start, b.end, block))
        elif m.parents:
            # A new test module inside another module: its position cannot be reproduced safely.
            raise RustLexError(f"new nested test module {m.qualname} cannot be placed in the base file")
        else:
            appended.append(block)
    for start, end, block in sorted(replacements, reverse=True):
        text = text[:start] + block + text[end:]
    if appended:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n" + "\n\n".join(appended) + "\n"
    touched = set()
    for f in head.test_functions:
        inside = any(f.parents[:len(m.parents) + 1] == m.parents + [m.name] for m in changed)
        if inside and any(ln in changed_lines for ln in f.lines()):
            touched.add(f.name)
    return Transplant(text, names, touched)
