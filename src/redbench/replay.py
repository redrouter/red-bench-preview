"""Score an operator-held API ledger and emit aggregate JSON."""

import argparse
import json
from pathlib import Path

from .scoring import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--ledger", required=True, nargs="+", type=Path)
    parser.add_argument("--configs", required=True, nargs="+")
    parser.add_argument("--repeats", required=True, type=int)
    args = parser.parse_args()
    gold = json.loads(args.gold.read_bytes())
    rows = []
    for path in args.ledger:
        with path.open() as stream:
            rows.extend(json.loads(line) for line in stream if line.strip())
    report = evaluate(rows, gold, config_ids=args.configs, repeats=args.repeats)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
