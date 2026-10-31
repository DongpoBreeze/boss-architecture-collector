"""Read live checkpoints and validate their task-local paths safely."""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path, PurePath, PureWindowsPath
import threading
import time
from typing import Any, BinaryIO


# Five seconds covers longer transient file inspection. Each interval remains
# short so a collector's stop event can end the retry promptly.
REPLACE_RETRY_DELAYS = (.01, .02, .04, .08, .15, .2) + (.25,) * 18
PATH_CHECK_RETRY_DELAYS = (.01, .02, .04)


class PathBoundaryError(ValueError):
    """A lexical or resolved path is outside its permitted directory."""


class PathCheckError(OSError):
    """The path could not be checked reliably; callers must not access it."""


def _comparison_path(path: PurePath) -> PurePath:
    """Fold only equivalent DOS/UNC namespaces, never arbitrary devices.

    Windows realpath can retain its extended prefix if a second lookup races
    with an atomic replacement. PureWindowsPath supplies case-insensitive,
    component-wise comparison without another filesystem lookup.
    """
    if not isinstance(path, PureWindowsPath):
        if not path.is_absolute():
            raise ValueError("not an absolute path")
        return path
    value = str(path)
    if value[:8].casefold() == "\\\\?\\unc\\":
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
        if len(value) < 3 or value[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" or value[1:3] != ":\\":
            raise ValueError("unsupported path namespace")
    key = PureWindowsPath(value)
    drive = key.drive
    if len(drive) == 2 and drive[0] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" and drive[1] == ":":
        anchor_parts = ()
    elif drive.startswith("\\\\"):
        anchor_parts = tuple(drive[2:].split("\\"))
        if len(anchor_parts) != 2 or any(part in {"", ".", "..", "?"} for part in anchor_parts):
            raise ValueError("unsupported path namespace")
    else:
        raise ValueError("unsupported path namespace")
    if not key.is_absolute():
        raise ValueError("not an absolute path")
    # Stripping a prefix is not equivalent for DOS devices, ADS, or components
    # whose trailing dots/spaces Win32 would trim. Refuse those ambiguous forms.
    for part in (*anchor_parts, *key.parts[1:]):
        if part != ".." and (part.endswith((".", " ")) or any(char in part for char in '\x00<>:"|?*')
                             or PureWindowsPath(part).is_reserved()):
            raise ValueError("unsupported path component")
    return key


def _resolve_output_path(path: Path) -> Path:
    """Resolve strictly, allowing only a genuinely absent output tail."""
    current, missing = path, []
    while True:
        try:
            resolved = current.resolve(strict=True)
        except FileNotFoundError:
            # A dangling link or an existing but unresolvable entry is not a
            # missing output. Do not fall back after a failed access check.
            try:
                current.lstat()
            except FileNotFoundError:
                if current.parent == current:
                    raise
                missing.append(current.name)
                current = current.parent
                continue
            raise
        if missing and not current.is_dir():
            raise NotADirectoryError("output parent is not a directory")
        return resolved.joinpath(*reversed(missing))


def confined_path(path: Path, base: Path) -> tuple[PurePath, Path]:
    """Return the lexical relative path and resolved path after both checks.

    Reparse-point inspection remains the caller's responsibility, including
    links inside the boundary. Missing outputs are allowed, not missing bases.
    Only transient Windows lookup/sharing errors receive a short bounded retry.
    """
    for attempt in range(len(PATH_CHECK_RETRY_DELAYS) + 1):
        try:
            path_key, base_key = _comparison_path(path.absolute()), _comparison_path(base.absolute())
            try:
                relative = path_key.relative_to(base_key)
            except ValueError:
                raise PathBoundaryError("path outside boundary") from None
            if ".." in relative.parts:
                raise PathBoundaryError("parent traversal outside boundary")
            resolved = _resolve_output_path(path)
            resolved_key = _comparison_path(resolved)
            resolved_base_key = _comparison_path(base.resolve(strict=True))
            if ".." in resolved_key.parts or ".." in resolved_base_key.parts:
                raise ValueError("incomplete path resolution")
            try:
                resolved_key.relative_to(resolved_base_key)
            except ValueError:
                raise PathBoundaryError("resolved path outside boundary") from None
            return relative, resolved
        except PathBoundaryError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            if (os.name == "nt" and isinstance(exc, OSError)
                    and getattr(exc, "winerror", None) in {2, 3, 5, 32, 33}
                    and attempt < len(PATH_CHECK_RETRY_DELAYS)):
                time.sleep(PATH_CHECK_RETRY_DELAYS[attempt])
                continue
            raise PathCheckError("path resolution or inspection failed") from None


@lru_cache(maxsize=1)
def _windows_file_api():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.ReplaceFileW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
    kernel.ReplaceFileW.restype = wintypes.BOOL
    return kernel


def open_shared_read(path: str | Path) -> BinaryIO:
    """Open an existing file for reading, sharing read/write/delete access.

    The handle sees one complete snapshot even if another process replaces the
    pathname while it is open. Only GENERIC_READ is requested; this neither
    changes file permissions nor bypasses an existing exclusive handle.
    """
    if os.name != "nt":
        return Path(path).open("rb")

    import ctypes
    import msvcrt

    kernel = _windows_file_api()
    handle = kernel.CreateFileW(os.path.abspath(path), 0x80000000, 0x1 | 0x2 | 0x4,
                                None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # The descriptor takes ownership of the native handle on success.
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def read_text(path: str | Path, encoding: str = "utf-8-sig") -> str:
    # Close the OS handle before decoding or parsing potentially large JSON.
    with open_shared_read(path) as stream:
        payload = stream.read()
    return payload.decode(encoding)


def read_json(path: str | Path) -> Any:
    return json.loads(read_text(path))


def _replace_once(source: str | Path, destination: str | Path) -> None:
    if os.name == "nt":
        import ctypes

        # CPython's os.replace uses MoveFileExW, which still fails with error 5
        # for an open destination on Windows. ReplaceFileW supports our readers'
        # FILE_SHARE_DELETE handles and preserves the destination's ACLs.
        kernel = _windows_file_api()
        if kernel.ReplaceFileW(os.path.abspath(destination), os.path.abspath(source), None, 0, None, None):
            return
        error = ctypes.get_last_error()
        if error not in {2, 3}:
            raise ctypes.WinError(error)
        # ReplaceFileW requires an existing destination. First publication uses
        # a rename; a racing creation is handled by the next bounded retry.
    os.replace(source, destination)


def replace_with_retry(source: str | Path, destination: str | Path, *,
                       stop_event: threading.Event | None = None) -> None:
    """Atomically replace once, then retry sharing errors for at most 5 s.

    Even a stopped collector attempts one publication to preserve completed
    work. A stop only cancels retrying a failed replacement; the last OS error
    still propagates so an unpublished checkpoint is never reported as saved.
    """
    for attempt in range(len(REPLACE_RETRY_DELAYS) + 1):
        try:
            _replace_once(source, destination)
            return
        except OSError as exc:
            if (not isinstance(exc, PermissionError)
                    and getattr(exc, "winerror", None) not in {5, 32, 33}) or attempt == len(REPLACE_RETRY_DELAYS):
                raise
            delay = REPLACE_RETRY_DELAYS[attempt]
            if stop_event is not None:
                if stop_event.wait(delay):
                    raise
            else:
                time.sleep(delay)
