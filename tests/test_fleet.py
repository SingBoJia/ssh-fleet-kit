import base64
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fleet  # noqa: E402

BASE = """
[settings]
key = id_ed25519_fleet
hub = web1

[host web1]
hostname = 10.0.0.11
user = root
forward = yes
repos = /srv/a; /srv/b

[host db1]
hostname = 10.0.0.12
port = 2222
user = admin

[host app1]
hostname = 192.168.1.20
user = deploy
jump = web1
forward = yes

[host win]
hostname = 10.0.0.30
user = Administrator
os = windows
repos = D:\\code\\portal

[git github-work]
key = id_ed25519_work
name = A
email = a@example.com
dir = ~/work
"""


def cfg(text=BASE):
    return fleet.parse_config(text)


class ParseTests(unittest.TestCase):
    def test_basic(self):
        fl = cfg()
        self.assertEqual([h.alias for h in fl.hosts], ["web1", "db1", "app1", "win"])
        self.assertEqual(fl.host("db1").port, 2222)
        self.assertEqual(fl.host("web1").repos, ["/srv/a", "/srv/b"])
        self.assertTrue(fl.host("win").windows)
        self.assertEqual(fl.host("win").repos, ["D:\\code\\portal"])
        self.assertEqual(fl.git[0].hostname, "github.com")

    def test_errors(self):
        bad = {
            "unknown jump": BASE.replace("jump = web1", "jump = nope"),
            "self jump": BASE.replace("jump = web1", "jump = app1"),
            "bad port": BASE.replace("port = 2222", "port = abc"),
            "bad os": BASE.replace("os = windows", "os = solaris"),
            "dup alias": BASE + "\n[git web1]\nkey = k\n",
            "unknown section": BASE + "\n[server x]\nhostname = 1\n",
            "missing git key": BASE.replace("key = id_ed25519_work", ""),
            "bad hub": BASE.replace("hub = web1", "hub = ghost"),
        }
        for name, text in bad.items():
            with self.subTest(name):
                with self.assertRaises(fleet.FleetError):
                    cfg(text)

    def test_cycle(self):
        text = BASE.replace("[host db1]\n", "[host db1]\njump = app1\n").replace("jump = web1", "jump = db1")
        with self.assertRaises(fleet.FleetError):
            cfg(text)

    def test_inline_comments(self):
        fl = cfg(BASE.replace("user = root", "user = root   # login user").replace("repos = /srv/a; /srv/b", "repos = /srv/a; /srv/b  # two repos"))
        self.assertEqual(fl.host("web1").user, "root")
        self.assertEqual(fl.host("web1").repos, ["/srv/a", "/srv/b"])

    def test_readme_sample_parses(self):
        import re
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        ini = re.search(r"```ini\n(.*?)```", readme, re.S).group(1)
        fl = fleet.parse_config(ini)
        self.assertEqual(fl.host("app1").jump, "web1")
        self.assertEqual(fl.host("win-srv").repos, ["D:\\code\\portal"])

    def test_macos_maps_to_linux(self):
        self.assertEqual(cfg(BASE.replace("os = windows", "os = macos")).host("win").os, "linux")

    def test_bom_and_crlf(self):
        fl = fleet.parse_config(("\ufeff" + BASE).replace("\n", "\r\n").lstrip("\ufeff"))
        self.assertEqual(len(fl.hosts), 4)


