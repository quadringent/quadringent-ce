"Export the reviewed tree without history, caches or ignored files."
from pathlib import Path
import argparse
import shutil

from check_publication import load_private_hashes, publication_paths, scan_tree


def export(root: Path, destination: Path,
           denied_hashes: frozenset[str] = frozenset()) -> int:
    if destination.exists():
        raise ValueError("The destination must be new")
    findings = scan_tree(root, denied_hashes)
    if findings:
        raise ValueError(f"Export refused: {len(findings)} finding(s)")
    paths = publication_paths(root)
    # Ne suit pas un lien qui introduirait des fichiers hors du clone audité.
    if any((root / path).is_symlink() for path in paths):
        raise ValueError("Export refused: symbolic link")
    destination.mkdir(parents=True, mode=0o700)
    for path in paths:
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / path, target)
    return len(paths)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-denylist-file", type=Path,
                        help="external file of site-specific SHA256 digests")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    denied_hashes = frozenset()
    if args.private_denylist_file:
        if args.private_denylist_file.resolve().is_relative_to(root):
            parser.error("private denylist must live outside the source tree")
        try:
            denied_hashes = load_private_hashes(args.private_denylist_file)
        except (OSError, UnicodeError, ValueError) as exc:
            parser.error(f"private denylist cannot be loaded: {type(exc).__name__}")
    print(f"Private export: {export(root, args.destination, denied_hashes)} files")
