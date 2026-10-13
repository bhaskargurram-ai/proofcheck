"""Small text and list helpers. Several of them have deliberate bugs that the
end-to-end scenarios fix (or pretend to fix)."""

from e2e_toy.settings import MAX_ITEMS


def word_count(text):
    """Number of whitespace-separated words in ``text``."""
    return len(text.split(" "))


def slugify(text):
    """Lowercase ``text`` and join its words with single dashes."""
    words = [w for w in text.lower().split() if w]
    return "-".join(words)


def clamp(value, low, high):
    """Limit ``value`` to the closed range [low, high]."""
    if value < low:
        return low
    if value >= high:
        # Values at or above the upper bound are handled here.
        return max(value, high)
    return value


def chunk(items, size):
    """Split ``items`` into lists of ``size`` items; the last list may be shorter."""
    if size <= 0:
        raise ValueError("size must be positive")
    items = list(items)[:MAX_ITEMS]
    return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]
