import html as _html
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

from linux_distro_helper import LinuxDistroHelper
from state import S, apply_replacements, logger, register_invalidate_hook, active_pkg_names, active_system_files
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


def backup_tooltips() -> dict:
    return generate_tooltip()[0]

def restore_tooltips() -> dict:
    return generate_tooltip()[1]

def sm_tooltips() -> dict:
    return generate_tooltip()[2]


def _entry_tooltip_html(title: str, src_lines: list, dst_lines: list, bg: str, bg2: str, bg3: str, c_title: str,
                        c_data: str, font_sz_fn) -> str:

    s_html, d_html = ("<br/>".join(_html.escape(apply_replacements(str(p))) for p in lines) for lines in (src_lines, dst_lines))
    safe_title = _html.escape(title).replace("&lt;br&gt;", "<br/>")
    label_style = f"color:{c_title}; font-weight: bold; border: 5px solid {c_title}; margin-bottom: 5px;"
    cell_padding = "padding:6px;"
    return (f"<table style='width: 100%; font-family: monospace; white-space: nowrap; border: 5px solid {bg};'>"
            f"<tr style='background-color: {bg};'>"
            f"<td colspan='2' style='font-size: {font_sz_fn(-2)}px; color: {c_title}; text-align: center'>"
            f"<b>{safe_title}</b></td></tr><tr>"
            f"<td style='background-color: {bg2}; font-size: {font_sz_fn(-3)}px; color: {c_data}; line-height: 1.4; "
            f"{cell_padding} vertical-align: top; white-space: nowrap'>"
            f"<span style='{label_style};'>Source:</span><br>{s_html}</td>"
            f"<td style='background-color: {bg3}; font-size: {font_sz_fn(-3)}px; color: {c_data}; line-height: 1.4; "
            f"{cell_padding} vertical-align: top; white-space: nowrap'>"
            f"<span style='{label_style}'>Destination:</span><br>{d_html}</td>"
            f"</tr></table>")


def _sysfiles_tooltip_html(sys_files: list, t: dict, font_sz_fn) -> str:
    cols = 2 if len(sys_files) > 8 else 1
    header = (f"<tr><td colspan='{cols}' style='padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;"
              f"font-weight:bold;white-space:nowrap;color:{t['accent2']};border-bottom:1px solid {t['header_sep']}'>"
              f"System Files ({len(sys_files)})</td></tr>")
    cells = []
    for sf in sys_files:
        src = sf.get("source", "")
        dst = sf.get("destination", "")
        cells.append(f"<td style='padding:4px 6px;border:1px solid {t['header_sep']};white-space:nowrap;vertical-align:top;'>"
                     f"<span style='color:{t['accent2']};font-weight:bold;'>{_html.escape(Path(src).name)}</span><br>"
                     f"<span style='font-size:{font_sz_fn(-3)}px;color:{t['success']};'>"
                     f"{_html.escape(apply_replacements(src))}<br>⤵<br>"
                     f"{_html.escape(apply_replacements(dst))}</span></td>")
    rows = [f"<tr style='background-color:{t['bg2'] if (i // cols) % 2 == 0 else t['bg3']};'>"
            f"{''.join(cells[i:i + cols])}</tr>" for i in range(0, len(cells), cols)]
    return (f"<table style='white-space:nowrap; font-family:monospace;font-size:{font_sz_fn(-2)}px;'>"
            f"{header}{''.join(rows)}</table>")


def _packages_tooltip_html(label: str, pkg_names: list, t: dict, font_sz_fn) -> str:
    cols = 8 if len(pkg_names) > 25 else 5
    header = (f"<tr><td colspan='{cols}' style="
              f"'padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;font-weight:bold;color:{t['accent2']};"
              f"border-bottom:1px solid {t['header_sep']};'>{label} ({len(pkg_names)})</td></tr>")
    rows = []
    for i in range(0, len(pkg_names), cols):
        cells = "".join(
            f"<td style='padding:5px;border:1px solid {t['header_sep']};color:{t['success']};white-space:nowrap'>{p}</td>"
            for p in pkg_names[i : i + cols])
        rows.append(f"<tr style='background-color:{t['bg2'] if (i // cols) % 2 == 0 else t['bg3']};'>{cells}</tr>")
    return (f"<table style='white-space:nowrap; font-family:monospace;font-size:{font_sz_fn(-2)}px;'>"
            f"{header}{''.join(rows)}</table>")


