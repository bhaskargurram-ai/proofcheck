import json
import os
import tempfile
import unittest

import _helpers  # noqa: F401

from proofcheck.card import CARD_MARKER
from proofcheck.github_api import GitHubClient, GitHubError, find_card_comment, pr_number_from_event, validate_number
from proofcheck.httpclient import HTTPResponse

BOT = {"login": "github-actions[bot]", "type": "Bot"}
HUMAN = {"login": "mallory", "type": "User"}


class FakeGitHub:
    """In-memory stand-in for the GitHub comments API."""

    def __init__(self, comments):
        self.comments = comments
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, json.loads(body) if body else None))
        assert headers["Authorization"] == "Bearer tok"
        if method == "GET" and "/comments" in url:
            page = int(url.rsplit("&page=", 1)[1])
            chunk = self.comments[(page - 1) * 100: page * 100]
            return HTTPResponse(200, json.dumps(chunk).encode())
        if method == "PATCH":
            return HTTPResponse(200, b"{}")
        if method == "POST":
            return HTTPResponse(201, json.dumps({"id": 999}).encode())
        return HTTPResponse(404, b"{}")


def card(text="card"):
    return f"{CARD_MARKER}\n{text}"


class UpsertTest(unittest.TestCase):
    def client(self, fake):
        return GitHubClient(token="tok", repo="acme/widgets", transport=fake)

    def test_creates_when_absent(self):
        fake = FakeGitHub([{"id": 1, "body": "nice PR", "user": HUMAN}])
        self.assertEqual(self.client(fake).upsert_card_comment(5, card()), ("created", 999))
        self.assertEqual(fake.calls[-1][0], "POST")
        self.assertTrue(fake.calls[-1][1].endswith("/repos/acme/widgets/issues/5/comments"))

    def test_updates_existing_bot_card(self):
        fake = FakeGitHub([{"id": 1, "body": card("old"), "user": BOT}, {"id": 2, "body": "lgtm", "user": HUMAN}])
        self.assertEqual(self.client(fake).upsert_card_comment(5, card("new")), ("updated", 1))
        method, url, payload = fake.calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.endswith("/issues/comments/1"))
        self.assertEqual(payload["body"], card("new"))

    def test_ignores_marker_planted_by_human(self):
        fake = FakeGitHub([{"id": 3, "body": card("fake"), "user": HUMAN}])
        self.assertEqual(self.client(fake).upsert_card_comment(5, card()), ("created", 999))

    def test_paginates(self):
        comments = [{"id": i, "body": "x", "user": HUMAN} for i in range(150)] + [{"id": 500, "body": card(), "user": BOT}]
        fake = FakeGitHub(comments)
        self.assertEqual(self.client(fake).upsert_card_comment(5, card()), ("updated", 500))

    def test_bot_login_filter(self):
        other_bot = {"login": "other[bot]", "type": "Bot"}
        comments = [{"id": 1, "body": card(), "user": other_bot}]
        self.assertIsNone(find_card_comment(comments, bot_login="github-actions[bot]"))
        self.assertEqual(find_card_comment(comments)["id"], 1)

    def test_refuses_card_without_marker_and_bad_input(self):
        fake = FakeGitHub([])
        with self.assertRaises(GitHubError):
            self.client(fake).upsert_card_comment(5, "no marker")
        with self.assertRaises(GitHubError):
            GitHubClient(token="t", repo="acme/../x", transport=fake)
        for bad in ("5/../../x", "-1", "0", 0, True, "1e3", None):
            with self.assertRaises(GitHubError, msg=repr(bad)):
                validate_number(bad)
        self.assertEqual(validate_number(" 42 "), 42)

    def test_pr_number_from_event(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"pull_request": {"number": 17}}, fh)
        try:
            self.assertEqual(pr_number_from_event(fh.name), 17)
        finally:
            os.unlink(fh.name)
        self.assertIsNone(pr_number_from_event("/nonexistent/event.json"))


if __name__ == "__main__":
    unittest.main()
