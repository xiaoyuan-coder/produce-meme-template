#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.produce_meme_template.experience_regression import (
    EvidenceStatusRecordingResult,
    compile_evidence_execution_results,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="执行历史经验矩阵绑定的 unittest 证据"
    )
    parser.add_argument("--test-id", action="append", required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromNames(args.test_id)
    stream = io.StringIO()
    runner = unittest.TextTestRunner(
        stream=stream,
        verbosity=0,
        resultclass=EvidenceStatusRecordingResult,
    )
    result = runner.run(suite)
    payload = compile_evidence_execution_results(
        ROOT,
        args.test_id,
        result.evidence_status_by_test_id,
        result.evidence_detail_by_test_id,
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
