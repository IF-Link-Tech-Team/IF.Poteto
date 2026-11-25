"""Keep public documentation artifacts tied to the current repository sources."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from publisher.cli_tools import VERSION


class DocumentationDistributionTests(unittest.TestCase):
    def test_distribution_contains_current_readme_skill_and_alias_help(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "dist"
            subprocess.run([sys.executable, str(root / "scripts/build-poteto-dist"), str(out),
                            "--url", "https://poteto.iflink.tech",
                            "--api-url", "https://deploy.iflink.tech"],
                           check=True, capture_output=True)
            readme = (out / "README.md").read_text()
            release = f"\nCLI release: **{VERSION}**.\n"
            self.assertEqual(1, readme.count(release))
            self.assertEqual((root / "README.md").read_text(), readme.replace(release, "", 1))
            self.assertNotIn("Poteto 0.23.0", readme)
            self.assertNotIn("They do not run source builds", readme)
            self.assertIn("With `platform_builds` enabled, they also accept Node, Dockerfile and static source", readme)
            name = "skills/poteto-deploy/SKILL.md"
            self.assertEqual((root / name).read_bytes(), (out / name).read_bytes(), name)
            page = (out / "index.html").read_text()
            self.assertIn('href="/README.md"', page)
            self.assertIn("poteto project alias my-blog", page)
            skill = (out / "skills/poteto-deploy/SKILL.md").read_text()
            self.assertNotIn("Poteto 0.23.0", skill)
            self.assertIn("Poteto 0.25.0 或更新版本", skill)
            self.assertIn('poteto deploy "$APP_DIR" --local-build --prod --url-only', skill)
            self.assertIn("运行内存至少 256 MiB", skill)
