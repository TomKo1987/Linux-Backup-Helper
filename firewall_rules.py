import html
import importlib
import ipaddress
import os
import re
import shlex
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from linux_distro_helper import LinuxDistroHelper
from privileged import auth_required_message, run_privileged
from state import S, save_profile
from themes import current_theme
from translations import tr
from ui_utils import ok_cancel_buttons

importlib.import_module("firewall_translations")

VALID_ACTIONS = ("allow", "deny", "reject", "limit")
VALID_DEFAULT_ACTIONS = ("default allow", "default deny", "default reject")
VALID_DIRECTIONS = ("in", "out")
VALID_PROTOS = ("both", "tcp", "udp")

BASELINE_DEFAULTS = {"in": "default deny", "out": "default allow"}

_LEGACY_DEFAULT_COMMENT = "Default Policy"
_DIR_WORDS = {"in": "in", "incoming": "in", "out": "out", "outgoing": "out"}
_ANY_WORDS = ("", "any", "anywhere")
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,32}$")
_PORT_ITEM_RE = re.compile(r"^\d{1,5}(?::\d{1,5})?$")
_SERVICE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.+-]*$")
_WRAPPER_FLAGS_WITH_ARG = frozenset({"-u", "-g", "-p", "-C", "-D", "-r", "-t", "-R", "-T", "-U"})
_FIREWALLD_DEFAULT_ZONES = {"allow": "trusted", "deny": "drop", "reject": "block"}
_FIREWALLD_ZONE_ACTIONS = {"trusted": "allow", "drop": "deny", "block": "reject"}
_UFW_POLICIES = {"ACCEPT": "allow", "DROP": "deny", "REJECT": "reject"}
_ACTION_CHOICES = ["allow", "deny", "reject", "limit", "default allow", "default deny", "default reject"]

ServiceResolver = Callable[[str], "list[tuple[str, str]] | None"]


def is_default_action(action: str) -> bool:
    return action.startswith("default ")


def split_ports(port: str) -> list[str]:
    return [p.strip() for p in port.split(",") if p.strip()] if port else []


def is_port_range(port: str) -> bool:
    return bool(port) and ":" in port


def is_multi_port(port: str) -> bool:
    return bool(port) and ("," in port or ":" in port)


def port_to_firewalld(port: str) -> str:
    return port.replace(":", "-") if port else port


def _service_number(name: str, proto: str = "both") -> int | None:
    order = (proto,) if proto in ("tcp", "udp") else ("tcp", "udp")
    for p in order:
        try:
            return socket.getservbyname(name, p)
        except (OSError, OverflowError):
            continue
    return None


def is_valid_port(port: str) -> bool:
    if not port:
        return True
    items = split_ports(port)
    if not items:
        return False
    for item in items:
        if _PORT_ITEM_RE.match(item):
            nums = [int(x) for x in item.split(":")]
            if any(n < 1 or n > 65535 for n in nums):
                return False
            if len(nums) == 2 and nums[0] > nums[1]:
                return False
        elif len(items) == 1 and _SERVICE_RE.match(item):
            if _service_number(item) is None:
                return False
        else:
            return False
    return True


def _port_ranges(port: str) -> list[tuple[int, int]]:
    out = []
    for item in split_ports(port):
        if ":" in item:
            lo, hi = item.split(":", 1)
            if lo.isdigit() and hi.isdigit():
                out.append((int(lo), int(hi)))
        elif item.isdigit():
            out.append((int(item), int(item)))
        else:
            number = _service_number(item)
            if number is not None:
                out.append((number, number))
    return out


def is_valid_interface(name: str) -> bool:
    return not name or bool(_IFACE_RE.match(name))


def source_family(source: str) -> str:
    if not source:
        return "ipv4"
    candidate = source.split("/", 1)[0].strip()
    try:
        return "ipv6" if ipaddress.ip_address(candidate).version == 6 else "ipv4"
    except ValueError:
        pass
    try:
        return "ipv6" if ipaddress.ip_network(source, strict=False).version == 6 else "ipv4"
    except ValueError:
        return "ipv4"


def is_valid_source(source: str) -> bool:
    if source.strip().lower() in _ANY_WORDS:
        return True
    candidate = source.split("/", 1)[0].strip()
    try:
        ipaddress.ip_address(candidate)
        if "/" not in source:
            return True
    except ValueError:
        pass
    try:
        ipaddress.ip_network(source, strict=False)
        return True
    except ValueError:
        return False


def _clean_address(value) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _ANY_WORDS else text


