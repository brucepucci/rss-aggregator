"""Stateful external Podman stand-in for simulated-clean command tests."""
import json
import os
import sys
from pathlib import Path

path = Path(os.environ['FAKE_PODMAN_STATE'])
state = json.loads(path.read_text())
args = sys.argv[1:]
if args == ['machine', 'list', '--format', 'json']:
    print(json.dumps(state['machines']))
elif args == ['system', 'connection', 'list', '--format', 'json']:
    print(json.dumps(state['connections']))
elif args[:2] == ['machine', 'inspect']:
    print(json.dumps([state['details'][args[2]]]))
elif args[:2] == ['machine', 'init']:
    name = args[-1]
    if any(m['Name'] == name for m in state['machines']):
        raise SystemExit('machine name conflict')
    def option(key, default):
        return int(args[args.index(key) + 1]) if key in args else default
    machine = {'Name': name, 'Running': False, 'State': 'stopped',
               'Created': 'fixture-creation', 'ConfigDir': {'Path': '/project'},
               'Rootful': '--rootful=false' not in args,
               'SSHConfig': {'Port': 50001, 'IdentityPath': '/project/key'},
               'Resources': {'CPUs': option('--cpus', 5), 'Memory': option('--memory', 4096),
                             'DiskSize': option('--disk-size', 100)}}
    state['machines'].append(machine)
    state['details'][name] = machine
    state['connections'].append({'Name': name, 'URI': 'ssh://core@localhost:50001/run/user/501/podman/podman.sock',
                                 'Identity': '/project/key', 'Default': '--update-connection=false' not in args})
    path.write_text(json.dumps(state))
else:
    raise SystemExit('Unsupported fixture operation; setup must not start/change other resources')
