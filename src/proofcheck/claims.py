"""Rule-based extraction of verifiable claims from pull request text.

The extractor is deliberately conservative: it only recognises a handful of
claim shapes that proofcheck knows how to check, and it records the sentence
each claim came from so that reviewers can see exactly what was matched.

Claim types:

* ``fixes_issue``         -- "fixes #123", "closes owner/repo#9", issue URLs
* ``performance``         -- "3x faster", "40% faster", "from 162 s to 0.22 s",
                             "reduces memory by 30%"
* ``no_behavior_change``  -- "no functional change", "refactor only"
* ``adds_tests``          -- "adds tests", "includes a regression test"
* ``backwards_compatible``-- "backwards compatible", "no breaking changes"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FIXES_ISSUE = "fixes_issue"
PERFORMANCE = "performance"
NO_BEHAVIOR_CHANGE = "no_behavior_change"
ADDS_TESTS = "adds_tests"
BACKWARDS_COMPATIBLE = "backwards_compatible"

CLAIM_TYPES = (FIXES_ISSUE, PERFORMANCE, NO_BEHAVIOR_CHANGE, ADDS_TESTS, BACKWARDS_COMPATIBLE)

#: Maximum characters of PR text that are analysed (longer text is truncated).
MAX_TEXT = 100_000


@dataclass
class Claim:
    """A single claim made by the pull request author."""

    type: str
    text: str  # short normalized description, e.g. "fixes #12" or "3x faster"
    source: str  # the sentence the claim was found in
    details: dict[str, Any] = field(default_factory=dict)
    origin: str = "rules"  # "rules" or "llm"

    def key(self) -> tuple:
        """Identity used to de-duplicate claims."""
        if self.type == FIXES_ISSUE:
            return (self.type, self.details.get("repo") or "", self.details.get("number"))
        if self.type == PERFORMANCE:
            return (self.type, self.details.get("metric"), round(float(self.details.get("factor") or 0), 2))
        return (self.type,)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "text": self.text, "source": self.source, "details": dict(self.details), "origin": self.origin}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        return cls(
            type=str(data["type"]),
            text=str(data.get("text", "")),
            source=str(data.get("source", "")),
            details=dict(data.get("details") or {}),
            origin=str(data.get("origin", "rules")),
        )


# --------------------------------------------------------------------------- text prep

_FENCE_RE = re.compile(r"(^|\n)(```|~~~)[^\n]*\n.*?(\n\2[^\n]*(?=\n|$)|$)", re.S)
_HTML_COMMENT_RE = re.compile(r"<!--.*?(-->|$)", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = re.compile(r"^\s*>.*$", re.M)


def strip_non_prose(text: str) -> str:
    """Remove code blocks, HTML comments and quoted lines.

    Claims inside code samples, quoted text or unfilled PR templates (HTML
    comments) are not claims made by the author. Inline code is removed later,
    per sentence (see :func:`prose_of`).
    """
    text = text[:MAX_TEXT]
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _FENCE_RE.sub("\n\n", text)
    text = _QUOTE_LINE_RE.sub(" ", text)
    return text


_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n|\n(?=\s*(?:[-*+•]\s|\d+[.)]\s|#{1,6}\s|\|))")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9#*`\-(\[])")


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences.

    Paragraphs, list items and headings are separate blocks; soft-wrapped
    lines inside a paragraph are joined before splitting on sentence
    punctuation.
    """
    out = []
    for block in _BLOCK_SPLIT_RE.split(text):
        block = re.sub(r"\s+", " ", block).strip()
        block = re.sub(r"^(?:#{1,6}|[-*+•]|\d+[.)])\s+", "", block)
        for part in _SENTENCE_SPLIT_RE.split(block):
            s = part.strip(" \t-*•")
            if s:
                out.append(s)
    return out


def prose_of(sentence: str) -> str:
    """The sentence with inline code spans blanked out (used for matching)."""
    return _INLINE_CODE_RE.sub(" ", sentence)


def display_of(sentence: str) -> str:
    """The sentence as shown to reviewers (backticks dropped)."""
    return sentence.replace("`", "")


# --------------------------------------------------------------------------- issue refs

