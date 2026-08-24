---
name: ai-native-mortgage
description: Build, extend, or reason about the Coforge AI-Native Mortgage agentic solution and its MVP demo. Use whenever work touches the mortgage origination pipeline, AI exception handling, HITL queues, supervisor approvals, the Underwriters' Digital Hub, loan/exception data shapes, the agent roster, or the Coforge design tokens. Also use when decoding the reference HTML bundle, or when adding agent design, tool design, or harness layers to this program.
---

# AI-Native Mortgage (Coforge)

Reference design and MVP conventions for an agentic mortgage-origination solution.

## Program shape

Three artifacts, in order of authority:

1. **Reference design** — `Coforge AI-Native Mortgage Demo.html` (repo root). A bundled React
   prototype covering the full origination lifecycle. This is the **source of truth for domain,
   vocabulary, and visual language**. It is a design artifact, not production code.
2. **MVP demo** — `mvp/index.html`. A single self-contained file, zero dependencies, opens by
   double-click. Demonstrates the *differentiating* slice, not the full lifecycle. Built now.
3. **Agentic solution** — real agents, tools, and harness. Later stages. Conventions reserved
   at the bottom of this file.

Never regress vocabulary. If the reference calls it an *exception* with a *disposition*, so does
everything downstream.

## Decoding the reference bundle

The reference HTML is a self-extracting bundle — 886 KB, 389 lines, gzip+base64 payloads. Do not
try to read it directly. Unpack it:

```bash
node - "Coforge AI-Native Mortgage Demo.html" <outdir> <<'EOF'
const fs=require('fs'),zlib=require('zlib'),path=require('path');
const html=fs.readFileSync(process.argv[2],'utf8'), out=process.argv[3];
fs.mkdirSync(out,{recursive:true});
const man=JSON.parse(html.match(/<script type="__bundler\/manifest">\s*([\s\S]*?)\s*<\/script>/)[1]);
for(const [uuid,v] of Object.entries(man)){
  let b=Buffer.from(v.data,'base64'); if(v.compressed) b=zlib.gunzipSync(b);
  const ext=v.mime.includes('javascript')?'js':v.mime.includes('css')?'css':'bin';
  fs.writeFileSync(path.join(out,uuid+'.'+ext),b);
}
const t=html.match(/<script type="__bundler\/template">\s*([\s\S]*?)\s*<\/script>/);
fs.writeFileSync(path.join(out,'_template.html'),JSON.parse(t[1]));
EOF
```

Then the **application logic** is the third inline `<script type="text/x-dc">` inside
`_template.html` (~137 KB, a `class Component extends DCLogic`). Extract it with a regex on
`<script...>([\s\S]*?)</script>`. The two `.js` payloads are framework (`dc-runtime`) and the
design-system bundle (`OnboardXSecureDesignSystem_45f068`) — rarely worth reading.

Useful anchors in the app script: `freshDemo()` (seed data), `buildScanQueue()`/`applyEvent()`
(the agent engine), `evaluateReadiness()`, `hubData()` (underwriting rules), `kpis()`.

## Domain model

Preserve these shapes and enum values exactly.

**Loan**
```js
{ id:'LN-2026-0001', borrowers, type:'Purchase'|'Refi', program:'Conv'|'FHA'|'VA'|'Jumbo',
  amount, fico, city, st, dti, ltv,
  stage:-1..4, scanned:false, ready:false, decision:null, closingDone:false, delivered:false }
```

**Exception** — the central object. Everything the AI finds is an exception.
```js
{ id:'EX-001', loanId, stage, type, severity:'Low'|'Medium'|'High'|'Critical',
  conf:0..100, disp:'auto'|'hitl', queue:'A'|'B'|'C'|null,
  rec,        // the AI's recommended action
  rationale,  // why the AI flagged it
  snap,       // the evidence snippet shown to a human
  requiresSup:false, status:'idle', note:'' }
```

