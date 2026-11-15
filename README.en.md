# fleet: SSH and Git manager for multiple servers

English | [中文](README.md)

[![Release](https://img.shields.io/github/v/release/SingBoJia/ssh-fleet-kit)](https://github.com/SingBoJia/ssh-fleet-kit/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Log in to servers across Linux, macOS, and Windows, hop between servers, and run
`git pull` / `git push` in batches with one config file and one command.

- One `fleet.ini` describes every host and Git account
- `fleet setup` does the initial setup in one pass: generates keys, writes the SSH config, loads ssh-agent, configures multiple Git identities, installs public keys on every host (one password prompt for password-only hosts), and deploys the remote config
- Afterwards `ssh web1` logs in directly; from a server you can keep using `ssh` to reach other hosts and run `git pull` / `push`, while **private keys never leave your computer**
- `fleet git pull` runs across every host and repository in parallel, then summarizes successes and failures
- Supports a hub server (jump host / operations center) mode
- Depends only on Python 3.8+ standard library and the system OpenSSH client; the CLI is a single `fleet.py`

## Installation

Requires Python 3.8+, an OpenSSH client, and Git.

**Linux / macOS**

```bash
git clone git@github.com:SingBoJia/ssh-fleet-kit.git ~/ssh-fleet-kit
mkdir -p ~/.local/bin && ln -sf ~/ssh-fleet-kit/fleet.py ~/.local/bin/fleet
fleet --version
```

**Windows** (PowerShell)

```powershell
git clone git@github.com:SingBoJia/ssh-fleet-kit.git $HOME\ssh-fleet-kit
# Add this directory to PATH to run fleet directly
# (fleet.cmd prefers py -3, then falls back to python)
[Environment]::SetEnvironmentVariable('Path', $env:Path + ";$HOME\ssh-fleet-kit", 'User')
```

Windows 10 1809 and later include an OpenSSH client. If it is missing, add
"OpenSSH Client" under Settings → System → Optional Features.

## Quick start

```bash
mkdir -p ~/.fleet && cd ~/.fleet
fleet init                # scan ~/.ssh/config and ~/.gitconfig (fallback: sample)
fleet init --example      # force the sample config
# edit fleet.ini for your hosts
fleet setup --dry-run     # preview what will happen
fleet setup               # run it (use an Administrator terminal on Windows the first time)
fleet check               # verify every host and Git account
```

At the end of `setup`, `fleet` prints the public key for each Git account. Add
them to GitHub / GitLab SSH Keys (or repository Deploy Keys), then run
`fleet check` again.

`setup` is idempotent: completed steps are skipped. After adding a host, edit
`fleet.ini` and run it again.

## Configuration

The config file is looked up in this order: `-c path` → `FLEET_CONFIG` →
`./fleet.ini` → `~/.fleet/fleet.ini`. See [fleet.example.ini](fleet.example.ini)
for a complete example.

```ini
[settings]
key = id_ed25519_fleet      # key used to log in to servers, stored under ~/.ssh/
hub = web1                  # optional hub server
via_hub = no                # yes: route every other host through the hub by default

[host web1]
hostname = 10.0.0.11
user = root
os = linux                  # linux (including macOS) or windows
forward = yes               # allow further ssh / git from this host after login
repos = /srv/www/site; /opt/api

[host app1]
hostname = 192.168.1.20
user = deploy
jump = web1                 # only reachable through web1; use none to force a direct connection

[host win-srv]
hostname = 10.0.0.30
user = Administrator
os = windows
repos = D:\code\portal

[git github-work]           # clone with the alias: git clone git@github-work:org/repo.git
hostname = github.com
key = id_ed25519_work
name = Your Name
email = you@company.com
dir = ~/work/               # repositories under this directory use this identity
```

## Commands

| Command | Description |
| --- | --- |
| `fleet init [path] [--example]` | Scan the home directory for a config; `--example` writes the sample |
| `fleet ls` | List hosts, routes, and Git accounts |
| `fleet setup [-n] [-o host]` | One-pass initialization; `-n` only prints the plan |
| `fleet check [-o host]` | Check passwordless login and Git accounts |
| `fleet ssh [alias]` | Log in; without an alias, show a menu (uses fzf if installed) |
| `fleet git pull` | Pull every repository in parallel; also `push`, `status`, `fetch` |
| `fleet git pull -o web1,app1` | Only process the selected hosts |
| `fleet git -- log -1 --oneline` | Put arbitrary Git arguments after `--` |
| `fleet run -- df -h` | Run a command on every host (PowerShell on Windows hosts) |
| `fleet run -o us01 -- df -h` | Run a command on one host only |
| `fleet config [--os windows] [--for alias]` | Print the generated SSH config |
| `fleet hub show` / `fleet hub deploy` | Show hub settings / deploy fleet to the hub |

`git` and `run` accept `-j` (parallel jobs, default 8), `-t` (per-task timeout in
seconds, default 300), and `-n` (preview). `git pull` uses `--ff-only` by
default, so it will not create unexpected merge commits.

### Run a command on one host

`-o` selects one host; everything after `--` is the command:

```powershell
# Linux host: parsed by the shell
fleet run -o us01 -- df -h
fleet run -o jp -- "ps aux | grep nginx"

# Windows host: parsed by PowerShell
fleet run -o win-srv -- "Get-Service | Where-Object Status -eq 'Running'"

# Log in to one host
fleet ssh us01

# Check one host
fleet check -o us01

# Run git on one host (the host must have repos configured)
fleet git pull -o us01
```

- Separate multiple hosts with commas: `fleet run -o us01,jp -- uptime`.
- `fleet init` can discover SSH hosts, but it cannot infer remote repository
  paths; add `repos` manually under the matching `[host alias]`.
- To use cmd syntax on a Windows host, run
  `fleet run -o win-srv -- cmd /c "dir"`.

## How cross-host access works

```
Your computer (private keys live here, held by ssh-agent)
   │  ssh web1 (ForwardAgent)
   ▼
web1  ── ssh app1 ──▶ app1        ← uses the forwarded agent
   └── git pull ──▶ GitHub         ← same; picks the account from the repository alias
```

- For every host with `forward = yes`, `setup` deploys a `~/.ssh/fleet.conf`
  containing aliases for the other hosts and Git accounts. `IdentityFile`
  points to the **public** `.pub` key so OpenSSH can select the matching key
  from the forwarded agent.
- Private keys are never copied to any server.

## Hub mode

Set `hub = hub-alias` in `[settings]`. Both modes can be used together:

**1. Jump host** (`via_hub = yes`): other hosts are reached through the hub by
default. `ssh db1` on your computer goes through the hub automatically, so
remote firewalls only need to allow the hub. In this mode the hub only forwards
encrypted traffic and never sees your keys.

**2. Operations center** (`fleet hub deploy`): deploy `fleet.py` and the config
to `~/.fleet/` on the hub. After logging in with `ssh hub`, run
`~/.fleet/fleet git pull` there to operate every host. Repositories on the hub
itself run locally.

Notes:

- Operations-center mode relies on agent forwarding. While you are logged in,
  anyone with root on the hub can use your agent. Only set `forward = yes` on
  servers you trust; use jump-host mode if you only need routing.
- When you are not logged in (for example, from a scheduled job), the hub cannot
  access your agent. Configure a separate Deploy Key for it.

## Keep the agent resident (enter the passphrase once per boot)

- **Windows**: `fleet setup` sets the ssh-agent service to start automatically
  and makes Git use the system OpenSSH.
- `fleet.cmd` sets `PYTHONUTF8=1` to keep Chinese output from turning into `?`
  when piped or redirected. `fleet.py` resolves OpenSSH at
  `%WINDIR%\System32\OpenSSH\ssh.exe` and falls back to `ssh` on PATH. On
  Windows, local `fleet run -- <command>` uses PowerShell, same as remote
  Windows; use PowerShell syntax. For cmd syntax, run `fleet run -- cmd /c "..."`.
- **macOS**: `fleet setup` stores the passphrase in the keychain with
  `--apple-use-keychain`. Add `Host *` and `  UseKeychain yes` to the end of
  `~/.ssh/config` to load it automatically after reboot.
- **Linux**: install keychain (`apt install keychain` or
  `dnf install keychain`) and add this to `~/.bashrc`:

  ```bash
  eval "$(keychain --eval --quiet id_ed25519_fleet id_ed25519_work)"
  ```

## Windows notes

- A Windows server needs OpenSSH Server enabled first. In an Administrator
  PowerShell on the server:

  ```powershell
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
  Set-Service sshd -StartupType Automatic; Start-Service sshd
  ```
- The public key for an administrator account is written to
  `C:\ProgramData\ssh\administrators_authorized_keys` with the required ACLs.
- Remote commands on Windows are sent as PowerShell `-EncodedCommand`, so they
  work regardless of whether the default shell is cmd or PowerShell, including
  paths with spaces.
- Agent forwarding through a Windows server is not reliable. If Git fails on
  such a server, configure a separate Deploy Key for it.
- For cross-platform repositories, consider adding `.gitattributes`:
  `* text=auto eol=lf`, with `eol=crlf` for `*.ps1`, `*.bat`, and `*.cmd`.

## FAQ

| Symptom | Fix |
| --- | --- |
| `Permission denied (publickey)` | Run `ssh -v alias` to see which key was used; rerun `fleet setup -o alias` |
| `UNPROTECTED PRIVATE KEY FILE` (Windows) | `icacls key-path /inheritance:r /grant:r "%USERNAME%:F"`; `setup` does this automatically |
| Git asks for a passphrase on Windows every time | `git config --global core.sshCommand C:/Windows/System32/OpenSSH/ssh.exe`; `setup` sets this automatically |
| Git on a server uses the wrong account or is rejected | Run `ssh-add -l` on the server; if the key is missing, check `forward = yes` |
| `fleet git` shows FAIL but direct ssh works | The key is not in the agent after a reboot, and batch mode cannot prompt. Run `ssh-add` first |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | Confirm the server was really reinstalled, then `ssh-keygen -R address` and verify the fingerprint again |
| ssh hangs (Linux / macOS) | `ssh -O exit alias` to close the old multiplexed connection |

## Web generator

[web/index.html](web/index.html) is an optional GUI. Open it in a browser, fill
in the host details, and generate a standalone Bash / PowerShell script—useful
on machines where Python is not convenient to install. The page never connects
to any server; data stays in the browser.

## Development

```bash
python3 -m unittest discover -s tests -v
```

GitHub Actions runs the tests on Linux, macOS, and Windows.

## License

This project is open source under the [MIT License](LICENSE),
Copyright (c) 2026 SingBoJia.
