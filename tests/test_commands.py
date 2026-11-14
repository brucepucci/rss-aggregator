"""Project-command boundary tests; external tools are controlled executables."""
import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / 'scripts' / 'rss'


class Commands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.env = dict(os.environ, RSS_STATE_DIR=self.temp.name, RSS_PORT=str(port))
        self.tool('podman', "import sys\nif sys.argv[1:3] in (['machine', 'list'], ['system', 'connection']): print('[]')\nelse: raise SystemExit('unexpected Podman operation')\n")

    def run_command(self, *args):
        return subprocess.run([sys.executable, str(COMMAND), *args], env=self.env, check=False,
                              text=True, capture_output=True)

    def tool(self, name, source):
        folder = Path(self.temp.name) / 'bin'
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o755)
        self.env['PATH'] = str(folder) + os.pathsep + os.environ['PATH']

    def test_bootstrap_refuses_a_preview_that_upgrades_existing_packages(self):
        self.tool('brew', '''import sys
args = sys.argv[1:]
if args == ['list', '--formula', '--versions']: print('shared 1.0')
elif args[:2] == ['install', '--dry-run']: print('Would upgrade shared 1.0 -> 2.0')
elif args[:1] == ['install']: raise SystemExit('INSTALL MUST NOT RUN')
else: print('{}')
''')
        result = self.run_command('setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('approval', result.stderr)
        self.assertTrue((Path(self.temp.name) / 'host-before.json').exists())

    def test_install_preview_cannot_reinstall_an_existing_dependency(self):
        self.tool('brew', '''import sys
args = sys.argv[1:]
if args == ['list', '--formula', '--versions']: print('shared 1.0')
elif args[:2] == ['install', '--dry-run']:
 sys.stderr.write('==> Would install dependencies for podman:\\nshared 2.0\\n')
elif args[:1] == ['install']: raise SystemExit('INSTALL MUST NOT RUN')
else: print('{}')
''')
        result = self.run_command('setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('approval', result.stderr)
        self.assertNotIn('INSTALL MUST NOT RUN', result.stderr)

    def test_simulated_clean_setup_preserves_dependencies_defaults_and_initial_inventory(self):
        external = Path(self.temp.name) / 'podman-state.json'
        external.write_text(json.dumps({
            'machines': [{'Name': 'personal', 'Running': False}], 'details': {},
            'connections': [{'Name': 'personal', 'URI': 'ssh://personal', 'Identity': '/personal/key', 'Default': True}]}))
        self.env['FAKE_PODMAN_STATE'] = str(external)
        self.env['FAKE_BREW_INSTALLED'] = str(Path(self.temp.name) / 'packages.json')
        Path(self.env['FAKE_BREW_INSTALLED']).write_text(json.dumps({'shared': '1.0'}))
        self.tool('podman', (ROOT / 'tests/fake_bootstrap_podman.py').read_text())
        self.tool('brew', '''import json, os, sys
from pathlib import Path
path = Path(os.environ['FAKE_BREW_INSTALLED'])
packages = json.loads(path.read_text())
args = sys.argv[1:]
if args == ['list', '--formula', '--versions']:
 print('\\n'.join(name + ' ' + version for name, version in packages.items()))
elif args == ['install', '--dry-run', 'podman']:
 print('Would install:\\npodman 6.2.0\\nvm-runtime 1.0')
elif args == ['install', 'podman']:
 packages.update({'podman': '6.2.0', 'vm-runtime': '1.0'}); path.write_text(json.dumps(packages))
else: print('{}')
''')
        first = self.run_command('setup')
        self.assertEqual(first.returncode, 0, first.stderr)
        initial = Path(self.temp.name, 'host-before.json').read_text()
        second = self.run_command('setup')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(Path(self.temp.name, 'host-before.json').read_text(), initial)
        additions = json.loads(Path(self.temp.name, 'host-additions.json').read_text())
        self.assertEqual(additions, {'podman': '6.2.0', 'vm-runtime': '1.0'})
        state = json.loads(external.read_text())
        self.assertEqual(state['details']['rss-aggregator']['Resources'], {'CPUs': 2, 'Memory': 2048, 'DiskSize': 20})
        self.assertFalse(state['details']['rss-aggregator']['Rootful'])
        self.assertFalse(any(m['Running'] for m in state['machines']))
        self.assertEqual([c['Name'] for c in state['connections'] if c['Default']], ['personal'])

    def test_setup_refuses_an_unowned_machine_before_any_mutation(self):
        self.tool('podman', '''import json, sys
args = sys.argv[1:]
if args == ['machine', 'list', '--format', 'json']:
 print(json.dumps([{'Name': 'rss-aggregator', 'Running': False}]))
elif args == ['system', 'connection', 'list', '--format', 'json']: print('[]')
else: raise SystemExit('MUTATION MUST NOT RUN')
''')
        result = self.run_command('setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unowned', result.stderr)
        self.assertFalse((Path(self.temp.name) / 'owner.json').exists())

    def machine_fixture(self, *, running=False, other=False, replaced=False, extra='', state=None):
        machine = {'Name': 'rss-aggregator', 'Created': 'fixture-creation',
                   'ConfigDir': {'Path': '/project'}, 'Rootful': False,
                   'SSHConfig': {'Port': 50001, 'IdentityPath': '/project/key'},
                   'State': state or ('running' if running else 'stopped')}
        connection = {'Name': 'rss-aggregator', 'URI': 'ssh://core@localhost:50001/run/user/501/podman/podman.sock',
                      'Identity': '/project/key'}
        Path(self.temp.name, 'owner.json').write_text(json.dumps(
            {'machine': machine, 'connection': connection, 'token': 'fixture-owner'}))
        if replaced:
            machine['Created'] = 'someone-elses-machine'
        machines = [{'Name': 'rss-aggregator', 'Running': running}]
        if other:
            machines.append({'Name': 'personal', 'Running': True})
        self.tool('podman', f'''import json, sys
args = sys.argv[1:]
if args == ['machine', 'list', '--format', 'json']: print({json.dumps(machines)!r})
elif args == ['system', 'connection', 'list', '--format', 'json']: print({json.dumps([connection])!r})
elif args == ['machine', 'inspect', 'rss-aggregator']: print({json.dumps([machine])!r})
{extra}
else: raise SystemExit('UNEXPECTED MUTATION: ' + ' '.join(args))
''')

    def test_security_failure_never_publishes_application(self):
        self.machine_fixture(running=True, extra='''elif '--connection' in args:
 args = args[2:]
 if args[:2] == ['image', 'inspect']:
  print(json.dumps([{'Id': 'verified-id', 'Os': 'linux', 'Architecture': 'arm64',
   'RepoDigests': ['docker.io/freshrss/freshrss@sha256:af44385229541bee24aa9148d0ff8f3b40e095373c63a2c0f74e09a27d3c59df', 'docker.io/freshrss/freshrss@sha256:c889a3e8baddfb73dc1cecc553697f250c70cbfc7dbac6212626bf5cbccfc797'],
   'Labels': {'org.opencontainers.image.version': '1.30.4', 'org.opencontainers.image.revision': '08f97bd497ace733c151ea1c709d85cd5b65cc53'},
   'Config': {'Entrypoint': ['./Docker/entrypoint.sh']}}]))
 elif args[:2] in (['container', 'exists'], ['volume', 'exists']): raise SystemExit(1)
 elif args[0] == 'run':
  if '--publish' in args: raise SystemExit('UNSAFE PUBLICATION')
  print('preflight-id')
 elif args[0] in ['cp', 'rm']: pass
 elif args[0] == 'exec':
  import os
  print(os.environ['FAKE_WEB_RESULT'])
 else: raise SystemExit('UNEXPECTED MUTATION: ' + ' '.join(args))''')
        for web in ({'sapi': 'apache2handler', 'php': '8.4.26', 'upload_progress': ''},
                    {'sapi': 'apache2handler', 'php': '8.4.27', 'upload_progress': '1'},
                    {'sapi': 'cli', 'php': '8.4.27', 'upload_progress': '0'}):
            with self.subTest(web=web):
                self.env['FAKE_WEB_RESULT'] = json.dumps(web)
                result = self.run_command('start')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('PHP', result.stderr)
                self.assertNotIn('UNSAFE PUBLICATION', result.stderr)

    def test_start_preserves_another_running_machine_and_explains_conflict(self):
        self.machine_fixture(other=True)
        result = self.run_command('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('personal', result.stderr)
        self.assertIn('running', result.stderr)
        self.assertNotIn('UNEXPECTED MUTATION', result.stderr)

    def test_stop_is_repeatable_when_project_vm_is_already_stopped(self):
        self.machine_fixture()
        for _ in range(2):
            result = self.run_command('stop')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('already stopped', result.stdout)

    def test_stop_refuses_a_replaced_machine_even_with_old_ownership_record(self):
        self.machine_fixture(replaced=True)
        result = self.run_command('stop')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('identity', result.stderr)
        self.assertNotIn('UNEXPECTED MUTATION', result.stderr)

    def test_occupied_port_is_reported_without_touching_its_owner(self):
        self.machine_fixture(running=True, extra='''elif '--connection' in args:
 if args[2:4] in (['container', 'exists'], ['volume', 'exists']): raise SystemExit(1)
 raise SystemExit('UNEXPECTED MUTATION')''')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', int(self.env['RSS_PORT'])))
            listener.listen()
            result = self.run_command('start')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('occupied', result.stderr)
            self.assertNotIn('UNEXPECTED MUTATION', result.stderr)

    def test_recently_closed_tcp_connections_do_not_block_restart(self):
        self.machine_fixture(running=True, extra='''elif '--connection' in args:
 args = args[2:]
 if args[:2] in (['container', 'exists'], ['volume', 'exists']): raise SystemExit(1)
 elif args[:2] == ['image', 'inspect'] or args[0] == 'pull': raise SystemExit('REACHED_IMAGE_GATE')
 else: raise SystemExit('UNEXPECTED MUTATION')''')
        # An active server-side close leaves TIME_WAIT but no listener owns the port.
        with socket.socket() as listener, socket.socket() as client:
            listener.bind(('127.0.0.1', int(self.env['RSS_PORT'])))
            listener.listen()
            client.connect(listener.getsockname())
            connection, _ = listener.accept()
            connection.close()
            client.recv(1)
        result = self.run_command('start')
        self.assertNotIn('occupied', result.stderr)
        self.assertIn('REACHED_IMAGE_GATE', result.stderr)

    def test_command_lock_prevents_concurrent_lifecycle_changes(self):
        with Path(self.temp.name, 'command.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_command('setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Another project command', result.stderr)

    def test_unowned_volume_is_never_adopted(self):
        self.machine_fixture(running=True, extra='''elif '--connection' in args:
 args = args[2:]
 if args[:2] == ['container', 'exists']: raise SystemExit(1)
 elif args[:2] == ['volume', 'exists']: pass
 elif args[:2] == ['volume', 'inspect']: print('[{"Labels":{"io.rss-aggregator.owner":"someone-else"}}]')
 else: raise SystemExit('UNEXPECTED MUTATION')''')
        result = self.run_command('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unowned volume', result.stderr)
        self.assertNotIn('UNEXPECTED MUTATION', result.stderr)

    def test_stop_failure_is_propagated_without_stopping_vm(self):
        self.machine_fixture(running=True, extra='''elif '--connection' in args:
 args = args[2:]
 if args[0] == 'ps': print('[{"Names":["rss-aggregator-freshrss"]}]')
 elif args[:2] == ['container', 'exists']: pass
 elif args[:2] == ['container', 'inspect']:
  print('[{"Config":{"Labels":{"io.rss-aggregator.owner":"fixture-owner"}},"State":{"Running":true}}]')
 elif args[0] == 'stop': sys.stderr.write('simulated stop failure'); raise SystemExit(125)
 else: raise SystemExit('UNEXPECTED MUTATION')''')
        result = self.run_command('stop')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('simulated stop failure', result.stderr)
        self.assertNotIn('UNEXPECTED MUTATION', result.stderr)

    def test_setup_also_validates_existing_machine_ownership(self):
        self.machine_fixture(replaced=True)
        self.tool('brew', "import sys\nprint('podman 6.2.0' if sys.argv[1:2] == ['list'] else '{}')\n")
        result = self.run_command('setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('identity', result.stderr)

    def test_invalid_port_is_rejected_before_starting_vm(self):
        self.machine_fixture()
        self.env['RSS_PORT'] = '80'
        result = self.run_command('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('RSS_PORT', result.stderr)
        self.assertNotIn('UNEXPECTED MUTATION', result.stderr)

    def test_stop_preserves_unrelated_running_containers(self):
        self.machine_fixture(running=True, extra='''elif '--connection' in args:
 if args[2] == 'ps': print('[{"Names":["personal-workload"]}]')
 else: raise SystemExit('UNEXPECTED MUTATION')''')
        result = self.run_command('stop')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('personal-workload', result.stderr)
        self.assertNotIn('UNEXPECTED MUTATION', result.stderr)

    def test_stop_does_not_claim_a_transitioning_vm_is_stopped(self):
        self.machine_fixture(state='starting')
        result = self.run_command('stop')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('starting', result.stderr)
        self.assertNotIn('already stopped', result.stdout)

    def test_fetch_waits_until_http_installer_is_fully_completed(self):
        self.machine_fixture(running=True, extra='''elif '--connection' in args:
 args = args[2:]
 if args[:2] == ['container', 'inspect']:
  print('[{"Config":{"Labels":{"io.rss-aggregator.owner":"fixture-owner"}}}]')
 elif args[0] == 'exec' and args[2] == 'test':
  raise SystemExit(1 if args[-1].endswith('applied_migrations.txt') else 0)
 else: raise SystemExit('UNEXPECTED MUTATION')''')
        result = self.run_command('fetch')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('installer pending', result.stdout)

    def test_unreachable_engine_is_not_mistaken_for_an_absent_container(self):
        self.machine_fixture(running=True, extra='''elif '--connection' in args:
 if args[2:4] == ['container', 'exists']:
  sys.stderr.write('engine unreachable'); raise SystemExit(125)
 else: raise SystemExit('UNEXPECTED MUTATION')''')
        result = self.run_command('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot determine container state', result.stderr)
        self.assertIn('engine unreachable', result.stderr)
        self.assertNotIn('UNEXPECTED MUTATION', result.stderr)

    def test_status_before_setup_reports_absent_without_creating_resources(self):
        result = self.run_command('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('not set up', result.stdout)
        self.assertFalse((Path(self.temp.name) / 'owner.json').exists())


if __name__ == '__main__':
    unittest.main()
