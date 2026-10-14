"""Static validation (and light repair) of agent-written Rust integration tests.

Nothing is compiled or executed here; the checks work on the token stream of
:mod:`proofcheck.rustsrc`, so string literals and comments never trigger them.

Checks:

* the file tokenizes and its brackets balance (:func:`rustsrc.match_brackets`);
* it defines at least one ``#[test]`` (or ``#[tokio::test]``-style) function;
* it reaches the crate under test through its library name
  (``use <lib>::...`` or a ``<lib>::`` path): an integration test is a
  separate crate and only sees the library's public API;
* a denylist of process, network, FFI and build-environment access:
  ``std::process`` / ``Command::new``, ``std::net`` / ``TcpStream`` & co.,
  ``unsafe``, ``extern "C"``, ``include!``-family macros, ``asm!``,
  ``env!``/``option_env!`` (except ``CARGO_MANIFEST_DIR``/``CARGO_PKG_*``) and
  ``std::env::var`` & co. Like the Python denylist this is a lint against
  off-task code, **not a sandbox**.

Repairs (recorded in the manifest):

* ``crate::`` paths become ``<lib>::`` (in an integration test ``crate``
  is the test itself), unless they name an item the file defines;
* a top-level ``use super::...`` becomes ``use <lib>::...``;
* the package name written with hyphens (``use my-crate::x``) or with
  underscores when ``[lib] name`` differs becomes the library name.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import rustsrc
from ..rustsrc import RustLexError, Token
from .static import MAX_CODE_CHARS, MIN_CODE_CHARS, StaticReport

DENY_MACROS = {
    "include": "include!() pulls other files into the test",
    "include_str": "include_str!() reads files at compile time",
    "include_bytes": "include_bytes!() reads files at compile time",
    "asm": "inline assembly is not allowed",
    "global_asm": "inline assembly is not allowed",
    "llvm_asm": "inline assembly is not allowed",
}
ALLOWED_ENV = ("CARGO_MANIFEST_DIR", "CARGO_PKG_", "CARGO_CRATE_NAME")
NET_TYPES = {"TcpStream", "TcpListener", "UdpSocket", "UnixStream", "UnixListener", "UnixDatagram"}
ENV_READS = {"var", "var_os", "vars", "vars_os"}
_ITEM_KEYWORDS = {"mod", "fn", "struct", "enum", "const", "static", "type", "trait", "union", "macro_rules"}


@dataclass
class RustTarget:
    """What the checks need to know about the crate under test."""

    lib: str  # library crate name, e.g. ``my_crate``
    package: str  # package name from Cargo.toml, e.g. ``my-crate``


def _is_punct(toks: list[Token], k: int, text: str) -> bool:
    return 0 <= k < len(toks) and toks[k].kind == "punct" and toks[k].text == text


def _is_ident(toks: list[Token], k: int, text: str | None = None) -> bool:
    return 0 <= k < len(toks) and toks[k].kind == "ident" and (text is None or toks[k].text == text)


def _path_sep(toks: list[Token], k: int) -> bool:
    """``::`` starting at token ``k`` (two adjacent ':' tokens)."""
    return _is_punct(toks, k, ":") and _is_punct(toks, k + 1, ":") and toks[k].end == toks[k + 1].start


def _line(code: str, offset: int) -> int:
    return code.count("\n", 0, offset) + 1


def _local_items(toks: list[Token]) -> set[str]:
    names = set()
    for k, t in enumerate(toks):
        if t.kind == "ident" and t.text in _ITEM_KEYWORDS and _is_ident(toks, k + 1):
            names.add(toks[k + 1].text)
    return names


def _depths(toks: list[Token]) -> list[int]:
    """Brace depth at every token (best effort; brackets are checked separately)."""
    out, d = [], 0
    for t in toks:
        if t.kind == "punct" and t.text == "}":
            d = max(0, d - 1)
        out.append(d)
        if t.kind == "punct" and t.text == "{":
            d += 1
    return out


def _repairs(code: str, toks: list[Token], target: RustTarget) -> tuple[str, list[str]]:
    """Apply the path repairs; returns ``(new_code, descriptions)``."""
    lib, package = target.lib, target.package
    local = _local_items(toks)
    depth = _depths(toks)
    edits: list[tuple[int, int, str]] = []
    notes: list[str] = []
    k = 0
    while k < len(toks):
        t = toks[k]
        prev_sep = k >= 2 and _path_sep(toks, k - 2)
        if t.kind == "ident" and not prev_sep:
            # crate::x -> lib::x (unless x is defined in the test file itself)
            if t.text == "crate" and _path_sep(toks, k + 1) and not _is_ident(toks, k - 1, "in"):
                nxt = toks[k + 3].text if _is_ident(toks, k + 3) else ""
                if nxt not in local:
                    edits.append((t.start, t.end, lib))
                    notes.append(f"crate:: -> {lib}:: (an integration test is its own crate; use the library's name)")
            # top-level `use super::` -> `use lib::`
            elif t.text == "super" and depth[k] == 0 and _is_ident(toks, k - 1, "use") and _path_sep(toks, k + 1):
                edits.append((t.start, t.end, lib))
                notes.append(f"use super:: -> use {lib}:: (an integration test has no parent module)")
            # package-name-with-hyphens::x -> lib::x
            elif "-" in package and t.text == package.split("-", 1)[0]:
                j, parts = k, [t.text]
                while _is_punct(toks, j + 1, "-") and _is_ident(toks, j + 2):
                    parts.append(toks[j + 2].text)
                    j += 2
                if j > k and "-".join(parts) == package and (_path_sep(toks, j + 1) or _is_punct(toks, j + 1, ";")
                                                             or _is_ident(toks, j + 1, "as")):
                    edits.append((t.start, toks[j].end, lib))
                    notes.append(f"{package} -> {lib} (crate names use underscores in Rust paths)")
                    k = j + 1
                    continue
            # package_with_underscores::x when [lib] name differs
            elif t.text == package.replace("-", "_") and t.text != lib and (_path_sep(toks, k + 1) or _is_ident(toks, k - 1, "use")):
                edits.append((t.start, t.end, lib))
                notes.append(f"{t.text} -> {lib} (the library target's name)")
        k += 1
    if not edits:
        return code, []
    for start, end, text in sorted(edits, reverse=True):
        code = code[:start] + text + code[end:]
    seen: list[str] = []
    for n in notes:
        if n not in seen:
            seen.append(n)
    return code, seen


def _attr_name(toks: list[Token]) -> str:
    name = ""
    for t in toks:
        if t.kind == "ident":
            name = t.text
        elif t.text == ":":
            continue
        else:
            break
    return name


def _has_test_attr(toks: list[Token], pairs: dict[int, int]) -> bool:
    for k, t in enumerate(toks):
        if t.kind == "punct" and t.text == "#" and _is_punct(toks, k + 1, "[") and k + 1 in pairs:
            if _attr_name(toks[k + 2:pairs[k + 1]]) == "test":
                return True
    return False


def _use_statements(toks: list[Token]) -> list[list[Token]]:
    out = []
    k = 0
    while k < len(toks):
        if _is_ident(toks, k, "use"):
            j = k + 1
            while j < len(toks) and not _is_punct(toks, j, ";"):
                j += 1
            out.append(toks[k + 1:j])
            k = j
        k += 1
    return out


def _string_value(tok: Token) -> str:
    """Contents of a (raw) string literal token, without quotes; '' for other literals."""
    text = tok.text
    if text.startswith(('"', 'r"', "r#")) and text.count('"') >= 2:
        return text[text.index('"') + 1:text.rindex('"')]
    return ""


def _denylist(code: str, toks: list[Token], target: RustTarget) -> list[str]:
    problems: list[str] = []
    uses = _use_statements(toks)
    crate_command = any(u and u[0].text == target.lib and any(t.text == "Command" for t in u) for u in uses)

    def add(k: int, msg: str) -> None:
        item = f"line {_line(code, toks[k].start)}: {msg}"
        if item not in problems:
            problems.append(item)

    for u in uses:
        idents = [t.text for t in u if t.kind == "ident"]
        if idents and idents[0] in ("std", "core") and ({"process", "net"} & set(idents)):
            bad = "process" if "process" in idents else "net"
            problems.append(f"line {_line(code, u[0].start)}: importing std::{bad} is not allowed in reproduction tests")
        if "env" in idents and ENV_READS & set(idents[idents.index("env") + 1:]):
            problems.append(f"line {_line(code, u[0].start)}: reading environment variables (std::env::var) is not allowed")
    for k, t in enumerate(toks):
        if t.kind == "ident":
            if t.text == "unsafe":
                add(k, "unsafe code is not allowed in reproduction tests")
            elif t.text == "extern" and k + 1 < len(toks) and toks[k + 1].kind == "literal":
                add(k, "extern \"ABI\" blocks and functions (FFI) are not allowed")
            elif t.text in NET_TYPES:
                add(k, f"network access ({t.text}) is not allowed in reproduction tests")
            elif t.text == "std" and _path_sep(toks, k + 1) and _is_ident(toks, k + 3) and toks[k + 3].text in ("process", "net"):
                add(k, f"std::{toks[k + 3].text} is not allowed in reproduction tests")
            elif t.text == "env" and _path_sep(toks, k + 1) and _is_ident(toks, k + 3) and toks[k + 3].text in ENV_READS:
                add(k, f"reading environment variables (env::{toks[k + 3].text}) is not allowed")
            elif t.text == "Command" and _path_sep(toks, k + 1) and _is_ident(toks, k + 3, "new") and not crate_command:
                add(k, "starting processes (Command::new) is not allowed in reproduction tests")
            elif _is_punct(toks, k + 1, "!"):
                name = t.text
                if name in DENY_MACROS:
                    add(k, DENY_MACROS[name])
                elif name in ("env", "option_env"):
                    arg = toks[k + 3] if _is_punct(toks, k + 2, "(") and k + 3 < len(toks) else None
                    value = _string_value(arg) if arg is not None and arg.kind == "literal" else ""
                    if not value.startswith(ALLOWED_ENV):
                        add(k, f"{name}!() reads the build environment (only CARGO_MANIFEST_DIR and CARGO_PKG_* are allowed)")
    return problems


def _references_crate(toks: list[Token], lib: str) -> bool:
    for k, t in enumerate(toks):
        if t.kind == "ident" and t.text == lib:
            if _path_sep(toks, k + 1) or _is_ident(toks, k - 1, "use") or _is_ident(toks, k - 1, "crate"):
                return True
    return False


def validate_rust_test(code: str, target: RustTarget) -> StaticReport:
    """Validate (and possibly repair) an agent-written integration test for ``target``."""
    report = StaticReport(ok=False, code=code)
    if not isinstance(code, str) or len(code.strip()) < MIN_CODE_CHARS:
        report.errors.append("the test file is empty")
        return report
    if len(code) > MAX_CODE_CHARS:
        report.errors.append(f"the test file is too long ({len(code)} characters, limit {MAX_CODE_CHARS})")
        return report
    if "\x00" in code:
        report.errors.append("the test file contains NUL bytes")
        return report
    try:
        toks = rustsrc.tokenize(code)
    except RustLexError as exc:
        report.errors.append(f"cannot tokenize the file: {exc}")
        return report
    repaired, notes = _repairs(code, toks, target)
    if notes:
        try:
            toks = rustsrc.tokenize(repaired)
            code = repaired
            report.code = repaired
            report.repairs.extend(notes)
        except RustLexError:  # pragma: no cover - defensive: never ship a broken repair
            report.errors.append("path repair produced invalid code; fix the paths manually")
            return report
    try:
        pairs = rustsrc.match_brackets(toks)
    except RustLexError as exc:
        report.errors.append(f"unbalanced brackets: {exc}")
        return report
    try:
        layout = rustsrc.analyze(code)
    except RustLexError as exc:
        report.errors.append(f"cannot analyse the file: {exc}")
        return report
    report.test_names = [f.name for f in layout.test_functions]
    if not report.test_names or not _has_test_attr(toks, pairs):
        report.errors.append("no tests found: define at least one `#[test] fn ...()` function")
    if layout.test_modules and not report.test_names:
        report.warnings.append("#[cfg(test)] modules are not needed in an integration test")
    report.errors.extend(_denylist(code, toks, target))
    if not _references_crate(toks, target.lib):
        report.errors.append(f"the test does not use the crate under test: an integration test only sees its public API, "
                             f"through `use {target.lib}::...` (or `{target.lib}::` paths)")
    report.ok = not report.errors
    return report
