"""Std-only Rust fixture workspaces for the cargo runner tests (no crates.io access needed)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from _helpers import GitRepo

CARGO_BIN = Path.home() / ".cargo" / "bin"


def cargo_path() -> str | None:
    """PATH value under which ``cargo`` is found, or None."""
    path = os.environ.get("PATH", "")
    if shutil.which("cargo", path=path):
        return path
    if (CARGO_BIN / "cargo").exists():
        return f"{CARGO_BIN}{os.pathsep}{path}"
    return None


#: One shared target directory for the whole test session: workspace crates
#: still rebuild per worktree path, but the toolchain's std artifacts and any
#: unchanged unit are reused, which keeps the suite fast.
SHARED_TARGET = Path(tempfile.gettempdir()) / "proofcheck-test-cargo-target"


def cargo_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CARGO_TARGET_DIR")}
    env["PATH"] = cargo_path() or env.get("PATH", "")
    env["CARGO_NET_OFFLINE"] = "true"
    env["CARGO_INCREMENTAL"] = "0"
    return env


WORKSPACE_TOML = """[workspace]
resolver = "2"
members = ["crates/mathx", "crates/textx"]
"""


def crate_toml(name: str) -> str:
    return f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'


MATHX_LIB = """//! Tiny arithmetic helpers.

pub mod parse;

/// Adds two numbers.
pub fn add(a: i64, b: i64) -> i64 {
    a - b
}

/// Multiplies two numbers.
pub fn mul(a: i64, b: i64) -> i64 {
    a * b
}
"""

MATHX_LIB_FIXED = MATHX_LIB.replace("    a - b\n", "    a + b\n")

PARSE_RS = """//! Parsing with tricky literals: '{', "}", r#"{"#, /* { */ and lifetimes.

pub fn parse_num<'a>(s: &'a str) -> Option<i64> {
    let brace = '{';
    let _ = (brace, "}", r#"{"#);
    s.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_plain_number() {
        assert_eq!(parse_num("42"), Some(42));
    }
}
"""

# The fix: ignore surrounding whitespace. The PR also adds an inline test for it.
PARSE_RS_FIXED = PARSE_RS.replace(
    "    s.parse().ok()\n",
    "    s.trim().parse().ok()\n",
).replace(
    "        assert_eq!(parse_num(\"42\"), Some(42));\n    }\n",
    "        assert_eq!(parse_num(\"42\"), Some(42));\n    }\n\n"
    "    #[test]\n    fn parses_padded() {\n        assert_eq!(parse_num(\" 7 \"), Some(7));\n    }\n",
)

ARITH_TESTS = """use mathx::mul;

#[test]
fn multiplies() {
    assert_eq!(mul(2, 3), 6);
}
"""

ARITH_TESTS_WITH_ADD = ARITH_TESTS.replace("use mathx::mul;", "use mathx::{add, mul};") + """
#[test]
fn adds() {
    assert_eq!(add(2, 3), 5);
}
"""

TEXTX_LIB = """pub fn shout(s: &str) -> String {
    format!("{}!", s.to_uppercase())
}

#[cfg(test)]
mod tests {
    #[test]
    fn shouts() {
        assert_eq!(super::shout("hi"), "HI!");
    }
}
"""

TEXTX_MAIN = """fn main() {
    let arg = std::env::args().nth(1).unwrap_or_default();
    println!("{}", textx::shout(&arg));
}
"""

TEXTX_CLI_TEST = """use std::process::Command;

#[test]
fn binary_shouts() {
    let out = Command::new(env!("CARGO_BIN_EXE_textx")).arg("hey").output().unwrap();
    assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "HEY!");
}
"""


def base_files() -> dict[str, str]:
    return {
        "Cargo.toml": WORKSPACE_TOML,
        ".gitignore": "target/\n",
        "crates/mathx/Cargo.toml": crate_toml("mathx"),
        "crates/mathx/src/lib.rs": MATHX_LIB,
        "crates/mathx/src/parse.rs": PARSE_RS,
        "crates/mathx/tests/arith.rs": ARITH_TESTS,
        "crates/textx/Cargo.toml": crate_toml("textx"),
        "crates/textx/src/lib.rs": TEXTX_LIB,
        "crates/textx/src/main.rs": TEXTX_MAIN,
    }


def make_rust_repo() -> GitRepo:
    """main: a 2-crate workspace with a bug in ``mathx::add`` and in ``parse_num``.

    Branches:

    * ``fix``        -- fixes add(), adds an integration test for it (fail->pass) and
                        changes an unrelated line in textx (not exercised);
    * ``passpass``   -- adds an integration test that passes on base too;
    * ``newapi``     -- adds ``mathx::sub`` and a test using it (does not compile on base);
    * ``inline``     -- fixes parse_num and adds an inline test in the same file;
    * ``ambiguous``  -- two ``#[cfg(...test...)] mod tests`` blocks: cannot be separated;
    * ``binfix``     -- the textx binary now trims its argument; tested through CARGO_BIN_EXE;
    * ``broken``     -- adds a test that fails on head too;
    * ``headbuild``  -- adds a test that does not compile on head either.
    """
    r = GitRepo()
    r.commit(base_files(), "base")
    r.branch("fix")
    r.commit({
        "crates/mathx/src/lib.rs": MATHX_LIB_FIXED,
        "crates/mathx/tests/arith.rs": ARITH_TESTS_WITH_ADD,
        "crates/textx/src/lib.rs": TEXTX_LIB.replace('format!("{}!"', 'format!("{}{}", ').replace(
            "s.to_uppercase())", 's.to_uppercase(), "!")'),
    }, "fix add")
    r.branch("passpass", "main")
    r.commit({"crates/mathx/tests/arith.rs": ARITH_TESTS + "\n#[test]\nfn multiplies_by_zero() {\n    assert_eq!(mul(0, 9), 0);\n}\n"},
             "more tests")
    r.branch("newapi", "main")
    r.commit({
        "crates/mathx/src/lib.rs": MATHX_LIB + "\n/// Subtracts.\npub fn sub(a: i64, b: i64) -> i64 {\n    a - b\n}\n",
        "crates/mathx/tests/sub.rs": "use mathx::sub;\n\n#[test]\nfn subtracts() {\n    assert_eq!(sub(5, 3), 2);\n}\n",
    }, "add sub")
    r.branch("inline", "main")
    r.commit({"crates/mathx/src/parse.rs": PARSE_RS_FIXED}, "accept +")
    r.branch("ambiguous", "main")
    amb = PARSE_RS_FIXED.replace("#[cfg(test)]\nmod tests {", "#[cfg(all(test, unix))]\nmod tests {") + (
        "\n#[cfg(all(test, not(unix)))]\nmod tests {\n    #[test]\n    fn other() {}\n}\n")
    r.commit({"crates/mathx/src/parse.rs": amb}, "platform tests")
    r.branch("binfix", "main")
    r.commit({
        "crates/textx/src/main.rs": TEXTX_MAIN.replace("textx::shout(&arg)", "textx::shout(arg.trim())"),
        "crates/textx/tests/cli.rs": TEXTX_CLI_TEST.replace('.arg("hey")', '.arg("  hey ")'),
    }, "trim arg")
    r.branch("broken", "main")
    r.commit({"crates/mathx/tests/arith.rs": ARITH_TESTS_WITH_ADD}, "test only")
    r.branch("headbuild", "main")
    r.commit({"crates/mathx/tests/div.rs": "use mathx::div;\n\n#[test]\nfn divides() {\n    assert_eq!(div(6, 3), 2);\n}\n"},
             "test for a missing fn")
    r.checkout("main")
    return r
