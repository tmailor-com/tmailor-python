# CLI config and token files

The `tmailor` CLI stores your API key in a config file (`~/.tmailor`, or the file named by `TMAILOR_CONFIG`) and
can read an address token from a file (`--token-file FILE`). Both files hold secrets, so the CLI checks them
before reading. This page describes those checks. The Python SDK itself never reads these files.

## POSIX (Linux, macOS, BSD)

- The file must be a regular file, not a symbolic link, owned by you, with mode `600` or stricter.
- Every directory in its path must be a real directory (not a symbolic link; give the real path), owned by you or
  root, and not writable by others unless it has the sticky bit. When you run as root, the directory that holds
  the file must not be writable by others at all.
- The checks run on the open directory descriptors that are then used, so nothing can be swapped in between.
- `tmailor login` writes the config file atomically: it creates a random temporary file with mode `600`, calls
  `fsync`, and renames it into place. If anything fails before the rename, the temporary file is removed.
- The CLI needs `openat`-style calls (`dir_fd` for open, stat, unlink, and rename), `O_NOFOLLOW`, and
  `O_DIRECTORY`. If any of them is missing, the CLI fails closed ("platform not supported for config/token
  files") and never falls back to path-based operations. On such platforms, use `TMAILOR_API_KEY` and
  `--token-stdin`, which keep working. The default `~/.tmailor` is skipped. If the CLI would otherwise read an
  explicitly set `TMAILOR_CONFIG`, it refuses to use that file.

## Windows

- The file must be inside your user profile.
- The CLI works with paths, but it refuses any symbolic link, junction, or other reparse point in the file or in
  any directory below the profile, and it checks that the file it opened is the file it checked.
- Owner and mode checks are POSIX-only. On Windows, the protection comes from the profile's default ACL: only
  you, SYSTEM, and Administrators can write to it, so no other unprivileged user can swap entries.

## Threat model

These checks stop other local users from planting or swapping a config or token file. A program already running
as your user is out of scope, because it could read the file anyway.

If a check fails, the CLI refuses to use the file. Supply the key through `TMAILOR_API_KEY` and the token
through `--token-stdin` or `--ask-token` instead.
