#!/usr/bin/env python3
"""Installed locally on CP. Pull only CI-approved main; never execute repo code on CP."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

REPO = 'Kimjunbaek/beet-openstack-autoscaling'
API = 'https://api.github.com/repos/' + REPO
NS = 'beet-cd-demo'
APP = 'beet-cd-demo'
ROOT = Path(__file__).resolve().parent
K = ['sudo', '-n', 'k3s', 'kubectl', '-n', NS]

def request(url, limit=3000000):
    req = urllib.request.Request(url, headers={'User-Agent': 'BEET-CD',
                                               'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=20) as res:
        data = res.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Response exceeds limit')
    return data

def api(path):
    return json.loads(request(API + path))

def kubectl(*args, data=None):
    proc = subprocess.run(K + list(args), input=data, text=True, capture_output=True, timeout=180)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-3000:] or proc.stdout[-3000:])
    return proc.stdout

def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)

def eligible_run(runs, sha):
    candidates = [r for r in runs if r.get('head_sha') == sha and r.get('head_branch') == 'main'
                  and r.get('event') in ('push', 'workflow_dispatch')
                  and r.get('head_repository', {}).get('full_name') == REPO
                  and r.get('path') == '.github/workflows/ci.yml']
    if not candidates:
        return None
    latest = max(candidates, key=lambda r: (r['id'], r.get('run_attempt', 1)))
    return latest if latest.get('status') == 'completed' and latest.get('conclusion') == 'success' else None

def manifest(sha, source):
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Invalid commit SHA')
    if len(source.encode()) > 65536:
        raise ValueError('App source exceeds limit')
    ast.parse(source)
    config_name = APP + '-' + sha[:12]
    labels = {'app': APP, 'app.kubernetes.io/managed-by': 'beet-pull-cd'}
    config = {'apiVersion': 'v1', 'kind': 'ConfigMap',
              'metadata': {'name': config_name, 'namespace': NS, 'labels': labels},
              'immutable': True, 'data': {'app.py': source}}
    container = {'name': 'app', 'image': 'python:3.12-alpine',
        'command': ['python', '-B', '/app/app.py'],
        'env': [{'name': 'RELEASE_SHA', 'value': sha}, {'name': 'PYTHONUNBUFFERED', 'value': '1'}],
        'ports': [{'containerPort': 8080}],
        'resources': {'requests': {'cpu': '10m', 'memory': '24Mi'},
                      'limits': {'cpu': '200m', 'memory': '64Mi'}},
        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                            'capabilities': {'drop': ['ALL']}},
        'readinessProbe': {'httpGet': {'path': '/healthz', 'port': 8080}, 'periodSeconds': 3},
        'livenessProbe': {'httpGet': {'path': '/healthz', 'port': 8080}, 'initialDelaySeconds': 10,
                          'periodSeconds': 15},
        'volumeMounts': [{'name': 'source', 'mountPath': '/app', 'readOnly': True}]}
    deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment',
        'metadata': {'name': APP, 'namespace': NS, 'labels': labels},
        'spec': {'replicas': 1, 'revisionHistoryLimit': 3, 'progressDeadlineSeconds': 120,
            'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 0, 'maxSurge': 1}},
            'selector': {'matchLabels': {'app': APP}},
            'template': {'metadata': {'labels': labels, 'annotations': {'beet/release': sha}},
                'spec': {'nodeSelector': {'kubernetes.io/hostname': 'beet-k3s-w1'},
                    'automountServiceAccountToken': False,
                    'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                                        'seccompProfile': {'type': 'RuntimeDefault'}},
                    'containers': [container],
                    'volumes': [{'name': 'source', 'configMap': {'name': config_name}}]}}}}
    service = {'apiVersion': 'v1', 'kind': 'Service',
        'metadata': {'name': APP, 'namespace': NS, 'labels': labels},
        'spec': {'type': 'ClusterIP', 'selector': {'app': APP}, 'ports': [{'port': 8080, 'targetPort': 8080}]}}
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
        'metadata': {'name': APP, 'namespace': NS},
        'spec': {'podSelector': {'matchLabels': {'app': APP}}, 'policyTypes': ['Egress'], 'egress': []}}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [config, deployment, service, policy]}

def health(sha):
    address = kubectl('get', 'service', APP, '-o', 'jsonpath={.spec.clusterIP}').strip()
    req = urllib.request.Request('http://' + address + ':8080/healthz')
    with urllib.request.urlopen(req, timeout=10) as res:
        value = json.load(res)
        if res.status != 200 or value.get('status') != 'ok' or value.get('release') != sha:
            raise RuntimeError('HTTP release verification failed')
    return value

def deploy(sha, source, run_id):
    release = manifest(sha, source)
    state_path = ROOT / 'state.json'
    previous_path = ROOT / 'last-good.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else None
    current = kubectl('get', 'deployment', APP, '--ignore-not-found', '-o', 'json')
    if current:
        owner = json.loads(current)['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by')
        if owner != 'beet-pull-cd':
            raise RuntimeError('Refusing to overwrite existing deployment')
    try:
        kubectl('apply', '-f', '-', data=json.dumps(release))
        kubectl('rollout', 'status', 'deployment/' + APP, '--timeout=150s')
        observation = health(sha)
    except Exception:
        if previous:
            kubectl('apply', '-f', '-', data=json.dumps(previous))
            kubectl('rollout', 'status', 'deployment/' + APP, '--timeout=150s')
            old_sha = previous['items'][1]['spec']['template']['metadata']['annotations']['beet/release']
            health(old_sha)
            print('ROLLBACK VERIFIED:', old_sha, flush=True)
        else:
            kubectl('delete', 'deployment', APP, '--ignore-not-found')
            print('First deployment failed; no previous version exists', flush=True)
        raise
    atomic_json(previous_path, release)
    state = {'commit': sha, 'ci_run_id': run_id, 'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
             'deployed_at': datetime.now(timezone.utc).isoformat(), 'http': observation}
    atomic_json(state_path, state)
    print('DEPLOYED:', json.dumps(state), flush=True)
    return state

def poll():
    sha = api('/branches/main')['commit']['sha']
    state_path = ROOT / 'state.json'
    if state_path.exists() and json.loads(state_path.read_text()).get('commit') == sha:
        print('Already deployed:', sha)
        return
    failed_path = ROOT / 'failed.json'
    if failed_path.exists() and json.loads(failed_path.read_text()).get('commit') == sha:
        print('BLOCKED: failed release requires a new commit or operator retry:', sha)
        return
    runs = api('/actions/workflows/ci.yml/runs?branch=main&head_sha=' + sha + '&per_page=20')['workflow_runs']
    run = eligible_run(runs, sha)
    if not run:
        print('WAIT: current main has no successful eligible CI:', sha)
        return
    source = request('https://raw.githubusercontent.com/' + REPO + '/' + sha + '/cd/app.py', 65536).decode()
    if api('/branches/main')['commit']['sha'] != sha:
        print('WAIT: main changed while preparing deployment')
        return
    try:
        deploy(sha, source, run['id'])
    except Exception as exc:
        atomic_json(failed_path, {'commit': sha, 'ci_run_id': run['id'], 'error': str(exc),
                                  'failed_at': datetime.now(timezone.utc).isoformat()})
        raise

if __name__ == '__main__':
    import fcntl
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.parse_args()
    with (ROOT / 'deploy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another deployment is running')
        poll()
