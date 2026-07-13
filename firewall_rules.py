import ipaddress

VALID_ACTIONS = ("allow", "deny", "reject", "limit")
VALID_DEFAULT_ACTIONS = ("default allow", "default deny")
VALID_DIRECTIONS = ("in", "out")
VALID_PROTOS = ("both", "tcp", "udp")


def is_port_range(port: str) -> bool:
    return bool(port) and ":" in port


def port_to_firewalld(port: str) -> str:
    return port.replace(":", "-") if port else port


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


def normalize_rule(rule: dict) -> dict:
    action = str(rule.get("action", "allow")).strip().lower()
    if action not in VALID_ACTIONS and action not in VALID_DEFAULT_ACTIONS:
        action = "allow"

    direction = str(rule.get("direction", "in")).strip().lower()
    if direction not in VALID_DIRECTIONS:
        direction = "in"

    proto = str(rule.get("proto", "both")).strip().lower()
    if proto not in VALID_PROTOS:
        proto = "both"

    port = str(rule.get("port", "")).strip()
    source = str(rule.get("source", "")).strip()
    if source.lower() == "any":
        source = ""

    if is_port_range(port) and proto == "both":
        proto = "tcp"

    return {
        "action": action,
        "direction": direction,
        "proto": proto,
        "source": source,
        "port": port,
        "comment": str(rule.get("comment", "")).strip(),
    }


def build_ufw_command(rule: dict) -> list[str]:
    rule = normalize_rule(rule)
    action = rule["action"]
    direction = rule["direction"]

    if action.startswith("default "):
        policy = action.split()[1]
        dir_mapped = "incoming" if direction == "in" else "outgoing"
        return ["sudo", "ufw", "default", policy, dir_mapped]

    cmd = ["sudo", "ufw", action]
    if direction:
        cmd.append(direction)

    if rule["source"]:
        cmd.extend(["from", rule["source"]])
    else:
        cmd.extend(["from", "any"])

    if rule["port"]:
        cmd.extend(["to", "any", "port", rule["port"]])
        if rule["proto"] != "both":
            cmd.extend(["proto", rule["proto"]])
    elif rule["proto"] != "both":
        cmd.extend(["to", "any", "proto", rule["proto"]])

    if rule["comment"]:
        cmd.extend(["comment", rule["comment"]])

    return cmd


def build_ufw_commands(rules: list[dict]) -> list[list[str]]:
    cmds = [["sudo", "ufw", "default", "deny", "incoming"], ["sudo", "ufw", "default", "allow", "outgoing"]]
    for rule in rules:
        cmds.append(build_ufw_command(rule))
    cmds.extend([["sudo", "ufw", "--force", "enable"], ["sudo", "ufw", "reload"]])
    return cmds


def build_firewalld_rich_rule(rule: dict, *, _skip_normalize: bool = False) -> str | None:
    if not _skip_normalize:
        rule = normalize_rule(rule)
    action_val = rule["action"]
    if action_val.startswith("default "):
        return None

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

    src = f' source address="{rule["source"]}"' if rule["source"] else ""

    port = ""
    proto = ""
    if rule["port"]:
        firewalld_port = port_to_firewalld(rule["port"])
        proto_val = rule["proto"] if rule["proto"] != "both" else "tcp"
        port = f' port port="{firewalld_port}" protocol="{proto_val}"'
    elif rule["proto"] != "both":
        proto = f' protocol="{rule["proto"]}"'

    return f'rule family="{source_family(rule["source"])}"{src}{port}{proto} {action}{limit_clause}'


def build_firewalld_commands(rules: list[dict]) -> list[list[str]]:
    cmds = [["sudo", "firewall-cmd", "--set-default-zone=drop"]]
    for rule in rules:
        norm = normalize_rule(rule)
        action_val = norm["action"]

        if action_val.startswith("default "):
            zone = "trusted" if "allow" in action_val else "drop"
            cmds.append(["sudo", "firewall-cmd", f"--set-default-zone={zone}"])
            continue

        rich_rule = build_firewalld_rich_rule(norm, _skip_normalize=True)
        if rich_rule:
            cmds.append(["sudo", "firewall-cmd", "--permanent", f"--add-rich-rule={rich_rule}"])

    cmds.append(["sudo", "firewall-cmd", "--reload"])
    return cmds
