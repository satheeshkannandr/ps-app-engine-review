#!/usr/bin/env python3
"""Run the ae_extractor tests without pytest.

    python tests/run_tests.py
"""

import io
import contextlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_ae_extractor as suite  # noqa: E402


def main() -> int:
    tests = [
        (name, fn) for name, fn in vars(suite).items()
        if name.startswith("test_") and callable(fn)
    ]
    failures: list[str] = []
    for name, fn in tests:
        try:
            # argparse error paths write usage text to stderr; keep output readable.
            with contextlib.redirect_stderr(io.StringIO()):
                fn()
        except Exception as exc:
            failures.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=2)

    passed = len(tests) - len(failures)
    print(f"\n{passed} passed, {len(failures)} failed, {len(tests)} total")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
