# Red-Bench

Runners, scoring code, and synthetic Harbor tasks for evaluating authorized defensive security work.

## Run the Harbor tasks

Install Docker and `uvx`, then run the empty-agent baseline from the repository root.

```sh
uvx --from 'harbor==0.25.0' --python 3.12 harbor run \
  -p datasets/harbor-dev -a nop --job-name red-bench-empty -n 4 \
  --jobs-dir "$(mktemp -d)"
```

The 12 tasks run in offline containers. The empty agent should earn zero on every task.

## API runner and scorer

Use Python 3.11 or newer on macOS or Linux. Keep case files, provider configurations, and run data outside the checkout.

```sh
PYTHONPATH=src python3 -m redbench.runner --help
PYTHONPATH=src python3 -m redbench.replay --help
```

The runner previews the request plan and estimated cost by default. Live execution requires `--execute`, the reviewed case file's SHA-256, and `OPENROUTER_API_KEY` in the environment. The replay command grades saved responses offline.

Case files use JSONL with `case_id`, `arm`, and `prompt` fields, plus optional `prior_context`. Use case IDs ending in `-r01`, `-r02`, and so on for replay. Route files use a JSON array with `id`, `model`, `provider`, `input_usd_per_million`, and `output_usd_per_million`.

## Checks

```sh
python3 src/redbench/check_release.py . --allow-git-metadata
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
```
