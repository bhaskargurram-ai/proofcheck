"""Static validation of candidate tests and Otter-style import repair."""

import unittest

import _helpers  # noqa: F401  (sets up sys.path)

from proofcheck.agent.static import ModuleIndex, closest, levenshtein, validate_test_code

SOURCES = {
    "src/shop/__init__.py": "from .cart import Cart\n",
    "src/shop/cart.py": "class Cart:\n    pass\n\n\ndef total(items):\n    return sum(items)\n\nTAX = 0.2\n",
    "src/shop/pricing.py": "def apply_discount(price, pct):\n    return price\n\n\ndef round_price(p):\n    return p\n",
    "src/shop/util/__init__.py": "",
    "src/shop/util/text.py": "from .helpers import *\n",
    "tests/test_cart.py": "import pytest\nfrom shop.cart import Cart\n",
    "setup.py": "",
}


def index():
    return ModuleIndex.from_paths(SOURCES, read=SOURCES.get)


class EditDistanceTest(unittest.TestCase):
    def test_levenshtein(self):
        self.assertEqual(levenshtein("kitten", "sitting"), 3)
        self.assertEqual(levenshtein("", "abc"), 3)
        self.assertEqual(levenshtein("same", "same"), 0)

    def test_closest_is_unique_and_bounded(self):
        self.assertEqual(closest("totl", ["total", "Cart", "TAX"]), "total")
        self.assertIsNone(closest("completely_different", ["total"]))
        self.assertIsNone(closest("ab", ["ac", "ad"]))  # ambiguous


class ModuleIndexTest(unittest.TestCase):
    def test_src_layout_and_packages(self):
        idx = index()
        for m in ("shop", "shop.cart", "shop.pricing", "shop.util", "shop.util.text", "tests.test_cart", "setup"):
            self.assertTrue(idx.has_module(m), m)
        names, open_ = idx.names_in("shop.cart")
        self.assertEqual(names, {"Cart", "total", "TAX"})
        self.assertFalse(open_)
        self.assertTrue(idx.names_in("shop.util.text")[1])  # star import: cannot judge
        self.assertIn("cart", idx.names_in("shop")[0])  # submodule counts as a name
        self.assertEqual(idx.modules_defining("apply_discount"), ["shop.pricing"])


PYTEST_OK = "from shop.cart import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n"


class ValidateTestCodeTest(unittest.TestCase):
    def test_valid_pytest_file(self):
        rep = validate_test_code(PYTEST_OK, "pytest", index())
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual(rep.test_names, ["test_total"])
        self.assertEqual(rep.code, PYTEST_OK)

    def test_syntax_error_and_empty(self):
        self.assertIn("syntax error", validate_test_code("def test_x(:\n  pass\n", "pytest", None).errors[0])
        self.assertFalse(validate_test_code("   ", "pytest", None).ok)

    def test_needs_a_collectable_test(self):
        rep = validate_test_code("def helper():\n    return 1\n\nX = 1\n", "pytest", None)
        self.assertIn("no tests found", rep.errors[0])
        rep = validate_test_code(PYTEST_OK, "unittest", index())
        self.assertFalse(rep.ok)  # unittest projects need a TestCase
        rep = validate_test_code("import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n",
                                 "unittest", index())
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual(rep.test_names, ["T.test_a"])

    def test_unittest_projects_reject_pytest_import(self):
        code = "import pytest\nimport unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
        rep = validate_test_code(code, "unittest", index())
        self.assertTrue(any("do not import pytest" in e for e in rep.errors))

    def test_denylist(self):
        for snippet in ("import subprocess", "import socket", "from urllib.request import urlopen", "import requests",
                        "from os import system", "os.system('id')", "eval('1')", "__import__('os')"):
            code = f"import os\n{snippet}\n\ndef test_x():\n    pass\n"
            rep = validate_test_code(code, "pytest", None)
            self.assertFalse(rep.ok, snippet)
        rep = validate_test_code("import os, tempfile, shutil\nfrom urllib.parse import quote\n\ndef test_x():\n    pass\n",
                                 "pytest", None)
        self.assertTrue(rep.ok, rep.errors)

    def test_relative_import_rejected(self):
        rep = validate_test_code("from .cart import total\n\ndef test_x():\n    pass\n", "pytest", index())
        self.assertTrue(any("relative imports" in e for e in rep.errors))


class ImportRepairTest(unittest.TestCase):
    def check(self, code, expected_line):
        rep = validate_test_code(code, "pytest", index())
        self.assertTrue(rep.ok, rep.errors)
        self.assertIn(expected_line, rep.code)
        self.assertTrue(rep.repairs)
        return rep

    def test_module_typo(self):
        self.check("from shop.carts import total\n\ndef test_x():\n    assert total([1]) == 1\n", "from shop.cart import total")

    def test_top_level_typo(self):
        self.check("from shops.cart import total\n\ndef test_x():\n    pass\n", "from shop.cart import total")
        self.check("import shops\n\ndef test_x():\n    pass\n", "import shop as shops")

    def test_name_typo_keeps_the_name_the_body_uses(self):
        self.check("from shop.cart import totl\n\ndef test_x():\n    assert totl([1]) == 1\n", "from shop.cart import total as totl")

    def test_name_from_another_module(self):
        rep = self.check("from shop.cart import Cart, apply_discount\n\ndef test_x():\n    pass\n",
                         "from shop.cart import Cart\nfrom shop.pricing import apply_discount")
        self.assertIn("from shop.cart import apply_discount -> from shop.pricing import apply_discount", rep.repairs)

    def test_indented_import_is_repaired_in_place(self):
        code = "def test_x():\n    from shop.carts import total\n    assert total([2]) == 2\n"
        rep = self.check(code, "    from shop.cart import total\n")
        compile(rep.code, "t.py", "exec")

    def test_unknown_name_is_reported(self):
        rep = validate_test_code("from shop.cart import does_not_exist_anywhere\n\ndef test_x():\n    pass\n", "pytest", index())
        self.assertFalse(rep.ok)
        self.assertIn("not defined in module 'shop.cart'", rep.errors[0])

    def test_third_party_and_stdlib_untouched(self):
        code = "import json\nimport some_dependency_xyz\nfrom collections import OrderedDict\n\ndef test_x():\n    pass\n"
        rep = validate_test_code(code, "pytest", index())
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual(rep.code, code)
        self.assertEqual(rep.repairs, [])


if __name__ == "__main__":
    unittest.main()
