#!/usr/bin/env python3
"""Stage and apply Loyalsoldier geodata; disruptive operations only at 04:00 NSK.

--prepare is safe during the day. It downloads and validates a candidate and
records the exact bootstrap Compose file. --nightly downloads again, validates,
then applies under the same lock as container maintenance. No catch-up applies.
"""
import argparse
import copy
from contextlib import closing
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/opt/x-ui')
DATA = ROOT / 'geodata'
MOUNT = '/var/lib/xray/geodata'
ASSET = MOUNT + '/current'
LOCK = '/run/lock/xui-container-update.lock'
REPO = 'Loyalsoldier/v2ray-rules-dat'
FILES = ('geoip.dat', 'geosite.dat')
NSK = dt.timezone(dt.timedelta(hours=7))
MAX_ASSET = 64 * 1024 * 1024
COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.warp.yml', 'docker-compose.warp-udp.yml')
CACHE_RULE = {'type': 'field', 'inboundTag': ['in-10000-tcp'], 'network': 'tcp,udp',
              'port': '443', 'domain': ['full:rr2---sn-npcnxu-v8cs.googlevideo.com'], 'outboundTag': 'warp-out'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')


def atomic_bytes(path, data, mode=0o600):
    temp = path.with_name(path.name + '.geodata-tmp')
    with temp.open('wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    temp.chmod(mode)
    os.replace(temp, path)


def nightly_window(now=None):
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(NSK)
    if local.hour != 4 or local.minute >= 30:
        raise RuntimeError('Disruptive geodata changes allowed only 04:00-04:29 Asia/Novosibirsk')


def varint(data, offset):
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('Truncated protobuf varint')
        value = data[offset]
        offset += 1
        result |= (value & 127) << shift
        if value < 128:
            return result, offset
    raise ValueError('Oversized protobuf varint')


def fields(data):
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError('Invalid protobuf field')
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if offset + size > len(data):
                raise ValueError('Truncated protobuf field')
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError('Unsupported protobuf wire type')
        yield number, wire, value


def validate_asset(data, kind):
    """Validate the actual GeoIPList/GeoSiteList schema, not filename alone."""
    categories = {}
    for number, wire, entry in fields(data):
        if (number, wire) != (1, 2):
            raise ValueError('Unexpected geodata list field')
        name = None
        count = 0
        for num, wt, value in fields(entry):
            if (num, wt) == (1, 2):
                name = value.decode('ascii').lower()
                if not re.fullmatch(r'[a-z0-9_@!.-]{1,120}', name):
                    raise ValueError('Invalid category name')
            elif (num, wt) == (2, 2):
                child = list(fields(value))
                if kind == 'geoip.dat':
                    parts = {n: v for n, w, v in child}
                    address = ipaddress.ip_address(parts[1])
                    prefix = parts.get(2, 0)
                    if not isinstance(prefix, int) or not 0 <= prefix <= address.max_prefixlen:
                        raise ValueError('Invalid GeoIP prefix')
                else:
                    parts = {n: v for n, w, v in child if n in (1, 2)}
                    if parts.get(1, 0) not in (0, 1, 2, 3) or not parts.get(2, b'').decode('utf-8'):
                        raise ValueError('Invalid GeoSite domain')
                count += 1
            elif num not in (3, 4):
                raise ValueError('Unexpected geodata entry field')
        if not name or name in categories or count == 0:
            raise ValueError('Missing, duplicate or empty category')
        categories[name] = count
    required = {'ru', 'private'} if kind == 'geoip.dat' else {'category-ru'}
    if not required.issubset(categories):
        raise ValueError('Required routing categories missing: ' + ','.join(sorted(required - categories.keys())))
    return {'categories': len(categories), 'entries': sum(categories.values()),
            'required_categories': {key: categories[key] for key in sorted(required)}}


def verify_checksum(data, checksum, filename):
    match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?' + re.escape(filename) + r'\s*', checksum)
    if not match or sha(data) != match[1].lower():
        raise ValueError('SHA256 checksum mismatch: ' + filename)


def fetch(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'x-ui-geodata-updater/1', 'Accept': '*/*'})
    with urllib.request.urlopen(request, timeout=45) as response:
        result = response.read(limit + 1)
        if len(result) > limit:
            raise ValueError('Download exceeds size limit')
        return result


def download(stage):
    release = json.loads(fetch('https://api.github.com/repos/' + REPO + '/releases/latest', 1024 * 1024))
    tag = release['tag_name']
    if not re.fullmatch(r'\d{12}', tag) or release.get('draft') or release.get('prerelease'):
        raise ValueError('Unexpected upstream release')
    assets = {item['name']: item for item in release['assets']}
    folder = stage / 'assets'
    folder.mkdir()
    metadata = {'repository': REPO, 'tag': tag, 'published_at': release['published_at'], 'files': {}}
    for name in FILES:
        base = 'https://github.com/' + REPO + '/releases/download/' + tag + '/'
        for item in (name, name + '.sha256sum'):
            if assets[item]['browser_download_url'] != base + item:
                raise ValueError('Unexpected release asset URL')
        data = fetch(base + name, MAX_ASSET)
        if len(data) < 1024 * 1024 or len(data) != assets[name]['size']:
            raise ValueError('Unexpected geodata size')
        checksum = fetch(base + name + '.sha256sum', 1024).decode('ascii')
        verify_checksum(data, checksum, name)
        digest = assets[name].get('digest')
        if digest and digest != 'sha256:' + sha(data):
            raise ValueError('GitHub asset digest mismatch')
        info = validate_asset(data, name)
        info.update(sha256=sha(data), bytes=len(data))
        metadata['files'][name] = info
        (folder / name).write_bytes(data)
    save(stage / 'release.json', metadata)
    return metadata


_HELPER = None


def helpers():
    global _HELPER
    if _HELPER is None:
        _HELPER = runpy.run_path(str(ROOT / 'tools/update-containers.py'))
    return _HELPER


def run(*args, timeout=90, stage=None):
    return helpers()['run'](*args, timeout=timeout, log=stage / 'commands.log' if stage else None)


def inspect():
    return helpers()['inspect']('3x-ui')


def compose(base=None):
    result = ['docker', 'compose', '--project-directory', str(ROOT), '-p', 'x-ui']
    for name in COMPOSE_FILES:
        result += ['-f', str(base if name == COMPOSE_FILES[0] and base else ROOT / name)]
    return result


def resolved(base=None):
    return json.loads(run(*compose(base), 'config', '--format', 'json'))


def managed(container):
    return ('XRAY_LOCATION_ASSET=' + ASSET in container['Config'].get('Env', [])
            and any(m['Destination'] == MOUNT and m['Source'] == str(DATA) and not m['RW']
                    for m in container['Mounts']))


def pair_hashes(folder):
    return {name: sha((folder / name).read_bytes()) for name in FILES}


def require_warp_egress(config):
    outbounds = config.get('outbounds') or []
    first = outbounds[0] if outbounds else {}
    servers = first.get('settings', {}).get('servers', [])
    if (first.get('tag') != 'warp-out' or first.get('protocol') != 'socks'
            or [(s.get('address'), s.get('port')) for s in servers] != [('warp', 1080)]):
        raise RuntimeError('Maintenance requires WARP SOCKS at warp:1080 as the first outbound')


def cache_exception_template(template):
    """The one approved exception; preserve API/private precedence and RU rules."""
    result = copy.deepcopy(template)
    rules = result['routing']['rules']
    api = {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'}
    private = {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'}
    catchall = {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'warp-out'}
    require_warp_egress(result)
    if result['routing'].get('domainStrategy') != 'IPOnDemand':
        raise RuntimeError('Cache exception requires IPOnDemand')
    if not rules or rules[-1] != catchall or rules.count(catchall) != 1:
        raise RuntimeError('Expected one final catch-all WARP rule')
    if rules[0] != api or rules.count(private) != 1 or rules.count(CACHE_RULE) > 1:
        raise RuntimeError('Unexpected API/private/exception rules')
    for key, value in [('domain', ['geosite:category-ru']), ('ip', ['geoip:ru'])]:
        ru_block = {'type': 'field', key: value, 'inboundTag': ['in-10000-tcp'], 'outboundTag': 'blocked'}
        if rules.count(ru_block) != 1:
            raise RuntimeError('Expected existing Russian destination blocks')
    if any(r.get('outboundTag') != 'blocked' for r in rules[1:-1] if r != CACHE_RULE):
        raise RuntimeError('Unexpected forwarding rule before WARP catch-all')
    if any(r != CACHE_RULE and CACHE_RULE['domain'][0] in r.get('domain', []) for r in rules):
        raise RuntimeError('Conflicting cache exception already exists')
    result['routing']['rules'] = [api, private, copy.deepcopy(CACHE_RULE)] + [
        r for r in rules[1:] if r != private and r != CACHE_RULE]
    return result


def template_state(container):
    mount = next(m['Source'] for m in container['Mounts'] if m['Destination'] == '/etc/x-ui')
    db = Path(mount) / 'x-ui.db'
    with closing(sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)) as connection:
        text = connection.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0]
    return db, text


def cas_template(db, expected, replacement):
    # Only this setting is changed: never restore an entire DB over newer clients/stats.
    with closing(sqlite3.connect(db, timeout=20)) as connection, connection:
        cursor = connection.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig' AND value=?",
                                    (replacement, expected))
        if cursor.rowcount == 0:
            current = connection.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0]
            if current != replacement:
                raise RuntimeError('Panel template changed concurrently; refusing to overwrite')


