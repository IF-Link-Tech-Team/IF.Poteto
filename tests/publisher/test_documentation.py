"""Keep public documentation artifacts tied to the current repository sources."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DocumentationDistributionTests(unittest.TestCase):
    def test_distribution_contains_current_readme_skill_and_alias_help(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "dist"
            subprocess.run([sys.executable, str(root / "scripts/build-poteto-dist"), str(out),
                            "--url", "https://poteto.iflink.tech",
                            "--api-url", "https://deploy.iflink.tech"],
                           check=True, capture_output=True)
            for name in ["README.md", "skills/poteto-deploy/SKILL.md"]:
                self.assertEqual((root / name).read_bytes(), (out / name).read_bytes(), name)
            page = (out / "index.html").read_text()
            self.assertIn('href="/README.md"', page)
            self.assertIn("poteto project alias my-blog", page)
            skill = (out / "skills/poteto-deploy/SKILL.md").read_text()
            self.assertIn("Poteto 0.23.0 或更新版本", skill)
            self.assertIn('poteto deploy "$APP_DIR" --local-build --prod --url-only', skill)
            self.assertIn("运行内存至少 256 MiB", skill)
