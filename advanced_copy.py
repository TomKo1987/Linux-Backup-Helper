import concurrent.futures
import os
import re
import stat as _stat_mod
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from drive_utils import is_smb, is_ssh, build_rsync_cmd
from state import apply_replacements, logger
from copy_worker_core import (
    _should_skip, _RSYNC_DELETE_RE, _SMB_WORKERS, _run_futures, _run_capture,
    _rsync_excludes, _rsync_src_arg, _ssh_join,
)
from translations import tr

__all__ = [
    "DeletedItem", "DeleteError", "AdvancedOptionsResult",
    "make_versioned_path", "prune_old_versions", "find_extraneous_paths",
    "delete_paths", "apply_advanced_options", "restore_exclude_paths",
]


_VERSION_RE = re.compile(r"^(\d+) - \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}$")
_SSH_PREVIEW_TIMEOUT_S = 300
_CONFIRM_PREVIEW_MAX = 200

ConfirmFn = Callable[[str, "list[str]"], bool]


class _Cancelled(Exception):
    pass


def _is_cancelled(cancel: "threading.Event | None") -> bool:
    return cancel is not None and cancel.is_set()


@dataclass
class DeletedItem:
    path: str
    title: str
    reason: str
    size: int = 0


@dataclass
class DeleteError:
    path: str
    title: str
    reason: str


@dataclass
class AdvancedOptionsResult:
    tasks: list[tuple]
    deleted: list[DeletedItem] = field(default_factory=list)
    errors: list[DeleteError] = field(default_factory=list)


def _is_local(path: str) -> bool:
    return not is_smb(path) and not is_ssh(path)


def _make_traversable(p: str) -> None:
    if os.access(p, os.R_OK | os.W_OK | os.X_OK):
        return
    try:
        os.chmod(p, _stat_mod.S_IMODE(os.lstat(p).st_mode) | 0o700)
    except OSError:
        pass


def _rmtree_with_size(p: str, cancel: "threading.Event | None" = None) -> int:
    if os.path.islink(p):
        os.remove(p)
        return 0
    if not os.path.isdir(p):
        try:
            sz = os.lstat(p).st_size
        except OSError:
            sz = 0
        os.remove(p)
        return sz

    total = 0
    stack: list[tuple[str, bool]] = [(p, False)]
    while stack:
        current, emptied = stack.pop()
        if emptied:
            os.rmdir(current)
            continue
        if _is_cancelled(cancel):
            raise _Cancelled
        _make_traversable(current)
        stack.append((current, True))
        with os.scandir(current) as it:
            entries = list(it)
        for e in entries:
            if e.is_symlink():
                os.remove(e.path)
            elif e.is_dir(follow_symlinks=False):
                stack.append((e.path, False))
            else:
                try:
                    total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
                os.remove(e.path)
    return total


def _existing_versions(dst_abs: str) -> list[tuple[int, str]]:
    versions: list[tuple[int, str]] = []
    try:
        with os.scandir(dst_abs) as it:
            for e in it:
                if not e.is_dir(follow_symlinks=False):
                    continue
                m = _VERSION_RE.match(e.name)
                if m is not None:
                    versions.append((int(m.group(1)), e.path))
    except OSError:
        return []
    versions.sort(key=lambda v: v[0])
    return versions


def make_versioned_path(dst_abs: str) -> str:
    versions = _existing_versions(dst_abs)
    n = (versions[-1][0] + 1) if versions else 1
    ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    return os.path.join(dst_abs, f"{n:03d} - {ts}")


def prune_old_versions(dst_abs: str, keep: int, title: str = "",
                       cancel: "threading.Event | None" = None) -> tuple[list[DeletedItem], list[DeleteError]]:
    if keep < 0:
        return [], []
    versions = _existing_versions(dst_abs)
    overflow = len(versions) - keep
    if overflow <= 0:
        return [], []
    deleted: list[DeletedItem] = []
    errors: list[DeleteError] = []
    for _, path in versions[:overflow]:
        if _is_cancelled(cancel):
            break
        try:
            sz = _rmtree_with_size(path, cancel)
            deleted.append(DeletedItem(path=path, title=title, reason=tr("Pruned old version"), size=sz))
        except _Cancelled:
            break
        except OSError as exc:
            errors.append(DeleteError(path=path, title=title, reason=tr("Could not delete: {exc}", exc=exc)))
            logger.warning("Versioned archive: could not remove old version %r: %s", apply_replacements(path), exc)
    return deleted, errors


