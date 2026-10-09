import os
from pathlib import Path
import subprocess


TEMPLATE = Path(__file__).parents[1] / 'deploy/terraform/gcp/vm/startup-script.tpl.sh'


def run_startup(tmp_path, curl_exit=0, service_exit=0):
    binary = tmp_path / 'bin'
    binary.mkdir()
    events = tmp_path / 'events'
    installer = tmp_path / 'installer.sh'
    installer.write_text('echo installed >> "$EVENTS"\n')
    scripts = {
        'curl': f'echo curl >> "$EVENTS"\n[ {curl_exit} -eq 0 ] || exit {curl_exit}\nwhile [ "$#" -gt 0 ] && [ "$1" != "--output" ]; do shift; done\n[ "$#" -gt 0 ] || exit 2\ncp "$INSTALLER" "$2"\n',
        'systemctl': f'echo service >> "$EVENTS"\nexit {service_exit}\n',
    }
    for name, body in scripts.items():
        target = binary / name
        target.write_text('#!/bin/sh\n' + body)
        target.chmod(0o700)
    text = TEMPLATE.read_text().replace('${k3s_channel}', 'stable').replace('${site_name}', 'test-site').replace('/etc/quadringent', str(tmp_path / 'site'))
    result = subprocess.run(['sh'], input=text, text=True, capture_output=True,
                            env={**os.environ, 'PATH': str(binary) + ':' + os.environ['PATH'], 'EVENTS': str(events), 'INSTALLER': str(installer)})
    return result, events.read_text().splitlines(), tmp_path / 'site/site.env'


def test_failed_download_never_executes_or_claims_bootstrap(tmp_path):
    result, events, site = run_startup(tmp_path, curl_exit=28)
    assert result.returncode == 28
    assert events == ['curl']
    assert not site.exists()


def test_success_requires_active_service_before_site_marker(tmp_path):
    result, events, site = run_startup(tmp_path)
    assert result.returncode == 0
    assert events == ['curl', 'installed', 'service']
    assert site.read_text() == 'site=test-site\n'


def test_inactive_service_fails_bootstrap(tmp_path):
    result, events, site = run_startup(tmp_path, service_exit=3)
    assert result.returncode == 3
    assert events == ['curl', 'installed', 'service']
    assert not site.exists()