def prepare_cache_exception(stage, container, runtime, request=None):
    pending = ROOT / 'pending-youtube-cache-exception.json'
    if request is None and pending.is_file():
        request = json.loads(pending.read_text())
    if request is None:
        return False
    if request.get('rule') != CACHE_RULE:
        raise RuntimeError('Pending routing change is not the approved exact exception')
    db, old = template_state(container)
    old_template = json.loads(old)
    if any(old_template.get(k) != runtime.get(k) for k in ('routing', 'outbounds')):
        raise RuntimeError('Panel template and generated egress differ; inspect before applying')
    new = json.dumps(cache_exception_template(old_template), ensure_ascii=False, sort_keys=True)
    if sha(old.encode()) == request['new_template_sha256'] and old_template == cache_exception_template(old_template):
        save(stage / 'routing-already-applied.json', {'pending_sha256': sha(json.dumps(request, indent=2).encode())})
        return False
    if (sha(old.encode()) != request['expected_template_sha256']
            or sha(json.dumps(runtime, sort_keys=True).encode()) != request['expected_runtime_sha256']
            or sha(new.encode()) != request['new_template_sha256']):
        raise RuntimeError('Approved exception baseline changed; no template changes applied')
    save(stage / 'routing-change.json', {'db': str(db), 'before': old, 'after': new,
         'pending_sha256': sha(json.dumps(request, indent=2).encode())})
    expected = copy.deepcopy(runtime)
    expected['routing'] = json.loads(new)['routing']
    save(stage / 'runtime.expected.json', expected)
    return True


