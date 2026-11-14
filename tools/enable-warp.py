#!/usr/bin/env python3
"""Historical migration of the direct-only VPS to TCP-only MicroWARP/MicroSOCKS.

Default: read-only plan. --apply: preflight, backup, validate, switch, smoke test;
on failure restore the previous template and operational files automatically.
"""
import argparse
import copy
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import time

ROOT = Path('/opt/x-ui')
CRON = Path('/etc/cron.d/xray-cert-reload')
IMAGE_ID = 'sha256:d5b709efd49dbec3a965c617eb263b46cdf1449e65776d1b9a351927439b2b17'
IMAGE_TAG = 'ghcr.io/ccbkkb/microwarp:latest'
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'docker-compose.yml'),
           '-f', str(ROOT / 'docker-compose.warp.yml')]


def run(*args, timeout=90):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        # Commands may print UUIDs or key material on errors. Keep output private.
        raise RuntimeError(f'{args[0]} command failed (exit {p.returncode})')
    return p.stdout.strip()


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def read_template(db):
    with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True)) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()
        if not row:
            raise RuntimeError('xrayTemplateConfig is absent')
        return row[0]


def transform(config):
    result = copy.deepcopy(config)
    if any(o.get('tag') == 'warp-out' for o in result['outbounds']):
        raise RuntimeError('WARP is already configured; inspect manually')
    result['outbounds'].insert(0, {
        'tag': 'warp-out', 'protocol': 'socks',
        'settings': {'servers': [{'address': 'warp', 'port': 1080}]}})
    rules = result['routing']['rules']
    # Used by both historical migrations (MicroSOCKS and Hev). Preserve
    # existing blocks, and reject catch-alls that could shadow the WARP route.
    for rule in rules:
        if not any(k in rule for k in ('inboundTag', 'ip', 'domain', 'protocol', 'user')):
            raise RuntimeError('Unexpected broad routing rule; refusing to reorder it')
    rules.append({'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'warp-out'})
    return result


def wait_panel():
    for _ in range(45):
        c = inspect('3x-ui')
        if c['State']['Running'] and c['State'].get('Health', {}).get('Status') == 'healthy':
            run('docker', 'exec', '3x-ui', 'pgrep', '-f', 'xray-linux-amd64')
            return
        time.sleep(2)
    raise RuntimeError('3x-ui/Xray did not become healthy')


def smoke(binary, config, log):
    # This client traverses sslh -> Caddy -> the real VLESS/XHTTP inbound.
    # A listener is bound only to host loopback; it is removed on every exit.
    with log.open('a') as output:
        proc = subprocess.Popen([str(binary), 'run', '-c', str(config)],
                                stdout=output, stderr=output)
        try:
            for _ in range(30):
                if proc.poll() is not None:
                    raise RuntimeError('Temporary XHTTP client failed to start')
                try:
                    with socket.create_connection(('127.0.0.1', 29989), timeout=.2):
                        break
                except OSError:
                    time.sleep(.2)
            return run('curl', '--fail', '--silent', '--show-error', '--noproxy', '',
                       '--connect-timeout', '10', '--max-time', '25',
                       '--socks5-hostname', '127.0.0.1:29989',
                       'https://1.1.1.1/cdn-cgi/trace', timeout=30)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    container = inspect('3x-ui')
    db = Path(next(m['Source'] for m in container['Mounts']
                   if m['Destination'] == '/etc/x-ui')) / 'x-ui.db'
    old_template = read_template(db)
    template = transform(json.loads(old_template))
    runtime = json.loads(run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
    candidate = transform(runtime)
    services = json.loads(run(*COMPOSE, 'config', '--format', 'json'))['services']
    if services['warp']['image'] != IMAGE_TAG:
        raise RuntimeError('Unexpected WARP image reference')
    if json.loads(run('docker', 'image', 'inspect', IMAGE_TAG))[0]['Id'] != IMAGE_ID:
        raise RuntimeError('WARP image changed since the successful probe')
    names = run('docker', 'ps', '-a', '--format', '{{.Names}}').splitlines()
    if 'warp' in names or 'xui-warp-probe' in names:
        raise RuntimeError('Existing WARP container or unfinished probe; refusing conflict')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 29989))
    identity = ROOT / 'backups/warp-probe-identity/wg0.conf'
    if not identity.is_file():
        raise RuntimeError('The tested WARP identity is absent')
    cron_old = CRON.read_bytes()
    old_command = 'docker compose -f docker-compose.yml'
    new_command = old_command + ' -f docker-compose.warp.yml'
    cron_text = cron_old.decode()
    if cron_text.count(old_command) != 2 or 'docker-compose.warp.yml' in cron_text:
        raise RuntimeError('Unexpected cron command; refusing blind replacement')
    print('Plan: reuse the tested WARP image and identity; start only warp.', flush=True)
    print('Validate a complete candidate with the installed Xray before stopping 3x-ui.', flush=True)
    print('Preserve existing blocks. MicroSOCKS carries TCP; UDP routed to it will fail.', flush=True)
    print('Back up SQLite and files; update only xrayTemplateConfig, .env and image-update cron.', flush=True)
    print('Restart only 3x-ui; verify the real sslh/Caddy/VLESS/XHTTP chain reports warp=on.', flush=True)
    print('On failure: restore the old template/files, start 3x-ui and remove the new warp container.', flush=True)
    if not args.apply:
        return

    with open('/run/lock/xui-container-update.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        apply(db, old_template, template, runtime, candidate, identity, cron_old,
              cron_text.replace(old_command, new_command))


def apply(db, old_template, template, runtime, candidate, identity, cron_old, cron_new):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / 'backups' / ('enable-warp-' + stamp)
    backup.mkdir(mode=0o700)
    print('Backup:', backup, flush=True)
    paths = [ROOT / '.env', ROOT / 'docker-compose.warp.yml', CRON]
    originals = {p: (p.read_bytes(), p.stat().st_mode & 0o777) for p in paths}
    for p, (data, _) in originals.items():
        (backup / p.name).write_bytes(data)
    (backup / 'old-template.json').write_text(old_template)
    (backup / 'new-template.json').write_text(json.dumps(template, indent=2))
    (backup / 'runtime-before.json').write_text(json.dumps(runtime, indent=2))
    (backup / 'candidate.json').write_text(json.dumps(candidate, indent=2))
    with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup / 'x-ui.db')) as dest:
            source.backup(dest)

    binary = backup / 'xray-smoke'
    run('docker', 'cp', '3x-ui:/app/bin/xray-linux-amd64', str(binary))
    binary.chmod(0o700)
    inbound = next(i for i in runtime['inbounds'] if i.get('protocol') == 'vless'
                   and i.get('streamSettings', {}).get('network') == 'xhttp')
    xhttp = inbound['streamSettings']['xhttpSettings']
    domain = xhttp['host']
    client = {'log': {'loglevel': 'warning'},
              'inbounds': [{'listen': '127.0.0.1', 'port': 29989, 'protocol': 'socks',
                            'settings': {'auth': 'noauth'}}],
              'outbounds': [{'protocol': 'vless', 'settings': {'vnext': [{
                  'address': domain, 'port': 443, 'users': [{
                      'id': inbound['settings']['clients'][0]['id'], 'encryption': 'none'}]}]},
                  'streamSettings': {'network': 'xhttp', 'security': 'tls',
                      'tlsSettings': {'serverName': domain, 'alpn': ['h2'], 'fingerprint': 'firefox'},
                      'xhttpSettings': {'host': domain, 'path': xhttp['path'], 'mode': 'stream-up'}}}]}
    smoke_config = backup / 'smoke-client.json'
    smoke_config.write_text(json.dumps(client))
    baseline = smoke(binary, smoke_config, backup / 'smoke.log')
    if 'warp=off' not in baseline:
        raise RuntimeError('Baseline real XHTTP chain did not report warp=off')
    print('Baseline real XHTTP chain OK: warp=off', flush=True)

    mutated = False
    warp_created = False
    template_written = False
    stopped = False
    success = False
    def interrupted(signum, frame):
        raise RuntimeError(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        mutated = True
        warp_file = ROOT / 'docker-compose.warp.yml'
        warp_text = warp_file.read_text()
        if '- ALLOW_NO_AUTH=1' not in warp_text or 'ENABLE_IPV6=' in warp_text:
            raise RuntimeError('Unexpected WARP environment')
        warp_file.write_text(warp_text.replace('- ALLOW_NO_AUTH=1',
                             '- ALLOW_NO_AUTH=1\n      - ENABLE_IPV6=0'))
        warp_file.chmod(originals[warp_file][1])
        volume = services_volume()
        run('docker', 'volume', 'create', volume)
        mount = Path(json.loads(run('docker', 'volume', 'inspect', volume))[0]['Mountpoint'])
        if any(mount.iterdir()):
            raise RuntimeError('WARP volume is not empty; refusing identity replacement')
        shutil.copy2(identity, mount / 'wg0.conf')
        (mount / 'wg0.conf').chmod(0o600)
        warp_created = True  # cleanup also covers partially successful Compose up
        run(*COMPOSE, 'up', '-d', '--no-deps', '--pull', 'never', 'warp')
        for _ in range(60):
            if inspect('warp')['State'].get('Health', {}).get('Status') == 'healthy':
                break
            time.sleep(2)
        else:
            raise RuntimeError('WARP not healthy')
        print('Persistent warp healthy', flush=True)
        run('docker', 'cp', str(backup / 'candidate.json'), '3x-ui:/tmp/xui-warp-candidate.json')
        run('docker', 'exec', '3x-ui', '/app/bin/xray-linux-amd64', 'run', '-test',
            '-c', '/tmp/xui-warp-candidate.json')
        print('Complete candidate accepted by installed Xray', flush=True)

        env_path = ROOT / '.env'
        env = env_path.read_text()
        env = re.sub(r'^PROXY_MODE=.*$', 'PROXY_MODE=warp', env, flags=re.M) if re.search(
            r'^PROXY_MODE=', env, re.M) else env.rstrip() + '\nPROXY_MODE=warp\n'
        env_path.write_text(env)
        env_path.chmod(originals[env_path][1])
        CRON.write_text(cron_new)
        CRON.chmod(originals[CRON][1])
        stopped = True
        run('docker', 'stop', '--time', '20', '3x-ui')
        with closing(sqlite3.connect(db, timeout=20)) as conn, conn:
            cur = conn.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig' AND value=?",
                               (json.dumps(template), old_template))
            if cur.rowcount != 1:
                raise RuntimeError('Template changed concurrently')
        template_written = True
        run('docker', 'start', '3x-ui')
        wait_panel()
        actual = json.loads(run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
        if actual['outbounds'][0]['tag'] != 'warp-out' or actual['routing']['rules'][-1].get('outboundTag') != 'warp-out':
            raise RuntimeError('Running config does not match the intended WARP route')
        result = smoke(binary, smoke_config, backup / 'smoke.log')
        if 'warp=on' not in result and 'warp=plus' not in result:
            raise RuntimeError('Real XHTTP chain did not use WARP')
        (backup / 'trace-after.txt').write_text(result)
        print('\n'.join(l for l in result.splitlines() if l.startswith(('warp=', 'ip=', 'loc='))), flush=True)
        success = True
        (backup / 'SUCCESS').write_text('Real sslh/Caddy/VLESS/XHTTP chain verified through WARP\n')
        print('SUCCESS: Xray output is now warp:1080', flush=True)
    finally:
        if not success and mutated:
            print('FAILED: restoring previous configuration', flush=True)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if stopped:
                run('docker', 'stop', '--time', '20', '3x-ui')
            try:
                if template_written:
                    with closing(sqlite3.connect(db, timeout=20)) as conn, conn:
                        changed = conn.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig' AND value=?",
                                               (old_template, json.dumps(template)))
                        if changed.rowcount != 1:
                            raise RuntimeError('Template changed during migration; manual recovery required')
                for p, (data, mode) in originals.items():
                    p.write_bytes(data)
                    p.chmod(mode)
            finally:
                if stopped:
                    run('docker', 'start', '3x-ui')
            if stopped:
                wait_panel()
            if warp_created:
                if 'warp' in run('docker', 'ps', '-a', '--format', '{{.Names}}').splitlines():
                    run('docker', 'rm', '-f', 'warp')
            (backup / 'ROLLED_BACK').write_text('Old template and operational files restored\n')
            print('ROLLBACK COMPLETE', flush=True)


def services_volume():
    conf = json.loads(run(*COMPOSE, 'config', '--format', 'json'))
    source = next(v['source'] for v in conf['services']['warp']['volumes']
                  if v['target'] == '/etc/wireguard')
    return conf['volumes'][source]['name']


if __name__ == '__main__':
    main()
