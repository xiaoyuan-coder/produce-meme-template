#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.produce_meme_template import release_management


def main() -> int:
    original_validation = release_management._run_release_validation

    def nested_validation(
        runtime_root: Path,
        contract: dict,
        *,
        include_tests: bool,
    ) -> tuple[bool, str | None]:
        del include_tests
        return original_validation(
            runtime_root, contract, include_tests=False
        )

    release_management._run_release_validation = nested_validation
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), top_level_dir=str(ROOT)
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
