"""Build-only private gateway. Jobs run as an unprivileged KVM worker, not here."""
import base64
from contextlib import contextmanager
import fcntl
import http.server
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = Path(os.environ.get('POTETO_CI_ROOT', '/var/lib/poteto-ci'))
ID = re.compile(r'[a-f0-9]{24}')
LIMIT = 50 * 1024**2


def write(path, value):
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


@contextmanager
def locked(name):
    with (ROOT / name).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def unit(identifier):
    return 'poteto-ci-build@' + identifier + '.service'


def control(action, identifier):
    subprocess.run(['systemctl', action, unit(identifier)], check=True, capture_output=True, timeout=45)


def deliver(identifier):
    callback = ROOT/'callbacks'/identifier
    data = json.loads(callback.read_text())
    artifact = ROOT/'jobs'/identifier/'artifact.tar.gz'
    def request(method, suffix='', body=None):
        payload = json.dumps(body).encode() if isinstance(body, dict) else body
        headers = {'Authorization': 'Bearer '+data['token'], 'Content-Type': 'application/json' if isinstance(body, dict) else 'application/octet-stream'}
        with urllib.request.urlopen(urllib.request.Request(data['url']+suffix, data=payload, headers=headers, method=method), timeout=60) as response:
            return json.load(response)
    try:
        digest = hashlib.sha256()
        with artifact.open('rb') as source:
            for chunk in iter(lambda: source.read(1024**2), b''):
                digest.update(chunk)
        metadata = {'size': artifact.stat().st_size, 'sha256': digest.hexdigest()}
        # Repeating begin and asking for committed offsets is safe after a lost reply.
        failures = 0
        while True:
            try:
                state = request('POST', body=metadata)
                write(callback.with_suffix('.status'), {'state': 'delivering', 'received': state['received'], 'size': state['size']})
                with artifact.open('rb') as source:
                    while not state['complete']:
                        source.seek(state['received'])
                        chunk = source.read(min(2*1024**2, state['size']-state['received']))
                        if not chunk:
                            raise ValueError('Artifact progress does not match the file')
                        state = request('PUT', '?offset='+str(state['received']), chunk)
                        write(callback.with_suffix('.status'), {'state': 'delivering', 'received': state['received'], 'size': state['size']})
                write(callback.with_suffix('.status'), {'state': 'delivered'})
                return
            except (OSError, urllib.error.URLError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code not in (408, 409, 429):
                    raise
                failures += 1
                if failures >= 6:
                    raise
                time.sleep(min(2**failures, 15))
    except Exception as error:
        # Credentials and response bodies never enter logs or user-visible errors.
        reason = 'HTTP '+str(error.code) if isinstance(error, urllib.error.HTTPError) else type(error).__name__
        write(callback.with_suffix('.status'), {'state': 'failed', 'error': f'CI artifact HTTPS upload failed ({reason}); inspect or retry this deployment'})


def worker(identifier):
    job = ROOT / 'jobs' / identifier
    with locked('worker.lock'):
        if json.loads((job / 'status.json').read_text())['state'] != 'queued':
            return
        write(job / 'status.json', {'state': 'running', 'started': time.time()})
        config = None
        try:
            config = json.loads((ROOT / 'worker.json').read_text())
            driver = config.get('driver', 'vm')
            if driver == 'kubernetes':
                from execution import ci_ovh_pool
                from execution.ci_kubernetes import build
                if config.get('ovh_node_pool'):
                    (job / 'build.log').write_text('Platform CI: waiting for the OVH node and gVisor runtime\n')
                ci_ovh_pool.wake(config, ROOT)
                build(job, config)
            elif driver == 'vm':
                image = Path(os.environ['IFLINK_CI_VM_IMAGE'])/'root.img'
                virtual = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', str(image)]))['virtual-size']
                if shutil.disk_usage(ROOT).free < virtual+20*1024**3:
                    raise ValueError('CI disk cannot reserve the disposable VM plus 20 GiB headroom')
                subprocess.run([config['node'], str(Path(__file__).with_name('ci_vm.mjs')), str(job),
                                config['vm_module']], check=True, timeout=1200)
            else:
                raise ValueError('Unknown CI execution driver')
            receipt = json.loads((job / 'result.json').read_text())
            size = (job / 'artifact.tar.gz').stat().st_size
            if receipt.get('state') != 'succeeded' or not 0 < size <= receipt['limit']:
                raise ValueError('CI returned no valid build artifact')
            write(job / 'status.json', receipt | {'state': 'succeeded', 'artifact_bytes': size})
        except Exception as error:
            write(job / 'status.json', {'state': 'failed', 'error':
                  str(error) if isinstance(error, ValueError) else 'CI build failed; inspect deployment logs'})
        finally:
            if config and config.get('driver') == 'kubernetes' and config.get('ovh_node_pool'):
                from execution.ci_ovh_pool import touch
                touch(ROOT)


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        token = (ROOT / 'api.token').read_text().strip()
        if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
            self.reply(401, {'error': 'Build credential required'})
            return None
        parts = self.path.strip('/').split('/')
        if len(parts) not in (3, 4) or parts[:2] != ['v1', 'builds'] or not ID.fullmatch(parts[2]):
            self.reply(404, {'error': 'Build-only endpoint'})
            return None
        return parts[2], parts[3] if len(parts) == 4 else ''

    def do_POST(self):
        route = self.route()
        if route is None:
            return
        identifier, action = route
        job = ROOT / 'jobs' / identifier
        try:
            if action in ('cancel', 'delete'):
                body = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length', '0')), 4096)))
                if not job.exists():
                    return self.reply(200, {'absent': True})
                spec = json.loads((job / 'spec.json').read_text())
                if body.get('account_id') != spec['account_id']:
                    return self.reply(404, {'error': 'Build not owned by account'})
                control('stop', identifier)
                if action == 'delete':
                    subprocess.run(['systemctl', 'stop', 'poteto-ci-deliver@'+identifier+'.service'], check=True, capture_output=True, timeout=45)
                    shutil.rmtree(job)
                    for path in (ROOT/'callbacks'/identifier, (ROOT/'callbacks'/identifier).with_suffix('.status')):
                        path.unlink(missing_ok=True)
                else:
                    write(job / 'status.json', {'state': 'cancelled'})
                return self.reply(200, {'state': 'cancelled'})
            if action:
                return self.reply(404, {'error': 'Unknown build action'})
            size = int(self.headers.get('Content-Length', '0'))
            header = self.headers.get('X-Paas-Build-Spec', '')
            if not 0 < size <= LIMIT or len(header) > 16384:
                return self.reply(413, {'error': 'Source upload exceeds 50 MiB'})
            spec = json.loads(base64.urlsafe_b64decode(header))
            callback_url = self.headers.get('X-Poteto-Result-Url')
            callback_token = self.headers.get('X-Poteto-Result-Token')
            if callback_url:
                origin = json.loads((ROOT/'worker.json').read_text()).get('callback_origin', '')
                if callback_url != origin+'/v1/platform-builds/'+identifier+'/result' or not callback_token or len(callback_token) > 200:
                    return self.reply(400, {'error': 'Invalid CI artifact destination'})
            if (not isinstance(spec, dict) or spec.get('runtime') not in ('static', 'node', 'dockerfile')
                    or any(not isinstance(spec.get(k), str) or not ID.fullmatch(spec[k])
                           for k in ('account_id', 'project_id'))):
                return self.reply(400, {'error': 'Invalid source build specification'})
            # Only source and build instructions cross this boundary, never runtime credentials.
            allowed = {'runtime', 'build_command', 'start_command', 'output_directory', 'dockerfile',
                       'account_id', 'project_id', 'output_kind', 'npm_registry'}
            if set(spec) - allowed:
                return self.reply(400, {'error': 'Unsupported build field'})
            if spec.get('output_kind', 'image') not in ('image', 'static'):
                return self.reply(400, {'error': 'Invalid artifact kind'})
            with locked('submit.lock'):
                if job.exists():
                    if json.loads((job / 'spec.json').read_text()) != spec:
                        return self.reply(409, {'error': 'Build ID has another specification'})
                    self.close_connection = True
                    return self.reply(200, json.loads((job / 'status.json').read_text()))
                pending = sum(json.loads(p.read_text())['state'] in ('queued', 'running')
                              for p in (ROOT / 'jobs').glob('*/status.json'))
                if pending >= 32:
                    return self.reply(429, {'error': 'Platform build queue is full; retry later'})
                if shutil.disk_usage(ROOT).free < 20 * 1024**3:
                    return self.reply(507, {'error': 'Platform builder free disk reserve reached'})
                staging = ROOT / 'jobs' / (identifier + '.upload')
                staging.mkdir(mode=0o700)
                try:
                    self.connection.settimeout(120)
                    with (staging / 'source.tar.gz').open('wb') as output:
                        if callback_url:
                            source_url = callback_url.rsplit('/', 1)[0]+'/source'
                            request = urllib.request.Request(source_url, headers={'Authorization': 'Bearer '+callback_token})
                            with urllib.request.urlopen(request, timeout=120) as source:
                                received = 0
                                while chunk := source.read(1024**2):
                                    received += len(chunk)
                                    if received > LIMIT:
                                        raise ValueError('CI source exceeds 50 MiB')
                                    output.write(chunk)
                                if not received:
                                    raise ValueError('CI source is empty')
                        else:
                            while size:
                                chunk = self.rfile.read(min(size, 1024**2))
                                if not chunk:
                                    raise ValueError('Incomplete source upload')
                                output.write(chunk)
                                size -= len(chunk)
                    write(staging / 'spec.json', spec)
                    write(staging / 'status.json', {'state': 'queued'})
                    owner = pwd.getpwnam('potetoci')
                    os.chown(staging, owner.pw_uid, owner.pw_gid)
                    for entry in staging.iterdir():
                        os.chown(entry, owner.pw_uid, owner.pw_gid)
                    staging.rename(job)
                    if callback_url:
                        write(ROOT/'callbacks'/identifier, {'url': callback_url, 'token': callback_token})
                    control('start', identifier)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            return self.reply(202, {'state': 'queued'})
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            return self.reply(400, {'error': str(error)})

    def do_GET(self):
        route = self.route()
        if route is None:
            return
        identifier, action = route
        job = ROOT / 'jobs' / identifier
        try:
            receipt = json.loads((job / 'status.json').read_text())
            if not action:
                callback = ROOT/'callbacks'/identifier
                if receipt['state'] == 'succeeded' and callback.exists():
                    progress = callback.with_suffix('.status')
                    if not progress.exists():
                        write(progress, {'state': 'delivering'})
                        subprocess.run(['systemctl', 'start', 'poteto-ci-deliver@'+identifier+'.service'], check=True, capture_output=True, timeout=45)
                    delivery = json.loads(progress.read_text())
                    if delivery['state'] != 'delivered':
                        return self.reply(200, receipt | {'state': 'failed' if delivery['state'] == 'failed' else 'running',
                            'error': delivery.get('error'), 'phase': 'uploading-artifact',
                            'received': delivery.get('received', 0), 'size': delivery.get('size', receipt['artifact_bytes'])})
                return self.reply(200, receipt)
            if action == 'logs':
                log = job / 'build.log'
                return self.reply(200, {'log': log.read_text(errors='replace')[:2*1024**2] if log.exists() else ''})
            if action != 'artifact' or receipt['state'] != 'succeeded':
                return self.reply(404, {'error': 'Build artifact is unavailable'})
            size = (job / 'artifact.tar.gz').stat().st_size
            self.send_response(200)
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Type', 'application/gzip')
            self.end_headers()
            with (job / 'artifact.tar.gz').open('rb') as source:
                shutil.copyfileobj(source, self.wfile, 1024**2)
        except FileNotFoundError:
            self.reply(404, {'error': 'Build not found'})


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'deliver' and ID.fullmatch(sys.argv[2]):
        deliver(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == 'delivery-finished' and ID.fullmatch(sys.argv[2]):
        path = (ROOT/'callbacks'/sys.argv[2]).with_suffix('.status')
        if path.exists() and json.loads(path.read_text())['state'] == 'delivering':
            write(path, {'state': 'failed', 'error': 'CI artifact upload supervisor stopped'})
    elif len(sys.argv) == 3 and sys.argv[1] == 'finish' and ID.fullmatch(sys.argv[2]):
        job = ROOT / 'jobs' / sys.argv[2]
        if job.exists():
            config = json.loads((ROOT / 'worker.json').read_text())
            if config.get('driver') == 'kubernetes':
                from execution.ci_kubernetes import cleanup
                cleanup(config, sys.argv[2])
            receipt = json.loads((job / 'status.json').read_text())
            if receipt['state'] in ('queued', 'running'):
                write(job / 'status.json', {'state': 'failed', 'error': 'CI supervisor stopped or timed out'})
            for name in ('vm', 'bundle'):
                if (job / name).exists():
                    shutil.rmtree(job / name)
    elif len(sys.argv) == 2 and ID.fullmatch(sys.argv[1]):
        worker(sys.argv[1])
    else:
        http.server.ThreadingHTTPServer(('127.0.0.1', 21998), Handler).serve_forever()
