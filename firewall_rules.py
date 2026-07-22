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

    cmd = ["sudo", "ufw", action, direction]

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


def build_firewalld_rich_rule(rule: dict) -> str | None:
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
        action_val = normalize_rule(rule)["action"]

        if action_val.startswith("default "):
            zone = "trusted" if "allow" in action_val else "drop"
            cmds.append(["sudo", "firewall-cmd", f"--set-default-zone={zone}"])
            continue

        rich_rule = build_firewalld_rich_rule(rule)
        if rich_rule:
            cmds.append(["sudo", "firewall-cmd", "--permanent", f"--add-rich-rule={rich_rule}"])

    cmds.append(["sudo", "firewall-cmd", "--reload"])
    return cmds


import shlex

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QComboBox,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)

from linux_distro_helper import LinuxDistroHelper
from state import S, save_profile
from themes import current_theme
from ui_utils import ok_cancel_buttons


class RuleDialog(QDialog):
    def __init__(self, parent=None, rule=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Rule" if rule else "Add Rule")
        self.rule = rule or {}
        self.setMinimumWidth(400)
        lay = QFormLayout(self)

        self.action_cb = QComboBox()
        self.action_cb.addItems(["allow", "deny", "reject", "limit", "default allow", "default deny"])
        self.action_cb.setCurrentText(self.rule.get("action", "allow"))

        self.dir_cb = QComboBox()
        self.dir_cb.addItems(["in", "out"])
        self.dir_cb.setCurrentText(self.rule.get("direction", "in"))

        self.proto_cb = QComboBox()
        self.proto_cb.addItems(["both", "tcp", "udp"])
        self.proto_cb.setCurrentText(self.rule.get("proto", "both"))

        self.src_ed = QLineEdit(str(self.rule.get("source", "")))
        self.src_ed.setPlaceholderText("e.g. 192.168.0.0/24 or any")

        self.port_ed = QLineEdit(str(self.rule.get("port", "")))
        self.port_ed.setPlaceholderText("e.g. 1982")

        self.comment_ed = QLineEdit(str(self.rule.get("comment", "")))
        self.comment_ed.setPlaceholderText("e.g. Yeelight Discovery")

        lay.addRow("Action:", self.action_cb)
        lay.addRow("Direction:", self.dir_cb)
        lay.addRow("Protocol:", self.proto_cb)
        lay.addRow("Source:", self.src_ed)
        lay.addRow("Port:", self.port_ed)
        lay.addRow("Comment:", self.comment_ed)

        lay.addWidget(ok_cancel_buttons(self, self.accept))

    def get_rule(self):
        return normalize_rule({
            "action": self.action_cb.currentText(),
            "direction": self.dir_cb.currentText(),
            "proto": self.proto_cb.currentText(),
            "source": self.src_ed.text().strip(),
            "port": self.port_ed.text().strip(),
            "comment": self.comment_ed.text().strip()
        })


class FirewallSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Firewall Settings")
        self.setMinimumSize(1250, 1000)
        self.rules = [normalize_rule(r) for r in S.firewall_config.get("rules", [])]

        lay = QVBoxLayout(self)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Firewall Backend:"))

        self.backend_cb = QComboBox()
        self.backend_cb.addItems(["ufw", "firewalld"])

        saved_backend = S.firewall_config.get("backend", "")
        initial_backend = saved_backend if saved_backend in ("ufw",
                                                             "firewalld") else LinuxDistroHelper().get_firewall_service_name()
        if initial_backend in ("ufw", "firewalld"):
            self.backend_cb.setCurrentText(initial_backend)

        top_row.addWidget(self.backend_cb)
        top_row.addStretch()
        lay.addLayout(top_row)

        quick_row = QHBoxLayout()
        quick_row.addWidget(QLabel("Raw Rule:"))
        self.raw_input = QLineEdit()
        self.raw_input.setPlaceholderText(
            "e.g. sudo ufw default deny  OR  sudo ufw allow in from 192.168.0.0/24 to any port 1982 proto udp comment 'Yeelight'")
        self.raw_input.returnPressed.connect(self._add_raw_rule)
        quick_add_btn = QPushButton("Quick Add")
        quick_add_btn.clicked.connect(self._add_raw_rule)
        quick_row.addWidget(self.raw_input, 1)
        quick_row.addWidget(quick_add_btn)
        lay.addLayout(quick_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Action", "Dir", "Proto", "Source", "Port", "Comment"])

        header = self.table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table)

        self._refresh_table()

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Rule")
        add_btn.clicked.connect(self._add_rule)
        edit_btn = QPushButton("Edit Rule")
        edit_btn.clicked.connect(self._edit_rule)
        del_btn = QPushButton("Remove Rule")
        del_btn.clicked.connect(self._del_rule)

        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        lay.addLayout(btn_row)

        lay.addWidget(ok_cancel_buttons(self, self._save))

    def _refresh_table(self):
        self.table.setRowCount(len(self.rules))
        for i, r in enumerate(self.rules):
            self.table.setItem(i, 0, QTableWidgetItem(str(r.get("action", ""))))
            self.table.setItem(i, 1, QTableWidgetItem(str(r.get("direction", ""))))
            self.table.setItem(i, 2, QTableWidgetItem(str(r.get("proto", ""))))
            self.table.setItem(i, 3, QTableWidgetItem(str(r.get("source", ""))))
            self.table.setItem(i, 4, QTableWidgetItem(str(r.get("port", ""))))
            self.table.setItem(i, 5, QTableWidgetItem(str(r.get("comment", ""))))

    def _add_raw_rule(self):
        raw = self.raw_input.text().strip()
        if not raw: return

        rule = {"action": "allow", "direction": "in", "proto": "both", "source": "", "port": "", "comment": ""}
        success = True

        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = []
            success = False

        if tokens and tokens[0] == "sudo": tokens.pop(0)
        if tokens and tokens[0] == "ufw": tokens.pop(0)

        if not tokens:
            success = False
        elif tokens[0].lower() == "default":
            tokens.pop(0)
            if tokens and tokens[0].lower() in ["allow", "deny", "reject"]:
                rule["action"] = f"default {tokens.pop(0).lower()}"
            else:
                success = False

            if success and tokens and tokens[0].lower() in ["in", "out", "incoming", "outgoing"]:
                dir_val = tokens.pop(0).lower()
                rule["direction"] = "in" if dir_val in ["in", "incoming"] else "out"
            elif success:
                rule["direction"] = "in"

            if success and tokens:
                success = False

            if success:
                rule["comment"] = "Default Policy"

        else:
            action = tokens.pop(0).lower()
            if action in ["allow", "deny", "reject", "limit"]:
                rule["action"] = action
            else:
                success = False

            if success and tokens and tokens[0].lower() in ["in", "out", "incoming", "outgoing"]:
                dir_val = tokens.pop(0).lower()
                rule["direction"] = "in" if dir_val in ["in", "incoming"] else "out"

            i = 0
            while success and i < len(tokens):
                tok = tokens[i].lower()
                if tok == "from" and i + 1 < len(tokens):
                    rule["source"] = tokens[i + 1]
                    if rule["source"].lower() == "any": rule["source"] = ""
                    i += 2
                elif tok == "to":
                    i += 1
                    if i < len(tokens) and tokens[i].lower() == "any":
                        i += 1
                    else:
                        success = False
                elif tok == "port" and i + 1 < len(tokens):
                    rule["port"] = tokens[i + 1]
                    i += 2
                elif tok == "proto" and i + 1 < len(tokens):
                    p = tokens[i + 1].lower()
                    if p in ["tcp", "udp", "both"]:
                        rule["proto"] = p
                    else:
                        success = False
                    i += 2
                elif tok == "comment" and i + 1 < len(tokens):
                    rule["comment"] = " ".join(tokens[i + 1:])
                    i = len(tokens)
                else:
                    success = False

        if success:
            self.rules.append(normalize_rule(rule))
            self._refresh_table()
            self.raw_input.clear()
        else:
            dlg = RuleDialog(self, rule)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.rules.append(dlg.get_rule())
                self._refresh_table()
                self.raw_input.clear()

    def _add_rule(self):
        dlg = RuleDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.rules.append(dlg.get_rule())
            self._refresh_table()

    def _edit_rule(self):
        row = self.table.currentRow()
        if row < 0: return
        dlg = RuleDialog(self, self.rules[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.rules[row] = dlg.get_rule()
            self._refresh_table()

    def _del_rule(self):
        row = self.table.currentRow()
        if row < 0: return
        del self.rules[row]
        self._refresh_table()

    def _save(self):
        S.firewall_config = {
            "backend": self.backend_cb.currentText(),
            "rules": self.rules
        }
        save_profile()
        self.accept()




def firewall_rules_tooltip() -> str:
    rules = S.firewall_config.get("rules", [])
    if not rules:
        return "No custom firewall rules configured."

    t = current_theme()
    header_cells = "".join(
        f"<th style='text-align:left;padding:3px 14px 3px 4px;"
        f"border-bottom:2px solid {t['header_sep']};'>{h}</th>"
        for h in ("#", "Action", "Dir", "Proto", "Source", "Port", "Comment")
    )
    rows = []
    for i, r in enumerate(rules):
        norm = normalize_rule(r)
        action = norm["action"]
        direction = norm["direction"]
        proto = norm["proto"] if norm["proto"] != "both" else ""
        source = norm["source"] or "any"
        port = norm["port"]
        comment = norm["comment"]
        row_bg = f"background:{t['bg2']};" if i % 2 else ""
        num_cell = (f"<td style='padding:3px 14px 3px 4px;text-align:right;"
                    f"color:{t['muted']};border-bottom:1px solid {t['header_sep']};'>{i + 1}</td>")
        cells = "".join(
            f"<td style='padding:3px 14px 3px 4px;"
            f"border-bottom:1px solid {t['header_sep']};'>{v}</td>"
            for v in (action, direction, proto, source, port, comment)
        )
        rows.append(f"<tr style='{row_bg}'>{num_cell}{cells}</tr>")

    return (f"<b>Firewall Rules:</b>"
            f"<table style='border-collapse:collapse;margin-top:4px;'>"
            f"<tr>{header_cells}</tr>{''.join(rows)}</table>")
