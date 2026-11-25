import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution import ci_ovh_pool as power


class OVHPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'jobs').mkdir()
        self.config = {'ovh_node_pool': 'poteto', 'kubectl': 'kubectl', 'kubeconfig': '/protected/ci'}

    def pool(self, *, active=True, desired=1, count=1, conditions=None):
        return {'spec': {'minNodes': 0, 'maxNodes': 2, 'autoscale': active, 'desiredNodes': desired},
                'status': {'currentNodes': count, 'conditions': conditions or []}}

    def activity(self, when=1000):
        (self.root / 'jobs/.ovh-pool-activity.json').write_text(json.dumps({'last_activity': when}))

    def test_disabled_mode_does_not_change_other_kubernetes_installations(self):
        with patch.object(power, 'request') as request:
            self.assertFalse(power.wake({}, self.root))
            self.assertEqual({'state': 'disabled'}, power.idle({}, self.root))
            request.assert_not_called()

    def test_wake_from_zero_is_explicit_and_keeps_the_two_node_limit(self):
        with patch.object(power, 'pool', return_value=self.pool(active=False, desired=0, count=0)), patch.object(power, 'resize') as resize:
            self.assertTrue(power.wake(self.config, self.root))
            resize.assert_called_once_with(self.config, autoscale=True, desiredNodes=1)
        self.assertTrue((self.root / 'jobs/.ovh-pool-activity.json').is_file())

    def test_wake_waits_for_in_progress_provider_shutdown(self):
        stopping = self.pool(active=False, desired=0, count=0, conditions=[{'message': 'status: DOWNSCALING'}])
        with patch.object(power, 'pool', side_effect=[stopping, self.pool(active=False, desired=0, count=0)]), patch.object(power, 'resize') as resize, patch.object(power.time, 'sleep') as sleep:
            power.wake(self.config, self.root)
            sleep.assert_called_once_with(5)
            resize.assert_called_once_with(self.config, autoscale=True, desiredNodes=1)

    def test_wake_does_not_shrink_an_active_two_node_pool(self):
        with patch.object(power, 'pool', return_value=self.pool(desired=2, count=2)), patch.object(power, 'resize') as resize:
            power.wake(self.config, self.root)
            resize.assert_not_called()

    def test_idle_preserves_a_held_worker_lock(self):
        with (self.root / 'worker.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with patch.object(power, 'request') as request:
                self.assertEqual('busy', power.idle(self.config, self.root)['state'])
                request.assert_not_called()

    def test_idle_preserves_queued_uploading_and_running_jobs(self):
        for state in ['queued', 'running']:
            job = self.root / 'jobs' / state
            job.mkdir()
            (job / 'status.json').write_text(json.dumps({'state': state}))
            with patch.object(power, 'request') as request:
                self.assertEqual('busy', power.idle(self.config, self.root, now=2000)['state'])
                request.assert_not_called()
            (job / 'status.json').unlink(); job.rmdir()
        (self.root / 'jobs/current.upload').mkdir()
        self.assertEqual('busy', power.idle(self.config, self.root, now=2000)['state'])

    def test_idle_waits_ten_minutes_after_activity(self):
        self.activity()
        with patch.object(power, 'request') as request:
            self.assertEqual({'state': 'waiting', 'seconds_remaining': 1}, power.idle(self.config, self.root, now=1599))
            request.assert_not_called()

    def test_idle_disables_autoscaler_before_setting_zero(self):
        self.activity()
        system = {'metadata': {'namespace': 'kube-system', 'name': 'coredns'}, 'status': {'phase': 'Pending'}}
        installer = {'metadata': {'namespace': 'poteto-system', 'name': 'installer', 'ownerReferences': [{'kind': 'DaemonSet'}]}, 'status': {'phase': 'Running'}}
        with patch.object(power, 'pool', return_value=self.pool()), patch.object(power, 'request', return_value={'items': [system, installer]}), patch.object(power, 'resize') as resize:
            self.assertEqual({'state': 'stopping'}, power.idle(self.config, self.root, now=1600))
            resize.assert_called_once_with(self.config, autoscale=False, desiredNodes=0)

    def test_idle_never_stops_unknown_or_active_ci_workloads(self):
        self.activity()
        for namespace in ['another-project', 'poteto-ci', 'poteto-system']:
            pod = {'metadata': {'namespace': namespace, 'name': 'preserve'}, 'status': {'phase': 'Pending'}}
            with patch.object(power, 'pool', return_value=self.pool()), patch.object(power, 'request', return_value={'items': [pod]}), patch.object(power, 'resize') as resize:
                self.assertEqual('protected-workload', power.idle(self.config, self.root, now=1600)['state'])
                resize.assert_not_called()

    def test_already_stopped_pool_is_not_repeatedly_patched(self):
        self.activity()
        with patch.object(power, 'pool', return_value=self.pool(active=False, desired=0, count=0)), patch.object(power, 'request') as request:
            self.assertEqual({'state': 'stopped'}, power.idle(self.config, self.root, now=1600))
            request.assert_not_called()

    def test_only_an_explicit_valid_pool_with_approved_limits_is_managed(self):
        for name in ['', '../pool', 'a; touch file', 'A', 'a'*64]:
            with self.assertRaises(ValueError): power.pool_name({'ovh_node_pool': name})
        for key, value in [('minNodes', 1), ('maxNodes', 3)]:
            item = self.pool(); item['spec'][key] = value
            with patch.object(power, 'request', return_value=item), self.assertRaises(ValueError):
                power.pool(self.config)

    def test_new_node_address_is_blocked_without_removing_existing_exclusions(self):
        node = {'status': {'addresses': [{'type': 'ExternalIP', 'address': '203.0.113.9'}]}}
        policy = {'metadata': {'resourceVersion': '42'}, 'spec': {'egress': [
            {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': ['10.0.0.0/8', '203.0.113.1/32']}}]}]}}
        with patch.object(power, 'request', side_effect=[node, policy, {}]) as request:
            power.guard_node(self.config, 'new-worker')
        args = request.call_args.args
        operations = json.loads(args[args.index('-p') + 1])
        self.assertEqual({'op': 'test', 'path': '/metadata/resourceVersion', 'value': '42'}, operations[0])
        exclusions = operations[1]['value'][0]['to'][0]['ipBlock']['except']
        self.assertEqual(['10.0.0.0/8', '203.0.113.1/32', '203.0.113.9/32'], exclusions)

    def test_missing_node_address_stops_source_delivery(self):
        with patch.object(power, 'request', return_value={'status': {'addresses': []}}), self.assertRaisesRegex(ValueError, 'IPv4'):
            power.guard_node(self.config, 'new-worker')

    def test_ovh_public_internal_ip_is_guarded_but_private_ip_is_not_an_egress_endpoint(self):
        node = {'status': {'addresses': [{'type': 'InternalIP', 'address': '40.160.15.220'},
                                         {'type': 'InternalIP', 'address': '10.0.0.2'}]}}
        policy = {'metadata': {'resourceVersion': '42'}, 'spec': {'egress': [
            {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': ['10.0.0.0/8']}}]}]}}
        with patch.object(power, 'request', side_effect=[node, policy, {}]) as request:
            power.guard_node(self.config, 'worker')
        operations = json.loads(request.call_args.args[request.call_args.args.index('-p') + 1])
        self.assertEqual(['10.0.0.0/8', '40.160.15.220/32'], operations[1]['value'][0]['to'][0]['ipBlock']['except'])