def restore_cache_exception(stage):
    change = stage / 'routing-change.json'
    if change.is_file():
        saved = json.loads(change.read_text())
        cas_template(saved['db'], saved['after'], saved['before'])


def verify_cache_template(stage, new):
    change = stage / 'routing-change.json'
    if change.is_file():
        saved = json.loads(change.read_text())
        with closing(sqlite3.connect('file:' + saved['db'] + '?mode=ro', uri=True)) as connection:
            value = connection.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0]
        if value != saved['after' if new else 'before']:
            raise RuntimeError('Persisted panel template differs after restart; refusing to commit approval')


def clear_completed_exception(stage):
    source = stage / 'routing-change.json'
    if not source.is_file():
        source = stage / 'routing-already-applied.json'
    pending = ROOT / 'pending-youtube-cache-exception.json'
    if source.is_file() and pending.is_file():
        if sha(pending.read_bytes()) == json.loads(source.read_text())['pending_sha256']:
            pending.unlink()


def bootstrap_text(original):
    """Small guarded insertion in the owned base service; preserve other edits."""
    start = original.index('  3x-ui:\n')
    end = original.index('\n  # ', start + 1)
    section = original[start:end]
    if 'XRAY_LOCATION_ASSET' in section or MOUNT in section:
        raise ValueError('Geodata Compose settings already exist; inspect manually')
    env = '      - TZ=${TIMEZONE:-UTC}\n'
    volume = '      - 3x-ui-data:/etc/x-ui\n'
    if section.count(env) != 1 or section.count(volume) != 1:
        raise ValueError('Unexpected base Compose shape')
    section = section.replace(env, env + '      - XRAY_LOCATION_ASSET=' + ASSET + '\n')
    section = section.replace(volume, volume + '      - ./geodata:' + MOUNT + ':ro\n')
    return original[:start] + section + original[end:]