**Status machine** — `idle → predicted → repairing → resolved` (auto lane), or
`idle → predicted → routed → inqueue → resolved` (HITL lane), or
`… → inqueue → pending → approved` (supervisor lane). `rejected` sends it back to `routed`.

**Disposition is the whole story.** `disp:'auto'` = high confidence, mechanical fix, no human.
`disp:'hitl'` = judgment call, routed to an analyst queue. `requiresSup:true` = the analyst may
only *propose*; a supervisor approves. Confidence and severity drive disposition; keep low-conf
or Critical items in HITL even when the fix looks obvious.

**Stages** — index into
`['Application & Intake','Loan Processing','Underwriting','Closing & Funding','Post-Closing QC']`.
Only stages 0–2 gate readiness; stage-3 (TRID) exceptions block funding, not underwriting.

**Readiness rule** — a loan is `ready` when every exception at stage ≤ 2 is `resolved`, `approved`,
or still `idle`, *and* at least one has moved off `idle`. Ready files land in the Underwriters' Hub.

## Agent roster

Named actors that write to the activity log and audit trail. Keep the names verbatim.

| Agent | Does |
|---|---|
| Supervisor Agent | Initiates scans, declares files ready, closes the run |
| Document Intake Agent | Ingests files, classifies docs, extracts borrower/income fields |
| Processing Agent | Orders title/appraisal/flood/credit; executes auto-repairs |
| Validation Agent | Runs the rules engine, predicts exceptions, confirms resolutions |
| Risk/AUS Scoring | DTI/LTV computation, AUS findings (DU / LPA / TOTAL) |
| Workflow Orchestration Agent | Routes HITL cases to the right analyst queue |
| Loan Delivery Agent | Closing, funding, QC, investor delivery, servicing handoff |

Every agent action appends to **both** the live log and the audit trail, tagged `kind:'ai'`.
Every human action does the same, tagged `kind:'human'`. This dual-tagging is the governance
story — never let an action mutate state without an audit entry.

## Personas and RBAC

Password for all demo accounts: `Coforge@123`.

| Username | Name | Role | Queue |
|---|---|---|---|
| `analyst1` | Priya Nair | analyst | A |
| `analyst2` | Arjun Mehta | analyst | B |
| `analyst3` | Lena Rossi | analyst | C |
| `supervisor` | Marcus Webb | supervisor | — |
| `underwriter` | Diane Foster | underwriter | — |

Rules: only a supervisor sees **Approvals**. Analysts see their own queue highlighted but may view
all. Underwriters land on the Hub at login. Role switching without re-auth is a demo affordance —
keep it, and audit it.

## Design tokens

Coforge brand: coral primary, teal secondary, navy ink.

```
--coral-500:#F06048  --coral-600:#D84B34  --coral-400:#F37A5C
--teal-500:#189078   --teal-400:#2BA487   --teal-50:#E9F6F2
--navy-800:#0A2140   --navy-700:#0E1E45   --navy-500:#1E3A78  --navy-100:#D3DAE8
--amber-500:#F09048  --gold-400:#F4CB4A
--neutral-0:#FFFFFF  --neutral-50:#F7F8FB --neutral-100:#EEF1F6 --neutral-200:#E1E6EE
--neutral-400:#9AA6B6 --neutral-500:#6C7889 --neutral-700:#323D4C
--status-danger:#D8362B  --status-warning:var(--amber-500)  --status-success:var(--teal-500)

--font-display:'Poppins'      --font-body:'Mulish'      --font-mono:'JetBrains Mono'
--radius-sm:6px --radius-md:10px --radius-lg:14px --radius-xl:20px --radius-pill:999px
--shadow-sm:0 1px 3px rgba(10,33,64,.08),0 1px 2px rgba(10,33,64,.04)
--shadow-md:0 4px 12px rgba(10,33,64,.08),0 2px 4px rgba(10,33,64,.05)
--shadow-lg:0 12px 28px rgba(10,33,64,.12),0 4px 8px rgba(10,33,64,.06)
```

Severity → colour: Critical/High = danger, Medium = amber, Low = navy-300.
Lane → colour: auto-repair = teal, HITL = amber, supervisor sign-off = coral.

