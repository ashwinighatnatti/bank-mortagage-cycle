# Domain reference — seed data, rules, KPIs

Extracted from `Coforge AI-Native Mortgage Demo.html`. Use verbatim where possible; the numbers
were chosen so the demo tells a story (every program, every severity, every lane represented).

## Seed loans

`L(id, borrowers, type, program, amount, fico, city, st, dti, ltv)`

| id | borrowers | type | program | amount | fico | city | dti | ltv |
|---|---|---|---|---|---|---|---|---|
| LN-2026-0001 | Michael & Sarah Thompson | Purchase | Conv | 480,000 | 742 | Austin, TX | 38 | 80 |
| LN-2026-0002 | David Chen | Refi | FHA | 315,000 | 681 | Phoenix, AZ | 47 | 91 |
| LN-2026-0003 | Maria Garcia | Purchase | Jumbo | 625,000 | 715 | San Diego, CA | 41 | 78 |
| LN-2026-0004 | James Wilson | Purchase | VA | 245,000 | 698 | Tampa, FL | 43 | 100 |
| LN-2026-0005 | Emily Davis | Refi | Conv | 410,000 | 760 | Denver, CO | 33 | 68 |
| LN-2026-0006 | Robert Johnson | Purchase | Conv | 530,000 | 705 | Seattle, WA | 44 | 85 |
| LN-2026-0007 | Aisha Khan | Purchase | FHA | 298,000 | 688 | Atlanta, GA | 45 | 96 |
| LN-2026-0008 | Daniel & Rachel Brooks | Refi | Jumbo | 720,000 | 772 | Boston, MA | 36 | 70 |
| LN-2026-0009 | Carlos Mendez | Purchase | VA | 362,000 | 701 | San Antonio, TX | 42 | 100 |
| LN-2026-0010 | Grace Liu | Purchase | Conv | 455,000 | 744 | Portland, OR | 39 | 82 |

The MVP uses loans 0001–0006. Keep 0002 (FHA DTI breach, needs supervisor) and 0006 (title lien,
Critical, needs supervisor) — they carry the approval lane.

## Seed exceptions

`E(loanId, stage, type, severity, conf, disp, queue, rec, rationale, snap, requiresSup)`

| loan | stg | type | sev | conf | disp | q | sup |
|---|---|---|---|---|---|---|---|
| 0001 | 0 | Low-confidence OCR — income | Low | 71 | auto | — | |
| 0001 | 3 | TRID fee tolerance variance (LE vs CD) | Low | 88 | auto | — | |
| 0002 | 0 | Income calc variance (paystub vs W-2 vs VOE) | High | 64 | hitl | A | |
| 0002 | 1 | DTI exceeds program threshold (FHA 43%) | High | 79 | hitl | A | ✔ |
| 0003 | 1 | Appraisal value variance / high CU score | High | 73 | hitl | B | |
| 0003 | 0 | Large / unsourced deposit | Medium | 68 | hitl | C | |
| 0004 | 1 | Flood certification missing | Medium | 92 | auto | — | |
| 0004 | 2 | AUS DU result "Refer/Eligible" | High | 70 | hitl | A | |
| 0005 | 1 | Flood zone determination mismatch | Low | 90 | auto | — | |
| 0006 | 0 | Undisclosed debt / new credit inquiry | High | 66 | hitl | B | |
| 0006 | 2 | Title exception — judgment lien | Critical | 61 | hitl | B | ✔ |
| 0007 | 0 | Missing / expired bank statement | Medium | 93 | auto | — | |
| 0007 | 1 | Identity / SSN mismatch | Critical | 58 | hitl | C | |
| 0008 | 0 | Expired homeowners insurance (HOI) | Low | 90 | auto | — | |
| 0008 | 2 | AUS LPA result "Caution" | High | 69 | hitl | A | |
| 0009 | 1 | Flood zone determination mismatch | Low | 91 | auto | — | |
| 0010 | 0 | Low-confidence OCR — income (ambiguous) | Medium | 62 | hitl | B | |
| 0010 | 3 | TRID fee tolerance variance — large | High | 67 | hitl | C | |

Recommendation / rationale / evidence text, verbatim from the reference:

- **Low-confidence OCR — income** · rec `Re-OCR W-2 at 300dpi & cross-ref VOE` ·
  why `Primary extraction below 0.80 confidence on box 1.` · evidence `W-2 Box 1: $12█,400 (blurred scan)`
- **TRID fee tolerance variance (LE vs CD)** · rec `Recompute fees, apply 10% cumulative bucket` ·
  why `Recording fee +$45 within cumulative tolerance.` · evidence `LE $1,250 → CD $1,295`
- **Income calc variance** · rec `Analyst reconciles qualifying income basis` ·
  why `YTD paystub annualizes 8% above W-2; bonus seasonality unclear.` · evidence `Paystub $6,520/mo · W-2 $5,940/mo`
- **DTI exceeds program threshold (FHA 43%)** · rec `Evaluate compensating factors / counsel` ·
  why `Back-end DTI 47% vs 43% cap; reserves 4 mo.` · evidence `DTI 47.0% · cap 43.0%`
- **Appraisal value variance / high CU score** · rec `Order field review or rebuttal` ·
  why `Appraisal 6% under contract; CU 3.1.` · evidence `Contract $625k · Appraisal $588k · CU 3.1`
- **Large / unsourced deposit** · rec `Request 60-day sourcing & LOX` ·
  why `Single $48k deposit not tied to payroll.` · evidence `Deposit $48,000 on 14-May`
- **Flood certification missing** · rec `Auto-order LOMA / flood cert from vendor` ·
  why `No active flood determination on file.` · evidence `Flood cert: not ordered`
