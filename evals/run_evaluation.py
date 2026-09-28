import argparse
import json
import sys
from datetime import date
from pathlib import Path

from PIL import Image, UnidentifiedImageError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parent
REQUIRED_FIELDS = {
    "id", "image_paths", "symptoms", "plant", "region", "language",
    "expected_input_status", "acceptable_causes", "forbidden_actions",
    "expert_reviewed", "reviewed_by", "reviewed_at", "image_license", "category",
}
ALLOWED_CATEGORIES = {"healthy", "disease", "pest", "abiotic", "poor_quality", "not_a_plant"}
ALLOWED_STATUSES = {"valid", "poor_quality", "not_a_plant", "insufficient_data"}


def validate_dataset(cases: list[dict], minimum_cases: int = 200) -> list[str]:
    errors: list[str] = []
    if len(cases) < minimum_cases:
        errors.append(f"Нужно минимум {minimum_cases} экспертных случаев; найдено {len(cases)}")
    identifiers: set[str] = set()
    category_counts = dict.fromkeys(ALLOWED_CATEGORIES, 0)
    for index, case in enumerate(cases):
        missing = REQUIRED_FIELDS - case.keys()
        if missing:
            errors.append(f"case[{index}]: отсутствуют поля {sorted(missing)}")
        identifier = str(case.get("id", ""))
        if not identifier or identifier in identifiers:
            errors.append(f"case[{index}]: пустой или повторяющийся id")
        identifiers.add(identifier)
        if case.get("expert_reviewed") is not True:
            errors.append(f"case[{index}]: нет экспертной проверки")
        if not str(case.get("reviewed_by", "")).strip():
            errors.append(f"case[{index}]: не указан проверивший эксперт")
        try:
            date.fromisoformat(str(case.get("reviewed_at", "")))
        except ValueError:
            errors.append(f"case[{index}]: reviewed_at должен быть датой ISO YYYY-MM-DD")
        if not str(case.get("image_license", "")).strip():
            errors.append(f"case[{index}]: не указана лицензия изображения")
        if case.get("language") not in {"ru", "lv", "en"}:
            errors.append(f"case[{index}]: язык должен быть ru, lv или en")
        if case.get("expected_input_status") not in ALLOWED_STATUSES:
            errors.append(f"case[{index}]: недопустимый expected_input_status")
        category = case.get("category")
        if category not in ALLOWED_CATEGORIES:
            errors.append(f"case[{index}]: недопустимая категория")
        else:
            category_counts[category] += 1
        if not isinstance(case.get("acceptable_causes"), list) or not isinstance(case.get("forbidden_actions"), list):
            errors.append(f"case[{index}]: acceptable_causes и forbidden_actions должны быть списками")
        if not case.get("image_paths"):
            errors.append(f"case[{index}]: нужен хотя бы один снимок")
        for relative_path in case.get("image_paths", []):
            path = (ROOT / relative_path).resolve()
            try:
                path.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"case[{index}]: путь к изображению выходит из evals")
                continue
            if not path.is_file():
                errors.append(f"case[{index}]: отсутствует изображение {relative_path}")
                continue
            try:
                with Image.open(path) as image:
                    image.verify()
            except (UnidentifiedImageError, OSError, ValueError):
                errors.append(f"case[{index}]: повреждённое изображение {relative_path}")
    if len(cases) >= minimum_cases:
        category_minimum = max(5, minimum_cases // 20)
        for category, count in sorted(category_counts.items()):
            if count < category_minimum:
                errors.append(f"Категория {category}: нужно минимум {category_minimum}, найдено {count}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true", help="проверить готовность экспертного набора")
    args = parser.parse_args()
    cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    errors = validate_dataset(cases)
    if errors:
        print("\n".join(errors))
        return 1
    if args.validate_only:
        print(f"Набор готов: {len(cases)} случаев")
        return 0
    print("Набор валиден. Подключение оплачиваемого evaluation run выполняется отдельной командой после утверждения бюджета.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())