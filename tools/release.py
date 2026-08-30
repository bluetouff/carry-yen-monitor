#!/usr/bin/env python3
"""Construit et verifie un artefact YCT a liste blanche, sans secret ni .git."""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "AUDIT.md",
    "LICENSE",
    "README.fr.md",
    "README.md",
    "RUNBOOK.md",
    "build_snapshot.py",
    "config/boj-policy.json",
    "config/source-calendars.json",
    "deploy/activate-release.sh",
    "deploy/yct-snapshot.service",
    "deploy/yct-snapshot.timer",
    "deploy/yct.l0g.fr.conf",
    "env.example",
    "tests/test_yct.py",
    "tools/release.py",
    "verify_live.py",
    "verify_snapshot.py",
    "web/app.css",
    "web/app.js",
    "web/en/index.html",
    "web/index.html",
    "yct_quality.py",
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"apiKey=[A-Za-z0-9_-]{16,}"),
)


def run_git(*args):
    return subprocess.check_output(("git", "-C", str(ROOT), *args), text=True).strip()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_file(path):
    body = path.read_bytes()
    for pattern in SECRET_PATTERNS:
        if pattern.search(body):
            raise RuntimeError("secret potentiel detecte dans %s" % path)


def write_manifest(release_dir):
    lines = []
    for relative in SOURCE_FILES:
        path = release_dir / relative
        lines.append("%s  %s" % (sha256(path), relative))
    (release_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(output):
    sha = run_git("rev-parse", "HEAD")
    if run_git("status", "--porcelain"):
        raise RuntimeError("le checkout Git doit etre propre pour construire une release")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ("yct-release-%s.tar.gz" % sha)
    with tempfile.TemporaryDirectory(prefix="yct-release-build-") as temporary:
        release_dir = Path(temporary) / ("yct-release-%s" % sha)
        release_dir.mkdir()
        for relative in SOURCE_FILES:
            source = ROOT / relative
            if not source.is_file() or source.is_symlink():
                raise RuntimeError("fichier de release absent ou non regulier : %s" % relative)
            scan_file(source)
            destination = release_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        (release_dir / "SOURCE_SHA").write_text(sha + "\n", encoding="ascii")
        write_manifest(release_dir)
        verify_dir(release_dir, sha)
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(release_dir, arcname=release_dir.name, recursive=True)
    print("[yct-artifact] %s" % archive)
    return archive


def _safe_relative(value):
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts and value not in ("", ".")


def verify_dir(directory, expected_sha):
    directory = directory.resolve()
    actual_sha = (directory / "SOURCE_SHA").read_text(encoding="ascii").strip()
    if actual_sha != expected_sha or not re.fullmatch(r"[0-9a-f]{40}", actual_sha):
        raise RuntimeError("SOURCE_SHA inattendu")
    manifest_path = directory / "SHA256SUMS"
    entries = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or not _safe_relative(relative):
            raise RuntimeError("ligne de manifeste invalide")
        if relative in entries:
            raise RuntimeError("fichier duplique dans le manifeste")
        entries[relative] = digest
    if set(entries) != set(SOURCE_FILES):
        raise RuntimeError("surface de release inattendue")
    actual_files = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*") if path.is_file()
    }
    if actual_files != set(SOURCE_FILES) | {"SOURCE_SHA", "SHA256SUMS"}:
        raise RuntimeError("fichiers non manifestes dans la release")
    for relative, expected_digest in entries.items():
        path = directory / relative
        if path.is_symlink() or sha256(path) != expected_digest:
            raise RuntimeError("empreinte invalide : %s" % relative)
        scan_file(path)
    print("[yct-artifact] OK sha=%s files=%d" % (actual_sha, len(entries)))


def verify_archive(archive, expected_sha):
    with tempfile.TemporaryDirectory(prefix="yct-release-verify-") as temporary:
        root = Path(temporary)
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            for member in members:
                if (not member.isfile() and not member.isdir()) or not _safe_relative(member.name):
                    raise RuntimeError("membre d'archive refuse : %s" % member.name)
            bundle.extractall(root, members=members)
        directories = [path for path in root.iterdir() if path.is_dir()]
        if len(directories) != 1:
            raise RuntimeError("racine d'archive invalide")
        verify_dir(directories[0], expected_sha)


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--output", type=Path, default=ROOT / "dist")
    directory_parser = subparsers.add_parser("verify-dir")
    directory_parser.add_argument("--directory", type=Path, required=True)
    directory_parser.add_argument("--expected-sha", required=True)
    archive_parser = subparsers.add_parser("verify-archive")
    archive_parser.add_argument("--archive", type=Path, required=True)
    archive_parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    if args.command == "build":
        build(args.output)
    elif args.command == "verify-dir":
        verify_dir(args.directory, args.expected_sha)
    else:
        verify_archive(args.archive, args.expected_sha)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print("[yct-artifact] ECHEC : %s" % exc, file=sys.stderr)
        sys.exit(1)
