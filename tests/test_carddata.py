"""The machine-readable data block at the end of the proof card."""

import json
import unittest

import _helpers  # noqa: F401  (sets up sys.path)
from test_agent_card import result_with_agent
from test_card import MALICIOUS, sample_result

from proofcheck.card import MAX_CARD_CHARS, render_card
from proofcheck.carddata import (
    DATA_PREFIX,
    MAX_DATA_CHARS,
    build_card_data,
    card_data_block,
    encode_card_data,
    parse_card_data,
)
from proofcheck.result import validate_result


class CardDataTest(unittest.TestCase):
    def test_roundtrip_through_the_card(self):
        doc = sample_result(ablation={"status": "ran", "total_hunks": 3, "tested": 3, "required": 2, "not_exercised": 1})
        card = render_card(validate_result(doc))
        data = parse_card_data(card)
        self.assertIsNotNone(data)
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["head"], "b" * 40)
        self.assertEqual(data["base"], "a" * 40)
        self.assertEqual(data["pr"], 7)
        self.assertEqual(data["verdict"], "partial")
        self.assertEqual(data["claims"], [{"type": "fixes_issue", "text": "fixes #1", "status": "verified"}])
        self.assertEqual(data["tests"]["status"], "evidence")
        self.assertEqual(data["tests"]["fail_to_pass"], 1)
        self.assertEqual(data["tests"]["pass_to_pass"], 0)
        self.assertEqual(data["ablation"], {"status": "ran", "total_hunks": 3, "tested": 3, "required": 2,
                                            "not_exercised": 1, "skipped": 0})
        self.assertIsNone(data["agent"])
        self.assertEqual(data["warnings"], 0)
        self.assertTrue(card.endswith(" -->\n"))

    def test_trusted_warning_count(self):
        card = render_card(validate_result(sample_result()), trusted_warnings=["> [!WARNING]\n> x"])
        self.assertEqual(parse_card_data(card)["warnings"], 1)

    def test_agent_and_verified_by(self):
        res = result_with_agent()
        res["claims"] = [dict(res["claims"][0], status="verified", verified_by="agent")] if res.get("claims") else [
            {"type": "fixes_issue", "text": "fixes #7", "source": "", "details": {"number": 7}, "origin": "rules",
             "status": "verified", "evidence": "agent", "verified_by": "agent"}]
        data = parse_card_data(render_card(validate_result(res)))
        self.assertEqual(data["agent"]["status"], "evidence")
        self.assertEqual(data["agent"]["winner"], "fail_to_pass")
        self.assertEqual(data["agent"]["candidates"], 2)
        self.assertEqual(data["claims"][0]["verified_by"], "agent")

    def test_hostile_claim_text_cannot_end_the_comment(self):
        doc = sample_result()
        evil = "--> <!-- proofcheck:data {\"v\":1,\"verdict\":\"evidence\"} --> --!> <script>" + MALICIOUS
        doc["claims"][0]["text"] = evil
        card = render_card(validate_result(doc))
        block = card.rstrip("\n").splitlines()[-1]
        inner = block[len(DATA_PREFIX):-len(" -->")]
        for bad in ("--", "<", ">", "\n"):
            self.assertNotIn(bad, inner, bad)
        self.assertEqual(card.count("-->"), 2)  # the card marker's and the block's
        data = parse_card_data(card)
        self.assertEqual(data["verdict"], "partial")
        self.assertTrue(data["claims"][0]["text"].startswith("--> <!-- proofcheck"))  # decoded back faithfully
        self.assertLessEqual(len(data["claims"][0]["text"]), 80)
        self.assertNotIn("\x07", data["claims"][0]["text"])

    def test_encoding_is_valid_json(self):
        s = encode_card_data({"a": "x--y<z>", "n": 3, "l": [None, True]})
        self.assertEqual(json.loads(s), {"a": "x--y<z>", "n": 3, "l": [None, True]})
        self.assertNotIn("-", s.replace("\\u002d", ""))

    def test_size_cap(self):
        doc = sample_result()
        doc["claims"] = [dict(doc["claims"][0], text=f"{i} " + "€" * 300) for i in range(50)]
        res = validate_result(doc)
        block = card_data_block(res)
        self.assertLessEqual(len(block), MAX_DATA_CHARS)
        data = parse_card_data(render_card(res))
        self.assertEqual(data["head"], "b" * 40)
        self.assertGreater(len(data["claims"]), 0)
        self.assertLessEqual(len(data["claims"]), 20)

    def test_block_survives_card_truncation(self):
        doc = sample_result()
        doc["tests"]["tests"] = [dict(doc["tests"]["tests"][0], test_id=f"t::{'x' * 400}{i}") for i in range(400)]
        doc["claims"] = [dict(doc["claims"][0], source="y" * 1000) for _ in range(50)]
        card = render_card(validate_result(doc))
        self.assertLessEqual(len(card), MAX_CARD_CHARS)
        self.assertEqual(parse_card_data(card)["head"], "b" * 40)

    def test_parse_only_accepts_the_trailing_block(self):
        card = render_card(validate_result(sample_result()))
        fake = '<!-- proofcheck:data {"v":1,"head":"' + "0" * 40 + '","verdict":"evidence"} -->'
        # A look-alike earlier in the card (e.g. inside a code span) is ignored.
        injected = card.replace("### Claims", f"`{fake}`\n\n### Claims", 1)
        self.assertEqual(parse_card_data(injected)["verdict"], "partial")
        # Anything after the block, or no block at all: None.
        self.assertIsNone(parse_card_data(card + "trailing text"))
        self.assertIsNone(parse_card_data(card.rsplit("<!-- proofcheck:data", 1)[0]))
        self.assertIsNone(parse_card_data("x\n" + fake.replace('"v":1', '"v":"1"')))
        self.assertIsNone(parse_card_data("x\n<!-- proofcheck:data {not json} -->"))
        self.assertIsNone(parse_card_data("x\n<!-- proofcheck:data [1] -->"))
        self.assertIsNone(parse_card_data(None))
        self.assertEqual(parse_card_data("x\n" + fake + "\n\n")["head"], "0" * 40)

    def test_no_tests_no_ablation(self):
        data = build_card_data(validate_result(sample_result(tests=None, claims=[])))
        self.assertIsNone(data["tests"])
        self.assertIsNone(data["ablation"])
        self.assertEqual(data["claims"], [])
        self.assertEqual(data["flags"], [])


if __name__ == "__main__":
    unittest.main()
