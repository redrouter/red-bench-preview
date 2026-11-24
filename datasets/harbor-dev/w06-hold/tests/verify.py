"""Harbor verifier for saved artifacts and final mock action state."""

import json
import pathlib
import sys

TESTS = pathlib.Path(__file__).parent
sys.path.insert(0, str(TESTS / "pilot" / "runner"))


def verify(workspace, spec):
    from grade import agent_files, evaluate_checks

    for path in workspace.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            return [{"label": "workspace contains a non-regular entry", "pass": False}]
    artifacts = agent_files(workspace, spec["fixture_hashes"])
    final_response = artifacts.pop(spec["response"], "")
    return evaluate_checks(spec["checks"], artifacts, final_response, list(artifacts))


if __name__ == "__main__":
    spec = json.loads((TESTS / "checks.json").read_text())
    results = verify(pathlib.Path("/work"), spec)
    output = pathlib.Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    (output / "checks.json").write_text(json.dumps(results, indent=2) + "\n")
    (output / "reward.txt").write_text(f"{int(bool(results) and all(r['pass'] for r in results))}\n")
