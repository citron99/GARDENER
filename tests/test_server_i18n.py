import ast
import re
from pathlib import Path

from app.i18n import translate_http_error

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _literal_http_errors():
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "HTTPException" or len(node.args) < 2:
                continue
            status_node, detail_node = node.args[:2]
            if (
                isinstance(status_node, ast.Constant)
                and isinstance(status_node.value, int)
                and isinstance(detail_node, ast.Constant)
                and isinstance(detail_node.value, str)
            ):
                yield status_node.value, detail_node.value


def test_all_literal_russian_http_errors_have_non_cyrillic_lv_en_output():
    errors = list(_literal_http_errors())
    assert errors
    for status_code, message in errors:
        if not CYRILLIC.search(message):
            continue
        assert not CYRILLIC.search(translate_http_error(message, "en", status_code))
        assert not CYRILLIC.search(translate_http_error(message, "lv", status_code))


def test_unknown_error_uses_status_specific_fallback():
    assert translate_http_error("Неизвестная конфликтная ошибка", "en", 409) == (
        "The request conflicts with the current state")
    assert translate_http_error("Неизвестная конфликтная ошибка", "lv", 409) == (
        "Pieprasījums konfliktē ar pašreizējo stāvokli")
