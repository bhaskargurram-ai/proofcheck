import unittest

import _helpers  # noqa: F401  (sets up sys.path)

from proofcheck import claims as C


def types(text: str, title: str = "") -> list[str]:
    return [c.type for c in C.extract_claims(title, text)]


class IssueReferenceTest(unittest.TestCase):
    def test_keywords_and_forms(self):
        cs = C.extract_claims("", "Fixes #123 and closes owner/repo#9. Resolved #7")
        got = [(c.details["number"], c.details["repo"]) for c in cs if c.type == C.FIXES_ISSUE]
        self.assertEqual(got, [(123, None), (9, "owner/repo"), (7, None)])

    def test_issue_url(self):
        (c,) = C.extract_claims("", "Resolves https://github.com/acme/widgets/issues/42")
        self.assertEqual((c.details["number"], c.details["repo"]), (42, "acme/widgets"))

    def test_comma_list_with_colon(self):
        nums = [c.details["number"] for c in C.extract_claims("", "Fix: #5, #6 and #8")]
        self.assertEqual(nums, [5, 6, 8])

    def test_title_is_scanned_and_source_recorded(self):
        (c,) = C.extract_claims("Closes #3: handle empty input", "")
        self.assertEqual(c.details["number"], 3)
        self.assertIn("handle empty input", c.source)

    def test_negatives(self):
        for text in ("see #12", "related to #4", "prefix#12 fixed", "I fixed the bug in parser", "fixes the #1 problem? no: fixes number one",
                     "Fixes #0"):
            self.assertNotIn(C.FIXES_ISSUE, types(text), text)

    def test_negated_references_are_not_claims(self):
        for text in ("This does not fix #3; it only renames variables.", "Note: it doesn't close #9 yet.",
                     "It doesn’t resolve #2 (curly apostrophe).",
                     "This PR no longer fixes #4, see the follow-up."):
            self.assertNotIn(C.FIXES_ISSUE, types(text), text)
        nums = [c.details["number"] for c in C.extract_claims("", "Fixes #5. It does not fix #6.")]
        self.assertEqual(nums, [5])

    def test_code_blocks_comments_and_quotes_are_ignored(self):
        body = "Some change.\n\n```\nfixes #1\n```\n<!-- closes #2 -->\n> resolves #3\nUse `fixes #4` syntax."
        self.assertEqual(types(body), [])

    def test_dedupe_and_referenced_numbers(self):
        cs = C.extract_claims("Fixes #5", "This fixes #5. Also closes other/repo#6 and closes me/proj#7.")
        self.assertEqual(len([c for c in cs if c.type == C.FIXES_ISSUE]), 3)
        self.assertEqual(C.referenced_issue_numbers(cs, "me/proj"), [5, 7])
        self.assertEqual(C.referenced_issue_numbers(cs, None), [5])


