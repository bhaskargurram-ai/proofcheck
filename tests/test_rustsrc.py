"""The conservative Rust lexer: brace matching through nasty literals, test modules, transplants."""

import unittest

import _helpers  # noqa: F401

from proofcheck.rustsrc import (
    RustLexError,
    analyze,
    match_brackets,
    tokenize,
    touched_test_functions,
    transplant_test_modules,
)


def braces(src):
    return [t.text for t in tokenize(src) if t.kind == "punct" and t.text in "{}"]


class LexerTest(unittest.TestCase):
    def assertBalanced(self, src, n_pairs):
        toks = tokenize(src)
        pairs = match_brackets(toks)
        self.assertEqual(sum(1 for k, v in pairs.items() if toks[k].text == "{" and k < v), n_pairs)

    def test_raw_strings_with_braces_and_quotes(self):
        src = 'fn f() { let a = r#"{ "}" }"#; let b = r##"x "# { y"##; let c = br"}"; let d = cr#"{"#; let e = r"\\"; }'
        self.assertBalanced(src, 1)
        lits = [t.text for t in tokenize(src) if t.kind == "literal"]
        self.assertIn('r#"{ "}" }"#', lits)
        self.assertIn('r##"x "# { y"##', lits)
        self.assertIn('r"\\"', lits)  # raw strings have no escapes: the backslash does not escape the quote

    def test_nested_block_comments(self):
        src = "/* outer /* inner { */ still a comment } */ fn f() { /* { */ }\n/** doc { */ fn g() {}"
        self.assertEqual(braces(src), ["{", "}", "{", "}"])
        with self.assertRaises(RustLexError):
            tokenize("/* /* only one close */ fn f() {}")

    def test_lifetimes_versus_char_literals(self):
        src = ("fn f<'a, 'b: 'a>(x: &'a str) -> char { let _s: &'static str = \"\"; "
               "let c = '{'; let q = '\\''; let bs = '\\\\'; let u = '\\u{7B}'; let b = b'}'; "
               "'outer: loop { break 'outer; } c }")
        self.assertBalanced(src, 2)
        kinds = {t.text: t.kind for t in tokenize(src)}
        self.assertEqual(kinds["'a"], "lifetime")
        self.assertEqual(kinds["'static"], "lifetime")
        self.assertEqual(kinds["'outer"], "lifetime")
        self.assertEqual(kinds["'{'"], "literal")
        self.assertEqual(kinds["b'}'"], "literal")
        self.assertEqual(kinds["'\\u{7B}'"], "literal")

    def test_braces_and_escapes_in_strings(self):
        src = 'fn f() { let a = "{"; let b = "\\"{"; let c = "\\\\"; let d = b"}"; let e = c"{"; let f = "multi\nline }"; }'
        self.assertBalanced(src, 1)

    def test_comments_with_quotes_and_braces(self):
        src = "// it's a { comment\n/// doc with \" and }\n//! inner doc '\nfn f() {}\n"
        self.assertEqual(braces(src), ["{", "}"])

    def test_raw_identifiers_and_numbers(self):
        toks = tokenize("let r#type = 1.5e3; let r#match = 0x1F_u8; let x = 1..2; let t = (1, 2).0;")
        idents = [t.text for t in toks if t.kind == "ident"]
        self.assertIn("type", idents)
        self.assertIn("match", idents)

    def test_shebang_and_unicode(self):
        src = "#!/usr/bin/env run-cargo-script\nfn ünïcode() -> char { 'é' }\n"
        self.assertBalanced(src, 1)
        self.assertBalanced("#![allow(dead_code)]\nfn f() {}", 1)

    def test_ambiguous_inputs_raise(self):
        for bad in ["let c = 'ab';", 'let s = "unterminated;', "let s = r#\"no end\";", "fn f() { {",
                    "fn f() }", "fn f() { ( }", "let c = '\\u{1F600", "let c = ''"]:
            with self.subTest(bad=bad), self.assertRaises(RustLexError):
                match_brackets(tokenize(bad))


