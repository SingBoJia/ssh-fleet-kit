#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fleet: 多台服务器的 SSH 登录、交叉访问和批量 Git 操作工具。

只依赖 Python 3.8+ 标准库和系统自带的 OpenSSH 客户端，Linux / macOS / Windows 通用。
机器清单写在 fleet.ini 里（运行 `fleet init` 扫描家目录生成）。

常用命令：
  fleet init                 扫描 ~/.ssh/config 和 ~/.gitconfig 生成 fleet.ini
  fleet bootstrap            一键：生成配置 + setup + 自检
  fleet setup                一次性初始化：密钥、SSH 配置、Git 身份、分发公钥、下发远端配置
  fleet ssh [别名]           登录某台机器；不写别名时弹出选择菜单
  fleet git pull             所有机器、所有仓库并行 git pull（还有 push / status / fetch）
  fleet run -- uptime        在所有机器上执行命令
  fleet check                检查每台机器能否免密登录、每个 Git 账号是否认得出
  fleet hub deploy           把 fleet 部署到主服务器，在主服务器上也能统一操作
"""
from __future__ import annotations

import argparse
import base64
import configparser
import datetime
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__version__ = "1.1.0"

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
INCLUDE_LINE = "Include ~/.ssh/fleet.conf"
INCLUDE_RE = re.compile(r"(?m)^\s*Include\s+~/\.ssh/fleet\.conf\s*$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
GIT_SHORTCUTS = {
    "pull": ["pull", "--ff-only"],
    "push": ["push"],
    "status": ["status", "-sb"],
    "fetch": ["fetch", "--all", "--prune"],
}


def _windows_openssh_path() -> str:
    """定位 Windows 自带 OpenSSH；不存在时退回 PATH 里的 ssh。"""
    p = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
    if p.is_file():
        return p.as_posix()
    found = shutil.which("ssh")
    if found:
        return f'"{found}"' if " " in found else found.replace("\\", "/")
    return "ssh"


WIN_SSH = _windows_openssh_path()


class FleetError(Exception):
    """配置或用法错误，直接把消息展示给用户。"""


# --------------------------------------------------------------------------- 输出

class Color:
    enabled = False

    @classmethod
    def init(cls) -> None:
        cls.enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        if cls.enabled and IS_WINDOWS:
            os.system("")  # 打开 Windows 10+ 控制台的 ANSI 颜色支持

    @classmethod
    def wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text


def green(t: str) -> str: return Color.wrap("32", t)
def red(t: str) -> str: return Color.wrap("31", t)
def yellow(t: str) -> str: return Color.wrap("33", t)
def dim(t: str) -> str: return Color.wrap("2", t)
def blue(t: str) -> str: return Color.wrap("1;34", t)


def say(msg: str) -> None: print("\n" + blue("==> ") + msg, flush=True)
def ok(msg: str) -> None: print("  " + green("OK  ") + " " + msg, flush=True)
def fail(msg: str) -> None: print("  " + red("FAIL") + " " + msg, flush=True)
def note(msg: str) -> None: print("  " + yellow("提示") + " " + msg, flush=True)
def info(msg: str) -> None: print("  " + msg, flush=True)


# --------------------------------------------------------------------------- 配置

@dataclass
class Host:
    alias: str
    hostname: str
    port: int = 22
    user: str = ""
    os: str = "linux"          # linux（包括 macOS 等类 Unix）或 windows
    jump: str = ""             # 跳板机别名；写 none 表示强制直连
    forward: bool = False      # 是否转发 ssh-agent（在这台上继续 ssh / git）
    repos: List[str] = field(default_factory=list)

    @property
    def windows(self) -> bool:
        return self.os == "windows"


@dataclass
class GitAccount:
    alias: str
    hostname: str = "github.com"
    key: str = ""
    name: str = ""
    email: str = ""
    dir: str = ""


@dataclass
class ScanResult:
    hosts: List[Host] = field(default_factory=list)
    git: List[GitAccount] = field(default_factory=list)
    keys: List[str] = field(default_factory=list)
    key: str = "id_ed25519"


@dataclass
class Fleet:
    key: str = "id_ed25519_fleet"
    comment: str = "fleet-key"
    hub: str = ""
    via_hub: bool = False
    self_alias: str = ""
    hosts: List[Host] = field(default_factory=list)
    git: List[GitAccount] = field(default_factory=list)
    path: Optional[Path] = None

    def host(self, alias: str) -> Host:
        for h in self.hosts:
            if h.alias == alias:
                return h
        raise FleetError(f"配置里没有叫 {alias} 的机器")

    def find(self, alias: str) -> Optional[Host]:
        return next((h for h in self.hosts if h.alias == alias), None)

    def jump_of(self, h: Host) -> str:
        """实际使用的跳板机：显式设置优先，其次是主服务器（via_hub）。"""
        if h.jump.lower() == "none":
            return ""
        if h.jump:
            return h.jump
        if self.via_hub and self.hub and h.alias != self.hub:
            return self.hub
        return ""

    def chain(self, h: Host) -> List[str]:
        """从本机出发依次经过的机器，最后一个是目标。出现循环时抛错。"""
        out, seen, cur = [h.alias], {h.alias}, h
        while True:
            j = self.jump_of(cur)
            if not j:
                return out
            if j in seen:
                raise FleetError(f"{h.alias} 的跳板设置形成了循环：{' → '.join([j] + out)}")
            seen.add(j)
            out.insert(0, j)
            nxt = self.find(j)
            if nxt is None:
                return out
            cur = nxt

    def route(self, h: Host) -> str:
        return "本机 → " + " → ".join(self.chain(h))

    def ordered(self) -> List[Host]:
        """跳板机排在依赖它的机器前面（先装好跳板机的公钥）。"""
        return sorted(self.hosts, key=lambda h: len(self.chain(h)))

    def select(self, only: Optional[str]) -> List[Host]:
        if not only:
            return list(self.hosts)
        names = [n.strip() for n in only.split(",") if n.strip()]
        for n in names:
            self.host(n)
        return [h for h in self.hosts if h.alias in names]


def _split_repos(value: str) -> List[str]:
    return [p.strip() for p in re.split(r"[;\n]", value or "") if p.strip()]


def parse_config(text: str, path: Optional[Path] = None) -> Fleet:
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#",))
    try:
        cp.read_string(text, source=str(path or "fleet.ini"))
    except configparser.Error as e:
        raise FleetError(f"配置文件格式错误：{e}") from None

    def get(sec: str, key: str, default: str = "") -> str:
        return cp.get(sec, key, fallback=default).strip()

    def getbool(sec: str, key: str) -> bool:
        try:
            return cp.getboolean(sec, key, fallback=False)
        except ValueError:
            raise FleetError(f"[{sec}] 里的 {key} 只能写 yes 或 no") from None

    fl = Fleet(path=path)
    if cp.has_section("settings"):
        fl.key = get("settings", "key", fl.key)
        fl.comment = get("settings", "comment", fl.comment)
        fl.hub = get("settings", "hub")
        fl.via_hub = getbool("settings", "via_hub")
        fl.self_alias = get("settings", "self")

    for sec in cp.sections():
        if sec == "settings":
            continue
        kind, _, name = sec.partition(" ")
        name = name.strip()
        if kind == "host" and name:
            port_s = get(sec, "port", "22") or "22"
            if not port_s.isdigit():
                raise FleetError(f"[{sec}] 的端口不是数字：{port_s}")
            os_name = get(sec, "os", "linux").lower()
            if os_name in ("mac", "macos", "darwin", "unix"):
                os_name = "linux"
            if os_name not in ("linux", "windows"):
                raise FleetError(f"[{sec}] 的 os 只能是 linux 或 windows，现在是 {os_name}")
            fl.hosts.append(Host(
                alias=name, hostname=get(sec, "hostname") or get(sec, "host"), port=int(port_s),
                user=get(sec, "user"), os=os_name, jump=get(sec, "jump"),
                forward=getbool(sec, "forward"), repos=_split_repos(cp.get(sec, "repos", fallback="")),
            ))
        elif kind == "git" and name:
            fl.git.append(GitAccount(
                alias=name, hostname=get(sec, "hostname", "github.com") or "github.com",
                key=get(sec, "key"), name=get(sec, "name"), email=get(sec, "email"), dir=get(sec, "dir"),
            ))
        else:
            raise FleetError(f"无法识别的配置段 [{sec}]，应为 [settings]、[host 别名] 或 [git 别名]")
    validate(fl)
    return fl


def validate(fl: Fleet) -> None:
    if not NAME_RE.match(fl.key):
        raise FleetError(f"settings.key 只能包含字母、数字、点、下划线和连字符：{fl.key}")
    seen: Dict[str, str] = {}
    for h in fl.hosts:
        if not NAME_RE.match(h.alias):
            raise FleetError(f"机器别名只能包含字母、数字、点、下划线和连字符：{h.alias}")
        if h.alias in seen:
            raise FleetError(f"别名 {h.alias} 重复了")
        seen[h.alias] = "host"
        if not h.hostname:
            raise FleetError(f"[host {h.alias}] 缺少 hostname")
    for g in fl.git:
        if not NAME_RE.match(g.alias):
            raise FleetError(f"Git 账号别名只能包含字母、数字、点、下划线和连字符：{g.alias}")
        if g.alias in seen:
            raise FleetError(f"别名 {g.alias} 重复了（机器和 Git 账号不能同名）")
        seen[g.alias] = "git"
        if not g.key or not NAME_RE.match(g.key):
            raise FleetError(f"[git {g.alias}] 需要填 key（密钥文件名，例如 id_ed25519_{g.alias}）")
    if fl.hub and fl.hub not in seen:
        raise FleetError(f"settings.hub 指向的 {fl.hub} 不在机器列表里")
    for h in fl.hosts:
        j = fl.jump_of(h)
        if j == h.alias:
            raise FleetError(f"{h.alias} 不能把自己设为跳板机")
        if j and seen.get(j) != "host":
            raise FleetError(f"{h.alias} 的跳板机 {j} 不在机器列表里")
        fl.chain(h)  # 检查循环


def find_config(explicit: Optional[str]) -> Path:
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    elif os.environ.get("FLEET_CONFIG"):
        candidates.append(Path(os.environ["FLEET_CONFIG"]).expanduser())
    else:
        candidates += [Path.cwd() / "fleet.ini", Path.home() / ".fleet" / "fleet.ini"]
    for c in candidates:
        if c.is_file():
            return c
    tried = "、".join(str(c) for c in candidates)
    raise FleetError(f"找不到配置文件（找过：{tried}）。先运行 fleet init 生成一个。")


def load_config(explicit: Optional[str]) -> Fleet:
    p = find_config(explicit)
    return parse_config(p.read_text(encoding="utf-8-sig"), p)


# --------------------------------------------------------------------------- 家目录扫描

SCAN_SKIP_INCLUDES = {"fleet.conf"}
PLACEHOLDER_GIT_NAMES = {"your name", "your_name"}
PLACEHOLDER_GIT_EMAILS = {"you@company.com", "you@example.com", "your@email.com"}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _expand_include(value: str, base: Path) -> List[Path]:
    raw = value.strip().strip('"')
    if not raw:
        return []
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = base / p
    return [Path(x) for x in sorted(glob.glob(str(p)))]


def _ssh_config_files(path: Path, seen: Optional[set] = None) -> List[Path]:
    """读取 SSH config 及其 Include；跳过 fleet 自己生成的 fleet.conf。"""
    seen = seen or set()
    try:
        real = path.resolve()
    except OSError:
        real = path
    if real in seen or not path.is_file() or path.name.lower() in SCAN_SKIP_INCLUDES:
        return []
    seen.add(real)
    out = [path]
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "include":
            for inc in _expand_include(parts[1], path.parent):
                out.extend(_ssh_config_files(inc, seen))
    return out


def _parse_ssh_file(path: Path) -> List[Dict[str, object]]:
    blocks: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        else:
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            key, value = parts[0], parts[1]
        key, value = key.strip().lower(), value.strip()
        if key == "host":
            aliases = [a for a in value.split()
                       if not a.startswith("!") and "*" not in a and "?" not in a]
            cur = {"aliases": aliases, "hostname": "", "user": "", "port": 22,
                   "jump": "", "forward": False}
            blocks.append(cur)
        elif cur is None:
            continue
        elif key == "hostname":
            cur["hostname"] = value
        elif key == "user":
            cur["user"] = value
        elif key == "port" and value.isdigit():
            cur["port"] = int(value)
        elif key == "proxyjump":
            if value.lower() == "none":
                cur["jump"] = "none"
            else:
                first = value.split(",", 1)[0].strip()
                first = first.split("@")[-1].split(":", 1)[0]
                cur["jump"] = first
        elif key == "forwardagent":
            cur["forward"] = value.lower() in ("yes", "true", "1")
    return blocks


def scan_ssh_hosts(config: Path) -> List[Host]:
    """从 ~/.ssh/config 扫描真实 Host；通配 Host 和 fleet.conf 不导入。"""
    blocks: List[Dict[str, object]] = []
    for path in _ssh_config_files(config):
        blocks.extend(_parse_ssh_file(path))
    hosts: List[Host] = []
    seen = set()
    for block in blocks:
        aliases = block["aliases"]
        assert isinstance(aliases, list)
        for alias in aliases:
            if alias in seen:
                continue
            seen.add(alias)
            hostname = str(block["hostname"] or alias).replace("%h", alias)
            hosts.append(Host(
                alias=alias, hostname=hostname, port=int(block["port"]),
                user=str(block["user"]), os="linux",
                jump=str(block["jump"]), forward=bool(block["forward"]),
            ))
    known = {h.alias for h in hosts}
    for h in hosts:
        if h.jump and h.jump != "none" and h.jump not in known:
            h.jump = ""
    return hosts


def _git_identity(path: Path) -> Tuple[str, str]:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(path, encoding="utf-8-sig")
    except configparser.Error:
        return "", ""
    if not cp.has_section("user"):
        return "", ""
    return cp.get("user", "name", fallback="").strip(), cp.get("user", "email", fallback="").strip()


def scan_git_accounts(home: Path, default_key: str) -> List[GitAccount]:
    """从 ~/.gitconfig 的 includeIf 导入非示例 Git 身份。"""
    cfg = home / ".gitconfig"
    if not cfg.is_file():
        return []
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(cfg, encoding="utf-8-sig")
    except configparser.Error:
        return []
    accounts: List[GitAccount] = []
    seen = set()
    for section in cp.sections():
        if not section.lower().startswith("includeif "):
            continue
        condition = section[len("includeIf "):].strip().strip('"')
        m = re.match(r"gitdir/i?:(.+)$", condition, re.I)
        if not m:
            continue
        git_dir = m.group(1).strip().strip('"')
        inc_raw = cp.get(section, "path", fallback="").strip()
        if not inc_raw:
            continue
        if inc_raw.startswith("~/"):
            inc = home / inc_raw[2:]
        else:
            inc = Path(inc_raw).expanduser()
            if not inc.is_absolute():
                inc = home / inc
        if not inc.is_file():
            continue
        name, email = _git_identity(inc)
        if not name and not email:
            continue
        if name.lower() in PLACEHOLDER_GIT_NAMES or email.lower() in PLACEHOLDER_GIT_EMAILS:
            continue
        alias = inc.name
        for prefix in (".gitconfig-", "gitconfig-"):
            if alias.startswith(prefix):
                alias = alias[len(prefix):]
                break
        alias = re.sub(r"[^A-Za-z0-9._-]+", "-", alias).strip("-") or "git"
        if alias in seen:
            continue
        seen.add(alias)
        host = "github.com"
        if "gitee" in alias.lower():
            host = "gitee.com"
        elif "gitlab" in alias.lower():
            host = "gitlab.com"
        accounts.append(GitAccount(alias=alias, hostname=host, key=default_key,
                                   name=name, email=email, dir=git_dir))
    return accounts


def scan_home(home: Optional[Path] = None) -> ScanResult:
    """扫描家目录：~/.ssh/config 的主机 + ~/.gitconfig 的 Git 身份。"""
    home = Path(home).expanduser() if home else Path.home()
    ssh_dir = home / ".ssh"
    result = ScanResult()
    for candidate in ("id_ed25519", "id_rsa", "id_ecdsa"):
        if (ssh_dir / candidate).is_file():
            result.key = candidate
            break
    config = ssh_dir / "config"
    if config.is_file():
        result.hosts = scan_ssh_hosts(config)
    result.git = scan_git_accounts(home, result.key)
    return result


def render_scanned_config(scan: ScanResult) -> str:
    lines = [
        "# fleet 配置：由 fleet init 扫描家目录生成",
        "# 已跳过 ~/.ssh/fleet.conf 和 Your Name / you@example.com 之类的示例身份",
        "# 请核对 hostname、user、repos 后再运行 fleet setup",
        "",
        "[settings]",
        f"key = {scan.key}",
        "comment = fleet-key",
        "hub =",
        "via_hub = no",
        "",
    ]
    for h in scan.hosts:
        lines.append(f"[host {h.alias}]")
        lines.append(f"hostname = {h.hostname}")
        if h.port != 22:
            lines.append(f"port = {h.port}")
        if h.user:
            lines.append(f"user = {h.user}")
        lines.append(f"os = {h.os}")
        if h.jump:
            lines.append(f"jump = {h.jump}")
        if h.forward:
            lines.append("forward = yes")
        lines.append("")
    for g in scan.git:
        lines.append(f"[git {g.alias}]")
        lines.append(f"hostname = {g.hostname}")
        lines.append(f"key = {g.key}")
        if g.name:
            lines.append(f"name = {g.name}")
        if g.email:
            lines.append(f"email = {g.email}")
        if g.dir:
            lines.append(f"dir = {g.dir}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- SSH 配置生成

def ssh_config(fl: Fleet, flavor: str, from_alias: Optional[str] = None,
               today: Optional[str] = None) -> str:
    """生成 fleet.conf。flavor 为 unix 或 windows；from_alias 表示给某台远端机器用的版本。"""
    remote = from_alias is not None
    today = today or datetime.date.today().isoformat()

    def idf(k: str) -> str:
        return f"~/.ssh/{k}" + (".pub" if remote else "")

    lines = [f"# fleet.conf - generated by fleet {__version__} on {today}" + (f" for {from_alias}" if remote else "")]
    lines.append(f"# Referenced by the first line of ~/.ssh/config:  {INCLUDE_LINE}")
    if remote:
        lines.append("# IdentityFile points to .pub files: the private key stays on your PC and arrives via agent forwarding")
    lines.append("")
    targets = [h for h in fl.hosts if h.alias != from_alias]
    for h in targets:
        lines.append(f"Host {h.alias}")
        lines.append(f"  HostName {h.hostname}")
        if h.port != 22:
            lines.append(f"  Port {h.port}")
        if h.user:
            lines.append(f"  User {h.user}")
        lines.append(f"  IdentityFile {idf(fl.key)}")
        lines.append("  IdentitiesOnly yes")
        j = fl.jump_of(h)
        if j and j != from_alias:
            lines.append(f"  ProxyJump {j}")
        if h.forward:
            lines.append("  ForwardAgent yes")
        lines.append("")
    for g in fl.git:
        lines.append(f"# Git account: git clone git@{g.alias}:<org>/<repo>.git")
        lines.append(f"Host {g.alias}")
        lines.append(f"  HostName {g.hostname}")
        lines.append("  User git")
        lines.append(f"  IdentityFile {idf(g.key)}")
        lines.append("  IdentitiesOnly yes")
        lines.append("")
    if targets:
        lines.append("Host " + " ".join(h.alias for h in targets))
        lines.append("  ServerAliveInterval 30")
        lines.append("  ServerAliveCountMax 3")
        if not remote:
            lines.append("  AddKeysToAgent yes")
        if flavor == "unix":
            lines.append("  # Reuse one connection per host: password / passphrase is asked once")
            lines.append("  ControlMaster auto")
            lines.append("  ControlPath ~/.ssh/cm-%C")
            lines.append("  ControlPersist 10m")
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- 远端命令

def psq(s: str) -> str:
    """PowerShell 单引号字符串。"""
    return "'" + s.replace("'", "''") + "'"


def ps_encoded(script: str) -> str:
    """在 Windows 远端执行 PowerShell：编码后传输，不受远端默认 shell（cmd 或 PowerShell）和引号影响。"""
    return " ".join(ps_encoded_args(script))


def ps_encoded_args(script: str) -> List[str]:
    """PowerShell -EncodedCommand 的参数列表（本机执行时避免走 cmd.exe）。"""
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]


PS_PROLOGUE = (
    "$ProgressPreference = 'SilentlyContinue'; "
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [Text.UTF8Encoding]::new($false); "
)


def ps_wrap(script: str) -> str:
    """包住用户命令：UTF-8 输出、抑制 progress，并把 cmdlet 错误变成非零退出码。"""
    return (
        PS_PROLOGUE
        + "$ErrorActionPreference = 'Stop'; $global:LASTEXITCODE = 0; "
        + "try {\n"
        + script
        + "\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
        + "exit 0\n"
        + "} catch {\n"
        + "  Write-Output ('ERROR: ' + $_.Exception.Message)\n"
        + "  exit 1\n"
        + "}"
    )


def git_remote_command(h: Host, repo: str, git_args: Sequence[str]) -> str:
    if h.windows:
        args = " ".join(psq(a) for a in git_args)
        return ps_encoded(ps_wrap(f"Set-Location -LiteralPath {psq(repo)}; git {args}"))
    args = " ".join(shlex.quote(a) for a in git_args)
    return f"cd {shlex.quote(repo)} && GIT_TERMINAL_PROMPT=0 git {args}"


def shell_remote_command(h: Host, command: str) -> str:
    if h.windows:
        return ps_encoded(ps_wrap(command))
    return command


PS_ADD_KEY = r"""
$k = '__PUB__'
$ids = [Security.Principal.WindowsIdentity]::GetCurrent().Groups | ForEach-Object { $_.Value }
$admin = $ids -contains 'S-1-5-32-544'
if ($admin) { $f = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys' }
else { $d = Join-Path $env:USERPROFILE '.ssh'; New-Item -ItemType Directory -Force $d | Out-Null; $f = Join-Path $d 'authorized_keys' }
$cur = ''
if (Test-Path $f) { $cur = [IO.File]::ReadAllText($f) }
if (-not $cur.Contains($k)) {
  if ($cur.Length -gt 0 -and -not $cur.EndsWith("`n")) { $cur += "`r`n" }
  [IO.File]::WriteAllText($f, $cur + $k + "`r`n", (New-Object Text.UTF8Encoding $false))
}
if ($admin) { icacls.exe $f /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null }
Write-Output "key installed: $f"
""".strip()

PS_INCLUDE = r"""
$d = Join-Path $env:USERPROFILE '.ssh'
$c = Join-Path $d 'config'
$t = ''
if (Test-Path $c) { $t = [IO.File]::ReadAllText($c) }
if ($t -notmatch '(?m)^\s*Include ~/.ssh/fleet.conf') { [IO.File]::WriteAllText($c, "Include ~/.ssh/fleet.conf`n" + $t, (New-Object Text.UTF8Encoding $false)) }
""".strip()

SH_INCLUDE = ('c=~/.ssh/config; touch "$c" && chmod 600 "$c"; '
              'grep -qxF "Include ~/.ssh/fleet.conf" "$c" || '
              '{ printf "Include ~/.ssh/fleet.conf\\n" | cat - "$c" > "$c.tmp" && mv "$c.tmp" "$c"; }')


def add_key_command(h: Host, pub: str) -> str:
    if h.windows:
        return ps_encoded(PS_ADD_KEY.replace("__PUB__", pub.replace("'", "''")))
    q = shlex.quote(pub)
    return ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
            f"grep -qxF {q} ~/.ssh/authorized_keys || echo {q} >> ~/.ssh/authorized_keys")


def put_file_command(h: Host, rel_dir: str, name: str, mode: str = "600") -> str:
    """返回从 stdin 读取 base64 内容、写到对方 ~/<rel_dir>/<name> 的远端命令。"""
    if h.windows:
        return ps_encoded(
            "$b = [Console]::In.ReadToEnd().Trim(); "
            f"$d = Join-Path $env:USERPROFILE {psq(rel_dir)}; New-Item -ItemType Directory -Force $d | Out-Null; "
            f"[IO.File]::WriteAllBytes((Join-Path $d {psq(name)}), [Convert]::FromBase64String($b))")
    d = "~/" + rel_dir
    return (f"mkdir -p {d} && chmod 700 {d} && tr -d '\\r' | base64 -d > {d}/{name} "
            f"&& chmod {mode} {d}/{name}")


# --------------------------------------------------------------------------- 进程执行

def ssh_args(alias: str, command: Optional[str] = None, batch: bool = True, no_stdin: bool = True,
             timeout: int = 10) -> List[str]:
    args = ["ssh"]
    if no_stdin:
        args.append("-n")
    if batch:
        args += ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}"]
    args.append(alias)
    if command is not None:
        args.append(command)
    return args


def run(args: Sequence[str], *, capture: bool = False, input_text: Optional[str] = None,
        cwd: Optional[str] = None, timeout: Optional[int] = None, shell: bool = False,
        quiet_stdin: bool = True) -> Tuple[int, str]:
    kw: Dict[str, object] = {"cwd": cwd, "timeout": timeout, "shell": shell}
    if input_text is not None:
        kw["input"] = input_text
    elif quiet_stdin:
        kw["stdin"] = subprocess.DEVNULL
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        r = subprocess.run(args if not shell else args[0], text=True, encoding="utf-8",
                           errors="replace", **kw)  # type: ignore[arg-type]
    except subprocess.TimeoutExpired:
        return 124, "超时"
    except FileNotFoundError as e:
        return 127, f"找不到命令：{e.filename}"
    return r.returncode, (r.stdout or "") if capture else ""


def login_ok(alias: str, timeout: int = 10) -> bool:
    code, _ = run(ssh_args(alias, "exit", timeout=timeout), capture=True, timeout=timeout + 20)
    return code == 0


def put_file(h: Host, rel_dir: str, name: str, data: bytes, mode: str = "600") -> bool:
    b64 = base64.b64encode(data).decode("ascii")
    code, out = run(ssh_args(h.alias, put_file_command(h, rel_dir, name, mode), no_stdin=False),
                    capture=True, input_text=b64 + "\n", timeout=120)
    if code != 0 and out.strip():
        info(dim(out.strip()))
    return code == 0


@dataclass
class Task:
    host: str
    label: str
    args: List[str]
    cwd: Optional[str] = None
    shell: bool = False


def run_parallel(tasks: List[Task], jobs: int, timeout: int) -> int:
    """并行执行，按原顺序输出结果，返回失败数。"""
    def one(t: Task) -> Tuple[int, str]:
        return run(t.args, capture=True, cwd=t.cwd, timeout=timeout, shell=t.shell)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(one, tasks))
    width = max((len(t.host) for t in tasks), default=0)
    failed = 0
    for t, (code, out) in zip(tasks, results):
        head = f"{t.host:<{width}}  {t.label}"
        if code == 0:
            print(green("OK  ") + "  " + head)
        else:
            failed += 1
            print(red("FAIL") + "  " + head + red(f"  (退出码 {code})"))
        for line in out.rstrip().splitlines():
            print("      " + line)
    print(f"\n完成：成功 {len(tasks) - failed}，失败 {failed}")
    return failed


# --------------------------------------------------------------------------- 本机操作

def ssh_dir() -> Path:
    return Path.home() / ".ssh"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if not IS_WINDOWS:
        os.chmod(path, 0o600)


def lock_key(path: Path) -> None:
    if IS_WINDOWS:
        user = os.environ.get("USERNAME", "")
        run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], capture=True)
    else:
        os.chmod(path, 0o600)
        pub = Path(str(path) + ".pub")
        if pub.exists():
            os.chmod(pub, 0o644)


def ensure_key(path: Path, comment: str) -> bool:
    if not path.exists():
        info(f"生成 {path.name}（可以设置口令，直接回车表示不设）")
        code, _ = run(["ssh-keygen", "-t", "ed25519", "-f", str(path), "-C", comment], quiet_stdin=False)
        if code != 0 or not path.exists():
            fail(f"生成 {path} 失败")
            return False
    lock_key(path)
    ok(str(path))
    return True


def install_local_config(fl: Fleet) -> None:
    d = ssh_dir()
    d.mkdir(mode=0o700, exist_ok=True)
    write_private(d / "fleet.conf", ssh_config(fl, "windows" if IS_WINDOWS else "unix"))
    cfg = d / "config"
    text = cfg.read_text(encoding="utf-8-sig") if cfg.exists() else ""
    if not INCLUDE_RE.search(text):
        if text:
            backup = cfg.with_name("config.bak." + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
            shutil.copy2(cfg, backup)
            info(f"原配置已备份到 {backup}")
        write_private(cfg, INCLUDE_LINE + "\n" + text)
    ok(f"{d / 'fleet.conf'} 已写入，{cfg} 已引用它")


def powershell(script: str) -> Tuple[int, str]:
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture=True)


def ensure_agent() -> bool:
    if IS_WINDOWS:
        code, out = powershell("(Get-Service ssh-agent -ErrorAction SilentlyContinue).Status")
        status = out.strip()
        if code != 0 or not status:
            fail("没有找到 OpenSSH 客户端：设置 → 系统 → 可选功能 → 添加「OpenSSH 客户端」")
            return False
        if status != "Running":
            code, _ = powershell("Set-Service ssh-agent -StartupType Automatic; Start-Service ssh-agent")
            if code != 0:
                fail("ssh-agent 服务没有启动。请用「以管理员身份运行」的终端再执行一次 fleet setup")
                return False
        ok("ssh-agent 服务正在运行，并已设为开机自动启动")
        return True
    if os.environ.get("SSH_AUTH_SOCK"):
        ok("已检测到 ssh-agent")
        return True
    code, out = run(["ssh-agent", "-s"], capture=True)
    if code != 0:
        fail("ssh-agent 启动失败")
        return False
    for key in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
        m = re.search(key + r"=([^;]+);", out)
        if m:
            os.environ[key] = m.group(1)
    note("本次临时启动了 ssh-agent。想每次开机只输一次口令，请看 README 里的「让 agent 常驻」")
    return True


def loaded_fingerprints() -> set:
    code, out = run(["ssh-add", "-l"], capture=True)
    return {ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 1} if code == 0 else set()


def add_keys(paths: List[Path]) -> None:
    loaded = loaded_fingerprints()
    for p in paths:
        if not p.exists():
            continue
        code, out = run(["ssh-keygen", "-lf", str(p) + ".pub"], capture=True)
        fp = out.split()[1] if code == 0 and len(out.split()) > 1 else ""
        if fp and fp in loaded:
            ok(f"{p.name} 已在 agent 里")
            continue
        args = ["ssh-add"] + (["--apple-use-keychain"] if IS_MAC else []) + [str(p)]
        code, _ = run(args, quiet_stdin=False)
        (ok if code == 0 else fail)(f"{'已加载' if code == 0 else '加载失败'} {p.name}")


def git_dir(d: str) -> str:
    d = d.strip().replace("\\", "/")
    return d if not d or d.endswith("/") else d + "/"


def setup_git(fl: Fleet) -> None:
    if not shutil.which("git"):
        fail("没有找到 git，安装后重新运行 fleet setup")
        return
    if IS_WINDOWS:
        run(["git", "config", "--global", "core.sshCommand", WIN_SSH])
        run(["git", "config", "--global", "core.longpaths", "true"])
        ok("Git 改用 Windows 自带的 OpenSSH（这样才能用 ssh-agent 服务）")
    for g in fl.git:
        inc = f"~/.gitconfig-{g.alias}"
        local = Path.home() / f".gitconfig-{g.alias}"
        if g.dir:
            prefix = "includeIf.gitdir/i:" if IS_WINDOWS else "includeIf.gitdir:"
            run(["git", "config", "--global", f"{prefix}{git_dir(g.dir)}.path", inc])
        if g.name:
            run(["git", "config", "-f", str(local), "user.name", g.name])
        if g.email:
            run(["git", "config", "-f", str(local), "user.email", g.email])
        where = f"{git_dir(g.dir)} 下的仓库自动使用这个身份" if g.dir else "没填 dir，需要在仓库里手动设置 user.name / user.email"
        ok(f"{g.alias}：{where}")


# --------------------------------------------------------------------------- 远端操作

def install_key(h: Host, pub: str) -> bool:
    if login_ok(h.alias):
        ok(f"{h.alias} 已能用密钥登录")
        return True
    info(f"首次连接会显示主机指纹，核对后输入 yes；然后输入 {h.alias} 的登录密码")
    code, _ = run(ssh_args(h.alias, add_key_command(h, pub), batch=False, no_stdin=False), quiet_stdin=False)
    if code != 0:
        fail(f"{h.alias} 安装公钥失败")
        return False
    ok(f"{h.alias} 公钥已安装")
    return True


def push_remote(fl: Fleet, h: Host) -> bool:
    conf = ssh_config(fl, "windows" if h.windows else "unix", from_alias=h.alias)
    if not put_file(h, ".ssh", "fleet.conf", conf.encode("utf-8")):
        fail(f"{h.alias} 远端配置下发失败")
        return False
    inc = ps_encoded(PS_INCLUDE) if h.windows else SH_INCLUDE
    run(ssh_args(h.alias, inc), capture=True, timeout=60)
    pubs = [ssh_dir() / f"{fl.key}.pub"] + [ssh_dir() / f"{g.key}.pub" for g in fl.git]
    for p in pubs:
        if p.exists():
            put_file(h, ".ssh", p.name, p.read_bytes(), mode="644")
    ok(f"{h.alias} 远端配置已下发：登录它以后可以继续 ssh 到其他机器、执行 git")
    return True


def git_account_ok(g: GitAccount) -> Tuple[bool, str]:
    args = ["ssh", "-T", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", g.alias]
    code, out = run(args, capture=True, timeout=40)
    text = out.strip().splitlines()[0] if out.strip() else f"退出码 {code}"
    good = bool(re.search(r"successfully authenticated|Welcome to GitLab|^Hi |Welcome to Gitea", out, re.M))
    return good, text


# --------------------------------------------------------------------------- 子命令

EXAMPLE_INI = """\
# fleet 配置文件。改好后运行：fleet setup
# 每个 [host 别名] 是一台机器，别名就是以后 ssh 用的名字，例如 ssh web1

[settings]
# 登录服务器用的密钥文件名（放在 ~/.ssh/ 下，不存在会自动生成）
key = id_ed25519_fleet
comment = fleet-key
# 主服务器（可选）。via_hub = yes 时，其他机器默认都经过主服务器跳转
hub = web1
via_hub = no

[host web1]
hostname = 10.0.0.11
port = 22
user = root
# linux（包括 macOS）或 windows
os = linux
# yes：登录这台以后，还能继续 ssh 到其他机器、用你的密钥执行 git pull / push
forward = yes
# 这台机器上的仓库，多个用 ; 分隔
repos = /srv/www/site; /opt/api

[host db1]
hostname = 10.0.0.12
port = 2222
user = admin
os = linux
forward = no

[host app1]
hostname = 192.168.1.20
user = deploy
os = linux
# 只能经过 web1 才能访问的内网机器
jump = web1
forward = yes
repos = /home/deploy/app

[host win-srv]
hostname = 10.0.0.30
user = Administrator
os = windows
forward = no
repos = D:\\code\\portal

# 每个 [git 别名] 是一个 Git 账号。克隆时用别名代替域名：git clone git@github-work:org/repo.git
[git github-work]
hostname = github.com
key = id_ed25519_work
name = Your Name
email = you@company.com
# 这个目录下的仓库自动使用上面的名字和邮箱
dir = ~/work/

[git github-personal]
hostname = github.com
key = id_ed25519_personal
name = Your Name
email = you@example.com
dir = ~/personal/
"""


def cmd_init(a: argparse.Namespace) -> int:
    p = Path(a.path).expanduser()
    if p.exists() and not a.force:
        raise FleetError(f"{p} 已经存在。要覆盖请加 --force")
    p.parent.mkdir(parents=True, exist_ok=True)
    if getattr(a, "example", False):
        text = EXAMPLE_INI
        summary = "已生成示例配置"
    else:
        scan = scan_home()
        if scan.hosts or scan.git:
            text = render_scanned_config(scan)
            summary = f"已扫描到 {len(scan.hosts)} 台主机、{len(scan.git)} 个 Git 账号"
        else:
            text = EXAMPLE_INI
            summary = "没有扫描到可用主机，已生成示例配置"
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"{summary}：{p}。核对后运行：fleet setup --dry-run")
    return 0


def _missing_commands() -> List[str]:
    missing = []
    for name in ("ssh", "git"):
        if not shutil.which(name):
            missing.append(name)
    return missing


def _install_hint() -> str:
    if IS_WINDOWS:
        return ("Windows：Git 用 winget install --id Git.Git -e 安装；"
                "OpenSSH 在「设置 → 系统 → 可选功能」添加「OpenSSH 客户端」。")
    if IS_MAC:
        return "macOS：xcode-select --install；没有 Git 时 brew install git。"
    return "Linux：用包管理器安装 openssh-client 和 git（例如 sudo apt install openssh-client git）。"


def cmd_bootstrap(a: argparse.Namespace) -> int:
    """一键：准备配置、检查依赖、执行 setup，并完成自检。"""
    p: Optional[Path] = None
    used_example = False
    if a.config:
        p = Path(a.config).expanduser()
    else:
        try:
            p = find_config(None)
        except FleetError:
            p = Path.home() / ".fleet" / "fleet.ini"
    if not p.exists():
        if a.example:
            text, summary = EXAMPLE_INI, "已生成示例配置"
        else:
            scan = scan_home()
            if scan.hosts or scan.git:
                text = render_scanned_config(scan)
                summary = f"已扫描到 {len(scan.hosts)} 台主机、{len(scan.git)} 个 Git 账号"
            else:
                text, summary = EXAMPLE_INI, "没有扫描到主机，已生成示例配置"
                used_example = True
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"{summary}：{p}")
    if used_example or _read_text(p).strip() == EXAMPLE_INI.strip():
        note(f"{p} 还是示例配置，请先把主机信息改成你自己的，再运行 fleet bootstrap")
        return 1
    fl = load_config(str(p))
    hosts = [h for h in fl.ordered() if h in fl.select(a.only) and h.alias != fl.self_alias]
    if a.dry_run:
        setup_plan(fl, hosts)
        return 0
    missing = _missing_commands()
    if missing:
        fail("缺少命令：" + "、".join(missing))
        note(_install_hint())
        return 1
    print("\n开始一键部署；需要密码的机器会逐台提示。")
    code = cmd_setup(fl, argparse.Namespace(dry_run=False, only=a.only))
    if code == 0:
        ok("一键部署完成")
    return code


def cmd_ls(fl: Fleet, a: argparse.Namespace) -> int:
    w = max([len(h.alias) for h in fl.hosts] + [4])
    for h in fl.hosts:
        tags = []
        if h.alias == fl.hub:
            tags.append("主服务器")
        if h.forward:
            tags.append("转发agent")
        if h.repos:
            tags.append(f"{len(h.repos)} 个仓库")
        addr = f"{h.user + '@' if h.user else ''}{h.hostname}{':' + str(h.port) if h.port != 22 else ''}"
        print(f"{h.alias:<{w}}  {h.os:<7}  {addr:<28}  {dim(fl.route(h))}  {yellow(' '.join(tags))}")
    for g in fl.git:
        print(f"{g.alias:<{w}}  git      {g.hostname:<28}  {dim('git@' + g.alias + ':<org>/<repo>.git')}")
    return 0


def cmd_config(fl: Fleet, a: argparse.Namespace) -> int:
    flavor = a.os or ("windows" if IS_WINDOWS else "unix")
    if a.for_host:
        h = fl.host(a.for_host)
        flavor = a.os or ("windows" if h.windows else "unix")
    sys.stdout.write(ssh_config(fl, flavor, a.for_host))
    return 0


def setup_plan(fl: Fleet, hosts: List[Host]) -> None:
    print("将要执行（dry-run，不做任何改动）：")
    print(f"  1. 生成密钥（已有的保留）：~/.ssh/{fl.key}" + "".join(f"、~/.ssh/{g.key}" for g in fl.git))
    print("  2. 写入 ~/.ssh/fleet.conf，并在 ~/.ssh/config 第一行引用它（原文件先备份）")
    print("  3. 启动 ssh-agent 并加载密钥" + ("（Windows：启用 ssh-agent 服务）" if IS_WINDOWS else ""))
    print("  4. 设置 Git 多账号身份" + ("，并让 Git 使用 Windows OpenSSH" if IS_WINDOWS else ""))
    print("  5. 按顺序处理机器：")
    for h in hosts:
        extra = "安装公钥 + 下发远端配置" if h.forward else "安装公钥"
        print(f"       {h.alias:<12} {fl.route(h):<32} {extra}")
    print("  6. 自检\n")
    print("本机将写入的 fleet.conf：\n")
    print(ssh_config(fl, "windows" if IS_WINDOWS else "unix"))


def cmd_setup(fl: Fleet, a: argparse.Namespace) -> int:
    hosts = [h for h in fl.ordered() if h in fl.select(a.only) and h.alias != fl.self_alias]
    if a.dry_run:
        setup_plan(fl, hosts)
        return 0
    failures = 0
    d = ssh_dir()
    d.mkdir(mode=0o700, exist_ok=True)

    say("1/6 生成密钥（已有的不会覆盖）")
    key_path = d / fl.key
    if not ensure_key(key_path, fl.comment):
        return 1
    git_keys = [d / g.key for g in fl.git]
    for g, p in zip(fl.git, git_keys):
        failures += not ensure_key(p, g.alias)
    pub = (d / f"{fl.key}.pub").read_text(encoding="utf-8").strip()

    say("2/6 写入本机 SSH 配置")
    install_local_config(fl)

    say("3/6 把密钥加载进 ssh-agent（设了口令的话每把输一次）")
    if ensure_agent():
        add_keys([key_path] + git_keys)
    else:
        failures += 1

    say("4/6 设置 Git 身份")
    setup_git(fl)

    say("5/6 给每台机器安装公钥，并下发远端配置")
    for h in hosts:
        print(f"\n  [{h.alias}] {h.os}  {fl.route(h)}")
        if not install_key(h, pub):
            failures += 1
            continue
        if h.forward and not push_remote(fl, h):
            failures += 1

    say("6/6 自检")
    failures += check_hosts(hosts)
    if fl.git:
        print("\n  把下面的公钥加到对应的 Git 平台（账号的 SSH Keys，或仓库的 Deploy Keys），然后运行 fleet check：")
        for g in fl.git:
            p = d / f"{g.key}.pub"
            if p.exists():
                print(f"\n  --- {g.alias}（{g.hostname}）")
                print("  " + p.read_text(encoding="utf-8").strip())
    say("全部完成" if not failures else f"完成，但有 {failures} 项失败，请看上面标 FAIL 的行")
    return 0 if not failures else 1


def check_hosts(hosts: List[Host]) -> int:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda h: login_ok(h.alias), hosts))
    bad = 0
    for h, good in zip(hosts, results):
        if good:
            ok(f"{h.alias} 免密登录正常")
        else:
            bad += 1
            fail(f"{h.alias} 还不能免密登录（运行 ssh -v {h.alias} 查看原因）")
    return bad


def cmd_check(fl: Fleet, a: argparse.Namespace) -> int:
    hosts = [h for h in fl.select(a.only) if h.alias != fl.self_alias]
    say("机器")
    bad = check_hosts(hosts)
    if fl.git and not a.only:
        say("Git 账号")
        with ThreadPoolExecutor(max_workers=4) as pool:
            res = list(pool.map(git_account_ok, fl.git))
        for g, (good, text) in zip(fl.git, res):
            (ok if good else fail)(f"{g.alias}：{text}")
            bad += not good
    return 0 if not bad else 1


def choose_host(fl: Fleet) -> Optional[str]:
    rows = [(h.alias, fl.route(h)) for h in fl.hosts]
    if not IS_WINDOWS and shutil.which("fzf") and sys.stdin.isatty():
        r = subprocess.run(["fzf", "--height", "40%", "--prompt", "ssh> "],
                           input="\n".join(f"{a}\t{rt}" for a, rt in rows), stdout=subprocess.PIPE, text=True)
        return r.stdout.split("\t")[0].strip() or None
    for i, (alias, rt) in enumerate(rows, 1):
        print(f"  [{i}] {alias:<14} {dim(rt)}")
    try:
        ans = input("选择编号或别名：").strip()
    except EOFError:
        return None
    if ans.isdigit() and 1 <= int(ans) <= len(rows):
        return rows[int(ans) - 1][0]
    matches = [a for a, _ in rows if a.startswith(ans)] if ans else []
    return matches[0] if len(matches) == 1 else (ans if ans in dict(rows) else None)


def cmd_ssh(fl: Fleet, a: argparse.Namespace) -> int:
    alias = a.alias or choose_host(fl)
    if not alias:
        return 1
    if alias not in {h.alias for h in fl.hosts} | {g.alias for g in fl.git}:
        raise FleetError(f"配置里没有叫 {alias} 的机器")
    argv = ["ssh", alias] + list(a.extra or [])
    sys.stdout.flush()
    if IS_WINDOWS:
        return subprocess.call(argv)
    os.execvp("ssh", argv)
    return 0  # 不会执行到这里


def _strip_dashes(items: Sequence[str]) -> List[str]:
    items = list(items or [])
    return items[1:] if items and items[0] == "--" else items


def cmd_run(fl: Fleet, a: argparse.Namespace) -> int:
    command = " ".join(_strip_dashes(a.command))
    if not command:
        raise FleetError("用法：fleet run [-o 主机] -- 命令，例如 fleet run -- uptime")
    tasks = []
    for h in fl.select(a.only):
        if h.alias == fl.self_alias:
            if IS_WINDOWS:
                tasks.append(Task(h.alias, command, ps_encoded_args(ps_wrap(command))))
            else:
                tasks.append(Task(h.alias, command, [command], shell=True))
        else:
            tasks.append(Task(h.alias, command, ssh_args(h.alias, shell_remote_command(h, command))))
    if a.dry_run:
        for t in tasks:
            print(dim("[dry-run]") + f" {t.host}: {command}")
        return 0
    return 0 if not run_parallel(tasks, a.jobs, a.timeout) else 1


def cmd_git(fl: Fleet, a: argparse.Namespace) -> int:
    words = _strip_dashes(a.args)
    if not words:
        raise FleetError("用法：fleet git pull|push|status|fetch，或 fleet git -- <任意 git 参数>")
    git_args = GIT_SHORTCUTS.get(words[0], words) if len(words) == 1 else words
    tasks = []
    for h in fl.select(a.only):
        for repo in h.repos:
            label = f"{repo}  git {' '.join(git_args)}"
            if h.alias == fl.self_alias:
                tasks.append(Task(h.alias, label, ["git"] + git_args, cwd=os.path.expanduser(repo)))
            else:
                tasks.append(Task(h.alias, label, ssh_args(h.alias, git_remote_command(h, repo, git_args))))
    if not tasks:
        print("选中的机器都没有配置 repos。")
        return 0
    if a.dry_run:
        for t in tasks:
            print(dim("[dry-run]") + f" {t.host}  {t.label}")
        return 0
    return 0 if not run_parallel(tasks, a.jobs, a.timeout) else 1


def hub_ini(text: str, hub: str) -> str:
    """给主服务器用的配置：在 [settings] 里写上 self = 主服务器别名。"""
    if re.search(r"(?m)^\s*self\s*=", text):
        return re.sub(r"(?m)^(\s*self\s*=).*$", rf"\g<1> {hub}", text)
    if re.search(r"(?m)^\[settings\]\s*$", text):
        return re.sub(r"(?m)^\[settings\]\s*$", f"[settings]\nself = {hub}", text, count=1)
    return f"[settings]\nself = {hub}\n\n" + text


def cmd_hub(fl: Fleet, a: argparse.Namespace) -> int:
    if not fl.hub:
        raise FleetError("先在 fleet.ini 的 [settings] 里写上 hub = 主服务器别名")
    h = fl.host(fl.hub)
    if a.action == "show":
        print(f"主服务器：{h.alias}（{h.hostname}）  via_hub = {'yes' if fl.via_hub else 'no'}")
        print(f"经过主服务器跳转的机器：{', '.join(x.alias for x in fl.hosts if fl.jump_of(x) == h.alias) or '无'}")
        return 0
    if not h.forward:
        note(f"{h.alias} 没有设置 forward = yes：部署后在它上面无法用你的密钥访问其他机器")
    if not login_ok(h.alias):
        raise FleetError(f"无法免密登录 {h.alias}，请先运行 fleet setup")
    probe = ps_encoded("if (Get-Command python -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }") \
        if h.windows else "command -v python3 >/dev/null"
    if run(ssh_args(h.alias, probe), capture=True, timeout=30)[0] != 0:
        raise FleetError(f"{h.alias} 上没有 Python 3，请先安装")
    assert fl.path is not None
    ok_all = put_file(h, ".fleet", "fleet.py", Path(__file__).read_bytes(), mode="700")
    ok_all &= put_file(h, ".fleet", "fleet.ini", hub_ini(fl.path.read_text(encoding="utf-8-sig"), h.alias).encode("utf-8"))
    if h.windows:
        wrapper = '@python "%USERPROFILE%\\.fleet\\fleet.py" -c "%USERPROFILE%\\.fleet\\fleet.ini" %*\r\n'
        ok_all &= put_file(h, ".fleet", "fleet.cmd", wrapper.encode("utf-8"))
    else:
        wrapper = '#!/bin/sh\nexec python3 "$HOME/.fleet/fleet.py" -c "$HOME/.fleet/fleet.ini" "$@"\n'
        ok_all &= put_file(h, ".fleet", "fleet", wrapper.encode("utf-8"), mode="700")
    if not ok_all:
        fail("部署失败")
        return 1
    ok(f"已部署到 {h.alias}:~/.fleet/")
    print(f"\n登录主服务器后就能直接用：\n  ssh {h.alias}")
    if h.windows:
        print("  %USERPROFILE%\\.fleet\\fleet.cmd git pull")
    else:
        print("  ~/.fleet/fleet git pull")
        print("\n想直接输入 fleet，在主服务器上执行一次：")
        print("  echo 'export PATH=\"$HOME/.fleet:$PATH\"' >> ~/.bashrc")
    return 0


# --------------------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet", description="多台服务器的 SSH 登录、交叉访问和批量 Git 操作。",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="配置文件查找顺序：-c 参数 → 环境变量 FLEET_CONFIG → ./fleet.ini → ~/.fleet/fleet.ini\n\n"
                                       "常用示例：\n"
                                       "  fleet run -o us01 -- df -h      只在一台机器执行\n"
                                       "  fleet run -o us01,jp -- uptime  在多台机器执行\n"
                                       "  fleet ssh us01                  登录单台机器\n"
                                       "  fleet git pull -o us01          只对单台机器跑 git")
    p.add_argument("-c", "--config", help="配置文件路径")
    p.add_argument("-V", "--version", action="version", version=f"fleet {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="命令")

    s = sub.add_parser("init", help="扫描家目录生成 fleet.ini（--example 用示例）")
    s.add_argument("path", nargs="?", default="fleet.ini")
    s.add_argument("--force", action="store_true", help="覆盖已有文件")
    s.add_argument("--example", action="store_true", help="生成示例配置，不扫描家目录")

    s = sub.add_parser("bootstrap", help="一键：生成配置 + setup + 自检")
    s.add_argument("-n", "--dry-run", action="store_true", help="只显示将要执行的操作")
    s.add_argument("-o", "--only", help="只处理这些机器，逗号分隔")
    s.add_argument("--example", action="store_true", help="没有配置时生成示例配置")

    sub.add_parser("ls", help="列出机器、访问路线和 Git 账号")

    s = sub.add_parser("config", help="打印生成的 SSH 配置")
    s.add_argument("--os", choices=["unix", "windows"], help="生成哪种系统用的配置")
    s.add_argument("--for", dest="for_host", metavar="别名", help="生成给某台远端机器用的版本")

    s = sub.add_parser("setup", help="一次性初始化（可重复运行）")
    s.add_argument("-n", "--dry-run", action="store_true", help="只显示将要做的事")
    s.add_argument("-o", "--only", help="只处理这些机器，逗号分隔")

    s = sub.add_parser("check", help="检查免密登录和 Git 账号")
    s.add_argument("-o", "--only", help="只检查这些机器，逗号分隔")

    s = sub.add_parser("ssh", help="登录某台机器，不写别名时弹出菜单")
    s.add_argument("alias", nargs="?")
    s.add_argument("extra", nargs=argparse.REMAINDER, help="传给 ssh 的其他参数或远端命令")

    for name, helptext in (("git", "在所有机器的仓库上执行 git"), ("run", "在多台机器上执行命令")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("-o", "--only", help="只处理这些机器，逗号分隔")
        s.add_argument("-j", "--jobs", type=int, default=8, help="并发数，默认 8")
        s.add_argument("-t", "--timeout", type=int, default=300, help="单个任务超时秒数，默认 300")
        s.add_argument("-n", "--dry-run", action="store_true", help="只显示将要执行的操作")
        if name == "git":
            s.add_argument("args", nargs="*", help="pull / push / status / fetch，或 -- 加任意 git 参数")
        else:
            s.add_argument("command", nargs="*", help="要执行的命令，带参数时写在 -- 后面")
        if name == "run":
            s.formatter_class = argparse.RawDescriptionHelpFormatter
            s.epilog = ("示例：\n"
                        "  fleet run -o us01 -- df -h\n"
                        "  fleet run -o us01,jp -- uptime\n"
                        "  fleet run -o win-srv -- \"Get-Service | Where-Object Status -eq 'Running'\"\n\n"
                        "说明：-o 后面只写一个别名就是单台；-- 后面才是命令。\n"
                        "本机和远端 Windows 都按 PowerShell 解析，Linux 按 Shell 解析。")

    s = sub.add_parser("hub", help="主服务器：show 查看，deploy 把 fleet 部署上去")
    s.add_argument("action", choices=["show", "deploy"])
    return p


def _force_utf8_stdio() -> None:
    """Windows 管道/重定向时 Python 默认用 ANSI 代码页，中文会变成 ?。"""
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            encoding = (stream.encoding or "").lower().replace("-", "")
            if encoding != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8_stdio()
    Color.init()
    parser = build_parser()
    a = parser.parse_args(argv)
    if not a.cmd:
        parser.print_help()
        return 0
    try:
        if a.cmd == "init":
            return cmd_init(a)
        if a.cmd == "bootstrap":
            return cmd_bootstrap(a)
        if not shutil.which("ssh"):
            raise FleetError("没有找到 ssh 命令。Windows：设置 → 系统 → 可选功能 → 添加「OpenSSH 客户端」")
        fl = load_config(a.config)
        handlers: Dict[str, Callable[[Fleet, argparse.Namespace], int]] = {
            "ls": cmd_ls, "config": cmd_config, "setup": cmd_setup, "check": cmd_check,
            "ssh": cmd_ssh, "run": cmd_run, "git": cmd_git, "hub": cmd_hub,
        }
        return handlers[a.cmd](fl, a)
    except FleetError as e:
        print(red("错误：") + str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
