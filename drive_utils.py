import os
import re
import shlex
import subprocess
import threading
import time
from functools import lru_cache
from os.path import abspath, expanduser

from PyQt6.QtWidgets import QMessageBox

from state import S, logger, _USER, frozen_safe_env
from translations import tr
from ui_utils import ask_yes_no

_DRIVE_NAME_RE = re.compile(r"^[\w\- .()@:]+$")


_SHELL_INJECTION_SEQS = ("&&", "$(", "${", ";", "|", "`", ">", "<", "\n", "\r", "\x00")


_ALLOWED_MOUNT_CMDS = frozenset({
    "mount", "umount", "mount.cifs", "udisksctl",
    "kdeconnect-cli", "sshfs", "fusermount3", "fusermount",
})


_TRUSTED_BIN_PREFIXES = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/", "/usr/local/bin/", "/usr/local/sbin/",
                         "/run/wrappers/bin/", "/run/current-system/sw/bin/", "/run/current-system/profile/bin/",
                         "/usr/lib/", "/usr/libexec/")


_session_managed_mounts: list[dict] = []
_session_mounts_lock = threading.Lock()
_OCTAL_ESCAPE_RE = re.compile(r"\\(\d{3})")
_mounts_cache: tuple[float, list] = (0.0, [])
_mounts_cache_lock = threading.Lock()
_MOUNT_TIMEOUT_S   = 15
_UNMOUNT_TIMEOUT_S = 30


def get_mounts(max_age: float = 0.5) -> list[tuple[str, str]]:
    global _mounts_cache
    now = time.monotonic()
    with _mounts_cache_lock:
        if now - _mounts_cache[0] < max_age:
            return _mounts_cache[1]
        stale_ts = _mounts_cache[0]

    mounts: list[tuple[str, str]] = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mounts.append((_decode_octal(parts[0]), _decode_octal(parts[1])))
    except OSError as e:
        logger.warning("get_mounts: /proc/mounts not available: %s", e)
        with _mounts_cache_lock:
            return _mounts_cache[1]

    with _mounts_cache_lock:
        if _mounts_cache[0] == stale_ts:
            _mounts_cache = (now, mounts)
        return _mounts_cache[1]


def _decode_octal(s: str) -> str:
    return _OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), s)


def get_session_managed_mounts() -> list[dict]:
    with _session_mounts_lock:
        return _session_managed_mounts.copy()


def _track_session_mount(drive: dict) -> None:
    with _session_mounts_lock:
        if drive not in _session_managed_mounts:
            _session_managed_mounts.append(drive)


def _untrack_session_mount(drive: dict) -> None:
    with _session_mounts_lock:
        if drive in _session_managed_mounts:
            _session_managed_mounts.remove(drive)


def _validate_cmd(cmd: str) -> tuple[bool, str, list[str]]:
    if not cmd:
        return False, tr("Empty command"), []
    if any(seq in cmd for seq in _SHELL_INJECTION_SEQS):
        return False, tr("Dangerous characters in command"), []
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        return False, str(e), []
    if not tokens:
        return False, tr("No command tokens found"), []
    expanded = [os.path.expanduser(tok) for tok in tokens]
    cmd_tok = expanded[0]
    base = os.path.basename(cmd_tok)
    if base in ("sudo", "doas"):
        for tok in expanded[1:]:
            if (tok in ("-u", "--user")
                    or tok.startswith("--user=")
                    or (tok.startswith("-u") and len(tok) > 2)):
                return False, tr("sudo user-switching flags are not permitted"), []
            if not tok.startswith("-"):
                cmd_tok = tok
                base = os.path.basename(tok)
                break
        else:
            return False, tr("sudo without a command"), []
    if base not in _ALLOWED_MOUNT_CMDS:
        return False, tr("'{base}' is not an allowed command", base=base), []
    if "/" in cmd_tok and not cmd_tok.startswith(_TRUSTED_BIN_PREFIXES):
        return False, tr("'{base}' is not an allowed command", base=cmd_tok), []
    for tok in expanded:
        if ".." in tok.split("/"):
            return False, tr("Path traversal detected in token: {tok!r}", tok=tok), []
    return True, "", expanded


def _valid_drive_name(name: str) -> bool: return bool(name and isinstance(name, str) and _DRIVE_NAME_RE.match(name) and len(name) <= 128)


def _str(v) -> str:
    return v.strip() if isinstance(v, str) else ""


@lru_cache(maxsize=64)
def _mount_paths(name: str) -> tuple[str, ...]:
    if not _valid_drive_name(name):
        return ()
    return (f"/run/media/{_USER}/{name}", f"/media/{_USER}/{name}", f"/mnt/{name}",
            f"/media/{name}", f"/run/media/{name}")


