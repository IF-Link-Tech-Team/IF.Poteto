"""One disposable Kubernetes Job per build; the build has no API credentials."""
import io
import json
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tarfile
import time

IDENTIFIER = re.compile(r'[a-f0-9]{24}')


def job_name(identifier):
    if not IDENTIFIER.fullmatch(identifier):
        raise ValueError('Invalid CI job identifier')
    return 'poteto-build-' + identifier


def manifest(identifier, image):
    name = job_name(identifier)
    if not isinstance(image, str) or not re.fullmatch(r'docker.io/library/docker@sha256:[a-f0-9]{64}', image):
        raise ValueError('Kubernetes builder needs a pinned official Docker image')
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': name, 'namespace': 'poteto-ci', 'labels': {'app': 'poteto-build'}},
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 1200, 'ttlSecondsAfterFinished': 600,
            'template': {'metadata': {'labels': {'app': 'poteto-build'}}, 'spec': {
                'runtimeClassName': 'poteto-ci', 'automountServiceAccountToken': False,
                'restartPolicy': 'Never', 'enableServiceLinks': False,
                'containers': [{'name': 'build', 'image': image,
                    'command': ['sh', '-ec', 'apk add --no-cache python3 >/dev/null; exec python3 /opt/paas-execution/ci_kube_worker.py'],
                    'securityContext': {'privileged': False, 'allowPrivilegeEscalation': False,
                        'capabilities': {'drop': ['ALL'], 'add': ['AUDIT_WRITE', 'CHOWN', 'DAC_OVERRIDE', 'FOWNER',
                            'FSETID', 'KILL', 'MKNOD', 'NET_ADMIN', 'NET_BIND_SERVICE', 'NET_RAW', 'SETFCAP', 'SETGID',
                            'SETPCAP', 'SETUID', 'SYS_ADMIN', 'SYS_CHROOT', 'SYS_PTRACE']},
                        'seccompProfile': {'type': 'RuntimeDefault'}},
                    'resources': {'requests': {'cpu': '2', 'memory': '4Gi', 'ephemeral-storage': '8Gi'},
                                  'limits': {'cpu': '2', 'memory': '4Gi', 'ephemeral-storage': '16Gi'}},
                    'volumeMounts': [{'name': 'work', 'mountPath': '/work'},
                                     {'name': 'docker', 'mountPath': '/var/lib/docker'},
                                     {'name': 'code', 'mountPath': '/opt/paas-execution', 'readOnly': True}]}],
                'volumes': [{'name': 'work', 'emptyDir': {'sizeLimit': '1Gi'}},
                            {'name': 'docker', 'emptyDir': {'sizeLimit': '12Gi'}},
                            {'name': 'code', 'configMap': {'name': 'poteto-ci-code', 'items': [
                                {'key': name, 'path': name.replace('publisher-', 'publisher/')}
                                for name in ['ci_kube_worker.py', 'guest_build.py', 'build_source.py',
                                             'publisher-__init__.py', 'publisher-archive.py', 'publisher-build_recipe.py']]}}]}}}}


class Client:
    def __init__(self, config):
        self.command = [config['kubectl'], '--kubeconfig', config['kubeconfig'],
                        '--request-timeout=30s', '-n', 'poteto-ci']

    def run(self, *args, data=None, check=True, timeout=45):
        result = subprocess.run(self.command + list(args), input=data, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise ValueError('Kubernetes build operation failed: ' + result.stderr.decode(errors='replace')[-1500:])
        return result

    def remove(self, identifier):
        self.run('delete', 'job', job_name(identifier), '--ignore-not-found', '--wait=true',
                 '--timeout=60s', timeout=70)


def cleanup(config, identifier):
    Client(config).remove(identifier)


def build(job, config):
    client = Client(config)
    identifier = job.name
    spec = json.loads((job / 'spec.json').read_text())
    name = job_name(identifier)
    # A crashed supervisor never resumes arbitrary processes in an old sandbox.
    client.remove(identifier)
    client.run('create', '-f', '-', data=json.dumps(manifest(identifier, config['image'])).encode())
    previous = signal.getsignal(signal.SIGTERM)
    def stopped(signum, frame):
        raise RuntimeError('Kubernetes build supervisor stopped')
    signal.signal(signal.SIGTERM, stopped)
    deadline = time.monotonic() + 1200
    try:
        pod = None
        while time.monotonic() < deadline:
            items = json.loads(client.run('get', 'pods', '-l', 'job-name=' + name, '-o', 'json').stdout)['items']
            if items:
                pod = items[0]
                if pod['status']['phase'] in {'Failed', 'Succeeded'}:
                    raise ValueError('Kubernetes build stopped before source delivery')
                if pod['status']['phase'] == 'Running':
                    break
            time.sleep(2)
        else:
            raise ValueError('Kubernetes build could not obtain a worker before its deadline')
        pod_name = pod['metadata']['name']
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as bundle:
            bundle.add(job / 'source.tar.gz', arcname='source.tar.gz')
            data = json.dumps(spec | {'id': identifier}).encode()
            entry = tarfile.TarInfo('spec.json'); entry.size = len(data); entry.mode = 0o600
            bundle.addfile(entry, io.BytesIO(data))
        client.run('exec', '-i', pod_name, '--', 'sh', '-ec',
                   'tar xf - -C /work; touch /work/input.ready', data=archive.getvalue(), timeout=120)
        while time.monotonic() < deadline:
            log = client.run('exec', pod_name, '--', 'sh', '-c',
                             'test ! -f /work/build.log || head -c 2097152 /work/build.log', check=False)
            if log.returncode == 0:
                (job / 'build.log').write_bytes(b'Platform CI: Kubernetes Job / gVisor\n' + log.stdout)
            result = client.run('exec', pod_name, '--', 'sh', '-c',
                                'test ! -f /work/result.json || cat /work/result.json', check=False)
            if result.returncode:
                status = json.loads(client.run('get', 'pod', pod_name, '-o', 'json').stdout)
                if status['status']['phase'] == 'Failed':
                    raise ValueError('Kubernetes build container failed; inspect build logs')
            elif result.stdout.strip():
                receipt = json.loads(result.stdout)
                if receipt.get('state') != 'succeeded':
                    raise ValueError('Application build failed; inspect build logs')
                break
            time.sleep(2)
        else:
            raise ValueError('Kubernetes build deadline reached')
        limit = (50 if spec.get('output_kind') == 'static' else 128) * 1024**2
        command = client.command + ['exec', pod_name, '--', 'cat', '/work/artifact.tar.gz']
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            size = 0
            try:
                with selectors.DefaultSelector() as selector, (job / 'artifact.tar.gz').open('wb') as output:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        if time.monotonic() > deadline:
                            raise ValueError('Kubernetes build artifact transfer timed out')
                        if not selector.select(timeout=1):
                            continue
                        chunk = process.stdout.read1(1024**2)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            raise ValueError('Kubernetes build artifact exceeds its transfer limit')
                        output.write(chunk)
                if process.wait(timeout=30) or not size:
                    raise ValueError('Kubernetes build artifact transfer failed')
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
        (job / 'result.json').write_text(json.dumps({'state': 'succeeded',
            'output_kind': spec.get('output_kind', 'image'), 'limit': limit, 'builder': 'kubernetes-gvisor'}))
    finally:
        signal.signal(signal.SIGTERM, previous)
        client.remove(identifier)
