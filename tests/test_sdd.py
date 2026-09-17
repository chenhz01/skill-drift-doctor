"""Test suite for skill-drift-doctor (stdlib unittest, no pip deps)."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
CLI = HERE.parent / "skill_drift_doctor.py"


def run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True, text=True, cwd=cwd, encoding="utf-8")


def copy_tree(src: Path, dst: Path):
    shutil.copytree(src, dst)


class TestCheck(unittest.TestCase):
    def test_healthy_skill_passes(self):
        r = run_cli("check", str(FIXTURES / "healthy"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[OK ] healthy", r.stdout)

    def test_missing_frontmatter_fails(self):
        with tempfile.TemporaryDirectory() as td:
            sk = Path(td) / "broken"
            sk.mkdir()
            (sk / "SKILL.md").write_text("# no frontmatter", encoding="utf-8")
            r = run_cli("check", td)
            self.assertEqual(r.returncode, 1)
            self.assertIn("frontmatter", r.stdout)

    def test_name_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as td:
            sk = Path(td) / "dirname"
            sk.mkdir()
            (sk / "SKILL.md").write_text(
                "---\nname: othername\ndescription: A description long enough to be valid.\n---\nbody",
                encoding="utf-8")
            r = run_cli("check", td)
            self.assertEqual(r.returncode, 0)  # warn only
            self.assertIn("WARN", r.stdout)

    def test_description_too_long_fails(self):
        with tempfile.TemporaryDirectory() as td:
            sk = Path(td) / "toolong"
            sk.mkdir()
            (sk / "SKILL.md").write_text(
                "---\nname: toolong\ndescription: " + "x" * 1100 + "\n---\nbody",
                encoding="utf-8")
            r = run_cli("check", td)
            self.assertEqual(r.returncode, 1)


class TestRefs(unittest.TestCase):
    def test_missing_referenced_script_fails(self):
        r = run_cli("refs", str(FIXTURES / "drifted"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("scripts/gone.py", r.stdout)

    def test_existing_reference_ok(self):
        r = run_cli("refs", str(FIXTURES / "healthy"))
        self.assertEqual(r.returncode, 0)


class TestIntegrity(unittest.TestCase):
    def _make_skill(self, td: Path) -> Path:
        sk = td / "guard"
        sk.mkdir()
        (sk / "SKILL.md").write_text(
            "---\nname: guard\ndescription: integrity fixture for baseline tests.\n---\n"
            "# guard\n\nhuman text v1\n",
            encoding="utf-8")
        (sk / "notes.md").write_text("author notes v1\n", encoding="utf-8")
        return sk

    def test_untouched_tree_passes_verify(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_skill(root)
            self.assertEqual(run_cli("baseline", str(root)).returncode, 0)
            r = run_cli("verify", str(root))
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("byte-identical", r.stdout)

    def test_human_file_modified_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sk = self._make_skill(root)
            run_cli("baseline", str(root))
            (sk / "SKILL.md").write_text(
                "---\nname: guard\ndescription: integrity fixture for baseline tests.\n---\n"
                "# guard REWRITTEN BY UPGRADE\n\nhuman text v2\n",
                encoding="utf-8")
            r = run_cli("verify", str(root))
            self.assertEqual(r.returncode, 1)
            self.assertIn("modified since baseline", r.stdout)

    def test_deleted_file_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sk = self._make_skill(root)
            run_cli("baseline", str(root))
            (sk / "notes.md").unlink()
            r = run_cli("verify", str(root))
            self.assertEqual(r.returncode, 1)
            self.assertIn("deleted", r.stdout)

    def test_injected_block_edit_is_violation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sk = root / "injected"
            shutil.copytree(FIXTURES / "injected", sk)
            run_cli("baseline", str(root))
            text = (sk / "SKILL.md").read_text(encoding="utf-8")
            tampered = text.replace("appended by an agent harness", "TAMPERED by an agent harness")
            (sk / "SKILL.md").write_text(tampered, encoding="utf-8")
            r = run_cli("verify", str(root))
            self.assertEqual(r.returncode, 1)
            self.assertIn("append-only violation", r.stdout)

    def test_injected_block_append_only_growth_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sk = root / "injected"
            shutil.copytree(FIXTURES / "injected", sk)
            run_cli("baseline", str(root))
            text = (sk / "SKILL.md").read_text(encoding="utf-8")
            grown = text.replace("*End of injected block*",
                                 "additional appended line\n\n*End of injected block*")
            (sk / "SKILL.md").write_text(grown, encoding="utf-8")
            r = run_cli("verify", str(root))
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("append-only growth", r.stdout)


class TestReport(unittest.TestCase):
    def test_report_json_summary(self):
        r = run_cli("report", str(FIXTURES), "--json")
        # drifted fixture carries a refs failure -> exit 1 is the correct signal
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        data = json.loads(r.stdout)
        self.assertIn("summary", data)
        self.assertGreaterEqual(data["summary"]["skills"], 3)
        self.assertEqual(data["summary"]["fail"], 1)

    def test_full_report_text(self):
        r = run_cli("report", str(FIXTURES))
        self.assertIn("skill-drift-doctor", r.stdout)
        self.assertIn("drifted", r.stdout)


if __name__ == "__main__":
    unittest.main()
