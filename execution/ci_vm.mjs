// Adapt IF.Lathe's disposable KVM sessions to Poteto's source/artifact contract.
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, copyFileSync, readdirSync, statSync, openSync, writeSync, closeSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const [job, module] = process.argv.slice(2);
const { startVm } = await import(pathToFileURL(module));
const code = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const spec = JSON.parse(readFileSync(join(job, 'spec.json'), 'utf8'));
const id = job.split('/').at(-1);
const log = openSync(join(job, 'build.log'), 'w');
let logBytes = 0;
const children = new Set();
const record = (chunk) => {
  const data = Buffer.from(chunk).subarray(0, Math.max(0, 2 * 1024 ** 2 - logBytes));
  if (data.length) { writeSync(log, data); logBytes += data.length; }
};
let vm;
const stop = () => { for (const child of children) child.kill('SIGKILL'); vm?.stop(); };
process.on('SIGTERM', () => { stop(); process.exit(143); });

function run(bin, args, { file, limit = 65536, timeout = 120000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    children.add(child);
    let bytes = 0;
    const chunks = [];
    let failure;
    const descriptor = file ? openSync(file, 'wx') : null;
    const timer = setTimeout(() => { failure = new Error('CI step timed out'); child.kill('SIGKILL'); }, timeout);
    child.stdout.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > limit) { failure = new Error('Build artifact or receipt exceeds its size limit'); child.kill('SIGKILL'); return; }
      if (descriptor !== null) writeSync(descriptor, chunk); else chunks.push(chunk);
    });
    child.stderr.on('data', record);
    child.on('error', error => { failure = error; });
    child.on('close', code => {
      clearTimeout(timer); children.delete(child);
      if (descriptor !== null) closeSync(descriptor);
      if (failure || code !== 0) reject(failure ?? new Error(`CI step exited ${code}; inspect build logs`));
      else resolve(Buffer.concat(chunks).toString('utf8'));
    });
  });
}

try {
  const bundle = join(job, 'bundle');
  for (const dir of ['work', 'opt/paas-execution/publisher']) mkdirSync(join(bundle, dir), { recursive: true });
  copyFileSync(join(job, 'source.tar.gz'), join(bundle, 'work/source.tar.gz'));
  writeFileSync(join(bundle, 'work/spec.json'), JSON.stringify({ ...spec, id }));
  for (const [source, target] of [
    ['execution/guest_build.py', 'guest_build.py'], ['execution/build_source.py', 'build_source.py'],
    ['publisher/archive.py', 'publisher/archive.py'], ['publisher/build_recipe.py', 'publisher/build_recipe.py'],
    ['publisher/__init__.py', 'publisher/__init__.py'],
  ]) copyFileSync(join(code, source), join(bundle, 'opt/paas-execution', target));
  vm = await startVm({ jobId: `poteto-${id}`, workdir: job, spec: { cpus: 2, memory_mb: 4096 }, hooks: { output: record } });
  if (!vm.ok) throw new Error(vm.error);
  record(`Platform CI: disposable KVM guest ready in ${Math.round(vm.bootMs / 1000)}s\n`);
  const uploaded = await vm.upload(bundle);
  if (uploaded.code) throw new Error('Could not send source to CI guest');
  const known = readFileSync(join(job, 'vm/known_hosts'), 'utf8');
  const port = /^\[127\.0\.0\.1\]:(\d+) /.exec(known)?.[1];
  if (!port) throw new Error('CI guest identity is missing');
  const ssh = ['-F', '/dev/null', '-p', port, '-i', join(job, 'vm/login'), '-o', 'BatchMode=yes',
    '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes', '-o', `UserKnownHostsFile=${join(job, 'vm/known_hosts')}`,
    '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4', 'ci@127.0.0.1'];
  await run('ssh', [...ssh, 'cd /home/ci/work && sudo -n mkdir -p /work /opt/paas-execution && sudo -n cp work/* /work/ && sudo -n cp -R opt/paas-execution/. /opt/paas-execution/']);
  const result = JSON.parse(await run('ssh', [...ssh,
    'sudo -n env POTETO_STREAM_BUILD_LOG=1 python3 /opt/paas-execution/guest_build.py'], { timeout: 960000 }));
  if (result.state !== 'succeeded') throw new Error('Application build failed; inspect deployment logs');
  const kind = spec.output_kind ?? 'image';
  const limit = (kind === 'static' ? 50 : 128) * 1024 ** 2;
  await run('ssh', [...ssh, `sudo -n gzip -n -c /work/${kind === 'static' ? 'static' : 'image'}.tar`],
    { file: join(job, 'artifact.tar.gz'), limit, timeout: 180000 });
  writeFileSync(join(job, 'result.json'), JSON.stringify({ state: 'succeeded', output_kind: kind, limit }));
} catch (error) {
  record(`${error.message}\n`);
  process.exitCode = 1;
} finally {
  stop(); closeSync(log);
  for (const name of ['vm', 'bundle']) rmSync(join(job, name), { recursive: true, force: true });
}
