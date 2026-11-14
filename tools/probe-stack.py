#!/usr/bin/env python3
"""Active transport probes: fixed-IP HTTPS, frontend TLS, UDP and XHTTP.

VPS default uses a temporary host-loopback Xray client and never changes Docker
services. --worker runs direct probes in the caller's network namespace.
--local-socks HOST:PORT probes the already running local client (routing intact).
"""
import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import runpy
import socket
import ssl
import struct
import subprocess
import sys
import time

TARGETS = [
    ('188.43.61.141', 'rr2---sn-ug5onuxaxjvh-n8v6.googlevideo.com'),
    ('188.43.61.147', 'rr8---sn-ug5onuxaxjvh-n8v6.googlevideo.com'),
    ('80.64.175.142', 'rr3---sn-npcnxu-v8cs.googlevideo.com'),
    ('89.113.122.236', 'r1---sn-8ph2xajvh-vhns.googlevideo.com'),
    ('172.217.131.169', 'rr4---sn-q4fl6nss.googlevideo.com'),
    ('173.194.57.70', 'rr1---sn-q4flrnsl.googlevideo.com'),
    ('173.194.151.68', 'rr4---sn-aj5go5-53.c.youtube.com'),
    ('74.125.100.39', 'rr2---sn-5hnekn7s.googlevideo.com'),
    ('173.194.151.67', 'rr3---sn-aj5go5-53.googlevideo.com'),
]


def exact(conn, size):
    result = b''
    while len(result) < size:
        chunk = conn.recv(size - len(result))
        if not chunk:
            raise RuntimeError('Unexpected EOF')
        result += chunk
    return result


def socks_connect(conn, ip, port, udp=False):
    conn.sendall(bytes.fromhex('050100'))
    if exact(conn, 2) != bytes.fromhex('0500'):
        raise RuntimeError('SOCKS requires authentication or rejected greeting')
    conn.sendall(bytes([5, 3 if udp else 1, 0, 1]) + socket.inet_aton(ip) + struct.pack('!H', port))
    response = exact(conn, 4)
    if response[1]:
        raise RuntimeError('SOCKS reply ' + str(response[1]))
    if response[3] == 1:
        address = socket.inet_ntoa(exact(conn, 4))
    elif response[3] == 4:
        address = socket.inet_ntop(socket.AF_INET6, exact(conn, 16))
    elif response[3] == 3:
        address = exact(conn, exact(conn, 1)[0]).decode()
    else:
        raise RuntimeError('Unknown SOCKS address type')
    return address, int.from_bytes(exact(conn, 2), 'big')


def https_probe(case):
    route, proxy, ip, host, iteration = case
    context = ssl.create_default_context()
    context.set_alpn_protocols(['http/1.1'])
    start = time.monotonic()
    result = {'route': route, 'ip': ip, 'host': host, 'iteration': iteration}
    phase = 'connect'
    try:
        with socket.create_connection(proxy or (ip, 443), timeout=5) as conn:
            conn.settimeout(5)
            if proxy:
                phase = 'socks'
                socks_connect(conn, ip, 443)
            result['setup_seconds'] = round(time.monotonic() - start, 4)
            phase = 'tls'
            with context.wrap_socket(conn, server_hostname=host) as tls:
                result['tls_completed_seconds'] = round(time.monotonic() - start, 4)
                result['tls'] = tls.version()
                phase = 'http'
                tls.sendall(f'GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
                response = b''
                while b'\r\n' not in response and len(response) < 1024:
                    data = tls.recv(1024)
                    if not data:
                        break
                    response += data
                result['status'] = response.split(b'\r\n')[0].decode(errors='replace')
                if not result['status'].startswith('HTTP/'):
                    raise RuntimeError('No HTTP status line')
    except Exception as error:
        result.update(phase=phase, error=f'{type(error).__name__}: {error}')
    result['seconds'] = round(time.monotonic() - start, 4)
    return result


def matrix(routes):
    cases = [(route, proxy, ip, host, n) for n in (1, 2)
             for route, proxy in routes for ip, host in TARGETS]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(https_probe, cases))


