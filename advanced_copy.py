import os
import re
import shutil
from datetime import datetime

from drive_utils import is_smb, is_ssh
from state import apply_replacements, logger

__all__ = [
    "make_versioned_path", "prune_old_versions", "find_extraneous_paths",
    "delete_paths", "apply_advanced_options",
]

_VERSION_RE = re.compile(r"^(\d+)\s*[-_]\s*")


def _is_local(path: str) -> bool:
    return not is_smb(path) and not is_ssh(path)


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


def prune_old_versions(dst_abs: str, keep: int) -> list[str]:
    if keep <= 0:
        return []
    versions = _existing_versions(dst_abs)
    overflow = len(versions) - keep
    if overflow <= 0:
        return []
    removed = []
    for _, path in versions[:overflow]:
        try:
            shutil.rmtree(path)
            removed.append(path)
        except OSError as exc:
            logger.warning("Versioned archive: could not remove old version %r: %s", apply_replacements(path), exc)
    return removed


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
            rel_path = os.path.join(rel, e.name) if rel else e.name
            s_path = os.path.join(src_abs, rel_path)
            if s_path in excludes:
                continue
            if not os.path.lexists(s_path):
                extraneous.append(e.path)
            elif e.is_dir(follow_symlinks=False) and os.path.isdir(s_path) and not os.path.islink(s_path):
                _walk(rel_path)

    _walk("")
    return extraneous


def delete_paths(paths: list[str]) -> tuple[int, list[str]]:
    deleted = 0
    errors: list[str] = []
    for p in paths:
        try:
            if os.path.islink(p) or os.path.isfile(p):
                os.remove(p)
            elif os.path.isdir(p):
                shutil.rmtree(p)
            else:
                continue
            deleted += 1
        except OSError as exc:
            errors.append(f"{apply_replacements(p)}: {exc}")
            logger.warning("Mirror delete: could not remove %r: %s", apply_replacements(p), exc)
    return deleted, errors


def _abs_excludes(excl, s_norm: str, s_str: str) -> frozenset:
    if not isinstance(excl, dict):
        return frozenset()
    names = excl.get(s_norm) or excl.get(s_str) or []
    return frozenset(os.path.join(s_norm, n) for n in names)


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


def apply_advanced_options(tasks: list[tuple], *, interactive: bool = True, parent=None) -> list[tuple]:
    result = []
    for src_list, dst_list, title, excl, pre_hooks, post_hooks, details in tasks:
        details = details or {}
        versioned = bool(details.get("versioned_archive"))
        mirror = bool(details.get("mirror_delete")) and not versioned
        confirm_del = bool(details.get("confirm_before_delete", True))
        try:
            max_versions = int(details.get("max_versions") or 0)
        except (TypeError, ValueError):
            max_versions = 0

        eff_dst = list(dst_list)
        mirror_remote = False

        for i, (s, d) in enumerate(zip(src_list, dst_list)):
            s_str, d_str = str(s), str(d)
            s_local, d_local = _is_local(s_str), _is_local(d_str)

            if versioned:
                if d_local:
                    d_abs = os.path.abspath(os.path.expanduser(d_str))
                    try:
                        os.makedirs(d_abs, exist_ok=True)
                        if max_versions > 0:
                            prune_old_versions(d_abs, max_versions - 1)
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
                        n, errors = delete_paths(extraneous)
                        logger.info("Mirror delete [%s]: removed %d item(s) from %r",
                                    title, n, apply_replacements(d_abs))
                        for err in errors:
                            logger.warning("Mirror delete [%s]: %s", title, err)
                    else:
                        logger.info("Mirror delete [%s]: deletion cancelled by user", title)
                elif is_ssh(s_str) or is_ssh(d_str):
                    mirror_remote = True
                else:
                    logger.info("Mirror delete [%s]: SMB destinations are not supported — skipped", title)

        result.append((src_list, eff_dst, title, excl, pre_hooks, post_hooks, mirror_remote))
    return result
