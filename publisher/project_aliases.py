"""Project-owned platform aliases; released names cannot change tenants."""
import time

from fastapi import Depends
from pydantic import Field, field_validator

from publisher.alias_contract import valid_alias
from publisher.models import Input
from publisher.store import Problem


SCHEMA = """
CREATE TABLE IF NOT EXISTS project_aliases (
 alias TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 active INTEGER NOT NULL, updated REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_project_alias
 ON project_aliases(project_id) WHERE active=1;
"""


class AliasInput(Input):
    alias: str = Field(min_length=3, max_length=48)

    @field_validator("alias")
    @classmethod
    def check_alias(cls, value):
        if not valid_alias(value):
            raise ValueError("Use 3-48 lowercase letters, digits or hyphens; start with a letter, end with a letter or digit; system names are reserved")
        return value


def view(db, store, project):
    row = db.execute("SELECT alias FROM project_aliases WHERE project_id=? AND active=1",
                     (project["id"],)).fetchone()
    alias = row["alias"] if row else None
    domain = store.settings.new_app_domain
    return {"project_id": project["id"], "alias": alias,
            "url": f"https://{alias}.{domain}" if alias and domain else None,
            "default_url": "https://" + store.settings.hostname(project["id"], domain=project["app_domain"]),
            "has_production": project["current_deployment"] is not None}


def register(app, store, actor):
    base = "/v1/projects/{project_id}/alias"

    @app.get(base, tags=["Projects"])
    def get_alias(project_id: str, user=Depends(actor)):
        with store.connection() as db:
            project = store.project(db, user, project_id, allow_preview=True)
            return view(db, store, project)

    @app.put(base, tags=["Projects"])
    def set_alias(project_id: str, body: AliasInput, user=Depends(actor)):
        with store.connection() as db:
            project = store.project(db, user, project_id, "projects:write")
            if not store.settings.new_app_domain:
                raise Problem(503, "Platform application aliases are not configured")
            previous = db.execute("SELECT project_id FROM project_aliases WHERE alias=?",
                                  (body.alias,)).fetchone()
            if previous and previous["project_id"] != project_id:
                raise Problem(409, "This alias is already reserved; choose another name")
            db.execute("UPDATE project_aliases SET active=0,updated=? WHERE project_id=? AND active=1",
                       (time.time(), project_id))
            db.execute("INSERT INTO project_aliases VALUES(?,?,1,?) ON CONFLICT(alias) DO UPDATE SET active=1,updated=excluded.updated",
                       (body.alias, project_id, time.time()))
            store.audit(db, user, "project.alias-set", project_id)
            return view(db, store, project)

    @app.delete(base, tags=["Projects"])
    def remove_alias(project_id: str, user=Depends(actor)):
        with store.connection() as db:
            project = store.project(db, user, project_id, "projects:write")
            db.execute("UPDATE project_aliases SET active=0,updated=? WHERE project_id=? AND active=1",
                       (time.time(), project_id))
            store.audit(db, user, "project.alias-remove", project_id)
            return view(db, store, project)


def routes(db, settings):
    if not settings.new_app_domain:
        return {}
    return {row["alias"] + "." + settings.new_app_domain:
            settings.hostname(row["id"], domain=row["app_domain"])
            for row in db.execute(
                "SELECT a.alias,p.id,p.app_domain FROM project_aliases a JOIN projects p ON p.id=a.project_id "
                "JOIN deployments d ON d.id=p.current_deployment AND d.project_id=p.id "
                "WHERE a.active=1 AND p.state='active' AND d.state IN ('ready','activating')")}
