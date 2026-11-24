"""Validate the release candidate without opening task instructions."""

import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tokenize
import tomllib


DATASET = "datasets/harbor-dev"
MANIFEST = "protocols/harbor-dev-manifest.json"
MANIFEST_SHA256 = "60b3cd70a0e2f21af107cd2e1d91b2c3e18fb16636914647b5f34e1f72a8f358"
ROOT_FILES = {"README.md", "LICENSE", ".gitignore", MANIFEST}
REQUIRED = {
    "README.md", "LICENSE", MANIFEST, "src/redbench/check_release.py",
    "src/redbench/runner.py", "src/redbench/replay.py", "src/redbench/scoring.py",
}
DIRECTORIES = {"protocols", "datasets", DATASET, "src", "src/redbench", "tests"}
PATTERNS = (
    re.compile(r"src/redbench/[a-z_][a-z_0-9]*\.py\Z"),
    re.compile(r"tests/(?:test_[a-z_0-9]+|__init__)\.py\Z"),
)
PRIVATE_PATH_PART = re.compile(
    r"(?:^|[_.-])(?:tasks[_-]private|private|gold|oracle|solution(?:s)?|ledgers?|traces?|raw|secrets?|"
    r"keys?|packets?|holdout|evidence)(?:$|[_.-])",
    re.IGNORECASE,
)
SECRET_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{25,}|"
    r"sk-(?:or-v1-|proj-|svcacct-)?[A-Za-z0-9_-]{24,}|"
    r"(?:rrk?|rrlive|nanogpt)_[A-Za-z0-9_-]{24,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{12,}\.eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{20,})\b"
    r"|-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"|(?i:authorization:\s*bearer\s+)[A-Za-z0-9_-]{24,}"
)
PERSONAL_PATH = re.compile(
    r"(?i)(?:/(?:users|home)/[A-Za-z0-9._-]+|/(?:private/)?var/folders/[A-Za-z0-9._-]+|"
    r"[A-Z]:[\\/](?:users|documents and settings)[\\/][A-Za-z0-9._-]+)"
)
SYNTHETIC_AUTH_LOG = re.compile(rf"{DATASET}/w01-(?:authentic|control|hold)/environment/fixtures/auth\.log\Z")
MAX_FILE_BYTES = 8 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path, info):
    if info.st_size > MAX_FILE_BYTES:
        raise ValueError("Oversized file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (
            opened.st_dev, opened.st_ino, opened.st_size
        ) != (info.st_dev, info.st_ino, info.st_size):
            raise ValueError("File changed during audit")
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) != info.st_size:
        raise ValueError("File changed during audit")
    return data


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def reject_json_constant(_):
    raise ValueError("Nonstandard JSON constant")


def load_json(data):
    return json.loads(data, object_pairs_hook=unique_keys, parse_constant=reject_json_constant)


