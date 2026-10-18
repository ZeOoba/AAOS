"""tests/test_known_defect.py -- the @known_defect helper must be stricter than expectedFailure.

The three behaviours that matter, each exercised through a real inner TestCase run:
  1. invariant violated      -> the test passes (defect still present) and a warning is emitted
  2. any OTHER exception     -> a real error (a broken hook, a rename, a changed signature)
  3. every invariant holds   -> the test FAILS, telling the fixer to remove the decorator
"""
from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _known_defect import invariant, known_defect  # noqa: E402


def _run(case_cls):
    result = unittest.TestResult()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        unittest.defaultTestLoader.loadTestsFromTestCase(case_cls).run(result)
    return result, [str(w.message) for w in caught]


class KnownDefectSemantics(unittest.TestCase):
    def test_violated_invariant_passes_and_warns(self):
        class Inner(unittest.TestCase):
            @known_defect("pinned bug")
            def test_it(self):
                invariant(False, "still broken")

        result, warns = _run(Inner)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(warns), 1)
        self.assertIn("KNOWN DEFECT still present", warns[0])
        self.assertIn("pinned bug", warns[0])

    def test_a_still_present_defect_does_not_fail_under_warnings_as_errors(self):
        class Inner(unittest.TestCase):
            @known_defect("pinned bug")
            def test_it(self):
                invariant(False, "still broken")

        result = unittest.TestResult()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            unittest.defaultTestLoader.loadTestsFromTestCase(Inner).run(result)
        self.assertTrue(result.wasSuccessful(), result.errors)

    def test_unrelated_exceptions_are_real_errors_not_expected_failures(self):
        for exc in (AttributeError("renamed"), TypeError("signature changed"), AssertionError("hook never fired")):
            with self.subTest(exc=type(exc).__name__):
                class Inner(unittest.TestCase):
                    @known_defect("pinned bug")
                    def test_it(self, _exc=exc):
                        raise _exc

                result, _ = _run(Inner)
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(len(result.errors) + len(result.failures), 1)

    def test_a_fixed_defect_fails_the_run(self):
        class Inner(unittest.TestCase):
            @known_defect("pinned bug")
            def test_it(self):
                invariant(True, "fine now")

        result, warns = _run(Inner)
        self.assertFalse(result.wasSuccessful())
        self.assertIn("appears FIXED", result.failures[0][1])
        self.assertEqual(warns, [])

    def test_contrast_with_expectedFailure_which_would_hide_a_broken_test(self):
        """Documents why the helper exists: expectedFailure treats a plain AttributeError as the
        expected failure."""
        class Inner(unittest.TestCase):
            @unittest.expectedFailure
            def test_it(self):
                raise AttributeError("a refactor renamed the patched function")

        result, _ = _run(Inner)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.expectedFailures), 1)


if __name__ == "__main__":
    unittest.main()