_ISSUE_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_ISSUE_REF = r"(?:(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+|https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+)"
_ISSUE_RE = re.compile(
    rf"\b{_ISSUE_KEYWORD}\b:?\s+(?P<refs>{_ISSUE_REF}(?:\s*(?:,|and|&)\s*(?:{_ISSUE_KEYWORD}\s+)?{_ISSUE_REF})*)",
    re.I,
)
_SINGLE_REF_RE = re.compile(
    r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(?P<num>\d+))|(?:https?://github\.com/(?P<urepo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(?P<unum>\d+))"
)


def _issue_claims(sentence: str) -> list[Claim]:
    claims = []
    for m in _ISSUE_RE.finditer(sentence):
        if _negated(sentence, m.start()):
            continue  # "does not fix #3" is not a claim to fix #3
        keyword = m.group(0).split()[0].rstrip(":").lower()
        for ref in _SINGLE_REF_RE.finditer(m.group("refs")):
            repo = ref.group("repo") or ref.group("urepo") or None
            num = int(ref.group("num") or ref.group("unum"))
            if num <= 0:
                continue
            label = f"{repo}#{num}" if repo else f"#{num}"
            claims.append(
                Claim(FIXES_ISSUE, f"{keyword} {label}", sentence, {"number": num, "repo": repo, "keyword": keyword})
            )
    return claims


# --------------------------------------------------------------------------- performance

_NUM = r"(\d+(?:[.,]\d+)?)"
_X = r"\s*(?:x|×|times)"
_SPEED_WORDS = r"(?:faster|quicker|speed-?ups?|speedier|performance (?:improvement|gain|boost)|improvement in (?:speed|performance|runtime|latency)|throughput)"
_MEM_WORDS = r"(?:less memory|lower memory|smaller memory|memory (?:reduction|savings?|usage reduction)|less ram|fewer allocations)"
_TIME_UNITS = {
    "ns": 1e-9, "nanoseconds": 1e-9,
    "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "microseconds": 1e-6,
    "ms": 1e-3, "msec": 1e-3, "milliseconds": 1e-3,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
}
_MEM_UNITS = {"b": 1, "kb": 1e3, "kib": 1024, "mb": 1e6, "mib": 1024**2, "gb": 1e9, "gib": 1024**3}
_UNIT_RE = r"(ns|nanoseconds|µs|μs|us|microseconds|ms|msec|milliseconds|seconds|second|secs|sec|s|minutes|minute|mins|min|m|hours|hour|hrs|hr|h|gib|gb|mib|mb|kib|kb|b)\b"

