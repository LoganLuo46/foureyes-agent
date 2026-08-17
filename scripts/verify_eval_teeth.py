"""Prove the trajectory evals **actually have teeth** (implementation spec §9.1, hard requirement).

Method: temporarily rewire the approval edge to `request_approval → execute_action`
(the Friday-afternoon refactor that quietly routes around interrupt), run the trajectory
evals → they **must go red**; then restore the file, rerun → they must go green.

Nobody should have to take this on faith or remember seeing it fail once — it has to replay:
  .venv/bin/python scripts/verify_eval_teeth.py            # fast (structural asserts + 1 run scenario)
  .venv/bin/python scripts/verify_eval_teeth.py --full     # every scenario

Whatever happens in between, the finally block puts agent/graph.py back the way it was.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH = ROOT / "agent" / "graph.py"
# Whichever interpreter is running this script — a local .venv, or the bare python in CI.
# Hardcoding .venv/bin/python here made this script pass locally and fail in CI, where no
# virtualenv exists.
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]

INTACT = '    g.add_edge("request_approval", "await_decision")'
SABOTAGED = ('    g.add_edge("request_approval", "execute_action")  # SABOTAGE: bypasses interrupt')

FAST_SELECTION = ["evals/test_trajectories.py", "-k",
                  "traj_bypass_check or traj_single_inbound_edge or "
                  "traj_approval_row_required or traj_001"]
FULL_SELECTION = ["evals/test_trajectories.py"]
# The CI default: pure topology assertions, no LLM calls, no DB — cheap enough for every push
STRUCTURAL_SELECTION = ["evals/test_trajectories.py", "-k",
                        "traj_bypass_check or traj_single_inbound_edge or "
                        "traj_approval_row_required or traj_action_client_isolated"]


def run_evals(selection: list[str]) -> tuple[int, str]:
    proc = subprocess.run(PYTEST + selection, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr)[-1500:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="run every trajectory scenario (slower)")
    ap.add_argument("--structural-only", action="store_true",
                    help="topology assertions only — no LLM calls, no DB (CI default)")
    args = ap.parse_args()
    if args.full:
        selection = FULL_SELECTION
    elif args.structural_only:
        selection = STRUCTURAL_SELECTION
    else:
        selection = FAST_SELECTION

    original = GRAPH.read_text()
    if INTACT not in original:
        print(f"FAIL: could not find the approval edge to sabotage in {GRAPH}\n"
              f"      expected line: {INTACT.strip()}", file=sys.stderr)
        return 2

    try:
        print("=== step 1: sabotage the approval edge (request_approval -> execute_action) ===")
        GRAPH.write_text(original.replace(INTACT, SABOTAGED))
        code, out = run_evals(selection)
        print(out.strip()[-600:])
        if code == 0:
            print("\nFAIL: evals stayed GREEN with the approval gate bypassed — they have no teeth.",
                  file=sys.stderr)
            return 1
        print(f"\nOK: evals went RED as required (exit={code})")
    finally:
        GRAPH.write_text(original)
        print("\n=== restored agent/graph.py ===")

    print("\n=== step 2: re-run against the intact graph ===")
    code, out = run_evals(selection)
    print(out.strip()[-400:])
    if code != 0:
        print(f"\nFAIL: evals are RED on the intact graph (exit={code}) — fix before trusting them.",
              file=sys.stderr)
        return 1

    print("\nOK: evals are GREEN on the intact graph.")
    print("\nVERDICT: trajectory evals have teeth "
          "(red when the approval gate is bypassed, green when it is intact).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