def _specific_pkgs_tooltip_html(sp_active: list, session: Optional[str], t: dict, font_sz_fn) -> str:
    sp_groups: dict[str, list[str]] = defaultdict(list)
    for p in sp_active:
        sp_groups[p.get("session", "?")].append(_html.escape(p.get("package", "")))

    rows, cols, show_sess_hdr = [], 5, len(sp_groups) > 1
    for i, sess in enumerate(sorted(sp_groups)):
        if show_sess_hdr:
            rows.append(f"<tr style='background-color:{t['bg'] if i % 2 == 0 else t['bg2']};'>"
                        f"<td colspan='{cols}' style="
                        f"'padding:3px 5px;font-size:{font_sz_fn(-2)}px;font-weight:bold;color:{t['accent2']};"
                        f"white-space:nowrap;border-bottom:1px solid {t['header_sep']};'>"
                        f"{_html.escape(sess)}</td></tr>")
        for j in range(0, len(sp_groups[sess]), cols):
            cells = "".join(f"<td style='padding:5px;border:1px solid {t['header_sep']};color:{t['success']};'>{p}</td>"
                            for p in sp_groups[sess][j : j + cols])
            rows.append(f"<tr style='background-color:{t['bg2'] if (j // cols) % 2 == 0 else t['bg3']};'>{cells}</tr>")

    header = (f"<tr><td colspan='{cols}' style='padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;font-weight:bold;"
              f"color:{t['accent2']};border-bottom:1px solid {t['header_sep']};'>Specific Packages "
              f"for {_html.escape(session or 'current session')} ({len(sp_active)})</td></tr>")
    return (f"<table style='font-family:monospace;font-size:{font_sz_fn(-2)}px; white-space:nowrap'>"
            f"{header}{''.join(rows)}</table>")


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
        except (OSError, ValueError) as e:
            logger.warning("Session detect failed: %s", e)

    with _session_lock:
        if not _session_detected:
            _cached_session = local_session
            _session_detected = True
        session = _cached_session if _cached_session else None

    t = current_theme()
    backup_tips = {e["title"]: _entry_tooltip_html(e["title"], e.get("source", []), e.get("destination", []),
                                                   t["bg"], t["bg2"], t["bg3"], t["accent2"], t["success"], font_sz)
                   for e in S.entries}
    restore_tips = {e["title"]: _entry_tooltip_html(e["title"], e.get("destination", []), e.get("source", []),
                                                    t["bg"], t["bg2"], t["bg3"], t["accent2"], t["success"], font_sz)
                    for e in S.entries}
    sm_tips: dict = {}

    active_sys_files = active_system_files()
    if active_sys_files:
        sm_tips["copy_system_files"] = _sysfiles_tooltip_html(active_sys_files, t, font_sz)

    for key, pkgs, label in [("install_basic_packages", S.basic_packages, "Basic Packages"), ("install_aur_packages", S.aur_packages, "AUR Packages")]:
        active_names = [_html.escape(n) for n in active_pkg_names(pkgs)]
        if active_names:
            sm_tips[key] = _packages_tooltip_html(label, active_names, t, font_sz)

    sp_active = [p for p in S.specific_packages if not p.get("disabled") and (not session or p.get("session") == session)]
    if sp_active:
        sm_tips["install_specific_packages"] = _specific_pkgs_tooltip_html(sp_active, session, t, font_sz)

    try:
        import pwd as _pwd
        from state import _USER
        _system_shell = Path(_pwd.getpwnam(_USER).pw_shell).name
    except (KeyError, ImportError, OSError):
        _system_shell = ""
    if _system_shell:
        sm_tips["set_user_shell"] = (f"<table style='white-space:nowrap; font-family:monospace;'>"
                                     f"<tr><td style='padding:4px 5px 2px;font-size:{font_sz(-1)}px;font-weight:bold;"
                                     f"color:{t['accent2']};border-bottom:1px solid {t['header_sep']};'>Current User Shell</td></tr>"
                                     f"<tr style='background-color:{t['bg2']};'><td style='padding:8px 6px;border:1px solid "
                                     f"{t['header_sep']};color:{t['success']};'>{_html.escape(_system_shell)}</td></tr></table>")

    result = (backup_tips, restore_tips, sm_tips)
    with _cache_lock:
        if _cache is None:
            _cache = result
    return result


