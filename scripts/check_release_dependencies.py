"Require complete early scans of both locked dependency sets."
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

COMPONENTS = ('control-plane', 'verifier')
MAX_LOCK_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
PIN = re.compile(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)(.*)')
HASH = re.compile(r'--hash=sha256:[0-9a-f]{64}')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def canonical(name):
    require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name), "Invalid package name")
    return re.sub(r'[-_.]+', '-', name).lower()


def read_regular(path, maximum, *, make_private=False):
    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), "file link refused")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_size <= maximum,
                "irregular or oversized file")
        if make_private:
            os.fchmod(stream.fileno(), 0o600)
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    require(len(raw) == before.st_size and len(raw) <= maximum
            and (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns),
            "file changed during read")
    return raw


def locked_packages(raw):
    require(len(raw) <= MAX_LOCK_BYTES, "Lockfile is too large")
    lines, pending = [], ''
    for line in raw.decode().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        pending += ' ' + line.removesuffix('\\').strip()
        if not line.endswith('\\'):
            lines.append(pending.strip())
            pending = ''
    require(not pending and lines, "empty or truncated lockfile")
    packages = {}
    for line in lines:
        match = PIN.fullmatch(line)
        require(match is not None, "unpinned dependency")
        name, version, hashes = match.groups()
        hashes = hashes.split()
        require(hashes and all(HASH.fullmatch(value) for value in hashes), "dependency hash absent or invalid")
        key = canonical(name)
        require(key not in packages, "duplicate package")
        packages[key] = version
    return packages


def write_private(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)


def prepare(root, source=Path('.')):
    root, source = Path(root), Path(source)
    require(not root.exists() and not any(p.is_symlink() for p in (root, *root.parents)), "new private output required")
    root.mkdir(mode=0o700)
    (root / 'locks').mkdir(mode=0o700)
    expected = {}
    for component in COMPONENTS:
        raw = read_regular(source / 'docker' / f'{component}-requirements.txt', MAX_LOCK_BYTES)
        packages = locked_packages(raw)
        directory = root / 'locks' / component
        directory.mkdir(mode=0o700)
        write_private(directory / 'requirements.txt', raw)
        expected[component] = {'sha256': hashlib.sha256(raw).hexdigest(), 'packages': packages}
    write_private(root / 'expected.json', json.dumps(expected, sort_keys=True).encode())
    return {name: len(value['packages']) for name, value in expected.items()}


def check(root):
    root = Path(root)
    require(not root.is_symlink() and stat.S_IMODE(root.stat().st_mode) == 0o700, "private output required")
    expected = json.loads(read_regular(root / 'expected.json', MAX_LOCK_BYTES))
    require(isinstance(expected, dict) and set(expected) == set(COMPONENTS), "Both lockfiles are required")
    for component in COMPONENTS:
        raw = read_regular(root / 'locks' / component / 'requirements.txt', MAX_LOCK_BYTES)
        require(hashlib.sha256(raw).hexdigest() == expected[component]['sha256']
                and locked_packages(raw) == expected[component]['packages'], "parsed lockfile changed")
    report = json.loads(read_regular(root / 'report.json', MAX_REPORT_BYTES, make_private=True))
    require(isinstance(report, dict) and type(report.get('SchemaVersion')) is int and report['SchemaVersion'] == 2
            and report.get('ArtifactType') == 'filesystem', "unexpected Trivy report")
    results = report.get('Results')
    require(isinstance(results, list) and len(results) == len(COMPONENTS), "Scan of both files is missing")
    found, vulnerabilities = {}, 0
    targets = {f'{component}/requirements.txt': component for component in COMPONENTS}
    for result in results:
        require(isinstance(result, dict) and result.get('Target') in targets
                and result.get('Class') == 'lang-pkgs' and result.get('Type') == 'pip', "Unexpected scanner or file")
        component = targets[result['Target']]
        require(component not in found, "duplicate result")
        packages = result.get('Packages')
        require(isinstance(packages, list) and packages, "Package inventory is missing")
        inventory = {}
        for package in packages:
            require(isinstance(package, dict) and isinstance(package.get('Version'), str), "Invalid Trivy package")
            name = canonical(package.get('Name'))
            require(name not in inventory, "duplicate Trivy package")
            inventory[name] = package['Version']
        require(inventory == expected[component]['packages'], "Trivy inventory differs from the lockfile")
        findings = result.get('Vulnerabilities', [])
        require(isinstance(findings, list), "malformed vulnerabilities")
        for finding in findings:
            require(isinstance(finding, dict) and finding.get('Severity') in {'HIGH', 'CRITICAL'}
                    and isinstance(finding.get('FixedVersion'), str) and finding['FixedVersion']
                    and inventory.get(canonical(finding.get('PkgName'))) == finding.get('InstalledVersion'),
                    "unexpected vulnerability filter")
        vulnerabilities += len(findings)
        found[component] = len(inventory)
    require(set(found) == set(COMPONENTS), "incomplete file coverage")
    require(not vulnerabilities, "fixable HIGH/CRITICAL vulnerabilities detected")
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'check'))
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    try:
        counts = prepare(args.root) if args.phase == 'prepare' else check(args.root)
        print(json.dumps({'status': 'release_dependencies_' + args.phase, 'packages': counts}))
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        print('release_dependency_gate_INCOMPLETE')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
