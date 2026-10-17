"""``tmailor`` command line tool: try the Open API by hand (standard library only, on top of the SDK).

    tmailor me | domains | usage [--from D --to D] | health
    tmailor create [--domain D] [--name N] | list
    tmailor inbox EMAIL [--unread] [--from X] [--subject Y]
    tmailor read EMAIL MESSAGE_ID | wait EMAIL [--from X] [--subject Y] [--timeout 120]
    tmailor rm-msg EMAIL MESSAGE_ID | clear EMAIL | delete EMAIL | token EMAIL
    tmailor shell | login | logout

Addresses are given by e-mail; the tool looks the token up with ``GET /v1/mailboxes`` (cached for the run).
``--token-stdin``, ``--token-file FILE`` or ``--ask-token`` can replace the e-mail (Review 17f SDK-16: never a
token on the command line). The API key comes from ``TMAILOR_API_KEY``, then the file ``~/.tmailor``
(``api_key=...``, mode 600 or stricter, owned by you, not a symlink), then a hidden prompt. The key is never
printed; tokens are printed only by ``tmailor token``. ``rm-msg``, ``clear``, ``delete`` and ``logout``
ask for confirmation (``--yes`` skips it; without a terminal ``--yes`` is required). A base URL whose host is not
open-api.tmailor.com needs ``--allow-untrusted-base-url`` on every run (Review 17f SDK-12).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import errno
import getpass
import json
import os
import re
import secrets as _secrets
import stat
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, NoReturn, Optional, Sequence, TextIO, Tuple

from ._client import Client, Mailbox
from ._core import BaseURLError, resolve_base_url
from ._version import __version__
from .errors import InvalidKeyError, KeyRevokedError, MissingKeyError, TmailorError, WaitTimeoutError
from .models import Message

__all__ = ["main"]

PROG = "tmailor"
CONFIG_ENV = "TMAILOR_CONFIG"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_WAIT_TIMEOUT = 3
EXIT_INTERRUPTED = 130

# Values that must never reach the terminal unless asked for (tmailor token): API keys and JWT-like tokens.
_SECRET_PATTERNS = [
    re.compile(r"tmk_(?:live|test)_[A-Za-z0-9]+_[A-Za-z0-9]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]*\.[A-Za-z0-9_\-]*\.[A-Za-z0-9_\-]*"),
]
_TOKEN_FIELDS = frozenset({"token", "accesstoken", "transfertoken", "reuse_url"})
# Review 17f SDK-14: Unicode bidirectional controls (they reorder what the terminal shows, "Trojan Source") and
# the line/paragraph separators. Never printed as is, in any output mode.
_BIDI = "\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029"
# C0/C1 control characters except tab and newline: message content must not drive the terminal (escape codes).
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f" + _BIDI + "]")
# Review 17f SDK-14: characters JSON leaves raw with ensure_ascii=False (C1, DEL, bidi) and that must be \u-escaped.
_JSON_UNSAFE = re.compile("[\x7f-\x9f" + _BIDI + "]")
# Largest config / token file read (bytes).
_MAX_PRIVATE_FILE = 64 * 1024

# (api_key, validated base_url, allow_untrusted_base_url) -> Client
ClientFactory = Callable[[Optional[str], str, bool], Client]


class CliError(Exception):
    """An error of the tool itself (bad input, missing address, refused confirmation)."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.exit_code = code


# ── output helpers
def clean(value: Any) -> str:
    """Text without terminal control characters (C0 except tab/newline, DEL, C1) and bidi controls.

    Review 17f SDK-14: applied to EVERY line the tool prints (App.say / App.note / error messages), not only to
    message fields.
    """
    return _CONTROL.sub("", "" if value is None else str(value))


def to_json(data: Any, tty: bool) -> str:
    """Review 17f SDK-14: JSON for --raw/--json that cannot drive a terminal.

    On a terminal every non-ASCII character is escaped (``ensure_ascii=True``). Redirected to a file or a pipe the
    JSON keeps Unicode, but C1 controls, DEL and bidi controls are still written as ``\\uXXXX`` escapes (same
    decoded value: those characters can only occur inside JSON strings). JSON itself escapes C0.
    """
    if tty:
        return json.dumps(data, indent=2, ensure_ascii=True)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    return _JSON_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def redact(text: str, extra: Iterable[str] = ()) -> str:
    for secret in extra:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    for pat in _SECRET_PATTERNS:
        text = pat.sub("***", text)
    return text