def _execute_drive_op(drive: dict, cmd_key: str, timeout: int) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    cmd = _str(drive.get(cmd_key))
    if not cmd:
        return False, tr("No {cmd_key} configured for '{name}'.", cmd_key=cmd_key.replace('_', ' '), name=name)
    ok, reason, tokens = _validate_cmd(cmd)
    if not ok:
        return False, tr("Drive '{name}': {reason}", name=name, reason=reason)
    from privileged import run_user_command
    try:
        result = run_user_command(tokens, timeout=timeout)
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip() or result.stdout.strip() or tr("exit code {code}", code=result.returncode)
    except Exception as e:
        logger.debug("_execute_drive_op: '%s' (%s) raised %s: %s", name, cmd_key, type(e).__name__, e)
        return False, str(e)


def _norm_mount_path(path: str) -> str:
    return path.rstrip("/") or path


def _opt_mount_path(opt: dict) -> str:
    raw = _str(opt.get("mount_path"))
    if not raw:
        return ""
    return _norm_mount_path(raw if raw.startswith(("smb://", "cifs://")) else abspath(expanduser(raw)))


def _drive_roots(opt: dict) -> list[str]:
    mount_path = _opt_mount_path(opt)
    if mount_path:
        return [mount_path]
    name = opt.get("drive_name", "")
    return list(_mount_paths(name)) if _valid_drive_name(name) else []


def is_mounted(opt: dict, mounts: list[tuple[str, str]] | None = None) -> bool:
    name = opt.get("drive_name", "")
    mount_path = _opt_mount_path(opt)

    if not _valid_drive_name(name) and not mount_path:
        return False

    if mounts is None:
        mounts = get_mounts()

    expected_paths = set(_mount_paths(name)) if _valid_drive_name(name) else set()
    smb_prefixes: tuple = ()

    if mount_path and is_smb(mount_path):
        without_schema = re.sub(r"^(smb|cifs)://", "", mount_path).rstrip("/")
        parts = without_schema.split("/", 2)
        if len(parts) >= 2:
            host = parts[0]
            share = parts[1]
            full_device = f"//{host}/{share}"
            if len(parts) == 3 and parts[2]:
                full_device = f"{full_device}/{parts[2].lstrip('/')}"
            smb_prefixes = (full_device.lower(),)

    for dev, mnt in (mounts or []):
        if mnt in expected_paths or (mount_path and mnt == mount_path):
            return True
        if smb_prefixes:
            dev_clean = dev.lower().rstrip("/")
            if any(dev_clean == p.rstrip("/") for p in smb_prefixes):
                return True

    return False


def mount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "mount_command", _MOUNT_TIMEOUT_S)
    if success:
        mounted_confirmed = False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if is_mounted(drive, mounts=get_mounts(max_age=0.0)):
                mounted_confirmed = True
                break
            time.sleep(0.1)
        if not mounted_confirmed:
            mounted_confirmed = is_mounted(drive)
        is_managed = has_managed_mount_path(drive)
        if not mounted_confirmed:
            if not is_managed:
                msg = tr("Mount command succeeded but '{name}' not visible in /proc/mounts after 1 s", name=name)
                logger.error("mount_drive: %s", msg)
                return False, msg
            _track_session_mount(drive)
            logger.warning("mount_drive: '%s' not confirmed in /proc/mounts (managed mount path)", name)
        else:
            if is_managed:
                _track_session_mount(drive)
        logger.info("Mounted '%s' (confirmed=%s)", name, mounted_confirmed)
        return True, ""
    msg = tr("Mount failed for '{name}': {error_msg}", name=name, error_msg=error_msg)
    logger.error("mount_drive: %s", msg)
    return False, msg


def unmount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "unmount_command", _UNMOUNT_TIMEOUT_S)
    if success:
        _untrack_session_mount(drive)
        logger.info("Unmounted '%s'", name)
        return True, ""
    msg = tr("Unmount failed for '{name}': {error_msg}", name=name, error_msg=error_msg)
    logger.error("unmount_drive: %s", msg)
    return False, msg


def is_smb(path: str) -> bool: return path.startswith(("smb://", "cifs://"))


def _is_subpath(parent: str, child: str) -> bool: return (child.rstrip("/") + "/").startswith(parent.rstrip("/") + "/")


_SSH_USER_HOST_RE = re.compile(r"^(?:(?P<user>[^/@\s]+)@)?(?P<host>[^/@\s:]+):(?!//)(?P<rpath>.+)$")


def is_ssh(path: str) -> bool:
    return path.startswith("ssh://") or _SSH_USER_HOST_RE.match(path) is not None


