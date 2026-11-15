# fleet：多台服务器的 SSH 与 Git 管理工具

在 Linux、macOS、Windows 之间来回登录服务器、在服务器之间交叉访问、批量 `git pull` / `git push`，用一个配置文件和一个命令搞定。

- 一份 `fleet.ini` 写清所有机器和 Git 账号
- `fleet setup` 一次完成初始化：生成密钥、写 SSH 配置、加载 ssh-agent、设置 Git 多账号、给每台机器装公钥（只能用密码的机器各输一次密码）、下发远端配置
- 之后 `ssh web1` 直接登录；登录到服务器上还能继续 `ssh` 到其他机器、执行 `git pull` / `push`，**私钥始终只在你自己的电脑上**
- `fleet git pull` 所有机器、所有仓库并行执行，最后汇总成功和失败
- 支持主服务器（跳板机 / 操作中心）模式
- 只依赖 Python 3.8+ 标准库和系统自带的 OpenSSH，单个文件 `fleet.py`

## 安装

需要 Python 3.8+、OpenSSH 客户端和 Git。

**Linux / macOS**

```bash
git clone git@github.com:SingBoJia/ssh-fleet-kit.git ~/ssh-fleet-kit
mkdir -p ~/.local/bin && ln -sf ~/ssh-fleet-kit/fleet.py ~/.local/bin/fleet
fleet --version
```

**Windows**（PowerShell）

```powershell
git clone git@github.com:SingBoJia/ssh-fleet-kit.git $HOME\ssh-fleet-kit
# 把这个目录加入 PATH 后，就能直接输入 fleet（fleet.cmd 优先用 py -3，找不到时用 python）
[Environment]::SetEnvironmentVariable('Path', $env:Path + ";$HOME\ssh-fleet-kit", 'User')
```

Windows 10 1809 及以上自带 OpenSSH 客户端；没有的话在「设置 → 系统 → 可选功能」里添加「OpenSSH 客户端」。

## 快速开始

```bash
mkdir -p ~/.fleet && cd ~/.fleet
fleet init                # 生成示例配置 fleet.ini
# 用编辑器把 fleet.ini 改成你的机器
fleet setup --dry-run     # 先看看会做什么
fleet setup               # 正式执行（Windows 第一次请用「以管理员身份运行」的终端）
fleet check               # 检查每台机器和每个 Git 账号
```

`setup` 最后会打印每个 Git 账号的公钥，把它们加到 GitHub / GitLab 的 SSH Keys（或仓库的 Deploy Keys），再运行 `fleet check` 确认。

`setup` 可以重复运行：已经完成的步骤会跳过。新增机器后改好 `fleet.ini`，再运行一次即可。

## 配置文件

配置文件按这个顺序查找：`-c 路径` → 环境变量 `FLEET_CONFIG` → 当前目录的 `fleet.ini` → `~/.fleet/fleet.ini`。完整示例见 [fleet.example.ini](fleet.example.ini)。

```ini
[settings]
key = id_ed25519_fleet      # 登录服务器用的密钥，放在 ~/.ssh/ 下
hub = web1                  # 主服务器（可选）
via_hub = no                # yes：其他机器默认都经过主服务器跳转

[host web1]
hostname = 10.0.0.11
user = root
os = linux                  # linux（包括 macOS）或 windows
forward = yes               # 登录后还能继续 ssh 到其他机器、执行 git
repos = /srv/www/site; /opt/api

[host app1]
hostname = 192.168.1.20
user = deploy
jump = web1                 # 只能经过 web1 访问；写 none 表示强制直连

[host win-srv]
hostname = 10.0.0.30
user = Administrator
os = windows
repos = D:\code\portal

[git github-work]           # 克隆时用别名：git clone git@github-work:org/repo.git
hostname = github.com
key = id_ed25519_work
name = Your Name
email = you@company.com
dir = ~/work/               # 这个目录下的仓库自动使用这个身份
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `fleet init [路径]` | 生成示例配置 |
| `fleet ls` | 列出机器、访问路线和 Git 账号 |
| `fleet setup [-n] [-o 机器]` | 一次性初始化，`-n` 只显示计划 |
| `fleet check [-o 机器]` | 检查免密登录和 Git 账号 |
| `fleet ssh [别名]` | 登录；不写别名时弹出菜单（装了 fzf 会用 fzf） |
| `fleet git pull` | 所有仓库并行 pull，还有 `push`、`status`、`fetch` |
| `fleet git pull -o web1,app1` | 只处理指定机器 |
| `fleet git -- log -1 --oneline` | `--` 后面写任意 git 参数 |
| `fleet run -- df -h` | 在所有机器上执行命令（Windows 机器上按 PowerShell 执行） |
| `fleet config [--os windows] [--for 别名]` | 打印生成的 SSH 配置 |
| `fleet hub show` / `fleet hub deploy` | 查看主服务器设置 / 把 fleet 部署到主服务器 |

`git` 和 `run` 都支持 `-j` 并发数（默认 8）、`-t` 单个任务超时秒数（默认 300）、`-n` 只预览。`git pull` 默认使用 `--ff-only`，不会产生意外的合并提交。

## 交叉访问是怎么实现的

```
你的电脑（私钥在这里，ssh-agent 持有）
   │  ssh web1（ForwardAgent）
   ▼
