"""Opt-in security fault injection through the real project-command/HTTP seams."""
import json
import os
import subprocess
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = 'rss-aggregator-freshrss'
OVERRIDE = '/etc/php/8.4/apache2/conf.d/99-project-negative-check.ini'


@unittest.skipUnless(os.environ.get('RSS_LIVE') == '1', 'opt-in real Podman/HTTP test')
class LiveSecurityGate(unittest.TestCase):
    def command(self, *args):
        return subprocess.run(args, text=True, capture_output=True, check=False)

    def exec(self, *args):
        return self.command('podman', '--connection', 'rss-aggregator', 'exec', APP, *args)

    def test_effective_apache_override_blocks_serving_and_removes_probe(self):
        result = self.command(str(ROOT / 'scripts/rss'), 'status')
        self.assertEqual(result.returncode, 0, 'Project ownership/status check failed')
        state = Path(os.environ.get('RSS_STATE_DIR', ROOT / '.local'))
        owner = json.loads((state / 'owner.json').read_text())
        result = self.command('podman', '--connection', 'rss-aggregator', 'container', 'inspect', APP)
        self.assertEqual(result.returncode, 0, 'Application must exist before this test')
        info = json.loads(result.stdout)[0]
        self.assertEqual(info['Config']['Labels'].get('io.rss-aggregator.owner'), owner['token'])
        self.assertTrue(info['State']['Running'], 'Start the project before this test')
        self.assertEqual(self.exec('test', '-e', OVERRIDE).returncode, 1,
                         'Refusing to overwrite an existing PHP configuration file')
        try:
            result = self.exec('php', '-r',
                               'file_put_contents($argv[1], "session.upload_progress.enabled=1\\n");', OVERRIDE)
            self.assertEqual(result.returncode, 0, 'Could not inject test-only override')
            result = self.command(str(ROOT / 'scripts/rss'), 'start')
            self.assertNotEqual(result.returncode, 0, 'Security gate incorrectly accepted upload progress')
            self.assertIn('upload progress is not disabled', result.stderr)
            port = info['HostConfig']['PortBindings']['80/tcp'][0]['HostPort']
            try:
                urllib.request.urlopen('http://127.0.0.1:' + port + '/', timeout=2)
            except urllib.error.HTTPError:
                self.fail('HTTP listener still enabled after gate failure')
            except (urllib.error.URLError, OSError, RemoteDisconnected):
                pass
            else:
                self.fail('Application still serving after failed security gate')
            result = self.exec('php', '-r', 'echo count(glob("p/.project-security-*.php"));')
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '0', 'Diagnostic endpoint was left behind')
        finally:
            result = self.exec('rm', '-f', OVERRIDE)
            self.assertEqual(result.returncode, 0, 'Could not remove test-only override')
            result = self.command(str(ROOT / 'scripts/rss'), 'start')
            self.assertEqual(result.returncode, 0, 'Application did not recover after override removal')


if __name__ == '__main__':
    unittest.main()
