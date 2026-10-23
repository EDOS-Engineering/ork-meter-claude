"""Recognize host annotations without accepting changes to recovered evidence."""
import os


def assert_host_suffix(test, suffix, work):
    suffix = suffix.strip()
    if os.name == "nt":
        notice = "Shell cwd was reset to " + str(work)
        if suffix == notice or suffix.startswith(notice + "\n"):
            suffix = suffix[len(notice):].strip()
    test.assertTrue(not suffix or suffix.startswith("<system-reminder>"), suffix[:500])