def normalize_rule(rule: dict) -> dict:
    action = str(rule.get("action", "allow")).strip().lower()
    if action not in VALID_ACTIONS and action not in VALID_DEFAULT_ACTIONS:
        action = "allow"

    direction = str(rule.get("direction", "in")).strip().lower()
    direction = _DIR_WORDS.get(direction, "in")

    comment = str(rule.get("comment", "")).strip()

    if is_default_action(action):
        if comment == _LEGACY_DEFAULT_COMMENT:
            comment = ""
        return {
            "action": action, "direction": direction, "interface": "", "proto": "both",
            "source": "", "destination": "", "port": "", "comment": comment,
        }

    proto = str(rule.get("proto", "both")).strip().lower()
    if proto not in VALID_PROTOS:
        proto = "both"

    port = re.sub(r"\s+", "", str(rule.get("port", "")))
    if "/" in port:
        base, _, suffix = port.rpartition("/")
        if suffix.lower() in ("tcp", "udp"):
            port = base
            if proto == "both":
                proto = suffix.lower()

    if is_multi_port(port) and proto == "both":
        proto = "tcp"

    return {
        "action": action,
        "direction": direction,
        "interface": str(rule.get("interface", "")).strip(),
        "proto": proto,
        "source": _clean_address(rule.get("source", "")),
        "destination": _clean_address(rule.get("destination", "")),
        "port": port,
        "comment": comment,
    }


def rule_key(rule: dict) -> tuple:
    norm = normalize_rule(rule)
    return tuple(norm[k] for k in ("action", "direction", "interface", "proto", "source", "destination", "port"))