- **AUS DU result "Refer/Eligible"** · rec `Manual underwrite per VA residual income` ·
  why `DU Refer; residual income needs manual check.` · evidence `DU: Refer/Eligible`
- **Flood zone determination mismatch** · rec `Re-pull determination from authoritative source` ·
  why `Vendor zone X vs prior AE; re-pull resolves.` · evidence `Zone X vs AE (stale)`
- **Undisclosed debt / new credit inquiry** · rec `Soft-pull refresh; obtain borrower LOX` ·
  why `Two inquiries post-application; possible new auto loan.` · evidence `2 inquiries · 09-Jun, 12-Jun`
- **Title exception — judgment lien** · rec `Obtain payoff or waiver — needs sign-off` ·
  why `Open $14.2k judgment lien on title.` · evidence `Lien: $14,200 · 2023 judgment`
- **Missing / expired bank statement** · rec `Auto-request latest statement via Outreach` ·
  why `Latest month statement absent.` · evidence `Statement: Apr missing`
- **Identity / SSN mismatch** · rec `Re-verify SSA-89; CIP review` ·
  why `SSN trailing digits differ doc vs application.` · evidence `Doc ••••-1182 vs app ••••-1128`
- **Expired homeowners insurance (HOI)** · rec `Auto-order updated dec page from carrier` ·
  why `HOI dec page lapsed 11 days.` · evidence `HOI exp 31-May`
- **AUS LPA result "Caution"** · rec `Document reserves & rent history` ·
  why `LPA Caution on credit depth.` · evidence `LPA: Caution`

## Scan engine

`buildScanQueue()` emits a flat, deterministic event list:

1. One `reveal` per loan (ingest).
2. For `stage` in 0..2, for each loan: a `stage` event, then per exception at that stage —
   `predict`, then either (`repair`, `resolve-auto`) if `disp==='auto'`, or `route` if HITL.
3. A final `settle` event → `evaluateReadiness()`.

Playback steps one event at a time on a timer; speed multipliers 0.5× / 1× / 2× / 4×.

Log lines per event kind (agent → message):
- `reveal` → Document Intake Agent · `Ingested {id} — {borrowers} · {program} {type} · ${amount}`
- `stage 0` → Document Intake Agent · `Classified docs for {id} — W-2 (0.97), paystub (0.95), ID (0.93); extracted borrower & income fields.`
- `stage 1` → Processing Agent · `Ordered Title, Appraisal/AMC, Flood & Credit for {id}; LOS sync OK.`
- `stage 2` → Risk/AUS Scoring · `Ran AUS & risk scoring for {id}; computing DTI/LTV and pulling AUS finding.`
- `predict` → Validation Agent · `Predicted exception on {loan}: {type} (sev {severity}, conf {conf}%).`
- `repair` → Processing Agent · `Auto-repair on {loan}: {rec}…`
- `resolve-auto` → Validation Agent · `Resolved {loan}: {type} — auto-repaired by AI.`
- `route` → Workflow Orchestration Agent · `Routed {loan} ({type}) to HITL Queue {queue}.`
- `settle` → Supervisor Agent · `Scan complete. Auto-repairs applied; HITL cases awaiting analyst action.`
- ready → Supervisor Agent · `{id} is clean — delivered to Underwriters' Digital Hub as Ready for Underwriting.`

## Human action labels

`verify` → *Verified & Approved* · `recalc` → *Recalculated* ·
`request` → *Document requested (Outreach)* · `override` → *Overridden*

Approval record: `{ id:'AP-001', exId, loanId, type, proposedBy, queue, proposedAction, aiRec,
status:'pending'|'approved'|'rejected', t }`. Rejection returns the exception to `routed` with
note `Rejected: {reason}`.

## Underwriting rules (`hubData`)

| Program | DTI cap | LTV cap | FICO floor |
|---|---|---|---|
| Conv | 50 | 95 | 620 |
| FHA | 43 | 96.5 | 620 |
| VA | 50 | 100 | 620 |
| Jumbo | 43 | 80 | 700 |

CU score threshold `< 2.5`. AUS engine: FHA → TOTAL, else DU; LPA when the loan carries the
*LPA Caution* exception. Default result `Approve/Eligible` unless a DU/LPA exception exists.

Conditions list = one *Clear: {exception type}* per stage ≤ 2 exception (auto-checked when the
exception is resolved/approved), plus two standing conditions:
`Verify income documentation (W-2 + paystub)` and `Evidence of hazard insurance at closing`.

Decisions: `approve` → *Approved* · `approve-conditions` → *Approved with Conditions* ·
`suspend` → *Suspended* · `deny` → *Denied*.

GenAI document summaries shown in the Hub cover **Income package**, **Credit profile**, and
**Collateral**, each with a suggested action.

## KPIs

- `predicted` — exceptions off `idle`
- `autoRepaired` / `autoTotal` / `autoPct` — auto-lane throughput
- `resolved` — resolved + approved
- `openHitl` — routed + inqueue + pending
- `ready` / `decided` / `delivered` — pipeline progression
- `stp` — % of scanned loans whose every exception is auto-dispositioned
- `cycle` — `43 − min(14, round((resolved + delivered*2) / 2))` days, i.e. a 43-day baseline
  compressing toward 29 as work clears. Cosmetic, but keep the baseline honest.

## Validation checks (Rules Engine panel)

Fannie Mae / Freddie Mac eligibility · Income & employment (4506-C / VOE) · DTI within program
limits · Asset sourcing & reserves · TRID disclosure timing · Flood / property eligibility ·
CFPB / identity (CIP). Each renders Pass / Review / Fail against the loan's exceptions.