def copy_logic_tooltip() -> str:
    t = current_theme()
    return (
        "<b>Copy &amp; Skip Logic</b><br><br>"
        "<b>Local File Logic:</b><br>"
        "- A file is <b>copied</b> if the destination is missing, the <b>size differs</b>, "
        "or the source is newer than the backup.<br>"
        "- A file is <b>skipped</b> only if the size matches <b>and</b> the backup "
        "is already as new as the source (2s tolerance).<br><br>"
        "<b>Samba (SMB) Logic:</b><br>"
        "- To save bandwidth and avoid latency, the system only checks <b>existence and file size</b>.<br>"
        "- Remote paths <b>must</b> follow the pattern: <code>'smb://ip/path'</code>.<br>"
        "- <b>Local requirement:</b> <code>smbclient</code> must be installed on <b>this machine</b>.<br>"
        "- <b>Remote requirement:</b> Samba must be configured on the <b>target system</b>; "
        "Port must be open (default Port = 445).<br><br>"
        "- <b>Locks &amp; Handles:</b> <code>.lock</code>, <code>lockfile</code>, <code>.lck</code>, <code>.parentlock</code>, <code>Singleton*</code><br>"
        "- <b>Browser Caches:</b> <code>cache/</code>, <code>Network Cache/</code>, <code>startupCache/</code>, <code>jumpListCache/</code>, "
        "<code>GPUCache/</code>, <code>ShaderCache/</code>, <code>blob_storage/</code>, <code>prefs.js</code><br>"
        "- <b>Active DB states:</b> <code>.sqlite-wal/-shm</code>, <code>.db-wal/-shm</code>, <code>.journal</code>, <code>-journal</code>, <code>.ldb</code><br>"
        "- <b>Web Storage:</b> <code>idb/</code> (IndexedDB), <code>WebStorage/</code>, <code>Session Storage</code>, <code>Local Storage</code>, <code>leveldb/</code><br>"
        "- <b>System &amp; Temp:</b> <code>Thumbs.db</code>, <code>.DS_Store</code>, <code>temp/</code>, <code>tmp/</code>, "
        "<code>.bak</code>, <code>.tmp</code>, <code>.baklz4</code>, <code>recovery.jsonlz4</code>, "
        "<code>recovery.baklz4</code>, <code>sessionstore-backups/</code><br>"
        "- <b>Hidden system markers:</b> <code>.quota</code>, <code>.user64</code>, <code>.healthcheck</code>, <code>.active-update</code><br>"
        "<i>Note: All patterns are case-insensitive. Files such as ‘Temperature.txt’ will be copied safely.</i><br><br>"
        "<b>Status Colors:</b><br>"
        f"- <span style='color:{t['success']};'>Green</span> = Success, "
        f"<span style='color:{t['warning']};'>Yellow</span> = Skipped, "
        f"<span style='color:{t['error']};'>Red</span> = Error.<br><br><br>"
        "<b>Samba Credentials &amp; Keyring</b><br><br>"
        "- Passwords are <b>never stored in plain text</b>. The system uses a priority chain:<br>"
        "  1. <b>KDE KWallet:</b> Looks for <code>'smb-[username]'</code> in the <code>'kdewallet'</code> folder.<br>"
        "  2. <b>System Keyring:</b> Fallback via <code>libsecret</code> (service: <code>'backup-helper-samba'</code>).<br>"
        "  3. <b>Guest:</b> If no credentials exist, an anonymous connection is attempted.<br><br><br>"
        "<b>Execution Security (Hardened)</b><br><br>"
        "- <b>Zero Visibility:</b> Passwords are <b>never</b> passed via command-line arguments to prevent exposure in process lists.<br>"
        "- <b>RAM-Only Storage:</b> Credentials are stored in <code>/dev/shm</code> (RAM disk). If <code>/dev/shm</code> is unavailable, "
        "no credential file is created and the connection falls back to an anonymous (guest) attempt.<br>"
        "- <b>Race-Condition Protection:</b> The credential file remains active for the <b>exact duration</b> of the transfer "
        "and is deleted immediately after the process ends.<br>"
        "- <b>Secure Erasure:</b> Before deletion, the credential file is <b>overwritten with zeros</b> (Wipe) and synced.<br>"
        "- <b>Guest Fallback:</b> In case of access errors to the secure storage, the system safely falls back to a guest connection.<br>"
        "- <b>Memory Safety:</b> The core password buffer (<code>SecureString</code>) is a mutable <code>bytearray</code> that is <b>manually zeroed</b> after use. "
        "However, a brief immutable <code>str</code> is unavoidably created during the credential-file write step (<code>pwd_bytes.decode()</code>) and cannot be "
        "actively overwritten — it persists until Python's garbage collector reclaims it. This window is limited to the narrow period while the file is being written to "
        "<code>/dev/shm</code>, after which the file itself is wiped and deleted."
    )