def safe_mode(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def read_manifest(root):
    if not stat.S_ISDIR((root / "protocols").lstat().st_mode):
        raise ValueError("Invalid manifest directory")
    path = root / MANIFEST
    info = path.lstat()
    if not safe_mode(info):
        raise ValueError("Invalid manifest file")
    data = read_regular(path, info)
    if digest(data) != MANIFEST_SHA256:
        raise ValueError("Manifest digest mismatch")
    value = load_json(data)
    return {f"{DATASET}/{rel}": record for rel, record in value["files"].items()}


def check_code(data, issues):
    try:
        tokens = tokenize.tokenize(io.BytesIO(data).readline)
        for item in tokens:
            if item.type == tokenize.COMMENT and not (
                (item.start[0] == 1 and item.string.startswith("#!"))
                or (item.start[0] <= 2 and re.match(r"^#.*coding[=:]", item.string))
            ):
                issues["source_comment"] += 1
    except (SyntaxError, UnicodeError, tokenize.TokenError):
        issues["invalid_python_tokens"] += 1


def allowed_file(name, dataset):
    if name in dataset or name in ROOT_FILES or name == f"{DATASET}/README.md":
        return True
    if any(PRIVATE_PATH_PART.search(part) for part in name.split("/")):
        return False
    return any(pattern.fullmatch(name) for pattern in PATTERNS)


def scan_file(path, name, info, expected, issues):
    if expected is not None:
        if info.st_size != expected["bytes"]:
            issues["dataset_size_mismatch"] += 1
        if name.endswith("/instruction.md"):
            if not info.st_mode & stat.S_IRUSR:
                issues["instruction_unreadable"] += 1
            return
    try:
        data = read_regular(path, info)
        text = data.decode("utf-8")
    except (OSError, ValueError):
        issues["unreadable_or_binary_file"] += 1
        return
    pinned = expected is not None and digest(data) == expected["sha256"]
    if expected is not None and not pinned:
        issues["dataset_digest_mismatch"] += 1
    if SECRET_PATTERN.search(text):
        issues["credential_signature"] += 1
    synthetic_home = pinned and SYNTHETIC_AUTH_LOG.fullmatch(name) is not None
    if any(
        not (synthetic_home and match.group().casefold().startswith("/home/"))
        for match in PERSONAL_PATH.finditer(text)
    ):
        issues["personal_path"] += 1
    if name.endswith("/task.toml") and expected is not None:
        try:
            task = tomllib.loads(text)
            if task.get("environment", {}).get("network_mode") != "no-network":
                issues["networked_task"] += 1
        except (ValueError, AttributeError):
            issues["invalid_task_metadata"] += 1
    if name.endswith("/tests/test.sh") and expected is not None and not info.st_mode & stat.S_IXUSR:
        issues["verifier_not_executable"] += 1
    if name.endswith(".py") and (expected is not None or name.startswith(("src/", "tests/"))):
        check_code(data, issues)


def audit(root, allow_git_metadata=False):
    issues = Counter()
    root = root.absolute()
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise ValueError("Invalid release root")
        dataset = read_manifest(root)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return {"status": "failed", "issues": {"invalid_root_or_manifest": 1}, "files": 0, "tasks": 0}
    allowed_directories = DIRECTORIES | {
        parent.as_posix() for name in dataset for parent in PurePosixPath(name).parents
    }
    stack = [root]
    files = set()
    while stack:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            issues["unreadable_directory"] += 1
            continue
        for path in entries:
            name = path.relative_to(root).as_posix()
            try:
                info = path.lstat()
            except OSError:
                issues["unreadable_entry_metadata"] += 1
                continue
            if stat.S_ISLNK(info.st_mode):
                issues["symlink"] += 1
            elif stat.S_ISDIR(info.st_mode):
                if name == ".git" and allow_git_metadata:
                    continue
                if name in allowed_directories:
                    stack.append(path)
                else:
                    issues["unapproved_directory"] += 1
            elif not stat.S_ISREG(info.st_mode):
                issues["special_file"] += 1
            elif not allowed_file(name, dataset):
                issues["unapproved_file"] += 1
            elif not safe_mode(info):
                issues["hardlink"] += 1
            else:
                files.add(name)
                scan_file(path, name, info, dataset.get(name), issues)
    missing = (REQUIRED | dataset.keys()) - files
    if missing:
        issues["missing_file"] += len(missing)
    tasks = sum(name in dataset and name.endswith("/task.toml") for name in files)
    return {
        "status": "failed" if issues else "passed", "issues": dict(sorted(issues.items())),
        "files": len(files), "tasks": tasks, "git_metadata_skipped": allow_git_metadata,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_root", type=Path)
    parser.add_argument("--allow-git-metadata", action="store_true")
    args = parser.parse_args()
    result = audit(args.release_root, args.allow_git_metadata)
    print(json.dumps(result, sort_keys=True))
    return int(result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
