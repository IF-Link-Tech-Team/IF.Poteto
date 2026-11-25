#!/bin/sh
# Install a build-only gateway with an explicit KVM or Kubernetes backend.
# Run as root on Linux. Does not restart Lathe or application runtimes.
set -eu
test "$(id -u)" = 0
CODE=${POTETO_CI_CODE:-/opt/poteto-ci}
NODE=${POTETO_CI_NODE:-/opt/iflink-ci/node/bin/node}
VM_MODULE=${POTETO_CI_VM_MODULE:-/opt/iflink-ci/runner/vm.mjs}
VM_IMAGE=${POTETO_CI_VM_IMAGE:-/var/lib/iflink-ci-vm/images/current}
DRIVER=${POTETO_CI_DRIVER:-vm}
KUBECTL=${POTETO_CI_KUBECTL:-/opt/poteto-ci/bin/kubectl}
KUBECONFIG=${POTETO_CI_KUBECONFIG:-/etc/poteto-ci/kubeconfig}
BUILDER_IMAGE=${POTETO_CI_BUILDER_IMAGE:-docker.io/library/docker@sha256:9a06753d2401cd049b34cd27dbbc3e0db717d4c1db7bc7f2efad1c187e00bf5a}
NODE_POOL=${POTETO_CI_OVH_NODE_POOL:-}
if [ -n "$NODE_POOL" ]; then
  test "$DRIVER" = kubernetes
  printf '%s\n' "$NODE_POOL" | grep -Eq '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'
fi
case "$DRIVER" in
  vm) test -c /dev/kvm && test -x "$NODE" && test -f "$VM_MODULE" && test -d "$VM_IMAGE"
      WORKER_MEMORY=6G; WORKER_CPU=200%; WORKER_DEVICE='DeviceAllow=/dev/kvm rw'
      WORKER_GROUPS='SupplementaryGroups=kvm' ;;
  kubernetes) test -x "$KUBECTL" && test -r "$KUBECONFIG"
      WORKER_MEMORY=512M; WORKER_CPU=50%; WORKER_DEVICE=''; WORKER_GROUPS='' ;;
  *) echo 'POTETO_CI_DRIVER must be vm or kubernetes' >&2; exit 2 ;;
esac
id potetoci >/dev/null 2>&1 || useradd --system --home-dir /var/lib/poteto-ci --shell /usr/sbin/nologin potetoci
if [ "$DRIVER" = kubernetes ]; then
  chown root:potetoci "$KUBECONFIG"
  chmod 640 "$KUBECONFIG"
fi
install -d -m 750 -o root -g potetoci /var/lib/poteto-ci
install -d -m 700 -o potetoci -g potetoci /var/lib/poteto-ci/jobs
install -d -m 700 -o root -g root /var/lib/poteto-ci/callbacks
touch /var/lib/poteto-ci/worker.lock
chown potetoci:potetoci /var/lib/poteto-ci/worker.lock
if [ ! -f /var/lib/poteto-ci/api.token ]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > /var/lib/poteto-ci/api.token)
fi
python3 - "$NODE" "$VM_MODULE" "${POTETO_CI_CALLBACK_ORIGIN:-https://deploy.iflink.tech}" "$DRIVER" "$KUBECTL" "$KUBECONFIG" "$BUILDER_IMAGE" "$NODE_POOL" <<'PY'
import json, sys
from pathlib import Path
config = {'node':sys.argv[1], 'vm_module':sys.argv[2], 'callback_origin':sys.argv[3],
    'driver':sys.argv[4], 'kubectl':sys.argv[5], 'kubeconfig':sys.argv[6], 'image':sys.argv[7]}
if sys.argv[8]:config['ovh_node_pool'] = sys.argv[8]
Path('/var/lib/poteto-ci/worker.json').write_text(json.dumps(config))
PY
install -d /etc/poteto-ci /etc/netns/poteto-ci
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/netns/poteto-ci/resolv.conf
cat > /etc/poteto-ci/network.sh <<'NET'
#!/bin/sh
set -eu
ip netns list | grep -q '^poteto-ci\b' || ip netns add poteto-ci
if ! ip link show poteto-ci-host >/dev/null 2>&1; then
  ip link add poteto-ci-host type veth peer name eth0 netns poteto-ci