def to_browsable_uri(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return p
    if p.startswith("cifs://"):
        return "smb://" + p[len("cifs://"):]
    if p.startswith("smb://"):
        return p
    if p.startswith("ssh://"):
        return "sftp://" + p[len("ssh://"):]
    m = _SSH_USER_HOST_RE.match(p)
    if m:
        user  = m.group("user")
        host  = m.group("host")
        rpath = m.group("rpath")
        if not rpath.startswith("/"):
            rpath = "/" + rpath
        return f"sftp://{(user + '@') if user else ''}{host}{rpath}"
    return abspath(expanduser(p))


def open_in_file_manager(path: str) -> tuple[bool, str]:
    target = to_browsable_uri(path)
    if not target:
        return False, tr("No path given.")
    if not is_smb(target) and not target.startswith("sftp://"):
        if os.path.isfile(target):
            target = os.path.dirname(target) or target
        elif not os.path.exists(target):
            return False, tr("Path does not exist:\n{target}", target=target)
    try:
        subprocess.Popen(
            ["xdg-open", target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=frozen_safe_env(),
        )
    except OSError as exc:
        logger.warning("open_in_file_manager xdg-open: %s", exc)
        return False, str(exc)
    return True, ""


_OPENSSH_VER_RE = re.compile(r"OpenSSH_(\d+)\.(\d+)")


@lru_cache(maxsize=1)
def _ssh_supports_accept_new() -> bool:
    try:
        r = subprocess.run(["ssh", "-V"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=5, check=False, env=frozen_safe_env())
    except (OSError, subprocess.SubprocessError):
        return True
    m = _OPENSSH_VER_RE.search((r.stderr or "") + (r.stdout or ""))
    if not m:
        return True
    return (int(m.group(1)), int(m.group(2))) >= (7, 6)


_SSH_UNSAFE_PATH_RE = re.compile(r"[\s'\"\\]")


@lru_cache(maxsize=1)
def _ssh_mux_opts() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if (not runtime or len(runtime) > 40 or _SSH_UNSAFE_PATH_RE.search(runtime)
            or not os.path.isdir(runtime) or not os.access(runtime, os.W_OK | os.X_OK)):
        return ""
    return f"-o ControlMaster=auto -o ControlPersist=30 -o ControlPath={runtime}/bh-ssh-%C "


def _rsync_ssh_opts(port: "str | None" = None) -> str:
    strict = "-o StrictHostKeyChecking=accept-new " if _ssh_supports_accept_new() else ""
    port_opt = f"-p {port} " if port else ""
    return (f"ssh {port_opt}{strict}{_ssh_mux_opts()}"
            "-o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 "
            "-o BatchMode=yes -o Compression=no")


_SSH_URL_SPEC_RE = re.compile(
    r"^ssh://(?:(?P<user>[^@/]+)@)?(?P<host>\[[^\x5d]+]|[^/:@]+)(?::(?P<port>\d{1,5}))?(?P<path>/.*)?$"
)


def _rsync_remote_spec(spec: str) -> "tuple[str, str | None]":
    m = _SSH_URL_SPEC_RE.match(spec)
    if m is None:
        return spec, None
    user = f"{m.group('user')}@" if m.group("user") else ""
    return f"{user}{m.group('host')}:{m.group('path') or ''}", m.group("port")


_RSYNC_SHELL_META_RE = re.compile(r"[\s'\"()&;$*?\[\]{}<>|!#~`\\]")


def _remote_spec_needs_protect(spec: str) -> bool:
    if not (is_ssh(spec) and not spec.startswith("ssh://")):
        return False
    return bool(_RSYNC_SHELL_META_RE.search(spec.split(":", 1)[-1]))

_RSYNC_SKIP_COMPRESS = (
    "7z/avi/bz2/deb/flac/gz/heic/heif/iso/jpeg/jpg/lz/lz4/lzma/lzo/"
    "m4a/m4v/mkv/mov/mp3/mp4/ogg/opus/png/rar/rpm/tbz/tbz2/tgz/tlz/"
    "txz/webm/webp/xz/z/zip/zst"
)


@lru_cache(maxsize=1)
def _rsync_supports_mkpath() -> bool:
    try:
        res = subprocess.run(["rsync", "--version"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=5, check=False, env=frozen_safe_env())
    except (OSError, subprocess.SubprocessError):
        return False
    m = re.search(r"version\s+(\d+)\.(\d+)\.(\d+)", res.stdout or "")
    if m is None:
        return False
    return tuple(int(x) for x in m.groups()) >= (3, 2, 3)


def build_rsync_cmd(src: str, dst: str, *, delete: bool = False, exclude: list[str] | None = None,
                    dry_run: bool = False) -> list[str]:
    info = "progress2,del" if delete else "progress2"
    src, src_port = _rsync_remote_spec(src)
    dst, dst_port = _rsync_remote_spec(dst)
    cmd = [
        "rsync", "-azH8", "--numeric-ids",
        f"--skip-compress={_RSYNC_SKIP_COMPRESS}",
        "--partial", "--partial-dir=.rsync-partial",
        f"--info={info}", "-e", _rsync_ssh_opts(src_port or dst_port),
    ]
    if _remote_spec_needs_protect(src) or _remote_spec_needs_protect(dst):
        cmd.append("--protect-args")
    if delete:
        cmd.append("--delete")
    if dry_run:
        cmd.append("--dry-run")
    elif _rsync_supports_mkpath():
        cmd.append("--mkpath")
    for ex in (exclude or []):
        cmd += [f"--exclude={ex}"]
    cmd += ["--", src, dst]
    return cmd


def build_rsync_list_cmd(spec: str) -> list[str]:
    spec, port = _rsync_remote_spec(spec)
    cmd = ["rsync", "--list-only", "-e", _rsync_ssh_opts(port)]
    if _remote_spec_needs_protect(spec):
        cmd.append("--protect-args")
    cmd += ["--", spec]
    return cmd


def check_drives_to_mount(paths: list[str]) -> list[dict]:
    paths = [p if (is_smb(p) or is_ssh(p)) else abspath(expanduser(p)) for p in paths]
    mounts = get_mounts()
    needed = []
    for opt in S.mount_options:
        if is_mounted(opt, mounts):
            continue
        name = opt.get("drive_name", "")
        roots = _drive_roots(opt)
        if not roots:
            logger.warning(
                "check_drives_to_mount: Drive %r does not have a valid name or a "
                "mount_path — will be skipped. Please correct this in the settings.", name
            )
            continue
        if any(_is_subpath(root, p) for root in roots for p in paths):
            needed.append(opt)
    return needed


def unmounted_drive_for_path(path: str, mounts: "list[tuple[str, str]] | None" = None) -> "str | None":
    if not path or is_smb(path) or is_ssh(path):
        return None
    target = abspath(expanduser(path))
    if mounts is None:
        mounts = get_mounts()
    for opt in S.mount_options:
        if is_mounted(opt, mounts):
            continue
        for root in _drive_roots(opt):
            if not is_smb(root) and _is_subpath(root, target):
                return str(opt.get("drive_name") or root)
    return None


def mount_drives_unattended(paths: list[str]) -> list[str]:
    failed: list[str] = []
    for opt in check_drives_to_mount(paths):
        ok, msg = mount_drive(opt)
        if not ok:
            logger.error("Unattended mount failed: %s", msg)
            failed.append(str(opt.get("drive_name") or opt.get("mount_path") or "?"))
    return failed


def has_managed_mount_path(opt: dict) -> bool:
    mount_path = _opt_mount_path(opt)
    return bool(mount_path and mount_path not in _mount_paths(opt.get("drive_name", "")))


def mount_required_drives(drives: list[dict], parent=None) -> bool:
    for opt in drives:
        drive_name = opt.get("drive_name", "?")
        is_managed = has_managed_mount_path(opt)

        if is_managed:
            msg = tr("'{drive_name}' is required, but cannot be automatically detected due to an external mount path."
                     "\n\nRun mount command now?", drive_name=drive_name)
        else:
            msg = tr("'{drive_name}' is required but not mounted.\n\nRun mount command now?", drive_name=drive_name)

        answer = ask_yes_no(parent, tr("Drive Required"), msg)

        if answer != QMessageBox.StandardButton.Yes:
            if is_managed:
                logger.info("Skipping mount for managed drive '%s'; continuing operation.", drive_name)
                QMessageBox.information(
                    parent, tr("Skipping Drive"), tr("'{drive_name}' was not mounted.\n\n"
                                              "Since this drive uses an external mount path, the operation will continue.\n"
                                              "Files on this drive may be unavailable.", drive_name=drive_name))
                continue
            QMessageBox.warning(parent, tr("Operation Cancelled"),
                                tr("The required drive '{drive_name}' was not mounted.\n\nThe operation has been cancelled.", drive_name=drive_name))
            return False

        success, error_msg = mount_drive(opt)
        if not success:
            QMessageBox.critical(parent, tr("Mount Failed"),
                                 tr("Could not mount '{drive_name}':\n\n{error_msg}\n\nThe operation has been cancelled.", drive_name=drive_name, error_msg=error_msg))
            return False

    return True
