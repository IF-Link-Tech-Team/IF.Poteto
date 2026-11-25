"""Trusted build supervisor inside a disposable gVisor sandbox."""
import gzip
import json
import os
from pathlib import Path
import subprocess
import time

WORK = Path('/work')


def main():
    os.environ['POTETO_STREAM_BUILD_LOG'] = '1'
    os.environ['POTETO_BUILD_NETWORK'] = 'host'
    daemon_log = (WORK / 'daemon.log').open('wb')
    daemon = subprocess.Popen(['dockerd', '--host=unix:///var/run/docker.sock',
        '--storage-driver=vfs', '--iptables=false', '--ip6tables=false', '--bridge=none',
        '--ip-forward=false', '--ip-masq=false', '--feature=containerd-snapshotter=false'],
        stdout=daemon_log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                raise RuntimeError('Sandbox Docker daemon stopped during startup')
            if subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Sandbox Docker daemon did not become ready')
        while not (WORK / 'input.ready').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('Source did not reach the build sandbox')
            time.sleep(1)
        result = subprocess.run(['python3', '/opt/paas-execution/guest_build.py'], capture_output=True)
        receipt = json.loads(result.stdout)
        if result.returncode or receipt.get('state') != 'succeeded':
            raise RuntimeError('Application build failed; inspect build logs')
        spec = json.loads((WORK / 'spec.json').read_text())
        source = WORK / ('static.tar' if spec.get('output_kind') == 'static' else 'image.tar')
        limit = (50 if spec.get('output_kind') == 'static' else 128) * 1024**2
        with source.open('rb') as data, (WORK / 'artifact.tar.gz').open('wb') as destination:
            with gzip.GzipFile(fileobj=destination, mode='wb', mtime=0) as packed:
                while chunk := data.read(1024**2):
                    packed.write(chunk)
                    if destination.tell() > limit:
                        raise RuntimeError('Build artifact exceeds its limit')
        (WORK / 'result.json').write_text(json.dumps({'state': 'succeeded'}))
    except Exception as error:
        with (WORK / 'build.log').open('ab') as log:
            log.write(('\nKubernetes CI: ' + str(error) + '\n').encode())
        (WORK / 'result.json').write_text(json.dumps({'state': 'failed'}))
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=15)
        except subprocess.TimeoutExpired:
            daemon.kill(); daemon.wait()
        daemon_log.close()
    # The gateway retrieves the result using its own scoped Kubernetes identity.
    # No deployment, registry or Kubernetes credential enters this sandbox.
    time.sleep(300)


if __name__ == '__main__':
    main()
