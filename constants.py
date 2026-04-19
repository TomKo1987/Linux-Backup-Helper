import re


USER_SHELLS: list[str] = [
    "bash", "fish", "zsh", "elvish", "nushell", "powershell", "xonsh", "ngs"
]


ARCH_KERNEL_VARIANTS: dict[str, tuple[str, str]] = {
    "linux":          ("linux",          "linux-headers"),
    "linux-lts":      ("linux-lts",      "linux-lts-headers"),
    "linux-zen":      ("linux-zen",      "linux-zen-headers"),
    "linux-hardened": ("linux-hardened", "linux-hardened-headers"),
}


PKG_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9\-._+]*$")