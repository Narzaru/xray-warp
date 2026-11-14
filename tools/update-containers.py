#!/usr/bin/env python3
"""VPS container update: --prepare downloads/checks; --apply DIR uses that plan.

Only a Caddy image change is supported by this rollout. Registry image tags
are refreshed, the local WARP build is inspected; an unexpected change aborts
before any production container is restarted.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path('/opt/x-ui')
SERVICES = ('warp', '3x-ui', 'caddy', 'sslh', 'certbot')
FILES = (ROOT / 'docker-compose.yml', ROOT / 'docker-compose.warp.yml',
         ROOT / '.env', ROOT / 'Caddyfile', Path('/etc/cron.d/xray-cert-reload'),
         ROOT / 'docker-compose.warp-udp.yml', ROOT / 'config/warp/Dockerfile',
         ROOT / 'config/warp/microsocks-compat.sh')
COMPOSE = ['docker', 'compose', '--project-directory', str(ROOT), '-p', 'x-ui',
           '-f', str(FILES[0]), '-f', str(FILES[1]), '-f', str(FILES[5])]
LOCAL_WARP_IMAGE = 'local/microwarp-hev:0.3.1-hev2.13.1'
SMOKE_PORT = 29989


def run(*args, timeout=90, log=None):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if log:
        with Path(log).open('a') as out:
            out.write(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'{args[0]} command failed: exit {result.returncode}; see private command log')
    return result.stdout.strip()


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def check_project_profile(*, recovering_panel=False):
    # Containers in the live project were created with different historical
    # -f lists. Check service membership and the network itself, not a single
    # container's config_files label.
    services = run('docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=x-ui',
                   '--format', '{{.Label "com.docker.compose.service"}}').splitlines()
    expected = set(SERVICES)
    if set(services) != expected and not (recovering_panel and set(services) == expected - {'3x-ui'}):
        raise RuntimeError('Maintenance requires the five-service x-ui WARP profile')
    network = json.loads(run('docker', 'network', 'inspect', 'x-ui_xray-net'))[0]
    if network['EnableIPv6']:
        raise RuntimeError('Maintenance only supports the tested IPv4 Docker bridge')


def file_hashes():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in FILES}


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2))


def refresh_image(service, config, log):
    ref = config['image']
    if config.get('build'):
        if service != 'warp' or ref != LOCAL_WARP_IMAGE or config.get('pull_policy') != 'never':
            raise RuntimeError('Unexpected locally built service')
        print(f'Inspect pinned local image {service}: {ref}', flush=True)
    else:
        print(f'Pull/check {service}: {ref}', flush=True)
        run('docker', 'pull', '--quiet', ref, timeout=300, log=log)
    return json.loads(run('docker', 'image', 'inspect', ref))[0]['Id']


def smoke(stage):
    """Traverse actual sslh/Caddy/VLESS/XHTTP to an external HTTPS endpoint."""
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', SMOKE_PORT))
    with (stage / 'smoke.log').open('a') as log:
        proc = subprocess.Popen([str(stage / 'xray-smoke'), 'run', '-c', str(stage / 'smoke.json')],
                                stdout=log, stderr=log)
        try:
            ready = False
            for _ in range(40):
                if proc.poll() is not None:
                    raise RuntimeError('Smoke client exited before readiness')
                try:
                    with socket.create_connection(('127.0.0.1', SMOKE_PORT), timeout=.2):
                        ready = True
                        break
                except OSError:
                    time.sleep(.2)
            if not ready:
                raise RuntimeError('Smoke client not ready')
            trace = run('curl', '--fail', '--silent', '--show-error', '--noproxy', '',
                        '--connect-timeout', '10', '--max-time', '25',
                        '--socks5-hostname', f'127.0.0.1:{SMOKE_PORT}',
                        'https://1.1.1.1/cdn-cgi/trace', timeout=30,
                        log=stage / 'commands.log')
            if not any(line in ('warp=on', 'warp=plus') for line in trace.splitlines()):
                raise RuntimeError('Real XHTTP chain did not use WARP')
            return {k: v for line in trace.splitlines() if '=' in line
                    for k, v in [line.split('=', 1)] if k in ('ip', 'loc', 'warp')}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def make_smoke(stage, config):
    inbound = next(i for i in config['inbounds'] if i.get('protocol') == 'vless'
                   and i.get('streamSettings', {}).get('network') == 'xhttp')
    settings = inbound['streamSettings']['xhttpSettings']
    domain = settings['host']
    client = {'log': {'loglevel': 'warning'},
              'inbounds': [{'listen': '127.0.0.1', 'port': SMOKE_PORT,
                            'protocol': 'socks', 'settings': {'auth': 'noauth'}}],
              'outbounds': [{'protocol': 'vless', 'settings': {'vnext': [{
                  'address': domain, 'port': 443, 'users': [{
                      'id': inbound['settings']['clients'][0]['id'], 'encryption': 'none'}]}]},
                  'streamSettings': {'network': 'xhttp', 'security': 'tls',
                      'tlsSettings': {'serverName': domain, 'alpn': ['h2'], 'fingerprint': 'firefox'},
                      'xhttpSettings': {'host': domain, 'path': settings['path'], 'mode': 'stream-up'}}}]}
    save_json(stage / 'smoke.json', client)
    run('docker', 'cp', '3x-ui:/app/bin/xray-linux-amd64', str(stage / 'xray-smoke'))
    (stage / 'xray-smoke').chmod(0o700)


def preflight():
    check_project_profile()
    config = json.loads(run(*COMPOSE, 'config', '--format', 'json'))
    if set(config['services']) != set(SERVICES):
        raise RuntimeError('Unexpected Compose service set')
    current = {name: inspect(name) for name in SERVICES}
    for name, container in current.items():
        if not container['State']['Running']:
            raise RuntimeError(f'{name} is not running')
        health = container['State'].get('Health', {}).get('Status')
        if health and health != 'healthy':
            raise RuntimeError(f'{name} is not healthy')
        labels = container['Config']['Labels']
        if labels.get('com.docker.compose.project') != 'x-ui':
            raise RuntimeError(f'{name}: unexpected Compose project')
    if os.statvfs(ROOT).f_bavail * os.statvfs(ROOT).f_frsize < 2 * 1024**3:
        raise RuntimeError('Less than 2 GiB available')
    sslh_config = run('docker', 'exec', 'sslh', 'cat', '/etc/sslh/sslh.cfg')
    tls = next(line for line in sslh_config.splitlines() if 'name: "tls"' in line)
    if 'resolve_on_forward: true' not in tls:
        raise RuntimeError('sslh does not re-resolve the Caddy upstream')
    runtime = json.loads(run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
    if runtime['outbounds'][0]['tag'] != 'warp-out':
        raise RuntimeError('Expected active WARP output')
    return config, current, runtime


def prepare():
    config, current, runtime = preflight()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = ROOT / 'backups' / ('container-update-' + stamp)
    stage.mkdir(mode=0o700)
    print(f'Preparation directory: {stage}', flush=True)
    save_json(stage / 'compose.resolved.json', config)
    save_json(stage / 'runtime.before.json', runtime)
    save_json(stage / 'inspect.before.json', current)
    for n, path in enumerate(FILES):
        (stage / f'file-{n}.backup').write_bytes(path.read_bytes())
    make_smoke(stage, runtime)
    print('Baseline:', smoke(stage), flush=True)
    new_images = {}
    refs = {s: config['services'][s]['image'] for s in SERVICES}
    try:
        for service in SERVICES:
            new_images[service] = refresh_image(service, config['services'][service],
                                                stage / 'commands.log')
        changed = [s for s in SERVICES if current[s]['Image'] != new_images[s]]
        print('Changed images:', ', '.join(changed) or 'none', flush=True)
        if set(changed) - {'caddy'}:
            raise RuntimeError('Changes beyond Caddy require a separate compatibility rehearsal')
        version = run('docker', 'run', '--rm', '--network', 'none',
                      '--entrypoint', 'caddy', new_images['caddy'], 'version',
                      log=stage / 'commands.log')
        print('Candidate Caddy:', version, flush=True)
        if changed and not version.startswith('v2.11.8 '):
            raise RuntimeError('Unexpected Caddy version; review release before deployment')
        run('docker', 'run', '--rm', '--network', 'x-ui_xray-net',
            '--volumes-from', 'caddy:ro', '--entrypoint', 'caddy', new_images['caddy'],
            'validate', '--config', '/etc/caddy/Caddyfile', log=stage / 'commands.log')
        # After geodata bootstrap the panel loads a host-managed release.
        # Rehearse against the same files instead of the pair in the image.
        asset_args = []
        asset_env = next((e for e in current['3x-ui']['Config']['Env']
                          if e.startswith('XRAY_LOCATION_ASSET=')), None)
        if asset_env and asset_env != 'XRAY_LOCATION_ASSET=/app/bin':
            mount = next((m for m in current['3x-ui']['Mounts'] if m['Destination'] == '/var/lib/xray/geodata'), None)
            if (asset_env != 'XRAY_LOCATION_ASSET=/var/lib/xray/geodata/current' or not mount
                    or mount['RW'] or mount['Source'] != str(ROOT / 'geodata')):
                raise RuntimeError('Unexpected Xray asset location; cannot rehearse safely')
            asset_args = ['-e', asset_env, '--mount',
                          f'type=bind,src={mount["Source"]},dst={mount["Destination"]},readonly']
        run('docker', 'run', '--rm', '--network', 'x-ui_xray-net', *asset_args,
            '--mount', f'type=bind,src={stage / "runtime.before.json"},dst=/tmp/config.json,readonly',
            '--entrypoint', '/app/bin/xray-linux-amd64', new_images['3x-ui'],
            'run', '-test', '-c', '/tmp/config.json', log=stage / 'commands.log')
        manifest = {'prepared_at': time.time(), 'files': file_hashes(), 'refs': refs,
                    'old_images': {s: current[s]['Image'] for s in SERVICES},
                    'new_images': new_images, 'changed': changed, 'caddy_version': version,
                    'container_ids': {s: current[s]['Id'] for s in SERVICES}}
        save_json(stage / 'new-images.json', {'services': {s: {'image': new_images[s]} for s in SERVICES}})
        save_json(stage / 'old-images.json', {'services': {s: {'image': current[s]['Image']} for s in SERVICES}})
        save_json(stage / 'manifest.json', manifest)
        (stage / 'PREPARED').write_text('Caddy config and full Xray config validated; baseline passed\n')
        print('PREPARED: production containers unchanged', flush=True)
    except BaseException:
        # Prevent the weekly cron from later deploying a rejected floating tag.
        for service in new_images:
            run('docker', 'tag', current[service]['Image'], refs[service])
        raise


def compose_up(stage, images, service):
    run(*COMPOSE, '-f', str(stage / images), 'up', '-d', '--no-deps',
        '--pull', 'never', '--no-build', '--force-recreate', service,
        timeout=120, log=stage / 'commands.log')


def wait_caddy(stage):
    for _ in range(45):
        state = inspect('caddy')['State']
        if state['Running'] and state.get('Health', {}).get('Status') == 'healthy':
            return
        time.sleep(2)
    raise RuntimeError('Caddy did not become healthy')


def apply(stage):
    stage = stage.resolve(strict=True)
    if stage.parent != (ROOT / 'backups').resolve() or not (stage / 'PREPARED').is_file():
        raise RuntimeError('Not a prepared update directory')
    manifest = json.loads((stage / 'manifest.json').read_text())
    if time.time() - manifest['prepared_at'] > 3600:
        raise RuntimeError('Prepared plan is over one hour old')
    if file_hashes() != manifest['files']:
        raise RuntimeError('Operational configuration changed since preparation')
    _, current, _ = preflight()
    if {s: c['Id'] for s, c in current.items()} != manifest['container_ids']:
        raise RuntimeError('Production containers changed since preparation')
    if set(manifest['changed']) - {'caddy'}:
        raise RuntimeError('Unsupported rollout scope')
    print('Fresh baseline:', smoke(stage), flush=True)
    if not manifest['changed']:
        (stage / 'SUCCESS').write_text('All running images already current\n')
        print('SUCCESS: all five running images already current', flush=True)
        return
    # Stop the certificate writer briefly for a consistent recovery snapshot;
    # the proxy and ssh stay running throughout this backup phase.
    certbot_stopped = False
    mutated = False
    success = False
    def interrupted(signum, frame):
        raise RuntimeError(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        certbot_stopped = True
        run('docker', 'stop', '--time', '20', 'certbot', log=stage / 'commands.log')
        # Caddy is the only upgraded service. Back up its writable state and
        # shared certificates; no database migrations are performed here.
        for mount in current['caddy']['Mounts']:
            if mount['Destination'] in ('/data', '/config', '/etc/letsencrypt'):
                name = mount['Destination'].strip('/').replace('/', '-')
                run('tar', '-C', mount['Source'], '-czf', str(stage / f'{name}.tar.gz'), '.',
                    log=stage / 'commands.log')
        run('docker', 'start', 'certbot', log=stage / 'commands.log')
        certbot_stopped = False
        mutated = True
        print('Applying checked immutable Caddy image; sslh/3x-ui/WARP stay running', flush=True)
        compose_up(stage, 'new-images.json', 'caddy')
        wait_caddy(stage)
        if inspect('caddy')['Image'] != manifest['new_images']['caddy']:
            raise RuntimeError('Unexpected running Caddy image')
        result = smoke(stage)
        # Verify SSH dispatch through the same public port as well as HTTPS.
        with socket.create_connection(('127.0.0.1', 443), timeout=10) as sock:
            sock.settimeout(10)
            sock.sendall(b'SSH-2.0-XuiUpdateProbe\r\n')
            if not sock.recv(256).startswith(b'SSH-2.0-'):
                raise RuntimeError('SSH dispatch through sslh failed')
        _, after, runtime = preflight()
        for service in SERVICES:
            if service != 'caddy' and after[service]['Id'] != current[service]['Id']:
                raise RuntimeError(f'Unexpected recreation of {service}')
        if after['caddy']['RestartCount']:
            raise RuntimeError('New Caddy restarted unexpectedly')
        save_json(stage / 'trace.after.json', result)
        (stage / 'SUCCESS').write_text('Caddy updated; real XHTTP/WARP and SSH checks passed\n')
        success = True
        print('SUCCESS:', result, flush=True)
    finally:
        if certbot_stopped:
            run('docker', 'start', 'certbot', log=stage / 'commands.log')
        if mutated and not success:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print('ROLLBACK: recreating Caddy from the saved immutable old image', flush=True)
            # No destructive volume restore is needed: this patch release uses
            # the same Caddy storage format; untouched snapshots remain available.
            run('docker', 'tag', manifest['old_images']['caddy'], manifest['refs']['caddy'])
            compose_up(stage, 'old-images.json', 'caddy')
            wait_caddy(stage)
            smoke(stage)
            (stage / 'ROLLED_BACK').write_text('Old Caddy restored and real XHTTP/WARP checked\n')
            print('ROLLBACK COMPLETE', flush=True)


def main():
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--prepare', action='store_true')
    group.add_argument('--apply', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    with open('/run/lock/xui-container-update.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prepare:
            prepare()
        elif args.apply:
            apply(args.apply)
        else:
            config, _, _ = preflight()
            print('Plan: refresh registry images, inspect local WARP; validate before production changes.')
            print('Supported rollout: Caddy only; unexpected updates stop before deployment.')
            print('Apply in one systemd job with old-image rollback and real XHTTP/WARP + SSH checks.')
            print('Services:', ', '.join(config['services']))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
