import html as _html
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

from linux_distro_helper import LinuxDistroHelper
from state import S, apply_replacements, logger, register_invalidate_hook
from themes import current_theme, font_sz


_cache: Optional[tuple[dict, dict, dict]] = None
_cache_lock = threading.Lock()

_session_lock = threading.Lock()
_cached_session: str = ""
_session_detected: bool = False


def _reset_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None

register_invalidate_hook(_reset_cache)


def _entry_tooltip_html(
    title: str,
    src_lines: list,
    dst_lines: list,
    bg: str,
    bg2: str,
    bg3: str,
    c_title: str,
    c_data: str,
    font_sz_fn,
) -> str:
    s_html, d_html = (
        "<br/>".join(
            _html.escape(apply_replacements(str(p))) for p in lines
        )
        for lines in (src_lines, dst_lines)
    )
    safe_title = _html.escape(title).replace("&lt;br&gt;", "<br/>")
    label_style = (
        f"color:{c_title}; font-weight: bold; border: 5px solid {c_title}; margin-bottom: 5px;"
    )
    cell_padding = "padding:6px;"
    return (
        f"<table style='width: 100%; font-family: monospace; white-space: nowrap; border: 5px solid {bg};'>"
        f"<tr style='background-color: {bg};'>"
        f"<td colspan='2' style='font-size: {font_sz_fn(-2)}px; color: {c_title}; text-align: center'>"
        f"<b>{safe_title}</b></td></tr><tr>"
        f"<td style='background-color: {bg2}; font-size: {font_sz_fn(-3)}px; color: {c_data}; line-height: 1.4; "
        f"{cell_padding} vertical-align: top; white-space: nowrap'>"
        f"<span style='{label_style};'>Source:</span><br>{s_html}</td>"
        f"<td style='background-color: {bg3}; font-size: {font_sz_fn(-3)}px; color: {c_data}; line-height: 1.4; "
        f"{cell_padding} vertical-align: top; white-space: nowrap'>"
        f"<span style='{label_style}'>Destination:</span><br>{d_html}</td>"
        f"</tr></table>"
    )


def _sysfiles_tooltip_html(sys_files: list, t: dict, font_sz_fn) -> str:
    cols = 2 if len(sys_files) > 8 else 1
    header = (
        f"<tr><td colspan='{cols}' style='padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;"
        f"font-weight:bold;white-space:nowrap;color:{t['accent2']};border-bottom:1px solid {t['header_sep']}'>"
        f"System Files ({len(sys_files)})</td></tr>"
    )
    cells = []
    for sf in sys_files:
        src = sf.get("source", "")
        dst = sf.get("destination", "")
        cells.append(
            f"<td style='padding:4px 6px;border:1px solid {t['header_sep']};white-space:nowrap;vertical-align:top;'>"
            f"<span style='color:{t['accent2']};font-weight:bold;'>{_html.escape(Path(src).name)}</span><br>"
            f"<span style='font-size:{font_sz_fn(-3)}px;color:{t['success']};'>"
            f"{_html.escape(apply_replacements(src))}<br>⤵<br>"
            f"{_html.escape(apply_replacements(dst))}</span></td>"
        )
    rows = [
        f"<tr style='background-color:{t['bg2'] if (i // cols) % 2 == 0 else t['bg3']};'>"
        f"{''.join(cells[i:i + cols])}</tr>"
        for i in range(0, len(cells), cols)
    ]
    return (
        f"<table style='white-space:nowrap; font-family:monospace;font-size:{font_sz_fn(-2)}px;'>"
        f"{header}{''.join(rows)}</table>"
    )


def _packages_tooltip_html(
    label: str, pkg_names: list, t: dict, font_sz_fn
) -> str:
    cols = 8 if len(pkg_names) > 25 else 5
    header = (
        f"<tr><td colspan='{cols}' style="
        f"'padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;font-weight:bold;color:{t['accent2']};"
        f"border-bottom:1px solid {t['header_sep']};'>{label} ({len(pkg_names)})</td></tr>"
    )
    rows = []
    for i in range(0, len(pkg_names), cols):
        cells = "".join(
            f"<td style='padding:5px;border:1px solid {t['header_sep']};color:{t['success']};white-space:nowrap'>{p}</td>"
            for p in pkg_names[i : i + cols]
        )
        rows.append(
            f"<tr style='background-color:{t['bg2'] if (i // cols) % 2 == 0 else t['bg3']};'>{cells}</tr>"
        )
    return (
        f"<table style='white-space:nowrap; font-family:monospace;font-size:{font_sz_fn(-2)}px;'>"
        f"{header}{''.join(rows)}</table>"
    )


