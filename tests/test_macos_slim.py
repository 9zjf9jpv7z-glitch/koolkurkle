#!/usr/bin/env python3
"""Contract + tick-state tests for macos-slim (Heavy 20260905-02). No launchctl."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLIM = ROOT / "macos-slim"
README = SLIM / "README.md"
CONTROLLER = SLIM / "macos-slim.sh"
APPLY = SLIM / "apply.sh"
RESTORE = SLIM / "restore.sh"
USER_PLIST = SLIM / "com.user.macos-slim.plist.template"
ROOT_HELPER = SLIM / "root" / "macos-slim-root.sh"
ROOT_PLIST = SLIM / "root" / "com.user.macos-slim-root.plist"
INSTALL_ROOT = SLIM / "root" / "INSTALL-ROOT.sh"
SUDOERS = SLIM / "root" / "macos-slim.sudoers.example"

REQUIRED_AGENTS = (
    "com.apple.mediaanalysisd",
    "com.apple.photoanalysisd",
    "com.apple.photolibraryd",
    "com.apple.duetexpertd",
)
FORBIDDEN_AGENTS = (
    "com.apple.coreduetd",
    "coreduetd",
    "com.apple.dasd",
    "dasd",
    "com.apple.suggestd",
    "suggestd",
    "com.apple.sharingd",
    "sharingd",
    "com.apple.rapportd",
    "rapportd",
    "com.apple.useractivityd",
    "useractivityd",
)
CLI_COMMANDS = (
    "arm",
    "disarm",
    "persist",
    "restore-now",
    "status",
    "install",
    "uninstall",
    "tick",
)


class FileLayoutTests(unittest.TestCase):
    def test_expected_paths_exist(self):
        for path in (
            README,
            CONTROLLER,
            APPLY,
            RESTORE,
            USER_PLIST,
            ROOT_HELPER,
            ROOT_PLIST,
            INSTALL_ROOT,
            SUDOERS,
        ):
            self.assertTrue(path.is_file(), path)

    def test_scripts_are_owner_executable_not_world_writable(self):
        # git stores the +x bit (0755 after clone). Operator/install chmod 700.
        for path in (CONTROLLER, APPLY, RESTORE, ROOT_HELPER, INSTALL_ROOT):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertTrue(mode & stat.S_IXUSR, f"{path} not executable")
            self.assertFalse(mode & stat.S_IWOTH, f"{path} world-writable")

    def test_plists_are_not_executable(self):
        for path in (USER_PLIST, ROOT_PLIST):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertFalse(mode & stat.S_IXUSR, f"{path} executable")
            self.assertFalse(mode & stat.S_IWOTH, f"{path} world-writable")


class AgentSetTests(unittest.TestCase):
    def test_four_agents_in_apply_restore_controller(self):
        for path in (APPLY, RESTORE, CONTROLLER):
            text = path.read_text(encoding="utf-8")
            for label in REQUIRED_AGENTS:
                self.assertIn(label, text, f"{label} missing from {path.name}")

    def test_forbidden_agents_not_in_slim_set(self):
        for path in (APPLY, RESTORE, CONTROLLER, ROOT_HELPER):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "do not add" in stripped.lower() or "Do not add" in line:
                    continue
                if stripped.startswith("printf") or stripped.startswith("cat"):
                    continue
                for bad in FORBIDDEN_AGENTS:
                    if bad in ("dasd", "coreduetd"):
                        # short names only count as standalone tokens in code
                        if f"com.apple.{bad}" in stripped or stripped == bad:
                            self.fail(f"{path.name} slim set includes {bad}: {line}")
                    elif bad.startswith("com.apple."):
                        self.assertNotIn(bad, stripped, f"{path.name}: {line}")

    def test_root_helper_is_mdutil_only(self):
        text = ROOT_HELPER.read_text(encoding="utf-8")
        self.assertIn("mdutil -d", text)
        self.assertIn("mdutil -i on", text)
        self.assertIn("tmutil destinationinfo", text)
        self.assertIn("/System/Volumes/Data", text)
        code = "\n".join(
            line
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("launchctl", code)
        for label in REQUIRED_AGENTS:
            self.assertNotIn(label, text)


class CliAndTickContractTests(unittest.TestCase):
    def test_cli_commands_present(self):
        text = CONTROLLER.read_text(encoding="utf-8")
        for cmd in CLI_COMMANDS:
            self.assertIn(cmd, text)

    def test_tick_state_machine_comments_or_branches(self):
        text = CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("armed)", text)
        self.assertIn("slim)", text)
        self.assertIn("persist)", text)
        self.assertIn("kern.bootsessionuuid", text)
        self.assertIn("mode=off", text)
        self.assertIn("did not apply", text)

    def test_install_does_not_apply(self):
        text = CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("cmd_install", text)
        install_fn = text.split("cmd_install()")[1].split("cmd_uninstall()")[0]
        self.assertNotIn("run_apply", install_fn)
        self.assertIn("ensure_mode_off_if_missing", install_fn)
        self.assertIn("PATH_LINE", text)
        self.assertIn('export PATH="$HOME/bin:$PATH"', text)

    def test_apply_restore_launchctl_verbs(self):
        apply_txt = APPLY.read_text(encoding="utf-8")
        restore_txt = RESTORE.read_text(encoding="utf-8")
        self.assertIn("launchctl disable", apply_txt)
        self.assertIn("launchctl bootout", apply_txt)
        self.assertIn("kill SIGINT", apply_txt)
        self.assertIn("launchctl enable", restore_txt)
        self.assertIn("launchctl kickstart", restore_txt)
        self.assertIn("/usr/local/libexec/macos-slim-root.sh", apply_txt)
        self.assertIn("/usr/local/libexec/macos-slim-root.sh", restore_txt)


class PlistTests(unittest.TestCase):
    def test_user_template(self):
        # template has placeholders; substitute so plistlib can parse
        raw = USER_PLIST.read_text(encoding="utf-8")
        filled = raw.replace("__MACOS_SLIM_SH__", "/tmp/macos-slim.sh").replace(
            "__HOME__", "/tmp"
        )
        data = plistlib.loads(filled.encode("utf-8"))
        self.assertEqual(data["Label"], "com.user.macos-slim")
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["StartInterval"], 300)
        self.assertEqual(data["ProgramArguments"][-1], "tick")
        self.assertIn("macos-slim.sh", data["ProgramArguments"][1])

    def test_root_plist_boot_only(self):
        data = plistlib.loads(ROOT_PLIST.read_bytes())
        self.assertEqual(data["Label"], "com.user.macos-slim-root")
        self.assertTrue(data["RunAtLoad"])
        self.assertNotIn("StartInterval", data)
        self.assertEqual(
            data["ProgramArguments"],
            ["/usr/local/libexec/macos-slim-root.sh", "boot"],
        )


class SudoersAndReadmeTests(unittest.TestCase):
    def test_sudoers_is_template_only(self):
        text = SUDOERS.read_text(encoding="utf-8").strip()
        self.assertEqual(
            text,
            "USERNAME ALL=(root) NOPASSWD: /usr/local/libexec/macos-slim-root.sh",
        )
        self.assertNotIn("password", text.lower())

    def test_install_root_permissions(self):
        text = INSTALL_ROOT.read_text(encoding="utf-8")
        self.assertIn("-m 700", text)
        self.assertIn("-m 644", text)
        self.assertIn("bootstrap system", text)
        self.assertIn("macos-slim.sudoers.example", text)

    def test_readme_covers_required_topics(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("Mac Mini M4 24GB", text)
        self.assertIn("Tahoe", text)
        self.assertIn("26.3", text)
        self.assertIn("SIP stays on", text)
        self.assertIn("csrutil", text)
        self.assertIn("mode=off", text)
        self.assertIn("USERNAME ALL=(root) NOPASSWD", text)
        self.assertIn("com.apple.duetexpertd", text)
        self.assertIn("Apple Intelligence", text)
        self.assertIn("Spotlight", text)
        self.assertIn("Analytics", text)
        self.assertIn("com.mailroom.cull-photos", text)
        self.assertIn("chmod 700", text)
        self.assertIn("softwareupdated", text)
        self.assertIn("Do not `arm` or `persist`", text)
        for label in REQUIRED_AGENTS:
            self.assertIn(label, text)
        self.assertNotIn("kirkbacon", text)
        self.assertNotIn("@icloud.com", text)

    def test_no_secrets_in_tree(self):
        for path in SLIM.rglob("*"):
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("IMAP_APP_PASSWORD", raw)
            self.assertNotIn("-----BEGIN", raw)


class TickStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.calls = Path(self.tmp.name) / "calls.log"
        apply_sh = Path(self.tmp.name) / "apply.sh"
        restore_sh = Path(self.tmp.name) / "restore.sh"
        apply_sh.write_text(
            '#!/bin/sh\nprintf "apply\\n" >> "$MACOS_SLIM_CALLS"\n', encoding="utf-8"
        )
        restore_sh.write_text(
            '#!/bin/sh\nprintf "restore\\n" >> "$MACOS_SLIM_CALLS"\n', encoding="utf-8"
        )
        apply_sh.chmod(0o700)
        restore_sh.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update(
            {
                "MACOS_SLIM_STATE": str(self.state),
                "MACOS_SLIM_LOG": str(Path(self.tmp.name) / "slim.log"),
                "MACOS_SLIM_APPLY": str(apply_sh),
                "MACOS_SLIM_RESTORE": str(restore_sh),
                "MACOS_SLIM_CALLS": str(self.calls),
                "MACOS_SLIM_SKIP_LAUNCHCTL": "1",
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args: str, session: str = "boot-a") -> subprocess.CompletedProcess:
        env = self.env.copy()
        env["MACOS_SLIM_SESSION"] = session
        return subprocess.run(
            ["/bin/zsh", str(CONTROLLER), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _mode(self) -> str:
        return (self.state / "mode").read_text(encoding="utf-8").strip()

    def _session(self) -> str:
        p = self.state / "session"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def _calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return self.calls.read_text(encoding="utf-8").splitlines()

    def test_off_tick_is_noop(self):
        (self.state / "mode").write_text("off\n", encoding="utf-8")
        rc = self._run("tick")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(self._calls(), [])

    def test_armed_applies_and_becomes_slim(self):
        rc = self._run("arm", session="boot-a")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(self._mode(), "slim")
        self.assertEqual(self._session(), "boot-a")
        self.assertEqual(self._calls(), ["apply"])

    def test_slim_same_session_reapplies(self):
        (self.state / "mode").write_text("slim\n", encoding="utf-8")
        (self.state / "session").write_text("boot-a\n", encoding="utf-8")
        rc = self._run("tick", session="boot-a")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(self._mode(), "slim")
        self.assertEqual(self._calls(), ["apply"])

    def test_slim_new_session_restores_off(self):
        (self.state / "mode").write_text("slim\n", encoding="utf-8")
        (self.state / "session").write_text("boot-a\n", encoding="utf-8")
        rc = self._run("tick", session="boot-b")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(self._mode(), "off")
        self.assertEqual(self._calls(), ["restore"])

    def test_persist_applies_and_updates_session(self):
        rc = self._run("persist", session="boot-a")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(self._mode(), "persist")
        self.assertEqual(self._session(), "boot-a")
        self.assertEqual(self._calls(), ["apply"])
        rc = self._run("tick", session="boot-b")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(self._mode(), "persist")
        self.assertEqual(self._session(), "boot-b")
        self.assertEqual(self._calls(), ["apply", "apply"])

    def test_restore_now_sets_off(self):
        (self.state / "mode").write_text("slim\n", encoding="utf-8")
        rc = self._run("restore-now")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(self._mode(), "off")
        self.assertEqual(self._calls(), ["restore"])

    def test_status_lists_four_agents(self):
        rc = self._run("status")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        for label in REQUIRED_AGENTS:
            self.assertIn(label, rc.stdout)

    def test_install_writes_mode_off_without_apply(self):
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        env_home = self.env.copy()
        env_home["HOME"] = str(home)
        env_home["MACOS_SLIM_STATE"] = str(home / "Library" / "Application Support" / "macos-slim")
        env_home["MACOS_SLIM_LOG"] = str(home / "slim.log")
        env_home["MACOS_SLIM_ZSHRC"] = str(home / ".zshrc")
        env_home["MACOS_SLIM_BIN"] = str(home / "bin" / "macos-slim")
        env_home["MACOS_SLIM_PLIST"] = str(
            home / "Library" / "LaunchAgents" / "com.user.macos-slim.plist"
        )
        env_home["MACOS_SLIM_SKIP_LAUNCHCTL"] = "1"
        rc = subprocess.run(
            ["/bin/zsh", str(CONTROLLER), "install"],
            env=env_home,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rc.returncode, 0, rc.stderr + rc.stdout)
        mode = (
            home / "Library" / "Application Support" / "macos-slim" / "mode"
        ).read_text(encoding="utf-8").strip()
        self.assertEqual(mode, "off")
        self.assertFalse(self.calls.exists())
        zshrc = (home / ".zshrc").read_text(encoding="utf-8")
        self.assertIn('export PATH="$HOME/bin:$PATH"', zshrc)
        self.assertTrue((home / "bin" / "macos-slim").is_symlink())
        plist = plistlib.loads(
            (home / "Library" / "LaunchAgents" / "com.user.macos-slim.plist").read_bytes()
        )
        self.assertEqual(plist["Label"], "com.user.macos-slim")
        self.assertEqual(plist["StartInterval"], 300)
        # second install does not duplicate PATH
        rc2 = subprocess.run(
            ["/bin/zsh", str(CONTROLLER), "install"],
            env=env_home,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rc2.returncode, 0, rc2.stderr)
        self.assertEqual(zshrc.count('export PATH="$HOME/bin:$PATH"'), 1)


if __name__ == "__main__":
    unittest.main()