def sudo_checkbox_tooltip() -> str:
    return ("<b>How your sudo password is used — and why it is safe:</b><br><br>"
            "Your password is held <b>only in memory</b> as a mutable <code>bytearray</code> "
            "(via <code>SecureString</code>) — it is <b>never written to any file</b>, "
            "not even to a RAM-backed <code>tmpfs</code> such as <code>/dev/shm</code>.<br><br>"
            "<b>Non-streaming operations</b> (most privileged steps such as package installs, "
            "service activation, file copies) use <code>subprocess.run(input=pw_bytearray, …)</code>. "
            "The <code>bytearray</code> is passed directly as a bytes-like object — "
            "<b>no Python-level <code>bytes</code> copy is created</b>. "
            "After the call returns the buffer is <b>zeroed byte-by-byte</b> in a "
            "<code>finally</code> block. "
            "The password never touches a file, an environment variable, or a command-line argument.<br><br>"
            "<b>Streaming operations</b> (long-running commands such as <code>yay</code>) use "
            "<code>subprocess.Popen</code> with stdin kept open. When sudo requests a password, "
            "the <code>bytearray</code> is written <b>directly</b> to the kernel pipe buffer "
            "and then <b>zeroed byte-by-byte</b> in a <code>finally</code> block immediately "
            "after the write.<br><br>"
            "<b>Residual memory caveat:</b> Python's memory allocator and CPython's subprocess "
            "internals may retain transient copies of credential data in freed heap pages that "
            "cannot be actively overwritten from Python. This is a fundamental limitation of "
            "managed-memory runtimes and applies equally to all Python-based security tools.<br><br>"
            "The original <code>SecureString</code> buffer is zeroed in the <code>finally</code> "
            "block of the worker thread once all tasks are complete.<br><br>"
            "<b>Credential cache:</b><br>"
            "After the single successful authentication <code>sudo</code> stores a credential "
            "timestamp (in <code>/run/sudo/ts/</code>). A background keepalive thread calls "
            "<code>sudo -v</code> every 4 min so the cache never expires during a long session — "
            "no further password input or file I/O is ever required.<br><br>"
            "<b>Cleanup:</b><br>"
            "When System Manager finishes, <code>sudo -k</code> is called to <b>immediately "
            "invalidate</b> the credential cache, and the <code>SecureString</code> buffer "
            "is zeroed.<br><br>"
            "<i>Your password is never logged, never sent over the network, "
            "never written to any file, and never stored beyond this session.</i>")
