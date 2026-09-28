import argparse
import csv
import json
import shutil
from pathlib import Path

from evals.run_evaluation import validate_dataset

ROOT = Path(__file__).resolve().parent
LIST_FIELDS = {"image_paths", "acceptable_causes", "forbidden_actions"}


def _read(path: Path) -> list[dict]:
    suffix = path.suffix.casefold()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("JSON должен содержать массив случаев")
        return payload
    if suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            for field in LIST_FIELDS:
                row[field] = [item.strip() for item in (row.get(field) or "").split(";") if item.strip()]
            row["expert_reviewed"] = str(row.get("expert_reviewed", "")).casefold() in {"1", "true", "yes"}
        return rows
    raise ValueError("Поддерживаются только .json, .jsonl и .csv")


def import_cases(source: Path, images_root: Path, output: Path) -> int:
    cases = _read(source)
    destination = ROOT / "images"
    destination.mkdir(parents=True, exist_ok=True)
    for case in cases:
        copied = []
        for relative in case.get("image_paths", []):
            source_image = (images_root / relative).resolve()
            source_image.relative_to(images_root.resolve())
            if not source_image.is_file():
                raise FileNotFoundError(source_image)
            target = destination / f"{case['id']}-{len(copied) + 1}{source_image.suffix.casefold()}"
            shutil.copy2(source_image, target)
            copied.append(target.relative_to(ROOT).as_posix())
        case["image_paths"] = copied
    errors = validate_dataset(cases)
    if errors:
        raise ValueError("\n".join(errors))
    output.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(cases)


def main() -> int:
    parser = argparse.ArgumentParser(description="Импорт утверждённого экспертного eval-набора")
    parser.add_argument("source", type=Path)
    parser.add_argument("--images-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "cases.json")
    args = parser.parse_args()
    try:
        count = import_cases(args.source.resolve(), args.images_root.resolve(), args.output.resolve())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(str(exc))
        return 1
    print(f"Импортировано {count} проверенных случаев в {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())