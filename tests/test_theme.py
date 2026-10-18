import json
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = ["index.html", "settings.html", "login.html"]

# Runs static/theme.js against a fake document/localStorage, records data-theme after each step.
HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function run(initial, blocked){
  const attrs = {};
  const store = initial ? {'panopticon.theme': initial} : {};
  const listeners = {};
  const localStorage = {
    getItem(k){ if(blocked) throw new Error('blocked'); return k in store ? store[k] : null; },
    setItem(k, v){ if(blocked) throw new Error('blocked'); store[k] = String(v); },
    removeItem(k){ if(blocked) throw new Error('blocked'); delete store[k]; },
  };
  const document = {documentElement: {
    setAttribute(k, v){ attrs[k] = v; }, removeAttribute(k){ delete attrs[k]; },
  }};
  const window = {addEventListener(t, fn){ listeners[t] = fn; }};
  new Function('window', 'document', 'localStorage', src)(window, document, localStorage);
  const steps = {initial: attrs['data-theme'] || null, get: window.panopticonTheme.get()};
  window.panopticonTheme.set('dark');
  steps.afterDark = [attrs['data-theme'] || null, store['panopticon.theme'] || null, window.panopticonTheme.get()];
  window.panopticonTheme.set('system');
  steps.afterSystem = [attrs['data-theme'] || null, store['panopticon.theme'] || null, window.panopticonTheme.get()];
  store['panopticon.theme'] = 'light';
  listeners.storage({key: 'panopticon.theme'});
  steps.afterOtherTab = attrs['data-theme'] || null;
  return steps;
}
console.log(JSON.stringify({
  fresh: run(null, false), light: run('light', false), junk: run('purple', false), blocked: run(null, true),
}));
"""


class ThemePageTests(unittest.TestCase):
    def test_every_page_applies_the_theme_before_the_stylesheet(self):
        # Loaded synchronously ahead of uwp.css so a reload never flashes the other theme.
        for page in PAGES:
            source = (ROOT / "static" / page).read_text(encoding="utf-8")
            head = source.split("</head>")[0]
            self.assertIn('<script src="/static/theme.js"></script>\n<link rel="stylesheet" href="/static/uwp.css">', head, page)

    def test_no_page_hardcodes_the_system_scheme(self):
        # Theme-dependent colours belong in uwp.css's dictionaries, which honour data-theme.
        for page in PAGES:
            source = (ROOT / "static" / page).read_text(encoding="utf-8")
            self.assertNotIn("prefers-color-scheme", source, page)

    def test_light_dictionary_is_identical_for_choice_and_system(self):
        css = (ROOT / "static" / "uwp.css").read_text(encoding="utf-8")
        chosen = re.search(r':root\[data-theme="light"\]\{(.*?)\n\}', css, re.S).group(1)
        system = re.search(r':root:not\(\[data-theme="dark"\]\)\{(.*?)\n  \}', css, re.S).group(1)
        norm = lambda s: [line.strip() for line in s.strip().splitlines()]
        self.assertEqual(norm(chosen), norm(system))

    def test_settings_offers_the_three_choices(self):
        source = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")
        for value in ("light", "dark", "system"):
            self.assertIn(f'<input type="radio" name="theme" value="{value}">', source)


@unittest.skipUnless(shutil.which("node"), "needs node to run theme.js")
class ThemeScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "-e", HARNESS, str(ROOT / "static" / "theme.js")],
                             capture_output=True, text=True, timeout=30, check=True)
        cls.r = json.loads(out.stdout)

    def test_default_follows_the_system(self):
        self.assertIsNone(self.r["fresh"]["initial"])
        self.assertEqual(self.r["fresh"]["get"], "system")

    def test_stored_choice_is_applied(self):
        self.assertEqual(self.r["light"]["initial"], "light")

    def test_unknown_value_falls_back_to_system(self):
        self.assertIsNone(self.r["junk"]["initial"])
        self.assertEqual(self.r["junk"]["get"], "system")

    def test_choice_is_stored_and_system_clears_it(self):
        self.assertEqual(self.r["fresh"]["afterDark"], ["dark", "dark", "dark"])
        self.assertEqual(self.r["fresh"]["afterSystem"], [None, None, "system"])

    def test_other_tab_change_is_followed(self):
        self.assertEqual(self.r["fresh"]["afterOtherTab"], "light")

    def test_blocked_storage_still_switches_this_page(self):
        self.assertEqual(self.r["blocked"]["afterDark"], ["dark", None, "dark"])


if __name__ == "__main__":
    unittest.main()