def find_extraneous_paths(src_abs: str, dst_abs: str, excludes: frozenset,
                          cancel: "threading.Event | None" = None) -> list[str]:
    extraneous: list[str] = []
    if not os.path.isdir(src_abs) or not os.path.isdir(dst_abs):
        return extraneous

    join = os.path.join
    stack: list[str] = [""]
    while stack:
        if _is_cancelled(cancel):
            break
        rel = stack.pop()
        d_dir = join(dst_abs, rel) if rel else dst_abs
        try:
            with os.scandir(d_dir) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            if _should_skip(e.name):
                continue
            rel_path = join(rel, e.name) if rel else e.name
            s_path = join(src_abs, rel_path)
            if s_path in excludes:
                continue
            try:
                s_st = os.lstat(s_path)
            except (FileNotFoundError, NotADirectoryError):
                extraneous.append(e.path)
                continue
            except OSError as exc:
                logger.warning("Mirror delete: cannot inspect source %r (%s) — keeping %r",
                               apply_replacements(s_path), exc, apply_replacements(e.path))
                continue
            try:
                dst_is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            src_is_dir = _stat_mod.S_ISDIR(s_st.st_mode)
            if dst_is_dir and src_is_dir:
                stack.append(rel_path)
            elif dst_is_dir != src_is_dir:
                extraneous.append(e.path)

    return extraneous


def delete_paths(paths: list[str], title: str = "", reason: str | None = None,
                 cancel: "threading.Event | None" = None) -> tuple[list[DeletedItem], list[DeleteError]]:
    if reason is None:
        reason = tr("Mirror delete")
    deleted: list[DeletedItem] = []
    errors: list[DeleteError] = []
    for p in paths:
        if _is_cancelled(cancel):
            break
        try:
            if os.path.islink(p):
                os.remove(p)
                sz = 0
            elif os.path.isfile(p):
                sz = os.path.getsize(p)
                os.remove(p)
            elif os.path.isdir(p):
                sz = _rmtree_with_size(p, cancel)
            else:
                continue
            deleted.append(DeletedItem(path=p, title=title, reason=reason, size=sz))
        except _Cancelled:
            break
        except OSError as exc:
            errors.append(DeleteError(path=p, title=title, reason=tr("Could not delete: {exc}", exc=exc)))
            logger.warning("Mirror delete: could not remove %r: %s", apply_replacements(p), exc)
    return deleted, errors


def _abs_excludes(excl, s_norm: str, s_str: str) -> frozenset:
    if not isinstance(excl, dict):
        return frozenset()
    names = excl.get(s_norm) or excl.get(s_str) or []
    return frozenset(os.path.join(s_norm, n) for n in names)


def _exclude_key(path: str) -> str:
    return path if (is_smb(path) or is_ssh(path)) else os.path.abspath(os.path.expanduser(path))


def restore_exclude_paths(entry: dict) -> dict:
    details  = entry.get("details", {}) or {}
    raw_excl = details.get("exclude_paths", {})
    if not isinstance(raw_excl, dict) or not raw_excl:
        return {}

    src_paths = entry.get("source", [])
    dst_paths = entry.get("destination", [])
    if isinstance(src_paths, str):
        src_paths = [src_paths]
    if isinstance(dst_paths, str):
        dst_paths = [dst_paths]

    remapped: dict = {}
    for s, d in zip(src_paths, dst_paths):
        if not s or not d:
            continue
        s_str = str(s)
        names = raw_excl.get(_exclude_key(s_str)) or raw_excl.get(s_str) or []
        if names:
            remapped[_exclude_key(str(d))] = list(names)
    return remapped