web1  ── ssh app1 ──▶ app1        ← 用的是转发过来的 agent
   └── git pull ──▶ GitHub         ← 同上，按仓库地址里的别名选账号
```

- `setup` 会给 `forward = yes` 的机器下发一份它自己用的 `~/.ssh/fleet.conf`：里面有所有其他机器和 Git 账号的别名，`IdentityFile` 指向**公钥** `.pub`，用来从转发的 agent 里挑选对应的密钥。
- 私钥不会被复制到任何服务器。

## 主服务器模式

在 `[settings]` 里写 `hub = 主服务器别名`，有两种用法，可以同时使用：

**1. 跳板**（`via_hub = yes`）：其他机器默认都经过主服务器连接。你在自己电脑上 `ssh db1`，会自动经过主服务器转过去。其他服务器的防火墙可以只放行主服务器。这种方式下，主服务器只转发加密流量，碰不到你的密钥。

**2. 操作中心**（`fleet hub deploy`）：把 `fleet.py` 和配置部署到主服务器的 `~/.fleet/`。之后 `ssh 主服务器` 登录，在上面运行 `~/.fleet/fleet git pull`，就能统一操作所有机器。主服务器自己的仓库会在本地直接执行。

注意：

- 操作中心模式依赖 Agent 转发。你登录期间，主服务器上有 root 权限的人可以借用你的 agent。只对你信任的服务器设置 `forward = yes`；只需要中转时，用跳板模式更安全。
- 你没有登录时（例如定时任务），主服务器拿不到你的 agent，需要给它单独配一把 Deploy Key。

## 让 agent 常驻（开机只输一次口令）

- **Windows**：`fleet setup` 已经把 ssh-agent 服务设为自动启动，并让 Git 改用系统自带的 OpenSSH。
- `fleet.cmd` 会设置 `PYTHONUTF8=1`，避免中文输出在管道/重定向时变成 `?`；`fleet.py` 会按 `%WINDIR%\System32\OpenSSH\ssh.exe` 定位 OpenSSH，找不到时回退到 PATH 里的 `ssh`。本机 Windows 上的 `fleet run -- <命令>` 通过 PowerShell 执行（与远端 Windows 一致），请使用 PowerShell 语法；需要 cmd 语法时写 `fleet run -- cmd /c "..."`。
- **macOS**：`fleet setup` 用 `--apple-use-keychain` 把口令存进钥匙串。在 `~/.ssh/config` 末尾加上 `Host *` 和 `  UseKeychain yes` 两行，重启后会自动加载。
- **Linux**：安装 keychain（`apt install keychain` 或 `dnf install keychain`），在 `~/.bashrc` 里加：

  ```bash
  eval "$(keychain --eval --quiet id_ed25519_fleet id_ed25519_work)"
  ```

## Windows 说明

- Windows 服务器需要先开启 OpenSSH Server。在服务器上用管理员 PowerShell 执行：

  ```powershell
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
  Set-Service sshd -StartupType Automatic; Start-Service sshd
  ```
- 管理员账号的公钥会自动写入 `C:\ProgramData\ssh\administrators_authorized_keys`，并设置它要求的权限。
- 在 Windows 机器上执行的远端命令都经过 PowerShell `-EncodedCommand` 编码传输，所以不受默认 shell 是 cmd 还是 PowerShell 的影响，路径里有空格也没问题。
- Windows 服务器作为中转时，OpenSSH 的 Agent 转发支持不稳定。如果在它上面执行 git 失败，请给它单独配置 Deploy Key。
- 跨平台仓库建议加 `.gitattributes`：`* text=auto eol=lf`，并为 `*.ps1`、`*.bat`、`*.cmd` 单独设置 `eol=crlf`。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `Permission denied (publickey)` | `ssh -v 别名` 看用了哪把密钥；重新运行 `fleet setup -o 别名` |
| `UNPROTECTED PRIVATE KEY FILE`（Windows） | `icacls 私钥路径 /inheritance:r /grant:r "%USERNAME%:F"`，`setup` 会自动处理 |
| Windows 上 git 每次要口令 | `git config --global core.sshCommand C:/Windows/System32/OpenSSH/ssh.exe`，`setup` 会自动设置 |
| 服务器上 git 用错账号或被拒 | 在服务器上运行 `ssh-add -l`，应能看到你的密钥；看不到就检查 `forward = yes` |
| `fleet git` 显示 FAIL，单独 ssh 正常 | 重启后 agent 里没有密钥，而批量模式不会弹出口令输入。先运行 `ssh-add` |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | 先确认服务器确实重装了，再 `ssh-keygen -R 地址` 并重新核对指纹 |
| ssh 卡住（Linux / macOS） | `ssh -O exit 别名` 关掉复用的旧连接 |

## 网页版生成器

[web/index.html](web/index.html) 是一个可选的图形界面：用浏览器打开，填写机器信息，就能生成独立的 Bash / PowerShell 脚本，适合不方便安装 Python 的机器。网页不会连接任何服务器，填写的内容只保存在浏览器本地。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

GitHub Actions 会在 Linux、macOS、Windows 上运行测试。

