import copy
import re
import unittest

import _helpers  # noqa: F401

from proofcheck.card import CARD_MARKER, MAX_CARD_CHARS, ZWJ, code, escape_md, render_card
from proofcheck.result import ResultValidationError, new_result, validate_result

SHA = "a" * 40


def sample_result(**overrides):
    doc = new_result(
        repo="acme/widgets",
        pr_number=7,
        base=SHA,
        head="b" * 40,
        merge_base=SHA,
        verdict="partial",
        claims=[{"type": "fixes_issue", "text": "fixes #1", "source": "Fixes #1.", "details": {"number": 1},
                 "origin": "rules", "status": "verified", "evidence": "1 test fail on base and pass on head"}],
        tests={"status": "evidence", "runner": "pytest", "test_files": ["tests/test_x.py"], "test_modules": ["tests/test_x.py"],
               "counts": {"fail_to_pass": 1}, "notes": [],
               "tests": [{"test_id": "tests.test_x::test_a", "file": "tests/test_x.py", "classification": "fail_to_pass",
                          "base": "failed", "head": "passed", "touched": True, "message": ""}]},
        repro=[], benchmark=None,
        scope={"files": [{"path": "x.py", "status": "M", "additions": 1, "deletions": 1, "binary": False}], "file_count": 1,
               "total_additions": 1, "total_deletions": 1, "flags": []},
        notes=[], config_source="defaults",
    )
    doc.update(overrides)
    return doc


MALICIOUS = ('<img src=x onerror=alert(1)> @octocat please review | col | <!-- proofcheck:card --> '
             '[click](https://evil.example) ![x](http://evil/img.png) <script>bad()</script> fixes acme/other#5 '
             'javascript:alert(1) ‮gnp.exe\x07')


class EscapeTest(unittest.TestCase):
    def test_escape_md_neutralizes_html_mentions_pipes_links(self):
        s = escape_md(MALICIOUS, 1000)
        self.assertNotIn("<img", s)
        self.assertNotIn("<script", s)
        self.assertNotIn("<!--", s)
        self.assertNotIn("@octocat", s)
        self.assertIn("@" + ZWJ + "octocat", s)
        self.assertNotRegex(s, r"(?<!\\)\|")  # every pipe escaped
        self.assertNotIn("](", s.replace("\\](", ""))  # no live link syntax
        self.assertNotIn("https://", s)
        self.assertNotIn("javascript:", s)
        self.assertNotIn("‮", s)
        self.assertNotIn("\x07", s)
        self.assertNotRegex(s, r"other#5")  # cross-repo reference defused

    def test_truncation(self):
        s = escape_md("x" * 1000, 50)
        self.assertLessEqual(len(s), 50)
        self.assertTrue(s.endswith("…"))

    def test_code_span(self):
        self.assertEqual(code("a|b`c"), "`a\\|b'c`")
        self.assertEqual(code(""), "` `")


class RenderCardTest(unittest.TestCase):
    def test_card_structure(self):
        card = render_card(validate_result(sample_result()))
        self.assertTrue(card.startswith(CARD_MARKER + "\n"))
        self.assertIn("| ✅ verified |", card)
        self.assertIn("✅ fail → pass", card)
        self.assertIn("evidence, not authorship", card)
        self.assertIn("aaaaaaa", card)

    def test_malicious_pr_text_is_neutralized(self):
        doc = sample_result()
        doc["claims"][0]["text"] = MALICIOUS
        doc["claims"][0]["source"] = MALICIOUS + "\n| injected | row |"
        doc["claims"][0]["evidence"] = "@everyone " + MALICIOUS
        doc["tests"]["tests"][0]["test_id"] = "tests.test_x::test_`a|b`<b>@admin"
        doc["notes"] = ["<details open>@team-leads</details>"]
        card = render_card(validate_result(doc))
        self.assertEqual(card.count(CARD_MARKER), 1)
        self.assertEqual(card.count("<!--"), 2)  # the marker + the trailing data block
        self.assertTrue(card.rstrip("\n").splitlines()[-1].startswith("<!-- proofcheck:data {"))
        for bad in ("<img", "<script", "<details", "@octocat", "@everyone", "@team-leads", "](https://", "![x]"):
            self.assertNotIn(bad, card, bad)
        # every table row of the claims table still has exactly 3 cells
        for line in card.splitlines():
            if line.startswith("| ✅ verified"):
                cells = re.split(r"(?<!\\)\|", line)
                self.assertEqual(len(cells), 5, line)  # leading/trailing empty + 3 cells
        # the backtick-containing test id stays inside a single code span
        self.assertIn("`tests.test_x::test_'a\\|b'<b>@admin`", card)

    def test_size_cap(self):
        doc = sample_result()
        doc["tests"]["tests"] = [dict(doc["tests"]["tests"][0], test_id=f"t::{'x' * 400}{i}") for i in range(400)]
        doc["claims"] = [dict(doc["claims"][0], source="y" * 1000) for _ in range(50)]
        card = render_card(validate_result(doc))
        self.assertLessEqual(len(card), MAX_CARD_CHARS)


class ResultValidationTest(unittest.TestCase):
    def test_valid_roundtrip(self):
        v = validate_result(sample_result())
        self.assertEqual(validate_result(copy.deepcopy(v)), v)

    def test_rejects_bad_documents(self):
        bad_cases = [
            dict(sample_result(), schema="other"),
            dict(sample_result(), verdict="totally-fine"),
            dict(sample_result(), pr_number="7; rm -rf /"),
            dict(sample_result(), pr_number=True),
            dict(sample_result(), base="not-a-sha"),
            dict(sample_result(), repo="../../etc"),
            dict(sample_result(), claims=[{"type": "pwn", "status": "verified"}]),
            dict(sample_result(), claims="nope"),
        ]
        for doc in bad_cases:
            with self.assertRaises(ResultValidationError):
                validate_result(doc)
        with self.assertRaises(ResultValidationError):
            validate_result([1, 2])

    def test_unknown_keys_dropped_and_strings_capped(self):
        doc = sample_result(evil="x", notes=["n" * 5000])
        v = validate_result(doc)
        self.assertNotIn("evil", v)
        self.assertEqual(len(v["notes"][0]), 1000)


if __name__ == "__main__":
    unittest.main()