def isolated_validate(stage, image, assets):
    """Same runtime config/image, no network, ports, production DB or capabilities."""
    name = 'xui-geodata-validation-' + str(os.getpid())
    cmd = ['docker', 'run', '--name', name, '--network', 'none', '--read-only',
           '--cap-drop', 'ALL', '--pids-limit', '64', '--memory', '256m',
           '--entrypoint', '/app/bin/xray-linux-amd64', '-e', 'XRAY_LOCATION_ASSET=/candidate-assets',
           '-v', str(assets) + ':/candidate-assets:ro',
           '-v', str(stage / ('runtime.expected.json' if (stage / 'runtime.expected.json').exists()
                             else 'runtime.before.json')) + ':/candidate.json:ro']
    run(*cmd, '--rm', image, 'run', '-test', '-c', '/candidate.json', timeout=90, stage=stage)
    try:
        run(*cmd, '-d', image, 'run', '-c', '/candidate.json', stage=stage)
        time.sleep(2)
        state = json.loads(run('docker', 'inspect', name))[0]['State']
        if not state['Running']:
            raise RuntimeError('Isolated Xray failed to stay running')
        listeners = run('nsenter', '-t', str(state['Pid']), '-n', 'ss', '-lnt', 'sport', '=', ':10000', stage=stage)
        if ':10000' not in listeners:
            raise RuntimeError('Isolated Xray inbound did not start')
    finally:
        # This name is unique to this job; never remove a production container.
        logs = subprocess.run(['docker', 'logs', name], capture_output=True, text=True)
        (stage / 'isolated.log').write_text(logs.stdout + logs.stderr)
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)


def prepare_stage(route_request=None):
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = ROOT / 'backups' / ('geodata-' + stamp + '-' + str(os.getpid()))
    stage.mkdir(mode=0o700)
    before = inspect()
    if not before['State']['Running'] or before['State'].get('Health', {}).get('Status') != 'healthy':
        raise RuntimeError('3x-ui must be healthy before preparing geodata')
    config = json.loads(run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
    require_warp_egress(config)
    warp_id = json.loads(run('docker', 'image', 'inspect', helpers()['LOCAL_WARP_IMAGE']))[0]['Id']
    if helpers()['inspect']('warp')['Image'] != warp_id:
        raise RuntimeError('Geodata UDP verification requires the running local WARP/Hev image')
    save(stage / 'runtime.before.json', config)
    routing_change = prepare_cache_exception(stage, before, config, route_request)
    previous = stage / 'previous-assets'
    previous.mkdir()
    active_dir = ASSET if managed(before) else '/app/bin'
    for name in FILES:
        run('docker', 'cp', '3x-ui:' + active_dir + '/' + name, str(previous / name), stage=stage)
    metadata = download(stage)
    isolated_validate(stage, before['Image'], stage / 'assets')
    old = resolved()
    if managed(before):
        service = old['services']['3x-ui']
        if (service.get('environment', {}).get('XRAY_LOCATION_ASSET') != ASSET
                or not any(v['source'] == str(DATA) and v['target'] == MOUNT and v.get('read_only')
                           for v in service.get('volumes', []))):
            raise RuntimeError('Weekly Compose would lose the managed geodata mount')
    (stage / 'base.before.yml').write_bytes((ROOT / COMPOSE_FILES[0]).read_bytes())
    old['services']['3x-ui']['image'] = before['Image']
    save(stage / 'rollback.compose.json', old)
    original = (ROOT / COMPOSE_FILES[0]).read_bytes()
    if not managed(before):
        candidate = bootstrap_text(original.decode('utf-8')).encode('utf-8')
        (stage / 'base.candidate.yml').write_bytes(candidate)
        new = resolved(stage / 'base.candidate.yml')
        reverted = copy.deepcopy(new)
        reverted['services']['3x-ui']['environment'].pop('XRAY_LOCATION_ASSET', None)
        reverted['services']['3x-ui']['volumes'] = [v for v in reverted['services']['3x-ui']['volumes'] if v['target'] != MOUNT]
        current = resolved()
        if reverted != current:
            raise RuntimeError('Bootstrap changes more than asset mount/environment')
    state = {'container_id': before['Id'], 'image': before['Image'],
             'compose_sha256': sha(original), 'runtime_sha256': sha(json.dumps(config, sort_keys=True).encode()),
             'release': metadata, 'managed_before': managed(before), 'previous_hashes': pair_hashes(previous),
             'routing_change': routing_change}
    save(stage / 'plan.json', state)
    return stage, state


def set_current(target):
    if target is None:
        (DATA / 'current').unlink(missing_ok=True)
        return
    if Path(target).is_absolute() or '..' in Path(target).parts or not target.startswith('releases/'):
        raise ValueError('Unsafe geodata symlink target')
    if not all((DATA / target / name).is_file() for name in FILES):
        raise ValueError('Geodata target incomplete')
    temp = DATA / ('current.tmp-' + str(os.getpid()))
    temp.unlink(missing_ok=True)
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp, DATA / 'current')


