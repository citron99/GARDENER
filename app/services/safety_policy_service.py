import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import RegulatedProductRegistration
from app.schemas import AnalysisOutcome, DiagnosisResult

_DOSE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:ml|millilit(?:er|re)|g|gram|kg|l|lit(?:er|re)|мл|г|кг|л)\b",
    re.IGNORECASE,
)
_MIX = re.compile(r"\b(?:mix|combine|tank[- ]mix|смеш\w*|samais\w*|maisījum\w*)\b", re.IGNORECASE)
_SAFE_MIX_PROHIBITION = re.compile(
    r"(?:do not|don't|never|не|нельзя|nedrīkst|nekad)\s+\w*\s*(?:mix|смеш|samais|mais)",
    re.IGNORECASE,
)
_CHEMICAL = re.compile(
    r"\b(?:pesticid\w*|fungicid\w*|herbicid\w*|insecticid\w*|acaricid\w*|"
    r"chemical\w*|фунгицид\w*|гербицид\w*|инсектицид\w*|акарицид\w*|"
    r"пестицид\w*|химичес\w*|pesticīd\w*|fungicīd\w*|herbicīd\w*|insekticīd\w*)\b",
    re.IGNORECASE,
)
_APPLICATION = re.compile(
    r"\b(?:apply|spray|dose|use|treat|примен\w*|опрыск\w*|обработ\w*|доз\w*|"
    r"lietot\w*|izsmidzin\w*|apstrād\w*|dev\w*)\b",
    re.IGNORECASE,
)
_SAFE_APPLICATION_PROHIBITION = re.compile(
    r"(?:do not|don't|never|не|нельзя|nedrīkst|nekad)\s+(?:\w+\s+){0,3}"
    r"(?:apply|spray|use|treat|примен|опрыск|обработ|lietot|izsmidzin|apstrād)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SafetyPolicyDecision:
    adjusted: bool
    reason_codes: tuple[str, ...]
    removed_action_count: int


def classify_unsafe_action(action: str, registered_names: set[str] | None = None) -> tuple[str, ...]:
    """Return stable policy reason codes without persisting sensitive action text."""
    registered_names = registered_names or set()
    normalized = action.casefold()
    reasons: set[str] = set()
    if _DOSE.search(action):
        reasons.add("unverified_dosage")
    if _MIX.search(action) and not _SAFE_MIX_PROHIBITION.search(action):
        reasons.add("chemical_mixing")
    if (
        _CHEMICAL.search(action)
        and _APPLICATION.search(action)
        and not _SAFE_APPLICATION_PROHIBITION.search(action)
    ):
        reasons.add("unverified_chemical_action")
        if not any(name in normalized for name in registered_names):
            reasons.add("unregistered_product")
    return tuple(sorted(reasons))


def _fallback_actions(text: str) -> list[str]:
    if re.search(r"[А-Яа-яЁё]", text):
        return [
            "Не смешивайте и не применяйте химические средства без проверки регистрации и инструкции.",
            "Зафиксируйте изменения, изолируйте растение при необходимости и обратитесь к специалисту.",
        ]
    if re.search(r"[āčēģīķļņšūž]", text, re.IGNORECASE):
        return [
            "Nejauciet un nelietojiet ķīmiskus līdzekļus bez reģistrācijas un marķējuma pārbaudes.",
            "Dokumentējiet izmaiņas, vajadzības gadījumā izolējiet augu un konsultējieties ar speciālistu.",
        ]
    return [
        "Do not mix or apply chemical products before checking registration and the approved label.",
        "Document changes, isolate the plant if appropriate, and consult a qualified specialist.",
    ]


def enforce_diagnosis_safety(db: Session, result: DiagnosisResult) -> tuple[DiagnosisResult, SafetyPolicyDecision]:
    registered_names = {
        name.casefold()
        for name in db.scalars(select(RegulatedProductRegistration.product_name).where(
            RegulatedProductRegistration.status == "active",
            or_(
                RegulatedProductRegistration.valid_until.is_(None),
                RegulatedProductRegistration.valid_until >= date.today(),
            ),
        ))
    }
    retained: list[str] = []
    reasons: set[str] = set()
    removed = 0
    for action in result.safe_actions:
        action_reasons = classify_unsafe_action(action, registered_names)
        if not action_reasons:
            retained.append(action)
            continue
        removed += 1
        reasons.update(action_reasons)

    if removed == 0:
        return result, SafetyPolicyDecision(False, (), 0)

    fallback = _fallback_actions(" ".join(result.safe_actions))
    retained.extend(action for action in fallback if action not in retained)
    values = result.model_dump()
    values.update({
        "safe_actions": retained,
        "expert_required": True,
        "analysis_status": (
            "expert_required"
            if result.analysis_outcome == AnalysisOutcome.possible_problem
            else result.analysis_status
        ),
    })
    adjusted = DiagnosisResult.model_validate(values)
    return adjusted, SafetyPolicyDecision(True, tuple(sorted(reasons)), removed)
