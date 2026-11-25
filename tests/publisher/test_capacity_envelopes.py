from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from fastapi.testclient import TestClient
from publisher import capacity
from publisher.api import create_app
from publisher.config import Settings
from publisher.login_contract import ISSUER
from execution import host


class CapacityEnvelopeTests(unittest.TestCase):
    def test_legacy_envelope_is_opt_in_and_keeps_account_limits(self):
        rows=[{'runtime_driver':None,'state':'ready','backend_id':'legacy','account_id':'old',
               'config':json.dumps({'memory_mb':1024,'cpus':3.5})},
              {'runtime_driver':'gvisor','state':'ready','backend_id':'native','account_id':'new',
               'config':json.dumps({'memory_mb':256,'cpus':0.1})}]
        data=[{'account_id':'old','runtime_driver':None,'kind':'database'},
              {'account_id':'old','runtime_driver':None,'kind':'volume'}]
        def db():
            result=Mock();result.execute.side_effect=[Mock(fetchall=Mock(return_value=rows)),data,[]];return result
        old=capacity.reservations(db())
        actual=capacity.reservations(db(),legacy_vm_accounting=True)
        self.assertEqual({'memory_mib':1536,'vcpus':3.85},old['platform'])
        self.assertEqual({'memory_mib':6400,'vcpus':2.1},actual['platform'])
        self.assertEqual(old['accounts'],actual['accounts'])
        self.assertEqual(['old'],actual['legacy_accounts'])
        self.assertEqual(capacity.empty(),capacity.platform_increment(actual,'old',{'memory_mb':256,'cpus':.1},None,legacy_vm_accounting=True))
        self.assertEqual(capacity.LEGACY_VM,capacity.platform_increment(actual,'another',{'memory_mb':256,'cpus':.1},None,legacy_vm_accounting=True))

    def test_volume_only_tenant_reserves_the_vm_but_not_an_application(self):
        db=Mock();db.execute.side_effect=[Mock(fetchall=Mock(return_value=[])),
            [{'account_id':'volume-owner','kind':'volume','runtime_driver':None}],[]]
        result=capacity.reservations(db,legacy_vm_accounting=True)
        self.assertEqual(capacity.LEGACY_VM,result['platform'])
        self.assertEqual({},result['accounts'])

    def test_empty_account_retains_its_vm_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            store=create_app(Settings(Path(directory),legacy_vm_accounting=True)).state.store
            with store.connection() as db:
                project=store.enroll_cli_identity(db,ISSUER,'retained-vm')
                account=db.execute('SELECT account_id FROM projects WHERE id=?',(project,)).fetchone()[0]
                store.retain_runtime(db,account,None)
                result=capacity.reservations(db,legacy_vm_accounting=True)
                self.assertEqual(capacity.LEGACY_VM,result['platform'])
                self.assertEqual({},result['accounts'])

    def test_50_mixed_light_applications_fit_but_excess_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(Path(directory),personal_previews=True,platform_builds=True,
                sandbox_memory_limit_mib=14336,sandbox_cpu_limit=6,platform_cpu_limit=8,legacy_vm_accounting=True)
            app=create_app(settings);store=app.state.store
            with store.connection() as db:
                project=store.enroll_cli_identity(db,ISSUER,'envelope-test')
                account=db.execute('SELECT account_id FROM projects WHERE id=?',(project,)).fetchone()[0]
                token=store.mint_token(db,{'id':None,'account_id':account},name='test',scopes=['deployments:preview'],project_ids=[project],expires=9999999999)['token']
            body={'configuration':{'runtime':'node','target':'preview'},'archive_bytes':100,'unpacked_bytes':200,'files':2}
            fifty={'platform':{'memory_mib':20224,'vcpus':7.8},'sandbox':{'memory_mib':14080,'vcpus':5.8},'accounts':{},'legacy_accounts':['old']}
            with TestClient(app,headers={'Authorization':'Bearer '+token}) as api:
                with patch('publisher.capacity.reservations',return_value=fifty):
                    response=api.post('/v1/projects/'+project+'/preflight',json=body)
                    self.assertEqual(200,response.status_code,response.text)
                full=fifty|{'platform':{'memory_mib':20480,'vcpus':7.9},'sandbox':{'memory_mib':14336,'vcpus':5.9}}
                with patch('publisher.capacity.reservations',return_value=full):
                    response=api.post('/v1/projects/'+project+'/preflight',json=body)
                    self.assertEqual(429,response.status_code)
            self.assertFalse(Settings(Path(directory)).legacy_vm_accounting)
            with self.assertRaises(ValueError):replace(settings,legacy_vm_accounting='yes')


class SuspendEmptyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.account='b'*24
        self.record={'account_id':self.account,'slot':1,'alias':'runtime-'+self.account,
                     'ssh_port':22101,'http_port':22201,'state':'ready'}
        (self.root/'placements.json').write_text(json.dumps({self.account:self.record}))
        self.disk=self.root/'vm/runtime1/root.qcow2';self.disk.parent.mkdir(parents=True);self.disk.write_bytes(b'keep-data')
        p=patch.object(host,'ROOT',self.root);p.start();self.addCleanup(p.stop)

    def test_nonempty_runtime_is_never_stopped(self):
        with patch.object(host,'confirm_empty_runtime',side_effect=host.ExecutionError('owns volumes')),patch.object(host,'systemctl') as service:
            with self.assertRaises(host.ExecutionError):host.suspend_empty_runtime(self.account)
            service.assert_not_called()
        self.assertEqual(b'keep-data',self.disk.read_bytes())

    def test_suspend_preserves_placement_and_disk_and_resumes_on_real_use(self):
        with patch.object(host,'confirm_empty_runtime'),patch.object(host,'systemctl') as service,patch.object(host,'unit_state',return_value='inactive'):
            result=host.suspend_empty_runtime(self.account)
            self.assertTrue(result['suspended'])
            service.assert_called_once_with('disable','--now','paas-runtime@1.service')
        with patch.object(host,'systemctl') as service,patch.object(host,'wait_ready'),patch.object(host,'unit_state',return_value='inactive'):
            host.runtime(self.account)
            service.assert_called_once_with('enable','--now','paas-runtime@1.service')
        self.assertFalse(host.suspension_path(self.account).exists())
        self.assertEqual({self.account:self.record},json.loads((self.root/'placements.json').read_text()))
        self.assertEqual(b'keep-data',self.disk.read_bytes())

    def test_status_read_does_not_wake_suspended_runtime(self):
        marker=host.suspension_path(self.account);marker.parent.mkdir();marker.write_text('{}')
        handler=object.__new__(host.Handler)
        handler.path='/v1/runtimes/'+self.account
        handler.authorized=Mock(return_value=True);handler.response=Mock()
        with patch.object(host,'runtime') as resume,patch.object(host,'unit_state',return_value='inactive'):
            handler.do_GET()
            resume.assert_not_called()
        self.assertEqual(200,handler.response.call_args.args[0])
        self.assertTrue(handler.response.call_args.args[1]['suspended'])


if __name__=='__main__':unittest.main()
