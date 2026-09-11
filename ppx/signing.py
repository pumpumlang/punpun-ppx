#!/usr/bin/env python3
"""Ed25519 detached-signature primitives for PPX package archives.

PPX signs and verifies individual package archives. Release-bundle signing and
the `RELEASE_SIGNATURES.json` manifest format belong to the language
repository's release tooling and are deliberately not duplicated here.

Signing shells out to OpenSSL so PPX keeps its dependency-free install story;
no third-party crypto package is required.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile


def require_openssl() -> None:
    if not shutil.which("openssl"):
        raise SystemExit("ppx: package signing requires OpenSSL 3.x with Ed25519 support")


def sign_bytes(private_key: Path, data: bytes) -> bytes:
    """Return a detached Ed25519 signature over `data`."""
    require_openssl()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        message, signature = tmp / "message", tmp / "signature"
        message.write_bytes(data)
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(private_key),
             "-in", str(message), "-out", str(signature)],
            check=True,
        )
        return signature.read_bytes()


def verify_bytes(public_key: Path, data: bytes, signature: bytes) -> bool:
    """Return True when `signature` is a valid Ed25519 signature over `data`."""
    require_openssl()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        message, sig = tmp / "message", tmp / "signature"
        message.write_bytes(data)
        sig.write_bytes(signature)
        return subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(public_key),
             "-in", str(message), "-sigfile", str(sig)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
