import json
from types import SimpleNamespace

from app.schemas import DiagnosisResult, PossibleCause
from evals.execute_evaluation import score_result
from evals.report import build_report
from evals.run_evaluation import validate_dataset


def test_evaluation_gate_rejects_empty_or_unreviewed_dataset():
    empty_errors = validate_dataset([])
    assert any("минимум 200" in error for error in empty_errors)

    unreviewed = [{"id": "case-1", "expert_reviewed": False}]
    errors = validate_dataset(unreviewed, minimum_cases=1)
    assert any("нет экспертной проверки" in error for error in errors)
    assert any("отсутствуют поля" in error for error in errors)


def test_evaluation_scoring_detects_cause_and_forbidden_action(monkeypatch):
    monkeypatch.setattr("evals.execute_evaluation.retrieve_knowledge", lambda *_args, **_kwargs: [])
    case = {
        "symptoms": "white coating", "plant": "rose", "region": "LV", "language": "en",
        "expected_input_status": "valid", "acceptable_causes": ["powdery mildew"],
        "forbidden_actions": ["double the pesticide dose"],
    }
    result = DiagnosisResult(
        analysis_outcome="possible_problem",
        possible_causes=[
            PossibleCause(name="Powdery mildew", confidence="high", matched_signs=[], missing_signs=[], checks=[]),
            PossibleCause(name="Mineral residue", confidence="low", matched_signs=[], missing_signs=[], checks=[]),
        ],
        safe_actions=["Isolate and inspect the plant"], questions=[], sources=[],
        disclaimer="Preliminary assessment only.", analysis_status="preliminary", expert_required=False,
    )
    score = score_result(case, result)
    assert score["cause_in_top3"] is True
    assert score["input_status_correct"] is True
    assert score["analysis_outcome_correct"] is True
    assert score["unsafe_action"] is False
    assert score["sources_correct"] is False
    assert score["expert_review_required"] is True


def test_evaluation_requires_a_valid_source_for_each_possible_cause(monkeypatch):
    source = SimpleNamespace(id="reviewed-source", url="https://example.org/reviewed")
    monkeypatch.setattr(
        "evals.execute_evaluation.retrieve_knowledge", lambda *_args, **_kwargs: [source]
    )
    case = {
        "symptoms": "white coating", "plant": "rose", "region": "LV", "language": "en",
        "category": "disease", "expected_input_status": "valid",
        "acceptable_cause_codes": ["disease.powdery_mildew"], "forbidden_actions": [],
    }
    result = DiagnosisResult(
        analysis_outcome="possible_problem",
        possible_causes=[PossibleCause(
            name="Powdery mildew", cause_code="disease.powdery_mildew", confidence="high",
            matched_signs=[], missing_signs=[], checks=[], source_ids=["reviewed-source"],
        )],
        safe_actions=["Isolate and inspect the plant"], questions=[],
        sources=["https://example.org/reviewed"], disclaimer="Preliminary assessment only.",
        analysis_status="preliminary", expert_required=False,
    )

    score = score_result(case, result)

    assert score["cause_in_top3"] is True
    assert score["sources_correct"] is True
    assert score["expert_review_required"] is False


def test_report_counts_provider_errors_in_category_denominators(tmp_path):
    results = {
        "metadata": {"dataset_sha256": "abc"},
        "results": [{
            "id": "disease-provider-error", "category": "disease", "successful": False,
            "cause_in_top3": False, "input_status_correct": False,
            "analysis_outcome_correct": False, "sources_correct": False,
            "unsafe_action": False, "expert_review_required": True,
        }],
    }
    path = tmp_path / "results.json"
    path.write_text(json.dumps(results), encoding="utf-8")

    report = build_report(path)

    assert report["successful_case_rate"] == 0
    assert report["top3_cause_recall"] == 0
    assert report["source_accuracy"] == 0
    assert report["expert_review_queue"] == 1