class AnalyzeTest(unittest.TestCase):
    SRC = """use std::fmt;

pub fn add(a: i32, b: i32) -> i32 { a + b }

#[cfg(not(test))]
mod not_tests { #[test] fn hidden() {} }

/// Docs.
#[cfg(test)]
#[allow(dead_code)]
pub(crate) mod tests {
    use super::*;

    #[test]
    fn plain() { assert_eq!(add(1, 1), 2); }

    #[tokio::test]
    async fn asynchronous() { let _ = "}"; }

    #[rstest]
    #[case(1)]
    fn cased(#[case] x: i32) { let _ = x; }

    #[test]
    #[should_panic(expected = "{")]
    fn panics() { panic!("{") }

    fn helper() {}

    proptest! {
        #[test]
        fn prop(x in 0..10u8) { let _ = x; }
    }
}

mod inner {
    #[cfg(all(test, unix))]
    mod tests {
        #[test]
        fn unix_only() {}
    }
}
"""

    def test_finds_test_modules_and_functions(self):
        layout = analyze(self.SRC)
        self.assertEqual([m.qualname for m in layout.test_modules], ["tests", "inner::tests"])
        tests_mod = layout.test_modules[0]
        self.assertEqual(self.SRC.splitlines()[tests_mod.start_line - 1], "#[cfg(test)]")
        self.assertEqual(self.SRC[tests_mod.end - 1], "}")
        names = [f.name for f in layout.test_functions]
        # "hidden" is in a #[cfg(not(test))] module but still a #[test] fn; the helper is not a test.
        self.assertEqual(sorted(names), ["asynchronous", "cased", "hidden", "panics", "plain", "prop", "unix_only"])
        self.assertNotIn("helper", names)

    def test_touched_functions(self):
        lines = self.SRC.splitlines()
        panics_line = next(i for i, l in enumerate(lines, 1) if "fn panics" in l)
        attr_line = next(i for i, l in enumerate(lines, 1) if "should_panic" in l)
        self.assertEqual(touched_test_functions(self.SRC, {panics_line}), {"panics"})
        self.assertEqual(touched_test_functions(self.SRC, {attr_line}), {"panics"})  # attributes count
        self.assertEqual(touched_test_functions(self.SRC, {1}), set())
        self.assertEqual(len(touched_test_functions(self.SRC, None)), 7)


BASE = """pub fn parse(s: &str) -> Option<i32> {
    s.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain() {
        assert_eq!(parse("1"), Some(1));
    }
}

pub fn tail() {}
"""

HEAD = BASE.replace("    s.parse().ok()", "    s.trim().parse().ok()").replace(
    "        assert_eq!(parse(\"1\"), Some(1));\n    }\n",
    "        assert_eq!(parse(\"1\"), Some(1));\n    }\n\n    #[test]\n    fn padded() {\n        assert_eq!(parse(\" 2 \"), Some(2));\n    }\n",
)


def changed_lines(old, new):
    import difflib

    out = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old.splitlines(), new.splitlines()).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


class TransplantTest(unittest.TestCase):
    def test_replaces_the_test_module_but_not_the_fix(self):
        tp = transplant_test_modules(BASE, HEAD, changed_lines(BASE, HEAD))
        self.assertEqual(tp.modules, ["tests"])
        self.assertEqual(tp.touched, {"padded"})
        self.assertIn("fn padded()", tp.text)
        self.assertIn("    s.parse().ok()", tp.text)  # the base code is kept
        self.assertNotIn("trim()", tp.text)
        self.assertTrue(tp.text.endswith("pub fn tail() {}\n"))

    def test_appends_a_module_missing_on_base(self):
        base = "pub fn f() -> u8 { 1 }\n"
        head = base + "\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn one() { assert_eq!(super::f(), 1); }\n}\n"
        tp = transplant_test_modules(base, head, changed_lines(base, head))
        self.assertTrue(tp.text.startswith(base))
        self.assertIn("mod tests {", tp.text)
        self.assertEqual(tp.touched, {"one"})

    def test_code_only_change_is_not_a_transplant(self):
        head = BASE.replace("s.parse().ok()", "s.trim().parse().ok()")
        self.assertIsNone(transplant_test_modules(BASE, head, changed_lines(BASE, head)))

    def test_ambiguous_layouts_raise(self):
        twice = HEAD.replace("#[cfg(test)]\nmod tests {", "#[cfg(all(test, unix))]\nmod tests {") + (
            "\n#[cfg(all(test, not(unix)))]\nmod tests {\n    #[test]\n    fn other() {}\n}\n")
        with self.assertRaises(RustLexError):
            transplant_test_modules(BASE, twice, changed_lines(BASE, twice))
        nested_new = BASE + "\nmod inner {\n    #[cfg(test)]\n    mod tests {\n        #[test]\n        fn x() {}\n    }\n}\n"
        with self.assertRaises(RustLexError):
            transplant_test_modules(BASE, nested_new, changed_lines(BASE, nested_new))
        broken_base = BASE.replace("pub fn tail() {}", "pub fn tail() { let c = 'ab'; }")
        with self.assertRaises(RustLexError):
            transplant_test_modules(broken_base, HEAD, changed_lines(BASE, HEAD))


if __name__ == "__main__":
    unittest.main()
