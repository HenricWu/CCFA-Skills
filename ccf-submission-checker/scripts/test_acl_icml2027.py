"""Exercise the ACL and ICML 2027 local preflight without external programs."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import check_acl_icml2027 as checker


class PreflightTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tex = self.root / "main.tex"
        self.pdf = self.root / "main.pdf"
        self.pdf.touch()

    def write(self, package, body):
        self.tex.write_text(
            "\\documentclass{article}\n\\usepackage" + package
            + "\n\\begin{document}\n" + body + "\n\\end{document}\n",
            encoding="utf-8",
        )

    def statuses(self, venue, track, stage="review", **kwargs):
        report = checker.audit(venue, track, stage, self.tex, **kwargs)
        return {item["id"]: item["status"] for item in report["checks"]}

    def test_bundled_sources_match_official_manifests(self):
        for venue in ("ACL", "ICML"):
            kit = checker.TEMPLATES / venue / "2027"
            manifest = json.loads((kit / "template-source.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["venue"], venue)
            for item in manifest["files"]:
                with self.subTest(file=item["path"]):
                    data = (kit / item["path"]).read_bytes().replace(b"\r\n", b"\n")
                    self.assertEqual(sha256(data).hexdigest(), item["sha256"])

    def test_acl_reads_nested_section_and_ignores_includegraphics(self):
        (self.root / "limits.tex").write_text(r"\section*{Limitations} No test on dialect shifts.", encoding="utf-8")
        self.write("[review]{acl}", r"\includegraphics{example}\input{limits}\bibliography{refs}")
        status = self.statuses("acl", "short")
        self.assertEqual(status["template_mode"], "pass")
        self.assertEqual(status["source_inputs"], "pass")
        self.assertEqual(status["limitations"], "pass")

    def test_acl_sections_and_modes(self):
        self.write("[preprint]{acl}", r"\bibliography{refs}\section*{Limitations} Limited scope.")
        status = self.statuses("acl", "long")
        self.assertEqual(status["template_mode"], "fail")
        self.assertEqual(status["limitations"], "fail")
        self.write("[final]{acl}", r"\section*{Limitations} TODO\relax\bibliography{refs}")
        final = self.statuses("acl", "long", "camera-ready")
        self.assertEqual(final["template_mode"], "pass")
        self.assertEqual(final["limitations"], "fail")

    def test_icml_disclosure_may_follow_references(self):
        self.write("[nohyperref]{icml2027}", r"""
\section*{Impact Statement} The work has limited scope.
\bibliography{refs}
\section*{AI Disclosure Statement} Routine editing only.
\appendix
""")
        status = self.statuses("icml", "research")
        for key in ("template_mode", "impact_statement", "ai_disclosure", "appendix_order"):
            self.assertEqual(status[key], "pass")

    def test_icml_old_style_and_comment_only_disclosure(self):
        self.write("{icml2026}", r"""
% \section*{AI Use Statement} Comment only.
\bibliography{refs}
\section*{Impact Statement} Too late.
""")
        status = self.statuses("icml", "research")
        self.assertEqual(status["template_mode"], "fail")
        self.assertEqual(status["impact_statement"], "fail")
        self.assertEqual(status["ai_disclosure"], "not_verified")

    def test_conditional_source_needs_rendered_inspection(self):
        self.write("[review]{acl}", r"\iftrue\section*{Limitations} Limited scope.\fi")
        status = self.statuses("acl", "short")
        self.assertEqual(status["source_declarations"], "not_verified")
        self.assertNotIn("limitations", status)

    def test_acl_inspected_boundary_and_review_or_final_cap(self):
        self.write("[review]{acl}", r"\section*{Limitations} Limited scope.\bibliography{refs}")
        info = b"Pages: 9\nPage size: 595.276 x 841.89 pts (A4)\nAuthor: Anonymous\n"
        with patch.object(checker.shutil, "which", return_value="pdfinfo"), \
             patch.object(checker.subprocess, "run") as run:
            run.return_value.stdout = info
            self.assertEqual(self.statuses("acl", "short", pdf=self.pdf)["main_pages"], "not_verified")
            self.assertEqual(self.statuses("acl", "short", pdf=self.pdf, main_pages=4)["main_pages"], "pass")
            self.assertEqual(self.statuses("acl", "short", pdf=self.pdf, main_pages=5)["main_pages"], "fail")
            self.assertEqual(self.statuses("acl", "short", "camera-ready", pdf=self.pdf,
                                           main_pages=5)["main_pages"], "pass")

    def test_icml_final_cap_and_letter_geometry(self):
        self.write("[accepted]{icml2027}", r"\section*{AI Use Statement} Routine edits.\bibliography{refs}")
        info = b"Pages: 10\nPage size: 612 x 792 pts (letter)\n"
        with patch.object(checker.shutil, "which", return_value="pdfinfo"), \
             patch.object(checker.subprocess, "run") as run:
            run.return_value.stdout = info
            status = self.statuses("icml", "research", "camera-ready", pdf=self.pdf, main_pages=9)
            self.assertEqual(status["first_page_size"], "pass")
            self.assertEqual(status["main_pages"], "pass")
            self.assertEqual(self.statuses("icml", "research", pdf=self.pdf, main_pages=9)["main_pages"], "fail")
            self.assertEqual(self.statuses("icml", "research", "camera-ready", pdf=self.pdf,
                                           main_pages=11)["main_pages"], "fail")


if __name__ == "__main__":
    unittest.main()
