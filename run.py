"""A/B/C RSI scheduler: cold start, repeated evaluation, stability and forgetting repair."""
import argparse
from pathlib import Path

from pipeline.app import run
from pipeline.lifecycle import RunCancelled
from pipeline.storage import log


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Result directory; default: runs/<domain>-<UTC timestamp>")
    parser.add_argument("--resume", action="store_true", help="Continue completed rounds in --output; rounds is the total target")
    parser.add_argument("--rounds", type=int, default=1, help="Total rounds, including full-pool cold start as round 1")
    parser.add_argument("--config", type=Path, help="YAML with independent task_agent/meta_agent APIs")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--domain", default="terminal", help="Domain package under tasks/, e.g. terminal")
    parser.add_argument("--dataset", help="Optional dataset selected by the domain adapter; terminal supports tb/tbh")
    parser.add_argument("--split", choices=["train", "val", "test"], default="train")
    parser.add_argument("--limit", type=int, help="Use the first N tasks in the frozen split")
    parser.add_argument("--eval-only", action="store_true", help="Evaluate without any self-modification")
    parser.add_argument("--harness", type=Path, help="Seed version directory containing harness/")
    args = parser.parse_args()
    if args.resume and args.output is None:
        parser.error("--resume requires --output pointing to an existing run")
    if args.rounds < 1 or args.timeout < 1:
        parser.error("rounds and timeout must be positive")
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    run(args.output, args.rounds, timeout=args.timeout, config=args.config,
        domain=args.domain, dataset=args.dataset, split=args.split, limit=args.limit,
        eval_only=args.eval_only, harness=args.harness, resume=args.resume)


if __name__ == "__main__":
    try:
        main()
    except RunCancelled as exc:
        log(f'STOPPED: {exc}; last committed checkpoint preserved')
        raise SystemExit(1)