_PERF_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # "3x faster", "2.5× speedup", "10 times quicker"
    ("factor_speed", re.compile(rf"(?<![\w.]){_NUM}{_X}\s+(?:an?\s+)?{_SPEED_WORDS}", re.I)),
    # "speedup of 3x", "faster by ~2x", "a 4x speed-up"
    ("factor_speed", re.compile(rf"(?:speed-?up|faster)\s+(?:of|by)?\s*(?:~|about\s+|roughly\s+|around\s+|up to\s+)?{_NUM}{_X}(?![\w])", re.I)),
    # "speeds up parsing by 2.5x", "sped up the build by ~3x"
    ("factor_speed", re.compile(rf"\b(?:speeds?|sped)[- ]up\s+(?:[\w.-]+\s+){{0,3}}?by\s+(?:~|about\s+|roughly\s+|around\s+|up to\s+)?{_NUM}{_X}(?![\w])", re.I)),
    # "3x less memory", "2x lower memory"
    ("factor_memory", re.compile(rf"(?<![\w.]){_NUM}{_X}\s+{_MEM_WORDS}", re.I)),
    # "40% faster", "25 % speedup"
    ("percent_speed", re.compile(rf"(?<![\w.]){_NUM}\s*%\s+{_SPEED_WORDS}", re.I)),
    # "reduces runtime by 40%", "cuts latency by 30 %"
    ("percent_time_reduction", re.compile(
        rf"(?:reduc(?:es|ed|ing|e)|cuts?|lowers?|decreas(?:es|ed|e)|improves?|improved)\s+(?:the\s+)?(?:run\s*time|runtime|latency|execution time|wall[- ]?time|build time|time|duration|cpu time)\s+by\s+(?:~|about\s+|roughly\s+)?{_NUM}\s*%",
        re.I)),
    # "reduces memory by 30%", "memory usage reduced by 30%"
    ("percent_memory", re.compile(
        rf"(?:(?:reduc(?:es|ed|ing|e)|cuts?|lowers?|decreas(?:es|ed|e))\s+(?:the\s+|peak\s+)*(?:memory|ram|memory usage|memory footprint)\s+by\s+(?:~|about\s+)?{_NUM}\s*%)|(?:(?:memory|ram)(?:\s+usage|\s+footprint)?\s+(?:is\s+)?(?:reduced|cut|lowered|decreased)\s+by\s+(?:~|about\s+)?{_NUM}\s*%)|(?:(?<![\w.]){_NUM}\s*%\s+(?:less|lower)\s+(?:memory|ram))",
        re.I)),
    # "from 162 s to 0.22 s", "from 3.2 GB to 1 GB"
    ("from_to", re.compile(rf"\bfrom\s+(?:~|about\s+)?{_NUM}\s*{_UNIT_RE}\s+(?:down\s+)?to\s+(?:~|about\s+)?{_NUM}\s*{_UNIT_RE}", re.I)),
    # "15.5 s → 0.28 s", "162s -> 0.22s", "3.2 GB => 1 GB"
    ("from_to", re.compile(rf"(?<![\w.]){_NUM}\s*{_UNIT_RE}\s*(?:→|->|=>|⟶)\s*{_NUM}\s*{_UNIT_RE}", re.I)),
]


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _perf_claims(sentence: str) -> list[Claim]:
    claims = []
    for kind, pattern in _PERF_PATTERNS:
        for m in pattern.finditer(sentence):
            groups = [g for g in m.groups() if g is not None]
            phrase = m.group(0).strip()
            details: dict[str, Any] = {"phrase": phrase}
            try:
                if kind == "factor_speed":
                    factor = _num(groups[0])
                    details.update(metric="time", factor=factor)
                elif kind == "factor_memory":
                    details.update(metric="memory", factor=_num(groups[0]))
                elif kind == "percent_speed":
                    pct = _num(groups[0])
                    # "40% faster" is read as a 1.4x speedup (the conservative reading).
                    details.update(metric="time", percent=pct, factor=1 + pct / 100)
                elif kind == "percent_time_reduction":
                    pct = _num(groups[0])
                    if pct >= 100:
                        continue
                    details.update(metric="time", percent=pct, factor=1 / (1 - pct / 100))
                elif kind == "percent_memory":
                    pct = _num(groups[0])
                    if pct >= 100:
                        continue
                    details.update(metric="memory", percent=pct, factor=1 / (1 - pct / 100))
                elif kind == "from_to":
                    a, ua, b, ub = _num(groups[0]), groups[1].lower(), _num(groups[2]), groups[3].lower()
                    if ua in _TIME_UNITS and ub in _TIME_UNITS:
                        before, after, metric = a * _TIME_UNITS[ua], b * _TIME_UNITS[ub], "time"
                    elif ua in _MEM_UNITS and ub in _MEM_UNITS:
                        before, after, metric = a * _MEM_UNITS[ua], b * _MEM_UNITS[ub], "memory"
                    else:
                        continue
                    if after <= 0 or before <= after:
                        continue
                    details.update(metric=metric, before=a, before_unit=ua, after=b, after_unit=ub, factor=before / after)
            except (ValueError, IndexError, ZeroDivisionError):
                continue
            factor = details.get("factor")
            if not factor or factor <= 1 or factor > 1_000_000:
                continue
            claims.append(Claim(PERFORMANCE, phrase, sentence, details))
    return claims


# --------------------------------------------------------------------------- simple phrase claims

# "n't" sits inside a word ("doesn't"), so it must not require a leading word boundary.
_NEGATION_BEFORE = re.compile(r"(?:\b(?:not|no longer|never|without)|n[’']t)\s+(?:\w+\s+){0,2}$", re.I)