class PerformanceClaimTest(unittest.TestCase):
    def perf(self, text: str) -> list[C.Claim]:
        return [c for c in C.extract_claims("", text) if c.type == C.PERFORMANCE]

    def test_factor_forms(self):
        cases = {
            "This makes parsing 3x faster.": 3.0,
            "A 2.5× speedup on large inputs.": 2.5,
            "Now 10 times quicker!": 10.0,
            "Gives a speedup of ~4x on CI.": 4.0,
            "Decoding is faster by 1,5x.": 1.5,
        }
        for text, factor in cases.items():
            (c,) = self.perf(text)
            self.assertAlmostEqual(c.details["factor"], factor, msg=text)
            self.assertEqual(c.details["metric"], "time")

    def test_percent_forms(self):
        (c,) = self.perf("Startup is 40% faster.")
        self.assertAlmostEqual(c.details["factor"], 1.4)
        (c,) = self.perf("This reduces runtime by 50%.")
        self.assertAlmostEqual(c.details["factor"], 2.0)
        (c,) = self.perf("It reduces memory by 30%.")
        self.assertEqual(c.details["metric"], "memory")
        self.assertAlmostEqual(c.details["factor"], 1 / 0.7)

    def test_from_to(self):
        (c,) = self.perf("The full suite goes from 162 s to 0.22 s.")
        self.assertAlmostEqual(c.details["factor"], 162 / 0.22)
        (c,) = self.perf("Peak RSS drops from 2 GB to 500 MB.")
        self.assertEqual(c.details["metric"], "memory")
        self.assertAlmostEqual(c.details["factor"], 4.0)
        (c,) = self.perf("Import time went from 1.2 s to 300 ms")
        self.assertAlmostEqual(c.details["factor"], 4.0)

    def test_arrow_notation(self):
        (c,) = self.perf("jnp.unique: 15.5 s → 0.28 s")
        self.assertAlmostEqual(c.details["factor"], 15.5 / 0.28)
        (c,) = self.perf("Cold start 162s -> 0.22s on CI.")
        self.assertAlmostEqual(c.details["factor"], 162 / 0.22)
        (c,) = self.perf("Peak memory 3 GB => 1 GB")
        self.assertEqual(c.details["metric"], "memory")
        self.assertEqual(self.perf("Latency 0.2 s -> 1.5 s"), [])  # slower is not a speedup claim

    def test_speeds_up_by(self):
        (c,) = self.perf("This speeds up parsing by 2.5x.")
        self.assertAlmostEqual(c.details["factor"], 2.5)
        (c,) = self.perf("Caching sped up the whole build by ~3x")
        self.assertAlmostEqual(c.details["factor"], 3.0)

    def test_negatives(self):
        for text in (
            "Upgrade to python 3x",
            "We have 3 faster runners now",  # no multiplier
            "Version 2.5 is faster",
            "from 1 s to 5 s",  # slower, not a speedup claim
            "Changes from 2 apples to 3 pears",
            "1x faster",  # no improvement
            "This makes it faster",  # no number to verify
            "reduces runtime by 100%",  # nonsensical
        ):
            self.assertEqual(self.perf(text), [], text)


class PhraseClaimTest(unittest.TestCase):
    def test_no_behavior_change(self):
        for text in ("No functional change.", "Refactor only", "This is a pure refactor.", "This does not change behavior.",
                     "No behaviour change intended", "Behavior is unchanged."):
            self.assertIn(C.NO_BEHAVIOR_CHANGE, types(text), text)

    def test_no_behavior_change_negatives(self):
        for text in ("This is not a pure refactor", "This changes behavior for empty input", "refactoring is planned later"):
            self.assertNotIn(C.NO_BEHAVIOR_CHANGE, types(text), text)

    def test_adds_tests(self):
        for text in ("Adds tests.", "Added a regression test for the crash", "Includes unit tests", "with new tests", "I wrote tests for it"):
            self.assertIn(C.ADDS_TESTS, types(text), text)

    def test_adds_tests_negatives(self):
        for text in ("No tests added.", "I did not add tests", "Tests are not included", "Without tests for now", "the test suite is slow"):
            self.assertNotIn(C.ADDS_TESTS, types(text), text)

    def test_contracted_negation(self):
        self.assertNotIn(C.BACKWARDS_COMPATIBLE, types("This isn't backwards compatible."))
        self.assertNotIn(C.NO_BEHAVIOR_CHANGE, types("It wasn't a pure refactor."))

    def test_backwards_compatible(self):
        for text in ("This is backwards compatible.", "Fully backward-compatible", "No breaking changes."):
            self.assertIn(C.BACKWARDS_COMPATIBLE, types(text), text)
        for text in ("This is not backwards compatible", "This is a breaking change"):
            self.assertNotIn(C.BACKWARDS_COMPATIBLE, types(text), text)

    def test_sentence_splitting_keeps_soft_wrapped_lines_together(self):
        body = "Fixes #1. `word_count` now uses `str.split()` without an argument,\nso tabs are handled.\n\n- Adds a test\n- 2x faster"
        sentences = C.split_sentences(C.strip_non_prose(body))
        self.assertIn("so tabs are handled.", sentences[1])
        self.assertEqual(sorted(types(body)), sorted([C.FIXES_ISSUE, C.ADDS_TESTS, C.PERFORMANCE]))


if __name__ == "__main__":
    unittest.main()
