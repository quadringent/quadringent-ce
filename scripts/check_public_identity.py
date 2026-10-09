"Check the private identity policy without printing its contents."
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys
import zlib
import check_publication as publication


def load_policy(path: Path, root: Path) -> list[re.Pattern[str]]:
    if path.is_symlink() or path.resolve().is_relative_to(root.resolve()) or path.stat().st_mode & 0o077:
        raise ValueError('policy must be a private external file')
    document = json.loads(path.read_text())
    if set(document) != {'identities'} or not isinstance(document['identities'], list) or not document['identities']:
        raise ValueError('invalid policy')
    patterns = []
    for entry in document['identities']:
        # Les anciens littéraux restent valides : lexical court, substring long.
        if isinstance(entry, str):
            value = entry
            mode = 'lexical' if len(value) <= 3 else 'substring'
        elif isinstance(entry, dict) and set(entry) == {'value', 'mode'}:
            value, mode = entry['value'], entry['mode']
        else:
            raise ValueError('invalid identity rule')
        if not isinstance(value, str) or len(value.strip()) < 2 or value != value.strip():
            raise ValueError('invalid identity')
        if mode not in {'lexical', 'substring'} or (mode == 'substring' and len(value) < 4):
            raise ValueError('invalid identity mode')
        expression = re.escape(value)
        if mode == 'lexical':
            # _, -, . sont séparateurs ; pas de match dans un mot/empreinte.
            expression = r'(?<![A-Za-z0-9])' + expression + r'(?![A-Za-z0-9])'
        patterns.append(re.compile(expression, re.IGNORECASE))
    return patterns


def scan_identity(text: str, label: str, patterns: list[re.Pattern[str]]) -> list[publication.Finding]:
    return [publication.Finding(label, line, 'private_identity')
            for line, value in enumerate(text.splitlines(), 1)
            if any(pattern.search(value) for pattern in patterns)]


PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
MAX_PNG_TEXT = 16 * 1024 * 1024


def bounded_inflate(value: bytes) -> bytes:
    inflater = zlib.decompressobj()
    try:
        result = inflater.decompress(value, MAX_PNG_TEXT + 1)
    except zlib.error as error:
        raise ValueError('PNG text cannot be inspected') from error
    if len(result) > MAX_PNG_TEXT or inflater.unconsumed_tail or not inflater.eof or inflater.unused_data:
        raise ValueError('PNG text cannot be inspected')
    return result


def png_metadata(content: bytes) -> list[bytes]:
    """Valide framing/CRC ; IDAT représente des pixels, pas du texte privé."""
    offset = len(PNG_SIGNATURE); chunks = []; metadata = []; ended = False
    while offset < len(content):
        if offset + 12 > len(content):
            raise ValueError('invalid PNG framing')
        size = int.from_bytes(content[offset:offset + 4], 'big')
        kind = content[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(content) or not re.fullmatch(b'[A-Za-z]{4}', kind):
            raise ValueError('invalid PNG chunk')
        value = content[offset + 8:end - 4]
        if zlib.crc32(kind + value) != int.from_bytes(content[end - 4:end], 'big'):
            raise ValueError('invalid PNG CRC')
        if not chunks and (kind != b'IHDR' or size != 13):
            raise ValueError('missing PNG header')
        if kind == b'IHDR' and chunks:
            raise ValueError('duplicate PNG header')
        chunks.append(kind)
        if kind == b'IEND':
            if size or end != len(content) or b'IDAT' not in chunks:
                raise ValueError('invalid PNG end')
            ended = True
        elif kind == b'zTXt':
            keyword, separator, rest = value.partition(b'\0')
            if not separator or not 1 <= len(keyword) <= 79 or not rest or rest[0] != 0:
                raise ValueError('invalid PNG compressed text')
            metadata.extend([keyword, bounded_inflate(rest[1:])])
        elif kind == b'iTXt':
            keyword, separator, rest = value.partition(b'\0')
            if not separator or not 1 <= len(keyword) <= 79 or len(rest) < 2 or rest[0] not in (0, 1) or rest[1] != 0:
                raise ValueError('invalid PNG international text')
            flag = rest[0]; language, separator, rest = rest[2:].partition(b'\0')
            translated, separator2, text = rest.partition(b'\0')
            if not separator or not separator2:
                raise ValueError('invalid PNG international fields')
            metadata.extend([keyword, language, translated, bounded_inflate(text) if flag else text])
        elif kind not in {b'IHDR', b'PLTE', b'IDAT', b'IEND'}:
            # tEXt, eXIf et tous ancillary inconnus restent inspectés.
            if kind[0] < 97:
                raise ValueError('unknown PNG critical chunk')
            metadata.append(value)
        offset = end
    if not ended:
        raise ValueError('missing PNG end')
    return metadata


def identity_payload_findings(content: bytes, label: str,
                              patterns: list[re.Pattern[str]]) -> list[publication.Finding]:
    """Identités seulement : combiner avec check_publication.scan_payload.

    Aucun monkeypatch ici ; adapté aux entrées wheel/sdist/chart extraites.
    Les noms des entrées sont à contrôler séparément avec scan_identity.
    """
    if content.startswith(PNG_SIGNATURE):
        try:
            values = png_metadata(content)
        except ValueError:
            return [publication.Finding(label, 0, 'png_unscanned')]
    else:
        values = [content]
    return [finding for value in values
            for finding in scan_identity(value.decode('utf-8', errors='replace'), label, patterns)]


def review(root: Path, policy: Path, history_ref: str | None = None) -> list[publication.Finding]:
    patterns = load_policy(policy, root)
    original = publication.scan_text
    original_payload = publication.scan_payload
    def combined(text, label, denied_hashes=frozenset()):
        return original(text, label, denied_hashes) + scan_identity(text, label, patterns)
    # Réutilise le parcours complet des objets Git, notamment commits/tags et
    # noms d'anciens fichiers, ainsi que les refus d'objets trop volumineux.
    def payload(content, label, denied_hashes=frozenset()):
        # Secrets/home/accounts : toujours les octets complets, sans exemption.
        findings = original(content.decode('utf-8', errors='replace'), label, denied_hashes)
        findings.extend(identity_payload_findings(content, label, patterns))
        return findings
    publication.scan_text = combined
    publication.scan_payload = payload
    try:
        findings = publication.scan_tree(root)
        if history_ref:
            findings.extend(publication.scan_history(root, history_ref))
        return findings
    finally:
        publication.scan_text = original
        publication.scan_payload = original_payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--private-policy', type=Path, required=True)
    parser.add_argument('--history-ref')
    args = parser.parse_args()
    try:
        findings = review(args.root, args.private_policy, args.history_ref)
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({'status': 'FAILED_CLOSED', 'reason': 'identity_review_unavailable'}))
        return 2
    print(json.dumps({'findings': len(findings), 'details': [asdict(f) for f in findings]}))
    return int(bool(findings))

if __name__ == '__main__':
    sys.exit(main())