def _preview_ssh_mirror_delete(src: str, dst: str, excl,
                               cancel: "threading.Event | None" = None) -> "list[str] | None":
    rsync_src = _rsync_src_arg(src, cancel)
    s_key = _exclude_key(src)
    excludes_abs = _abs_excludes(excl, s_key, src)
    rsync_excludes = _rsync_excludes(rsync_src, list(excludes_abs) if excludes_abs else None)
    cmd = build_rsync_cmd(rsync_src, dst, delete=True, exclude=rsync_excludes, dry_run=True)
    res = _run_capture(cmd, _SSH_PREVIEW_TIMEOUT_S, cancel)
    if res is None:
        logger.warning("SSH mirror delete preview failed for %r \u2192 %r (timeout, cancelled or rsync unavailable)",
                       apply_replacements(src), apply_replacements(dst))
        return None
    returncode, stdout, stderr = res
    if returncode != 0:
        logger.warning(
            "SSH mirror delete preview: rsync --dry-run exited %d for %r \u2192 %r \u2014 %s",
            returncode, apply_replacements(src), apply_replacements(dst),
            (stderr or stdout or "").strip()[:300])
        return None
    deleted: list[str] = []
    for line in stdout.splitlines():
        m = _RSYNC_DELETE_RE.match(line.strip())
        if m:
            deleted.append(_ssh_join(dst, m.group(1).strip()))
    return deleted


def _resolve_ssh_mirror_delete(ssh_pairs: list[tuple[str, str]], excl, title: str,
                               confirm: "ConfirmFn | None", confirm_del: bool,
                               errors: list[DeleteError],
                               cancel: "threading.Event | None") -> bool:
    if not confirm_del or confirm is None:
        return True

    def _unverifiable(_s_str: str, _d_str: str) -> bool:
        if _is_cancelled(cancel):
            return False
        logger.warning(
            "Mirror delete [%s]: could not preview remote deletions for %r \u2192 %r "
            "\u2014 skipping remote --delete for this backup entry for safety",
            title, apply_replacements(_s_str), apply_replacements(_d_str))
        errors.append(DeleteError(
            path=apply_replacements(_d_str), title=title,
            reason=tr("Mirror delete skipped: the remote deletions could not be previewed")))
        return False

    if len(ssh_pairs) == 1:
        s_str, d_str = ssh_pairs[0]
        preview = _preview_ssh_mirror_delete(s_str, d_str, excl, cancel)
        if preview is None:
            return _unverifiable(s_str, d_str)
        all_deleted = preview
    else:
        results: dict[int, "list[str] | None"] = {}
        results_lock = threading.Lock()
        stop = threading.Event()

        def _run_one(idx: int, _s_str: str, _d_str: str) -> None:
            _preview = _preview_ssh_mirror_delete(_s_str, _d_str, excl, cancel)
            with results_lock:
                results[idx] = _preview
            if _preview is None:
                stop.set()

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(_SMB_WORKERS, len(ssh_pairs))) as pool:
            futs = [pool.submit(_run_one, i, s, d) for i, (s, d) in enumerate(ssh_pairs)]
            _run_futures(futs, stop, "ssh mirror preview")

        all_deleted = []
        for i, (s_str, d_str) in enumerate(ssh_pairs):
            preview = results.get(i)
            if preview is None:
                return _unverifiable(s_str, d_str)
            all_deleted.extend(preview)

    if _is_cancelled(cancel):
        return False
    if not all_deleted:
        return True

    if confirm(title, all_deleted):
        return True

    logger.info("Mirror delete [%s]: remote deletion cancelled by user", title)
    return False