class HubTests(unittest.TestCase):
    def test_via_hub(self):
        fl = cfg(BASE.replace("hub = web1", "hub = web1\nvia_hub = yes"))
        self.assertEqual(fl.jump_of(fl.host("db1")), "web1")
        self.assertEqual(fl.jump_of(fl.host("web1")), "")
        self.assertEqual(fl.route(fl.host("win")), "本机 → web1 → win")

    def test_jump_none_overrides_hub(self):
        fl = cfg(BASE.replace("hub = web1", "hub = web1\nvia_hub = yes").replace("port = 2222", "port = 2222\njump = none"))
        self.assertEqual(fl.jump_of(fl.host("db1")), "")

    def test_order_puts_jump_first(self):
        fl = cfg(BASE.replace("[host web1]", "[host zz]\nhostname = 1.1.1.1\njump = app1\n\n[host web1]"))
        order = [h.alias for h in fl.ordered()]
        self.assertLess(order.index("web1"), order.index("app1"))
        self.assertLess(order.index("app1"), order.index("zz"))

    def test_hub_ini(self):
        self.assertIn("[settings]\nself = web1", fleet.hub_ini(BASE, "web1"))
        self.assertIn("self = web1", fleet.hub_ini(BASE.replace("[settings]", "[settings]\nself = old"), "web1"))
        self.assertNotIn("self = old", fleet.hub_ini(BASE.replace("[settings]", "[settings]\nself = old"), "web1"))
        fl = fleet.parse_config(fleet.hub_ini(BASE, "web1"))
        self.assertEqual(fl.self_alias, "web1")


class SshConfigTests(unittest.TestCase):
    def test_local_unix(self):
        out = fleet.ssh_config(cfg(), "unix", today="2026-01-01")
        self.assertIn("Host app1\n  HostName 192.168.1.20\n  User deploy\n  IdentityFile ~/.ssh/id_ed25519_fleet\n"
                      "  IdentitiesOnly yes\n  ProxyJump web1\n  ForwardAgent yes\n", out)
        self.assertIn("  Port 2222", out)
        self.assertIn("ControlMaster auto", out)
        self.assertIn("Host web1 db1 app1 win\n", out)
        self.assertIn("Host github-work\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/id_ed25519_work\n", out)
        self.assertNotIn("Host *", out)

    def test_windows_has_no_controlmaster(self):
        out = fleet.ssh_config(cfg(), "windows")
        self.assertNotIn("ControlMaster", out)
        self.assertIn("AddKeysToAgent yes", out)

    def test_remote_version(self):
        out = fleet.ssh_config(cfg(), "unix", from_alias="web1")
        self.assertNotIn("Host web1\n", out)
        self.assertNotIn("ProxyJump web1", out)  # already on web1
        self.assertIn("IdentityFile ~/.ssh/id_ed25519_fleet.pub", out)
        self.assertIn("IdentityFile ~/.ssh/id_ed25519_work.pub", out)
        self.assertNotIn("AddKeysToAgent", out)

    def test_ascii_only(self):
        out = fleet.ssh_config(cfg(), "windows")
        out.encode("ascii")


class CommandTests(unittest.TestCase):
    def decode_ps(self, cmd):
        prefix = "powershell -NoProfile -NonInteractive -EncodedCommand "
        self.assertTrue(cmd.startswith(prefix))
        return base64.b64decode(cmd[len(prefix):]).decode("utf-16-le")

    def test_git_linux(self):
        h = cfg().host("web1")
        self.assertEqual(fleet.git_remote_command(h, "/srv/my app", ["pull", "--ff-only"]),
                         "cd '/srv/my app' && GIT_TERMINAL_PROMPT=0 git pull --ff-only")

    def test_git_windows(self):
        h = cfg().host("win")
        ps = self.decode_ps(fleet.git_remote_command(h, "D:\\it's", ["status", "-sb"]))
        self.assertIn("Set-Location -LiteralPath 'D:\\it''s'", ps)
        self.assertIn("git 'status' '-sb'", ps)
        self.assertIn("$ErrorActionPreference = 'Stop'", ps)
        self.assertIn("[Console]::OutputEncoding", ps)

    def test_add_key_linux_is_idempotent_command(self):
        cmd = fleet.add_key_command(cfg().host("web1"), "ssh-ed25519 AAAA me@pc")
        self.assertIn("grep -qxF 'ssh-ed25519 AAAA me@pc' ~/.ssh/authorized_keys ||", cmd)

    def test_add_key_windows(self):
        ps = self.decode_ps(fleet.add_key_command(cfg().host("win"), "ssh-ed25519 AAAA+/= me"))
        self.assertIn("$k = 'ssh-ed25519 AAAA+/= me'", ps)
        self.assertIn("administrators_authorized_keys", ps)

    def test_put_file(self):
        self.assertIn("base64 -d > ~/.ssh/fleet.conf && chmod 600", fleet.put_file_command(cfg().host("web1"), ".ssh", "fleet.conf"))
        ps = self.decode_ps(fleet.put_file_command(cfg().host("win"), ".fleet", "fleet.py"))
        self.assertIn("Join-Path $env:USERPROFILE '.fleet'", ps)

    def test_windows_command_fits_cmd_line(self):
        ps = fleet.add_key_command(cfg().host("win"), "ssh-ed25519 " + "A" * 68 + " comment")
        self.assertLess(len(ps), 8000)

    def test_select(self):
        fl = cfg()
        self.assertEqual([h.alias for h in fl.select("win, web1")], ["web1", "win"])
        with self.assertRaises(fleet.FleetError):
            fl.select("nope")