def install_release(stage, metadata):
    releases = DATA / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    target = 'releases/' + metadata['tag']
    dest = DATA / target
    if dest.exists():
        if pair_hashes(dest) != pair_hashes(stage / 'assets'):
            raise RuntimeError('Existing release tag has different contents')
    else:
        temp = releases / ('.' + metadata['tag'] + '-' + str(os.getpid()))
        temp.mkdir()
        for name in FILES:
            shutil.copyfile(stage / 'assets' / name, temp / name)
            (temp / name).chmod(0o644)
        save(temp / 'release.json', metadata)
        os.replace(temp, dest)
    return target


def verify_runtime(stage, expected_hashes, expect_managed, expected_config='runtime.before.json'):
    for _ in range(90):
        c = inspect()
        if c['State']['Running'] and c['State'].get('Health', {}).get('Status') == 'healthy':
            break
        time.sleep(1)
    else:
        raise RuntimeError('3x-ui did not become healthy')
    if managed(c) != expect_managed:
        raise RuntimeError('Unexpected geodata mount/environment after restart')
    asset_dir = ASSET if expect_managed else '/app/bin'
    for name, digest in expected_hashes.items():
        actual = run('docker', 'exec', '3x-ui', 'sha256sum', asset_dir + '/' + name).split()[0]
        if actual != digest:
            raise RuntimeError('Active geodata checksum differs after restart')
    config = json.loads(run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
    if config != json.loads((stage / expected_config).read_text()):
        raise RuntimeError('Runtime routing/inbounds changed unexpectedly')
    helper = helpers()
    helper['make_smoke'](stage, config)
    client = json.loads((stage / 'smoke.json').read_text())
    client['inbounds'][0]['settings']['udp'] = True
    save(stage / 'smoke.json', client)
    probes = runpy.run_path(str(ROOT / 'tools/probe-stack.py'))
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 29989))
    with (stage / 'smoke.log').open('a') as output:
        process = subprocess.Popen([str(stage / 'xray-smoke'), 'run', '-c', str(stage / 'smoke.json')], stdout=output, stderr=output)
        try:
            for _ in range(40):
                if process.poll() is not None:
                    raise RuntimeError('Temporary smoke client exited before readiness')
                try:
                    with socket.create_connection(('127.0.0.1', 29989), timeout=.2):
                        break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError('Temporary smoke client did not become ready')
            trace = run('curl', '-fsS', '--noproxy', '', '--connect-timeout', '5', '--max-time', '15',
                        '--socks5-hostname', '127.0.0.1:29989', 'https://1.1.1.1/cdn-cgi/trace', stage=stage)
            if 'warp=on' not in trace and 'warp=plus' not in trace:
                raise RuntimeError('Full XHTTP/WARP TCP check failed')
            udp = probes['udp_probe'](('127.0.0.1', 29989))
            save(stage / 'checks.json', {'tcp_warp': True, 'udp_dns': udp})
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    verify_cache_template(stage, expected_config == 'runtime.expected.json')


