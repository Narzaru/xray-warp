#!/usr/bin/env python3
"""Restore VPS egress for a historical template with one final WARP rule.

--prepare validates a full candidate and backs up the live database/template.
--apply DIR runs independently of SSH and rolls back on verification failure.
Templates with targeted WARP exceptions require an explicit routing decision.
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
import sqlite3
import sys
import time
from types import SimpleNamespace

ROOT = Path('/opt/x-ui')
H = W = None


def direct_config(config):
    result = copy.deepcopy(config)
    warp = [o for o in result['outbounds'] if o.get('tag') == 'warp-out']
    direct = [o for o in result['outbounds'] if o.get('tag') == 'direct']
    if len(warp) != 1 or len(direct) != 1 or direct[0].get('protocol') != 'freedom':
        raise RuntimeError('Expected exactly one warp-out and one direct/freedom')
    if result['outbounds'][0].get('tag') != 'warp-out':
        raise RuntimeError('Unexpected primary outbound')
    rules = result['routing']['rules']
    warp_rules = [r for r in rules if r.get('outboundTag') == 'warp-out']
    expected = {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'warp-out'}
    if warp_rules != [expected] or rules[-1] != expected:
        raise RuntimeError('Unexpected WARP routing; refusing broad rule removal')
    result['outbounds'] = [direct[0]] + [o for o in result['outbounds']
                                      if o.get('tag') not in ('direct', 'warp-out')]
    result['routing']['rules'] = rules[:-1]
    # Refuse hidden remaining references instead of leaving an invalid config.
    def references(value):
        if isinstance(value, dict):
            return any(references(v) for v in value.values())
        if isinstance(value, list):
            return any(references(v) for v in value)
        return value == 'warp-out'
    if references(result):
        raise RuntimeError('Additional warp-out references remain')
    return result


def read_template(db):
    with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True)) as conn:
        return conn.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0]


def trace(stage):
    raw = W.smoke(stage / 'xray-smoke', stage / 'smoke.json', stage / 'smoke.log')
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def prepare():
    _, containers, runtime = H.preflight()
    mount = next(m['Source'] for m in containers['3x-ui']['Mounts'] if m['Destination'] == '/etc/x-ui')
    db = Path(mount) / 'x-ui.db'
    old = read_template(db)
    new = direct_config(json.loads(old))
    candidate = direct_config(runtime)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = ROOT / 'backups' / ('xray-direct-' + stamp)
    stage.mkdir(mode=0o700)
    print('Preparation directory:', stage, flush=True)
    (stage / 'old-template.json').write_text(old)
    H.save_json(stage / 'new-template.json', new)
    H.save_json(stage / 'runtime.before.json', runtime)
    H.save_json(stage / 'candidate.json', candidate)
    with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(stage / 'x-ui.db')) as backup:
            source.backup(backup)
    H.make_smoke(stage, runtime)
    baseline = trace(stage)
    if baseline.get('warp') not in ('on', 'plus'):
        raise RuntimeError('Baseline XHTTP/WARP check failed')
    host_trace = H.run('curl', '--fail', '--silent', '--show-error', '--noproxy', '*',
                       '--connect-timeout', '5', '--max-time', '15',
                       'https://1.1.1.1/cdn-cgi/trace', timeout=20)
    host = dict(line.split('=', 1) for line in host_trace.splitlines() if '=' in line)
    if host.get('warp') != 'off' or not host.get('ip'):
        raise RuntimeError('Host direct baseline failed')
    H.run('docker', 'cp', str(stage / 'candidate.json'), '3x-ui:/tmp/xui-direct-candidate.json')
    H.run('docker', 'exec', '3x-ui', '/app/bin/xray-linux-amd64', 'run', '-test',
          '-c', '/tmp/xui-direct-candidate.json', log=stage / 'commands.log')
    H.save_json(stage / 'manifest.json', {'prepared_at': time.time(), 'db': str(db),
        'container_ids': {s: c['Id'] for s, c in containers.items()},
        'files': H.file_hashes(), 'host_ip': host['ip']})
    (stage / 'PREPARED').write_text('Direct candidate validated; database backed up; WARP and host baselines passed\n')
    print('PREPARED: full config accepted; direct host IP', host['ip'], flush=True)


def set_template(db, value, expected):
    with closing(sqlite3.connect(db, timeout=20)) as conn, conn:
        cursor = conn.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig' AND value=?", (value, expected))
        if cursor.rowcount != 1:
            raise RuntimeError('Template changed or setting is absent')


def apply(stage):
    stage = stage.resolve(strict=True)
    if stage.parent != (ROOT / 'backups').resolve() or not (stage / 'PREPARED').is_file():
        raise RuntimeError('Not a prepared direct-egress change')
    manifest = json.loads((stage / 'manifest.json').read_text())
    old = (stage / 'old-template.json').read_text()
    new = json.loads((stage / 'new-template.json').read_text())
    db = Path(manifest['db'])
    if new != direct_config(json.loads(old)):
        raise RuntimeError('Prepared template changed unexpectedly')
    if time.time() - manifest['prepared_at'] > 3600 or H.file_hashes() != manifest['files']:
        raise RuntimeError('Plan expired or operational files changed')
    _, before, _ = H.preflight()
    if {s: c['Id'] for s, c in before.items()} != manifest['container_ids'] or read_template(db) != old:
        raise RuntimeError('Production state changed')
    baseline = trace(stage).get('warp')
    if baseline not in ('on', 'plus'):
        raise RuntimeError('Fresh XHTTP/WARP baseline failed')
    print('Fresh baseline:', baseline, flush=True)
    stopped = written = success = False
    def interrupted(signum, frame):
        raise RuntimeError(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        stopped = True
        H.run('docker', 'stop', '--time', '20', '3x-ui')
        set_template(db, json.dumps(new), old)
        written = True
        H.run('docker', 'start', '3x-ui')
        W.wait_panel()
        actual = json.loads(H.run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
        prior = json.loads((stage / 'runtime.before.json').read_text())
        expected = direct_config(prior)
        # Generated ancillary data may change; verify the affected contracts exactly.
        if actual['outbounds'] != expected['outbounds'] or actual['routing'] != expected['routing']:
            raise RuntimeError('Runtime egress/routing does not match direct candidate')
        result = trace(stage)
        if result.get('warp') != 'off' or result.get('ip') != manifest['host_ip']:
            raise RuntimeError('Real XHTTP chain did not exit directly from VPS')
        for name, container in before.items():
            if name != '3x-ui' and H.inspect(name)['Id'] != container['Id']:
                raise RuntimeError(f'Unexpected recreation of {name}')
        H.save_json(stage / 'trace.after.json', {k: result.get(k) for k in ('warp', 'ip', 'loc')})
        (stage / 'SUCCESS').write_text('Direct egress verified through the real XHTTP chain; WARP container unchanged\n')
        success = True
        print('SUCCESS:', {k: result.get(k) for k in ('warp', 'ip', 'loc')}, flush=True)
    finally:
        if stopped and not success:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print('ROLLBACK: restoring prior Xray template', flush=True)
            H.run('docker', 'stop', '--time', '20', '3x-ui')
            try:
                if written or json.loads(read_template(db)) == new:
                    set_template(db, old, json.dumps(new))
            finally:
                H.run('docker', 'start', '3x-ui')
            W.wait_panel()
            if trace(stage).get('warp') not in ('on', 'plus'):
                raise RuntimeError('Rollback XHTTP/WARP verification failed')
            (stage / 'ROLLED_BACK').write_text('Prior template restored and real XHTTP/WARP checked\n')
            print('ROLLBACK COMPLETE', flush=True)


def main():
    import fcntl
    global H, W
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--prepare', action='store_true')
    group.add_argument('--apply', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    H = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name('update-containers.py'))))
    W = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name('enable-warp.py'))))
    with open('/run/lock/xui-container-update.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (ROOT / 'pending-youtube-cache-exception.json').exists():
            raise RuntimeError('A pending cache exception requires warp-out; resolve the queued change first')
        if args.prepare:
            prepare()
        elif args.apply:
            apply(args.apply)
        else:
            _, _, runtime = H.preflight()
            direct_config(runtime)
            print('Plan: remove only warp-out and its final catch-all; restore direct/freedom as default.')
            print('Validate candidate; back up SQLite; stop/update/start only 3x-ui; verify VPS IP and warp=off.')
            print('On failure restore old template. WARP container, Compose, cron and sslh remain unchanged.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
