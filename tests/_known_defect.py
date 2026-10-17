"""tests/_known_defect.py -- a stricter replacement for @unittest.expectedFailure.

Why not expectedFailure: it treats ANY exception as "the expected failure". A hook that never fires,
an AttributeError after a refactor renames a patched function, or an import error all keep the test
"failing as expected" forever, so the marker can silently outlive (or mask the absence of) the fix it
was meant to force. That defeats the point of pinning a known defect.

Contract of @known_defect:
  * The test body asserts its PRECONDITIONS with ordinary asserts (the race hook fired, the target
    symbol exists) and the defect-revealing INVARIANT with `invariant(cond, msg)`.
  * Only an InvariantViolated raised by `invariant()` means "defect still present": the test then
    passes and warns, so the pinned defect stays visible in the run output.
  * Any other exception (a failed precondition, an AttributeError, a TypeError from a changed
    signature) propagates as a real failure: the test is broken and must be updated.
  * If the body completes with every invariant holding, the defect is fixed: the test FAILS with a
    message telling the fixer to remove the decorator. The marker therefore cannot outlive its fix.
"""
from __future__ import annotations

import functools
import warnings


class InvariantViolated(AssertionError):
    """The known defect is present: the pinned invariant does not hold."""


def invariant(condition, message: str = "") -> None:
    if not condition:
        raise InvariantViolated(message)


def known_defect(reason: str):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                fn(self, *args, **kwargs)
            except InvariantViolated as exc:
                warnings.warn(f"KNOWN DEFECT still present in {fn.__name__}: {reason} ({exc})", stacklevel=2)
                return
            self.fail(
                f"{fn.__name__}: the known defect ({reason}) appears FIXED -- every invariant held. "
                "Remove @known_defect so this becomes an ordinary regression test."
            )
        return wrapper
    return decorate
