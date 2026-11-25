import json
import base64
from pathlib import Path
import runpy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch,Mock

from execution.ci_kubernetes import Client,job_name,manifest


class KubernetesCITests(unittest.TestCase):
    def test_build_job_is_bounded_and_does_not_receive_host_or_service_credentials(self):
        job=manifest('a'*24,'docker.io/library/docker@sha256:'+'b'*64)
        spec=job['spec']['template']['spec']
        self.assertEqual('poteto-ci',spec['runtimeClassName'])
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertFalse(spec['enableServiceLinks'])
        self.assertEqual(0,job['spec']['backoffLimit'])
        self.assertEqual(1200,job['spec']['activeDeadlineSeconds'])
        self.assertEqual(600,job['spec']['ttlSecondsAfterFinished'])
        self.assertFalse(spec['containers'][0]['securityContext']['privileged'])
        self.assertFalse(spec['containers'][0]['securityContext']['allowPrivilegeEscalation'])
        self.assertEqual({'cpu':'2','memory':'4Gi','ephemeral-storage':'16Gi'},spec['containers'][0]['resources']['limits'])
        for volume in spec['volumes']:
            self.assertNotIn('hostPath',volume)
            self.assertNotIn('secret',volume)
        self.assertNotIn('env',spec['containers'][0])

    def test_job_identifiers_and_images_cannot_inject_cluster_commands(self):
        for value in ('../bad','a'*23,'x'*24,'a'*24+';false'):
            with self.assertRaises(ValueError):job_name(value)
        for image in ('docker:latest','untrusted/image@sha256:'+'b'*64):
            with self.assertRaises(ValueError):manifest('a'*24,image)

    def test_cleanup_waits_for_old_pods_and_is_namespace_scoped(self):
        client=Client({'kubectl':'kubectl','kubeconfig':'/protected/ci'})
        with patch('execution.ci_kubernetes.subprocess.run',return_value=Mock(returncode=0,stdout=b'')) as run:
            client.remove('a'*24)
        command=run.call_args.args[0]
        self.assertIn('poteto-ci',command)
        self.assertIn('--wait=true',command)
        self.assertIn('--timeout=60s',command)

    def test_existing_vm_builder_keeps_its_original_network(self):
        from execution import guest_build
        with patch.dict('os.environ',{},clear=True), patch.object(guest_build,'prepare_source',return_value=(Path('/source'),Path('/Dockerfile'))), patch.object(guest_build,'logged',return_value=True) as run:
            guest_build.build_source({'runtime':'node'},'image')
        self.assertNotIn('--network=host',run.call_args.args[0])
        with patch.dict('os.environ',{'POTETO_BUILD_NETWORK':'host'}), patch.object(guest_build,'prepare_source',return_value=(Path('/source'),Path('/Dockerfile'))), patch.object(guest_build,'logged',return_value=True) as run:
            guest_build.build_source({'runtime':'node'},'image')
        self.assertIn('--network=host',run.call_args.args[0])

    def test_runtime_installer_is_separate_from_build_jobs(self):
        root=Path(__file__).resolve().parents[2]
        items=json.loads((root/'infra/kubernetes/runtime.json').read_text())['items']
        runtime=next(row for row in items if row['kind']=='RuntimeClass')
        self.assertEqual('poteto-ci',runtime['handler'])
        self.assertEqual({'poteto.io/ci-runtime':'gvisor'},runtime['scheduling']['nodeSelector'])
        installer=next(row for row in items if row['kind']=='DaemonSet')
        self.assertEqual('poteto-system',installer['metadata']['namespace'])
        self.assertTrue(installer['spec']['template']['spec']['containers'][0]['securityContext']['privileged'])
        policies=json.loads((root/'infra/kubernetes/admission.json').read_text())['items']
        self.assertEqual('Fail',policies[0]['spec']['failurePolicy'])
        self.assertEqual(['Deny'],policies[1]['spec']['validationActions'])
        self.assertEqual({'kubernetes.io/metadata.name':'poteto-ci'},policies[1]['spec']['matchResources']['namespaceSelector']['matchLabels'])

    def test_kubernetes_api_error_is_reported_without_driver_fallback(self):
        client=Client({'kubectl':'kubectl','kubeconfig':'/protected/ci'})
        with patch('execution.ci_kubernetes.subprocess.run',return_value=Mock(returncode=1,stderr=b'quota exceeded')):
            with self.assertRaisesRegex(ValueError,'quota exceeded'):
                client.run('create','-f','-')

    def test_builder_bootstrap_creates_its_namespace_and_exports_only_scoped_credentials(self):
        root=Path(__file__).resolve().parents[2]
        module=runpy.run_path(str(root/'scripts/install-kubernetes-builder'))
        config={'clusters':[{'name':'test','cluster':{'server':'https://api.example.com','certificate-authority-data':'public-ca'}}]}
        secret={'data':{'token':base64.b64encode(b'synthetic-ci-token').decode()}}
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'ci.json'
            args=SimpleNamespace(kubeconfig='/private/admin',output=output,api_ip=['203.0.113.10'])
            with patch('argparse.ArgumentParser.parse_args',return_value=args), patch('subprocess.check_output',
                side_effect=[json.dumps(config).encode(),b'',b'',b'{"items":[]}',b'',json.dumps(secret).encode()]) as call:
                module['main']()
            namespace=json.loads(call.call_args_list[1].kwargs['input'])
            self.assertEqual('Namespace',namespace['kind'])
            self.assertEqual('poteto-ci',namespace['metadata']['name'])
            credential=json.loads(output.read_text())
            self.assertEqual({'token':'synthetic-ci-token'},credential['users'][0]['user'])
            self.assertEqual(0o600,output.stat().st_mode&0o777)
