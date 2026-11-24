"""Job-bound CI result transfer, separate from retained application source."""
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import time
from publisher.ci_contract import credential

from fastapi import Request
from fastapi.responses import FileResponse
from publisher.store import Problem


def authenticate(store, identifier, authorization, method):
    if not re.fullmatch('[a-f0-9]{24}', identifier):
        raise Problem(404, 'Build result not found')
    path = Path(os.environ.get('POTETO_CI_UPLOAD_KEY_FILE', '/etc/paas-publisher-ci.key'))
    bearer = authorization.removeprefix('Bearer ')
    try:
        expires = int(bearer.split('.')[0])
        key = path.read_bytes().strip()
    except (ValueError, OSError):
        raise Problem(401, 'Build result credential required') from None
    if not time.time() < expires <= time.time()+7200 or not hmac.compare_digest(bearer, credential(key, identifier, expires)):
        raise Problem(401, 'Build result credential expired or invalid')
    with store.connection() as db:
        row = db.execute('SELECT state,config FROM deployments WHERE id=?', (identifier,)).fetchone()
    if row is None or (method in ('POST', 'PUT') and row['state'] not in ('building', 'queued')):
        raise Problem(404, 'Build no longer accepts an artifact')
    if json.loads(row['config'])['runtime'] not in ('node', 'dockerfile', 'static'):
        raise Problem(409, 'This deployment does not build source')
    return store.settings.root/'ci-results'/identifier


def register(app, store):
    def authorize(request, identifier):
        return authenticate(store, identifier, request.headers.get('authorization', ''), request.method)

    @contextmanager
    def locked(path):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (path/'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(path):
        if not (path/'result.json').exists():
            raise Problem(404, 'Build result not uploaded yet')
        state = json.loads((path/'result.json').read_text())
        if not state['complete'] and (path/'artifact.tar.gz').exists():
            state.update(complete=True, received=state['size'])
        return state

    def write(path, state):
        pending = path/'result.next'
        pending.write_text(json.dumps(state))
        pending.replace(path/'result.json')

    @app.get('/v1/platform-builds/{identifier}/result', tags=['Platform builds'])
    def status(identifier: str, request: Request):
        return read(authorize(request, identifier))

    @app.post('/v1/platform-builds/{identifier}/result', tags=['Platform builds'])
    async def begin(identifier: str, request: Request):
        path = authorize(request, identifier)
        if int(request.headers.get('content-length', '0')) > 1024:
            raise Problem(413, 'Result metadata is too large')
        body = await request.json()
        size, digest = body.get('size'), body.get('sha256')
        if type(size) is not int or not 0 < size <= 128*1024**2 or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise Problem(400, 'Invalid build result size or digest')
        with locked(path):
            if (path/'result.json').exists():
                previous = read(path)
                if (previous['size'], previous['sha256']) != (size, digest):
                    raise Problem(409, 'This job already has a different build result')
                return previous
            if shutil.disk_usage(path).free < 1024**3+size:
                raise Problem(507, 'Build result disk reserve reached')
            state = {'size': size, 'sha256': digest, 'received': 0, 'complete': False}
            (path/'artifact.part').touch(mode=0o600)
            write(path, state)
            return state

    @app.put('/v1/platform-builds/{identifier}/result', tags=['Platform builds'])
    async def upload(identifier: str, request: Request, offset: int):
        path = authorize(request, identifier)
        size = int(request.headers.get('content-length', '0'))
        if not 0 < size <= 2*1024**2:
            raise Problem(413, 'Build result parts must be at most 2 MiB')
        data = await request.body()
        if len(data) != size:
            raise Problem(400, 'Incomplete build result part')
        with locked(path):
            state = read(path)
            if offset < state['received'] or state['complete']:
                return state
            if offset != state['received'] or offset+size > state['size']:
                raise Problem(409, 'Resume at the confirmed build result offset')
            with (path/'artifact.part').open('r+b') as output:
                output.seek(offset); output.write(data); output.flush(); os.fsync(output.fileno())
            state['received'] += size
            if state['received'] == state['size']:
                digest = hashlib.sha256()
                with (path/'artifact.part').open('rb') as source:
                    for chunk in iter(lambda: source.read(1024**2), b''):
                        digest.update(chunk)
                if digest.hexdigest() != state['sha256']:
                    raise Problem(409, 'Build result integrity check failed')
                (path/'artifact.part').replace(path/'artifact.tar.gz')
                state['complete'] = True
            write(path, state)
            return state

    @app.get('/v1/platform-builds/{identifier}/artifact', tags=['Platform builds'])
    def download(identifier: str, request: Request):
        path = authorize(request, identifier)
        if not read(path)['complete']:
            raise Problem(409, 'Build result is still uploading')
        return FileResponse(path/'artifact.tar.gz', media_type='application/gzip', headers={'Cache-Control': 'no-store'})

    @app.get('/v1/platform-builds/{identifier}/source', tags=['Platform builds'])
    def source(identifier: str, request: Request):
        authorize(request, identifier)
        path = store.settings.root/'artifacts'/(identifier+'.tar.gz')
        if not path.is_file():
            raise Problem(404, 'Build source is unavailable')
        return FileResponse(path, media_type='application/gzip', headers={'Cache-Control': 'no-store'})

    @app.delete('/v1/platform-builds/{identifier}/result', tags=['Platform builds'])
    def remove(identifier: str, request: Request):
        path = authorize(request, identifier)
        if path.exists():
            with locked(path):
                for name in ('artifact.part', 'artifact.tar.gz', 'result.json'):
                    (path/name).unlink(missing_ok=True)
        return {'deleted': True}
