import copy
import json
import unittest
from unittest.mock import MagicMock, patch
import app
import pull_deploy as cd

SHA = 'a' * 40

class CDTests(unittest.TestCase):
    def run_record(self):
        return {'id': 1, 'head_sha': SHA, 'head_branch': 'main', 'event': 'push',
                'head_repository': {'full_name': cd.REPO}, 'path': '.github/workflows/ci.yml',
                'status': 'completed', 'conclusion': 'success'}

    def test_ci_accepts_successful_main(self):
        self.assertIsNotNone(cd.eligible_run([self.run_record()], SHA))

    def test_ci_failure_blocks(self):
        r = self.run_record(); r['conclusion'] = 'failure'
        self.assertIsNone(cd.eligible_run([r], SHA))

    def test_ci_running_blocks(self):
        r = self.run_record(); r['status'] = 'in_progress'
        self.assertIsNone(cd.eligible_run([r], SHA))

    def test_ci_pr_and_wrong_commit_block(self):
        r = self.run_record(); r['event'] = 'pull_request'
        self.assertIsNone(cd.eligible_run([r], SHA))
        self.assertIsNone(cd.eligible_run([self.run_record()], 'b' * 40))

    def test_fork_blocks(self):
        r = self.run_record(); r['head_repository']['full_name'] = 'other/fork'
        self.assertIsNone(cd.eligible_run([r], SHA))

    def test_latest_failed_run_blocks_old_success(self):
        good = self.run_record(); bad = copy.deepcopy(good)
        bad.update(id=2, conclusion='failure')
        self.assertIsNone(cd.eligible_run([good, bad], SHA))

    def test_bad_source_or_sha_rejected(self):
        with self.assertRaises(ValueError): cd.manifest('main', 'print(1)')
        with self.assertRaises(SyntaxError): cd.manifest(SHA, 'def :')

    def test_manifest_uses_isolated_namespace(self):
        value = cd.manifest(SHA, 'print(1)')
        self.assertTrue(all(x['metadata']['namespace'] == 'beet-cd-demo' for x in value['items']))
        spec = value['items'][1]['spec']['template']['spec']
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertTrue(spec['securityContext']['runAsNonRoot'])

    def test_commit_has_immutable_config_and_readiness(self):
        value = cd.manifest(SHA, 'print(1)')
        self.assertTrue(value['items'][0]['immutable'])
        self.assertEqual(value['items'][1]['spec']['template']['metadata']['annotations']['beet/release'], SHA)
        self.assertIn('readinessProbe', value['items'][1]['spec']['template']['spec']['containers'][0])

    def test_app_health_and_release(self):
        with patch.dict('os.environ', {'RELEASE_SHA': SHA}):
            status, value = app.response('/healthz')
        self.assertEqual((status, value['release']), (200, SHA))
        self.assertEqual(app.response('/missing')[0], 404)
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps(value).encode()
        with patch.object(cd, 'kubectl', return_value='10.0.0.1'), \
             patch.object(cd.urllib.request, 'urlopen', side_effect=[OSError('endpoint updating'), response]), \
             patch.object(cd.time, 'sleep') as sleep:
            self.assertEqual(cd.health(SHA)['release'], SHA)
            sleep.assert_called_once_with(2)

if __name__ == '__main__':
    unittest.main()
