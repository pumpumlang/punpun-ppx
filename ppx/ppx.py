#!/usr/bin/env python3
"""PunPunXPac (PPX) package client.

PPX deliberately uses the same Punpun.toml/Punpun.lock model as `pp`. Local and
registry packages are materialized as ordinary path dependencies before the
compiler sees them, so there is one resolver/build graph rather than a second
compiler path.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import signing

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
DEFAULT_REGISTRY = "http://127.0.0.1:8765"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")


def cache_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "PunPun" / "PPX"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ppx"


def state_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "PunPun" / "PPX"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ppx"


def package_search_paths() -> list[Path]:
    """Candidate locations of the first-party package set, best match first.

    The packages ship with the language, not with PPX, so they are found
    through an explicit override, a PunPun checkout, or an installed
    toolchain rather than relative to this script.
    """
    candidates: list[Path] = []
    override = os.environ.get("PUNPUN_PACKAGES")
    if override:
        candidates.append(Path(override))
    punpun_root = os.environ.get("PUNPUN_ROOT")
    if punpun_root:
        candidates.append(Path(punpun_root) / "packages")
    # A `punpun` checkout sitting beside this one, the usual development layout.
    candidates.append(ROOT.parent / "punpun" / "packages")
    # An installed toolchain, matching the language repository's `make install`.
    pp = shutil.which("pp") or shutil.which("punpun")
    if pp:
        prefix = Path(pp).resolve().parent.parent
        candidates.append(prefix / "lib" / "punpun" / "packages")
    candidates.append(Path("/usr/local/lib/punpun/packages"))
    return candidates


def bundled_packages() -> Path:
    for candidate in package_search_paths():
        if candidate.is_dir():
            return candidate
    return package_search_paths()[0]


def registry_url() -> str:
    value = os.environ.get("PPX_REGISTRY", DEFAULT_REGISTRY).rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"ppx: invalid registry URL: {value}")
    host = (parsed.hostname or "").lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not loopback and os.environ.get("PPX_ALLOW_INSECURE_REGISTRY") != "1":
        raise SystemExit(
            "ppx: refusing insecure non-loopback registry; use https or set "
            "PPX_ALLOW_INSECURE_REGISTRY=1 for an explicitly trusted development registry"
        )
    return value


def pp_command() -> str:
    override = os.environ.get("PUNPUN_PP")
    if override:
        return override
    found = shutil.which("pp")
    if found:
        return found
    sibling = ROOT.parent / "punpun" / "pp"
    return str(sibling) if sibling.is_file() else "pp"


def run_pp(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([pp_command(), *args], text=True, check=check)


def api(path: str, *, method: str = "GET", payload=None, token: str | None = None, timeout: float = 10.0) -> bytes:
    data = None
    headers = {"Accept": "application/json", "User-Agent": f"PPX/{VERSION}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(registry_url() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except Exception:
            pass
        raise SystemExit(f"ppx: registry error {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"ppx: cannot reach registry {registry_url()}: {exc.reason}")


def api_json(path: str, **kwargs):
    return json.loads(api(path, **kwargs).decode("utf-8"))


def parse_semver(value: str):
    match = SEMVER_RE.fullmatch(value)
    if not match:
        raise ValueError(f"invalid semantic version: {value}")
    major, minor, patch = map(int, match.group(1, 2, 3))
    prerelease = match.group(4)
    return major, minor, patch, prerelease


def version_key(value: str):
    major, minor, patch, prerelease = parse_semver(value)
    # Stable versions sort after prereleases of the same numeric tuple.
    return major, minor, patch, 1 if prerelease is None else 0, prerelease or ""


def satisfies(version: str, requirement: str | None) -> bool:
    parsed_version = parse_semver(version)
    actual = parsed_version[:3]
    req = (requirement or "*").strip()
    # Stable-by-default resolution: prereleases are selected only when the
    # requirement explicitly names a prerelease.
    if parsed_version[3] is not None and "-" not in req:
        return False
    if req in ("", "*", "latest"):
        return True
    if SEMVER_RE.fullmatch(req):
        return version == req
    if req.startswith("^"):
        base = parse_semver(req[1:])[:3]
        upper = (base[0] + 1, 0, 0) if base[0] else ((0, base[1] + 1, 0) if base[1] else (0, 0, base[2] + 1))
        return base <= actual < upper
    if req.startswith("~"):
        base = parse_semver(req[1:])[:3]
        return base <= actual < (base[0], base[1] + 1, 0)
    for piece in [p.strip() for p in req.split(",") if p.strip()]:
        op = next((x for x in (">=", "<=", ">", "<", "=") if piece.startswith(x)), None)
        if not op:
            return False
        rhs = parse_semver(piece[len(op):].strip())[:3]
        if op == ">=" and not actual >= rhs: return False
        if op == "<=" and not actual <= rhs: return False
        if op == ">" and not actual > rhs: return False
        if op == "<" and not actual < rhs: return False
        if op == "=" and not actual == rhs: return False
    return True


def choose_version(metadata: dict, requirement: str | None) -> dict:
    candidates = [v for v in metadata.get("versions", []) if not v.get("yanked") and satisfies(v["version"], requirement)]
    if not candidates:
        raise SystemExit(f"ppx: no version of {metadata.get('name', 'package')} satisfies {requirement or 'latest'}")
    return max(candidates, key=lambda v: version_key(v["version"]))


def safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts or "\x00" in name:
                raise SystemExit(f"ppx: unsafe archive path: {name!r}")
            target = (destination / name).resolve()
            if root != target and root not in target.parents:
                raise SystemExit(f"ppx: archive path escapes destination: {name!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def token_file() -> Path:
    return state_root() / "token"


def load_token(required: bool = False) -> str | None:
    path = token_file()
    if path.is_file():
        return path.read_text(encoding="utf-8").strip() or None
    if required:
        raise SystemExit("ppx: not logged in; run `ppx login`")
    return None


def valid_requirement(requirement: str) -> bool:
    req = requirement.strip()
    if req in ("", "*", "latest"):
        return True
    if SEMVER_RE.fullmatch(req):
        return True
    if req.startswith(("^", "~")):
        return bool(SEMVER_RE.fullmatch(req[1:]))
    pieces = [piece.strip() for piece in req.split(",") if piece.strip()]
    if not pieces:
        return False
    for piece in pieces:
        op = next((candidate for candidate in (">=", "<=", ">", "<", "=") if piece.startswith(candidate)), None)
        if not op or not SEMVER_RE.fullmatch(piece[len(op):].strip()):
            return False
    return True


def _safe_relative_file(root: Path, value: str, field: str) -> str:
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts:
        raise SystemExit(f"ppx: package {field} must stay inside the package directory")
    target = (root / rel).resolve()
    if root.resolve() != target and root.resolve() not in target.parents:
        raise SystemExit(f"ppx: package {field} escapes the package directory")
    if not target.is_file():
        raise SystemExit(f"ppx: package {field} file does not exist: {value}")
    return rel.as_posix()


def manifest_publish_metadata(root: Path) -> dict:
    path = root / "Punpun.toml"
    if not path.is_file():
        raise SystemExit("ppx: current directory has no Punpun.toml")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SystemExit(f"ppx: invalid Punpun.toml: {exc}")
    package = document.get("package")
    if not isinstance(package, dict):
        raise SystemExit("ppx: manifest requires a [package] table")
    name = str(package.get("name", "")).strip()
    version = str(package.get("version", "")).strip()
    description = str(package.get("description", "")).strip()
    if not NAME_RE.fullmatch(name):
        raise SystemExit("ppx: package name must start with a letter and contain only letters, digits, _ or -")
    try:
        parse_semver(version)
    except ValueError as exc:
        raise SystemExit(f"ppx: {exc}")
    if len(description) > 1000:
        raise SystemExit("ppx: package description exceeds 1000 characters")

    metadata = {"name": name, "version": version, "description": description}
    license_name = str(package.get("license", "")).strip()
    if license_name:
        if len(license_name) > 128:
            raise SystemExit("ppx: package license metadata is too long")
        metadata["license"] = license_name
    for field in ("repository", "homepage"):
        value = str(package.get(field, "")).strip()
        if value:
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise SystemExit(f"ppx: package {field} must be an http(s) URL")
            metadata[field] = value
    readme = str(package.get("readme", "")).strip()
    if readme:
        metadata["readme"] = _safe_relative_file(root, readme, "readme")
    keywords = package.get("keywords", [])
    if keywords:
        if not isinstance(keywords, list) or len(keywords) > 16:
            raise SystemExit("ppx: package keywords must be a list with at most 16 entries")
        normalized = []
        for keyword in keywords:
            text = str(keyword).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_+.-]{0,31}", text):
                raise SystemExit(f"ppx: invalid package keyword: {text!r}")
            normalized.append(text)
        metadata["keywords"] = normalized

    dependencies = {}
    raw_dependencies = document.get("dependencies") or {}
    if not isinstance(raw_dependencies, dict):
        raise SystemExit("ppx: [dependencies] must be a table")
    if len(raw_dependencies) > 128:
        raise SystemExit("ppx: package declares too many dependencies")
    for dep_name, spec in raw_dependencies.items():
        if not NAME_RE.fullmatch(str(dep_name)):
            raise SystemExit(f"ppx: invalid dependency name: {dep_name!r}")
        if isinstance(spec, str):
            requirement = spec.strip()
        elif isinstance(spec, dict):
            if "path" in spec:
                raise SystemExit(f"ppx: dependency {dep_name!r} uses a local path; publishable packages require a registry version")
            requirement = str(spec.get("version", "")).strip()
        else:
            raise SystemExit(f"ppx: dependency {dep_name!r} must be a version string or {{ version = ... }}")
        if not valid_requirement(requirement):
            raise SystemExit(f"ppx: invalid version requirement for {dep_name}: {requirement!r}")
        dependencies[str(dep_name)] = requirement or "*"
    metadata["dependencies"] = dependencies
    return metadata


def manifest_name_version(root: Path) -> tuple[str, str, str]:
    metadata = manifest_publish_metadata(root)
    return metadata["name"], metadata["version"], metadata.get("description", "")

def package_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in {".git", ".punpun", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"} for part in rel.parts):
            continue
        if rel.name in {".DS_Store", "Thumbs.db", "desktop.ini", "PPX-MANIFEST.json"} or rel.suffix in {".pyc", ".tmp"}:
            continue
        if path.stat().st_size > 16 * 1024 * 1024:
            raise SystemExit(f"ppx: refusing unusually large package file: {rel}")
        files.append(path)
    return files


def package_integrity_manifest(root: Path, files: list[Path]) -> bytes:
    payload = {
        "format": 1,
        "files": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
            for path in files
        ],
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def verify_package_tree(root: Path, *, require_manifest: bool = True) -> dict:
    manifest_path = root / "PPX-MANIFEST.json"
    if not manifest_path.is_file():
        if require_manifest:
            raise SystemExit("ppx: package archive has no PPX-MANIFEST.json integrity manifest")
        return {"format": 0, "files": []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ppx: invalid package integrity manifest: {exc}")
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
        raise SystemExit("ppx: unsupported package integrity manifest")
    declared: set[str] = set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise SystemExit("ppx: malformed package integrity entry")
        name = str(item.get("path", ""))
        rel = Path(name)
        if not name or rel.is_absolute() or ".." in rel.parts or name == "PPX-MANIFEST.json":
            raise SystemExit(f"ppx: unsafe integrity-manifest path: {name!r}")
        target = root / rel
        if not target.is_file():
            raise SystemExit(f"ppx: package integrity file is missing: {name}")
        content = target.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != item.get("sha256") or len(content) != item.get("size"):
            raise SystemExit(f"ppx: package integrity mismatch: {name}")
        declared.add(rel.as_posix())
    actual_files = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "PPX-MANIFEST.json"
    }
    undeclared = sorted(actual_files - declared)
    if undeclared:
        raise SystemExit("ppx: package contains undeclared file(s): " + ", ".join(undeclared[:8]))
    return manifest


def create_package_archive(root: Path) -> tuple[bytes, str]:
    files = package_source_files(root)
    manifest_bytes = package_integrity_manifest(root, files)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            def write_entry(name: str, content: bytes) -> None:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100644 & 0xFFFF) << 16
                info.create_system = 3
                zf.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            for path in files:
                write_entry(path.relative_to(root).as_posix(), path.read_bytes())
            write_entry("PPX-MANIFEST.json", manifest_bytes)
        content = temp_path.read_bytes()
        if len(content) > 32 * 1024 * 1024:
            raise SystemExit("ppx: package archive exceeds 32 MiB beta registry limit")
        return content, hashlib.sha256(content).hexdigest()
    finally:
        temp_path.unlink(missing_ok=True)


def bundled_matches(query: str):
    root = bundled_packages()
    if not root.is_dir(): return []
    result = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / "Punpun.toml").is_file(): continue
        if query.lower() not in child.name.lower(): continue
        try:
            name, version, description = manifest_name_version(child)
            result.append({"name": name, "latest": version, "description": description, "source": "bundled"})
        except SystemExit:
            continue
    return result


def resolve_remote(name: str, requirement: str | None, resolving=None) -> Path:
    resolving = set() if resolving is None else resolving
    key = (name, requirement or "latest")
    if key in resolving:
        raise SystemExit(f"ppx: dependency cycle while resolving {name}")
    resolving.add(key)
    meta = api_json("/api/v1/packages/" + urllib.parse.quote(name))
    selected = choose_version(meta, requirement)
    version = selected["version"]
    package_dir = cache_root() / "packages" / name / version
    marker = package_dir / ".ppx-checksum"
    checksum = selected["checksum"]
    if package_dir.is_dir() and marker.is_file() and marker.read_text().strip() == checksum:
        resolving.remove(key)
        return package_dir
    archive_bytes = api("/api/v1/packages/%s/%s/download" % (urllib.parse.quote(name), urllib.parse.quote(version)))
    actual = hashlib.sha256(archive_bytes).hexdigest()
    if actual != checksum:
        raise SystemExit(f"ppx: checksum mismatch for {name} {version}: expected {checksum}, got {actual}")
    package_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = package_dir.with_name(package_dir.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    archive = staging / "package.zip"
    archive.write_bytes(archive_bytes)
    safe_extract_zip(archive, staging / "src")
    archive.unlink()
    source_root = staging / "src"
    verify_package_tree(source_root, require_manifest=False)
    manifest = source_root / "Punpun.toml"
    if not manifest.is_file():
        raise SystemExit(f"ppx: {name} {version} archive has no Punpun.toml")
    # Resolve declared registry dependencies and materialize them as the path
    # dependencies understood by the single compiler package graph.
    deps = selected.get("dependencies") or {}
    if deps:
        text = manifest.read_text(encoding="utf-8")
        lines = text.splitlines()
        out, section = [], ""
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1].strip()
            if section == "dependencies" and "=" in raw:
                dep_name = raw.split("=", 1)[0].strip()
                if dep_name in deps:
                    dep_dir = resolve_remote(dep_name, deps[dep_name], resolving)
                    raw = f'{dep_name} = {{ path = "{dep_dir.as_posix()}" }}'
            out.append(raw)
        manifest.write_text("\n".join(out) + "\n", encoding="utf-8")
    (staging / ".ppx-checksum").write_text(checksum + "\n", encoding="utf-8")
    shutil.rmtree(package_dir, ignore_errors=True)
    os.replace(staging, package_dir)
    # package sources are under src/ because staging also stores PPX metadata.
    final_root = package_dir / "src"
    resolving.remove(key)
    return final_root


def cmd_search(args):
    rows = bundled_matches(args.query)
    try:
        remote = api_json("/api/v1/search?q=" + urllib.parse.quote(args.query)).get("packages", [])
        for row in remote:
            row = dict(row); row["source"] = "registry"; rows.append(row)
    except SystemExit:
        if not rows: raise
    if not rows:
        print("No packages found.")
        return
    seen = set()
    for row in rows:
        key = (row.get("name"), row.get("source"))
        if key in seen: continue
        seen.add(key)
        print(f"{row.get('name','?'):<24} {row.get('latest','?'):<14} {row.get('source',''):<9} {row.get('description','')}")


def cmd_info(args):
    local = bundled_packages() / args.name
    if local.is_dir():
        name, version, description = manifest_name_version(local)
        print(f"name: {name}\nversion: {version}\nsource: bundled\npath: {local}\ndescription: {description}")
        return
    meta = api_json("/api/v1/packages/" + urllib.parse.quote(args.name))
    print(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Dependency graph
#
# A `[dependencies]` entry is either a registry requirement ("^1.2.0") or a
# local path. PPX owns this table: `pp` only creates the initial manifest and
# the compiler never reads it.
#
# The table is edited textually rather than by re-serializing the document,
# because Python ships a TOML reader but no writer, and a round trip through a
# hand-rolled writer would drop the comments and field order of a file people
# maintain by hand.
# ---------------------------------------------------------------------------

DEP_LINE_RE = re.compile(r"^\s*(?:(?P<quoted>\"[^\"]+\"|'[^']+')|(?P<bare>[A-Za-z0-9_-]+))\s*=")


def project_root() -> Path:
    root = Path.cwd()
    if not (root / "Punpun.toml").is_file():
        raise SystemExit("ppx: no Punpun.toml in this directory; run `pp init` first")
    return root


def load_manifest(root: Path) -> dict:
    try:
        return tomllib.loads((root / "Punpun.toml").read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SystemExit(f"ppx: invalid Punpun.toml: {exc}")


def dependency_source(spec) -> tuple[str, str]:
    """Classify a dependency specification as ('path'|'registry', value)."""
    if isinstance(spec, dict):
        if spec.get("path"):
            return "path", str(spec["path"])
        return "registry", str(spec.get("version", "*"))
    text = str(spec)
    if text.startswith(".") or text.startswith("/") or "/" in text or "\\" in text:
        return "path", text
    return "registry", text


def _dependency_key(line: str) -> str | None:
    match = DEP_LINE_RE.match(line)
    if not match:
        return None
    quoted = match.group("quoted")
    return quoted[1:-1] if quoted else match.group("bare")


def write_dependency(root: Path, name: str, literal: str | None) -> None:
    """Insert, replace, or (literal=None) delete one `[dependencies]` entry."""
    path = root / "Punpun.toml"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[dependencies.") and stripped.rstrip("]").endswith("." + name):
            raise SystemExit(
                f"ppx: {name} is declared as a [dependencies.{name}] table; edit Punpun.toml by hand"
            )

    start = next((i + 1 for i, line in enumerate(lines) if line.strip() == "[dependencies]"), None)
    if start is None:
        if literal is None:
            raise SystemExit(f"ppx: {name} is not a dependency of this package")
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[dependencies]", f"{name} = {literal}"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].lstrip().startswith("["):
            end = index
            break

    existing = {_dependency_key(lines[i]): i for i in range(start, end)}
    existing.pop(None, None)

    if name in existing:
        if literal is None:
            del lines[existing[name]]
        else:
            lines[existing[name]] = f"{name} = {literal}"
    elif literal is None:
        raise SystemExit(f"ppx: {name} is not a dependency of this package")
    else:
        # Keep entries sorted so manifests stay diff-friendly.
        insert = end
        for key, index in sorted(existing.items(), key=lambda item: item[1]):
            if key > name:
                insert = index
                break
        else:
            while insert > start and not lines[insert - 1].strip():
                insert -= 1
        lines.insert(insert, f"{name} = {literal}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def display_path(root: Path, target: Path) -> str:
    """Render `target` relative to the project, unless absolute reads better."""
    try:
        relative = os.path.relpath(target, root)
    except ValueError:  # a different drive on Windows
        return str(target)
    return relative if len(relative) <= len(str(target)) else str(target)


def dependency_literal(root: Path, target: Path) -> str:
    value = display_path(root, target).replace("\\", "\\\\").replace('"', '\\"')
    return '{ path = "%s" }' % value


def resolve_dependency(root: Path, name: str, spec) -> tuple[str, Path | None, str]:
    """Return (kind, materialized path, display) for one dependency."""
    kind, value = dependency_source(spec)
    if kind == "path":
        target = (root / value).resolve()
        return "path", (target if target.is_dir() else None), value
    bundled = bundled_packages() / name
    if bundled.is_dir():
        return "bundled", bundled.resolve(), value
    return "registry", None, value


def write_lock(root: Path) -> Path:
    """Write Punpun.lock: a deterministic record of the resolved graph."""
    document = load_manifest(root)
    package = document.get("package") or {}
    entries = []
    for name, spec in sorted((document.get("dependencies") or {}).items()):
        kind, target, value = resolve_dependency(root, name, spec)
        entry = {"name": name, "source": kind, "requirement": value}
        if target is not None:
            entry["path"] = display_path(root, target)
            dep_manifest = target / "Punpun.toml"
            if dep_manifest.is_file():
                try:
                    dep_package = tomllib.loads(dep_manifest.read_text(encoding="utf-8")).get("package") or {}
                    if dep_package.get("version"):
                        entry["version"] = str(dep_package["version"])
                except tomllib.TOMLDecodeError:
                    pass
        entries.append(entry)
    lock = {
        "format": 1,
        "package": {"name": str(package.get("name", "")), "version": str(package.get("version", ""))},
        "packages": entries,
    }
    path = root / "Punpun.lock"
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def dependency_paths(root: Path) -> list[Path]:
    """Return every materialized dependency root in deterministic order."""
    document = load_manifest(root)
    paths: list[Path] = []
    seen: set[Path] = set()
    missing: list[str] = []
    for name, spec in sorted((document.get("dependencies") or {}).items()):
        kind, target, value = resolve_dependency(root, name, spec)
        if kind == "registry":
            missing.append(f"{name} {value} is not materialized; run `ppx update`")
            continue
        if target is None:
            missing.append(f"{name} path is missing: {value}")
            continue
        resolved = target.resolve()
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    if missing:
        raise SystemExit("ppx: " + "; ".join(missing))
    return paths


def cmd_paths(_args):
    """Machine-readable bridge used by `pp` to populate compiler module roots."""
    for path in dependency_paths(project_root()):
        print(path)


def _cached_identity(target: Path | None) -> tuple[str, str] | None:
    if target is None:
        return None
    try:
        relative = target.resolve().relative_to((cache_root() / "packages").resolve())
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) >= 3 and parts[2] == "src":
        return parts[0], parts[1]
    return None


def cmd_outdated(args):
    root = project_root()
    dependencies = load_manifest(root).get("dependencies") or {}
    rows = []
    for name, spec in sorted(dependencies.items()):
        kind, target, requirement = resolve_dependency(root, name, spec)
        cached = _cached_identity(target)
        if kind in {"path", "bundled"} and cached is None:
            current = "local" if kind == "path" else "bundled"
            if target is not None and (target / "Punpun.toml").is_file():
                try:
                    package = tomllib.loads((target / "Punpun.toml").read_text(encoding="utf-8")).get("package") or {}
                    current = str(package.get("version") or current)
                except tomllib.TOMLDecodeError:
                    pass
            rows.append({"name": name, "current": current, "latest": current, "status": kind})
            continue

        current = cached[1] if cached else "not installed"
        requested = None if cached else (requirement if requirement != "*" else None)
        try:
            metadata = api_json("/api/v1/packages/" + urllib.parse.quote(name))
            latest = choose_version(metadata, requested)["version"]
            status = "current" if current == latest else "outdated"
        except SystemExit as exc:
            latest = "unknown"
            status = str(exc)
        rows.append({"name": name, "current": current, "latest": latest, "status": status})

    if args.json:
        print(json.dumps({"packages": rows}, indent=2, sort_keys=True))
        return
    if not rows:
        print("No dependencies.")
        return
    for row in rows:
        print(f"{row['name']:<24} {row['current']:<16} {row['latest']:<16} {row['status']}")


def cmd_add(args):
    if not NAME_RE.fullmatch(args.name): raise SystemExit("ppx: invalid package name")
    root = project_root()
    if args.path:
        source = Path(args.path).expanduser().resolve()
        if not source.is_dir():
            raise SystemExit(f"ppx: path dependency not found: {source}")
    else:
        bundled = bundled_packages() / args.name
        source = bundled.resolve() if bundled.is_dir() else resolve_remote(args.name, args.version)
    write_dependency(root, args.name, dependency_literal(root, source))
    write_lock(root)
    print(f"PPX added {args.name} from {source}")


def cmd_remove(args):
    root = project_root()
    write_dependency(root, args.name, None)
    write_lock(root)
    print(f"PPX removed {args.name}")


def cmd_tree(_args):
    root = project_root()
    document = load_manifest(root)
    package = document.get("package") or {}
    print(f"{package.get('name', root.name)} {package.get('version', '')}".strip())

    def walk(base: Path, dependencies: dict, prefix: str, seen: set[Path]) -> None:
        items = sorted(dependencies.items())
        for index, (name, spec) in enumerate(items):
            last = index == len(items) - 1
            kind, target, value = resolve_dependency(base, name, spec)
            if kind == "registry":
                label = f"{name} {value} (registry; run `ppx install {name}`)"
            elif target is None:
                label = f"{name} {value} (missing)"
            else:
                label = f"{name} ({kind})"
            print(f"{prefix}{'`-- ' if last else '|-- '}{label}")
            if target is None:
                continue
            resolved = target.resolve()
            if resolved in seen:
                print(f"{prefix}{'    ' if last else '|   '}`-- (already shown)")
                continue
            child = target / "Punpun.toml"
            if not child.is_file():
                continue
            try:
                nested = tomllib.loads(child.read_text(encoding="utf-8")).get("dependencies") or {}
            except tomllib.TOMLDecodeError:
                continue
            if nested:
                walk(target, nested, prefix + ("    " if last else "|   "), seen | {resolved})

    dependencies = document.get("dependencies") or {}
    if not dependencies:
        print("(no dependencies)")
        return
    walk(root, dependencies, "", {root.resolve()})


def cmd_update(_args):
    root = project_root()
    document = load_manifest(root)
    dependencies = document.get("dependencies") or {}
    missing = []
    for name, spec in sorted(dependencies.items()):
        kind, target, value = resolve_dependency(root, name, spec)
        if kind == "registry":
            try:
                source = resolve_remote(name, value if value != "*" else None)
            except SystemExit as exc:
                missing.append(f"{name}: {exc}")
                continue
            write_dependency(root, name, dependency_literal(root, source))
            print(f"updated {name} -> {source}")
        elif target is None:
            missing.append(f"{name}: path dependency is missing: {value}")
        else:
            print(f"ok      {name} -> {target}")
    lock = write_lock(root)
    if missing:
        for item in missing:
            print(f"ppx: {item}", file=sys.stderr)
        raise SystemExit("ppx: some dependencies could not be resolved")
    print(f"wrote {lock.name} ({len(dependencies)} dependenc{'y' if len(dependencies) == 1 else 'ies'})")


def print_publish_plan(metadata: dict, content: bytes, checksum: str) -> None:
    print(f"package:      {metadata['name']} {metadata['version']}")
    print(f"description:  {metadata.get('description') or '(none)'}")
    print(f"archive:      {len(content)} bytes")
    print(f"sha256:       {checksum}")
    deps = metadata.get("dependencies") or {}
    print(f"dependencies: {len(deps)}")
    for name in sorted(deps):
        print(f"  {name} {deps[name]}")


def cmd_publish(args):
    root = Path.cwd()
    metadata = manifest_publish_metadata(root)
    content, checksum = create_package_archive(root)
    print_publish_plan(metadata, content, checksum)
    if args.dry_run:
        print("dry run: package validated; nothing uploaded")
        return
    payload = dict(metadata)
    payload.update({
        "checksum": checksum,
        "archive_b64": base64.b64encode(content).decode("ascii"),
    })
    response = api_json("/api/v1/packages", method="POST", payload=payload, token=load_token(True), timeout=30)
    print(f"published {metadata['name']} {metadata['version']} ({response.get('checksum', checksum)})")


def _password(args, prompt: str) -> str:
    explicit = getattr(args, "password", None)
    if explicit:
        return explicit
    env = os.environ.get("PPX_PASSWORD")
    if env:
        return env
    return getpass.getpass(prompt)


def cmd_register(args):
    password = _password(args, "New PPX password: ")
    confirmation = password if getattr(args, "password", None) or os.environ.get("PPX_PASSWORD") else getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("ppx: passwords do not match")
    api_json("/api/v1/register", method="POST", payload={"username": args.username, "password": password})
    print(f"registered {args.username}; run `ppx login {args.username}`")


def cmd_login(args):
    password = _password(args, "PPX password: ")
    payload = {"username": args.username, "password": password}
    response = api_json("/api/v1/login", method="POST", payload=payload)
    state_root().mkdir(parents=True, exist_ok=True)
    token_file().write_text(response["token"] + "\n", encoding="utf-8")
    try: os.chmod(token_file(), 0o600)
    except OSError: pass
    print(f"logged in as {args.username}")


def cmd_download(args):
    if not NAME_RE.fullmatch(args.name):
        raise SystemExit("ppx: invalid package name")
    metadata = api_json("/api/v1/packages/" + urllib.parse.quote(args.name))
    selected = choose_version(metadata, args.version)
    version = selected["version"]
    content = api("/api/v1/packages/%s/%s/download" % (urllib.parse.quote(args.name), urllib.parse.quote(version)))
    checksum = hashlib.sha256(content).hexdigest()
    if checksum != selected["checksum"]:
        raise SystemExit(f"ppx: checksum mismatch for {args.name} {version}")
    with tempfile.TemporaryDirectory(prefix="ppx-download-verify-") as td:
        archive = Path(td) / "package.zip"
        archive.write_bytes(content)
        extracted = Path(td) / "src"
        safe_extract_zip(archive, extracted)
        verify_package_tree(extracted, require_manifest=False)
    output = Path(args.output) if args.output else Path(f"{args.name}-{version}.zip")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    print(f"downloaded {args.name} {version} -> {output} ({checksum})")


def cmd_install(args):
    if not args.name:
        return cmd_update(args)
    return cmd_add(args)

def cmd_logout(_args):
    token = load_token(False)
    if token:
        try: api_json("/api/v1/logout", method="POST", payload={}, token=token)
        except SystemExit: pass
    token_file().unlink(missing_ok=True)
    print("logged out")


def cmd_yank(args):
    api_json(f"/api/v1/packages/{urllib.parse.quote(args.name)}/{urllib.parse.quote(args.version)}/yank",
             method="POST", payload={}, token=load_token(True))
    print(f"yanked {args.name} {args.version}")


def cmd_verify(args):
    archive = Path(args.archive).resolve()
    if not archive.is_file(): raise SystemExit(f"ppx: archive not found: {archive}")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "package"; safe_extract_zip(archive, root); manifest = verify_package_tree(root)
    signature=Path(args.signature).resolve() if getattr(args,"signature",None) else None
    if getattr(args,"trusted",False):
        signature=signature or Path(str(archive)+".sig")
        if not signature.is_file(): raise SystemExit("ppx: --trusted requires a detached .sig file")
        keys=sorted(trust_root().glob("*.pem"))
        if not keys: raise SystemExit("ppx: no trusted PPX signing keys; use `ppx trust add <public.pem>`")
        key=verify_detached_signature(archive,signature,keys); print(f"signature: trusted via {key.name}")
    elif signature:
        if not getattr(args,"public_key",None): raise SystemExit("ppx: --signature requires --public-key (or use --trusted)")
        key=verify_detached_signature(archive,signature,[Path(args.public_key).resolve()]); print(f"signature: valid via {key}")
    print(f"verified {archive.name}: {len(manifest['files'])} file(s), sha256={hashlib.sha256(archive.read_bytes()).hexdigest()}")

def _injection_files(root: Path) -> list[str]:
    result = []
    for path in sorted(root.rglob("*.pp")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "@inject->" in text:
            result.append(path.relative_to(root).as_posix())
    return result


def cmd_audit(args):
    root = Path.cwd()
    manifest = root / "Punpun.toml"
    if not manifest.is_file():
        raise SystemExit("ppx: audit requires a Punpun.toml project")
    document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    dependencies = document.get("dependencies") or {}
    findings: list[str] = []
    checked = 0
    for name, spec in sorted(dependencies.items()):
        path_value = None
        if isinstance(spec, dict):
            path_value = spec.get("path")
        elif isinstance(spec, str) and (spec.startswith(".") or "/" in spec or "\\" in spec):
            path_value = spec
        if not path_value:
            findings.append(f"{name}: registry dependency is not materialized as a local audited path")
            continue
        dep_root = (root / str(path_value)).resolve()
        if not dep_root.is_dir():
            findings.append(f"{name}: dependency path is missing: {dep_root}")
            continue
        checked += 1
        injections = _injection_files(dep_root)
        for item in injections:
            findings.append(f"{name}: native injection in {item}")
    print(f"PPX audit: {checked} local dependency path(s) checked")
    if findings:
        for item in findings:
            print("warning: " + item)
        if args.deny_injection and any("native injection" in item for item in findings):
            raise SystemExit("ppx: audit failed because --deny-injection was requested")
    else:
        print("PPX audit: no dependency findings")



def trust_root() -> Path:
    root = state_root() / "trust"
    root.mkdir(parents=True, exist_ok=True)
    return root

def cmd_sign(args):
    archive=Path(args.archive).resolve()
    if not archive.is_file(): raise SystemExit(f"ppx: archive not found: {archive}")
    output=Path(args.output or (str(archive)+".sig")); output.write_bytes(signing.sign_bytes(Path(args.private_key),archive.read_bytes())); print(f"signed {archive.name} -> {output}")

def cmd_trust(args):
    if args.action=="list":
        keys=sorted(trust_root().glob("*.pem")); print("\n".join(k.name for k in keys) if keys else "no trusted PPX signing keys"); return
    if not args.key: raise SystemExit("ppx: trust add/remove requires a key argument")
    if args.action=="add":
        source=Path(args.key).resolve()
        if not source.is_file(): raise SystemExit("ppx: trusted key file not found")
        digest=hashlib.sha256(source.read_bytes()).hexdigest()[:16]; target=trust_root()/f"{digest}.pem"; shutil.copy2(source,target); print(f"trusted {target.name}"); return
    target=trust_root()/args.key
    if not target.is_file(): raise SystemExit(f"ppx: trusted key not found: {args.key}")
    target.unlink(); print(f"removed trust root {args.key}")

def verify_detached_signature(archive: Path, signature: Path, public_keys: list[Path]) -> Path:
    data=archive.read_bytes(); sig=signature.read_bytes()
    for key in public_keys:
        if signing.verify_bytes(key,data,sig): return key
    raise SystemExit("ppx: detached Ed25519 signature did not match any trusted key")

def cmd_doctor(_args):
    print(f"PPX {VERSION}")
    print(f"pp:       {pp_command()} {'OK' if Path(pp_command()).exists() or shutil.which(pp_command()) else 'MISSING (install the PunPun toolchain)'}")
    print(f"cache:    {cache_root()}")
    packages = bundled_packages()
    if packages.is_dir():
        print(f"bundled:  {packages} OK")
    else:
        print(f"bundled:  {packages} missing (set PUNPUN_ROOT or PUNPUN_PACKAGES to a PunPun checkout)")
    try:
        api_json("/api/v1/health", timeout=2)
        print(f"registry: {registry_url()} OK")
    except SystemExit:
        print(f"registry: {registry_url()} unavailable (optional for bundled/path packages)")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ppx", description="PunPunXPac package manager")
    p.add_argument("--version", action="version", version=f"ppx {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init").set_defaults(func=lambda a: run_pp("init"))
    add = sub.add_parser("add"); add.add_argument("name"); add.add_argument("version", nargs="?"); add.add_argument("--path"); add.set_defaults(func=cmd_add)
    install = sub.add_parser("install", help="install a registry package, or update the current graph when no name is supplied")
    install.add_argument("name", nargs="?"); install.add_argument("version", nargs="?"); install.add_argument("--path"); install.set_defaults(func=cmd_install)
    rem = sub.add_parser("remove"); rem.add_argument("name"); rem.set_defaults(func=cmd_remove)
    sub.add_parser("update").set_defaults(func=cmd_update)
    search = sub.add_parser("search"); search.add_argument("query", nargs="?", default=""); search.set_defaults(func=cmd_search)
    info = sub.add_parser("info"); info.add_argument("name"); info.set_defaults(func=cmd_info)
    download = sub.add_parser("download"); download.add_argument("name"); download.add_argument("version", nargs="?"); download.add_argument("-o", "--output"); download.set_defaults(func=cmd_download)
    sub.add_parser("tree").set_defaults(func=cmd_tree)
    sub.add_parser("paths", help=argparse.SUPPRESS).set_defaults(func=cmd_paths)
    outdated = sub.add_parser("outdated", help="show current and latest dependency versions")
    outdated.add_argument("--json", action="store_true")
    outdated.set_defaults(func=cmd_outdated)
    for command in ("publish", "upload"):
        pub = sub.add_parser(command, help="validate and publish the current package")
        pub.add_argument("--dry-run", action="store_true", help="build and validate the package archive without uploading")
        pub.set_defaults(func=cmd_publish)
    yank = sub.add_parser("yank"); yank.add_argument("name"); yank.add_argument("version"); yank.set_defaults(func=cmd_yank)
    register = sub.add_parser("register"); register.add_argument("username"); register.add_argument("--password", help=argparse.SUPPRESS); register.set_defaults(func=cmd_register)
    login = sub.add_parser("login"); login.add_argument("username"); login.add_argument("--password", help=argparse.SUPPRESS); login.set_defaults(func=cmd_login)
    sub.add_parser("logout").set_defaults(func=cmd_logout)
    cache = sub.add_parser("cache"); cache.add_argument("action", choices=["path", "clean"], nargs="?", default="path"); cache.set_defaults(func=lambda a: (shutil.rmtree(cache_root(), ignore_errors=True), print("PPX cache cleared")) if a.action == "clean" else print(cache_root()))
    verify = sub.add_parser("verify", help="verify package integrity and optional Ed25519 publisher signature")
    verify.add_argument("archive"); verify.add_argument("--signature"); verify.add_argument("--public-key"); verify.add_argument("--trusted", action="store_true"); verify.set_defaults(func=cmd_verify)
    sign = sub.add_parser("sign", help="create a detached Ed25519 package signature")
    sign.add_argument("archive"); sign.add_argument("--private-key", required=True); sign.add_argument("-o", "--output"); sign.set_defaults(func=cmd_sign)
    trust = sub.add_parser("trust", help="manage trusted PPX public signing keys")
    trust.add_argument("action", choices=["add","remove","list"]); trust.add_argument("key", nargs="?"); trust.set_defaults(func=cmd_trust)
    audit = sub.add_parser("audit", help="audit local dependency sources and native injection exposure")
    audit.add_argument("--deny-injection", action="store_true"); audit.set_defaults(func=cmd_audit)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    return p


def main() -> int:
    args = parser().parse_args()
    result = args.func(args)
    return result.returncode if isinstance(result, subprocess.CompletedProcess) else 0


if __name__ == "__main__":
    raise SystemExit(main())
