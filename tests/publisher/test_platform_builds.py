from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import base64
import hashlib
import http.client
import os
from contextlib import redirect_stdout, redirect_stderr
from http.server import ThreadingHTTPServer
from threading import Thread

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.backend import Dokploy
from publisher.config import Settings
from publisher.executor import ExecutionFailure
from publisher.login_contract import ISSUER
from publisher.models import DeploymentInput
from execution import ci_gateway, remote_build, sandbox


class PlatformBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root, personal_previews=True, personal_production=True,
                                 platform_builds=True, static_artifacts=True)
        self.app = create_app(self.settings)
        self.store = self.app.state.store
        with self.store.connection() as db:
            self.project = self.store.enroll_cli_identity(db, ISSUER, 'ordinary-platform-user')
            self.account = db.execute('SELECT account_id FROM projects WHERE id=?', (self.project,)).fetchone()[0]
            token = self.store.mint_token(db, {'id': None, 'account_id': self.account}, name='preview',
                                         scopes=['deployments:preview'], project_ids=[self.project], expires=9999999999)
        self.actor = self.store.authenticate(token['token'])
        self.api = TestClient(self.app, headers={'Authorization': 'Bearer ' + token['token']})
        self.addCleanup(self.api.close)
        self.base = '/v1/projects/' + self.project

    def preflight(self, config):
        return self.api.post(self.base + '/preflight', json={
            'configuration': config, 'archive_bytes': 100, 'unpacked_bytes': 200, 'files': 2})

    def test_all_personal_source_builds_are_allowed_without_registration(self):
        for config, driver in [
            ({'runtime': 'node'}, 'gvisor'), ({'runtime': 'dockerfile', 'build_mode': 'platform'}, 'gvisor'),
            ({'runtime': 'static', 'build_command': 'npm run build', 'output_directory': 'dist'}, 'static-artifact')]:
            response = self.preflight(config | {'target': 'preview'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(driver, response.json()['runtime_driver'])
            self.assertEqual('platform', response.json()['build_location'])
        with self.store.connection() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM deployments').fetchone()[0])

    def test_image_budget_checked_before_upload_and_old_limits_not_raised(self):
        from publisher.image_contract import ARCHIVE_LIMIT, UPLOAD_LIMIT, UNPACK_LIMIT
        self.assertEqual((128, 512), (UPLOAD_LIMIT // 1024**2, UNPACK_LIMIT // 1024**2))
        response = self.preflight({'runtime': 'node', 'target': 'preview'})
        self.assertEqual(ARCHIVE_LIMIT + 300, response.json()['storage']['required_bytes'])
        with self.store.connection() as db:
            db.execute('UPDATE accounts SET max_storage=? WHERE id=?', (ARCHIVE_LIMIT, self.account))
        response = self.preflight({'runtime': 'node', 'target': 'preview'})
        self.assertEqual(429, response.status_code)
        self.assertIn('before uploading', response.text)

    def test_disabled_flag_keeps_old_boundary_and_production_needs_scope(self):
        disabled = TestClient(create_app(replace(self.settings, platform_builds=False)), headers=self.api.headers)
        self.addCleanup(disabled.close)
        body = {'runtime': 'node', 'target': 'preview', 'build_mode': 'platform'}
        self.assertEqual(503, disabled.post(self.base + '/deployments', json=body).status_code)
        self.assertEqual(403, self.api.post(self.base + '/deployments', json=body | {'target': 'production'}).status_code)
        with self.assertRaises(ValueError):
            DeploymentInput(runtime='image', build_mode='platform')

    def test_source_is_built_by_ci_not_native_and_receipt_reserves_real_size(self):
        release = self.api.post(self.base + '/deployments', json={'runtime': 'node', 'target': 'preview'}).json()
        with self.store.connection() as db:
            deployment = self.store.deployment(db, self.actor, release['id'], allow_preview=True)
        backend = Dokploy({'routes': str(self.root), 'releases': str(self.root/'releases')}, self.settings)
        backend.store = self.store
        client = Mock()
        absent = ExecutionFailure('absent'); absent.status = 404
        client.build_status.side_effect = absent
        client.start_build.return_value = {'state': 'succeeded', 'driver': 'remote',
            'image': 'paas-release:' + release['id'], 'image_bytes': 4096}
        client.build_logs.return_value = ''
        client.sandbox.return_value = {'http_port': 23100}
        with patch.object(backend, 'execution_client', return_value=client):
            backend.publish_sandbox(deployment, self.root/'source.tar.gz', Mock(), Mock())
        spec = client.start_build.call_args.args[1]
        self.assertTrue(spec['platform_build'])
        self.assertTrue(spec['personal_preview'])
        self.assertNotIn('require_native', spec)
        self.assertNotIn('environment', spec)
        with self.store.connection() as db:
            self.assertEqual(4096, db.execute('SELECT built_bytes FROM deployments WHERE id=?', (release['id'],)).fetchone()[0])

    def test_https_result_is_job_bound_resumable_and_keeps_original_source(self):
        from publisher.ci_contract import credential
        import time
        release = self.api.post(self.base+'/deployments', json={'runtime': 'node', 'target': 'preview'}).json()['id']
        with self.store.connection() as db:
            db.execute("UPDATE deployments SET state='building' WHERE id=?", (release,))
        original = self.root/'artifacts'/(release+'.tar.gz'); original.write_bytes(b'original-source')
        key = self.root/'ci-key'; key.write_bytes(b'synthetic-builder-key')
        path = '/v1/platform-builds/'+release
        data = b'opaque-compressed-image'
        with patch.dict(os.environ, {'POTETO_CI_UPLOAD_KEY_FILE': str(key)}):
            self.assertEqual(401, self.api.post(path+'/result', json={}).status_code)
            headers = {'Authorization': 'Bearer '+credential(key.read_bytes(), release, int(time.time())+600)}
            meta = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            self.assertEqual(200, self.api.post(path+'/result', json=meta, headers=headers).status_code)
            first = self.api.put(path+'/result?offset=0', content=data[:8], headers=headers).json()
            self.assertEqual(8, first['received'])
            repeated = self.api.put(path+'/result?offset=0', content=data[:8], headers=headers).json()
            self.assertEqual(8, repeated['received'])
            result = self.api.put(path+'/result?offset=8', content=data[8:], headers=headers).json()
            self.assertTrue(result['complete'])
            self.assertEqual(data, self.api.get(path+'/artifact', headers=headers).content)
            self.assertEqual(b'original-source', self.api.get(path+'/source', headers=headers).content)
            self.assertEqual(b'original-source', original.read_bytes())
            other = {'Authorization': 'Bearer '+credential(key.read_bytes(), 'f'*24, int(time.time())+600)}
            self.assertEqual(401, self.api.get(path+'/artifact', headers=other).status_code)
            self.assertEqual(200, self.api.delete(path+'/result', headers=headers).status_code)
            self.assertFalse((self.root/'ci-results'/release/'artifact.tar.gz').exists())

    def test_durable_fifo_allows_one_builder_but_static_fast_lane_stays_available(self):
        import time
        from publisher.store import Store
        def enqueue(subject, *, build=True):
            with self.store.connection() as db:
                project = self.store.enroll_cli_identity(db, ISSUER, subject)
                account = db.execute('SELECT account_id FROM projects WHERE id=?', (project,)).fetchone()[0]
            actor = {'id': None, 'account_id': account, 'scopes': ['deployments:preview'], 'projects': [project]}
            config = DeploymentInput(runtime='static', target='preview', build_command='npm run build' if build else None).configuration()
            release = self.store.create_deployment(actor, project, config, None)
            upload = self.root/(release['id']+'.tmp'); upload.write_bytes(b'source')
            self.store.enqueue_upload(actor, release['id'], upload, 6, 6)
            return release
        first = enqueue('queue-one')
        second = enqueue('queue-two')
        active = self.store.claim('builder-one')
        self.assertEqual(first['id'], active['deployment_id'])
        self.assertIsNone(self.store.claim('builder-two'))
        queue = self.store.build_queue(second['id'])
        self.assertEqual((1, 1, 1), (queue['position'], queue['running'], queue['concurrency']))
        self.assertNotIn(first['id'], json.dumps(queue))
        ready_static = enqueue('fast-static', build=False)
        self.assertEqual(ready_static['id'], self.store.claim('fast-lane', static_previews_only=True)['deployment_id'])
        reopened = Store(self.settings)
        self.assertIsNone(reopened.claim('after-restart'))
        with self.store.connection() as db:
            db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time()-1, active['id']))
        recovered = reopened.claim('after-restart')
        self.assertEqual(active['id'], recovered['id'])
        self.assertEqual(2, recovered['attempts'])
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET state='completed' WHERE id=?", (active['id'],))
        self.assertEqual(second['id'], reopened.claim('next')['deployment_id'])

    def test_queue_has_a_bound_before_source_upload(self):
        config = DeploymentInput(runtime='static', target='preview', build_command='npm run build').configuration()
        first = self.store.create_deployment(self.actor, self.project, config, None)
        with self.store.connection() as db:
            for i in range(63):
                db.execute("INSERT INTO deployments(id,project_id,state,config,environment,created,updated) SELECT ?,project_id,state,config,environment,created,updated FROM deployments WHERE id=?", (f'{i:024x}', first['id']))
        response = self.preflight({'runtime': 'static', 'target': 'preview', 'build_command': 'npm run build'})
        self.assertEqual(429, response.status_code)
        self.assertIn('64 pending builds', response.text)


class RemoteBuildTests(unittest.TestCase):
    def test_new_ci_job_receives_source_not_environment_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)/('a'*24); job.mkdir()
            (job/'source.tar.gz').write_bytes(b'opaque-source')
            missing = ValueError('absent'); missing.status = 404
            sent = []
            def request(method, path, body=None, headers=None, **kw):
                if method == 'POST' and not path.endswith('/delete'):
                    sent.append(json.loads(base64.urlsafe_b64decode(headers['X-Paas-Build-Spec'])))
                    return {'state': 'succeeded'}
                if path.endswith('/logs'): return {'log': ''}
                if path.endswith('/artifact'): kw['destination'].write_bytes(b'image'); return
                if path.endswith('/delete'): return {}
                raise missing
            client = Mock(); client.request.side_effect = request
            client.callback.return_value = {}
            client.result.side_effect = lambda method, identifier, suffix, **kw: kw['destination'].write_bytes(b'image') if 'destination' in kw else {}
            spec = {'runtime': 'node', 'account_id': 'b'*24, 'project_id': 'c'*24,
                    'platform_build': True, 'environment': {'SECRET': 'private'}, 'token': 'private',
                    'npm_registry': 'https://registry.on.the.runtime.host/'}
            with patch.object(remote_build, 'Client', return_value=client), patch.object(remote_build.prebuilt_image, 'normalize', return_value={}):
                remote_build.build({}, job, spec)
            self.assertEqual([{'runtime': 'node', 'account_id': 'b'*24, 'project_id': 'c'*24}], sent)

    def test_private_endpoint_only(self):
        for url in ['http://example.com:21998', 'https://127.0.0.1:21998', 'http://127.0.0.1:80']:
            with self.assertRaises(ValueError):
                remote_build.Client({'url': url, 'token_file': '/unused'})

    def test_reconcile_existing_build_never_resubmits_and_normalizes_output(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)/('a'*24); job.mkdir()
            spec = {'runtime': 'node', 'account_id': 'b'*24, 'project_id': 'c'*24,
                    'environment': {'SECRET': 'not-for-ci'}}
            calls = []
            def request(method, path, body=None, headers=None, **kw):
                calls.append((method, path, body))
                if path.endswith('/logs'): return {'log': 'built\n'}
                if path.endswith('/artifact'):
                    kw['destination'].write_bytes(b'compressed-image')
                elif path.endswith('/delete'): return {}
                else: return {'state': 'succeeded'}
            client = Mock(); client.request.side_effect = request
            client.callback.return_value = {}
            client.result.side_effect = lambda method, identifier, suffix, **kw: kw['destination'].write_bytes(b'image') if 'destination' in kw else {}
            with patch.object(remote_build, 'Client', return_value=client), \
                 patch.object(remote_build.prebuilt_image, 'normalize', return_value={'image_bytes': 100}) as normalize:
                self.assertEqual({'image_bytes': 100}, remote_build.build({}, job, spec))
            self.assertEqual(0, sum(method == 'POST' and not path.endswith('/delete') for method, path, _ in calls))
            normalize.assert_called_once()
            self.assertEqual('built\n', (job/'build.log').read_text())

    def test_ci_source_cannot_masquerade_as_prebuilt_or_bypass_disabled_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); release, account, project = 'a'*24, 'b'*24, 'c'*24
            job = root/'builds'/release; job.mkdir(parents=True)
            (job/'spec.json').write_text(json.dumps({'account_id': account, 'project_id': project,
                'runtime': 'node', 'personal_preview': True, 'platform_build': True}))
            body = {'action': 'start', 'account_id': account, 'project_id': project, 'personal_preview': True,
                    'port': 3000, 'memory_mb': 256, 'cpus': .1, 'environment': {}}
            status = Mock(return_value={'driver': 'remote', 'state': 'succeeded', 'image': 'paas-release:'+release})
            with self.assertRaisesRegex(ValueError, 'prebuilt image receipt'):
                sandbox.operation(root, {'personal_previews': True}, release, body, status)


