import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from drive_utils import is_smb, is_ssh, build_rsync_cmd
from state import apply_replacements, logger
from copy_worker_core import _SKIP_RE

__all__ = [
    "DeletedItem", "DeleteError", "AdvancedOptionsResult",
    "make_versioned_path", "prune_old_versions", "find_extraneous_paths",
    "delete_paths", "apply_advanced_options", "restore_exclude_paths",
]

_VERSION_RE = re.compile(r"^(\d+)\s*[-_]\s*")
_RSYNC_DELETE_RE = re.compile(r"^deleting\s+(.+)$")
_SSH_PREVIEW_TIMEOUT_S = 30


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


def _path_size(p: str) -> int:
    try:
        if os.path.islink(p) or os.path.isfile(p):
            return os.path.getsize(p)
        if os.path.isdir(p):
            total = 0
            for root, _dirs, files in os.walk(p, followlinks=False):
                for fname in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fname))
                    except OSError:
                        pass
            return total
    except OSError:
        pass
    return 0


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
    except (FileNotFoundError, PermissionError, OSError):
        return []
    versions.sort(key=lambda v: v[0])
    return versions


def make_versioned_path(dst_abs: str) -> str:
    versions = _existing_versions(dst_abs)
    n = (versions[-1][0] + 1) if versions else 1
    ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    return os.path.join(dst_abs, f"{n:03d} - {ts}")


def prune_old_versions(dst_abs: str, keep: int, title: str = "") -> tuple[list[DeletedItem], list[DeleteError]]:
    if keep <= 0:
        return [], []
    versions = _existing_versions(dst_abs)
    overflow = len(versions) - keep
    if overflow <= 0:
        return [], []
    deleted: list[DeletedItem] = []
    errors: list[DeleteError] = []
    for _, path in versions[:overflow]:
        sz = _path_size(path)
        try:
            shutil.rmtree(path)
            deleted.append(DeletedItem(path=path, title=title, reason="Pruned old version", size=sz))
        except OSError as exc:
            errors.append(DeleteError(path=path, title=title, reason=f"Could not delete: {exc}"))
            logger.warning("Versioned archive: could not remove old version %r: %s", apply_replacements(path), exc)
    return deleted, errors


def find_extraneous_paths(src_abs: str, dst_abs: str, excludes: frozenset) -> list[str]:
    extraneous: list[str] = []
    if not os.path.isdir(src_abs) or not os.path.isdir(dst_abs):
        return extraneous

    def _walk(rel: str) -> None:
        d_dir = os.path.join(dst_abs, rel) if rel else dst_abs
        try:
            entries = list(os.scandir(d_dir))
        except (PermissionError, FileNotFoundError, OSError):
            return
        for e in entries:
            if _SKIP_RE.search(e.name):
                continue
            rel_path = os.path.join(rel, e.name) if rel else e.name
            s_path = os.path.join(src_abs, rel_path)
            if s_path in excludes:
                continue
            if not os.path.lexists(s_path):
                extraneous.append(e.path)
                continue
            dst_is_dir = e.is_dir(follow_symlinks=False)
            src_is_dir = os.path.isdir(s_path) and not os.path.islink(s_path)
            if dst_is_dir and src_is_dir:
                _walk(rel_path)
            elif dst_is_dir != src_is_dir:
                extraneous.append(e.path)

    _walk("")
    return extraneous


def delete_paths(paths: list[str], title: str = "", reason: str = "Mirror delete") -> tuple[list[DeletedItem], list[DeleteError]]:
    deleted: list[DeletedItem] = []
    errors: list[DeleteError] = []
    for p in paths:
        sz = _path_size(p)
        try:
            if os.path.islink(p) or os.path.isfile(p):
                os.remove(p)
            elif os.path.isdir(p):
                shutil.rmtree(p)
            else:
                continue
            deleted.append(DeletedItem(path=p, title=title, reason=reason, size=sz))
        except OSError as exc:
            errors.append(DeleteError(path=p, title=title, reason=f"Could not delete: {exc}"))
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


def _confirm(parent, title: str, paths: list[str]) -> bool:
    from PyQt6.QtWidgets import QMessageBox
    shown = paths[:25]
    preview = "\n".join(f"  \u2022  {apply_replacements(p)}" for p in shown)
    more = f"\n  \u2026and {len(paths) - 25} more" if len(paths) > 25 else ""
    clean_title = title.replace("<br>", " ")
    msg = (f"Mirror mode is about to delete {len(paths)} item(s) from the destination of "
          f"'{clean_title}' because they no longer exist in the source:\n\n{preview}{more}\n\n"
          f"Delete these now?")
    return QMessageBox.question(
        parent, "Confirm Mirror Delete", msg,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    ) == QMessageBox.StandardButton.Yes


