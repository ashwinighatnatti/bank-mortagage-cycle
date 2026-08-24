"""STEP 4 — drive the twelve tools over a real loan, with no model in the loop.

    python scripts/tool_smoke.py                    # LN-2026-0002 (FHA, DTI breach)
    python scripts/tool_smoke.py --loan LN-2026-0007

A scripted sequence standing in for what the agents will do in step 5: intake
extracts, validation checks and raises, processing repairs what it may and gets
refused on what it may not. Every call goes through the real dispatcher, the
real gate and the real database.

The point is to see the refusals. A run where everything succeeds has not
tested anything — the interesting lines are the DENIED ones, and they should be
denied for reasons you can read.

This WRITES to the database: exceptions, audit rows, tool calls. Re-running
adds another set rather than replacing the last, which is correct for an
append-only trail but means the counts grow.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import store, tools                          # noqa: E402
from app.db import init_db, session_scope             # noqa: E402
from app.gate import RunContext, confirmation_token   # noqa: E402
from app.models import Loan                           # noqa: E402
from app.policy import RunBudget                      # noqa: E402
from app.rules import Outcome                         # noqa: E402
from sqlmodel import select                           # noqa: E402

import json                                           # noqa: E402

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def show(agent: str, tool: str, result: tools.ToolResult, note: str = "") -> dict | None:
    mark = f"{RED}DENIED{OFF}" if result.is_error else f"{GREEN}ok    {OFF}"
    print(f"  {mark} {agent:<10} {tool:<26} {note}")
    if result.is_error:
        reason = result.content.split(": ", 1)[-1].replace("\n", " ")
        print(f"         {DIM}{reason[:150]}{OFF}")
        return None
    try:
        return json.loads(result.content)
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loan", default="LN-2026-0002")
    args = parser.parse_args()

    init_db()
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    budget = RunBudget(max_tool_calls=40, max_tokens=200_000, max_seconds=120, max_usd=1.5)

    with session_scope() as session:
        loan = session.get(Loan, args.loan)
        if loan is None:
            known = [l.id for l in session.exec(select(Loan).order_by(Loan.id)).all()]
            print(f"no such loan: {args.loan}\nknown: {', '.join(known)}")
            return 1

        print(f"\n{BOLD}{loan.id}{OFF}  {loan.borrowers} · {loan.program} "
              f"{loan.purpose} · ${loan.amount:,} · stated DTI {loan.dti}%\n")

        # --- intake -------------------------------------------------------
        intake = RunContext(run_id=f"RUN-{stamp}-I", agent="intake", loan_id=loan.id)
        store.open_run(session, run_id=intake.run_id, agent="intake", loan_id=loan.id)

        listing = show(intake.agent, "list_documents",
                       tools.dispatch("list_documents", {"loan_id": loan.id},
                                      ctx=intake, session=session, budget=budget))
        kinds = {d["kind"]: d["doc_id"] for d in (listing or {}).get("documents", [])}
        print(f"         {DIM}{len(kinds)} documents: {', '.join(sorted(kinds))}{OFF}")

        if "urla" in kinds:
            show(intake.agent, "read_document",
                 tools.dispatch("read_document",
                                {"loan_id": loan.id, "doc_id": kinds["urla"]},
                                ctx=intake, session=session, budget=budget),
                 note="urla")
            show(intake.agent, "record_extraction",
                 tools.dispatch("record_extraction",
                                {"loan_id": loan.id, "doc_id": kinds["urla"],
                                 "name": "app_ssn_last4", "value": "5558",
                                 "confidence": 0.94,
                                 "source_span": "Social Security Number"},
                                ctx=intake, session=session, budget=budget),
                 note="app_ssn_last4")

        # An extraction confidence given as a percentage — the likely mistake.
        show(intake.agent, "record_extraction",
             tools.dispatch("record_extraction",
                            {"loan_id": loan.id, "doc_id": kinds.get("urla", "x"),
                             "name": "monthly_income", "value": "6520",
                             "confidence": 94},
                            ctx=intake, session=session, budget=budget),
             note="confidence as a percentage")

        store.close_run(session, intake.run_id)

        # --- validation ---------------------------------------------------
        print()
        val = RunContext(run_id=f"RUN-{stamp}-V", agent="validation", loan_id=loan.id)
        store.open_run(session, run_id=val.run_id, agent="validation", loan_id=loan.id)

        failed: list[dict] = []
        for rule_id in ("dti_within_program", "ltv_within_program",
                        "document_completeness", "identity_cip", "income_employment"):
            body = show(val.agent, "evaluate_rule",
                        tools.dispatch("evaluate_rule",
                                       {"loan_id": loan.id, "rule_id": rule_id},
                                       ctx=val, session=session, budget=budget),
                        note=rule_id)
            if body:
                colour = {"fail": RED, "pass": GREEN}.get(body["outcome"], DIM)
                print(f"         {colour}{body['outcome'].upper()}{OFF} "
                      f"{DIM}{body['detail'][:110]}{OFF}")
                if body["outcome"] == Outcome.FAIL:
                    failed.append(body)

        raised: list[dict] = []
        for body in failed[:2]:
            if not body.get("suggested_exception_type"):
                continue
            out = show(val.agent, "raise_exception",
                       tools.dispatch("raise_exception",
                                      {"loan_id": loan.id, "stage": 1,
                                       "exception_type": body["suggested_exception_type"],
                                       "label": body["rule"],
                                       "severity": body["suggested_severity"] or "Medium",
                                       "confidence": 91,
                                       "rationale": body["detail"],
                                       "recommendation": "Per the rule detail",
                                       "evidence_doc_id": kinds.get("urla", ""),
                                       # Deliberately not a verbatim quote, to
                                       # exercise the evidence check.
                                       "evidence_quote": body["evidence"] or "n/a"},
                                      ctx=val, session=session, budget=budget),
                       note=body["suggested_exception_type"])
            if out:
                verdict = "verified" if out["evidence_verified"] else "NOT FOUND in the document"
                print(f"         {DIM}lane={out['lane']} queue={out['queue']} "
                      f"conf={out['confidence']} evidence {verdict}{OFF}")
                raised.append(out)

        # Validation attempting a repair — the separation-of-duties refusal.
        if raised:
            show(val.agent, "apply_auto_repair",
                 tools.dispatch("apply_auto_repair",
                                {"loan_id": loan.id, "exception_id": raised[0]["exception_id"],
                                 "action": "close it myself"},
                                ctx=val, session=session, budget=budget),
                 note="validation trying to repair")
        store.close_run(session, val.run_id)

        # --- processing ---------------------------------------------------
        print()
        proc = RunContext(run_id=f"RUN-{stamp}-P", agent="processing", loan_id=loan.id)
        store.open_run(session, run_id=proc.run_id, agent="processing", loan_id=loan.id)

        for out in raised:
            show(proc.agent, "apply_auto_repair",
                 tools.dispatch("apply_auto_repair",
                                {"loan_id": loan.id, "exception_id": out["exception_id"],
                                 "action": "Applied the recommended remedy"},
                                ctx=proc, session=session, budget=budget),
                 note=f"{out['exception_id']} ({out['lane']} lane)")

        order = {"loan_id": loan.id, "service": "appraisal", "reason": "collateral review"}
        show(proc.agent, "order_vendor_service",
             tools.dispatch("order_vendor_service", order,
                            ctx=proc, session=session, budget=budget),
             note="unconfirmed — should queue")

        confirmed = RunContext(run_id=proc.run_id, agent="processing", loan_id=loan.id,
                               confirmations={confirmation_token("order_vendor_service", order)})
        show(confirmed.agent, "order_vendor_service",
             tools.dispatch("order_vendor_service", order,
                            ctx=confirmed, session=session, budget=budget),
             note="after a human confirms")
        show(confirmed.agent, "order_vendor_service",
             tools.dispatch("order_vendor_service",
                            {**order, "service": "title"},
                            ctx=confirmed, session=session, budget=budget),
             note="different service, same confirmation")
        store.close_run(session, proc.run_id)

        # --- what it cost and whether the trail holds ----------------------
        loan = session.get(Loan, loan.id)
        ok, broken = store.verify_audit_chain(session)
        print(f"\n  {BOLD}after{OFF}  ready={loan.ready}  "
              f"tool calls={budget.tool_calls}  "
              f"audit chain={'intact' if ok else f'BROKEN at {broken}'}")
        print(f"  {DIM}open HITL exceptions: "
              f"{len(store.open_hitl(session))}{OFF}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