def _specific_pkgs_tooltip_html(
    sp_active: list, session: Optional[str], t: dict, font_sz_fn
) -> str:
    sp_groups: dict[str, list[str]] = defaultdict(list)
    for p in sp_active:
        sp_groups[p.get("session", "?")].append(_html.escape(p.get("package", "")))

    rows, cols, show_sess_hdr = [], 5, len(sp_groups) > 1
    for i, sess in enumerate(sorted(sp_groups)):
        if show_sess_hdr:
            rows.append(
                f"<tr style='background-color:{t['bg'] if i % 2 == 0 else t['bg2']};'>"
                f"<td colspan='{cols}' style="
                f"'padding:3px 5px;font-size:{font_sz_fn(-2)}px;font-weight:bold;color:{t['accent2']};"
                f"white-space:nowrap;border-bottom:1px solid {t['header_sep']};'>"
                f"{_html.escape(sess)}</td></tr>"
            )
        for j in range(0, len(sp_groups[sess]), cols):
            cells = "".join(
                f"<td style='padding:5px;border:1px solid {t['header_sep']};color:{t['success']};'>{p}</td>"
                for p in sp_groups[sess][j : j + cols]
            )
            rows.append(
                f"<tr style='background-color:{t['bg2'] if (j // cols) % 2 == 0 else t['bg3']};'>{cells}</tr>"
            )

    header = (
        f"<tr><td colspan='{cols}' style='padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;font-weight:bold;"
        f"color:{t['accent2']};border-bottom:1px solid {t['header_sep']};'>Specific Packages "
        f"for {_html.escape(session or 'current session')} ({len(sp_active)})</td></tr>"
    )
    return (
        f"<table style='font-family:monospace;font-size:{font_sz_fn(-2)}px; white-space:nowrap'>"
        f"{header}{''.join(rows)}</table>"
    )


def generate_tooltip() -> tuple[dict, dict, dict]:
    global _cache, _cached_session, _session_detected

    with _cache_lock:
        if _cache is not None:
            return _cache

    local_session = ""
    with _session_lock:
        already_detected = _session_detected
    if not already_detected:
        try:
            local_session = LinuxDistroHelper.detect_session() or ""
        except Exception as e:
            logger.warning("Session detect failed: %s", e)

    with _session_lock:
        if not _session_detected:
            _cached_session = local_session
            _session_detected = True
        session = _cached_session if _cached_session else None

    t = current_theme()
    backup_tips = {
        e["title"]: _entry_tooltip_html(
            e["title"],
            e.get("source", []),
            e.get("destination", []),
            t["bg"], t["bg2"], t["bg3"],
            t["accent2"], t["success"],
            font_sz,
        )
        for e in S.entries
    }
    restore_tips = {
        e["title"]: _entry_tooltip_html(
            e["title"],
            e.get("destination", []),
            e.get("source", []),
            t["bg"], t["bg2"], t["bg3"],
            t["accent2"], t["success"],
            font_sz,
        )
        for e in S.entries
    }
    sm_tips: dict = {}

    active_sys_files = [
        f for f in (S.system_files or [])
        if isinstance(f, dict) and not f.get("disabled")
    ]
    if active_sys_files:
        sm_tips["copy_system_files"] = _sysfiles_tooltip_html(active_sys_files, t, font_sz)

    for key, pkgs, label in [
        ("install_basic_packages",  S.basic_packages, "Basic Packages"),
        ("install_aur_packages",    S.aur_packages,   "AUR Packages"),
    ]:
        active_names = [
            _html.escape(p["name"])
            for p in pkgs
            if not p.get("disabled") and "name" in p
        ]
        if active_names:
            sm_tips[key] = _packages_tooltip_html(label, active_names, t, font_sz)

    sp_active = [
        p for p in S.specific_packages
        if not p.get("disabled") and (not session or p.get("session") == session)
    ]
    if sp_active:
        sm_tips["install_specific_packages"] = _specific_pkgs_tooltip_html(
            sp_active, session, t, font_sz
        )

    if S.user_shell:
        sm_tips["set_user_shell"] = (
            f"<table style='white-space:nowrap; font-family:monospace;'>"
            f"<tr><td style='padding:4px 5px 2px;font-size:{font_sz(-1)}px;font-weight:bold;"
            f"color:{t['accent2']};border-bottom:1px solid {t['header_sep']};'>Selected Shell</td></tr>"
            f"<tr style='background-color:{t['bg2']};'><td style='padding:8px 6px;border:1px solid "
            f"{t['header_sep']};color:{t['success']};'>{_html.escape(S.user_shell)}</td></tr></table>"
        )

    result = (backup_tips, restore_tips, sm_tips)
    with _cache_lock:
        if _cache is None:
            _cache = result
        return _cache
