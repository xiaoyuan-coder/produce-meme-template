#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.produce_meme_template import release_management
from scripts.produce_meme_template.release_management import VALIDATION_SUITE_ENV
from scripts.produce_meme_template.experience_regression import (
    EvidenceStatusRecordingResult,
    ExperienceRegressionAdapters,
    compile_evidence_execution_results,
    run_experience_regression,
)


class CompletedSuiteEvidenceAdapters(ExperienceRegressionAdapters):
    def __init__(
        self,
        status_by_test_id: dict[str, str],
        detail_by_test_id: dict[str, str | None] | None = None,
    ) -> None:
        self.status_by_test_id = status_by_test_id
        self.detail_by_test_id = detail_by_test_id or {}

    def execute_evidence(
        self, runtime_root: Path, test_ids: list[str]
    ) -> dict[str, dict[str, object]]:
        return compile_evidence_execution_results(
            runtime_root,
            test_ids,
            self.status_by_test_id,
            self.detail_by_test_id,
        )


def main() -> int:
    os.environ[VALIDATION_SUITE_ENV] = "1"
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
    result = unittest.TextTestRunner(
        verbosity=1,
        resultclass=EvidenceStatusRecordingResult,
    ).run(suite)
    if not result.wasSuccessful():
        return 1
    report = run_experience_regression(
        ROOT,
        None,
        adapters=CompletedSuiteEvidenceAdapters(
            result.evidence_status_by_test_id,
            result.evidence_detail_by_test_id,
        ),
    )
    rules = release_management._load_object(
        ROOT / "contracts" / "machine-rules.json"
    )
    pass_field = rules["historicalExperienceContract"]["reportFields"]["pass"]
    if not report[pass_field]:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
