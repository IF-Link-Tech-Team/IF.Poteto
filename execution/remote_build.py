"""Send a source job to the private CI gateway; import only validated artifacts."""
import base64
import http.client
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from execution import prebuilt_image
from publisher.ci_contract import credential


class Client:
    def __init__(self, config):
        parsed = urlsplit(config['url'])
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != 22998:
            raise ValueError('Platform builder must use the private loopback SSH tunnel on port 22998')
        result = urlsplit(config.get('result_url', ''))
        if result.scheme != 'https' or not result.hostname or result.username or result.password or result.path or result.query or result.fragment:
            raise ValueError('Configure an HTTPS public API origin for CI artifact transfers')
        self.token = Path(config['token_file']).read_text().strip()
        self.config = config

    def callback(self, identifier):
        return {'X-Poteto-Result-Url': self.config['result_url'].rstrip('/')+'/v1/platform-builds/'+identifier+'/result',
                'X-Poteto-Result-Token': credential(self.token.encode(), identifier, int(time.time())+7000)}

    def result(self, method, identifier, suffix, *, destination=None):
        parsed = urlsplit(self.config.get('result_read_url', self.config['result_url']))
        if parsed.scheme == 'https':
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=120)
        elif parsed.scheme == 'http' and parsed.hostname == '127.0.0.1':
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=120)
        else:
            raise ValueError('CI result reads require HTTPS or the local controller forward')
        headers = {'Authorization': 'Bearer '+credential(self.token.encode(), identifier, int(time.time())+7000),
                   'Host': self.config.get('result_host', urlsplit(self.config['result_url']).netloc)}
        try:
            connection.request(method, '/v1/platform-builds/'+identifier+'/'+suffix, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError('Uploaded CI result is unavailable')
            if destination is None:
                return json.loads(response.read(65536))
            size, expected = 0, response.length
            with destination.open('wb') as output:
                while chunk := response.read(1024**2):
                    size += len(chunk)
                    if size > 128*1024**2:
                        raise ValueError('CI artifact exceeds 128 MiB')
                    output.write(chunk)
            if size != expected or not size:
                raise ValueError('Uploaded CI result was truncated')
        finally:
            connection.close()

    def request(self, method, path, body=None, headers=None, *, destination=None, limit=128*1024**2):
        connection = http.client.HTTPConnection('127.0.0.1', 22998, timeout=120)
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        try:
            connection.request(method, path, body, {'Authorization': 'Bearer ' + self.token} | (headers or {}))
            response = connection.getresponse()
            if response.status >= 400:
                message = json.loads(response.read(65536)).get('error', 'Platform CI request failed')
                error = ValueError(message)
                error.status = response.status
                raise error
            if destination is None:
                return json.loads(response.read(16*1024**2))
            size = 0
            expected = response.length
            with destination.open('wb') as output:
                while chunk := response.read(1024**2):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('CI artifact exceeds its size limit')
                    output.write(chunk)
            if not size or size != expected:
                raise ValueError('CI artifact transfer did not finish')
        finally:
            connection.close()


def finish(config, identifier, account):
    client = Client(config)
    client.request('POST', '/v1/builds/' + identifier + '/delete', {'account_id': account})
    client.result('DELETE', identifier, 'result')


def build(config, job, spec):
    client = Client(config)
    path = '/v1/builds/' + job.name
    source_spec = {k: v for k, v in spec.items() if k in {
        'runtime', 'build_command', 'start_command', 'output_directory', 'dockerfile',
        'account_id', 'project_id', 'output_kind'}}
    try:
        state = client.request('GET', path)
    except ValueError as error:
        if getattr(error, 'status', None) != 404:
            raise
        with (job / 'source.tar.gz').open('rb') as source:
            state = client.request('POST', path, b'source-v1', {
                'Content-Length': '9',
                'X-Paas-Build-Spec': base64.urlsafe_b64encode(json.dumps(source_spec).encode()).decode(),
                **client.callback(job.name)})
    deadline = time.monotonic() + 2400
    with (job / 'build.log').open('wb') as log:
        offset = 0
        progress = None
        while True:
            data = client.request('GET', path + '/logs')['log'].encode()
            if len(data) > offset:
                log.write(data[offset:]); log.flush(); offset = len(data)
            if state.get('phase') == 'uploading-artifact' and state.get('received') != progress:
                progress = state.get('received', 0)
                log.write(f"CI artifact HTTPS upload: {progress/1024**2:.1f} / {state.get('size', 0)/1024**2:.1f} MiB\n".encode())
                log.flush()
            if state['state'] not in ('queued', 'running'):
                break
            if time.monotonic() >= deadline:
                raise ValueError('Platform build queue deadline reached')
            time.sleep(2)
            state = client.request('GET', path)
    if state['state'] != 'succeeded':
        raise ValueError(state.get('error', 'Platform build did not succeed'))
    artifact = job / 'ci-artifact.tar.gz'
    try:
        client.result('GET', job.name, 'artifact', destination=artifact)
        if spec.get('output_kind') == 'static':
            if state.get('output_kind') != 'static' or artifact.stat().st_size > 50 * 1024**2:
                raise ValueError('CI returned an invalid static artifact')
            artifact.replace(job / 'static.tar.gz')
            result = {'output_kind': 'static', 'artifact_bytes': (job / 'static.tar.gz').stat().st_size}
        else:
            result = prebuilt_image.normalize(artifact, job / 'image.tar', job.name)
        finish(config, job.name, spec['account_id'])
        return result
    finally:
        artifact.unlink(missing_ok=True)