Full token set and the seed dataset: `references/domain.md`.

## MVP scope

The MVP proves the **agentic exception loop** — the part a customer cannot get from a workflow
tool. It is deliberately narrower than the reference.

**In scope** (6 screens)
1. **Dashboard** — 8 icon StatCards, 4 charts (line / bar / donut / area), Analyst Queue Load.
2. **Loan Pipeline** — 5-stage funnel, full pipeline table, live agent activity log.
3. **AI Exceptions & HITL** — role-adaptive: an analyst sees only their own queue with a queue
   strip; a supervisor gets the consolidated two-column board plus the severity/queue/status
   filter bar. Exception cards carry evidence, AI recommendation, and an SLA meter.
4. **Underwriters' Hub** — decision-ready file: guideline flags, AUS, conditions checklist,
   GenAI document summaries, final decision.
5. **Approvals** — supervisor inbox for interventions requiring sign-off; approve or reject.
6. **Audit Trail** — immutable, filterable, AI-vs-human tagged.

**Out of scope for MVP** (present in the reference, add later): Application & Intake screen,
Loan Processing screen, Closing & Funding, Post-Closing QC, voice narration / guided tour,
document tiles, service-order tracker, SME chat, loan drawer.

**UI fidelity: the MVP tracks the reference closely, by decision.** It reuses the reference's
21-glyph SVG icon set verbatim, the 64px navy header (coral pill scan button, play/step/speed/reset
icon controls, avatar + role `<select>` + logout), the active-stage gradient banner with pulsing
dot and progress bar, the 248px sidebar with `SidebarItem` metrics and the "Your access" role card,
the 1320px centred content column, and re-implementations of `Card`, `StatCard`, `Badge`, `Tag`,
`Avatar` and `ProgressBar` matching the bundled design system's measurements. When adding a screen,
port the reference's version rather than inventing a layout.

**Non-negotiables** — cutting these guts the demo:
- Auto vs HITL split visible in the same run.
- At least one `requiresSup` case, so the approval lane fires.
- Audit entries for every mutation, AI and human alike.
- A loan that visibly transitions to *Ready for Underwriting* and gets decided.
- **RBAC enforced in the state layer, not just the view.** Hiding a button is not a control.
  Every mutating function re-checks the role before it writes — an underwriter cannot work a
  HITL case, a supervisor cannot render an underwriting decision, and only a supervisor can
  sign off. This carries straight into tool design later: the *tool* refuses, not the UI.
- **Only seed exceptions the demo can actually resolve.** The scan engine walks stages 0–2, so a
  stage-3 (Closing / TRID) exception would sit at `idle` forever and quietly skew the auto-repair
  KPI. Add stage-3 seeds back when the Closing & Funding screen lands.

**MVP tech constraints**
- One file: `mvp/index.html`. No build step, no npm, no CDN scripts — it must open offline from
  the filesystem and survive being emailed to a customer.
- Vanilla JS, single `App` object with `state` + `render()`. No framework. Screens return HTML
  strings; all interaction runs through `data-act` event delegation on `#root`.
- Text inputs write to `state.drafts` on `input` **without** re-rendering, so focus is never stolen.
- Google Fonts via `<link>` only, with real fallback stacks; everything else inline.
- Seed data ~6 loans / ~12 exceptions. Enough to fill the queues, small enough to follow.
- Deterministic. No `Math.random()` in the scan engine — same run every demo.

## Later stages

Reserved. When these land, document them here rather than in scattered files.

- **Agent design** — one section per agent: system prompt, inputs, outputs, escalation rule,
  the confidence threshold that decides `auto` vs `hitl`.
- **Tool design** — tool schemas for LOS read/write, document OCR, AUS submission, vendor
  ordering, rules-engine evaluation. Each tool states its side effects and whether it requires
  human confirmation.
- **Harness** — orchestration loop, queue transport, audit sink, evaluation set and metrics
  (STP rate, auto-repair precision, HITL touch time, cycle-time delta).
