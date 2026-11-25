from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from publisher.api import create_app
from publisher.config import Settings
from publisher.login_contract import ISSUER


class PlatformBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(Path(temporary.name), personal_previews=True, platform_builds=True,
                                 sandbox_memory_limit_mib=8192, sandbox_cpu_limit=4)
        app = create_app(self.settings)
        self.store = app.state.store
        with self.store.connection() as db:
            self.project = self.store.enroll_cli_identity(db, ISSUER, 'budget-user')
            self.account = db.execute('SELECT account_id FROM projects WHERE id=?', (self.project,)).fetchone()[0]
            token = self.store.mint_token(db, {'id': None, 'account_id': self.account}, name='budget-preview',
                                         scopes=['deployments:preview'], project_ids=[self.project], expires=9999999999)
        self.headers = {'Authorization': 'Bearer '+token['token']}
        self.path = '/v1/projects/'+self.project
        self.body = {'configuration': {'runtime': 'node', 'target': 'preview'},
                     'archive_bytes': 100, 'unpacked_bytes': 200, 'files': 2}
        self.reservations = {'platform': {'memory_mib': 6848, 'vcpus': 5.95},
                             'sandbox': {'memory_mib': 4352, 'vcpus': 2}, 'accounts': {}}

    def test_default_stays_compatible_and_invalid_cpu_budgets_are_rejected(self):
        self.assertEqual({'memory_mib': 20480, 'vcpus': 6}, self.settings.platform_limits)
        self.assertEqual({'memory_mib': 20480, 'vcpus': 8}, replace(self.settings, platform_cpu_limit=8).platform_limits)
        for value in (True, '8', 0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.settings, platform_cpu_limit=value)

    def test_eight_cpu_budget_unblocks_node_and_usage_reports_the_same_limit(self):
        with TestClient(create_app(self.settings), headers=self.headers) as old, \
             TestClient(create_app(replace(self.settings, platform_cpu_limit=8)), headers=self.headers) as new, \
             patch('publisher.capacity.reservations', return_value=self.reservations):
            denied = old.post(self.path+'/preflight', json=self.body)
            self.assertEqual(429, denied.status_code)
            self.assertIn('CPU 5.95/6 vCPU reserved, requesting 0.1', denied.json()['error'])
            allowed = new.post(self.path+'/preflight', json=self.body)
            self.assertEqual(200, allowed.status_code, allowed.text)
            self.assertEqual('gvisor', allowed.json()['runtime_driver'])
            usage = new.get(self.path+'/usage').json()
            self.assertEqual({'memory_mib': 20480, 'vcpus': 8}, usage['platform_runtime']['limit'])
            self.assertEqual({'memory_mib': 8192, 'vcpus': 4}, usage['sandbox_runtime']['limit'])
            self.assertEqual({'memory_mib': 4096, 'vcpus': 4}, usage['account_runtime']['limit'])
            with self.store.connection() as db:
                self.assertEqual(0, db.execute('SELECT count(*) FROM deployments').fetchone()[0])
            created = new.post(self.path+'/deployments', json=self.body['configuration'])
            self.assertEqual(201, created.status_code, created.text)

    def test_raising_platform_cpu_does_not_bypass_other_limits(self):
        cases = [
            ('Platform runtime capacity', 'platform', {'memory_mib': 20480, 'vcpus': 5.95}),
            ('Sandbox preview pool capacity', 'sandbox', {'memory_mib': 4352, 'vcpus': 4}),
            ('Account runtime CPU', 'accounts', {self.account: {'memory_mib': 256, 'vcpus': 4}}),
        ]
        with TestClient(create_app(replace(self.settings, platform_cpu_limit=8)), headers=self.headers) as client:
            for message, field, value in cases:
                totals = deepcopy(self.reservations)
                totals[field] = value
                with self.subTest(message=message), patch('publisher.capacity.reservations', return_value=totals):
                    response = client.post(self.path+'/preflight', json=self.body)
                    self.assertEqual(429, response.status_code)
                    self.assertIn(message, response.json()['error'])

    def test_runtime_configuration_loads_the_operator_cpu_budget(self):
        def contents(path, **kwargs):
            return json.dumps({'platform_cpu_limit': 8, 'sandbox_cpu_limit': 4,
                               'sandbox_memory_limit_mib': 8192}) if path.name == 'paas-publisher-runtime.json' else '{}'
        with patch('publisher.config.Path.exists', return_value=True), \
             patch('publisher.config.Path.read_text', autospec=True, side_effect=contents), \
             patch.dict('os.environ', {'PAAS_DOMAIN': 'w33d.xyz', 'PAAS_NEW_APP_DOMAIN': ''}):
            settings = Settings.from_env()
        self.assertEqual(8, settings.platform_limits['vcpus'])
        self.assertEqual({'memory_mib': 8192, 'vcpus': 4}, settings.sandbox_limits)
