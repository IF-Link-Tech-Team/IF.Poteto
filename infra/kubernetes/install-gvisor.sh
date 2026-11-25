#!/bin/sh
# Runs only in the privileged node installer, never in a user build.
set -eu
apk add --no-cache curl python3 bzip2 util-linux >/dev/null
version=20261123.0
root=/host
marker="$root/usr/local/share/poteto-gvisor-version"
if [ ! -f "$marker" ] || [ "$(cat "$marker")" != "$version" ]; then
  cd /tmp
  base="https://storage.googleapis.com/gvisor/releases/release/$version/x86_64"
  curl -fsSLO "$base/gvisor.tar.bz2"
  curl -fsSLO "$base/gvisor.tar.bz2.sha512"
  sha512sum -c gvisor.tar.bz2.sha512
  tar -xjf gvisor.tar.bz2 -C "$root/usr/local/bin"
  mkdir -p "$root/usr/local/share"
  echo "$version" > "$marker"
fi
python3 - <<'PY'
from pathlib import Path
import re, shutil, tomllib
root=Path('/host/etc/containerd'); path=root/'config.toml'
text=path.read_text(); cfg=tomllib.loads(text)
assert cfg['version']==3, 'Expected containerd 2.x configuration'
fragment='/etc/containerd/poteto-ci-runtime.toml'
if fragment not in cfg.get('imports',[]):
    shutil.copy2(path,root/'config.toml.poteto-before')
    imports=cfg.get('imports',[])+[fragment]
    line='imports = '+repr(imports).replace("'",'"')+'\n'
    if 'imports' in cfg:
        text=re.sub(r'^imports\s*=.*$',line.rstrip(),text,count=1,flags=re.M)
    else:
        text=line+text
    tmp=path.with_suffix('.next');tmp.write_text(text);tmp.replace(path)
(root/'poteto-ci-runtime.toml').write_text('''version = 3
[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.poteto-ci]
  runtime_type = "io.containerd.runsc.v1"
  [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.poteto-ci.options]
    TypeUrl = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/poteto-ci-runsc.toml"
''')
(root/'poteto-ci-runsc.toml').write_text('''[runsc_config]
  platform = "systrap"
  net-raw = "true"
  allow-packet-socket-write = "true"
''')
PY
nsenter -t 1 -m -p -- systemctl restart containerd
# Only advertise the runtime after the CRI has returned.
nsenter -t 1 -m -p -- systemctl is-active --quiet containerd
sa=/var/run/secrets/kubernetes.io/serviceaccount
curl -fsS --cacert "$sa/ca.crt" -X PATCH \
  -H "Authorization: Bearer $(cat "$sa/token")" -H 'Content-Type: application/merge-patch+json' \
  --data '{"metadata":{"labels":{"poteto.io/ci-runtime":"gvisor"}}}' \
  "https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}/api/v1/nodes/$NODE_NAME" >/dev/null
touch /tmp/ready
exec sleep infinity
