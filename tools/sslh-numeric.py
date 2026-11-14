#!/usr/bin/env python3
"""Prepare/check and enable numeric logging in the existing sslh service.

Run without arguments for a read-only plan, --prepare for an isolated rehearsal,
then --apply DIR in a detached systemd job. Reuses update-containers.py checks.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import signal
import socket
import ssl
import sys
import time
from types import SimpleNamespace

ROOT = Path('/opt/x-ui')
H = None


def enable_numeric(text):
    if re.search(r'^\s*numeric\s*:', text, re.M):
        raise RuntimeError('numeric is already specified; refusing duplicate or blind overwrite')
    match = re.search(r'^(?P<indent>[ \t]*)foreground: true;(?P<end>\r?\n)', text, re.M)
    if not match or len(re.findall(r'^\s*foreground: true;', text, re.M)) != 1:
        raise RuntimeError('Expected exactly one foreground setting')
    # Only the new line is inserted; all existing bytes and protocols stay intact.
    return text[:match.end()] + match['indent'] + 'numeric: true;' + match['end'] + text[match.end():]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode):
    temp = path.with_name(path.name + '.numeric-tmp')
    with temp.open('wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    temp.chmod(mode)
    os.replace(temp, path)


def ssh_probe(address):
    with socket.create_connection(address, timeout=8) as conn:
        conn.settimeout(8)
        conn.sendall(b'SSH-2.0-XuiNumericProbe\r\n')
        banner = b''
        while b'\n' not in banner and len(banner) < 256:
            data = conn.recv(256 - len(banner))
            if not data:
                break
            banner += data
        if not banner.startswith(b'SSH-2.0-'):
            raise RuntimeError('SSH dispatch check failed')


def tls_probe(address, stage):
    client = json.loads((stage / 'smoke.json').read_text())
    domain = client['outbounds'][0]['streamSettings']['tlsSettings']['serverName']
    context = ssl.create_default_context()
    context.set_alpn_protocols(['h2'])
    start = time.monotonic()
    with socket.create_connection(address, timeout=8) as conn:
        conn.settimeout(8)
        with context.wrap_socket(conn, server_hostname=domain) as secured:
            if secured.selected_alpn_protocol() != 'h2':
                raise RuntimeError('TLS did not negotiate h2')
    return round(time.monotonic() - start, 4)


def check_service(stage):
    for _ in range(20):
        if H.inspect('sslh')['State']['Running']:
            break
        time.sleep(.5)
    else:
        raise RuntimeError('sslh is not running')
    config = H.run('docker', 'exec', 'sslh', 'cat', '/etc/sslh/sslh.cfg')
    if len(re.findall(r'^numeric: true;$', config, re.M)) != 1:
        raise RuntimeError('Running sslh config does not contain numeric: true')
    ssh_probe(('127.0.0.1', 443))
    elapsed = tls_probe(('127.0.0.1', 443), stage)
    trace = H.smoke(stage)
    return {'local_tls_seconds': elapsed, 'xhttp_trace': trace}


def prepare():
    config, current, runtime = H.preflight()
    source = ROOT / 'docker-compose.yml'
    original = source.read_bytes()
    candidate = enable_numeric(original.decode()).encode()
    cfg_old = config['configs']['sslh-cfg']['content']
    cfg_new = enable_numeric(cfg_old)
    live_cfg = H.run('docker', 'exec', 'sslh', 'cat', '/etc/sslh/sslh.cfg')
    if live_cfg.strip() != cfg_old.strip():
        raise RuntimeError('Live sslh configuration differs from the Compose source')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = ROOT / 'backups' / ('sslh-numeric-' + stamp)
    stage.mkdir(mode=0o700)
    print('Preparation directory:', stage, flush=True)
    (stage / 'compose.before.yml').write_bytes(original)
    (stage / 'compose.after.yml').write_bytes(candidate)
    (stage / 'sslh.before.cfg').write_text(cfg_old)
    (stage / 'sslh.after.cfg').write_text(cfg_new)
    H.save_json(stage / 'inspect.before.json', current)
    H.make_smoke(stage, runtime)
    print('Baseline XHTTP:', H.smoke(stage), flush=True)
    ssh_probe(('127.0.0.1', 443))
    old_images = {'services': {s: {'image': current[s]['Image']} for s in H.SERVICES}}
    H.save_json(stage / 'images.json', old_images)
    H.save_json(stage / 'candidate.json', dict(old_images, configs={'sslh-cfg': {'content': cfg_new}}))
    effective = json.loads(H.run(*H.COMPOSE, '-f', str(stage / 'candidate.json'), 'config', '--format', 'json'))
    if effective['configs']['sslh-cfg']['content'] != cfg_new:
        raise RuntimeError('Compose candidate does not match intended config')
    before = json.loads(json.dumps(config))
    before['configs']['sslh-cfg']['content'] = cfg_new
    for s in H.SERVICES:
        before['services'][s]['image'] = current[s]['Image']
    if effective != before:
        raise RuntimeError('Compose candidate unexpectedly changes other settings')

    name = 'xui-sslh-check-' + stamp.lower()
    label = 'xui.numeric-check=' + stage.name
    try:
        # Compose run does not publish service ports unless --service-ports is set.
        # Production port 443 remains owned by the existing sslh container.
        H.run(*H.COMPOSE, '-f', str(stage / 'candidate.json'), 'run', '-d', '--no-deps',
              '--name', name, '--label', label, 'sslh', log=stage / 'commands.log')
        test = H.inspect(name)
        if not test['State']['Running'] or any((test['NetworkSettings']['Ports'] or {}).values()):
            raise RuntimeError('Temporary sslh failed or unexpectedly published ports')
        address = next(iter(test['NetworkSettings']['Networks'].values()))['IPAddress']
        ssh_probe((address, 443))
        tls = tls_probe((address, 443), stage)
        test_cfg = H.run('docker', 'exec', name, 'cat', '/etc/sslh/sslh.cfg')
        if test_cfg.strip() != cfg_new.strip():
            raise RuntimeError('Temporary sslh loaded the wrong configuration')
        print('Isolated same-image numeric sslh: SSH and TLS passed; TLS seconds', tls, flush=True)
        H.run('docker', 'logs', name, log=stage / 'candidate.log')
    finally:
        result = H.run('docker', 'ps', '-a', '--format', '{{.Names}}').splitlines()
        if name in result:
            owned = H.inspect(name)['Config']['Labels'].get('xui.numeric-check') == stage.name
            if not owned:
                raise RuntimeError('Temporary-container ownership mismatch; refusing deletion')
            H.run('docker', 'rm', '-f', name, log=stage / 'commands.log')
    H.save_json(stage / 'manifest.json', {
        'prepared_at': time.time(), 'file_hashes': H.file_hashes(),
        'container_ids': {s: c['Id'] for s, c in current.items()},
        'image': current['sslh']['Image'], 'before_sha256': digest(original),
        'after_sha256': digest(candidate), 'source_mode': source.stat().st_mode & 0o777,
        'helper_sha256': digest(Path(__file__).with_name('update-containers.py').read_bytes())})
    (stage / 'PREPARED').write_text('Numeric config checked in an isolated same-image sslh; SSH/TLS baseline passed\n')
    print('PREPARED: production configuration unchanged', flush=True)


def compose_up(stage):
    H.run(*H.COMPOSE, '-f', str(stage / 'images.json'), 'up', '-d', '--no-deps',
          '--pull', 'never', '--no-build', '--force-recreate', 'sslh',
          timeout=120, log=stage / 'commands.log')


def apply(stage):
    stage = stage.resolve(strict=True)
    if stage.parent != (ROOT / 'backups').resolve() or not (stage / 'PREPARED').is_file():
        raise RuntimeError('Not a prepared sslh update directory')
    manifest = json.loads((stage / 'manifest.json').read_text())
    source = ROOT / 'docker-compose.yml'
    original = (stage / 'compose.before.yml').read_bytes()
    candidate = (stage / 'compose.after.yml').read_bytes()
    if digest(original) != manifest['before_sha256'] or digest(candidate) != manifest['after_sha256']:
        raise RuntimeError('Prepared Compose files changed')
    if candidate != enable_numeric(original.decode()).encode():
        raise RuntimeError('Prepared mutation is not the single numeric insertion')
    if H.file_hashes() != manifest['file_hashes'] or time.time() - manifest['prepared_at'] > 3600:
        raise RuntimeError('Configuration changed or prepared plan expired')
    if digest(Path(__file__).with_name('update-containers.py').read_bytes()) != manifest['helper_sha256']:
        raise RuntimeError('Validation helper changed')
    _, current, _ = H.preflight()
    if {s: c['Id'] for s, c in current.items()} != manifest['container_ids']:
        raise RuntimeError('Production containers changed')
    print('Fresh XHTTP baseline:', H.smoke(stage), flush=True)
    mutated = False
    success = False
    def interrupted(signum, frame):
        raise RuntimeError(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        mutated = True
        atomic_write(source, candidate, manifest['source_mode'])
        # Ensure interpolation of the persisted YAML matches the rehearsed cfg.
        actual = json.loads(H.run(*H.COMPOSE, 'config', '--format', 'json'))
        if actual['configs']['sslh-cfg']['content'].strip() != (stage / 'sslh.after.cfg').read_text().strip():
            raise RuntimeError('Persisted config differs from the isolated rehearsal')
        print('Recreating only sslh with its existing immutable image', flush=True)
        compose_up(stage)
        if H.inspect('sslh')['Image'] != manifest['image']:
            raise RuntimeError('sslh image unexpectedly changed')
        checks = check_service(stage)
        _, after, _ = H.preflight()
        for name in H.SERVICES:
            if name != 'sslh' and after[name]['Id'] != current[name]['Id']:
                raise RuntimeError(f'Unexpected recreation of {name}')
        if after['sslh']['RestartCount']:
            raise RuntimeError('New sslh restarted unexpectedly')
        H.save_json(stage / 'checks.after.json', checks)
        (stage / 'SUCCESS').write_text('numeric=true active; SSH, TLS and real XHTTP/WARP checks passed\n')
        success = True
        print('SUCCESS:', checks, flush=True)
    finally:
        if mutated and not success:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print('ROLLBACK: restoring previous Compose and recreating only sslh', flush=True)
            atomic_write(source, original, manifest['source_mode'])
            compose_up(stage)
            ssh_probe(('127.0.0.1', 443))
            tls_probe(('127.0.0.1', 443), stage)
            H.smoke(stage)
            (stage / 'ROLLED_BACK').write_text('Previous sslh restored; SSH/TLS/XHTTP checked\n')
            print('ROLLBACK COMPLETE', flush=True)


def main():
    import fcntl
    global H
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--prepare', action='store_true')
    group.add_argument('--apply', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    H = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name('update-containers.py'))))
    with open('/run/lock/xui-container-update.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prepare:
            prepare()
        elif args.apply:
            apply(args.apply)
        else:
            config, _, _ = H.preflight()
            current_cfg = config['configs']['sslh-cfg']['content']
            if re.search(r'^\s*numeric:\s*true;\s*$', current_cfg, re.M):
                live = H.run('docker', 'exec', 'sslh', 'cat', '/etc/sslh/sslh.cfg')
                if current_cfg.strip() != live.strip():
                    raise RuntimeError('Live sslh configuration differs from the Compose source')
                print('Numeric logging is already enabled in Compose and the running sslh.')
                return
            enable_numeric(current_cfg)
            enable_numeric((ROOT / 'docker-compose.yml').read_text())
            print('Plan: add only numeric: true; preserve protocol targets and resolve_on_forward.')
            print('Prepare: test same-image isolated sslh without published ports; check SSH and TLS.')
            print('Apply: back up/replace Compose, recreate only sslh; check SSH/TLS/XHTTP/WARP.')
            print('Failure after mutation: restore previous Compose and recreate previous sslh.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
