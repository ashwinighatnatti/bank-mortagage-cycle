"""STEP 5 — run the four agents over a real loan, against Claude on Foundry.

    python scripts/run_agents.py                        # LN-2026-0002
    python scripts/run_agents.py --loan LN-2026-0007
    python scripts/run_agents.py --agent validation     # one agent only
    python scripts/run_agents.py --loan LN-2026-0001 --loan LN-2026-0005

THIS SPENDS MONEY. Each agent runs under the per-run budget in config.py
(default $1.50, 30 tool calls, 120s). Four agents per loan, so budget roughly
$0.30-0.80 per loan in practice — the guideline pack caches after the first loan
of a program, which is where most of the saving comes from.

Watch for three things:

  · DENIED lines. The gate refusing a model-initiated call is the system
    working. A run with no refusals has not been tested.
  · cache reads on the second loan of the same program. Zero means a cache
    invalidator crept into the system blocks and every request is paying full
    price for the guideline pack.
  · lane assignments. The model supplies a confidence; policy assigns the lane.
    A high-confidence income_variance still going to HITL is the point.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# The model writes en-dashes and arrows; the Windows console is cp1252 by
# default and raises on them. Replace rather than fail -- losing a glyph is
# acceptable, losing the run is not.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app import agents, store                       # noqa: E402
from app.config import get_settings                 # noqa: E402
from app.db import init_db, session_scope           # noqa: E402
from app.models import ExceptionRecord, Loan, Run   # noqa: E402
from sqlmodel import select                         # noqa: E402

G, R, Y, D, B, OFF = ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")

ROLE = {
    "intake": "Document Intake",
    "validation": "Validation",
    "processing": "Processing",
    "summarizer": "Summarizer",
}


def on_event(ev: agents.Event) -> None:
    role = ROLE.get(ev.agent, ev.agent)
    if ev.kind == "tool":
        print(f"  {G}ok    {OFF} {role:<16} {ev.tool}")
    elif ev.kind == "denied":
        print(f"  {R}DENIED{OFF} {role:<16} {ev.tool}")
        print(f"         {D}{ev.text.split(': ', 1)[-1][:150]}{OFF}")
    elif ev.kind == "say":
        for line in ev.text.splitlines():
            if line.strip():
                print(f"  {D}{role:<16} {line[:110]}{OFF}")
    elif ev.kind == "error":
        print(f"  {R}ERROR {OFF} {role:<16} {ev.text[:150]}")
    elif ev.kind == "done":
        print(f"  {D}       {role:<16} {ev.text}{OFF}\n")


def show_loan(session, loan_id: str) -> None:
    loan = session.get(Loan, loan_id)
    excs = store.exceptions_for(session, loan_id)
    print(f"\n  {B}result{OFF}  ready={loan.ready}  scanned={loan.scanned}  "
          f"exceptions={len(excs)}")
    for e in excs:
        colour = G if e.lane == "auto" else Y
        sup = " · needs sign-off" if e.requires_sup else ""
        print(f"    {colour}{e.lane:<5}{OFF} {e.id}  {e.exception_type:<28} "
              f"{e.severity:<8} conf={e.confidence:<3} q={e.queue or '-'}{sup}")
        print(f"          {D}{e.disposition_reason}{OFF}")
        if e.confidence_revised_from:
            print(f"          {R}revised from {e.confidence_revised_from}: "
                  f"{e.revision_reason}{OFF}")


def show_cost(session, run_prefix: str) -> None:
    runs = session.exec(
        select(Run).where(Run.run_id.like(f"{run_prefix}%")).order_by(Run.started_at)  # type: ignore[union-attr]
    ).all()
    total_usd = sum(r.usd for r in runs)
    total_cache = sum(r.cache_read_tokens for r in runs)
    print(f"\n  {B}cost{OFF}")
    for r in runs:
        print(f"    {ROLE.get(r.agent, r.agent):<16} {r.status:<16} "
              f"in={r.input_tokens:>7,} out={r.output_tokens:>6,} "
              f"cache_read={r.cache_read_tokens:>7,} ${r.usd:.4f}")
    print(f"    {'TOTAL':<16} {'':<16} ${total_usd:.4f}  "
          f"cache reads {total_cache:,}")
    if total_cache == 0:
        print(f"    {Y}No cache reads. Expected on the first loan of a program; "
              f"on a later one it means the prefix is being invalidated.{OFF}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loan", action="append", default=None)
    parser.add_argument("--agent", action="append", default=None,
                        help="run only these agents, in pipeline order")
    parser.add_argument("--max-turns", type=int, default=agents.MAX_TURNS)
    args = parser.parse_args()

    loans = args.loan or ["LN-2026-0002"]
    pipeline = tuple(a for a in agents.PIPELINE if not args.agent or a in args.agent)
    if not pipeline:
        print(f"no such agent. known: {', '.join(agents.PIPELINE)}")
        return 1

    settings = get_settings()
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    print(f"\n  {settings.model_id} · {settings.foundry_client_kwargs()}")
    print(f"  budget per agent: {settings.max_tool_calls_per_run} calls · "
          f"${settings.max_usd_per_run} · {settings.max_run_seconds}s")

    init_db()
    with session_scope() as session:
        for loan_id in loans:
            loan = session.get(Loan, loan_id)
            if loan is None:
                known = [l.id for l in session.exec(select(Loan).order_by(Loan.id)).all()]
                print(f"\nno such loan: {loan_id}\nknown: {', '.join(known)}")
                return 1

            print(f"\n{B}{'=' * 78}{OFF}")
            print(f"{B}{loan.id}{OFF}  {loan.borrowers} · {loan.program} "
                  f"{loan.purpose} · ${loan.amount:,} · FICO {loan.fico} · "
                  f"stated DTI {loan.dti}%\n")

            prefix = f"RUN-{stamp}-{loan_id[-4:]}"
            out = agents.run_pipeline(
                session, loan_id, run_prefix=prefix, agents=pipeline,
                max_turns=args.max_turns, on_event=on_event,
            )

            if out.summary:
                print(f"  {B}underwriter summary{OFF}")
                for line in out.summary.splitlines():
                    print(f"    {line[:110]}")

            show_loan(session, loan_id)
            show_cost(session, prefix)

            if out.failed:
                print(f"\n  {R}agents that did not finish cleanly: "
                      f"{', '.join(r.agent + ' (' + r.stopped + ')' for r in out.failed)}{OFF}")

        ok, broken = store.verify_audit_chain(session)
        print(f"\n  audit chain  {'intact' if ok else f'BROKEN at {broken}'}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