def frontend_probe(address, mode, domain):
    context = ssl.create_default_context()
    context.set_alpn_protocols(['h2'])
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_side=False, server_hostname=domain)
    start = time.monotonic()
    with socket.create_connection(address, timeout=5) as conn:
        conn.settimeout(5)
        try:
            tls.do_handshake()
        except ssl.SSLWantReadError:
            pass
        hello = outgoing.read()
        if mode == 'tcp-split':
            conn.sendall(hello[:3])
            time.sleep(.05)
            conn.sendall(hello[3:])
        elif mode == 'record-split':
            size = int.from_bytes(hello[3:5], 'big')
            payload = hello[5:5 + size]
            cut = min(100, len(payload) - 1)
            conn.sendall(hello[:3] + cut.to_bytes(2, 'big') + payload[:cut]
                         + hello[:3] + (size - cut).to_bytes(2, 'big') + payload[cut:] + hello[5 + size:])
        else:
            conn.sendall(hello)
        while True:
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                if outgoing.pending:
                    conn.sendall(outgoing.read())
                data = conn.recv(16384)
                if not data:
                    raise RuntimeError('EOF during frontend handshake')
                incoming.write(data)
        if outgoing.pending:
            conn.sendall(outgoing.read())
    return {'mode': mode, 'hello_bytes': len(hello), 'seconds': round(time.monotonic() - start, 4),
            'tls': tls.version(), 'alpn': tls.selected_alpn_protocol()}


def udp_probe(proxy):
    with socket.create_connection(proxy, timeout=5) as control:
        control.settimeout(5)
        ip, port = socks_connect(control, '0.0.0.0', 0, udp=True)
        if ip == '0.0.0.0':
            ip = proxy[0]
        query = bytes.fromhex('4a3301000001000000000000') + b'\x07example\x03com\x00' + struct.pack('!HH', 1, 1)
        packet = bytes.fromhex('00000001') + socket.inet_aton('1.1.1.1') + struct.pack('!H', 53) + query
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(5)
            start = time.monotonic()
            udp.sendto(packet, (ip, port))
            response, _ = udp.recvfrom(4096)
            if response[:4] != bytes.fromhex('00000001') or response[10:12] != query[:2]:
                raise RuntimeError('Unexpected UDP/DNS response')
            body = response[10:]
            result = {'seconds': round(time.monotonic() - start, 4), 'rcode': body[3] & 15,
                      'answers': int.from_bytes(body[6:8], 'big')}
            if result['rcode'] or not result['answers']:
                raise RuntimeError('DNS response was unsuccessful')
            return result