def mask_tokens(data: Any) -> Any:
    """Copy of an API JSON value with address tokens replaced by "***" (for --raw / --json)."""
    if isinstance(data, dict):
        return {k: ("***" if k in _TOKEN_FIELDS and v is not None else mask_tokens(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_tokens(v) for v in data]
    return data


def fmt_time(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def fmt_table(rows: List[List[str]], headers: List[str]) -> str:
    cols = [headers, *rows]
    widths = [max(len(r[i]) for r in cols) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in cols]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


# ── API key
def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or (Path.home() / ".tmailor"))


# ── private files (config and --token-file). Review 17f SDK-13: no symlinks, no TOCTOU, owner and mode checked
#    on the descriptor that is read; written through a random O_EXCL temp file + fsync + rename.
#    Review 17g SDK-13: the directory is walked and checked by descriptor (openat + fstat), never by path.
#    Review 17h SDK-18: no unsafe fallback. On POSIX every operation needs dir_fd, O_NOFOLLOW and O_DIRECTORY,
#    else the config/token file is refused (fail closed). Windows has its own documented branch (see _win_*).
_POSIX = os.name == "posix"
_WINDOWS = os.name == "nt"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOINHERIT = getattr(os, "O_NOINHERIT", 0)
# Review 17g SDK-13: os.replace is never listed in supports_dir_fd (it shares renameat with os.rename), so the
# 17f test was always False and every operation silently went by path. Check the functions that are listed.
_DIR_FD = _POSIX and all(f in os.supports_dir_fd for f in (os.open, os.stat, os.unlink, os.rename))
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
UNSUPPORTED_PRIVATE_FILES = (
    "This platform is not supported for config/token files (the safe file operations it needs are missing); "
    "use the TMAILOR_API_KEY environment variable / --token-stdin instead"
)


def private_files_supported() -> bool:
    """Review 17h SDK-18: whether config/token files can be used safely here (read at call time, for tests)."""
    if _POSIX:
        return bool(_DIR_FD and _O_NOFOLLOW and _O_DIRECTORY)
    return _WINDOWS


def _require_private_files() -> None:
    """Fail closed before any file system call (no check by path, no fallback)."""
    if not private_files_supported():
        raise CliError(UNSUPPORTED_PRIVATE_FILES)


def _euid() -> int:
    return os.geteuid() if hasattr(os, "geteuid") else -1


_O_PATH = getattr(os, "O_PATH", 0)


def _check_dir_stat(st: os.stat_result, directory: str, final: bool) -> None:
    """Review 17g SDK-13: the rule applied to EVERY directory of the path, from the descriptor that is then used.

    * a directory (fstat of the opened fd), owned by you or root;
    * not writable by group/others unless the sticky bit is set (then others cannot rename or replace an entry
      they do not own), for a normal user and for root alike;
    * the directory that holds the file itself: a sticky world-writable one (such as /tmp) is tolerated for a
      normal user, never for root.
    """
    if not stat.S_ISDIR(st.st_mode):
        raise CliError(f"Refusing {directory}: not a directory")
    euid = _euid()
    if st.st_uid not in (euid, 0):
        raise CliError(f"Refusing {directory}: it belongs to another user (uid {st.st_uid})")
    writable = st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if writable and (not st.st_mode & stat.S_ISVTX or (final and euid == 0)):
        raise CliError(
            f"Refusing {directory}: other users can write to it (mode {stat.S_IMODE(st.st_mode):o}). "
            "Keep the file in a private directory such as your home."
        )


def _split(path: Path) -> Tuple[str, str]:
    """(absolute directory, file name), normalised lexically only: no component is resolved through a symlink."""
    p = os.path.abspath(str(path))
    name = os.path.basename(p)
    if not name or name in (".", ".."):
        raise CliError(f"{path} is not a file path")
    return os.path.dirname(p), name


def _open_dir(directory: str) -> int:
    """Review 17g SDK-13: a verified descriptor of ``directory`` (POSIX only; callers ran _require_private_files).

    The path is walked from ``/`` one component at a time with ``openat(O_DIRECTORY | O_NOFOLLOW)``; every
    descriptor is checked with ``fstat`` (``_check_dir_stat``) before the next component is opened from it, so
    a symbolic link anywhere in the path is refused and no directory can be swapped between the check and the
    use: the caller reads, writes, renames and unlinks only through the returned descriptor.
    """
    if not (_POSIX and private_files_supported()):  # Review 17h SDK-18: never a path fallback
        raise CliError(UNSUPPORTED_PRIVATE_FILES)
    parts = [p for p in directory.split("/") if p]
    shown = "/"
    walk_flags = (_O_PATH or os.O_RDONLY) | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    fd = os.open("/", (_O_PATH or os.O_RDONLY) | _O_DIRECTORY | _O_CLOEXEC)
    try:
        _check_dir_stat(os.fstat(fd), shown, not parts)
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            shown = os.path.join(shown, part)
            # the last directory is opened for reading as well (fsync after a rename)
            flags = (os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC) if last else walk_flags
            try:
                nfd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    try:
                        is_link = stat.S_ISLNK(os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode)
                    except OSError:
                        is_link = False
                    if is_link:
                        raise CliError(
                            f"Refusing {shown}: a directory in the path is a symbolic link (give the real path)"
                        ) from None
                    raise CliError(f"Refusing {shown}: not a directory") from None
                raise CliError(f"Cannot use the directory {shown} ({exc.strerror})") from None
            os.close(fd)
            fd = nfd
            _check_dir_stat(os.fstat(fd), shown, last)
        if not parts and _O_PATH:  # the file is directly under "/": reopen it readable
            nfd = os.open(".", os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
            _check_dir_stat(os.fstat(fd), shown, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


# ── Review 17h SDK-18: Windows branch (no openat/O_NOFOLLOW there). Threat model: the file lives inside your own
#    user profile, whose default ACL lets only you, SYSTEM and Administrators write; no other unprivileged user
#    can create or swap entries there. Operations go by path, but every directory from the profile down and the
#    file itself are checked with lstat and refused when they are a symbolic link, junction or other reparse
#    point; the opened file must be the one that was checked (same volume and file index).
def _is_reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _win_check_dir(directory: str) -> None:
    home = os.path.abspath(str(Path.home()))
    nd, nh = os.path.normcase(directory), os.path.normcase(home)
    try:
        inside = os.path.commonpath([nd, nh]) == nh
    except ValueError:  # another drive
        inside = False
    if not inside:
        raise CliError(f"Refusing {directory}: on Windows the file must be inside your user profile ({home})")
    rel = os.path.relpath(directory, home)
    cur = home
    for part in [""] + ([] if rel == "." else rel.split(os.sep)):
        cur = os.path.join(cur, part) if part else cur
        try:
            st = os.lstat(cur)
        except OSError as exc:
            raise CliError(f"Cannot use the directory {cur} ({exc.strerror})") from None
        if _is_reparse(st):
            raise CliError(f"Refusing {cur}: a directory in the path is a symbolic link or junction (give the real path)")
        if not stat.S_ISDIR(st.st_mode):
            raise CliError(f"Refusing {cur}: not a directory")


def _win_lstat_file(full: str, path: Path, what: str) -> Optional[os.stat_result]:
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CliError(f"Cannot use the {what} {path} ({exc.strerror})") from None
    if _is_reparse(st):
        raise CliError(f"Refusing {path}: the {what} must not be a symbolic link or reparse point")
    if not stat.S_ISREG(st.st_mode):
        raise CliError(f"Refusing {path}: the {what} must be a regular file")
    return st


def _read_fd(fd: int, path: Path, what: str) -> str:
    """Checks on the open descriptor, then its contents (64 KiB at most)."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise CliError(f"Refusing {path}: the {what} must be a regular file")
    if _POSIX:
        if st.st_uid != _euid():
            raise CliError(f"Refusing {path}: the {what} belongs to another user (uid {st.st_uid})")
        if stat.S_IMODE(st.st_mode) & ~0o600:
            raise CliError(
                f"{path} can be used by other users (mode {stat.S_IMODE(st.st_mode):o}). Fix it with: chmod 600 {path}"
            )
    if st.st_size > _MAX_PRIVATE_FILE:
        raise CliError(f"Refusing {path}: the {what} is too large")
    chunks: List[bytes] = []
    size = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        size += len(chunk)
        if size > _MAX_PRIVATE_FILE:
            raise CliError(f"Refusing {path}: the {what} is too large")
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise CliError(f"The {what} {path} is not UTF-8 text") from None


def read_private_file(path: Path, what: str) -> Optional[str]:
    """Contents of a private file, or None when it does not exist.

    Refused (CliError): a symlink, anything but a regular file, a file owned by another user, a mode other than
    600 or stricter, a file larger than 64 KiB, an unsafe directory anywhere in the path (see ``_open_dir``),
    a platform without the safe file operations (Review 17h SDK-18).
    Every check is made on the descriptor that is read (no stat-then-open race).
    """
    _require_private_files()
    directory, name = _split(path)
    if _WINDOWS:
        _win_check_dir(directory)
        full = os.path.join(directory, name)
        before = _win_lstat_file(full, path, what)
        if before is None:
            return None
        try:
            fd = os.open(full, os.O_RDONLY | _O_BINARY | _O_NOINHERIT)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CliError(f"Cannot open the {what} {path} ({exc.strerror})") from None
        try:
            now = os.fstat(fd)
            if (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino):
                raise CliError(f"Refusing {path}: the {what} changed while it was being opened")
            return _read_fd(fd, path, what)
        finally:
            os.close(fd)
    dfd = _open_dir(directory)  # Review 17g SDK-13: verified fd, used for every operation below
    try:
        try:
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK, dir_fd=dfd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise CliError(f"Refusing {path}: the {what} must not be a symbolic link") from None
            raise CliError(f"Cannot open the {what} {path} ({exc.strerror})") from None
        try:
            return _read_fd(fd, path, what)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def write_private_file(path: Path, body: str) -> None:
    """Write a private file atomically: random temp name created with O_CREAT|O_EXCL|O_NOFOLLOW and mode 600,
    fsync, rename over the target, fsync of the directory. An existing target must be a regular file you own
    (a symlink is refused, never followed). Review 17h SDK-19: the temp file (it holds the secret) is removed
    whenever the rename did not happen, whatever failed."""
    _require_private_files()
    directory, name = _split(path)
    tmp = f".{name}.{_secrets.token_hex(8)}.tmp"
    data = body.encode("utf-8")
    if _WINDOWS:
        _win_check_dir(directory)
        full = os.path.join(directory, name)
        _win_lstat_file(full, path, "file")  # refuses a reparse point / non-regular target
        tmp_full = os.path.join(directory, tmp)
        created = False
        try:
            fd = os.open(tmp_full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOINHERIT, 0o600)
            created = True
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp_full, full)
            created = False
        finally:
            if created:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_full)
        return
    dfd = _open_dir(directory)  # Review 17g SDK-13: verified fd, used for every operation below
    created = False
    try:
        try:
            st: Optional[os.stat_result] = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None:
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise CliError(f"Refusing to write {path}: it is a symbolic link or not a regular file")
            if st.st_uid != _euid():
                raise CliError(f"Refusing to write {path}: it belongs to another user (uid {st.st_uid})")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=dfd)
        created = True
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        created = False  # renamed: the temp name no longer exists, never unlink after this point
        with contextlib.suppress(OSError):
            os.fsync(dfd)
    finally:
        try:
            if created:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dfd)
        finally:
            os.close(dfd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]
    os.fsync(fd)


def remove_private_file(path: Path) -> bool:
    """Delete the file (a symlink is removed itself, never followed). False when it does not exist."""
    _require_private_files()
    directory, name = _split(path)
    if _WINDOWS:
        _win_check_dir(directory)
        full = os.path.join(directory, name)
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CliError(f"Cannot use {path} ({exc.strerror})") from None
        if stat.S_ISDIR(st.st_mode) and not _is_reparse(st):
            raise CliError(f"Refusing to remove {path}: it is a directory")
        try:
            os.unlink(full)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CliError(f"Cannot remove {path} ({exc.strerror})") from None
        return True
    dfd = _open_dir(directory)  # Review 17g SDK-13: verified fd, used for every operation below
    try:
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(dfd)


def read_config(path: Path) -> Dict[str, str]:
    """``key=value`` lines (``api_key``, optional ``base_url``). Same checks as ``read_private_file``."""
    text = read_private_file(path, "config file")
    if text is None:
        return {}
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip().lower()] = v.strip().strip("'\"")
    return out


def write_config(path: Path, values: Dict[str, str]) -> None:
    """Write the config file with mode 600 from the start (never briefly readable by others)."""
    body = "# tmailor CLI settings. Keep this file private (chmod 600).\n" + "".join(f"{k}={v}\n" for k, v in values.items())
    write_private_file(path, body)


def resolve_key(interactive: bool) -> Optional[str]:
    key = os.environ.get("TMAILOR_API_KEY", "").strip()
    if key:
        return key
    # Review 17i SDK-18: like base_url_for, the default config is skipped where config files are unsupported, so
    # TMAILOR_API_KEY / --token-stdin keep working; an explicit TMAILOR_CONFIG is still read (and refused clearly).
    if private_files_supported() or os.environ.get(CONFIG_ENV):
        key = read_config(config_path()).get("api_key", "").strip()
        if key:
            return key
    if interactive and sys.stdin.isatty():
        key = getpass.getpass("Tmailor API key (input hidden): ").strip()
        return key or None
    return None


def default_factory(api_key: Optional[str], base_url: str, allow_untrusted: bool) -> Client:
    return Client(api_key, base_url=base_url, allow_untrusted_base_url=allow_untrusted)


def base_url_for(args: argparse.Namespace, from_config: bool = True) -> Tuple[str, bool]:
    """Review 17f SDK-12: the validated base URL (``--base-url``, else ``TMAILOR_BASE_URL``, else ``base_url`` of the
    config file, else production) and whether ``--allow-untrusted-base-url`` was given. A host other than
    open-api.tmailor.com is refused without that flag; http:// only for a loopback host."""
    allow = bool(getattr(args, "allow_untrusted_base_url", False))
    base = getattr(args, "base_url", None) or os.environ.get("TMAILOR_BASE_URL") or None
    # Review 17h SDK-18: where config files are unsupported, the default config is skipped (TMAILOR_API_KEY /
    # --token-stdin keep working); an explicit TMAILOR_CONFIG is still read, and refused with a clear error.
    if base is None and from_config and (private_files_supported() or os.environ.get(CONFIG_ENV)):
        base = read_config(config_path()).get("base_url") or None
    try:
        return resolve_base_url(base, allow), allow
    except BaseURLError as exc:
        raise CliError(f"Refused base URL: {exc}", EXIT_USAGE) from None


# ── address lookup
@dataclass
class Resolver:
    """E-mail -> token, from ``GET /v1/mailboxes`` (loaded once per run, reloaded once on a miss)."""

    client: Client
    boxes: Dict[str, Mailbox] = field(default_factory=dict)
    loaded: bool = False

    def load(self) -> List[Mailbox]:
        self.boxes = {}
        items = list(self.client.mailboxes.list())
        for mb in items:
            if mb.email:
                self.boxes[mb.email.lower()] = mb
        self.loaded = True
        return items

    def remember(self, mb: Mailbox) -> None:
        if mb.email:
            self.boxes[mb.email.lower()] = mb

    def forget(self, email: Optional[str]) -> None:
        if email:
            self.boxes.pop(email.lower(), None)

    def secrets(self) -> List[str]:
        out: List[str] = []
        for mb in self.boxes.values():
            if mb.token:
                out.append(mb.token)
            if mb.web:
                out += [mb.web.transfertoken, mb.web.reuse_url]
        return out

    def get(self, email: Optional[str], token: Optional[str]) -> Mailbox:
        if token:
            return self.client.mailbox(token)
        if not email:
            raise CliError("Give the address (EMAIL) or --token-stdin / --token-file / --ask-token", EXIT_USAGE)
        key = email.strip().lower()
        if "@" not in key:
            raise CliError(
                f"'{clean(email)}' is not an e-mail address (pass a token with --token-stdin or --token-file)", EXIT_USAGE
            )
        if key not in self.boxes:
            self.load()
        mb = self.boxes.get(key)
        if mb is None:
            raise CliError(f"{clean(email)} is not an address of this account (see: {PROG} list)")
        if not mb.token:
            raise CliError(f"The token of {clean(email)} is temporarily unavailable. Try again in a minute.")
        return mb


# ── the application
class App:
    def __init__(self, args: argparse.Namespace, factory: ClientFactory, out: TextIO, err: TextIO) -> None:
        self.args = args
        self.factory = factory
        self.out = out
        self.err = err
        self._client: Optional[Client] = None
        self._resolver: Optional[Resolver] = None
        self.api_key: Optional[str] = None
        self._token: Optional[str] = None

    # client is created on first use, so `health` and `--help` never ask for a key
    def client(self, need_key: bool = True) -> Client:
        if self._client is None:
            key = resolve_key(interactive=need_key) if need_key else None
            if need_key and not key:
                raise MissingKeyError(
                    f"No API key. Set TMAILOR_API_KEY, run '{PROG} login', or put api_key=... in {config_path()}",
                    code="MissingKey",
                )
            self.api_key = key
            base, allow = base_url_for(self.args)  # Review 17f SDK-12
            self._client = self.factory(key, base, allow)
        return self._client

    @property
    def resolver(self) -> Resolver:
        if self._resolver is None:
            self._resolver = Resolver(self.client())
        return self._resolver

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def secrets(self) -> List[str]:
        out = [self.api_key or ""]
        if self._resolver is not None:
            out += self._resolver.secrets()
        if self._token:
            out.append(self._token)
        return out

    # printing
    # Review 17f SDK-14: everything printed goes through clean() (control and bidi characters removed).
    def say(self, text: str = "") -> None:
        print(clean(text), file=self.out)

    def note(self, text: str) -> None:
        print(clean(text), file=self.err)

    @property
    def machine(self) -> bool:
        return bool(self.args.raw or self.args.json)

    def emit(self, raw: Any, data: Any, human: Callable[[], None]) -> None:
        """``--raw``: the API JSON (tokens masked); ``--json``: the CLI result; else human text."""
        if self.args.raw:
            self.say(to_json(mask_tokens(raw), _isatty(self.out)))  # Review 17f SDK-14
        elif self.args.json:
            self.say(to_json(mask_tokens(data), _isatty(self.out)))
        else:
            human()

    def token(self) -> Optional[str]:
        """Review 17f SDK-16: the address token from --token-stdin, --token-file or --ask-token (never from argv)."""
        if self._token is not None:
            return self._token
        a = self.args
        text: Optional[str] = None
        if getattr(a, "token_stdin", False):
            text = sys.stdin.read(_MAX_PRIVATE_FILE + 1)
            if len(text) > _MAX_PRIVATE_FILE:
                raise CliError("Too much input on stdin for a token", EXIT_USAGE)
        elif getattr(a, "token_file", None):
            text = read_private_file(Path(a.token_file), "token file")
            if text is None:
                raise CliError(f"{a.token_file} does not exist", EXIT_USAGE)
        elif getattr(a, "ask_token", False):
            if not sys.stdin.isatty():
                raise CliError("--ask-token needs a terminal (use --token-stdin or --token-file in scripts)", EXIT_USAGE)
            text = getpass.getpass("Address token (input hidden): ")
        else:
            return None
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise CliError("No token given", EXIT_USAGE)
        self._token = lines[0]
        return self._token

    def mailbox(self) -> Mailbox:
        return self.resolver.get(getattr(self.args, "email", None), self.token())

    def confirm(self, question: str) -> None:
        if getattr(self.args, "yes", False):
            return
        if not sys.stdin.isatty():
            raise CliError("Refusing without confirmation: no terminal to ask. Add --yes to skip the question.")
        answer = input(clean(f"{question} Type 'yes' to confirm: ")).strip().lower()
        if answer not in ("y", "yes"):
            raise CliError("Cancelled.")


# ── commands
def message_dict(m: Message) -> Dict[str, Any]:
    d: Dict[str, Any] = dict(m.raw)
    d["otp"] = m.otp() if m.has_body else None
    d["links"] = m.links() if m.has_body else []
    return d


def print_message(app: App, m: Message, show_html: bool = False) -> None:
    app.say(f"Subject:  {clean(m.subject)}")
    app.say(f"From:     {clean(m.from_.name)} <{clean(m.from_.address)}>")
    app.say(f"To:       {clean(m.to)}")
    app.say(f"Received: {fmt_time(m.received_at)}")
    app.say(f"ID:       {m.id}")
    otp = m.otp()
    app.say(f"OTP:      {otp if otp else '(none found)'}")
    links = m.links()
    if links:
        app.say("Links:")
        for u in links:
            app.say(f"  {clean(u)}")
    app.say("")
    if m.body_available is False:
        app.say("(The body of this message is no longer available.)")
    elif show_html:
        app.say(clean(m.html or "(no HTML part)"))
    else:
        app.say(clean(m.text or "(no text part)"))


def cmd_health(app: App) -> int:
    h = app.client(need_key=False).health()
    app.emit(h.raw, h.raw, lambda: app.say(f"Status: {h.status}   API version: {h.version}   Server time: {fmt_time(h.time)}"))
    return EXIT_OK


def cmd_me(app: App) -> int:
    me = app.client().me()

    def human() -> None:
        lim = me.limits
        app.say(f"User ID:        {me.user_id}")
        app.say(f"API key:        tmk_live_{me.key.prefix}_*** (id {me.key.key_id})")
        app.say(f"Your IP:        {me.client_ip}")
        app.say(f"Mailbox quota:  {lim.quota_used} used of {lim.quota_total} ({lim.quota_remaining} left)")
        app.say(f"Addresses:      {lim.mailbox_count}")
        cap = "no daily cap" if lim.daily_req_cap == 0 else f"{lim.daily_req_cap} per day"
        app.say(f"Rate limit:     {lim.rate_per_min} requests per minute, {cap}")
        app.say(f"IP allowlist:   {', '.join(me.ip_allowlist) or '(empty)'} (max {lim.max_ip})")
        if me.subscriptions:
            app.say("")
            rows = [
                [s.plan, f"{s.used}/{s.quota}", fmt_time(s.expires_at) if s.expires_at else "never", fmt_time(s.period_end)]
                for s in me.subscriptions
            ]
            app.say(fmt_table(rows, ["Plan", "Used/Quota", "Expires", "Period ends"]))

    app.emit(me.raw, me.raw, human)
    return EXIT_OK


def cmd_domains(app: App) -> int:
    d = app.client().domains()
    raw = {"items": [x.raw for x in d.items], "shared_domains_available": d.shared_domains_available}

    def human() -> None:
        if d.items:
            rows = [[x.domain, x.status, "yes" if x.usable else "no", "yes" if x.allow_custom_name else "no"] for x in d.items]
            app.say(fmt_table(rows, ["Domain", "Status", "Usable", "Custom names"]))
        else:
            app.say("No own domains. Add one on https://platform.tmailor.com/domains")
        app.say(f"Shared domains available: {'yes' if d.shared_domains_available else 'no'}")

    app.emit(raw, raw, human)
    return EXIT_OK


def cmd_usage(app: App) -> int:
    u = app.client().usage(from_=app.args.date_from, to=app.args.date_to)

    def human() -> None:
        app.say(f"Usage {u.from_} to {u.to} ({u.timezone})")
        rows = [
            [
                x.date,
                str(x.req_total),
                str(x.req_4xx),
                str(x.req_5xx),
                str(x.req_429),
                str(x.mailbox_created),
                str(x.msg_list),
                str(x.msg_read),
            ]
            for x in u.days
            if x.req_total or x.mailbox_created or app.args.all_days
        ]
        if rows:
            app.say(fmt_table(rows, ["Date", "Requests", "4xx", "5xx", "429", "Created", "Lists", "Reads"]))
        else:
            app.say("No requests in this range.")

    app.emit(u.raw, u.raw, human)
    return EXIT_OK


def cmd_create(app: App) -> int:
    mb = app.client().mailboxes.create(domain=app.args.domain, name=app.args.name)
    app.resolver.remember(mb)

    def human() -> None:
        app.say(f"Created {mb.email}")
        app.say(f"Domain: {mb.domain}{' (your domain)' if mb.is_user_domain else ''}   Created: {fmt_time(mb.created_at)}")
        app.say(f"Wait for mail with: {PROG} wait {mb.email}")

    app.emit(mb.raw, mb.raw, human)
    return EXIT_OK


def cmd_list(app: App) -> int:
    items = app.resolver.load()

    def human() -> None:
        if not items:
            app.say(f"No addresses yet. Create one with: {PROG} create")
            return
        rows = [
            [clean(mb.email), fmt_time(mb.created_at), "-" if mb.message_count is None else str(mb.message_count)] for mb in items
        ]
        app.say(fmt_table(rows, ["Address", "Created", "Messages (24h)"]))
        app.say(f"{len(items)} address(es)")

    raw = [mb.raw for mb in items]
    app.emit(raw, raw, human)
    return EXIT_OK


def cmd_inbox(app: App) -> int:
    mb = app.mailbox()
    msgs = list(
        mb.messages(
            unread=True if app.args.unread else None, from_=app.args.sender, subject=app.args.subject, limit=app.args.limit
        )
    )

    def human() -> None:
        if not msgs:
            app.say("No messages." if not (app.args.unread or app.args.sender or app.args.subject) else "No matching messages.")
            return
        rows = [
            [
                m.id,
                fmt_time(m.received_at),
                short(clean(m.from_.address), 32),
                short(clean(m.subject), 50),
                "no" if m.seen else "yes",
            ]
            for m in msgs
        ]
        app.say(fmt_table(rows, ["ID", "Received", "From", "Subject", "Unread"]))
        app.say(f"Read one with: {PROG} read {mb.email or '<email>'} <ID>")

    raw = [m.raw for m in msgs]
    app.emit(raw, raw, human)
    return EXIT_OK


def cmd_read(app: App) -> int:
    m = app.mailbox().message(app.args.message_id)
    app.emit(m.raw, message_dict(m), lambda: print_message(app, m, app.args.html))
    return EXIT_OK


def cmd_wait(app: App) -> int:
    mb = app.mailbox()
    since: Optional[int] = None
    if app.args.any:
        # Also accept a message that is already in the inbox: everything received after the address was created.
        if mb.created_at is None:
            mb.info()
        since = (mb.created_at or 0) - 1
    if not app.machine:
        what = "a matching message" if app.args.any else "a new message"
        app.note(f"Waiting up to {app.args.timeout:g} s for {what} to {mb.email or 'the address'} (Ctrl+C to stop)...")
    try:
        m = mb.wait_for_message(timeout=app.args.timeout, from_=app.args.sender, subject_contains=app.args.subject, since=since)
    except WaitTimeoutError:
        app.note(f"No new matching message within {app.args.timeout:g} s.")
        return EXIT_WAIT_TIMEOUT
    app.emit(m.raw, message_dict(m), lambda: print_message(app, m))
    return EXIT_OK


def cmd_rm_msg(app: App) -> int:
    mb = app.mailbox()
    app.confirm(f"Delete message {app.args.message_id} of {mb.email or 'this address'}? It cannot be undone.")  # 17f SDK-15
    mb.delete_message(app.args.message_id)
    app.emit({}, {"ok": True}, lambda: app.say("Message deleted."))
    return EXIT_OK


def cmd_clear(app: App) -> int:
    mb = app.mailbox()
    app.confirm(f"Delete ALL messages of {mb.email or 'this address'}?")
    n = mb.clear()
    app.emit({"deleted": n}, {"deleted": n}, lambda: app.say(f"Deleted {n} message(s)."))
    return EXIT_OK


def cmd_delete(app: App) -> int:
    mb = app.mailbox()
    label = mb.email or "this address"
    app.confirm(f"Delete {label}? It cannot be undone and the quota is not returned.")
    mb.delete()
    app.resolver.forget(mb.email)
    app.emit({}, {"deleted": True}, lambda: app.say(f"Deleted {label}."))
    return EXIT_OK


def cmd_token(app: App) -> int:
    mb = app.mailbox()
    if not mb.token:
        raise CliError("No token for this address right now. Try again in a minute.")
    app.note("Warning: this token gives full access to the address (read and delete mail). Do not share it.")
    if app.args.json:
        app.say(json.dumps({"email": mb.email, "token": mb.token}))
    else:
        app.say(mb.token)
    return EXIT_OK


def cmd_login(app: App) -> int:
    path = config_path()
    _require_private_files()  # Review 17h SDK-18: fail before asking for the key
    if not sys.stdin.isatty():
        raise CliError("login needs a terminal (the key is read with a hidden prompt).")
    key = getpass.getpass("Tmailor API key (input hidden): ").strip()
    if not key:
        raise CliError("No key entered.")
    # Review 17f SDK-12: --base-url is validated (untrusted host needs --allow-untrusted-base-url) and is used only
    # to check the key: it is never saved, so a later run cannot silently send the key to that host.
    base, allow = base_url_for(app.args, from_config=False)
    client = app.factory(key, base, allow)
    try:
        me = client.me()
        app.note(f"Key works: user {me.user_id}, {me.limits.quota_remaining} mailbox(es) of quota left.")
    except (InvalidKeyError, KeyRevokedError, MissingKeyError) as exc:
        raise CliError(f"This key was refused: {exc}") from None
    except TmailorError as exc:
        app.note(f"Could not verify the key now ({exc}); saving it anyway.")
    finally:
        client.close()
    write_config(path, {"api_key": key})
    app.say(f"Saved the key to {path} (mode 600).")
    if getattr(app.args, "base_url", None):
        app.note("The base URL is not saved: pass --base-url (or set TMAILOR_BASE_URL) on each run.")
    return EXIT_OK


def cmd_logout(app: App) -> int:
    path = config_path()
    _require_private_files()  # Review 17h SDK-18: not even an existence check by path on an unsupported platform
    # Review 17i SDK-18: no path-based existence pre-check; remove_private_file works only through verified dir fds
    # and reports False when the file is absent.
    app.confirm(f"Remove the saved API key ({path})?")  # Review 17f SDK-15
    if remove_private_file(path):
        app.say(f"Removed {path}.")
    else:
        app.say(f"Nothing to remove ({path} does not exist).")
    return EXIT_OK


# ── interactive shell
SHELL_HELP = """Commands:
  <number>   select an address from the list
  n          new address (random)        r   reload the list
  q          quit"""

BOX_HELP = """Commands for {email}:
  i          inbox                       <number>  open message from the last inbox
  w [sec]    wait for a new message      c   delete all messages
  d          delete this address         b   back to the list
  q          quit"""


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def cmd_shell(app: App) -> int:
    if not sys.stdin.isatty():
        raise CliError("shell needs a terminal.")
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401  (line editing and history when available)
    app.client()
    app.say(f"tmailor shell {__version__}. Type ? for help.")
    items = app.resolver.load()
    while True:
        app.say("")
        if items:
            for i, mb in enumerate(items, 1):
                n = "-" if mb.message_count is None else str(mb.message_count)
                app.say(f"  {i:>3}) {clean(mb.email)}   {n} message(s)")
        else:
            app.say("  (no addresses)")
        cmd = _ask("tmailor> ").lower()
        try:
            if cmd in ("q", "quit", "exit"):
                return EXIT_OK
            if cmd in ("?", "h", "help"):
                app.say(SHELL_HELP)
            elif cmd == "r" or cmd == "":
                items = app.resolver.load()
            elif cmd == "n":
                mb = app.client().mailboxes.create()
                app.resolver.remember(mb)
                app.say(f"Created {mb.email}")
                items = app.resolver.load()
            elif cmd.isdigit() and 1 <= int(cmd) <= len(items):
                if _box_shell(app, items[int(cmd) - 1]) == "quit":
                    return EXIT_OK
                items = app.resolver.load()
            else:
                app.say("Unknown command. Type ? for help.")
        except TmailorError as exc:
            app.note(f"Error: {redact(str(exc), app.secrets())}")


def _box_shell(app: App, mb: Mailbox) -> str:
    app.say(BOX_HELP.format(email=clean(mb.email)))
    last: List[Message] = []
    while True:
        cmd = _ask(f"{clean(mb.email)}> ").lower()
        try:
            if cmd in ("q", "quit", "exit"):
                return "quit"
            if cmd in ("b", "back"):
                return "back"
            if cmd in ("?", "h", "help"):
                app.say(BOX_HELP.format(email=clean(mb.email)))
            elif cmd in ("i", ""):
                last = list(mb.messages(limit=50))
                if not last:
                    app.say("No messages.")
                for i, m in enumerate(last, 1):
                    flag = " " if m.seen else "*"
                    sender, subj = short(clean(m.from_.address), 28), short(clean(m.subject), 50)
                    app.say(f"  {i:>3}){flag} {fmt_time(m.received_at)}  {sender}  {subj}")
            elif cmd.isdigit() and 1 <= int(cmd) <= len(last):
                app.say("")
                print_message(app, mb.message(last[int(cmd) - 1].id))
            elif cmd.split()[0] == "w":
                parts = cmd.split()
                secs = float(parts[1]) if len(parts) > 1 else 120.0
                app.say(f"Waiting up to {secs:g} s (Ctrl+C to stop waiting)...")
                try:
                    m = mb.wait_for_message(timeout=secs)
                    app.say("")
                    print_message(app, m)
                except WaitTimeoutError:
                    app.say("No new message.")
                except KeyboardInterrupt:
                    app.say("\nStopped waiting.")
            elif cmd == "c":
                if _ask("Delete ALL messages? Type 'yes': ").lower() in ("y", "yes"):
                    app.say(f"Deleted {mb.clear()} message(s).")
            elif cmd == "d":
                if _ask(f"Delete {clean(mb.email)}? It cannot be undone. Type 'yes': ").lower() in ("y", "yes"):
                    mb.delete()
                    app.resolver.forget(mb.email)
                    app.say("Deleted.")
                    return "back"
            else:
                app.say("Unknown command. Type ? for help.")
        except ValueError:
            app.say("Give the wait time in seconds, for example: w 60")
        except TmailorError as exc:
            app.note(f"Error: {redact(str(exc), app.secrets())}")


# ── argument parsing
def escape_terminal(text: str) -> str:
    """Review 17g SDK-14: C0 (except tab/newline), DEL, C1 and bidi controls written as visible ``\\uXXXX``."""
    return _CONTROL.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def write_safe(text: str, stream: Optional[TextIO] = None) -> None:
    """Review 17g SDK-14: the single writer for argparse output (usage, errors, help, version)."""
    if text:
        (stream or sys.stderr).write(escape_terminal(text))


class SafeArgumentParser(argparse.ArgumentParser):
    """Review 17g SDK-14: argparse messages can quote argv (unknown option, bad value, invalid choice); every one
    goes through ``write_safe`` instead of being printed raw. Exit code 2 for errors is unchanged.
    Sub-parsers inherit this class (``add_subparsers`` uses ``type(self)``)."""

    def _print_message(self, message: str, file: Optional[Any] = None) -> None:
        if message:
            with contextlib.suppress(AttributeError, OSError, ValueError):
                write_safe(message, file or sys.stderr)

    def exit(self, status: int = 0, message: Optional[str] = None) -> NoReturn:
        if message:
            self._print_message(message, sys.stderr)
        sys.exit(status)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


COMMANDS: Dict[str, Callable[[App], int]] = {
    "health": cmd_health,
    "me": cmd_me,
    "domains": cmd_domains,
    "usage": cmd_usage,
    "create": cmd_create,
    "list": cmd_list,
    "inbox": cmd_inbox,
    "read": cmd_read,
    "wait": cmd_wait,
    "rm-msg": cmd_rm_msg,
    "clear": cmd_clear,
    "delete": cmd_delete,
    "token": cmd_token,
    "shell": cmd_shell,
    "login": cmd_login,
    "logout": cmd_logout,
}


def _common(sub: bool) -> SafeArgumentParser:
    """Options accepted before or after the command (on the sub-parser they never reset a value given before)."""
    d: Any = argparse.SUPPRESS if sub else None
    p = SafeArgumentParser(add_help=False)
    p.add_argument("--base-url", default=d, help="API base URL (default: TMAILOR_BASE_URL or https://open-api.tmailor.com)")
    p.add_argument(
        "--allow-untrusted-base-url",
        action="store_true",
        default=d if sub else False,
        help="allow a base URL whose host is not open-api.tmailor.com (your API key and tokens are sent there); "
        "also needed for http:// to a loopback host",
    )
    p.add_argument("--raw", action="store_true", default=d if sub else False, help="print the API JSON (tokens masked)")
    p.add_argument("--json", action="store_true", default=d if sub else False, help="print the result as JSON")
    p.add_argument("--debug", action="store_true", default=d if sub else False, help="show tracebacks (secrets masked)")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common(sub=True)
    parser = SafeArgumentParser(  # Review 17g SDK-14
        prog=PROG,
        parents=[_common(sub=False)],
        description="Command line tool for the Tmailor Open API (temporary e-mail addresses).",
        epilog="The API key comes from TMAILOR_API_KEY, ~/.tmailor (see 'tmailor login') or a hidden prompt.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sp = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sp.add_parser(name, parents=[common], help=help_, description=help_)

    def token_options(p: argparse.ArgumentParser) -> None:
        # Review 17f SDK-16: the token is never an argument (shell history, process list, audit logs).
        g = p.add_mutually_exclusive_group()
        g.add_argument("--token-stdin", action="store_true", help="read the address token from standard input")
        g.add_argument("--token-file", metavar="FILE", help="read the address token from FILE (mode 600, owned by you)")
        g.add_argument("--ask-token", action="store_true", help="ask for the address token (hidden input)")
        p.add_argument("--token", dest="token_argv", default=None, help=argparse.SUPPRESS)  # removed: refused below

    def address(p: argparse.ArgumentParser) -> None:
        p.add_argument("email", nargs="?", metavar="EMAIL", help="the address (e-mail)")
        token_options(p)

    def addr_and_id(p: argparse.ArgumentParser) -> None:
        p.add_argument("first", metavar="EMAIL", help="the address (e-mail); with a --token-* option give only MESSAGE_ID")
        p.add_argument("second", nargs="?", metavar="MESSAGE_ID", help="message id (from 'tmailor inbox')")
        token_options(p)

    def yes(p: argparse.ArgumentParser) -> None:
        p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    add("health", "service status (no API key needed)")
    add("me", "your account, quota, limits and plans")
    add("domains", "your own domains")
    p = add("usage", "daily request counters")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    p.add_argument("--all-days", action="store_true", help="also show days without requests")
    p = add("create", "create an address (uses 1 unit of mailbox quota)")
    p.add_argument("--domain", help="one of your usable domains (default: a random shared domain)")
    p.add_argument("--name", help="custom name, only with your own domain")
    add("list", "list your addresses")
    p = add("inbox", "list messages of an address (last 24 hours)")
    address(p)
    p.add_argument("--unread", action="store_true", help="only messages not read yet")
    p.add_argument("--from", dest="sender", metavar="TEXT", help="text in the sender name or address")
    p.add_argument("--subject", metavar="TEXT", help="text in the subject")
    p.add_argument("--limit", type=int, default=50, help="most messages to show (default 50)")
    p = add("read", "show one message: subject, sender, text, OTP and links (marks it as read)")
    addr_and_id(p)
    p.add_argument("--html", action="store_true", help="print the sanitized HTML instead of the text")
    p = add("wait", "wait for a new message and show it")
    address(p)
    p.add_argument("--from", dest="sender", metavar="TEXT", help="text in the sender name or address")
    p.add_argument("--subject", metavar="TEXT", help="text in the subject")
    p.add_argument("--timeout", type=float, default=120.0, help="seconds to wait (default 120)")
    p.add_argument(
        "--any",
        action="store_true",
        help="also accept a message already in the inbox (default: only messages that arrive after the wait starts)",
    )
    p = add("rm-msg", "delete one message")
    addr_and_id(p)
    yes(p)
    for name, help_ in (
        ("clear", "delete all messages of an address"),
        ("delete", "delete an address"),
    ):
        p = add(name, help_)
        address(p)
        yes(p)
    p = add("token", "print the token of an address (keep it secret)")
    address(p)
    add("shell", "interactive mode")
    add("login", "save your API key to ~/.tmailor (mode 600)")
    yes(add("logout", "remove ~/.tmailor"))
    return parser


def _has_token_source(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "token_stdin", False) or getattr(args, "token_file", None) or getattr(args, "ask_token", False))


def _normalize(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if getattr(args, "token_argv", None) is not None:
        # Review 17f SDK-16: refused (nothing is sent); the token is now in your shell history, consider rotating it on
        # https://platform.tmailor.com.
        parser.error(
            "--token was removed: a token on the command line ends up in shell history and the process list. "
            "Use --token-stdin, --token-file FILE or --ask-token."
        )
    has_token = _has_token_source(args)
    if hasattr(args, "first"):
        if has_token and args.second is None:
            args.email, args.message_id = None, args.first
        elif args.second is None:
            parser.error(f"{args.command}: give EMAIL and MESSAGE_ID (or --token-stdin/--token-file/--ask-token and MESSAGE_ID)")
        else:
            args.email, args.message_id = args.first, args.second
    if hasattr(args, "email") and not getattr(args, "email", None) and not has_token:
        parser.error(f"{args.command}: give EMAIL (or --token-stdin / --token-file FILE / --ask-token)")
    if getattr(args, "timeout", None) is not None and args.timeout < 0:
        parser.error("--timeout must be >= 0")
    if getattr(args, "limit", None) is not None and args.limit < 1:
        parser.error("--limit must be >= 1")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    factory: ClientFactory = default_factory,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    """Run the CLI. Returns the exit code (0 ok, 1 error, 2 usage, 3 wait timeout, 130 interrupted)."""
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(out)
        return EXIT_USAGE
    _normalize(args, parser)
    app = App(args, factory, out, err)
    try:
        return COMMANDS[args.command](app)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=err)
        return EXIT_INTERRUPTED
    # Review 17f SDK-14: error text can come from the API (message, description): cleaned before printing.
    except CliError as exc:
        print(f"{PROG}: {clean(redact(str(exc), app.secrets()))}", file=err)
        return exc.exit_code
    except TmailorError as exc:
        print(f"{PROG}: error: {clean(redact(str(exc), app.secrets()))}", file=err)
        if exc.description and exc.description != exc.message:
            print(f"  {clean(redact(exc.description, app.secrets()))}", file=err)
        if args.debug:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            print(clean(redact(tb, app.secrets())), file=err)
        return EXIT_ERROR
    except Exception as exc:  # unexpected: short message, traceback only with --debug (secrets masked)
        print(f"{PROG}: unexpected error: {clean(redact(f'{type(exc).__name__}: {exc}', app.secrets()))}", file=err)
        if args.debug:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            print(clean(redact(tb, app.secrets())), file=err)
        else:
            print("  Run again with --debug for details.", file=err)
        return EXIT_ERROR
    finally:
        app.close()


def run() -> None:
    """Console script entry point."""
    try:
        code = main()
    except KeyboardInterrupt:  # Ctrl+C during argument parsing or teardown
        print("\nInterrupted.", file=sys.stderr)
        code = EXIT_INTERRUPTED
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    run()
