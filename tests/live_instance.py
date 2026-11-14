"""Opt-in real Podman/HTTP checks using disposable, labelled test resources.

RSS_LIVE=1 python3 -m unittest discover -s tests -p 'live_*.py' -v
Add RSS_OBSERVE_CRON=1 to observe two real scheduler executions, ~20–40 minutes.
Never uses or deletes the application's data volume. Needs scripts/rss start first.
"""
import html
import http.cookiejar
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'io.rss-aggregator.test'
IMAGE = json.loads((ROOT / 'config/image.json').read_text())['image']


class Browser:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path='/i/', fields=None):
        data = None if fields is None else urllib.parse.urlencode(fields).encode()
        with self.opener.open(self.base + path, data=data, timeout=90) as response:
            return response.read().decode()

    @staticmethod
    def csrf(page):
        match = re.search(r'name="_csrf"\s+value="([^"]+)"', page)
        if not match:
            match = re.search(r'"csrf"\s*:\s*"([^"]+)"', page)
        if not match:
            raise AssertionError('FreshRSS CSRF token missing')
        return html.unescape(match[1])


@unittest.skipUnless(os.environ.get('RSS_LIVE') == '1', 'opt-in real Podman/HTTP test')
class LiveInstance(unittest.TestCase):
    def podman(self, *args, input=None, check=True):
        result = subprocess.run(['podman', '--connection', 'rss-aggregator', *args],
                                input=input, text=True, capture_output=True, check=False)
        if check and result.returncode:
            # Never dump commands, HTML, credentials, or application logs on failure.
            self.fail('Podman fixture operation failed: ' + args[0] + ' (' + str(result.returncode) + ')')
        return result.stdout

    def setUp(self):
        self.token = secrets.token_hex(8)
        self.name = 'rss-aggregator-test-' + self.token
        self.volume = self.name + '-data'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.podman('volume', 'create', '--label', LABEL + '=' + self.token, self.volume)
        self.addCleanup(self.cleanup_fixture)
        self.start_fixture()
        browser = Browser(self.base)
        browser.request('/i/index.php')
        browser.request('/i/index.php?step=0', {'language': 'en'})
        page = browser.request('/i/index.php?step=2', {'type': 'sqlite'})
        self.assertTrue('name="default_user"' in page, 'HTTP SQLite installer step failed')
        self.password = secrets.token_urlsafe(32)
        page = browser.request('/i/index.php?step=3', {
            'default_user': 'testreader', 'auth_type': 'form', 'passwordPlain': self.password})
        self.assertTrue('installation process was successful' in page, 'HTTP installer did not complete')
        browser.request('/i/index.php?step=5')
        self.login()

    def login(self):
        self.browser = Browser(self.base)
        page = self.browser.request()
        self.assertTrue('id="passwordPlain"' in page, 'Unauthenticated HTTP did not require login')
        nonce = json.loads(self.browser.request('/i/?c=javascript&a=nonce&user=testreader'))
        challenge = self.podman('exec', '--interactive', self.name, 'php', '-r',
            '$v=json_decode(stream_get_contents(STDIN),true);'
            '$s=crypt($v["password"],$v["salt1"]);'
            'echo password_hash($s.$v["nonce"], PASSWORD_BCRYPT, ["cost"=>4]);',
            input=json.dumps(dict(nonce, password=self.password)))
        page = self.browser.request('/i/?c=auth&a=login', {
            'username': 'testreader', 'challenge': challenge, '_csrf': Browser.csrf(page)})
        self.assertTrue('id="passwordPlain"' not in page, 'HTTP login did not succeed')

    def start_fixture(self):
        self.podman('create', '--name', self.name, '--label', LABEL + '=' + self.token,
                    '--pull=never', '--publish', f'127.0.0.1:{self.port}:80',
                    '--mount', f'type=volume,source={self.volume},destination=/var/www/FreshRSS/data',
                    '--env', 'CRON_MIN=*/20', '--env', 'ENABLE_ACCESS_LOG=0',
                    '--env', 'COPY_LOG_TO_SYSLOG=Off', '--env', 'COPY_SYSLOG_TO_STDERR=Off', IMAGE)
        self.podman('cp', str(ROOT / 'config/config-user.custom.php'),
                    self.name + ':/var/www/FreshRSS/data/config-user.custom.php')
        self.podman('cp', str(ROOT / 'config/public-feed.opml'), self.name + ':/var/www/FreshRSS/data/opml.xml')
        # Fixture-only permission to fetch its controlled loopback publisher. Production retains upstream denylist.
        custom = Path(self.temp.name) / 'config.custom.php'
        custom.write_text("<?php return ['internal_host_allowlist' => ['127.0.0.1:80']];\n")
        self.podman('cp', str(custom), self.name + ':/var/www/FreshRSS/data/config.custom.php')
        # The image treats static Atom files as long-lived assets by default. This
        # controlled publisher must advertise freshness suitable for a changing feed.
        publisher = Path(self.temp.name) / 'fixture-publisher.conf'
        publisher.write_text('<Files "project-fixture.atom">\nExpiresActive Off\n'
                             'Header unset Cache-Control\nHeader always set Cache-Control "no-cache, max-age=0"\n</Files>\n')
        self.podman('cp', str(publisher), self.name + ':/etc/apache2/conf-enabled/fixture-publisher.conf')
        self.podman('start', self.name)
        for _ in range(30):
            try:
                Browser(self.base).request()
                return
            except (OSError, TimeoutError):
                time.sleep(1)
        self.fail('Fixture HTTP not ready')

    def cleanup_fixture(self):
        # Deletion is restricted to unique, labelled disposable test fixtures, not application resources.
        for kind, name in [('container', self.name), ('volume', self.volume)]:
            result = self.podman(kind, 'inspect', name, check=False)
            if not result.strip():
                continue
            info = json.loads(result)[0]
            labels = info.get('Labels', {}) if kind == 'volume' else info['Config'].get('Labels', {})
            if not name.startswith('rss-aggregator-test-') or labels.get(LABEL) != self.token:
                raise AssertionError('Refusing fixture cleanup: ownership mismatch')
            if kind == 'container':
                self.podman('stop', '--time', '60', name)
                self.podman('rm', name)
            else:
                self.podman('volume', 'rm', name)

    def test_cron_command_collects_a_changed_controlled_feed(self):
        # Fast feedback only: remove the 60-second minimum cache floor in this disposable
        # fixture. The natural */20 scenario retains the production cache/TTL policy.
        self.podman('exec', '--user', 'www-data', self.name, 'php', '-r',
                    'require "cli/_cli.php";$p="data/config.php";$v=require $p;$d=require "config.default.php";'
                    '$v["limits"]=array_replace($d["limits"],$v["limits"]??[],["cache_duration_min"=>0]);'
                    'file_put_contents($p,"<?php return ".var_export($v,true).";");')
        self.controlled_feed(1)
        opml = Path(self.temp.name) / 'controlled.opml'
        opml.write_text('<opml version="2.0"><head><title>Test</title></head><body>'
                        '<outline text="Controlled fixture" type="rss" '
                        'xmlUrl="http://127.0.0.1/project-fixture.atom" /></body></opml>')
        self.podman('cp', str(opml), self.name + ':/tmp/controlled.opml')
        self.podman('exec', '--user', 'www-data', self.name, 'php', 'cli/import-for-user.php',
                    '--user', 'testreader', '--filename', '/tmp/controlled.opml')
        self.podman('exec', '--user', 'www-data', self.name, 'php', 'cli/reconfigure-user.php',
                    '--user', 'testreader', '--key', 'ttl_default', '--set', '--value', '1')
        cron = ('. /var/www/FreshRSS/Docker/env.txt; '
                "su www-data -s /bin/sh -c 'php /var/www/FreshRSS/app/actualize_script.php' > /tmp/FreshRSS.log")
        self.podman('exec', self.name, 'sh', '-c', cron)
        self.wait_article('Controlled article 1')
        self.controlled_feed(2)
        time.sleep(2)  # Wait for fixture eligibility; the scheduler configuration is unchanged.
        self.podman('exec', self.name, 'sh', '-c', cron)
        page = self.browser.request('/i/?a=normal&get=a&state=3')
        self.assertTrue('Controlled article 2' in page, 'Completed cron command missed changed controlled feed')

    def test_actualization_observer_detects_a_completed_run(self):
        # Match the official cron stdout boundary without changing its schedule.
        self.podman('exec', self.name, 'sh', '-c',
                    "su www-data -s /bin/sh -c 'php /var/www/FreshRSS/app/actualize_script.php' > /tmp/FreshRSS.log")
        self.assertTrue(self.cron_starts(), 'Actualization observer missed a known completed execution')

    def test_installed_instance_can_move_to_another_loopback_port(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        self.podman('exec', '--user', 'www-data', self.name, 'php', 'cli/reconfigure.php', '--base-url', base)
        self.podman('stop', '--time', '60', self.name)
        self.podman('rm', self.name)
        self.port, self.base = port, base
        self.start_fixture()
        self.login()
        page = self.browser.request('/i/?c=subscription')
        self.assertTrue('FreshRSS releases' in page, 'Subscription missing after base URL/port change')

    def test_authenticated_feed_articles_settings_and_replacement(self):
        self.podman('exec', '--user', 'www-data', self.name, 'php', 'app/actualize_script.php')
        page = self.browser.request('/i/?a=normal&get=a&state=3')
        self.assertTrue('FreshRSS releases' in page, 'Public subscription missing from HTTP')
        entry = re.search(r'id="flux_(\d+)"', page)
        self.assertIsNotNone(entry, 'Fetched public-feed articles missing from HTTP')
        entry_id = entry[1]
        csrf = Browser.csrf(page)
        self.browser.request('/i/?c=entry&a=bookmark', {'id': entry_id, 'is_favorite': '1', '_csrf': csrf})
        self.browser.request('/i/?c=entry&a=read', {'id': entry_id, 'is_read': '1', '_csrf': csrf})
        self.assert_read_and_starred(entry_id)
        settings = self.browser.request('/i/?c=configure&a=archiving')
        # Independent HTTP assertions: both purge limits must be off for the installed user.
        for name in ('enable_keep_period', 'enable_keep_max'):
            control = re.search(r'<input[^>]*name="' + name + r'"[^>]*>', settings)
            self.assertIsNotNone(control, 'Archiving checkbox missing: ' + name)
            self.assertNotIn('checked', control[0])
        self.podman('stop', '--time', '60', self.name)
        self.podman('rm', self.name)
        self.start_fixture()
        self.login()  # PHP sessions are ephemeral; the persisted account must still authenticate.
        page = self.browser.request('/i/?a=normal&get=a&state=3')
        self.assertTrue('FreshRSS releases' in page, 'Public subscription lost after replacement')
        self.assert_read_and_starred(entry_id)
        # Exercise project shutdown/restart with fixture writers stopped first.
        self.podman('stop', '--time', '60', self.name)
        for command in ('stop', 'stop', 'start'):
            result = subprocess.run([str(ROOT / 'scripts/rss'), command], text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, 'Project lifecycle command failed: ' + command + '\n' + result.stderr)
            if command == 'stop':
                with socket.socket() as connection:
                    self.assertNotEqual(connection.connect_ex(('127.0.0.1', self.port)), 0)
        self.podman('start', self.name)
        self.login()
        self.assert_read_and_starred(entry_id)
        self.controlled_feed(1)
        opml = Path(self.temp.name) / 'controlled.opml'
        opml.write_text('<opml version="2.0"><head><title>Test</title></head><body>'
                        '<outline text="Controlled fixture" type="rss" '
                        'xmlUrl="http://127.0.0.1/project-fixture.atom" /></body></opml>')
        self.podman('cp', str(opml), self.name + ':/tmp/controlled.opml')
        self.podman('exec', '--user', 'www-data', self.name, 'php', 'cli/import-for-user.php',
                    '--user', 'testreader', '--filename', '/tmp/controlled.opml')
        self.podman('exec', '--user', 'www-data', self.name, 'php', 'app/actualize_script.php')
        page = self.browser.request('/i/?a=normal&get=a&state=3')
        self.assertTrue('Controlled article 1' in page, 'Controlled feed fetch failed')
        if os.environ.get('RSS_OBSERVE_CRON') == '1':
            # Real scheduler observations, not a shortened schedule or manual cron invocation.
            started = time.monotonic()
            baseline = self.cron_starts()
            seen = set()
            self.controlled_feed(2)
            advanced = False
            while time.monotonic() - started < 2460:
                seen.update(self.cron_starts() - baseline)
                if seen and not advanced:
                    self.wait_article('Controlled article 2')
                    self.controlled_feed(3)
                    advanced = True
                if len(seen) >= 2:
                    self.wait_article('Controlled article 3')
                    print('Observed natural */20 cron execution timestamps: ' + ', '.join(sorted(seen)), flush=True)
                    break
                time.sleep(5)
            self.assertGreaterEqual(len(seen), 2, 'Two natural cron executions were not observed')

    def assert_read_and_starred(self, entry_id):
        page = self.browser.request('/i/?a=normal&get=s&state=1')
        self.assertTrue('id="flux_' + entry_id + '"' in page, 'Read/starred article state not preserved')

    def cron_starts(self):
        logs = self.podman('exec', self.name, 'php', '-r',
                           'echo @file_get_contents("/tmp/FreshRSS.log");')
        return set(re.findall(r'FreshRSS starting feeds actualization at ([^\s<]+)', logs))

    def controlled_feed(self, number):
        path = Path(self.temp.name) / 'controlled.atom'
        entries = ''.join(f'<entry><id>urn:project:fixture:{i}</id><title>Controlled article {i}</title>'
                          '<updated>2026-11-14T00:00:00Z</updated><content>Deterministic test content</content>'
                          '</entry>' for i in range(1, number + 1))
        path.write_text('<feed xmlns="http://www.w3.org/2005/Atom"><id>urn:project:fixture</id>'
                        '<title>Controlled fixture</title><updated>2026-11-14T00:00:00Z</updated>'
                        + entries + '</feed>')
        stamp = time.time() + number
        os.utime(path, (stamp, stamp))  # Distinct Last-Modified even for fixtures written in the same second.
        self.podman('cp', str(path), self.name + ':/var/www/FreshRSS/p/project-fixture.atom')
        with Browser(self.base).opener.open(self.base + '/project-fixture.atom', timeout=5) as response:
            publisher = response.read().decode()
            self.assertEqual(response.headers.get('Cache-Control'), 'no-cache, max-age=0',
                             'Controlled publisher must not advertise long-lived asset caching')
        self.assertTrue(f'Controlled article {number}' in publisher, 'Controlled publisher did not expose new content')

    def wait_article(self, title):
        for _ in range(30):
            if title in self.browser.request('/i/?a=normal&get=a&state=3'):
                return
            time.sleep(1)
        self.fail('Scheduled fetching did not collect ' + title)


if __name__ == '__main__':
    unittest.main()