def dedupe_rules(rules: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for rule in rules:
        norm = normalize_rule(rule)
        key = rule_key(norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def build_ufw_command(rule: dict) -> list[str]:
    rule = normalize_rule(rule)
    action = rule["action"]
    direction = rule["direction"]

    if is_default_action(action):
        policy = action.split()[1]
        return ["sudo", "ufw", "default", policy, "incoming" if direction == "in" else "outgoing"]

    cmd = ["sudo", "ufw", action, direction]
    if rule["interface"]:
        cmd.extend(["on", rule["interface"]])
    cmd.extend(["from", rule["source"] or "any", "to", rule["destination"] or "any"])
    if rule["port"]:
        cmd.extend(["port", rule["port"]])
    if rule["proto"] != "both":
        cmd.extend(["proto", rule["proto"]])
    if rule["comment"]:
        cmd.extend(["comment", rule["comment"]])
    return cmd


def _rules_cover_ssh(rules: list[dict]) -> bool:
    for rule in rules:
        norm = normalize_rule(rule)
        if norm["action"] not in ("allow", "limit") or norm["direction"] != "in":
            continue
        if norm["proto"] == "udp" or norm["interface"]:
            continue
        if not norm["port"]:
            return True
        if any(lo <= 22 <= hi for lo, hi in _port_ranges(norm["port"])):
            return True
    return False


def build_ufw_commands(rules: list[dict], *, ensure_ssh_allowed: bool = True) -> list[list[str]]:
    cmds = [["sudo", "ufw", "default", "deny", "incoming"], ["sudo", "ufw", "default", "allow", "outgoing"]]

    if ensure_ssh_allowed and not _rules_cover_ssh(rules):
        cmds.append(["sudo", "ufw", "allow", "in", "22/tcp", "comment", "Auto-added: prevent SSH lockout"])

    for rule in rules:
        cmds.append(build_ufw_command(rule))
    cmds.extend([["sudo", "ufw", "--force", "enable"], ["sudo", "ufw", "reload"]])
    return cmds


def build_firewalld_rich_rules(rule: dict) -> list[str]:
    rule = normalize_rule(rule)
    action_val = rule["action"]
    if is_default_action(action_val) or rule["direction"] != "in" or rule["interface"]:
        return []

    limit_clause = ""
    if action_val == "allow":
        action = "accept"
    elif action_val == "deny":
        action = "drop"
    elif action_val == "limit":
        action = "accept"
        limit_clause = ' limit value="6/m"'
    else:
        action = "reject"

    src = rule["source"] if rule["source"] and is_valid_source(rule["source"]) else ""
    dst = rule["destination"] if rule["destination"] and is_valid_source(rule["destination"]) else ""
    families = {source_family(a) for a in (src, dst) if a}
    if len(families) > 1:
        return []

    addr = ""
    if src:
        addr += f' source address="{src}"'
    if dst:
        addr += f' destination address="{dst}"'

    matches: list[str] = []
    if rule["port"]:
        protos = ("tcp", "udp") if rule["proto"] == "both" else (rule["proto"],)
        for item in split_ports(rule["port"]):
            if ":" in item or item.isdigit():
                number = item
            else:
                resolved = _service_number(item, rule["proto"])
                if resolved is None:
                    return []
                number = str(resolved)
            for proto in protos:
                matches.append(f' port port="{port_to_firewalld(number)}" protocol="{proto}"')
    elif rule["proto"] != "both":
        matches.append(f' protocol value="{rule["proto"]}"')
    else:
        matches.append("")

    if families:
        fams = [f' family="{next(iter(families))}"']
    elif matches == [""]:
        fams = [' family="ipv4"', ' family="ipv6"']
    else:
        fams = [""]

    return [f"rule{fam}{addr}{match} {action}{limit_clause}" for fam in fams for match in matches]


def build_firewalld_commands(rules: list[dict], *, ensure_ssh_allowed: bool = True) -> list[list[str]]:
    cmds = [["sudo", "firewall-cmd", "--set-default-zone=drop"]]

    if ensure_ssh_allowed and not _rules_cover_ssh(rules):
        cmds.append(["sudo", "firewall-cmd", "--zone=drop", "--add-service=ssh", "--permanent"])
        cmds.append(["sudo", "firewall-cmd", "--zone=drop", "--add-service=ssh"])

    for rule in rules:
        norm = normalize_rule(rule)

        if is_default_action(norm["action"]):
            if norm["direction"] == "in":
                zone = _FIREWALLD_DEFAULT_ZONES[norm["action"].split()[1]]
                cmds.append(["sudo", "firewall-cmd", f"--set-default-zone={zone}"])
            continue

        for rich_rule in build_firewalld_rich_rules(norm):
            cmds.append(["sudo", "firewall-cmd", "--permanent", f"--add-rich-rule={rich_rule}"])

    cmds.append(["sudo", "firewall-cmd", "--reload"])
    return cmds


def _strip_wrappers(tokens: list[str]) -> list[str]:
    tokens = list(tokens)
    while tokens and os.path.basename(tokens[0]) in ("sudo", "doas"):
        tokens.pop(0)
        while tokens and tokens[0].startswith("-"):
            flag = tokens.pop(0)
            if flag == "--":
                break
            if flag in _WRAPPER_FLAGS_WITH_ARG and tokens:
                tokens.pop(0)
    return tokens


def _split_port_proto(spec: str) -> tuple[str, str] | None:
    proto = "both"
    port = spec
    if "/" in spec:
        port, _, suffix = spec.rpartition("/")
        if suffix.lower() not in ("tcp", "udp"):
            return None
        proto = suffix.lower()
    if not port or not is_valid_port(port):
        return None
    return port, proto


def _parse_ufw(tokens: list[str]) -> dict | None:
    tokens = list(tokens)
    while tokens and tokens[0] in ("--force", "--dry-run"):
        tokens.pop(0)
    if not tokens:
        return None

    if tokens[0].lower() == "default":
        rest = tokens[1:]
        if not rest or rest[0].lower() not in ("allow", "deny", "reject"):
            return None
        direction = "in"
        if len(rest) >= 2:
            direction = _DIR_WORDS.get(rest[1].lower(), "")
            if not direction:
                return None
        if len(rest) > 2:
            return None
        return normalize_rule({"action": f"default {rest[0].lower()}", "direction": direction})

    if tokens[0].lower() == "insert":
        tokens = tokens[2:]
    if tokens and tokens[0].lower() == "prepend":
        tokens = tokens[1:]
    if not tokens or tokens[0].lower() not in VALID_ACTIONS:
        return None

    rule = {"action": tokens[0].lower(), "direction": "in", "interface": "", "proto": "both",
            "source": "", "destination": "", "port": "", "comment": ""}
    rest = tokens[1:]
    n = len(rest)
    i = 0
    while i < n:
        tok = rest[i]
        low = tok.lower()
        if low in _DIR_WORDS and i == 0:
            rule["direction"] = _DIR_WORDS[low]
            i += 1
        elif low == "on" and i + 1 < n:
            rule["interface"] = rest[i + 1]
            i += 2
        elif low in ("log", "log-all"):
            i += 1
        elif low == "proto" and i + 1 < n:
            proto = rest[i + 1].lower()
            if proto in ("any", "both"):
                proto = "both"
            elif proto not in ("tcp", "udp"):
                return None
            rule["proto"] = proto
            i += 2
        elif low in ("from", "to") and i + 1 < n:
            addr = rest[i + 1]
            i += 2
            if i < n and rest[i].lower() in ("port", "app"):
                if rest[i].lower() == "app" or i + 1 >= n or low == "from":
                    return None
                rule["port"] = rest[i + 1]
                i += 2
            if not is_valid_source(addr):
                return None
            rule["source" if low == "from" else "destination"] = _clean_address(addr)
        elif low == "port" and i + 1 < n:
            rule["port"] = rest[i + 1]
            i += 2
        elif low == "comment":
            rule["comment"] = " ".join(rest[i + 1:])
            break
        elif not rule["port"]:
            parsed = _split_port_proto(tok)
            if parsed is None:
                return None
            rule["port"] = parsed[0]
            if parsed[1] != "both":
                rule["proto"] = parsed[1]
            i += 1
        else:
            return None

    if rule["port"] and not is_valid_port(rule["port"]):
        return None
    if not is_valid_interface(rule["interface"]):
        return None
    return normalize_rule(rule)


def _default_service_resolver(name: str) -> list[tuple[str, str]] | None:
    out = []
    for proto in ("tcp", "udp"):
        try:
            out.append((str(socket.getservbyname(name, proto)), proto))
        except (OSError, OverflowError):
            continue
    return out or None


def _port_spec_rules(spec: str, action: str = "allow") -> list[dict] | None:
    port, sep, proto = spec.partition("/")
    if not sep or proto.lower() not in ("tcp", "udp"):
        return None
    port = port.replace("-", ":")
    if not is_valid_port(port):
        return None
    return [normalize_rule({"action": action, "proto": proto.lower(), "port": port})]


def _service_rules(name: str, action: str, resolver: ServiceResolver,
                   extra: dict | None = None) -> list[dict] | None:
    ports = resolver(name)
    if not ports:
        return None
    rules = []
    for port, proto in ports:
        base = dict(extra or {})
        base.update({"action": action, "proto": proto, "port": port.replace("-", ":")})
        rules.append(normalize_rule(base))
    return rules


def _parse_rich_rule(text: str, resolver: ServiceResolver) -> list[dict] | None:
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    if not tokens or tokens[0] != "rule":
        return None

    src = dst = proto = port = service = ""
    action = ""
    limited = False
    elem = ""
    in_log = False

    for tok in tokens[1:]:
        if "=" in tok:
            key, _, val = tok.partition("=")
            key = key.lower()
            if elem == "source" and key == "address":
                src = val
            elif elem == "destination" and key == "address":
                dst = val
            elif elem == "port" and key == "port":
                port = val.replace("-", ":")
            elif elem == "port" and key == "protocol":
                proto = val.lower()
            elif elem == "protocol" and key == "value":
                proto = val.lower()
            elif elem == "service" and key == "name":
                service = val
            elif key == "invert" and val.lower() == "true":
                return None
            continue
        low = tok.lower()
        if low in ("source", "destination", "port", "protocol", "service"):
            elem = low
        elif low in ("log", "audit"):
            in_log = True
            elem = low
        elif low in ("accept", "drop", "reject"):
            action = low
            in_log = False
            elem = "action"
        elif low == "limit":
            if action and not in_log:
                limited = True
            elem = "limit"
        else:
            return None

    if not action:
        return None
    mapped = {"accept": "allow", "drop": "deny", "reject": "reject"}[action]
    if mapped == "allow" and limited:
        mapped = "limit"
    if not is_valid_source(src) or not is_valid_source(dst):
        return None

    extra = {"source": src, "destination": dst}
    if service:
        return _service_rules(service, mapped, resolver, extra)
    if port:
        if proto not in ("tcp", "udp") or not is_valid_port(port):
            return None
        return [normalize_rule({**extra, "action": mapped, "proto": proto, "port": port})]
    if proto:
        if proto not in ("tcp", "udp"):
            return None
        return [normalize_rule({**extra, "action": mapped, "proto": proto})]
    return [normalize_rule({**extra, "action": mapped})]


def _option_value(tokens: list[str], i: int, name: str) -> tuple[str | None, int]:
    tok = tokens[i]
    if tok.startswith(name + "="):
        return tok.split("=", 1)[1], i + 1
    if tok == name and i + 1 < len(tokens):
        return tokens[i + 1], i + 2
    return None, i


def _parse_firewalld_cmd(tokens: list[str], resolver: ServiceResolver) -> list[dict] | None:
    rules: list[dict] = []
    i = 0
    recognised = False
    while i < len(tokens):
        handled = False
        for name in ("--add-port", "--add-service", "--add-rich-rule", "--add-source", "--set-default-zone"):
            value, nxt = _option_value(tokens, i, name)
            if value is None:
                continue
            handled = recognised = True
            if name == "--add-port":
                parsed = _port_spec_rules(value)
            elif name == "--add-service":
                parsed = _service_rules(value, "allow", resolver)
            elif name == "--add-rich-rule":
                parsed = _parse_rich_rule(value, resolver)
            elif name == "--add-source":
                parsed = [normalize_rule({"action": "allow", "source": value})] if is_valid_source(value) else None
            else:
                action = _FIREWALLD_ZONE_ACTIONS.get(value.lower())
                parsed = [normalize_rule({"action": f"default {action}"})] if action else None
            if parsed is None:
                return None
            rules.extend(parsed)
            i = nxt
            break
        if not handled:
            i += 1
    return rules if recognised and rules else None


def parse_raw_rules(raw: str, resolver: ServiceResolver | None = None) -> list[dict] | None:
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return None
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return None
    tool = os.path.basename(tokens[0])
    if tool in ("firewall-cmd", "firewall-offline-cmd"):
        return _parse_firewalld_cmd(tokens[1:], resolver or _default_service_resolver)
    if tool == "ufw":
        tokens = tokens[1:]
    rule = _parse_ufw(tokens)
    return [rule] if rule else None


_UFW_STATUS_RE = re.compile(
    r"^\[\s*\d+]\s+(?P<to>.+?)\s{2,}(?P<action>ALLOW|DENY|REJECT|LIMIT)(?:\s+(?P<dir>IN|OUT|FWD))?"
    r"\s{2,}(?P<frm>.+?)(?:\s+#\s*(?P<comment>.*))?$"
)


def _is_ip_or_net(text: str) -> bool:
    return bool(text) and text.lower() not in _ANY_WORDS and is_valid_source(text)


def _parse_ufw_status_side(text: str) -> dict | None:
    v6 = "(v6)" in text
    text = text.replace("(v6)", " ").strip()
    iface = ""
    match = re.search(r"\s+on\s+(\S+)\s*$", " " + text)
    if match:
        iface = match.group(1)
        text = re.sub(r"\s+on\s+\S+\s*$", "", " " + text).strip()
    parts = text.split()
    addr = ""
    port = ""
    proto = "both"
    if not parts:
        return None
    if len(parts) == 1:
        word = parts[0]
        if word.lower() in _ANY_WORDS:
            pass
        elif _is_ip_or_net(word):
            addr = word
        else:
            parsed = _split_port_proto(word)
            if parsed is None:
                return None
            port, proto = parsed
    elif len(parts) == 2:
        if parts[0].lower() not in _ANY_WORDS and not _is_ip_or_net(parts[0]):
            return None
        addr = "" if parts[0].lower() in _ANY_WORDS else parts[0]
        parsed = _split_port_proto(parts[1])
        if parsed is None:
            return None
        port, proto = parsed
    else:
        return None
    return {"v6": v6, "iface": iface, "addr": addr, "port": port, "proto": proto}


def parse_ufw_status_numbered(text: str) -> tuple[list[dict], int]:
    rules: list[dict] = []
    skipped = 0
    v4_keys = set()
    pending_v6 = []
    for line in text.splitlines():
        match = _UFW_STATUS_RE.match(line.strip())
        if not match:
            continue
        direction = (match.group("dir") or "IN").upper()
        to_side = _parse_ufw_status_side(match.group("to"))
        from_side = _parse_ufw_status_side(match.group("frm"))
        if direction == "FWD" or to_side is None or from_side is None or from_side["port"]:
            skipped += 1
            continue
        rule = normalize_rule({
            "action": match.group("action").lower(),
            "direction": direction.lower(),
            "interface": to_side["iface"] or from_side["iface"],
            "proto": to_side["proto"],
            "source": from_side["addr"],
            "destination": to_side["addr"],
            "port": to_side["port"],
            "comment": match.group("comment") or "",
        })
        if to_side["v6"] or from_side["v6"]:
            pending_v6.append(rule)
        else:
            v4_keys.add(rule_key(rule))
            rules.append(rule)
    for rule in pending_v6:
        if rule_key(rule) not in v4_keys:
            rules.append(rule)
    return rules, skipped


@dataclass
class FirewallImportResult:
    rules: list[dict] = field(default_factory=list)
    skipped: int = 0
    error: str = ""


class _ImportError(Exception):
    pass


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _looks_missing(result) -> bool:
    text = f"{result.stderr}\n{result.stdout}".lower()
    return result.returncode == 127 or "not found" in text or "no such file" in text


def _run_checked(attempts: list[list[str]], tool: str) -> str:
    last = None
    for argv in attempts:
        result = run_privileged(argv, timeout=20)
        if result is None:
            raise _ImportError(auth_required_message())
        if result.returncode == 0:
            return result.stdout or ""
        last = result
    if last is not None and _looks_missing(last):
        raise _ImportError(tr("'{tool}' is not installed or not available.", tool=tool))
    detail = _first_line(last.stderr or last.stdout) if last is not None else ""
    raise _ImportError(tr("The firewall query failed: {detail}", detail=detail or tr("unknown error")))


def _read_ufw_defaults() -> list[dict]:
    text = ""
    try:
        text = Path("/etc/default/ufw").read_text(encoding="utf-8", errors="replace")
    except OSError:
        result = run_privileged(["cat", "/etc/default/ufw"], timeout=10)
        if result is not None and result.returncode == 0:
            text = result.stdout or ""
    rules = []
    for var, direction in (("DEFAULT_INPUT_POLICY", "in"), ("DEFAULT_OUTPUT_POLICY", "out")):
        match = re.search(rf'^\s*{var}\s*=\s*"?([A-Za-z]+)"?', text, re.MULTILINE)
        policy = _UFW_POLICIES.get(match.group(1).upper()) if match else None
        if policy and f"default {policy}" != BASELINE_DEFAULTS[direction]:
            rules.append(normalize_rule({"action": f"default {policy}", "direction": direction}))
    return rules


def import_ufw_rules() -> FirewallImportResult:
    rules: list[dict] = []
    skipped = 0
    out = _run_checked([["ufw", "show", "added"]], "ufw")
    lines = [ln.strip() for ln in out.splitlines()]
    has_rule_lines = any(ln.startswith("ufw ") for ln in lines)
    for line in lines:
        if not line.startswith("ufw "):
            continue
        parsed = parse_raw_rules(line)
        if parsed:
            rules.extend(parsed)
        else:
            skipped += 1
    if not has_rule_lines and "added user rules" not in out.lower():
        status = _run_checked([["ufw", "status", "numbered"]], "ufw")
        rules, skipped = parse_ufw_status_numbered(status)
    return FirewallImportResult(_read_ufw_defaults() + dedupe_rules(rules), skipped)


def _firewalld_service_resolver() -> ServiceResolver:
    cache: dict[str, list[tuple[str, str]] | None] = {}

    def resolve(name: str) -> list[tuple[str, str]] | None:
        if name in cache:
            return cache[name]
        found: list[tuple[str, str]] = []
        try:
            out = _run_checked([
                ["firewall-cmd", "--permanent", f"--service={name}", "--get-ports"],
                ["firewall-offline-cmd", f"--service={name}", "--get-ports"],
            ], "firewall-cmd")
            for item in out.split():
                port, _, proto = item.partition("/")
                if proto.lower() in ("tcp", "udp") and port:
                    found.append((port, proto.lower()))
        except _ImportError:
            found = []
        cache[name] = found or None
        return cache[name]

    return resolve


def import_firewalld_rules() -> FirewallImportResult:
    zone = _run_checked([
        ["firewall-cmd", "--get-default-zone"],
        ["firewall-offline-cmd", "--get-default-zone"],
    ], "firewall-cmd").strip() or "public"

    def query(option: str) -> str:
        return _run_checked([
            ["firewall-cmd", "--permanent", f"--zone={zone}", option],
            ["firewall-offline-cmd", f"--zone={zone}", option],
        ], "firewall-cmd")

    resolver = _firewalld_service_resolver()
    rules: list[dict] = []
    skipped = 0

    for item in query("--list-ports").split():
        parsed = _port_spec_rules(item)
        if parsed:
            rules.extend(parsed)
        else:
            skipped += 1

    for name in query("--list-services").split():
        parsed = _service_rules(name, "allow", resolver)
        if parsed:
            rules.extend(parsed)
        else:
            skipped += 1

    for line in query("--list-rich-rules").splitlines():
        if not line.strip():
            continue
        parsed = _parse_rich_rule(line.strip(), resolver)
        if parsed:
            rules.extend(parsed)
        else:
            skipped += 1

    return FirewallImportResult(dedupe_rules(rules), skipped)


def read_system_rules(backend: str) -> FirewallImportResult:
    try:
        if backend == "ufw":
            return import_ufw_rules()
        if backend == "firewalld":
            return import_firewalld_rules()
    except _ImportError as exc:
        return FirewallImportResult(error=str(exc))
    return FirewallImportResult(error=tr("Unsupported firewall backend: {backend}", backend=backend))


def _row_cells(rule: dict, hide_both: bool) -> tuple[str, ...]:
    norm = normalize_rule(rule)
    if is_default_action(norm["action"]):
        return norm["action"], norm["direction"], "", "", "", "", "", norm["comment"]
    proto = "" if hide_both and norm["proto"] == "both" else norm["proto"]
    return (
        norm["action"], norm["direction"], norm["interface"], proto,
        norm["source"] or tr("any"), norm["destination"] or tr("any"),
        norm["port"], norm["comment"],
    )


class RuleDialog(QDialog):
    def __init__(self, parent=None, rule=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Edit Rule") if rule else tr("Add Rule"))
        self.rule = normalize_rule(rule) if rule else {}
        self.setMinimumWidth(420)
        lay = QFormLayout(self)

        self.action_cb = QComboBox()
        self.action_cb.addItems(_ACTION_CHOICES)
        self.action_cb.setCurrentText(self.rule.get("action", "allow"))

        self.dir_cb = QComboBox()
        self.dir_cb.addItems(["in", "out"])
        self.dir_cb.setCurrentText(self.rule.get("direction", "in"))

        self.iface_ed = QLineEdit(str(self.rule.get("interface", "")))
        self.iface_ed.setPlaceholderText(tr("e.g. eth0"))

        self.proto_cb = QComboBox()
        self.proto_cb.addItems(["both", "tcp", "udp"])
        self.proto_cb.setCurrentText(self.rule.get("proto", "both"))

        self.src_ed = QLineEdit(str(self.rule.get("source", "")))
        self.src_ed.setPlaceholderText(tr("e.g. 192.168.0.0/24 or any"))

        self.dst_ed = QLineEdit(str(self.rule.get("destination", "")))
        self.dst_ed.setPlaceholderText(tr("e.g. 192.168.0.0/24 or any"))

        self.port_ed = QLineEdit(str(self.rule.get("port", "")))
        self.port_ed.setPlaceholderText(tr("e.g. 1982"))

        self.comment_ed = QLineEdit(str(self.rule.get("comment", "")))
        self.comment_ed.setPlaceholderText(tr("e.g. Yeelight Discovery"))

        lay.addRow(tr("Action:"), self.action_cb)
        lay.addRow(tr("Direction:"), self.dir_cb)
        lay.addRow(tr("Interface:"), self.iface_ed)
        lay.addRow(tr("Protocol:"), self.proto_cb)
        lay.addRow(tr("Source:"), self.src_ed)
        lay.addRow(tr("Destination:"), self.dst_ed)
        lay.addRow(tr("Port:"), self.port_ed)
        lay.addRow(tr("Comment:"), self.comment_ed)

        lay.addWidget(ok_cancel_buttons(self, self._on_accept))

        self._match_widgets = (self.iface_ed, self.proto_cb, self.src_ed, self.dst_ed, self.port_ed)
        self.action_cb.currentTextChanged.connect(self._sync_enabled)
        self._sync_enabled(self.action_cb.currentText())

    def _sync_enabled(self, action: str) -> None:
        enabled = not is_default_action(action)
        for widget in self._match_widgets:
            widget.setEnabled(enabled)

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _on_accept(self) -> None:
        if not is_default_action(self.action_cb.currentText()):
            source = self.src_ed.text().strip()
            if not is_valid_source(source):
                self._warn(tr("Invalid Source"),
                           tr("'{value}' is not a valid IP address or network (e.g. 192.168.0.0/24).", value=source))
                return
            destination = self.dst_ed.text().strip()
            if not is_valid_source(destination):
                self._warn(tr("Invalid Destination"),
                           tr("'{value}' is not a valid IP address or network (e.g. 192.168.0.0/24).",
                              value=destination))
                return
            port = re.sub(r"\s+", "", self.port_ed.text())
            bare_port = port.rpartition("/")[0] if port.lower().endswith(("/tcp", "/udp")) else port
            if not is_valid_port(bare_port):
                self._warn(tr("Invalid Port"),
                           tr("'{value}' is not a valid port, port range or service name "
                              "(e.g. 22, 6000:6007, 80,443 or ssh).", value=port))
                return
            iface = self.iface_ed.text().strip()
            if not is_valid_interface(iface):
                self._warn(tr("Invalid Interface"),
                           tr("'{value}' is not a valid network interface name.", value=iface))
                return
        self.accept()

    def get_rule(self):
        return normalize_rule({
            "action": self.action_cb.currentText(),
            "direction": self.dir_cb.currentText(),
            "interface": self.iface_ed.text().strip(),
            "proto": self.proto_cb.currentText(),
            "source": self.src_ed.text().strip(),
            "destination": self.dst_ed.text().strip(),
            "port": self.port_ed.text().strip(),
            "comment": self.comment_ed.text().strip(),
        })


class FirewallSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Firewall Settings"))
        self.setMinimumSize(1100, 720)
        self.rules = [normalize_rule(r) for r in S.firewall_config.get("rules", [])]

        lay = QVBoxLayout(self)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel(tr("Firewall Backend:")))

        self.backend_cb = QComboBox()
        self.backend_cb.addItems(["ufw", "firewalld"])

        saved_backend = S.firewall_config.get("backend", "")
        initial_backend = saved_backend if saved_backend in ("ufw", "firewalld") \
            else LinuxDistroHelper().get_firewall_service_name()
        if initial_backend in ("ufw", "firewalld"):
            self.backend_cb.setCurrentText(initial_backend)

        top_row.addWidget(self.backend_cb)
        top_row.addStretch()
        import_btn = QPushButton(tr("Import Existing Rules"))
        import_btn.clicked.connect(self._import_rules)
        top_row.addWidget(import_btn)
        lay.addLayout(top_row)

        quick_row = QHBoxLayout()
        quick_row.addWidget(QLabel(tr("Raw Rule:")))
        self.raw_input = QLineEdit()
        self.raw_input.setPlaceholderText(
            tr("e.g. ufw allow 22/tcp  OR  sudo ufw default reject incoming  OR  "
               "firewall-cmd --permanent --add-port=80/tcp"))
        self.raw_input.returnPressed.connect(self._add_raw_rule)
        quick_add_btn = QPushButton(tr("Quick Add"))
        quick_add_btn.clicked.connect(self._add_raw_rule)
        quick_row.addWidget(self.raw_input, 1)
        quick_row.addWidget(quick_add_btn)
        lay.addLayout(quick_row)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            tr("Action"), tr("Dir"), tr("Interface"), tr("Proto"),
            tr("Source"), tr("Destination"), tr("Port"), tr("Comment"),
        ])

        header = self.table.horizontalHeader()
        if header is not None:
            for col in range(7):
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)

        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda *_: self._edit_rule())
        lay.addWidget(self.table)

        self._refresh_table()

        btn_row = QHBoxLayout()
        for label, slot in (
            (tr("Add Rule"), self._add_rule),
            (tr("Edit Rule"), self._edit_rule),
            (tr("Remove Rule"), self._del_rule),
            (tr("Move Up"), lambda: self._move_rule(-1)),
            (tr("Move Down"), lambda: self._move_rule(1)),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            btn_row.addWidget(btn)
        lay.addLayout(btn_row)

        lay.addWidget(ok_cancel_buttons(self, self._save))

    def _refresh_table(self, select_row: int = -1):
        self.table.setRowCount(len(self.rules))
        for i, r in enumerate(self.rules):
            for col, value in enumerate(_row_cells(r, hide_both=False)):
                self.table.setItem(i, col, QTableWidgetItem(value))
        self.table.setColumnHidden(2, not any(normalize_rule(r)["interface"] for r in self.rules))
        if 0 <= select_row < len(self.rules):
            self.table.selectRow(select_row)

    def _append_rules(self, new_rules: list[dict]) -> int:
        existing = {rule_key(r) for r in self.rules}
        added = 0
        for rule in new_rules:
            norm = normalize_rule(rule)
            key = rule_key(norm)
            if key in existing:
                continue
            existing.add(key)
            self.rules.append(norm)
            added += 1
        self._refresh_table(len(self.rules) - 1 if added else -1)
        return added

    def _add_raw_rule(self):
        raw = self.raw_input.text().strip()
        if not raw:
            return
        rules = parse_raw_rules(raw)
        if not rules:
            QMessageBox.warning(
                self, tr("Rule not recognised"),
                tr("'{rule}' could not be interpreted. Use ufw syntax (e.g. 'ufw allow 22/tcp') or "
                   "firewall-cmd syntax (e.g. 'firewall-cmd --add-port=80/tcp'), or use 'Add Rule'.",
                   rule=raw))
            return
        self._append_rules(rules)
        self.raw_input.clear()

    def _import_rules(self):
        from privileged import SudoSession, authenticate, release_session

        backend = self.backend_cb.currentText()
        had_password = SudoSession.instance().has_password()
        if not authenticate(self):
            QMessageBox.warning(self, tr("Authentication Required"),
                                tr("Reading the current firewall rules requires administrator rights."))
            return
        own_session = not had_password and SudoSession.instance().has_password()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = read_system_rules(backend)
        finally:
            QApplication.restoreOverrideCursor()
            if own_session:
                release_session()

        if result.error:
            QMessageBox.warning(self, tr("Import Failed"), result.error)
            return
        if not result.rules:
            QMessageBox.information(self, tr("Import Existing Rules"),
                                    tr("No existing firewall rules were found."))
            return

        replace = False
        if self.rules:
            box = QMessageBox(self)
            box.setWindowTitle(tr("Import Existing Rules"))
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(tr("{n} rule(s) found. Replace the current list or merge them into it?",
                           n=len(result.rules)))
            replace_btn = box.addButton(tr("Replace"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(tr("Merge"), QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or box.standardButton(clicked) == QMessageBox.StandardButton.Cancel:
                return
            replace = clicked is replace_btn

        if replace:
            self.rules = []
        self._append_rules(result.rules)

        if result.skipped:
            QMessageBox.information(
                self, tr("Import Existing Rules"),
                tr("{n} rule(s) could not be imported because they use features this editor does not "
                   "support (e.g. application profiles or routing).", n=result.skipped))

    def _add_rule(self):
        dlg = RuleDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.rules.append(dlg.get_rule())
            self._refresh_table(len(self.rules) - 1)

    def _edit_rule(self):
        row = self.table.currentRow()
        if row < 0:
            return
        dlg = RuleDialog(self, self.rules[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.rules[row] = dlg.get_rule()
            self._refresh_table(row)

    def _del_rule(self):
        row = self.table.currentRow()
        if row < 0:
            return
        del self.rules[row]
        self._refresh_table(min(row, len(self.rules) - 1))

    def _move_rule(self, delta: int):
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < len(self.rules):
            return
        self.rules[row], self.rules[target] = self.rules[target], self.rules[row]
        self._refresh_table(target)

    def _save(self):
        S.firewall_config = {
            "backend": self.backend_cb.currentText(),
            "rules": self.rules,
        }
        save_profile()
        self.accept()


def firewall_rules_tooltip() -> str:
    rules = S.firewall_config.get("rules", [])
    if not rules:
        return tr("No custom firewall rules configured.")

    t = current_theme()
    norms = [normalize_rule(r) for r in rules]
    show_iface = any(r["interface"] for r in norms)
    show_dest = any(r["destination"] for r in norms)

    headers = [("#", None), (tr("Action"), 0), (tr("Dir"), 1), (tr("Interface"), 2), (tr("Proto"), 3),
               (tr("Source"), 4), (tr("Destination"), 5), (tr("Port"), 6), (tr("Comment"), 7)]
    visible = [(h, idx) for h, idx in headers
               if not (idx == 2 and not show_iface) and not (idx == 5 and not show_dest)]

    header_cells = "".join(
        f"<th style='text-align:left;padding:3px 14px 3px 4px;"
        f"border-bottom:2px solid {t['header_sep']};'>{html.escape(h)}</th>"
        for h, _ in visible
    )
    rows = []
    for i, norm in enumerate(norms):
        cells = _row_cells(norm, hide_both=True)
        row_bg = f"background:{t['bg2']};" if i % 2 else ""
        num_cell = (f"<td style='padding:3px 14px 3px 4px;text-align:right;"
                    f"color:{t['muted']};border-bottom:1px solid {t['header_sep']};'>{i + 1}</td>")
        body = "".join(
            f"<td style='padding:3px 14px 3px 4px;"
            f"border-bottom:1px solid {t['header_sep']};'>{html.escape(cells[idx])}</td>"
            for _, idx in visible if idx is not None
        )
        rows.append(f"<tr style='{row_bg}'>{num_cell}{body}</tr>")

    return (f"<b>{tr('Firewall Rules:')}</b>"
            f"<table style='border-collapse:collapse;margin-top:4px;'>"
            f"<tr>{header_cells}</tr>{''.join(rows)}</table>")
