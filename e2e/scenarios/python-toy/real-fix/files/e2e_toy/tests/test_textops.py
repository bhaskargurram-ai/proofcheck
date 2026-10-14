import unittest

from e2e_toy.textops import chunk, clamp, slugify, word_count


class WordCountTest(unittest.TestCase):
    def test_single_spaces(self):
        self.assertEqual(word_count("a b c"), 3)

    def test_repeated_spaces(self):
        self.assertEqual(word_count("hello  world"), 2)
        self.assertEqual(word_count(" padded\ttext\n"), 2)


class SlugifyTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_extra_spaces(self):
        self.assertEqual(slugify("  Hello   World "), "hello-world")


class ClampTest(unittest.TestCase):
    def test_inside(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_below(self):
        self.assertEqual(clamp(-3, 0, 10), 0)


class ChunkTest(unittest.TestCase):
    def test_even(self):
        self.assertEqual(chunk([1, 2, 3, 4], 2), [[1, 2], [3, 4]])

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            chunk([1], 0)