def _preview_ssh_mirror_delete(src: str, dst: str, excl) -> "list[str] | None":
    from copy_worker import _rsync_excludes, _rsync_src_arg, _ssh_join

    rsync_src = _rsync_src_arg(src)
    excludes_abs = _abs_excludes(excl, src, src)
    rsync_excludes = _rsync_excludes(rsync_src, list(excludes_abs) if excludes_abs else None)
    cmd = build_rsync_cmd(rsync_src, dst, delete=True, exclude=rsync_excludes, dry_run=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_SSH_PREVIEW_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("SSH mirror delete preview failed for %r \u2192 %r: %s",
                        apply_replacements(src), apply_replacements(dst), exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "SSH mirror delete preview: rsync --dry-run exited %d for %r \u2192 %r \u2014 %s",
            proc.returncode, apply_replacements(src), apply_replacements(dst),
            (proc.stderr or proc.stdout or "").strip()[:300])
        return None
    deleted: list[str] = []
    for line in (proc.stdout or "").splitlines():
        m = _RSYNC_DELETE_RE.match(line.strip())
        if m:
            deleted.append(_ssh_join(dst, m.group(1).strip()))
    return deleted


def _resolve_ssh_mirror_delete(ssh_pairs: list[tuple[str, str]], excl, title: str,
                               confirm_del: bool, interactive: bool, parent) -> bool:

    if not confirm_del or not interactive:
        return True

    all_deleted: list[str] = []
    for s_str, d_str in ssh_pairs:
        preview = _preview_ssh_mirror_delete(s_str, d_str, excl)
        if preview is None:
            logger.warning(
                "Mirror delete [%s]: could not preview remote deletions for %r \u2192 %r "
                "\u2014 skipping remote --delete for this backup entry for safety",
                title, apply_replacements(s_str), apply_replacements(d_str))
            return False
        all_deleted.extend(preview)

    if not all_deleted:
        return True

    if _confirm(parent, title, all_deleted):
        return True

    logger.info("Mirror delete [%s]: remote deletion cancelled by user", title)
    return False


def apply_advanced_options(tasks: list[tuple], *, interactive: bool = True, parent=None,
                          is_restore: bool = False) -> AdvancedOptionsResult:
    result = []
    all_deleted: list[DeletedItem] = []
    all_errors: list[DeleteError] = []

    for src_list, dst_list, title, excl, pre_hooks, post_hooks, details in tasks:
        details = details or {}
        versioned = bool(details.get("versioned_archive")) and not is_restore
        mirror = bool(details.get("mirror_delete")) and not versioned
        if is_restore and details.get("versioned_archive"):
            logger.info(
                "Versioned archive [%s]: skipped during Restore (only applies to the "
                "backup destination) — restoring directly instead", title)
        confirm_del = bool(details.get("confirm_before_delete", True))
        try:
            max_versions = int(details.get("max_versions") or 0)
        except (TypeError, ValueError):
            max_versions = 0

        eff_dst = list(dst_list)
        mirror_remote = False
        ssh_mirror_pairs: list[tuple[str, str]] = []

        for i, (s, d) in enumerate(zip(src_list, dst_list)):
            s_str, d_str = str(s), str(d)
            s_local, d_local = _is_local(s_str), _is_local(d_str)

            if versioned:
                if d_local:
                    d_abs = os.path.abspath(os.path.expanduser(d_str))
                    try:
                        os.makedirs(d_abs, exist_ok=True)
                        if max_versions > 0:
                            pruned, prune_errs = prune_old_versions(d_abs, max_versions - 1, title=title)
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

            if mirror:
                if s_local and d_local:
                    s_abs = os.path.abspath(os.path.expanduser(s_str))
                    d_abs = os.path.abspath(os.path.expanduser(d_str))
                    if not os.path.isdir(s_abs):
                        logger.warning(
                            "Mirror delete [%s]: source %r missing — skipping cleanup for safety",
                            title, apply_replacements(s_abs))
                        continue
                    excludes = _abs_excludes(excl, s_abs, s_str)
                    extraneous = find_extraneous_paths(s_abs, d_abs, excludes)
                    if not extraneous:
                        continue
                    proceed = True
                    if confirm_del and interactive:
                        proceed = _confirm(parent, title, extraneous)
                    if proceed:
                        items, errs = delete_paths(extraneous, title=title, reason="Mirror delete")
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

        if ssh_mirror_pairs:
            mirror_remote = _resolve_ssh_mirror_delete(
                ssh_mirror_pairs, excl, title, confirm_del, interactive, parent)

        result.append((src_list, eff_dst, title, excl, pre_hooks, post_hooks, mirror_remote))

    return AdvancedOptionsResult(tasks=result, deleted=all_deleted, errors=all_errors)
