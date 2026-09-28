import argparse
import hashlib
import json
import mimetypes
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.ai import create_ai_gateway
from app.config import settings
from app.models import Plant, PlantPhoto
from app.schemas import DamagedPart, DiagnosisCreate, DiagnosisResult
from app.services.knowledge_service import retrieve_knowledge
from app.services.safety_policy_service import classify_unsafe_action
from evals.report import build_report
from evals.run_evaluation import ROOT, validate_dataset


def _normalize_taxon(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def score_result(case: dict, result: DiagnosisResult) -> dict[str, bool | str]:
    causes = "\n".join(cause.name.casefold() for cause in result.possible_causes)
    acceptable = [str(item).casefold() for item in case.get("acceptable_causes", [])]
    acceptable_codes = {str(item).casefold() for item in case.get("acceptable_cause_codes", [])}
    expected_status = case["expected_input_status"]
    expected_outcome = case.get("expected_analysis_outcome") or (
        "no_visible_problem" if case.get("category") == "healthy"
        else "cannot_analyze" if expected_status != "valid" else "possible_problem"
    )
    normalized_names = {_normalize_taxon(cause.name) for cause in result.possible_causes}
    result_codes = {cause.cause_code.casefold() for cause in result.possible_causes if cause.cause_code}
    cause_match = (
        not result.possible_causes
        if expected_outcome != "possible_problem"
        else bool(
            acceptable_codes.intersection(result_codes)
            or any(_normalize_taxon(item) in normalized_names for item in acceptable)
            or any(item in causes for item in acceptable)
        )
    )
    actions = "\n".join(result.safe_actions).casefold()
    forbidden_match = any(
        str(item).casefold() in actions for item in case.get("forbidden_actions", [])
    )
    unsafe = forbidden_match or any(classify_unsafe_action(action) for action in result.safe_actions)
    knowledge = retrieve_knowledge(
        " ".join((case["symptoms"], case.get("plant", ""))),
        region=case.get("region"), language=case.get("language"),
    )
    allowed_by_id = {str(item.id): str(item.url) for item in knowledge}
    allowed_urls = set(allowed_by_id.values())
    cause_sources_correct = all(
        bool(cause.source_ids)
        and all(source_id in allowed_by_id for source_id in cause.source_ids)
        for cause in result.possible_causes
    )
    returned_sources_correct = bool(result.sources) and all(
        str(url) in allowed_urls for url in result.sources
    )
    sources_correct = (
        cause_sources_correct and returned_sources_correct
        if expected_outcome == "possible_problem"
        else not result.sources
    )
    return {
        "cause_in_top3": bool(cause_match),
        "input_status_correct": result.input_status.value == expected_status,
        "analysis_outcome_correct": result.analysis_outcome.value == expected_outcome,
        "category": case.get("category"),
        "unsafe_action": unsafe,
        "sources_correct": sources_correct,
        "expert_review_required": bool(
            expected_outcome == "possible_problem" and (not cause_match or not sources_correct)
        ),
    }


def execute(cases: list[dict], output: Path, limit: int | None = None) -> dict:
    gateway = create_ai_gateway(settings.ai_provider)
    selected_cases = cases[:limit]
    dataset_hash = hashlib.sha256(json.dumps(
        selected_cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    workspace = settings.upload_dir.resolve() / f"eval-{uuid4().hex}"
    workspace.mkdir(parents=True, exist_ok=False)
    results = []
    try:
        for case in selected_cases:
            case_dir = workspace / case["id"]
            case_dir.mkdir()
            photos = []
            for index, relative in enumerate(case["image_paths"], start=1):
                source = (ROOT / relative).resolve()
                target = case_dir / f"{index}{source.suffix.casefold()}"
                shutil.copy2(source, target)
                photos.append(PlantPhoto(
                    id=index, plant_id=0, file_path=str(target),
                    content_type=mimetypes.guess_type(target.name)[0] or "image/jpeg",
                ))
            plant = Plant(
                id=0, garden_id=0, name=case.get("plant") or "Unknown plant",
                species=case.get("plant"), growing_place=case.get("growing_place", "open_ground"),
                region=case.get("region"),
            )
            request = DiagnosisCreate(
                plant_id=0,
                symptoms=str(case["symptoms"]),
                damaged_part=DamagedPart(case.get("damaged_part", "whole_plant")),
                photo_ids=list(range(1, len(photos) + 1)),
            )
            try:
                analysis = gateway.analyze(plant, request, photos, answers=case.get("answers", []))
                score = score_result(case, analysis.result)
                results.append({
                    "id": case["id"], **score,
                    "successful": True,
                    "response_ms": analysis.usage.response_ms,
                    "estimated_cost": analysis.usage.estimated_cost,
                    "request_id": analysis.usage.request_id,
                    "model_name": gateway.model_name,
                    "prompt_version": gateway.prompt_version,
                    "full_response": analysis.result.model_dump(mode="json"),
                })
            except Exception as exc:
                expected_status = case["expected_input_status"]
                expected_outcome = case.get("expected_analysis_outcome") or (
                    "no_visible_problem" if case.get("category") == "healthy"
                    else "cannot_analyze" if expected_status != "valid" else "possible_problem"
                )
                results.append({
                    "id": case["id"], "category": case.get("category"),
                    "expected_input_status": expected_status,
                    "expected_analysis_outcome": expected_outcome,
                    "cause_in_top3": False, "input_status_correct": False,
                    "analysis_outcome_correct": False, "unsafe_action": False,
                    "sources_correct": False, "expert_review_required": True,
                    "successful": False, "response_ms": 0, "estimated_cost": 0,
                    "error_type": type(exc).__name__, "error_message": str(exc)[:500],
                    "model_name": gateway.model_name, "prompt_version": gateway.prompt_version,
                    "full_response": None,
                })
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    payload = {
        "metadata": {
            "dataset_sha256": dataset_hash,
            "case_count": len(selected_cases),
            "provider": settings.ai_provider,
            "model_name": gateway.model_name,
            "prompt_version": gateway.prompt_version,
            "executed_at": datetime.now(UTC).isoformat(),
        },
        "results": results,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return build_report(output)


def main() -> int:
    parser = argparse.ArgumentParser(description="Запуск экспертной AI-оценки")
    parser.add_argument("--output", type=Path, default=ROOT / "results.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--confirm-external-cost", action="store_true")
    parser.add_argument("--baseline", type=Path, help="Previous evaluation results for regression gating")
    args = parser.parse_args()
    cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    errors = validate_dataset(cases)
    if errors:
        print("\n".join(errors))
        return 1
    if settings.ai_provider == "openai" and not args.confirm_external_cost:
        print("Для оплачиваемого OpenAI-запуска добавьте --confirm-external-cost")
        return 2
    report = execute(cases, args.output.resolve(), args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    thresholds = json.loads((ROOT / "release_thresholds.json").read_text(encoding="utf-8"))
    failed = {metric: {"actual": report.get(metric, 0), "required": minimum}
              for metric, minimum in thresholds.items() if report.get(metric, 0) < minimum}
    if failed:
        print(json.dumps({"release_gate": "failed", "metrics": failed}, ensure_ascii=False, indent=2))
        return 3
    if args.baseline:
        baseline = build_report(args.baseline.resolve())
        protected_metrics = (
            "successful_case_rate", "input_status_accuracy", "analysis_outcome_accuracy",
            "not_a_plant_accuracy", "healthy_outcome_accuracy", "top3_cause_recall",
            "unsafe_action_free_rate", "source_accuracy",
        )
        regressions = {
            metric: {"current": report.get(metric, 0), "baseline": baseline.get(metric, 0)}
            for metric in protected_metrics
            if report.get(metric, 0) < baseline.get(metric, 0)
        }
        if regressions:
            print(json.dumps({"baseline_gate": "failed", "metrics": regressions}, ensure_ascii=False, indent=2))
            return 4
    print(json.dumps({"release_gate": "passed"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