class GatewayTests(unittest.TestCase):
    def test_private_authentication_submission_idempotency_and_owned_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'jobs').mkdir(); (root/'api.token').write_text('private-gateway-key')
            owner = Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(ci_gateway, 'ROOT', root), patch.object(ci_gateway.pwd, 'getpwnam', return_value=owner), \
                 patch.object(ci_gateway, 'control') as control, patch.object(ci_gateway.subprocess, 'run'), \
                 patch.object(ci_gateway.shutil, 'disk_usage', return_value=Mock(free=40*1024**3)):
                server = ThreadingHTTPServer(('127.0.0.1', 0), ci_gateway.Handler)
                thread = Thread(target=server.serve_forever, daemon=True); thread.start()
                self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
                identifier = 'a'*24
                spec = {'runtime': 'node', 'account_id': 'b'*24, 'project_id': 'c'*24}
                def call(method, suffix='', body=b'', *, authenticated=True, extra=None):
                    client = http.client.HTTPConnection(*server.server_address)
                    headers = {'Authorization': 'Bearer private-gateway-key'} if authenticated else {}
                    headers |= extra or {}
                    client.request(method, '/v1/builds/'+identifier+suffix, body, headers)
                    response = client.getresponse(); value = json.loads(response.read()); client.close()
                    return response.status, value
                self.assertEqual(401, call('GET', authenticated=False)[0])
                bad = base64.urlsafe_b64encode(json.dumps(spec | {'environment': {'SECRET': 'value'}}).encode()).decode()
                self.assertEqual(400, call('POST', body=b'source', extra={'X-Paas-Build-Spec': bad})[0])
                header = {'X-Paas-Build-Spec': base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()}
                self.assertEqual(202, call('POST', body=b'source', extra=header)[0])
                self.assertEqual('queued', call('GET')[1]['state'])
                self.assertEqual(200, call('POST', body=b'ignored-retry', extra=header)[0])
                self.assertEqual(b'source', (root/'jobs'/identifier/'source.tar.gz').read_bytes())
                control.assert_called_once_with('start', identifier)
                self.assertEqual(404, call('POST', '/delete', json.dumps({'account_id': 'd'*24}).encode())[0])
                self.assertTrue((root/'jobs'/identifier).exists())
                self.assertEqual(200, call('POST', '/delete', json.dumps({'account_id': 'b'*24}).encode())[0])
                self.assertFalse((root/'jobs'/identifier).exists())


class PlatformCliTests(unittest.TestCase):
    def test_explicit_platform_build_never_uses_local_builder(self):
        import sys
        from publisher import cli
        from publisher.cli_tools import initialize
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); initialize(root, 'node')
            client = Mock()
            client.request.side_effect = [{'ok': True}, {'id': 'a'*24, 'upload_url': '/upload'}, {}]
            client.wait.return_value = {'url': 'https://preview.example.test'}
            with patch.dict(os.environ, {'PAAS_TOKEN': 'synthetic', 'POTETO_TOKEN_FILE': '', 'PAAS_TOKEN_FILE': ''}), \
                 patch.object(cli, 'Client', return_value=client), patch.object(cli, 'build_local') as local, \
                 patch.object(sys, 'argv', ['poteto', 'deploy', str(root), '--project', 'b'*24, '--remote-build', '--preview', '--url-only']), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli.main()
            local.assert_not_called()
            self.assertTrue(client.request.call_args_list[0].args[1].endswith('/preflight'))
            self.assertEqual('platform', client.request.call_args_list[1].args[2]['build_mode'])
