#!/usr/bin/env python3
"""Probe a candidate WARP SOCKS endpoint without changing production routing.

Run on the VPS as root. Uses three concurrent clients; reports RSS, anonymous
cgroup memory and file descriptors across repeated TCP/UDP sessions and idle.
"""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import runpy
import socket
import struct
import subprocess
import time


def run(*args):
    return subprocess.check_output(args, text=True, timeout=20).strip()


def snapshot(container):
    state = json.loads(run('docker', 'inspect', container))[0]['State']
    if not state['Running']:
        raise RuntimeError('Candidate container stopped')
    proc = Path('/proc') / str(state['Pid'])
    status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines())
    cgroup = next(line.split(':', 2)[2] for line in (proc / 'cgroup').read_text().splitlines()
                  if line.startswith('0::'))
    memory = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
    stat = dict(line.split() for line in (memory / 'memory.stat').read_text().splitlines())
    return {'time': round(time.time(), 3),
            'rss_kib': int(status['VmRSS'].split()[0]),
            'rss_anon_kib': int(status['RssAnon'].split()[0]),
            'fd_count': len(list((proc / 'fd').iterdir())),
            'threads': int(status['Threads']),
            'cgroup_anon_bytes': int(stat['anon']),
            'cgroup_file_bytes': int(stat['file']),
            'cgroup_kernel_bytes': int(stat['kernel'])}


def tcp_probe(proxy):
    body = run('curl', '--fail', '--silent', '--show-error', '--noproxy', '',
               '--connect-timeout', '5', '--max-time', '10', '--socks5-hostname',
               f'{proxy[0]}:{proxy[1]}', 'https://1.1.1.1/cdn-cgi/trace')
    trace = dict(line.split('=', 1) for line in body.splitlines() if '=' in line)
    if trace.get('warp') not in ('on', 'plus'):
        raise RuntimeError('TCP egress is not WARP')
    return {k: trace[k] for k in ('ip', 'warp', 'loc')}


def stun_probe(proxy, helpers):
    """Discover the actual UDP exit IP, not just successful UDP forwarding."""
    target = socket.gethostbyname('stun.cloudflare.com')
    transaction = os.urandom(12)
    query = struct.pack('!HHI', 1, 0, 0x2112A442) + transaction
    with socket.create_connection(proxy, timeout=5) as control:
        control.settimeout(5)
        relay, port = helpers['socks_connect'](control, '0.0.0.0', 0, udp=True)
        if relay == '0.0.0.0':
            relay = proxy[0]
        header = b'\x00\x00\x00\x01' + socket.inet_aton(target) + struct.pack('!H', 3478)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(5)
            udp.sendto(header + query, (relay, port))
            packet, _ = udp.recvfrom(4096)
    if packet[:4] != b'\x00\x00\x00\x01':
        raise RuntimeError('Unexpected SOCKS UDP response')
    body = packet[10:]
    if body[:2] != b'\x01\x01' or body[8:20] != transaction:
        raise RuntimeError('Unexpected STUN response')
    offset = 20
    while offset + 4 <= len(body):
        kind, size = struct.unpack('!HH', body[offset:offset + 4])
        value = body[offset + 4:offset + 4 + size]
        if kind == 0x0020 and len(value) == 8 and value[1] == 1:
            address = struct.unpack('!I', value[4:8])[0] ^ 0x2112A442
            return {'ip': socket.inet_ntoa(struct.pack('!I', address)),
                    'port': struct.unpack('!H', value[2:4])[0] ^ 0x2112}
        offset += 4 + ((size + 3) & ~3)
    raise RuntimeError('STUN IPv4 XOR-MAPPED-ADDRESS missing')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', required=True, help='IPv4 address:port')
    parser.add_argument('--container', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=8)
    parser.add_argument('--sessions', type=int, default=50, help='Sessions of EACH protocol per round')
    parser.add_argument('--pause', type=float, default=15)
    parser.add_argument('--idle', type=float, default=75)
    args = parser.parse_args()
    host, port = args.proxy.rsplit(':', 1)
    proxy = (host, int(port))
    helpers = runpy.run_path(str(Path(__file__).with_name('probe-stack.py')))
    result = {'started_at': time.time(), 'container': args.container,
              'rounds': args.rounds, 'sessions_per_protocol_per_round': args.sessions,
              'concurrency': 3, 'samples': []}
    def save():
        args.output.write_text(json.dumps(result, indent=2))
    def sample(label):
        value = dict(label=label, **snapshot(args.container))
        result['samples'].append(value)
        save()
        print(json.dumps(value), flush=True)
    def session(index):
        if index % 2:
            return helpers['udp_probe'](proxy)
        return tcp_probe(proxy)
    try:
        result['tcp'] = tcp_probe(proxy)
        result['udp_dns'] = helpers['udp_probe'](proxy)
        result['udp_stun'] = stun_probe(proxy, helpers)
        direct = run('curl', '--fail', '--silent', '--show-error', '--noproxy', '*',
                     '--connect-timeout', '5', '--max-time', '10',
                     'https://1.1.1.1/cdn-cgi/trace')
        direct = dict(line.split('=', 1) for line in direct.splitlines() if '=' in line)
        result['server_ip'] = direct['ip']
        # WARP can use different Cloudflare exit addresses for TCP and UDP.
        if direct.get('warp') != 'off' or result['udp_stun']['ip'] == direct['ip']:
            raise RuntimeError('Direct baseline unavailable or UDP exposes the VPS address')
        sample('baseline')
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for number in range(args.rounds):
                list(pool.map(session, range(args.sessions * 2)))
                sample(f'round-{number + 1}')
                if number + 1 < args.rounds:
                    time.sleep(args.pause)
        time.sleep(args.idle)
        sample('idle')
        result['tcp_after'] = tcp_probe(proxy)
        result['udp_after'] = helpers['udp_probe'](proxy)
        result['completed_sessions'] = args.rounds * args.sessions * 2
        result['finished_at'] = time.time()
        result['success'] = True
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        save()


if __name__ == '__main__':
    main()
