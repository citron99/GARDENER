from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

EXCLUDED_DIRECTORIES = {
    ".agents",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "backups",
    "e2e_uploads",
    "node_modules",
    "playwright-report",
    "test-results",
    "test_uploads",
    "uploads",
}
EXCLUDED_NAMES = {".env"}
EXCLUDED_SUFFIXES = {".db", ".pyc", ".pyo", ".zip"}
MANIFEST_NAME = "RELEASE_MANIFEST.sha256"


def _is_included(path: Path, source: Path) -> bool:
    relative = path.relative_to(source)
    if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
        return False
    if path.name in EXCLUDED_NAMES or path.name == MANIFEST_NAME:
        return False
    return path.suffix.lower() not in EXCLUDED_SUFFIXES


def release_files(source: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in source.rglob("*")
            if path.is_file() and _is_included(path, source)
        ),
        key=lambda path: path.relative_to(source).as_posix(),
    )


def _zip_info(name: str) -> ZipInfo:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def build_archive(source: Path, output: Path) -> tuple[int, str]:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    if output == source or source in output.parents:
        raise ValueError("Release archive must be created outside the source directory")

    files = release_files(source)
    manifest_lines: list[str] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with ZipFile(temporary, "w") as archive:
            for path in files:
                relative = PurePosixPath(path.relative_to(source).as_posix())
                content = path.read_bytes()
                archive.writestr(_zip_info(str(relative)), content)
                manifest_lines.append(f"{sha256(content).hexdigest()}  {relative}")
            manifest = ("\n".join(manifest_lines) + "\n").encode("utf-8")
            archive.writestr(_zip_info(MANIFEST_NAME), manifest)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(files), sha256(output.read_bytes()).hexdigest()


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Create a clean, deterministic AI Garden release ZIP."
    )
    parser.add_argument("--source", type=Path, default=project_root)
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root.parent / f"{project_root.name}_clean.zip",
    )
    args = parser.parse_args()
    count, digest = build_archive(args.source, args.output)
    print(f"Created {args.output.resolve()} with {count} files")
    print(f"SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
