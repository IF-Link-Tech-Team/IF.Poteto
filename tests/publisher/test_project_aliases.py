from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from publisher import cli
from publisher.api import create_app
from publisher.config import Settings
from publisher.project_aliases import routes


class ProjectAliasTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(self.root, new_app_domain="page.iflink.tech")
        self.app = create_app(self.settings)
        self.store = self.app.state.store
        owner = self.store.create_account("owner")
        other = self.store.create_account("other")
        self.client = TestClient(self.app, headers={"Authorization": "Bearer " + owner["token"]})
        self.bob = TestClient(self.app, headers={"Authorization": "Bearer " + other["token"]})
        self.addCleanup(self.client.close)
        self.addCleanup(self.bob.close)
        self.project = self.client.post("/v1/projects", json={"name": "site"}).json()["id"]
        self.other = self.bob.post("/v1/projects", json={"name": "site"}).json()["id"]
        self.path = "/v1/projects/" + self.project + "/alias"

    def test_assign_replace_remove_and_reserve_old_names(self):
        default = self.client.get("/v1/projects/" + self.project).json()["url"]
        self.assertIsNone(self.client.get(self.path).json()["alias"])
        first = self.client.put(self.path, json={"alias": "my-blog"})
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual("https://my-blog.page.iflink.tech", first.json()["url"])
        self.assertFalse(first.json()["has_production"])
        self.assertEqual(default, first.json()["default_url"])
        self.assertEqual(first.json(), self.client.put(self.path, json={"alias": "my-blog"}).json())
        self.assertEqual(200, self.client.put(self.path, json={"alias": "my-site"}).status_code)
        for name in ["my-blog", "my-site"]:
            self.assertEqual(409, self.bob.put("/v1/projects/" + self.other + "/alias", json={"alias": name}).status_code)
        self.assertIsNone(self.client.delete(self.path).json()["alias"])
        self.assertEqual(409, self.bob.put("/v1/projects/" + self.other + "/alias", json={"alias": "my-site"}).status_code)
        self.assertEqual(200, self.client.put(self.path, json={"alias": "my-blog"}).status_code)
        self.assertEqual(default, self.client.get("/v1/projects/" + self.project).json()["url"])
        with self.store.connection() as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM project_aliases WHERE active=1").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT count(*) FROM deployments").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT count(*) FROM jobs").fetchone()[0])

    def test_invalid_reserved_and_injected_labels_are_rejected(self):
        for label in ["a", "ab", "a"*49, "A-name", "-name", "name-", "name.test",
                      "admin", "api", "www", "preview-site", "app-site", 'site";']:
            with self.subTest(label=label):
                self.assertEqual(422, self.client.put(self.path, json={"alias": label}).status_code)
        for label in ["a1b", "my-blog", "a"*48]:
            self.assertEqual(200, self.client.put(self.path, json={"alias": label}).status_code)

    def test_owner_scope_required_and_cross_tenant_requests_do_not_leak(self):
        grant = self.client.post("/v1/tokens", json={"name": "preview",
            "scopes": ["deployments:preview"], "project_ids": [self.project]}).json()
        preview = TestClient(self.app, headers={"Authorization": "Bearer " + grant["token"]})
        self.addCleanup(preview.close)
        self.assertEqual(200, preview.get(self.path).status_code)
        self.assertEqual(403, preview.put(self.path, json={"alias": "my-blog"}).status_code)
        self.assertEqual(403, preview.delete(self.path).status_code)
        for method, kwargs in [("get", {}), ("put", {"json": {"alias": "my-blog"}}), ("delete", {})]:
            self.assertEqual(404, getattr(self.bob, method)(self.path, **kwargs).status_code)

    def test_route_follows_project_production_and_never_preview(self):
        self.client.put(self.path, json={"alias": "my-blog"})
        first = self.client.post("/v1/projects/" + self.project + "/deployments",
                                 json={"target": "preview"}).json()["id"]
        second = self.client.post("/v1/projects/" + self.project + "/deployments",
                                  json={"target": "preview"}).json()["id"]
        def current():
            with self.store.connection() as db:
                return routes(db, self.settings)
        self.assertEqual({}, current())
        expected = {"my-blog.page.iflink.tech": "app-" + self.project + ".page.iflink.tech"}
        for release in [first, second, first]:
            with self.store.connection() as db:
                db.execute("UPDATE deployments SET state='ready' WHERE id=?", (release,))
                db.execute("UPDATE projects SET current_deployment=? WHERE id=?", (release, self.project))
            self.assertEqual(expected, current())
        token = self.root / "edge-token"
        token.write_text("synthetic-only")
        with patch.dict(os.environ, {"PAAS_EDGE_TOKEN_FILE": str(token)}):
            inventory = self.client.get("/internal/routes", headers={"X-Paas-Edge-Key": "synthetic-only"})
        self.assertEqual(expected, inventory.json()["aliases"])
        self.assertIn(expected["my-blog.page.iflink.tech"], inventory.json()["hosts"])
        self.client.delete(self.path)
        self.assertEqual({}, current())
        self.client.put(self.path, json={"alias": "my-blog"})
        with self.store.connection() as db:
            db.execute("UPDATE projects SET state='deleted' WHERE id=?", (self.project,))
        self.assertEqual({}, current())


class PortableProjectAliasTests(unittest.TestCase):
    def command(self, arguments, client):
        output = io.StringIO()
        with patch.dict(os.environ, {"POTETO_TOKEN": "test-token", "POTETO_TOKEN_FILE": "",
                                     "PAAS_TOKEN_FILE": "", "POTETO_URL": "https://deploy.example.com"}), \
             patch.object(cli, "_credential_file", return_value=None), \
             patch.object(cli, "find_link", return_value=None), \
             patch.object(cli, "Client", return_value=client), \
             patch("sys.argv", ["poteto", "project", "alias", *arguments]), redirect_stdout(output):
            cli.main()
        return json.loads(output.getvalue())

    def test_set_read_remove_preserve_the_selected_project(self):
        project = "a"*24
        for args, method, body in [
            (["my-blog", "--project", project], "PUT", {"alias": "my-blog"}),
            (["--project", project], "GET", None),
            (["--remove", "--project", project], "DELETE", None),
        ]:
            client = Mock()
            client.request.return_value = {"alias": "my-blog"}
            self.assertEqual("my-blog", self.command(args, client)["alias"])
            self.assertEqual((method, "/v1/projects/" + project + "/alias"),
                             client.request.call_args.args[:2])
            if body:
                self.assertEqual(body, client.request.call_args.args[2])