fi
ip addr replace 10.205.0.1/30 dev poteto-ci-host
ip link set poteto-ci-host up
ip netns exec poteto-ci ip addr replace 10.205.0.2/30 dev eth0
ip netns exec poteto-ci ip link set eth0 up
ip netns exec poteto-ci ip link set lo up
ip netns exec poteto-ci ip route replace default via 10.205.0.1
nft list table inet poteto_ci >/dev/null 2>&1 && nft delete table inet poteto_ci
nft -f - <<'NFT'
table inet poteto_ci {
 chain input { type filter hook input priority -20; policy accept;
  iifname "poteto-ci-host" reject
 }
 chain forward { type filter hook forward priority -20; policy accept;
  iifname "poteto-ci-host" ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 } reject
 }
 chain nat { type nat hook postrouting priority srcnat; policy accept;
  ip saddr 10.205.0.2 masquerade
 }
}
NFT
# Docker hosts often default FORWARD to DROP. These rules match only our veth.
iptables -C FORWARD -i poteto-ci-host -j ACCEPT 2>/dev/null || iptables -I FORWARD -i poteto-ci-host -j ACCEPT
iptables -C FORWARD -o poteto-ci-host -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || iptables -I FORWARD -o poteto-ci-host -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
test "$(cat /proc/sys/net/ipv4/ip_forward)" = 1 || sysctl -w net.ipv4.ip_forward=1
NET
chmod 755 /etc/poteto-ci/network.sh
cat > /etc/systemd/system/poteto-ci-network.service <<'UNIT'
[Unit]
Description=Isolated network for Poteto CI guests
After=network-online.target
Before=poteto-ci.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/etc/poteto-ci/network.sh
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/poteto-ci.service <<EOF
[Unit]
Description=Poteto private build-only gateway
After=network-online.target poteto-ci-network.service
Requires=poteto-ci-network.service
[Service]
WorkingDirectory=$CODE
ExecStart=/usr/bin/python3 -m execution.ci_gateway
Restart=on-failure
UMask=0077
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/poteto-ci-build@.service <<EOF
[Unit]
Description=Poteto disposable CI build %i
After=poteto-ci-network.service
Requires=poteto-ci-network.service
[Service]
Type=exec
User=potetoci
Group=potetoci
$WORKER_GROUPS
WorkingDirectory=$CODE
Environment=IFLINK_CI_VM_IMAGE=$VM_IMAGE
Environment=HOME=/var/lib/poteto-ci
ExecStart=/usr/bin/python3 -m execution.ci_gateway %i
ExecStopPost=/usr/bin/python3 -m execution.ci_gateway finish %i
NetworkNamespacePath=/run/netns/poteto-ci
BindReadOnlyPaths=/etc/netns/poteto-ci/resolv.conf:/etc/resolv.conf
InaccessiblePaths=/var/lib/poteto-ci/api.token -/run/tailscale -/var/lib/cloud
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ProtectProc=invisible
ReadWritePaths=/var/lib/poteto-ci/jobs /var/lib/poteto-ci/worker.lock
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
DevicePolicy=closed
$WORKER_DEVICE
UMask=0077
MemoryMax=$WORKER_MEMORY
CPUQuota=$WORKER_CPU
TasksMax=1024
RuntimeMaxSec=3600
TimeoutStopSec=15
KillMode=control-group
EOF
cat > /etc/systemd/system/poteto-ci-deliver@.service <<EOF
[Unit]
Description=Poteto CI HTTPS artifact delivery %i
After=network-online.target
[Service]
Type=exec
WorkingDirectory=$CODE
ExecStart=/usr/bin/python3 -m execution.ci_gateway deliver %i
ExecStopPost=/usr/bin/python3 -m execution.ci_gateway delivery-finished %i
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/poteto-ci/callbacks
NoNewPrivileges=yes
UMask=0077
MemoryMax=192M
CPUQuota=50%
RuntimeMaxSec=1200
TimeoutStopSec=15
EOF
systemctl daemon-reload
systemctl enable --now poteto-ci-network.service poteto-ci.service
if [ -n "$NODE_POOL" ]; then
  cat > /etc/systemd/system/poteto-ci-pool.service <<EOF
[Unit]
Description=Return the dedicated OVH CI pool to zero after ten idle minutes
After=network-online.target
[Service]
Type=oneshot
User=potetoci
Group=potetoci
WorkingDirectory=$CODE
ExecStart=/usr/bin/python3 -m execution.ci_ovh_pool
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/poteto-ci/jobs /var/lib/poteto-ci/worker.lock
MemoryMax=128M
CPUQuota=20%
UMask=0077
EOF
  cat > /etc/systemd/system/poteto-ci-pool.timer <<'UNIT'
[Unit]
Description=Check the dedicated OVH CI pool idle deadline
[Timer]
OnBootSec=30
OnUnitActiveSec=60
AccuracySec=10
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now poteto-ci-pool.timer
elif [ -f /etc/systemd/system/poteto-ci-pool.timer ]; then
  systemctl disable --now poteto-ci-pool.timer
fi
printf 'Poteto CI ready on 127.0.0.1:21998; connect using a restricted SSH tunnel.\n'
