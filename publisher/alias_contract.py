"""Portable rules for project names under the platform's application domain."""
import re

RESERVED = {"admin", "api", "app", "auth", "deploy", "mail", "poteto", "preview",
            "status", "support", "www"}


def valid_alias(value):
    return (isinstance(value, str)
            and re.fullmatch(r"[a-z][a-z0-9-]{1,46}[a-z0-9]", value) is not None
            and value not in RESERVED
            and not value.startswith(("app-", "preview-")))
