"""`triage --push` was asked to queue work. If it queued none because it could not
read the inbox, that is a failure and the exit code has to say so.

Measured twice on the real nightly pass. The lawnmower-dayclose runs of 9/16 and
9/17/26 both lost their entire push here -- `dispatch ls` returns
`secret_unavailable` in a routed session with no DISPATCH_SECRET_FILE, so
`_push_to_inbox` bailed by design, printed one line to stderr, and returned. The
run carried on, reported, and looked like it had worked. The 9/17 session only
caught it by re-running the command by hand.

The bail itself is right: filing without reading the inbox first risks another
round of duplicates, and a missed finding reappears tomorrow while a duplicate
never leaves. What was wrong was exiting 0 after it.

Queueing nothing because every finding is ALREADY OPEN is a different outcome. It
is healthy and stays 0.
"""
import inspect
import unittest

from opp_axi import cli


class UnreadableInboxIsNotSuccess(unittest.TestCase):
    def test_the_bail_returns_e_partial(self):
        src = inspect.getsource(cli._push_to_inbox)
        bail = src.index("could not read the inbox")
        after = src[bail:]
        self.assertIn("return E_PARTIAL", after,
                      "the inbox-unreadable bail must return a failure code")

    def test_the_success_path_returns_ok(self):
        src = inspect.getsource(cli._push_to_inbox)
        self.assertIn("return E_OK", src)

    def test_the_call_site_propagates_it(self):
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn("rc = _push_to_inbox(findings)", src)
        self.assertIn("return rc", src)

    def test_main_honours_a_commands_return_code(self):
        """Before this, `a.fn(a)` discarded the return value, so cmd_triage could
        return whatever it liked and the process still exited 0."""
        src = inspect.getsource(cli.main)
        self.assertIn("rc = a.fn(a)", src)
        self.assertIn("isinstance(rc, int)", src)
        self.assertNotIn("        a.fn(a)\n", src,
                         "the return value must not be discarded")

    def test_a_non_int_return_is_not_read_as_an_exit_code(self):
        """A command returning some other object is not declaring an exit code."""
        src = inspect.getsource(cli.main)
        self.assertIn("if isinstance(rc, int) and rc:", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
