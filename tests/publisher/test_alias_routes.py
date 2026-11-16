import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from publisher.origin_routes import alias_map, sync


class AliasRouteTests(unittest.TestCase):
    def test_alias_uses_existing_production_route_and_rejects_injection(self):
        target = "app-" + "a"*24 + ".old.example"
        hosts = [target, "preview-" + "b"*24 + ".page.iflink.tech"]
        rendered = alias_map(hosts, "page.iflink.tech", {"my-blog.page.iflink.tech": target})
        self.assertIn('"my-blog.page.iflink.tech" "' + target + '";', rendered)
        self.assertIn('"app-' + "a"*24 + '.page.iflink.tech" "' + target + '";', rendered)
        for aliases in [
            {"my-blog.other.example": target},
            {"app-admin.page.iflink.tech": target},
            {'bad";.page.iflink.tech': target},
            {"my-blog.page.iflink.tech": hosts[1]},
            {"my-blog.page.iflink.tech": "app-" + "c"*24 + ".old.example"},
        ]:
            with self.assertRaises(ValueError):
                alias_map(hosts, "page.iflink.tech", aliases)

    def test_sync_removes_old_alias_and_keeps_last_good_on_reload_failure(self):
        target = "app-" + "a"*24 + ".old.example"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "conf").mkdir()
            (root / "token").write_text("test-only")
            (root / "config.json").write_text(json.dumps({
                "inventory_url": "http://127.0.0.1/routes", "inventory_host": "api.test",
                "inventory_token_file": str(root / "token"), "app_domain": "page.iflink.tech",
            }))
            destination = root / "conf/apps.map"
            previous = alias_map([target], "page.iflink.tech", {"old-blog.page.iflink.tech": target})
            destination.write_text(previous)
            payload = json.dumps({"hosts": [target], "aliases": {"new-blog.page.iflink.tech": target}}).encode()
            with patch("publisher.origin_routes.urlopen", return_value=io.BytesIO(payload)), \
                 patch("publisher.origin_routes.subprocess.run", side_effect=subprocess.CalledProcessError(1, "nginx")):
                with self.assertRaises(subprocess.CalledProcessError):
                    sync(root)
            self.assertEqual(previous, destination.read_text())
            with patch("publisher.origin_routes.urlopen", return_value=io.BytesIO(payload)), \
                 patch("publisher.origin_routes.subprocess.run"):
                sync(root)
            self.assertNotIn("old-blog", destination.read_text())
            self.assertIn("new-blog", destination.read_text())