_NO_BEHAVIOR_RE = re.compile(
    r"\b(?:no (?:functional|behaviou?ral|behaviou?r|logic|semantic|user[- ]facing|runtime) changes?"
    r"|(?:pure|purely a|pure a|just a|only a)?\s*refactor(?:ing)?[ -]only"
    r"|(?:this is )?(?:a )?pure(?:ly)? (?:a )?refactor(?:ing)?"
    r"|(?:does not|doesn't|should not|shouldn't|won't|will not) change (?:any )?(?:the )?(?:existing )?behaviou?r"
    r"|behaviou?r is unchanged|no change in behaviou?r|(?:is )?behaviou?r[- ]preserving)\b",
    re.I,
)

_ADDS_TESTS_RE = re.compile(
    r"\b(?:add(?:s|ed|ing)?|includ(?:es|ed|ing|e)|with|writ(?:es|ten|ing)|wrote|cover(?:s|ed)? (?:it |this )?(?:with|by))\s+"
    r"(?:(?:a|an|some|new|more|unit|regression|integration|extra|additional|missing|corresponding|the)\s+){0,3}"
    r"(?:test(?:s|cases?| cases?| coverage)?|unit[- ]tests?|regression[- ]tests?)\b",
    re.I,
)
_NO_TESTS_RE = re.compile(r"\b(?:no|without|didn't add|did not add|haven't added|have not added|not adding)\s+(?:any\s+)?(?:new\s+)?tests?\b", re.I)

_BACKCOMPAT_RE = re.compile(
    r"\b(?:backwards?[- ]compatib(?:le|ility)|fully compatible|no breaking changes?|non[- ]breaking(?: change)?)\b",
    re.I,
)


def _negated(sentence: str, start: int) -> bool:
    return bool(_NEGATION_BEFORE.search(sentence[:start]))


def _phrase_claims(sentence: str) -> list[Claim]:
    claims = []
    m = _NO_BEHAVIOR_RE.search(sentence)
    if m and not _negated(sentence, m.start()):
        claims.append(Claim(NO_BEHAVIOR_CHANGE, m.group(0).strip(), sentence))
    m = _ADDS_TESTS_RE.search(sentence)
    if m and not _NO_TESTS_RE.search(sentence) and not _negated(sentence, m.start()):
        claims.append(Claim(ADDS_TESTS, m.group(0).strip(), sentence))
    m = _BACKCOMPAT_RE.search(sentence)
    if m and not _negated(sentence, m.start()):
        claims.append(Claim(BACKWARDS_COMPATIBLE, m.group(0).strip(), sentence))
    return claims


# --------------------------------------------------------------------------- public API

def extract_claims(title: str, body: str) -> list[Claim]:
    """Extract claims from a PR title and body using deterministic rules."""
    text = f"{title or ''}\n\n{body or ''}"
    claims: list[Claim] = []
    seen: set[tuple] = set()
    for sentence in split_sentences(strip_non_prose(text)):
        prose = prose_of(sentence)
        for claim in _issue_claims(prose) + _perf_claims(prose) + _phrase_claims(prose):
            claim.source = display_of(sentence)
            k = claim.key()
            if k in seen:
                continue
            seen.add(k)
            claims.append(claim)
    return claims


def merge_claims(*groups: list[Claim]) -> list[Claim]:
    """Concatenate claim lists, dropping duplicates (first occurrence wins)."""
    out: list[Claim] = []
    seen: set[tuple] = set()
    for group in groups:
        for c in group:
            if c.key() not in seen:
                seen.add(c.key())
                out.append(c)
    return out


def referenced_issue_numbers(claims: list[Claim], repo_slug: str | None = None) -> list[int]:
    """Return issue numbers from ``fixes_issue`` claims that point at ``repo_slug`` (or no repo)."""
    nums: list[int] = []
    for c in claims:
        if c.type != FIXES_ISSUE:
            continue
        repo = c.details.get("repo")
        if repo and repo_slug and repo.lower() != repo_slug.lower():
            continue
        if repo and not repo_slug:
            continue
        n = int(c.details["number"])
        if n not in nums:
            nums.append(n)
    return nums
