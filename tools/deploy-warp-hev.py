#!/usr/bin/env python3
"""One-time migration from this VPS's direct/RU profile to its tested Hev image.

Run --prepare, then --apply DIR in a detached systemd job. --rollback DIR
restores the prior Xray template, WARP image and operational files.
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
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path('/opt/x-ui')
IMAGE = 'local/microwarp-hev:0.3.1-hev2.13.1'
IMAGE_ID = 'sha256:f2685e0be27e54c7f9efe046b58eb5b9ba06f4b09dd8ecba458de478008ad42e'
BASE = ['docker', 'compose', '--project-directory', str(ROOT), '-p', 'x-ui',
        '-f', str(ROOT / 'docker-compose.yml'), '-f', str(ROOT / 'docker-compose.warp.yml')]
COMPOSE = BASE + ['-f', str(ROOT / 'docker-compose.warp-udp.yml')]
H = B = E = P = N = None


def hashes():
    names = ['docker-compose.yml', 'docker-compose.warp.yml', 'docker-compose.warp-udp.yml',
             'Caddyfile', 'config/warp/Dockerfile', 'config/warp/microsocks-compat.sh']
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def atomic_file(path, data, mode):
    tmp = path.with_name(path.name + '.hev-tmp')
    tmp.write_bytes(data)
    tmp.chmod(mode)
    os.replace(tmp, path)


def wait_warp():
    for _ in range(75):
        state = H.inspect('warp')['State']
        if state['Running'] and state.get('Health', {}).get('Status') == 'healthy':
            return
        time.sleep(1)
    raise RuntimeError('WARP did not become healthy')


def udp_checks(stage, warp):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 29989))
    with (stage / 'smoke.log').open('a') as log:
        proc = subprocess.Popen([str(stage / 'xray-smoke'), 'run', '-c', str(stage / 'smoke.json')],
                                stdout=log, stderr=log)
        try:
            for _ in range(40):
                if proc.poll() is not None:
                    raise RuntimeError('UDP XHTTP client exited')
                try:
                    with socket.create_connection(('127.0.0.1', 29989), timeout=.2):
                        break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError('UDP XHTTP client did not start')
            proxy = ('127.0.0.1', 29989)
            result = {'dns': N.udp_probe(proxy), 'stun': P.stun_probe(proxy, vars(N))}
            trace = H.run('curl', '-4fsS', '--max-time', '10', '--noproxy', '*',
                          'https://1.1.1.1/cdn-cgi/trace')
            direct = dict(line.split('=', 1) for line in trace.splitlines() if '=' in line)
            if direct.get('warp') != 'off' or not direct.get('ip'):
                raise RuntimeError('Could not establish the direct host IPv4 baseline')
            if warp == (result['stun']['ip'] == direct['ip']):
                raise RuntimeError('UDP exit does not match the selected egress')
            with socket.create_connection(proxy, timeout=3) as control:
                control.settimeout(3)
                relay, port = N.socks_connect(control, '0.0.0.0', 0, udp=True)
                if relay == '0.0.0.0':
                    relay = proxy[0]
                query = bytes.fromhex('4a3301000001000000000000') + b'\x07example\x03com\x00\x00\x01\x00\x01'
                packet = bytes.fromhex('00000001') + socket.inet_aton('77.88.8.8') + b'\x00\x35' + query
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.settimeout(2)
                    udp.sendto(packet, (relay, port))
                    try:
                        udp.recvfrom(4096)
                    except socket.timeout:
                        result['ru_udp_blocked'] = True
                    else:
                        raise RuntimeError('Russian UDP destination was not blocked')
            return result
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def checks(stage, warp):
    return {'http': B.run_checks(stage, blocked=True, expected_warp='on' if warp else 'off'),
            'udp': udp_checks(stage, warp)}


def prepare():
    H.check_project_profile()
    check_geodata()
    containers = {name: H.inspect(name) for name in H.SERVICES}
    if any(not c['State']['Running'] for c in containers.values()):
        raise RuntimeError('A production service is not running')
    if containers['warp']['Image'] == IMAGE_ID:
        raise RuntimeError('Candidate is already deployed')
    if json.loads(H.run('docker', 'image', 'inspect', IMAGE))[0]['Id'] != IMAGE_ID:
        raise RuntimeError('Candidate image differs from the tested image')
    services = json.loads(H.run(*COMPOSE, 'config', '--format', 'json'))['services']
    if set(services) != set(H.SERVICES) or services['warp']['image'] != IMAGE or services['warp'].get('ports'):
        raise RuntimeError('Unexpected Compose configuration')
    runtime = json.loads(H.run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
    if runtime['outbounds'][0]['tag'] != 'direct':
        raise RuntimeError('Expected direct egress before deployment')
    ru_rules = [r for r in runtime['routing']['rules'] if r.get('domain') == ['geosite:category-ru']
                or r.get('ip') == ['geoip:ru']]
    if len(ru_rules) != 2 or any(r.get('outboundTag') != 'blocked' for r in ru_rules):
        raise RuntimeError('Expected active Russian destination blocks')
    db = Path(next(m['Source'] for m in containers['3x-ui']['Mounts']
                   if m['Destination'] == '/etc/x-ui')) / 'x-ui.db'
    template = E.read_template(db)
    new = json.dumps(E.transform(json.loads(template)))
    candidate = E.transform(runtime)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = ROOT / 'backups' / ('deploy-warp-hev-' + stamp)
    stage.mkdir(mode=0o700)
    H.save_json(stage / 'candidate.json', candidate)
    H.save_json(stage / 'runtime.before.json', runtime)
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as source:
        with sqlite3.connect(stage / 'x-ui.db') as backup:
            source.backup(backup)
    warp_volume = next(m['Source'] for m in containers['warp']['Mounts'] if m['Destination'] == '/etc/wireguard')
    H.run('tar', '-czf', str(stage / 'warp-data.tar.gz'), '-C', warp_volume, '.')
    if H.LOCAL_WARP_IMAGE != IMAGE or H.COMPOSE != COMPOSE:
        raise RuntimeError('Install the Hev-aware update-containers.py from this checkout first')
    paths = [Path('/etc/cron.d/xray-cert-reload'), ROOT / '.env']
    originals = [p.read_bytes() for p in paths]
    cron = originals[0].decode()
    lines = [line for line in cron.splitlines() if line.startswith('0 3 * * 0 root ') and 'docker compose' in line]
    if len(lines) != 1:
        raise RuntimeError('Unexpected weekly update cron')
    dc = 'docker compose -f docker-compose.yml -f docker-compose.warp.yml -f docker-compose.warp-udp.yml'
    command = ('cd "/opt/x-ui" && ' + dc + ' pull --quiet --ignore-buildable && ' + dc
               + ' up -d --no-build --pull never --remove-orphans')
    H.run('bash', '-n', '-c', command)
    replacement = ("0 3 * * 0 root /usr/bin/flock -n /run/lock/xui-container-update.lock /bin/bash -c '"
                   + command + "' >> /var/log/xray-image-update.log 2>&1")
    env = originals[1].decode()
    if re.search(r'(?m)^PROXY_MODE=', env):
        env = re.sub(r'(?m)^PROXY_MODE=.*$', 'PROXY_MODE=warp', env)
    else:
        env += '\nPROXY_MODE=warp\n'
    updates = [cron.replace(lines[0], replacement).encode(), env.encode()]
    files = []
    for index, (path, old, updated) in enumerate(zip(paths, originals, updates)):
        (stage / f'file-{index}.old').write_bytes(old)
        (stage / f'file-{index}.new').write_bytes(updated)
        files.append({'path': str(path), 'mode': path.stat().st_mode & 0o777})
    H.make_smoke(stage, runtime)
    smoke = json.loads((stage / 'smoke.json').read_text())
    smoke['inbounds'][0]['settings']['udp'] = True
    H.save_json(stage / 'smoke.json', smoke)
    H.run('docker', 'run', '--rm', '--network', 'none', '--read-only',
          '--mount', f'type=bind,src={stage / "candidate.json"},dst=/candidate.json,readonly',
          '--entrypoint', '/app/bin/xray-linux-amd64', containers['3x-ui']['Image'],
          'run', '-test', '-c', '/candidate.json', log=stage / 'commands.log')
    H.save_json(stage / 'baseline.json', checks(stage, warp=False))
    state = {'db': str(db), 'old_template': template, 'new_template': new, 'files': files,
             'prepared_at': time.time(), 'hashes': hashes(), 'warp_volume': warp_volume,
             'warp_public_key': H.run('docker', 'exec', 'warp', 'wg', 'show', 'wg0', 'public-key'),
             'containers': {n: {'id': c['Id'], 'image': c['Image'], 'ref': c['Config']['Image']}
                            for n, c in containers.items()}}
    H.save_json(stage / 'state.json', state)
    (stage / 'PREPARED').write_text('Image, candidate, backups and full XHTTP TCP/UDP baseline checked\n')
    print('PREPARED:', stage, flush=True)


def set_template(state, reverse=False):
    source, target = ('new_template', 'old_template') if reverse else ('old_template', 'new_template')
    with sqlite3.connect(state['db'], timeout=20) as conn:
        cursor = conn.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig' AND value=?",
                              (state[target], state[source]))
        if cursor.rowcount != 1:
            raise RuntimeError('Xray template changed concurrently')


def check_geodata():
    markers = ('geodata-bootstrap-plan.json', 'pending-youtube-cache-exception.json',
               'geodata/pending.json', 'geodata/last-applied.json', 'geodata/current')
    if any(os.path.lexists(ROOT / name) for name in markers):
        raise RuntimeError('This historical migration predates managed geodata; refusing to change WARP')


def restore(stage, state, template_written, warp_changed, *, partial=False, panel_stop_attempted=False):
    # A failed forward apply can enter recovery with the originally running
    # panel already stopped. Its start must also cover preflight exceptions.
    restart_panel = panel_stop_attempted
    try:
        check_geodata()
        expected = state['new_template' if template_written else 'old_template']
        if E.read_template(Path(state['db'])) != expected:
            raise RuntimeError('Xray template changed concurrently; refusing rollback')
        files = []
        for index, item in enumerate(state['files']):
            path = Path(item['path'])
            old = (stage / f'file-{index}.old').read_bytes()
            new = (stage / f'file-{index}.new').read_bytes()
            current = path.read_bytes()
            if current != new and not (partial and current == old):
                raise RuntimeError('Operational file changed concurrently; refusing rollback')
            files.append((path, old, current, item['mode']))
        if warp_changed and not (stage / 'warp-data.tar.gz').is_file():
            raise RuntimeError('Missing WireGuard backup; refusing rollback')
        if not partial and not H.inspect('3x-ui')['State']['Running']:
            raise RuntimeError('Expected a running 3x-ui before historical rollback')
        restart_panel = True
        H.run('docker', 'stop', '--time', '20', '3x-ui')
        if template_written:
            set_template(state, reverse=True)
        for path, old, previous, mode in files:
            if path.read_bytes() != previous:
                raise RuntimeError('Operational file changed during rollback; refusing overwrite')
            if old != previous:
                atomic_file(path, old, mode)
        if warp_changed:
            H.run('docker', 'stop', '--time', '15', 'warp')
            H.run('tar', '-xzf', str(stage / 'warp-data.tar.gz'), '-C', state['warp_volume'])
            old = state['containers']['warp']
            H.run('docker', 'tag', old['image'], old['ref'])
            H.run(*BASE, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', 'warp')
            wait_warp()
    finally:
        if restart_panel:
            H.run('docker', 'start', '3x-ui')
    E.wait_panel()
    H.save_json(stage / 'checks.rollback.json', checks(stage, warp=False))
    (stage / 'ROLLED_BACK').write_text('Prior image, routing, files and full XHTTP TCP/UDP restored\n')
    print('ROLLBACK COMPLETE', flush=True)


def apply(stage, reverse=False):
    H.check_project_profile()
    check_geodata()
    stage = stage.resolve(strict=True)
    if stage.parent != (ROOT / 'backups').resolve() or not (stage / 'PREPARED').is_file():
        raise RuntimeError('Not a prepared deployment')
    state = json.loads((stage / 'state.json').read_text())
    if hashes() != state['hashes']:
        raise RuntimeError('Deployment inputs changed')
    if E.read_template(Path(state['db'])) != state['new_template' if reverse else 'old_template']:
        raise RuntimeError('Template changed since preparation')
    for index, item in enumerate(state['files']):
        expected = (stage / f'file-{index}.{"new" if reverse else "old"}').read_bytes()
        if Path(item['path']).read_bytes() != expected:
            raise RuntimeError('Operational file changed since preparation')
    if reverse:
        if H.inspect('warp')['Image'] != IMAGE_ID:
            raise RuntimeError('Unexpected WARP image before rollback')
        restore(stage, state, template_written=True, warp_changed=True)
        return
    if time.time() - state['prepared_at'] > 3600:
        raise RuntimeError('Prepared deployment expired')
    if any(H.inspect(n)['Id'] != c['id'] for n, c in state['containers'].items()):
        raise RuntimeError('Production containers changed')
    if json.loads(H.run('docker', 'image', 'inspect', IMAGE))[0]['Id'] != IMAGE_ID:
        raise RuntimeError('Candidate image changed')
    print('Fresh baseline:', checks(stage, warp=False), flush=True)
    if not H.inspect('3x-ui')['State']['Running']:
        raise RuntimeError('Expected a running 3x-ui before migration')
    touched = written = warp_changed = success = panel_stop_attempted = False
    def interrupted(signum, frame):
        raise RuntimeError(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        touched = True
        for index, item in enumerate(state['files']):
            atomic_file(Path(item['path']), (stage / f'file-{index}.new').read_bytes(), item['mode'])
        warp_changed = True
        H.run(*COMPOSE, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', 'warp')
        wait_warp()
        warp = H.inspect('warp')
        if warp['Image'] != IMAGE_ID or H.run('docker', 'exec', 'warp', 'cat', '/proc/1/comm') != 'hev-socks5-serv':
            raise RuntimeError('WARP is not running the tested Hev process')
        if H.run('docker', 'exec', 'warp', 'wg', 'show', 'wg0', 'public-key') != state['warp_public_key']:
            raise RuntimeError('WireGuard identity changed')
        address = warp['NetworkSettings']['Networks']['x-ui_xray-net']['IPAddress']
        P.tcp_probe((address, 1080))
        N.udp_probe((address, 1080))
        panel_stop_attempted = True
        H.run('docker', 'stop', '--time', '20', '3x-ui')
        set_template(state)
        written = True
        H.run('docker', 'start', '3x-ui')
        panel_stop_attempted = False
        E.wait_panel()
        actual = json.loads(H.run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
        expected = json.loads((stage / 'candidate.json').read_text())
        if any(actual[k] != expected[k] for k in ('routing', 'outbounds', 'inbounds')):
            raise RuntimeError('Generated Xray config differs from the validated candidate')
        H.save_json(stage / 'checks.after.json', checks(stage, warp=True))
        if any(H.inspect(n)['Id'] != state['containers'][n]['id'] for n in ('sslh', 'caddy', 'certbot', '3x-ui')):
            raise RuntimeError('An unrelated container was recreated')
        (stage / 'SUCCESS').write_text('MicroWARP/Hev deployed; real XHTTP TCP/UDP and RU blocks verified\n')
        success = True
        print('SUCCESS:', stage, flush=True)
    finally:
        if touched and not success:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            restore(stage, state, template_written=written, warp_changed=warp_changed,
                    partial=True, panel_stop_attempted=panel_stop_attempted)


def main():
    import fcntl
    global H, B, E, P, N
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare', action='store_true')
    group.add_argument('--apply', type=Path)
    group.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    modules = ['update-containers.py', 'block-ru.py', 'enable-warp.py', 'probe-warp-socks.py', 'probe-stack.py']
    H, B, E, P, N = [SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name(name)))) for name in modules]
    with open('/run/lock/xui-container-update.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prepare:
            prepare()
        else:
            apply(args.rollback or args.apply, reverse=bool(args.rollback))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