def apply_advanced_options(tasks: list[tuple], *, confirm: "ConfirmFn | None" = None,
                           is_restore: bool = False,
                           cancel: "threading.Event | None" = None) -> AdvancedOptionsResult:
    result = []
    all_deleted: list[DeletedItem] = []
    all_errors: list[DeleteError] = []

    for src_list, dst_list, title, excl, pre_hooks, post_hooks, details in tasks:
        if _is_cancelled(cancel):
            break
        details = details or {}
        src_list = [src_list] if isinstance(src_list, str) else list(src_list)
        dst_list = [dst_list] if isinstance(dst_list, str) else list(dst_list)
        versioned = bool(details.get("versioned_archive")) and not is_restore
        mirror = bool(details.get("mirror_delete")) and not versioned and not is_restore
        if is_restore and details.get("versioned_archive"):
            logger.info(
                "Versioned archive [%s]: skipped during Restore (only applies to the "
                "backup destination) — restoring directly instead", title)
        if is_restore and details.get("mirror_delete") and not details.get("versioned_archive"):
            logger.info(
                "Mirror delete [%s]: skipped during Restore (only applies to the "
                "backup destination) — restoring without deleting local files", title)
        confirm_del = bool(details.get("confirm_before_delete", True))
        try:
            max_versions = int(details.get("max_versions") or 0)
        except (TypeError, ValueError):
            max_versions = 0

        eff_dst = list(dst_list)
        mirror_remote = False
        ssh_mirror_pairs: list[tuple[str, str]] = []

        if len(src_list) != len(dst_list):
            logger.warning(
                "apply_advanced_options [%s]: source/destination count mismatch "
                "(%d source(s) vs %d destination(s)) — only the first %d pair(s) "
                "will be considered for advanced options; the copy step will skip "
                "this entry entirely",
                title, len(src_list), len(dst_list), min(len(src_list), len(dst_list)))

        for i, (s, d) in enumerate(zip(src_list, dst_list)):
            if _is_cancelled(cancel):
                break
            s_str, d_str = str(s), str(d)
            s_local, d_local = _is_local(s_str), _is_local(d_str)

            if versioned:
                if d_local:
                    d_abs = os.path.abspath(os.path.expanduser(d_str))
                    try:
                        os.makedirs(d_abs, exist_ok=True)
                        if max_versions > 0:
                            pruned, prune_errs = prune_old_versions(d_abs, max_versions - 1, title=title,
                                                                    cancel=cancel)
                            all_deleted.extend(pruned)
                            all_errors.extend(prune_errs)
                        eff_dst[i] = make_versioned_path(d_abs)
                    except OSError as exc:
                        logger.warning(
                            "Versioned archive [%s]: could not prepare %r — keeping original destination (%s)",
                            title, apply_replacements(d_abs), exc)
                else:
                    logger.info(
                        "Versioned archive [%s]: destination %r is remote — skipped (local only)",
                        title, apply_replacements(d_str))
                continue

            if not mirror:
                continue

            if s_local and d_local:
                s_abs = os.path.abspath(os.path.expanduser(s_str))
                d_abs = os.path.abspath(os.path.expanduser(d_str))
                if not os.path.isdir(s_abs):
                    if not os.path.exists(s_abs):
                        logger.warning(
                            "Mirror delete [%s]: source %r missing — skipping cleanup for safety",
                            title, apply_replacements(s_abs))
                    continue
                try:
                    with os.scandir(s_abs) as _probe:
                        source_empty = next(_probe, None) is None
                except OSError as exc:
                    logger.warning("Mirror delete [%s]: source %r unreadable (%s) — skipping cleanup for safety",
                                   title, apply_replacements(s_abs), exc)
                    continue
                if source_empty:
                    logger.warning("Mirror delete [%s]: source %r is empty (unmounted drive?) — "
                                   "skipping cleanup for safety", title, apply_replacements(s_abs))
                    continue
                excludes = _abs_excludes(excl, s_abs, s_str)
                extraneous = find_extraneous_paths(s_abs, d_abs, excludes, cancel)
                if not extraneous or _is_cancelled(cancel):
                    continue
                proceed = True
                if confirm_del and confirm is not None:
                    proceed = confirm(title, extraneous)
                if proceed and not _is_cancelled(cancel):
                    items, errs = delete_paths(extraneous, title=title, reason=tr("Mirror delete"), cancel=cancel)
                    all_deleted.extend(items)
                    all_errors.extend(errs)
                    logger.info("Mirror delete [%s]: removed %d item(s) from %r",
                                title, len(items), apply_replacements(d_abs))
                    for err in errs:
                        logger.warning("Mirror delete [%s]: %s: %s", title, apply_replacements(err.path), err.reason)
                else:
                    logger.info("Mirror delete [%s]: deletion cancelled by user", title)
            elif is_ssh(s_str) or is_ssh(d_str):
                ssh_mirror_pairs.append((s_str, d_str))
            else:
                logger.info("Mirror delete [%s]: SMB destinations are not supported — skipped", title)

        if ssh_mirror_pairs and not _is_cancelled(cancel):
            mirror_remote = _resolve_ssh_mirror_delete(
                ssh_mirror_pairs, excl, title, confirm, confirm_del, all_errors, cancel)

        result.append((src_list, eff_dst, title, excl, pre_hooks, post_hooks, mirror_remote))

    return AdvancedOptionsResult(tasks=result, deleted=all_deleted, errors=all_errors)
