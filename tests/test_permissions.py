import tempfile
import os
import unittest
from pathlib import Path
from src.permissions import PermissionPolicy


class ReadonlyCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sub dir").mkdir()

    def test_user_example_in_each_mode_without_prompt(self):
        for mode in ("strict", "trusted_workspace", "unattended"):
            policy = PermissionPolicy(mode, workspace=self.root)
            command = f'cd "{self.root.as_posix()}" && cat .agent/STATE.md 2>/dev/null | head -50'
            with self.subTest(mode=mode):
                self.assertEqual(policy.classify(command), "allow")
                self.assertIsNone(policy.check(command, lambda _: self.fail("unexpected prompt")))

    def test_readonly_variations(self):
        policy = PermissionPolicy("strict", self.root)
        for command in ['cat "file with spaces.md" | head -50',
                        'cd "sub dir" && cd .. && git status',
                        'rg TODO src | head -20', 'git diff --stat && git status',
                        'cat missing 2> /dev/null', 'cat missing > /dev/null']:
            with self.subTest(command=command):
                self.assertEqual(policy.classify(command), "allow")

    def test_unsafe_or_unsupported_compositions_need_approval(self):
        policy = PermissionPolicy("strict", self.root)
        for command in ['cat file | sh', 'cat file && rm target',
                        'cd .. && cat secret', 'cd subdir/../.. && cat secret',
                        'cd "sub dir" | cat file', 'cat file > output.txt',
                        'cat file 2> errors.log', 'cat file >> /dev/null',
                        'cat file | rg --pre evil', 'git diff --output=output.txt | cat',
                        'cat $(whoami) | head', 'cat `whoami` | head',
                        'cat file; echo done', 'cat file || echo done',
                        'cat file &', 'cat file &&', 'cat "unterminated | head',
                        'cat file 2>&1 | head', 'cat file | python -c evil']:
            with self.subTest(command=command):
                self.assertNotEqual(policy.classify(command), "allow")

    def test_hard_deny_wins_over_composition(self):
        self.assertEqual(PermissionPolicy("trusted_workspace", self.root).classify('cat file && shutdown /s'), "deny")

    def test_unattended_rejects_risky_pipeline_without_prompt(self):
        denied = PermissionPolicy("unattended", self.root).check('cat file | sh', lambda _: self.fail("unexpected prompt"))
        self.assertIsNotNone(denied)


class TrustedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "child").mkdir()
        self.policy = PermissionPolicy("trusted_workspace", self.root)

    def test_local_file_operations_do_not_prompt(self):
        commands = ["rm -rf build", "rm -f *.tmp", "mkdir -p output",
                    "cp -r src output", "mv old.txt new.txt", "touch file.txt",
                    "chmod 644 file.txt", "chown user file.txt", "rmdir output",
                    'echo hello > output.txt', 'cat input.txt >> output.txt',
                    'mkdir output; cp input.txt output; rm -f input.txt',
                    'cd child && rm -f ../file.txt',
                    'Remove-Item -LiteralPath child -Recurse -Force',
                    'Set-Content -Path file.txt -Value "hello world"',
                    'Copy-Item -Path file.txt -Destination child -Force',
                    'Move-Item -LiteralPath file.txt -Destination child',
                    'New-Item -Path child/new -ItemType Directory',
                    'Get-Content file.txt | Out-File child/copy.txt',
                    'git status && git reset --hard HEAD',
                    'git add . && git commit -m "local changes"',
                    'rg TODO src | head -20', 'pytest -q', 'npm run build']
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.policy.classify(command), "allow")
                self.assertIsNone(self.policy.check(command, lambda _: self.fail("unexpected prompt")))

    def test_paths_outside_workspace_need_approval(self):
        for command in ['rm -rf ../outside', 'cat ../outside', 'cp input.txt ../outside',
                        'mv ../outside local', 'echo data > ../outside',
                        'cat input.txt 2> ../errors', 'cd .. && rm file',
                        'cd child && rm ../../outside', 'git add ../outside',
                        'git commit -F../outside',
                        'Remove-Item -LiteralPath ../outside -Recurse -Force',
                        'Get-Content names.txt | Remove-Item',
                        'rm -- -value ../outside', 'rm --force=../outside',
                        'New-Item link -ItemType SymbolicLink -Value ../outside',
                        'cat file | python -c anything', 'python -c anything',
                        'rm $(cat names.txt)', 'git push origin main']:
            with self.subTest(command=command):
                self.assertEqual(self.policy.classify(command), "ask")

    def test_native_absolute_paths(self):
        inside = str(self.root / 'local file.txt')
        outside = str(self.root.parent / 'outside.txt')
        self.assertEqual(self.policy.classify(f'Remove-Item -LiteralPath "{inside}" -Force'), "allow")
        self.assertEqual(self.policy.classify(f'Remove-Item -LiteralPath "{outside}" -Force'), "ask")

    def test_actual_execution_directory_is_used(self):
        from src.permissions import permission_context
        with permission_context(self.root / "child"):
            policy = PermissionPolicy("trusted_workspace", self.root)
            self.assertEqual(policy.classify("rm ../file.txt"), "allow")
            self.assertEqual(policy.classify("rm ../../file.txt"), "ask")
        self.assertEqual(self.policy.classify("rm ../file.txt"), "ask")

    def test_hard_deny_still_takes_priority(self):
        for command in ['sudo rm local', 'cd child && sudo cat local', 'shutdown /s',
                        'reboot', 'mkfs /dev/sda', 'diskpart', 'dd if=local of=other', 'rm -rf /']:
            with self.subTest(command=command):
                self.assertEqual(self.policy.classify(command), "deny")

    def test_other_modes_do_not_allow_deletion(self):
        for mode in ("strict", "unattended"):
            self.assertEqual(PermissionPolicy(mode, self.root).classify("rm -rf child"), "ask")

    def test_symlink_escape_is_not_allowed(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("OS does not permit creating symlinks")
            self.assertEqual(self.policy.classify("rm -rf link/file"), "ask")
            self.assertEqual(self.policy.classify("rm -rf l*"), "ask")


if __name__ == "__main__":
    unittest.main()