def server_run():
    helper = runpy.run_path(str(Path(__file__).with_name('update-containers.py')))
    inspect, run = helper['inspect'], helper['run']
    before = {name: inspect(name) for name in helper['SERVICES']}
    runtime = json.loads(run('docker', 'exec', '3x-ui', 'cat', '/app/bin/config.json'))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = Path('/opt/x-ui/backups') / ('stack-probe-' + stamp)
    stage.mkdir(mode=0o700)
    helper['make_smoke'](stage, runtime)
    config = json.loads((stage / 'smoke.json').read_text())
    domain = config['outbounds'][0]['streamSettings']['tlsSettings']['serverName']
    config['inbounds'][0]['settings']['udp'] = True
    helper['save_json'](stage / 'smoke.json', config)
    results = {'utc': stamp, 'stage': str(stage), 'frontend': []}
    caddy_ip = next(iter(before['caddy']['NetworkSettings']['Networks'].values()))['IPAddress']
    for label, address in [('sslh', ('127.0.0.1', 443)), ('caddy', (caddy_ip, 8443))]:
        for mode in ('normal', 'tcp-split', 'record-split'):
            result = {'route': label, 'mode': mode}
            try:
                result.update(frontend_probe(address, mode, domain))
            except Exception as error:
                result['error'] = str(error)
            results['frontend'].append(result)
    results['sslh_numeric'] = 'numeric: true;' in run('docker', 'exec', 'sslh', 'cat', '/etc/sslh/sslh.cfg')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 29989))
    with (stage / 'client.log').open('a') as log:
        client = subprocess.Popen([str(stage / 'xray-smoke'), 'run', '-c', str(stage / 'smoke.json')],
                                  stdout=log, stderr=log)
        worker = None
        try:
            for _ in range(40):
                if client.poll() is not None:
                    raise RuntimeError('Temporary Xray client exited')
                try:
                    with socket.create_connection(('127.0.0.1', 29989), timeout=.2):
                        break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError('Temporary client did not start')
            trace = run('curl', '--fail', '--silent', '--show-error', '--noproxy', '',
                        '--max-time', '15', '--socks5-hostname', '127.0.0.1:29989',
                        'https://1.1.1.1/cdn-cgi/trace', timeout=20)
            results['trace'] = dict(line.split('=', 1) for line in trace.splitlines() if line.startswith(('ip=', 'warp=', 'loc=')))
            try:
                results['udp_dns'] = udp_probe(('127.0.0.1', 29989))
            except Exception as error:
                results['udp_dns'] = {'error': str(error)}
            worker = subprocess.Popen(['nsenter', '-t', str(before['3x-ui']['State']['Pid']), '-n',
                                       sys.executable, str(Path(__file__).resolve()), '--worker'],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            results['https'] = matrix([('host-direct', None), ('full-xhttp', ('127.0.0.1', 29989))])
            out, err = worker.communicate(timeout=45)
            if worker.returncode:
                raise RuntimeError('Namespace probe failed: ' + err[:200])
            results['https'] += json.loads(out)
            download = run('curl', '--fail', '--silent', '--show-error', '--noproxy', '',
                           '--max-time', '35', '--socks5-hostname', '127.0.0.1:29989', '-o', '/dev/null',
                           '-w', '%{json}', 'https://speed.cloudflare.com/__down?bytes=30000000', timeout=40)
            metrics = json.loads(download)
            results['download'] = {k: metrics[k] for k in ('http_code', 'size_download', 'speed_download', 'time_total')}
        finally:
            if worker and worker.poll() is None:
                worker.kill()
                worker.communicate()
            client.terminate()
            try:
                client.wait(timeout=10)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait()
            results['containers_unchanged'] = all(inspect(name)['Id'] == c['Id']
                                                 and inspect(name)['RestartCount'] == c['RestartCount']
                                                 for name, c in before.items())
            helper['save_json'](stage / 'results.json', results)
    print('RESULTS', stage / 'results.json')
    print('FRONTEND', json.dumps(results['frontend']))
    print('TRACE', json.dumps(results['trace']), 'UDP', json.dumps(results['udp_dns']))
    print('DOWNLOAD', json.dumps(results.get('download')), 'CONTAINERS_UNCHANGED', results['containers_unchanged'])
    for ip, _ in TARGETS:
        summary = {}
        for route in ('host-direct', 'xray-netns-direct', 'full-xhttp'):
            samples = [r for r in results['https'] if r['ip'] == ip and r['route'] == route]
            summary[route] = [(r.get('status', r.get('phase')), r['seconds']) for r in samples]
        print(ip, json.dumps(summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--local-socks')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(matrix([('xray-netns-direct', None)])))
    elif args.local_socks:
        host, port = args.local_socks.rsplit(':', 1)
        result = matrix([('pc-throne-socks', (host, int(port)))])
        if args.output:
            args.output.write_text(json.dumps(result, indent=2))
        for ip, _ in TARGETS:
            print(ip, json.dumps([(r.get('status', r.get('phase')), r['seconds']) for r in result if r['ip'] == ip]))
    else:
        os.umask(0o077)
        server_run()


if __name__ == '__main__':
    main()
