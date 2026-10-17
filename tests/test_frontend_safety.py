import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FrontendSafetyTests(unittest.TestCase):
    # Remote fields (machine names, errors, URLs, target names, disk paths) must never be
    # interpolated into HTML without escapeHtml / escapeAttr.
    RAW_REMOTE = re.compile(r"\$\{(p|a|h|t|q|s|r)\.(name|error|url|display_name|id|ip|panel_name|ssh_user|path|status)\b")

    def test_dashboard_escapes_remote_text_before_rendering(self):
        source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("escapeHtml(p.name || '未命名面板')", source)
        self.assertIn("${escapeHtml(p.url", source)
        self.assertIn("data-panel=\"${escapeAttr(panelName)}\"", source)
        self.assertIsNone(self.RAW_REMOTE.search(source))

    def test_settings_escapes_remote_text_before_rendering(self):
        source = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")
        self.assertIsNone(self.RAW_REMOTE.search(source))

    def test_settings_uses_data_attribute_not_inline_credential_javascript(self):
        source = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")
        self.assertIn("data-passkey-id=\"${escapeAttr(p.credential_id)}\"", source)
        self.assertNotIn("onclick=\"deletePasskey('${p.credential_id}')\"", source)

    def test_save_only_collects_panel_rows(self):
        source = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")
        self.assertIn("document.querySelectorAll('#panels .panel-card')", source)
        self.assertNotIn("document.querySelectorAll('.panel-card')", source)


if __name__ == "__main__":
    unittest.main()
