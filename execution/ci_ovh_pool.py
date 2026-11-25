"""Optional scale-to-zero policy for a dedicated OVH CI node pool."""
import fcntl
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import time


def pool_name(config):
    name = config.get('ovh_node_pool')
    if name is None:
        return None
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
        raise ValueError('Invalid OVH CI node pool name')
    return name


def request(config, *args):
    command = [config['kubectl'], '--kubeconfig', config['kubeconfig'], '--request-timeout=30s']
    result = subprocess.run(command + list(args), capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise ValueError('OVH CI pool operation failed: ' + result.stderr[-1000:])
    return json.loads(result.stdout)


def pool(config):
    value = request(config, 'get', 'nodepool', pool_name(config), '-o', 'json')
    if value['spec']['minNodes'] != 0 or value['spec']['maxNodes'] != 2:
        raise ValueError('The dedicated OVH CI pool must have minNodes=0 and maxNodes=2')
    return value


def resize(config, **spec):
    return request(config, 'patch', 'nodepool', pool_name(config), '--type=merge',
                   '-p', json.dumps({'spec': spec}), '-o', 'json')


def touch(root):
    marker = root / 'jobs/.ovh-pool-activity.json'
    temporary = marker.with_suffix('.next')
    temporary.write_text(json.dumps({'last_activity': time.time()}))
    temporary.replace(marker)


def wake(config, root):
    if pool_name(config) is None:
        return False
    touch(root)
    deadline = time.monotonic() + 300
    while True:
        value = pool(config)
        current = value['spec']
        stopping = not current['autoscale'] and current['desiredNodes'] == 0 and (
            value['status']['currentNodes'] > 0 or any('DOWNSCALING' in c.get('message', '')
                for c in value['status'].get('conditions', [])))
        if not stopping:
            break
        if time.monotonic() >= deadline:
            raise ValueError('OVH CI node shutdown did not finish; retry this build')
        time.sleep(5)
    if not current['autoscale'] or current['desiredNodes'] < 1:
        resize(config, autoscale=True, desiredNodes=max(1, current['desiredNodes']))
    return True


def guard_node(config, node_name):
    """Block a freshly allocated worker's public address before sending user code."""
    if pool_name(config) is None:
        return
    node = request(config, 'get', 'node', node_name, '-o', 'json')
    addresses = []
    for address in node['status']['addresses']:
        if address['type'] not in {'ExternalIP', 'InternalIP'}:
            continue
        value = ipaddress.ip_address(address['address'])
        # OVH also advertises a worker's public IPv4 as its InternalIP.
        if value.version == 4 and (address['type'] == 'ExternalIP' or value.is_global):
            addresses.append(str(value) + '/32')
    if not addresses:
        raise ValueError('OVH CI worker has no observable public IPv4 address')
    policy = request(config, '-n', 'poteto-ci', 'get', 'networkpolicy', 'build-isolation', '-o', 'json')
    found = False
    for rule in policy['spec']['egress']:
        for target in rule.get('to', []):
            block = target.get('ipBlock', {})
            if block.get('cidr') == '0.0.0.0/0':
                block['except'] = sorted(set(block['except']) | set(addresses))
                found = True
    if not found:
        raise ValueError('CI public egress policy is missing its private-address exclusions')
    operations = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': policy['metadata']['resourceVersion']},
                  {'op': 'replace', 'path': '/spec/egress', 'value': policy['spec']['egress']}]
    request(config, '-n', 'poteto-ci', 'patch', 'networkpolicy', 'build-isolation',
            '--type=json', '-p', json.dumps(operations), '-o', 'json')


def idle(config, root, *, now=None):
    if pool_name(config) is None:
        return {'state': 'disabled'}
    with (root / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'busy'}
        statuses = list((root / 'jobs').glob('*/status.json'))
        if list((root / 'jobs').glob('*.upload')) or any(
                json.loads(p.read_text())['state'] in {'queued', 'running'} for p in statuses):
            return {'state': 'busy'}
        marker = root / 'jobs/.ovh-pool-activity.json'
        activity = ([json.loads(marker.read_text())['last_activity']] if marker.exists() else [])
        activity += [p.stat().st_mtime for p in statuses]
        if not activity:
            touch(root)
            return {'state': 'waiting'}
        remaining = 600 - ((time.time() if now is None else now) - max(activity))
        if remaining > 0:
            return {'state': 'waiting', 'seconds_remaining': round(remaining)}
        current = pool(config)
        if not current['spec']['autoscale'] and current['spec']['desiredNodes'] == 0:
            return {'state': 'stopped' if current['status']['currentNodes'] == 0 else 'stopping'}
        pods = request(config, 'get', 'pods', '--all-namespaces', '-o', 'json')['items']
        for pod in pods:
            if pod['status']['phase'] in {'Succeeded', 'Failed'}:
                continue
            namespace = pod['metadata']['namespace']
            if namespace == 'kube-system':
                continue
            if namespace == 'poteto-system' and any(
                    owner['kind'] == 'DaemonSet' for owner in pod['metadata'].get('ownerReferences', [])):
                continue
            return {'state': 'protected-workload', 'namespace': namespace, 'name': pod['metadata']['name']}
        # Pending system pods otherwise immediately wake an empty cluster again.
        resize(config, autoscale=False, desiredNodes=0)
        return {'state': 'stopping'}


def main():
    root = Path('/var/lib/poteto-ci')
    print(json.dumps(idle(json.loads((root / 'worker.json').read_text()), root)))


if __name__ == '__main__':
    main()
