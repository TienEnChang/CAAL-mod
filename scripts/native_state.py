#!/usr/bin/env python3
"""Create and restore the minimal encrypted state for native CAAL installs."""

from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"CAALBK01"
FORMAT_VERSION = 1
SALT_SIZE = 16
NONCE_SIZE = 12
KDF_LENGTH = 32
KDF_N = 2**15
KDF_R = 8
KDF_P = 1
KEYCHAIN_SERVICE = "com.coreworxlab.caal.native-backup"
KEYCHAIN_LABEL = "CAAL Native Backup Key"

STATE_FILES = {
    "native/config/settings.json": ".native/config/settings.json",
    "native/config/n8n-encryption-key": ".native/config/n8n-encryption-key",
    "native/data/conversations.sqlite3": ".native/data/conversations.sqlite3",
    "native/data/short_term_memory.json": ".native/data/short_term_memory.json",
    "native/data/n8n/database.sqlite": ".native/data/n8n/.n8n/database.sqlite",
    "project/prompt/custom.md": "prompt/custom.md",
    "project/mcp_servers.json": "mcp_servers.json",
}
SQLITE_ARCHIVES = {
    "native/data/conversations.sqlite3",
    "native/data/n8n/database.sqlite",
}
REQUIRED_SETTING_KEYS = {"n8n_enabled", "n8n_url", "n8n_token", "n8n_api_key"}


class StateError(RuntimeError):
    """A user-actionable backup or restore error."""


def _derive_key(password: bytes, salt: bytes) -> bytes:
    return Scrypt(
        salt=salt,
        length=KDF_LENGTH,
        n=KDF_N,
        r=KDF_R,
        p=KDF_P,
    ).derive(password)


def encrypt_payload(payload: bytes, password: bytes) -> bytes:
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    key = _derive_key(password, salt)
    header = MAGIC + salt + nonce
    return header + AESGCM(key).encrypt(nonce, payload, header)


def decrypt_payload(blob: bytes, password: bytes) -> bytes:
    header_size = len(MAGIC) + SALT_SIZE + NONCE_SIZE
    if len(blob) <= header_size or blob[: len(MAGIC)] != MAGIC:
        raise StateError("Not a CAAL native-state backup")
    salt_start = len(MAGIC)
    nonce_start = salt_start + SALT_SIZE
    header = blob[:header_size]
    salt = blob[salt_start:nonce_start]
    nonce = blob[nonce_start:header_size]
    try:
        return AESGCM(_derive_key(password, salt)).decrypt(
            nonce, blob[header_size:], header
        )
    except Exception as exc:
        raise StateError("Incorrect passphrase or damaged backup") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sqlite_snapshot(path: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="caal-sqlite-") as temp_dir:
        snapshot = Path(temp_dir) / path.name
        source_uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(snapshot) as target:
                source.backup(target)
        return snapshot.read_bytes()


