import json
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = ["index.html", "login.html"]

# Runs static/theme.js against a fake document/localStorage, records data-theme after each step.
HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function run(initial, blocked){
  const attrs = {};
  const store = initial ? {'panopticon.theme': initial} : {};
  if(initial === 'light'){
    store['panopticon.accents'] = JSON.stringify({tile: '#4455dd'});
    store['panopticon.scale'] = '125';
  }
  if(initial === 'purple') store['panopticon.scale'] = '9000';
  const listeners = {};
  const localStorage = {
    getItem(k){ if(blocked) throw new Error('blocked'); return k in store ? store[k] : null; },
    setItem(k, v){ if(blocked) throw new Error('blocked'); store[k] = String(v); },
    removeItem(k){ if(blocked) throw new Error('blocked'); delete store[k]; },
  };
  const props = {};
  const document = {documentElement: {
    setAttribute(k, v){ attrs[k] = v; }, removeAttribute(k){ delete attrs[k]; },
    style: {setProperty(k, v){ props[k] = v; }, removeProperty(k){ delete props[k]; }},
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
  steps.cachedAccents = Object.assign({}, props);
  steps.initialZoom = document.documentElement.style.zoom || null;
  window.panopticonTheme.setScale(150);
  steps.after150 = [document.documentElement.style.zoom, store['panopticon.scale'] || null, window.panopticonTheme.getScale()];
  window.panopticonTheme.setScale(333);
  steps.afterJunkScale = [document.documentElement.style.zoom || null, store['panopticon.scale'] || null, window.panopticonTheme.getScale()];
  window.panopticonTheme.setAccents({tile: '#FFDD44', curve: 'red', bar: '#000080'});
  steps.afterAccents = [Object.assign({}, props), store['panopticon.accents'] || null];
  return steps;
}
console.log(JSON.stringify({
  fresh: run(null, false), light: run('light', false), junk: run('purple', false), blocked: run(null, true),
}));
"""


class ThemePageTests(unittest.TestCase):
    def test_surfaces_read_their_own_colour_tokens(self):
        css = (ROOT / "static" / "uwp.css").read_text(encoding="utf-8")
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("background:var(--BarAccent);color:var(--BarForeground)", css)
        self.assertIn(".tile.state-ok{--tile-bg:var(--TileAccent);color:var(--TileForeground)}", page)
        self.assertIn('stroke="var(--CurveAccent)"', page)

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
        source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
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

    def test_cached_accents_paint_before_the_server_answers(self):
        self.assertEqual(self.r["light"]["cachedAccents"], {"--TileAccent": "#4455dd", "--TileForeground": "#ffffff"})
        self.assertEqual(self.r["fresh"]["cachedAccents"], {})

    def test_accents_pick_a_readable_foreground_and_drop_junk(self):
        props, cached = self.r["fresh"]["afterAccents"]
        # Light yellow takes black text, navy white; "red" is not #rrggbb and falls back to the default.
        self.assertEqual(props, {"--TileAccent": "#ffdd44", "--TileForeground": "#000000",
                                 "--BarAccent": "#000080", "--BarForeground": "#ffffff"})
        self.assertEqual(json.loads(cached), {"tile": "#ffdd44", "bar": "#000080"})

    def test_stored_scale_is_applied_and_junk_ignored(self):
        self.assertEqual(self.r["light"]["initialZoom"], "1.25")
        self.assertIsNone(self.r["junk"]["initialZoom"])
        self.assertIsNone(self.r["fresh"]["initialZoom"])

    def test_scale_is_stored_and_100_clears_it(self):
        self.assertEqual(self.r["fresh"]["after150"], ["1.5", "150", 150])
        self.assertEqual(self.r["fresh"]["afterJunkScale"], [None, None, 100])

    def test_blocked_storage_still_scales_this_page(self):
        self.assertEqual(self.r["blocked"]["after150"], ["1.5", None, 150])

    def test_width_breakpoints_follow_the_zoomed_shell(self):
        # @media reads the unzoomed window, so inside the shell they must be container queries.
        css = (ROOT / "static" / "uwp.css").read_text(encoding="utf-8")
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn(".shell{container-type:inline-size}", css)
        self.assertNotIn("100vh", css)
        self.assertNotRegex(page, r"@media \((max|min)-width")

    def test_blocked_storage_still_applies_accents(self):
        props, cached = self.r["blocked"]["afterAccents"]
        self.assertEqual(props["--TileAccent"], "#ffdd44")
        self.assertIsNone(cached)


if __name__ == "__main__":
    unittest.main()