class WindowsEnvTests(unittest.TestCase):
    def test_ps_encoded_args_round_trip(self):
        args = fleet.ps_encoded_args("Write-Output '中文'")
        self.assertEqual(args[:4], ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand"])
        self.assertEqual(base64.b64decode(args[4]).decode("utf-16-le"), "Write-Output '中文'")

    def test_win_ssh_path_is_resolved(self):
        if os.name == "nt":
            self.assertTrue(Path(fleet.WIN_SSH.strip('"')).is_file())

    def test_fleet_cmd_prefers_py_launcher(self):
        text = (Path(__file__).resolve().parent.parent / "fleet.cmd").read_text(encoding="utf-8")
        self.assertIn("PYTHONUTF8=1", text)
        self.assertIn("py -3", text)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_force_utf8_stdio(self):
        class FakeStream:
            encoding = "cp1252"

            def __init__(self):
                self.kwargs = {}

            def reconfigure(self, **kwargs):
                self.kwargs = kwargs

        out, err = FakeStream(), FakeStream()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            fleet._force_utf8_stdio()
        self.assertEqual(out.kwargs.get("encoding"), "utf-8")
        self.assertEqual(err.kwargs.get("encoding"), "utf-8")

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_local_run_success_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / "fleet.ini"
            ini.write_text(
                "[settings]\nkey = test_key\nself = local\n\n[host local]\nhostname = 127.0.0.1\nuser = test\n",
                encoding="utf-8",
            )
            ok_buf = io.StringIO()
            with redirect_stdout(ok_buf):
                ok_code = fleet.main(["-c", str(ini), "run", "-o", "local", "--", "Write-Output", "OK"])
            self.assertEqual(ok_code, 0)
            self.assertIn("OK", ok_buf.getvalue())

            bad_buf = io.StringIO()
            with redirect_stdout(bad_buf):
                bad_code = fleet.main(["-c", str(ini), "run", "-o", "local", "--", "NoSuchCommandXyz"])
            self.assertEqual(bad_code, 1)
            self.assertIn("FAIL", bad_buf.getvalue())

    def test_run_parallel_empty(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(fleet.run_parallel([], 8, 10), 0)


class ScanTests(unittest.TestCase):
    def test_scan_ssh_hosts_skips_fleet_conf_and_wildcards(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ssh = home / ".ssh"
            ssh.mkdir()
            (ssh / "fleet.conf").write_text("Host fake1\n  HostName 10.0.0.99\n", encoding="utf-8")
            (ssh / "config").write_text(
                "Include fleet.conf\n"
                "Host real1\n  HostName 192.0.2.10\n  User deploy\n  Port 2222\n  ForwardAgent yes\n"
                "Host real2\n  HostName 192.0.2.11\n  ProxyJump real1\n"
                "Host *.example\n  HostName 192.0.2.12\n",
                encoding="utf-8",
            )
            (ssh / "id_ed25519").write_text("", encoding="utf-8")

            scan = fleet.scan_home(home)
            self.assertEqual([h.alias for h in scan.hosts], ["real1", "real2"])
            self.assertEqual(scan.hosts[0].port, 2222)
            self.assertTrue(scan.hosts[0].forward)
            self.assertEqual(scan.hosts[1].jump, "real1")
            self.assertEqual(scan.key, "id_ed25519")

            text = fleet.render_scanned_config(scan)
            parsed = fleet.parse_config(text)
            self.assertEqual([h.alias for h in parsed.hosts], ["real1", "real2"])

    def test_scan_git_accounts_skips_placeholders(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".gitconfig").write_text(
                '[includeIf "gitdir/i:~/work/"]\n  path = ~/.gitconfig-github-work\n'
                '[includeIf "gitdir/i:~/personal/"]\n  path = ~/.gitconfig-github-personal\n',
                encoding="utf-8",
            )
            (home / ".gitconfig-github-work").write_text(
                "[user]\n  name = Real Name\n  email = real@example.com\n", encoding="utf-8")
            (home / ".gitconfig-github-personal").write_text(
                "[user]\n  name = Your Name\n  email = you@example.com\n", encoding="utf-8")

            accounts = fleet.scan_git_accounts(home, "id_ed25519")
            self.assertEqual([g.alias for g in accounts], ["github-work"])
            self.assertEqual(accounts[0].dir, "~/work/")
            self.assertEqual(accounts[0].key, "id_ed25519")

    def test_init_scan_uses_scanned_hosts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "scan.ini"
            scan = fleet.ScanResult(hosts=[fleet.Host("h1", "192.0.2.1")], key="id_ed25519")
            with mock.patch.object(fleet, "scan_home", return_value=scan), redirect_stdout(io.StringIO()):
                self.assertEqual(fleet.main(["init", str(p)]), 0)
            text = p.read_text(encoding="utf-8")
            self.assertIn("[host h1]", text)
            self.assertNotIn("[host web1]", text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ini = Path(self.tmp.name) / "fleet.ini"
        self.ini.write_text(BASE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = fleet.main(["-c", str(self.ini)] + list(args))
        return code, buf.getvalue()

    def test_example_ini_is_valid(self):
        fl = fleet.parse_config(fleet.EXAMPLE_INI)
        self.assertEqual(len(fl.hosts), 4)
        self.assertEqual(fl.host("win-srv").repos, ["D:\\code\\portal"])

    def test_init(self):
        p = Path(self.tmp.name) / "new.ini"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(fleet.main(["init", str(p), "--example"]), 0)
            self.assertEqual(fleet.main(["init", str(p), "--example"]), 2)
        self.assertEqual(p.read_text(encoding="utf-8"), fleet.EXAMPLE_INI)

    def test_git_dry_run_and_options_after_action(self):
        code, out = self.call("git", "pull", "-n", "-o", "web1")
        self.assertEqual(code, 0)
        self.assertIn("/srv/a  git pull --ff-only", out)
        self.assertNotIn("portal", out)

    def test_git_raw_args(self):
        code, out = self.call("git", "-n", "--", "log", "-1", "--oneline")
        self.assertIn("git log -1 --oneline", out)

    def test_run_dry(self):
        code, out = self.call("run", "-n", "-o", "db1", "--", "df", "-h")
        self.assertIn("db1: df -h", out)

    def test_setup_dry_run(self):
        code, out = self.call("setup", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("本机 → web1 → app1", out)

    def test_ls_and_config(self):
        self.assertEqual(self.call("ls")[0], 0)
        code, out = self.call("config", "--for", "win")
        self.assertNotIn("ControlMaster", out)


if __name__ == "__main__":
    unittest.main()