def _collect_state(project: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for archive_name, relative_path in STATE_FILES.items():
        path = project / relative_path
        if not path.is_file():
            continue
        files[archive_name] = (
            _sqlite_snapshot(path)
            if archive_name in SQLITE_ARCHIVES
            else path.read_bytes()
        )

    # Greetings are editable user state even though defaults are tracked by Git.
    for path in sorted((project / "prompt").glob("*/greetings.txt")):
        language = path.parent.name
        files[f"project/prompt/{language}/greetings.txt"] = path.read_bytes()

    _validate_state(files)
    return files


def _validate_state(files: dict[str, bytes]) -> None:
    settings_name = "native/config/settings.json"
    if settings_name not in files:
        raise StateError("Missing .native/config/settings.json; start CAAL once first")
    try:
        settings = json.loads(files[settings_name])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StateError("Runtime settings.json is invalid") from exc
    missing = REQUIRED_SETTING_KEYS - settings.keys()
    if missing:
        raise StateError(f"Runtime settings.json is missing: {', '.join(sorted(missing))}")

    n8n_db = "native/data/n8n/database.sqlite"
    n8n_key = "native/config/n8n-encryption-key"
    if n8n_db in files and n8n_key not in files:
        raise StateError("n8n database exists but its encryption key is missing")
    if n8n_key in files and not files[n8n_key].strip():
        raise StateError("n8n encryption key is empty")

    memory_name = "native/data/short_term_memory.json"
    if memory_name in files:
        try:
            json.loads(files[memory_name])
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateError("Short-term memory JSON is invalid") from exc

    for name in SQLITE_ARCHIVES & files.keys():
        _validate_sqlite(name, files[name])


def _validate_sqlite(name: str, data: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="caal-validate-") as temp_dir:
        path = Path(temp_dir) / "database.sqlite"
        path.write_bytes(data)
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
                result = database.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise StateError(f"Invalid SQLite snapshot: {name}") from exc
        if result != ("ok",):
            raise StateError(f"SQLite integrity check failed: {name}")


def _make_archive(files: dict[str, bytes], project: Path) -> bytes:
    manifest = {
        "format": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(project),
        "files": {name: _sha256(data) for name, data in sorted(files.items())},
    }
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            _add_bytes(archive, "manifest.json", json.dumps(manifest, indent=2).encode())
            for name, data in sorted(files.items()):
                _add_bytes(archive, name, data)
    return buffer.getvalue()


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def _git_commit(project: Path) -> str | None:
    head = project / ".git" / "HEAD"
    if not head.is_file():
        return None
    value = head.read_text().strip()
    if not value.startswith("ref: "):
        return value
    ref = project / ".git" / value[5:]
    return ref.read_text().strip() if ref.is_file() else None


def _read_archive(payload: bytes) -> tuple[dict, dict[str, bytes]]:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as gz:
            with tarfile.open(fileobj=gz, mode="r:") as archive:
                members = {member.name: member for member in archive.getmembers()}
                manifest_member = members.get("manifest.json")
                if manifest_member is None:
                    raise StateError("Backup has no manifest")
                manifest_file = archive.extractfile(manifest_member)
                if manifest_file is None:
                    raise StateError("Backup manifest is unreadable")
                manifest = json.loads(manifest_file.read())
                expected = manifest.get("files", {})
                if manifest.get("format") != FORMAT_VERSION or not isinstance(expected, dict):
                    raise StateError("Unsupported CAAL backup format")
                files: dict[str, bytes] = {}
                for name, checksum in expected.items():
                    if name not in _allowed_archive_names() or name not in members:
                        raise StateError(f"Unexpected or missing backup entry: {name}")
                    source = archive.extractfile(members[name])
                    if source is None:
                        raise StateError(f"Unreadable backup entry: {name}")
                    data = source.read()
                    if _sha256(data) != checksum:
                        raise StateError(f"Checksum mismatch: {name}")
                    files[name] = data
                _validate_state(files)
                return manifest, files
    except StateError:
        raise
    except (gzip.BadGzipFile, tarfile.TarError, json.JSONDecodeError) as exc:
        raise StateError("Damaged CAAL backup payload") from exc


def _allowed_archive_names() -> set[str]:
    names = set(STATE_FILES)
    names.update(
        f"project/prompt/{language}/greetings.txt"
        for language in ("en", "fr", "it", "pt", "da", "ro")
    )
    return names


def _running_services(project: Path) -> list[str]:
    running = []
    for pid_file in (project / ".native" / "pids").glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            running.append(pid_file.stem)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            continue
    return sorted(running)


def _write_restored(project: Path, files: dict[str, bytes], force: bool) -> None:
    targets = {name: project / STATE_FILES[name] for name in files if name in STATE_FILES}
    for name in files:
        if name.startswith("project/prompt/") and name.endswith("/greetings.txt"):
            relative = name.removeprefix("project/")
            targets[name] = project / relative

    existing = [str(path.relative_to(project)) for path in targets.values() if path.exists()]
    if existing and not force:
        sample = ", ".join(existing[:3])
        suffix = "…" if len(existing) > 3 else ""
        raise StateError(
            f"Restore would replace existing state ({sample}{suffix}); rerun with --force"
        )

    staged: list[tuple[Path, Path]] = []
    try:
        for name, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            temporary = Path(temporary_name)
            with os.fdopen(fd, "wb") as output:
                output.write(files[name])
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            staged.append((temporary, target))
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def _keychain_password(*, create: bool) -> bytes:
    security = shutil.which("security")
    if security is None or sys.platform != "darwin":
        raise StateError(
            "macOS Keychain is unavailable; use --prompt or --password-file"
        )
    account = os.environ.get("USER") or getpass.getuser()
    lookup = subprocess.run(
        [
            security,
            "find-generic-password",
            "-a",
            account,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        check=False,
        capture_output=True,
    )
    if lookup.returncode == 0 and lookup.stdout.rstrip(b"\r\n"):
        return lookup.stdout.rstrip(b"\r\n")
    if not create:
        raise StateError(
            f"Backup key is missing from Keychain ({KEYCHAIN_LABEL}). "
            "Restore the login Keychain or use --prompt/--password-file."
        )

    password = secrets.token_urlsafe(48).encode()
    created = subprocess.run(
        [
            security,
            "add-generic-password",
            "-a",
            account,
            "-s",
            KEYCHAIN_SERVICE,
            "-l",
            KEYCHAIN_LABEL,
            "-T",
            security,
            "-w",
            password.decode(),
        ],
        check=False,
        capture_output=True,
    )
    if created.returncode != 0:
        detail = created.stderr.decode(errors="replace").strip()
        raise StateError(
            "Could not create the CAAL backup key in macOS Keychain"
            + (f": {detail}" if detail else "")
        )
    return password


def _password(
    path: Path | None, *, confirm: bool, prompt: bool, create_keychain: bool
) -> bytes:
    if path is not None:
        password = path.read_bytes().rstrip(b"\r\n")
    elif prompt:
        first = getpass.getpass("Backup passphrase: ").encode()
        if confirm and first != getpass.getpass("Confirm passphrase: ").encode():
            raise StateError("Passphrases do not match")
        password = first
    else:
        password = _keychain_password(create=create_keychain)
    if len(password) < 12:
        raise StateError("Use a backup passphrase of at least 12 characters")
    return password


def backup(
    project: Path, output: Path, password_file: Path | None, prompt: bool
) -> None:
    files = _collect_state(project)
    payload = _make_archive(files, project)
    password = _password(
        password_file, confirm=True, prompt=prompt, create_keychain=True
    )
    encrypted = encrypt_payload(payload, password)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as destination:
            destination.write(encrypted)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    # Do not report success until authentication, checksums, JSON, and SQLite
    # integrity have all passed against the bytes written to disk.
    _read_archive(decrypt_payload(output.read_bytes(), password))
    print(f"Created encrypted backup: {output}")
    print("Verified encrypted archive and restored-state integrity")
    print("Included:")
    for name in sorted(files):
        print(f"  {name}")


def restore(
    project: Path,
    source: Path,
    password_file: Path | None,
    force: bool,
    prompt: bool,
) -> None:
    running = _running_services(project)
    if running:
        raise StateError(
            "Stop CAAL before restoring; running services: " + ", ".join(running)
        )
    password = _password(
        password_file, confirm=False, prompt=prompt, create_keychain=False
    )
    manifest, files = _read_archive(decrypt_payload(source.read_bytes(), password))
    _write_restored(project, files, force)
    print(f"Restored backup created at {manifest['created_at']}")
    print("Restored:")
    for name in sorted(files):
        print(f"  {name}")
    print("Start CAAL, then test the n8n connection in Settings → Integrations.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("output", type=Path)
    backup_parser.add_argument("--password-file", type=Path)
    backup_parser.add_argument(
        "--prompt", action="store_true", help="prompt instead of using macOS Keychain"
    )
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("source", type=Path)
    restore_parser.add_argument("--password-file", type=Path)
    restore_parser.add_argument(
        "--prompt", action="store_true", help="prompt instead of using macOS Keychain"
    )
    restore_parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    project = args.project.resolve()
    if not (project / "start-native.sh").is_file():
        print("error: run this from a CAAL repository", file=sys.stderr)
        return 2
    try:
        if args.command == "backup":
            backup(project, args.output, args.password_file, args.prompt)
        else:
            restore(
                project,
                args.source.expanduser().resolve(),
                args.password_file,
                args.force,
                args.prompt,
            )
    except (StateError, FileNotFoundError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
