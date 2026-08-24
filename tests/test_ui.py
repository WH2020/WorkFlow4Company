from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.inline_scripts = 0
        self.cancel_submitters = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(str(attributes["id"]))
        if tag == "script" and not attributes.get("src"):
            self.inline_scripts += 1
        if (
            tag == "button"
            and attributes.get("type") == "submit"
            and attributes.get("value") == "cancel"
            and "formnovalidate" in attributes
        ):
            self.cancel_submitters += 1


class CompanyWorkbenchUiTests(unittest.TestCase):
    def test_company_library_controls_are_unique_and_csp_compatible(self) -> None:
        html = (ROOT / "ui/index.html").read_text(encoding="utf-8")
        parser = IdCollector()
        parser.feed(html)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertEqual(0, parser.inline_scripts)
        self.assertEqual(4, parser.cancel_submitters)
        for required in {
            "libraryImportButton",
            "librarySearchInput",
            "libraryList",
            "libraryDetail",
            "libraryDialog",
            "libraryFile",
        }:
            self.assertIn(required, parser.ids)
        self.assertIn('lang="zh-CN"', html)
        self.assertIn("公司资料库", html)

    def test_library_ui_uses_controlled_local_api_without_delete_entry(self) -> None:
        script = (ROOT / "ui/app.js").read_text(encoding="utf-8")
        self.assertIn('/api/library/import', script)
        self.assertIn('/api/library/items/${encodeURIComponent(itemId)}/current', script)
        self.assertIn('/api/library/versions/${encodeURIComponent(versionId)}/content', script)
        self.assertIn("X-Company-Session", script)
        self.assertNotIn("data-library-delete", script)
        self.assertIn("detailSequence", script)


if __name__ == "__main__":
    unittest.main()
