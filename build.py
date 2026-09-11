#!/usr/bin/env python3
"""Build the PPX catalog site.

The generated site is committed at the repository root so GitHub Pages can
serve it directly. `src/*.html` are the page templates and `static/` holds the
served assets; `static/catalog.json` is generated from the first-party package
set when a PunPun checkout is available, and otherwise left as committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'src'
STATIC = ROOT / 'static'
CATALOG = STATIC / 'catalog.json'
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
PAGES = ('index.html', 'package.html', 'security.html', 'publish.html')


def punpun_root(explicit: str | None) -> Path | None:
    """Locate a PunPun checkout holding the first-party `packages/` tree."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get('PUNPUN_ROOT'):
        candidates.append(Path(os.environ['PUNPUN_ROOT']))
    candidates.append(ROOT.parent / 'punpun')
    for candidate in candidates:
        if (candidate / 'packages').is_dir():
            return candidate
    return None


def package_digest(package_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob('*')):
        if not path.is_file():
            continue
        digest.update(path.relative_to(package_root).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def bundled_catalog(source: Path) -> dict:
    packages = []
    for manifest in sorted((source / 'packages').glob('*/Punpun.toml')):
        metadata = tomllib.loads(manifest.read_text(encoding='utf-8'))['package']
        version = str(metadata['version'])
        packages.append({
            'name': str(metadata['name']),
            'description': str(metadata.get('description', 'PunPun package')),
            'latest_version': version,
            'version': version,
            'owner': 'PunPun Project',
            'bundled': True,
            'checksum': package_digest(manifest.parent),
            'versions': [{'version': version, 'yanked': False, 'bundled': True}],
        })
    return {'schema': 1, 'release': VERSION, 'packages': packages}


def render_pages() -> dict[str, str]:
    return {name: (SRC / name).read_text(encoding='utf-8').replace('{{VERSION}}', VERSION)
            for name in PAGES}


def main() -> int:
    parser = argparse.ArgumentParser(description='build the PPX catalog site')
    parser.add_argument('--check', action='store_true',
                        help='verify the committed site matches the sources')
    parser.add_argument('--punpun-root', metavar='PATH',
                        help='PunPun checkout supplying the first-party packages')
    args = parser.parse_args()

    rendered = render_pages()
    source = punpun_root(args.punpun_root)
    catalog = json.dumps(bundled_catalog(source), indent=2, sort_keys=True) + '\n' if source else None

    if args.check:
        stale = [name for name, content in rendered.items()
                 if not (ROOT / name).is_file() or (ROOT / name).read_text(encoding='utf-8') != content]
        if catalog is not None and CATALOG.read_text(encoding='utf-8') != catalog:
            stale.append('static/catalog.json')
        if stale:
            print('stale generated files: ' + ', '.join(sorted(stale)), file=sys.stderr)
            print('run `python3 build.py` and commit the result', file=sys.stderr)
            return 1
        scope = 'pages and catalog' if catalog is not None else 'pages (catalog not regenerated)'
        print(f'PPX site is current ({scope})')
        return 0

    for name, content in rendered.items():
        (ROOT / name).write_text(content, encoding='utf-8')
    if catalog is not None:
        CATALOG.write_text(catalog, encoding='utf-8')
        print(f'built PPX site -> {ROOT} (catalog from {source})')
    else:
        print(f'built PPX site -> {ROOT} (kept the committed catalog; '
              'pass --punpun-root to regenerate it)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
