import re
from html.parser import HTMLParser
from pathlib import Path

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
QUOTED_KEY = re.compile(r'"((?:\\.|[^"\\])*)"\s*:')
JS_STRING = re.compile(r'"((?:\\.|[^"\\])*)"')


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: set[str] = set()
        self.ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        for name, value in attrs:
            if name in {"placeholder", "aria-label", "title"} and value and CYRILLIC.search(value):
                self.values.add(value.strip())

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data):
        value = " ".join(data.split())
        if not self.ignored_depth and value and CYRILLIC.search(value):
            self.values.add(value)


def _dictionary_keys(source: str, start: str, end: str) -> set[str]:
    section = source[source.index(start) + len(start):source.index(end)]
    return {bytes(value, "utf-8").decode("unicode_escape") if "\\" in value else value
            for value in QUOTED_KEY.findall(section)}


def test_all_visible_ru_strings_have_english_and_latvian_translations():
    i18n = Path("web/i18n.js").read_text(encoding="utf-8")
    en = _dictionary_keys(i18n, "en: {", "\n    lv: {")
    lv = _dictionary_keys(i18n, "lv: {", "\n    }\n  };")

    required: set[str] = set()
    for html_path in (
        Path("web/index.html"), Path("web/privacy.html"),
        Path("web/terms.html"), Path("web/b2b-terms.html"),
    ):
        parser = VisibleTextParser()
        parser.feed(html_path.read_text(encoding="utf-8"))
        required.update(parser.values)
    app_js = Path("web/app.js").read_text(encoding="utf-8")
    required.update(value for value in JS_STRING.findall(app_js) if CYRILLIC.search(value))

    missing_en = sorted(required - en)
    missing_lv = sorted(required - lv)
    assert not missing_en, f"Missing English translations: {missing_en}"
    assert not missing_lv, f"Missing Latvian translations: {missing_lv}"