def transaction(stage, activate, verify, rollback, verify_old):
    try:
        activate()
        verify()
    except BaseException as error:
        save(stage / 'failure.json', {'error': type(error).__name__ + ': ' + str(error)})
        try:
            rollback()
            verify_old()
            save(stage / 'rollback-result.json', {'success': True})
        except BaseException as rollback_error:
            save(stage / 'rollback-result.json', {'success': False, 'error': str(rollback_error)})
            raise RuntimeError('Geodata update AND rollback verification failed; inspect private report') from rollback_error
        raise RuntimeError('Geodata update failed; previous state restored and verified') from error
    save(stage / 'SUCCESS.json', {'success': True, 'completed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()})


def confirmed_no_change(boot, current_hashes, candidate_hashes, committed, routing_change=False):
    # Files on disk alone do not prove that the running Xray loaded them: the
    # previous process may have died after swapping current but before restart.
    return (not boot and not routing_change and current_hashes == candidate_hashes
            and committed.get('hashes') == candidate_hashes)


def recover_pending():
    pending = DATA / 'pending.json'
    if not pending.is_file():
        return
    nightly_window()
    stage = Path(json.loads(pending.read_text())['stage']).resolve()
    if stage.parent != (ROOT / 'backups').resolve() or not re.fullmatch(r'geodata-\d{8}T\d{6}Z-\d+', stage.name):
        raise RuntimeError('Invalid pending recovery path')
    committed = json.loads((DATA / 'last-applied.json').read_text()) if (DATA / 'last-applied.json').is_file() else {}
    if committed.get('report') == str(stage) and managed(inspect()) and pair_hashes(DATA / 'current') == committed.get('hashes'):
        # Commit is written only after the new runtime passed all checks.
        clear_completed_exception(stage)
        pending.unlink()
        return
    prior = json.loads((stage / 'transaction.before.json').read_text())
    current_base = sha((ROOT / COMPOSE_FILES[0]).read_bytes())
    allowed = {sha((stage / 'base.before.yml').read_bytes())}
    candidate = stage / 'base.candidate.yml'
    if candidate.exists():
        allowed.add(sha(candidate.read_bytes()))
    if current_base not in allowed:
        raise RuntimeError('Pending recovery found unrelated Compose edits; refusing to overwrite')
    try:
        restore_cache_exception(stage)
        set_current(prior['current'])
        if prior['bootstrap']:
            atomic_bytes(ROOT / COMPOSE_FILES[0], (stage / 'base.before.yml').read_bytes(), 0o644)
            run('docker', 'compose', '--project-directory', str(ROOT), '-p', 'x-ui',
                '-f', str(stage / 'rollback.compose.json'), 'up', '-d', '--no-deps',
                '--no-build', '--pull', 'never', '--force-recreate', '3x-ui', stage=stage)
        else:
            run('docker', 'restart', '3x-ui', stage=stage)
        verify_runtime(stage, prior['hashes'], not prior['bootstrap'])
        save(stage / 'crash-recovery.json', {'success': True})
        pending.unlink()
    except BaseException as error:
        save(stage / 'crash-recovery.json', {'success': False, 'error': str(error)})
        raise


def prune_reports():
    """Bound our own copies; preserve current, pending and retained rollback data."""
    backup_root = ROOT / 'backups'
    stages = sorted((p for p in backup_root.glob('geodata-*') if p.is_dir() and not p.is_symlink()
                     and re.fullmatch(r'geodata-\d{8}T\d{6}Z-\d+', p.name)), reverse=True)
    protected = set(stages[:7])
    for state_file, key in [(DATA / 'pending.json', 'stage'), (DATA / 'last-applied.json', 'report')]:
        if state_file.is_file():
            protected.add(Path(json.loads(state_file.read_text())[key]))
    for stage in stages:
        if stage not in protected and stage.resolve().parent == backup_root.resolve():
            shutil.rmtree(stage)
    targets = {os.readlink(DATA / 'current')} if (DATA / 'current').is_symlink() else set()
    for stage in protected:
        prior = stage / 'transaction.before.json'
        if prior.is_file():
            targets.add(json.loads(prior.read_text()).get('current'))
        release = stage / 'release.json'
        if release.is_file():
            targets.add('releases/' + json.loads(release.read_text())['tag'])
    releases = DATA / 'releases'
    versions = sorted((p for p in releases.glob('*') if p.is_dir() and not p.is_symlink()
                       and re.fullmatch(r'\d{12}', p.name)), reverse=True)
    for version in versions[3:]:
        if 'releases/' + version.name not in targets and version.resolve().parent == releases.resolve():
            shutil.rmtree(version)


def apply(stage, plan, bootstrap_plan):
    before = inspect()
    if before['Id'] != plan['container_id']:
        raise RuntimeError('3x-ui changed after preflight')
    original = (ROOT / COMPOSE_FILES[0]).read_bytes()
    if sha(original) != plan['compose_sha256']:
        raise RuntimeError('Compose changed after preflight')
    runtime = json.loads(run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
    if sha(json.dumps(runtime, sort_keys=True).encode()) != plan['runtime_sha256']:
        raise RuntimeError('Runtime config changed after preflight; no changes applied')
    boot = not managed(before)
    if boot:
        expected = json.loads(bootstrap_plan.read_text())
        if sha(original) != expected['compose_sha256']:
            raise RuntimeError('Bootstrap Compose differs from approved/prepared SHA; no changes applied')
    old_target = os.readlink(DATA / 'current') if (DATA / 'current').is_symlink() else None
    if (DATA / 'current').exists() and old_target is None:
        raise RuntimeError('Geodata current must be a relative managed symlink')
    candidate_hashes = pair_hashes(stage / 'assets')
    committed_path = DATA / 'last-applied.json'
    committed = json.loads(committed_path.read_text()) if committed_path.is_file() else {}
    current_hashes = pair_hashes(DATA / 'current') if old_target else None
    route_change = stage / 'routing-change.json'
    if route_change.is_file():
        saved = json.loads(route_change.read_text())
        pending_route = ROOT / 'pending-youtube-cache-exception.json'
        if (not pending_route.is_file() or sha(pending_route.read_bytes())
                != saved['pending_sha256']):
            raise RuntimeError('Pending exception changed after preparation')
        if template_state(before)[1] != saved['before']:
            raise RuntimeError('Panel template changed after preparation; no changes applied')
    if confirmed_no_change(boot, current_hashes, candidate_hashes, committed, plan.get('routing_change', False)):
        save(stage / 'NO_CHANGE.json', {'changed': False, 'restarted': False})
        clear_completed_exception(stage)
        print('Geodata unchanged; no restart', flush=True)
        return
    nightly_window()
    # SQLite online backup is private; routine rollback does not overwrite live DB.
    mount = next(m['Source'] for m in before['Mounts'] if m['Destination'] == '/etc/x-ui')
    with closing(sqlite3.connect('file:' + mount + '/x-ui.db?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(stage / 'x-ui.db.backup')) as target_db:
            source.backup(target_db)
    save(stage / 'container.before.json', before)
    save(stage / 'transaction.before.json', {'current': old_target, 'hashes': plan['previous_hashes'], 'bootstrap': boot})
    target = install_release(stage, plan['release'])
    atomic_bytes(DATA / 'pending.json', json.dumps({'stage': str(stage)}).encode())
    changed = {'current': False, 'compose': False, 'routing': False, 'restart_attempted': False}
    def activate():
        nightly_window()
        set_current(target)
        changed['current'] = True
        if route_change.is_file():
            saved = json.loads(route_change.read_text())
            cas_template(saved['db'], saved['before'], saved['after'])
            changed['routing'] = True
        if boot:
            atomic_bytes(ROOT / COMPOSE_FILES[0], (stage / 'base.candidate.yml').read_bytes(), 0o644)
            changed['compose'] = True
            # Never pull or restart dependencies; keep the currently running image.
            image_ref = resolved()['services']['3x-ui']['image']
            if json.loads(run('docker', 'image', 'inspect', image_ref))[0]['Id'] != before['Image']:
                raise RuntimeError('3x-ui image tag moved; refusing bootstrap')
            changed['restart_attempted'] = True
            run(*compose(), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', '3x-ui', stage=stage)
        else:
            changed['restart_attempted'] = True
            run('docker', 'restart', '3x-ui', stage=stage)
    def rollback():
        if changed['routing']:
            restore_cache_exception(stage)
        if changed['current']:
            set_current(old_target)
        if changed['compose']:
            atomic_bytes(ROOT / COMPOSE_FILES[0], original, 0o644)
        if changed['restart_attempted']:
            if boot:
                run('docker', 'compose', '--project-directory', str(ROOT), '-p', 'x-ui',
                    '-f', str(stage / 'rollback.compose.json'), 'up', '-d', '--no-deps',
                    '--no-build', '--pull', 'never', '--force-recreate', '3x-ui', stage=stage)
            else:
                run('docker', 'restart', '3x-ui', stage=stage)
    try:
        transaction(stage, activate, lambda: verify_runtime(stage, candidate_hashes, True,
                    'runtime.expected.json' if route_change.is_file() else 'runtime.before.json'), rollback,
                    lambda: verify_runtime(stage, plan['previous_hashes'], not boot))
    except BaseException:
        result = stage / 'rollback-result.json'
        if result.is_file() and json.loads(result.read_text()).get('success'):
            (DATA / 'pending.json').unlink(missing_ok=True)
        raise
    atomic_bytes(committed_path, json.dumps({'hashes': candidate_hashes, 'release': plan['release']['tag'],
                 'verified_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'report': str(stage)}).encode())
    clear_completed_exception(stage)
    (DATA / 'pending.json').unlink(missing_ok=True)
    print('Geodata applied and TCP/UDP verified: ' + str(stage), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--nightly', action='store_true')
    parser.add_argument('--queue-youtube-cache', action='store_true')
    parser.add_argument('--bootstrap-plan', type=Path)
    args = parser.parse_args()
    if sum((args.prepare, args.nightly, args.queue_youtube_cache)) != 1:
        parser.error('Choose --prepare, --nightly or --queue-youtube-cache')
    if args.nightly and args.bootstrap_plan is None:
        parser.error('--nightly requires --bootstrap-plan')
    os.umask(0o077)
    import fcntl
    with open(LOCK, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Container maintenance busy; skipped without restart', flush=True)
            return
        if args.nightly:
            nightly_window()
        helpers()['check_project_profile'](recovering_panel=args.nightly and (DATA / 'pending.json').is_file())
        if args.nightly:
            recover_pending()
        elif (DATA / 'pending.json').exists():
            raise RuntimeError('Pending transaction requires recovery in the next night window')
        try:
            request = None
            if args.queue_youtube_cache:
                if (ROOT / 'pending-youtube-cache-exception.json').exists():
                    raise RuntimeError('An approved cache exception is already queued')
                container = inspect()
                db, text = template_state(container)
                runtime = json.loads(run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
                template = json.loads(text)
                changed_template = cache_exception_template(template)
                if template == changed_template:
                    raise RuntimeError('Exact cache exception is already applied; nothing queued')
                new = json.dumps(changed_template, ensure_ascii=False, sort_keys=True)
                request = {'rule': CACHE_RULE, 'expected_template_sha256': sha(text.encode()),
                           'expected_runtime_sha256': sha(json.dumps(runtime, sort_keys=True).encode()),
                           'new_template_sha256': sha(new.encode())}
            stage, plan = prepare_stage(request)
            print('Prepared geodata: ' + str(stage), flush=True)
            if args.queue_youtube_cache:
                if not plan['routing_change']:
                    raise RuntimeError('Cache exception already applied; nothing queued')
                atomic_bytes(ROOT / 'pending-youtube-cache-exception.json', json.dumps(request, indent=2).encode())
                print('Exact cache exception queued for the existing 04:00 NSK job; DB/runtime unchanged', flush=True)
            if args.nightly:
                apply(stage, plan, args.bootstrap_plan)
        finally:
            prune_reports()


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise RuntimeError('Interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except Exception as error:
        print('ERROR: ' + str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)
