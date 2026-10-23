"""One retry for installed-host qualification after a failed assertion (#140).

These suites drive the real Claude Code CLI against a localhost fixture on
shared CI runners. A runner that stalls for a second or two (seen on Windows:
cold hooks normally 0.3-0.6 s, spikes to 1.8 s) makes a hook miss its window,
and Claude Code then correctly delivers the unreduced result: the plugin
fails open by design. That is a property of the runner, not of the code.

Each test method of a decorated class is run again once if it fails an
assertion, including one inside ``self.subTest(...)``. The first failure is
printed, so retries stay visible in the log. A real regression fails both
runs. Errors (exceptions other than assertion failures) are never retried,
nor are tests marked expectedFailure.

unittest's subTest swallows a failure into the result, so the method returns
normally and a plain ``except AssertionError`` never sees it. During the
first run, subTest is therefore wrapped: an assertion failure inside it is
collected (the other subtests still run, as usual) and, if any was, the
whole method is run again with the real subTest, whose failures are then
reported as usual.
"""
import contextlib
import functools
import sys


def retry_once(cls):
    for name in list(vars(cls)):
        # An expected failure (unittest.expectedFailure, a known gap) fails
        # every run by design; retrying it would only double its cost.
        if (name.startswith("test_") and callable(getattr(cls, name))
                and not getattr(getattr(cls, name), "__unittest_expecting_failure__", False)):
            setattr(cls, name, _retrying(getattr(cls, name)))
    return cls


def _retrying(test):
    @functools.wraps(test)
    def run(self, *args, **kwargs):
        failed = []
        real = self.subTest

        @contextlib.contextmanager
        def collecting(*sub_args, **params):
            # The real subTest still sees errors and skips, and reports them.
            with real(*sub_args, **params):
                try:
                    yield
                except AssertionError as failure:
                    failed.append("subTest %s: %s" % (", ".join(
                        [repr(arg) for arg in sub_args] + ["%s=%r" % item for item in params.items()]), failure))
        self.subTest = collecting
        try:
            try:
                test(self, *args, **kwargs)
            finally:
                del self.subTest
        except AssertionError as first:
            failed.append(str(first))
        if not failed:
            return None
        print("\n  RETRY %s.%s once after a failed assertion (host timing?): %s"
              % (type(self).__name__, test.__name__, "; ".join(failed)[:800]), file=sys.stderr)
        return test(self, *args, **kwargs)
    return run
