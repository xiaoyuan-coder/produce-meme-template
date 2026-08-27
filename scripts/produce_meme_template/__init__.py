"""Public API for the produce-meme-template workflow."""

from .adapters import (
    AliyunOssWorkflowAdapters,
    DeterministicFixtureAdapters,
    FalQueueWorkflowAdapters,
)
from .workflow import BatchProductionResult, ProductionResult, run_production
from .execution_authority import build_live_production_adapters
from .template_test import (
    TemplateTestPreparationResult,
    TemplateTestResult,
    prepare_template_test,
    run_recorded_template_test,
)
from .experience_regression import (
    ExperienceRegressionAdapters,
    run_experience_regression,
)
from .release_readiness import (
    LiveShadowReadinessAdapters,
    RecordedShadowReadinessAdapters,
    live_release_readiness_preflight,
    live_shadow_request,
    recorded_shadow_request,
    run_release_readiness,
    verify_code_review_receipt,
    verify_release_readiness_completion,
)
from .release_management import build_release, promote_release, stage_release

__all__ = [
    "AliyunOssWorkflowAdapters",
    "BatchProductionResult",
    "DeterministicFixtureAdapters",
    "ExperienceRegressionAdapters",
    "FalQueueWorkflowAdapters",
    "LiveShadowReadinessAdapters",
    "ProductionResult",
    "RecordedShadowReadinessAdapters",
    "TemplateTestResult",
    "TemplateTestPreparationResult",
    "build_release",
    "build_live_production_adapters",
    "live_release_readiness_preflight",
    "live_shadow_request",
    "promote_release",
    "run_production",
    "run_experience_regression",
    "recorded_shadow_request",
    "run_release_readiness",
    "stage_release",
    "prepare_template_test",
    "run_recorded_template_test",
    "verify_code_review_receipt",
    "verify_release_readiness_completion",
]
