"""A test can never write to the live app's data (22 Nov: tools/test_fork.py, run from a shell the app had started,
wrote 153 test chats into the user's list and replaced the live connections; another run did it again at 14:00).

    python -X utf8 tools/test_testhome.py

Held here (each case in a process of its own; the "live" folder is a path that is never created):
- harness/config.py stops a test process at the import when its data folder is neither a temp folder nor a test
  home: a tools/test_*.py script, and anything started with KCH_TEST set (a copy of the app a test runs);
- KCH_DATA cannot route around it (KCH_HOME wins in config.py, and the guard looks at the folder actually used);
- config.load() checks again, for a test that points config.DATA elsewhere after the import;
- a temp folder and a marked test home are allowed; the app itself (not a test) is not affected;
- tools/testhome.py gives a test a fresh temp KCH_HOME, removes KCH_DATA, sets KCH_TEST, and keeps a KCH_HOME that is
  already a temp folder or a test home;
- every tools/test_*.py imports tools/testhome.py before anything from harness."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)

TOOLS = Path(__file__).resolve().parent
APP = TOOLS.parent
LIVE = APP / "tools" / "_never_created_live_home"        # outside the temp folder, like the user's own data folder
SCRATCH = Path(tempfile.mkdtemp(prefix="kch-testhome-"))


def run(code, env, script_name="snippet.py"):
    """Run code as a script called script_name (in a temp folder), from the app folder, with env. -> (rc, output)"""
    f = SCRATCH / script_name
    f.write_text("import sys\nsys.path.insert(0, %r)\n" % str(APP) + code, encoding="utf-8")
    e = {k: v for k, v in os.environ.items() if not k.startswith(("KCH_", "RAVEN_", "NEST_", "FLOCK_"))}
    e.update(env)
    p = subprocess.run([sys.executable, "-X", "utf8", str(f)], cwd=str(APP), env=e, capture_output=True, text=True,
                       timeout=60)
    return p.returncode, (p.stdout + p.stderr)


class TestHome(unittest.TestCase):
    def tearDown(self):
        self.assertFalse(LIVE.exists(), "nothing was written to the 'live' folder")

    def test_a_test_script_on_the_live_folder_stops_at_the_import(self):
        rc, out = run("from harness import config\nprint('IMPORTED')\n", {"KCH_HOME": str(LIVE)}, "test_fake.py")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("IMPORTED", out)
        self.assertIn("may not use the data folder", out)

    def test_kch_test_covers_a_process_a_test_starts(self):
        rc, out = run("from harness import config\nprint('IMPORTED')\n", {"KCH_HOME": str(LIVE), "KCH_TEST": "1"},
                      "run.py")
        self.assertNotEqual(rc, 0)
        self.assertIn("may not use the data folder", out)

    def test_kch_data_does_not_route_around_it(self):
        rc, out = run("from harness import config\nprint('IMPORTED')\n",
                      {"KCH_HOME": str(LIVE), "KCH_DATA": str(SCRATCH / "d"), "KCH_TEST": "1"}, "test_fake.py")
        self.assertNotEqual(rc, 0, "KCH_HOME wins, and it is the live folder")
        rc, out = run("from harness import config\nprint('IMPORTED')\n", {"KCH_DATA": str(LIVE), "KCH_TEST": "1"},
                      "test_fake.py")
        self.assertNotEqual(rc, 0, "KCH_DATA alone on the live folder stops too")

    def test_load_checks_again(self):
        code = ("from harness import config\nfrom pathlib import Path\nconfig.DATA = Path(%r)\nconfig.load()\n"
                "print('LOADED')\n" % str(LIVE))
        rc, out = run(code, {"KCH_HOME": str(SCRATCH / "h1"), "KCH_TEST": "1"}, "test_fake.py")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("LOADED", out)

    def test_temp_folder_and_marked_home_are_allowed(self):
        rc, out = run("from harness import config\nconfig.load()\nprint('OK', config.DATA)\n",
                      {"KCH_HOME": str(SCRATCH / "h2"), "KCH_TEST": "1"}, "test_fake.py")
        self.assertEqual(rc, 0, out)
        self.assertIn("OK", out)
        marked = APP / "tools" / "_marked_test_home"
        try:
            testhome.mark(marked)
            rc, out = run("from harness import config\nprint('OK')\n", {"KCH_HOME": str(marked), "KCH_TEST": "1"},
                          "test_fake.py")
            self.assertEqual(rc, 0, out)
        finally:
            shutil.rmtree(marked, ignore_errors=True)

    def test_the_app_itself_is_not_affected(self):
        app_home = APP / "tools" / "_app_home_for_this_test"     # the app may use (and create) its own folder
        try:
            rc, out = run("from harness import config\nprint('IMPORTED', config.DATA)\n", {"KCH_HOME": str(app_home)},
                          "run.py")
            self.assertEqual(rc, 0, out)
            self.assertIn("IMPORTED", out)
        finally:
            shutil.rmtree(app_home, ignore_errors=True)

    def test_testhome_gives_a_fresh_temp_home(self):
        code = ("import sys, os\nsys.path.insert(0, %r)\nimport testhome\n"
                "print('HOME', os.environ['KCH_HOME']); print('DATA', os.environ.get('KCH_DATA')); "
                "print('TEST', os.environ.get('KCH_TEST'))\nfrom harness import config\nprint('CFG', config.DATA)\n"
                % str(TOOLS))
        rc, out = run(code, {"KCH_HOME": str(LIVE), "KCH_DATA": str(LIVE)}, "test_fake.py")
        self.assertEqual(rc, 0, out)
        home = re.search(r"HOME (.+)", out).group(1).strip()
        self.assertTrue(testhome.ok(home), home)
        self.assertNotEqual(Path(home), LIVE)
        self.assertIn("DATA None", out)
        self.assertIn("TEST 1", out)
        self.assertIn("CFG " + home, out)
        keep = SCRATCH / "already-temp"
        rc, out = run(code, {"KCH_HOME": str(keep)}, "test_fake.py")
        self.assertIn("HOME " + str(keep), out, "a temp KCH_HOME the test chose is kept")

    def test_every_test_imports_testhome_before_harness(self):
        missing = []
        for f in sorted(TOOLS.glob("test_*.py")):
            lines = f.read_text(encoding="utf-8").splitlines()
            th = next((i for i, ln in enumerate(lines) if re.match(r"import testhome\b", ln)), None)
            hz = next((i for i, ln in enumerate(lines) if re.match(r"(from harness|import harness)\b", ln)), None)
            if th is None or (hz is not None and hz < th):
                missing.append(f.name)
        self.assertEqual(missing, [], "these tests reach harness without tools/testhome.py first")


if __name__ == "__main__":
    unittest.main(verbosity=2)
