"""Refresh a bounded nginx alias map from the authenticated route inventory."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.request import Request, urlopen

from publisher.config import valid_domain
from publisher.alias_contract import valid_alias


def alias_map(hosts, domain, aliases=None):
    if not valid_domain(domain) or not isinstance(hosts, list) or len(hosts) > 10000:
        raise ValueError("Invalid origin route inventory")
    routes = {}
    for host in hosts:
        if not isinstance(host, str):
            raise ValueError("Invalid route hostname")
        match = re.fullmatch(r"((?:app|preview)-[a-f0-9]{24})\.(.+)", host)
        if not match or not valid_domain(match[2]):
            raise ValueError("Invalid route hostname")
        alias = match[1] + "." + domain
        if alias in routes and routes[alias] != host:
            raise ValueError("An application alias has multiple upstream hosts")
        routes[alias] = host
    if aliases is not None:
        if not isinstance(aliases, dict) or len(aliases) > 10000:
            raise ValueError("Invalid project alias inventory")
        for alias, host in aliases.items():
            suffix = "." + domain
            if (not isinstance(alias, str) or not alias.endswith(suffix)
                    or not valid_alias(alias[:-len(suffix)])
                    or not isinstance(host, str)
                    or not re.fullmatch(r"app-[a-f0-9]{24}\.(.+)", host)
                    or not valid_domain(host) or host not in hosts):
                raise ValueError("Invalid project alias route")
            routes[alias] = host
    return "".join(f'    "{alias}" "{host}";\n' for alias, host in sorted(routes.items()))


def sync(root, reload=True):
    settings = json.loads((root / "config.json").read_text())
    request = Request(settings["inventory_url"], headers={
        "Host": settings["inventory_host"],
        "X-Paas-Edge-Key": Path(settings["inventory_token_file"]).read_text().strip(),
    })
    with urlopen(request, timeout=15) as response:
        data = response.read(1024**2 + 1)
    if len(data) > 1024**2:
        raise ValueError("Origin route inventory is too large")
    inventory = json.loads(data)
    rendered = alias_map(inventory["hosts"], settings["app_domain"], inventory.get("aliases"))
    destination = root / "conf" / "apps.map"
    previous = destination.read_text() if destination.exists() else None
    if rendered == previous:
        return
    pending = destination.with_suffix(".next")
    pending.write_text(rendered)
    pending.chmod(0o644)
    pending.replace(destination)
    if reload:
        try:
            command = ["docker", "exec", "poteto-origin", "nginx", "-c", str(root / "conf" / "nginx.conf")]
            subprocess.run(command + ["-t"], check=True, capture_output=True)
            subprocess.run(command + ["-s", "reload"], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            if previous is not None:
                pending.write_text(previous)
                pending.chmod(0o644)
                pending.replace(destination)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/srv/poteto-origin"))
    parser.add_argument("--initial", action="store_true", help="Write the initial map before nginx starts")
    args = parser.parse_args()
    if args.initial:
        sync(args.root, reload=False)
        return
    while True:
        try:
            sync(args.root)
        except Exception as error:
            detail = getattr(error, "stderr", b"") or b""
            print("Origin route sync failed: " + type(error).__name__ + " " +
                  detail.decode(errors="replace")[:1000], flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
