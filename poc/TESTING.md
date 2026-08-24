# Testing guide

A POC of an AI-native mortgage origination pipeline. Four Claude agents read a
loan file, raise findings, repair what they are allowed to repair, and hand the
rest to people. You are testing whether that is useful and whether the human
parts of it make sense.

Please read **"Things that are refused on purpose"** before logging anything.
Most of what looks broken in this system is the system working.

---

## Getting in

```
http://127.0.0.1:8000
```

Password for every account: `Coforge@123`

| sign in as | who | role | sees |
|---|---|---|---|
| `analyst1` | Priya Nair | analyst | queue A |
| `analyst2` | Arjun Mehta | analyst | queue B |
| `analyst3` | Lena Rossi | analyst | queue C |
| `supervisor` | Marcus Webb | supervisor | approvals, and can start scans |
| `underwriter` | Diane Foster | underwriter | the Hub, and decides loans |

The dropdown in the top-right switches persona without signing out. It is a
real re-login — a new token and a different set of permissions — so use it
freely.

---

## Please read this before you scan anything

**Every scan calls a real Claude model and costs real money**, roughly $0.50 to
$0.60 for a loan. Ceilings are set so a stuck run stops rather than spending
freely: $0.60 per agent, four agents, so about $2.40 per loan worst case.

Scan a few loans, not all twelve, unless someone asks you to.

A scan takes about two minutes. Watch the live log on the right of the Loan
Pipeline screen while it runs.

---

## A walkthrough that covers everything

Sign in as **supervisor** and work down this list. It takes about fifteen
minutes and touches every screen.

**1. Dashboard.** Get a feel for the book — twelve loans, how many are scanned,
what has been found. The numbers are computed from the underlying rows on every
request, so they cannot disagree with the detail screens.

**2. Loan Pipeline → pick a loan → Run agents.** This is the centre of the
demo. Watch the log: green lines are tool calls that succeeded, red DENIED lines
are the system refusing an agent something. Both are expected.

**3. AI Exceptions & HITL.** Each card shows what was found, how confident the
agent was, the evidence it quoted, and — importantly — *why it went where it
went*. An agent proposes; Python decides whether a person sees it.

**4. Work a case.** Switch to whichever analyst owns the queue, pick a finding,
choose an action, resolve it. Some findings you can close yourself; some you can
only *propose* a fix for.

**5. Approvals** (supervisor only). Two different things live here:

- *Analyst proposals* — someone proposed a fix to a judgment call and needs
  sign-off.
- *Gated agent actions* — an agent tried to spend money or contact a borrower
  and was stopped. Authorising one does not run it; it records permission, and
  the agent performs it on its next run.

**6. Underwriters' Hub.** Switch to **underwriter**. Guideline flags, the rules
engine, conditions, and the decision. A loan can only be decided once every
blocking finding is closed.

**7. Audit Trail.** Every action, AI and human, with a "chain intact" check at
the top. The server re-hashes every row on each request — it is not reporting a
stored flag.

---

## Things that are refused on purpose

These are controls, not defects. Please do not log them as bugs — but *do* tell
us if the wording of a refusal is confusing, because that is worth fixing.

| what you will see | why |
|---|---|
| An analyst cannot open **Approvals** | Only supervisors sign off |
| A **supervisor cannot decide a loan** | Approving a fix and underwriting a loan are different jobs |
| An **underwriter cannot work a HITL case** | They would be clearing their own blockers |
| An analyst cannot act on **another analyst's queue** | They can read it; they cannot act on it |
| You **cannot sign off your own proposal** | Sign-off is a second pair of eyes |
| A loan **cannot be decided until it is ready** | Blocking findings must be closed first |
| An exception **cannot be proposed on twice** | One live proposal at a time; a rejection reopens it |
| A **decision cannot be changed** once recorded | It is a record, not a setting |
| Agents get **DENIED** in the live log | An agent asking for something it does not hold is the control working |
| A money-spending agent action **does not happen immediately** | It waits for a supervisor |

**High confidence does not mean automatic.** A finding at 98% confidence can
still go to a human, because some things are judgment calls regardless of how
sure the model is — a DTI breach, an identity mismatch, a title problem. The
card tells you which rule applied.

**"Not checked" is not "passed."** The rules engine reports three outcomes, and
the third one matters: if a document was unreadable or absent, the rule says so
rather than quietly passing. If you see *Not checked* on the Hub, that is
honest, not broken.

---

## Known gaps — already on the list

Please do not spend time on these; they are understood.

- **Some defects are not found.** Recall was measured at 63% on the previous
  book. Six of the seven misses were in areas with **no rule** — AUS results,
  document expiry dates, title exceptions, credit inquiries. If you plant one of
  those in your head and it goes unfound, that is why.
- **No Docker/deployment yet.** Deliberately held until after this round.
- **Nobody has visually reviewed the UI.** It was built without a browser
  available, so layout problems are entirely plausible. Screenshots very welcome.
- **All accounts share one password**, and the live-log endpoint passes its
  token in the URL. Demo-grade, known, not for anywhere real.
- **The loan data is synthetic**, generated with planted defects so accuracy can
  be measured. Borrowers, employers and figures are invented.

---

## What is most useful to report

In rough order:

1. **Anything that looks wrong in the browser** — layout, overflow, text that
   collides or gets cut off. This is the least-tested part of the whole build.
2. **A finding that is wrong** — the agent claimed something the document does
   not say. Include the loan id and the exception id.
3. **A finding that went to the wrong place** — auto-repaired when a person
   should have seen it, or vice versa.
4. **Wording that does not make sense** to someone who works in mortgage —
   refusals, recommendations, the underwriter summary.
5. **Anything you expected to be able to do and could not**, even if the table
   above explains it. If the explanation is not visible at the moment you are
   blocked, that is a design problem.

For anything on a specific loan, the **loan id** and **exception id** are enough
for us to reconstruct exactly what happened — every action is in the audit
trail.

---

## If it stops working

- **Page will not load** — the server is not running. From `poc/backend`:
  `python -m uvicorn app.api:app --host 127.0.0.1 --port 8000`
- **A scan does nothing** — only a supervisor can start one.
- **Everything looks empty** — the database may have been reset. Re-seed with
  `python scripts/init_db.py` from `poc/`, then scan a loan.
