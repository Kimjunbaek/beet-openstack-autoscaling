#!/usr/bin/env python3
"""One-time CP bootstrap; run the reviewed local copy, not fetched code."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) != '/home/ubuntu/beet-demo-cd':
    raise SystemExit('Place the reviewed files in /home/ubuntu/beet-demo-cd on CP')
k = ['sudo', '-n', 'k3s', 'kubectl']
old = subprocess.run(k + ['get', 'namespace', 'beet-cd-demo', '--ignore-not-found', '-o', 'json'],
                     capture_output=True, text=True, check=True).stdout
if old and json.loads(old)['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') != 'beet-pull-cd':
    raise SystemExit('Namespace exists but is not owned by this demo')
namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'beet-cd-demo',
    'labels': {'app.kubernetes.io/managed-by': 'beet-pull-cd',
               'pod-security.kubernetes.io/enforce': 'restricted'}}}
subprocess.run(k + ['apply', '-f', '-'], input=json.dumps(namespace), text=True, check=True)
for name in ['beet-demo-cd.service', 'beet-demo-cd.timer']:
    subprocess.run(['sudo', '-n', 'install', '-m', '644', str(ROOT / name), '/etc/systemd/system/' + name], check=True)
subprocess.run(['sudo', '-n', 'systemctl', 'daemon-reload'], check=True)
subprocess.run(['sudo', '-n', 'systemctl', 'enable', '--now', 'beet-demo-cd.timer'], check=True)
print('Timer enabled. Polling main every three minutes; operational BEET files are untouched.')
