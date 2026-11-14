#!/usr/bin/env python3
"""Migrate the original direct-only VPS template to RU blocking.

--prepare backs up SQLite and validates a candidate without changing routing.
--apply DIR updates only routing and inbound sniffing, with automatic rollback.
--rollback DIR restores those settings without overwriting traffic statistics.
"""
import argparse
import copy
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import runpy
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path('/opt/x-ui')
TAG = 'in-10000-tcp'
PORT = 29989
H = None


def read_state(db):
    with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True)) as conn:
        template = conn.execute(
            "SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0]
        rows = conn.execute('SELECT id,tag,sniffing FROM inbounds WHERE enable=1').fetchall()
    if len(rows) != 1 or rows[0][1] != TAG:
        raise RuntimeError('Expected one enabled XHTTP inbound')
    return template, rows[0]


def routing_config(config):
    result = copy.deepcopy(config)
    if [(o['tag'], o['protocol']) for o in result['outbounds']] != [
            ('direct', 'freedom'), ('blocked', 'blackhole')]:
        raise RuntimeError('Unexpected outbound configuration')
    routing = result['routing']
    expected_rules = [
        {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'},
        {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'},
        {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'blocked'}]
    if routing.get('domainStrategy') != 'AsIs' or routing['rules'] != expected_rules:
        raise RuntimeError('Unexpected prior routing configuration')
    routing['domainStrategy'] = 'IPOnDemand'
    routing['rules'][1:1] = [
        {'type': 'field', 'inboundTag': [TAG], 'domain': ['geosite:category-ru'],
         'outboundTag': 'blocked'},
        {'type': 'field', 'inboundTag': [TAG], 'ip': ['geoip:ru'],
         'outboundTag': 'blocked'}]
    return result


def sniff_config(value):
    result = json.loads(value)
    result.update(enabled=True, routeOnly=True, metadataOnly=False,
                  destOverride=['http', 'tls', 'quic'])
    return json.dumps(result)


def run_checks(stage, blocked, expected_warp='off'):
    """Exercise the real TLS/XHTTP chain, bypassing client split routing."""
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', PORT))
    checks = [
        ('foreign', ['--socks5-hostname', f'127.0.0.1:{PORT}',
                     'https://1.1.1.1/cdn-cgi/trace']),
        ('ru-domain', ['--socks5-hostname', f'127.0.0.1:{PORT}', 'https://ya.ru/']),
        ('ru-ip', ['--socks5-hostname', f'127.0.0.1:{PORT}', 'http://77.88.55.242/']),
        # Local resolution forces an IP destination in SOCKS/VLESS. Only the
        # server's sniffed HTTP Host can identify this as a Russian domain.
        ('ru-sniff', ['--socks5', f'127.0.0.1:{PORT}', '--resolve',
                      'ya.ru:80:1.1.1.1', 'http://ya.ru/'])]
    results = {}
    with (stage / 'smoke.log').open('a') as log:
        process = subprocess.Popen([str(stage / 'xray-smoke'), 'run', '-c',
                                    str(stage / 'smoke.json')], stdout=log, stderr=log)
        try:
            for _ in range(40):
                if process.poll() is not None:
                    raise RuntimeError('Temporary XHTTP client exited')
                try:
                    with socket.create_connection(('127.0.0.1', PORT), timeout=.2):
                        break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError('Temporary XHTTP client not ready')
            for name, args in checks:
                started = time.monotonic()
                p = subprocess.run(['curl', '--silent', '--show-error', '--noproxy', '',
                                    '--connect-timeout', '5', '--max-time', '8',
                                    '--write-out', '\nHTTP_CODE=%{http_code}', *args],
                                   capture_output=True, text=True, timeout=12)
                body, _, status = p.stdout.rpartition('\nHTTP_CODE=')
                seconds = round(time.monotonic() - started, 3)
                results[name] = {'exit': p.returncode, 'http': status, 'seconds': seconds}
                if name == 'foreign':
                    trace = dict(line.split('=', 1) for line in body.splitlines() if '=' in line)
                    if p.returncode or status != '200' or trace.get('warp') != expected_warp:
                        raise RuntimeError('Foreign XHTTP/egress check failed')
                    results[name].update(ip=trace.get('ip'), warp=trace.get('warp'))
                elif blocked:
                    if p.returncode == 0 or status != '000' or seconds >= 4:
                        raise RuntimeError(f'{name} was not immediately blocked')
                elif p.returncode or status == '000':
                    raise RuntimeError(f'{name} baseline unavailable')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return results


def wait_ready():
    for _ in range(45):
        state = H.inspect('3x-ui')['State']
        if state['Running'] and state.get('Health', {}).get('Status') == 'healthy':
            H.run('docker', 'exec', '3x-ui', 'pgrep', '-f', 'xray-linux-amd64')
            return
        time.sleep(1)
    raise RuntimeError('3x-ui did not become healthy')


def prepare():
    container = H.inspect('3x-ui')
    mount = next(m['Source'] for m in container['Mounts'] if m['Destination'] == '/etc/x-ui')
    db = Path(mount) / 'x-ui.db'
    old, row = read_state(db)
    runtime = json.loads(H.run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
    candidate = routing_config(runtime)
    sniffing = sniff_config(row[2])
    inbound = next(i for i in candidate['inbounds'] if i['tag'] == TAG)
    if inbound['protocol'] != 'vless' or inbound['streamSettings']['network'] != 'xhttp':
        raise RuntimeError('Unexpected inbound transport')
    inbound['sniffing'] = json.loads(sniffing)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = ROOT / 'backups' / ('block-ru-' + stamp)
    stage.mkdir(mode=0o700)
    H.save_json(stage / 'state.json', {'db': str(db), 'template_old': old,
        'template_new': json.dumps(routing_config(json.loads(old))),
        'inbound_id': row[0], 'sniff_old': row[2], 'sniff_new': sniffing,
        'container_id': container['Id'], 'prepared_at': time.time()})
    H.save_json(stage / 'runtime.before.json', runtime)
    H.save_json(stage / 'candidate.json', candidate)
    with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(stage / 'x-ui.db')) as backup:
            source.backup(backup)
    H.run('docker', 'cp', str(stage / 'candidate.json'), '3x-ui:/tmp/xui-block-ru-candidate.json')
    H.run('docker', 'exec', '3x-ui', '/app/bin/xray-linux-amd64', 'run', '-test',
          '-c', '/tmp/xui-block-ru-candidate.json', log=stage / 'commands.log')
    H.make_smoke(stage, runtime)
    H.save_json(stage / 'baseline.json', run_checks(stage, blocked=False))
    (stage / 'PREPARED').write_text('Candidate accepted; SQLite backed up; real XHTTP baselines passed\n')
    print('PREPARED:', stage, flush=True)


def write_state(state, reverse=False):
    source, target = ('new', 'old') if reverse else ('old', 'new')
    with closing(sqlite3.connect(state['db'], timeout=20)) as conn, conn:
        cursor = conn.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig' AND value=?",
                              (state['template_' + target], state['template_' + source]))
        if cursor.rowcount != 1:
            raise RuntimeError('Template changed concurrently')
        cursor = conn.execute('UPDATE inbounds SET sniffing=? WHERE id=? AND sniffing=?',
                              (state['sniff_' + target], state['inbound_id'], state['sniff_' + source]))
        if cursor.rowcount != 1:
            raise RuntimeError('Inbound changed concurrently')


def apply(stage, reverse=False):
    stage = stage.resolve(strict=True)
    if stage.parent != (ROOT / 'backups').resolve() or not (stage / 'PREPARED').is_file():
        raise RuntimeError('Not a prepared block-RU change')
    state = json.loads((stage / 'state.json').read_text())
    if not reverse and time.time() - state['prepared_at'] > 3600:
        raise RuntimeError('Preparation expired')
    if H.inspect('3x-ui')['Id'] != state['container_id']:
        raise RuntimeError('3x-ui container changed')
    before = {n: H.inspect(n)['Id'] for n in ('sslh', 'caddy', 'warp', 'certbot')}
    expected_source = 'new' if reverse else 'old'
    current, row = read_state(Path(state['db']))
    if current != state['template_' + expected_source] or row[2] != state['sniff_' + expected_source]:
        raise RuntimeError('Settings changed since preparation')
    print('Fresh checks:', run_checks(stage, blocked=reverse), flush=True)
    stopped = written = success = False
    def interrupted(signum, frame):
        raise RuntimeError(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        stopped = True
        H.run('docker', 'stop', '--time', '20', '3x-ui')
        write_state(state, reverse)
        written = True
        H.run('docker', 'start', '3x-ui')
        wait_ready()
        runtime = json.loads(H.run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
        expected = json.loads((stage / ('runtime.before.json' if reverse else 'candidate.json')).read_text())
        if runtime['routing'] != expected['routing'] or runtime['outbounds'] != expected['outbounds']:
            raise RuntimeError('Generated routing/outbounds differ from the validated candidate')
        actual_sniff = next(i['sniffing'] for i in runtime['inbounds'] if i['tag'] == TAG)
        if actual_sniff != next(i['sniffing'] for i in expected['inbounds'] if i['tag'] == TAG):
            raise RuntimeError('Generated sniffing differs from candidate')
        result = run_checks(stage, blocked=not reverse)
        if any(H.inspect(n)['Id'] != cid for n, cid in before.items()):
            raise RuntimeError('An unrelated container was recreated')
        H.save_json(stage / ('checks.rollback.json' if reverse else 'checks.after.json'), result)
        (stage / ('ROLLED_BACK' if reverse else 'SUCCESS')).write_text('Real XHTTP checks passed\n')
        success = True
        print('SUCCESS:', result, flush=True)
    finally:
        if stopped and not success:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            H.run('docker', 'stop', '--time', '20', '3x-ui')
            try:
                if written:
                    write_state(state, not reverse)
            finally:
                H.run('docker', 'start', '3x-ui')
            wait_ready()
            run_checks(stage, blocked=reverse)
            (stage / 'AUTO_ROLLBACK').write_text('Prior settings restored and real XHTTP verified\n')
            print('AUTO ROLLBACK COMPLETE', flush=True)


def main():
    import fcntl
    global H
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare', action='store_true')
    group.add_argument('--apply', type=Path)
    group.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    H = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name('update-containers.py'))))
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
