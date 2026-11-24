import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from redbench import check_release


class ReleaseAuditTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        name = "w00-authentic"
        self.task = f"{check_release.DATASET}/{name}"
        members = {
            "instruction.md": "Review a fictional backup ticket.\n",
            "task.toml": '[environment]\nnetwork_mode = "no-network"\n',
            "environment/Dockerfile": "FROM scratch\n",
            "tests/test.sh": "#!/bin/sh\nexit 0\n",
        }
        files = {}
        for filename, value in members.items():
            data = value.encode()
            output = self.write(f"{self.task}/{filename}", data)
            info = {"bytes": len(data)}
            if filename != "instruction.md":
                info["sha256"] = check_release.digest(data)
            if filename == "tests/test.sh":
                output.chmod(0o755)
            files[f"{name}/{filename}"] = info
        manifest = json.dumps({"files": files}).encode()
        self.write(check_release.MANIFEST, manifest)
        patcher = patch.object(check_release, "MANIFEST_SHA256", check_release.digest(manifest))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in check_release.REQUIRED - {check_release.MANIFEST}:
            value = "Notes for an evaluation.\n" if not name.endswith(".py") else 'NAME = "release"\n'
            self.write(name, value.encode())

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_valid_release_never_reads_task_instructions(self):
        original = check_release.read_regular

        def record(path, info):
            self.assertNotEqual("instruction.md", path.name)
            return original(path, info)

        with patch.object(check_release, "read_regular", side_effect=record):
            result = check_release.audit(self.root)
        self.assertEqual({"status": "passed", "tasks": 1}, {key: result[key] for key in ("status", "tasks")})

    def test_reject_unapproved_paths_and_results(self):
        self.write(f"{self.task}/tests/unapproved.jsonl", b"")
        self.write("private/synthetic.txt", b"")
        self.write("results/scorecard.json", b"{}")
        result = check_release.audit(self.root)
        self.assertEqual("failed", result["status"])
        self.assertEqual(
            {"unapproved_file": 1, "unapproved_directory": 2}, result["issues"]
        )

    def test_reject_links_comments_credentials_and_personal_paths(self):
        (self.root / "tests").mkdir()
        os.link(self.root / "README.md", self.root / "tests/test_alias.py")
        (self.root / "tests/test_symlink.py").symlink_to(self.root / "LICENSE")
        secret = "gh" + "p_" + "a" * 40
        personal = "/" + "Users/" + "example/private"
        self.write("src/redbench/test_signatures.py", f"{secret}\n{personal}\n".encode())
        self.write("src/redbench/test.py", b"x = 1 # unreviewed marker\n")
        result = check_release.audit(self.root)
        self.assertEqual("failed", result["status"])
        for key in ("hardlink", "symlink", "source_comment", "credential_signature", "personal_path"):
            self.assertIn(key, result["issues"])

    def test_reject_modified_network_mode_and_manifest(self):
        self.write(f"{self.task}/task.toml", b'[environment]\nnetwork_mode = "host"\n')
        issues = check_release.audit(self.root)["issues"]
        self.assertIn("dataset_digest_mismatch", issues)
        self.assertIn("networked_task", issues)
        self.write(check_release.MANIFEST, b'{"files": {}}')
        self.assertEqual({"invalid_root_or_manifest": 1}, check_release.audit(self.root)["issues"])

    def test_synthetic_home_paths_require_an_exact_pinned_fixture(self):
        name = f"{check_release.DATASET}/w01-control/environment/fixtures/auth.log"
        original = ("/" + "home/" + "fictional-analyst").encode()
        fixture = self.write(name, original)
        expected = {"bytes": len(original), "sha256": check_release.digest(original)}
        issues = check_release.Counter()
        check_release.scan_file(fixture, name, fixture.lstat(), expected, issues)
        self.assertFalse(issues)
        fixture.write_bytes(original.replace(b"analyst", b"reviewer"))
        check_release.scan_file(fixture, name, fixture.lstat(), expected, issues)
        self.assertIn("dataset_digest_mismatch", issues)
        self.assertIn("personal_path", issues)


if __name__ == "__main__":
    unittest.main()
