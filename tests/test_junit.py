import unittest

import _helpers  # noqa: F401

from proofcheck.junit import ERROR, FAILED, PASSED, SKIPPED, JUnitParseError, parse_junit_string
from proofcheck.runners import added_lines_from_diff, python_touched_tests

PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5">
  <testcase classname="tests.test_x" name="test_ok" time="0.001"/>
  <testcase classname="tests.test_x.TestCls" name="test_fail" time="0.001"><failure message="assert 1 == 2">trace</failure></testcase>
  <testcase classname="tests.test_x" name="test_skip" time="0"><skipped type="pytest.skip" message="nope"/></testcase>
  <testcase classname="tests.test_x" name="test_param[1-2]" time="0"/>
  <testcase classname="tests.test_x" name="test_ok" time="0"><error message="teardown boom"/></testcase>
  <testcase classname="" name="tests.test_broken" time="0"><error message="collection failure">ImportError</error></testcase>
</testsuite></testsuites>"""


class JUnitParseTest(unittest.TestCase):
    def test_parse_pytest_report(self):
        r = parse_junit_string(PYTEST_XML)
        self.assertEqual(r["tests.test_x.TestCls::test_fail"].outcome, FAILED)
        self.assertEqual(r["tests.test_x.TestCls::test_fail"].message, "assert 1 == 2")
        self.assertEqual(r["tests.test_x::test_skip"].outcome, SKIPPED)
        self.assertEqual(r["tests.test_x::test_param[1-2]"].outcome, PASSED)
        # duplicate entry (teardown error) wins over the pass
        self.assertEqual(r["tests.test_x::test_ok"].outcome, ERROR)
        # collection error has an id without "::"
        self.assertEqual(r["tests.test_broken"].outcome, ERROR)

    def test_single_testsuite_root_and_empty(self):
        r = parse_junit_string('<testsuite><testcase classname="a" name="b"/></testsuite>')
        self.assertEqual(list(r), ["a::b"])
        self.assertEqual(parse_junit_string("<testsuites/>"), {})

    def test_rejects_invalid_and_dtd(self):
        with self.assertRaises(JUnitParseError):
            parse_junit_string("<testsuite><testcase")
        bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase name="&a;"/></testsuite>'
        with self.assertRaises(JUnitParseError):
            parse_junit_string(bomb)


class TouchedTestsTest(unittest.TestCase):
    SRC = """import unittest

def helper():
    return 1


def test_alpha():
    assert helper() == 1


class TestThing(unittest.TestCase):
    @staticmethod
    def make():
        return 2

    def test_beta(self):
        self.assertEqual(self.make(), 2)

    def test_gamma(self):
        pass
"""

    def test_line_mapping(self):
        self.assertEqual(python_touched_tests(self.SRC, {8}), {(None, "test_alpha")})
        self.assertEqual(python_touched_tests(self.SRC, {17}), {("TestThing", "test_beta")})
        self.assertEqual(python_touched_tests(self.SRC, {4}), set())  # helper only
        self.assertEqual(len(python_touched_tests(self.SRC, None)), 3)  # new file
        self.assertIsNone(python_touched_tests("def (:", {1}))

    def test_added_lines_from_diff(self):
        diff = "@@ -3,0 +4,2 @@\n+a\n+b\n@@ -10 +12 @@\n-x\n+y\n@@ -20,2 +21,0 @@\n-gone\n-gone\n"
        self.assertEqual(added_lines_from_diff(diff), {4, 5, 12, 21, 22})


if __name__ == "__main__":
    unittest.main()
