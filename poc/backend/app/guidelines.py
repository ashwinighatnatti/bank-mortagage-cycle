"""The guideline corpus, and the cacheable context pack built from it.

TWO JOBS, ONE SOURCE.

`lookup_guideline` serves single passages on demand. `build_context_pack()`
concatenates the passages a program actually needs into the stable prefix of
every request for that program, marked with a cache breakpoint.

WHY THE PACK IS BUILT PER PROGRAM AND NOT PER LOAN. Prompt caching only pays
when the prefix is byte-identical across requests. Every FHA loan gets exactly
the same pack, so the second FHA loan of a run reads it from cache instead of
paying for it — roughly a 90% saving on the largest block in the request. Put
one loan-specific detail in here and the saving disappears silently: nothing
errors, the bill just stops going down. Loan-specific context belongs in the
`messages`, after the breakpoint.

Every number that appears in a passage is interpolated from `market_data`
rather than typed in. A guideline that says 43% while `PROGRAM_LIMITS` says 45%
would have the model and the rules engine enforcing different rules, and the
model's version is the one the analyst reads in the rationale.
"""

from __future__ import annotations

from dataclasses import dataclass

from .market_data import (
    AS_OF,
    CONFORMING_BASELINE,
    CU_SCORE_THRESHOLD,
    FHA_ANNUAL_MIP,
    FHA_FLOOR,
    FHA_UPFRONT_MIP,
    PROGRAM_LIMITS,
    VA_FUNDING_FEE_FIRST_USE,
)

# Documentary sources, cited in each passage so a rationale can name one.
SOURCES = {
    "fnma": "Fannie Mae Selling Guide B3",
    "fhlmc": "Freddie Mac Seller/Servicer Guide 5300",
    "fha": "HUD Handbook 4000.1 II.A",
    "va": "VA Lenders Handbook M26-7 Ch.4",
    "trid": "12 CFR 1026.19(e)/(f)",
    "flood": "42 USC 4012a / FEMA SFHDF",
    "cip": "31 CFR 1020.220",
    "fhfa": "FHFA Conforming Loan Limit Values 2026",
}


@dataclass(frozen=True, slots=True)
class Passage:
    topic: str
    title: str
    body: str
    source: str

    def render(self) -> str:
        return f"### {self.title}\n[{self.source}]\n{self.body.strip()}\n"


def _p(topic: str, title: str, source_key: str, body: str) -> Passage:
    return Passage(topic, title, body, SOURCES[source_key])


# ---------------------------------------------------------------------------
# Topics that apply to every program
# ---------------------------------------------------------------------------
def _common() -> dict[str, Passage]:
    return {
        "income": _p(
            "income", "Qualifying income — stability and continuity", "fnma",
            """
Qualifying income must be stable, predictable and reasonably likely to continue
for at least three years. Document salaried borrowers with the two most recent
W-2s and a paystub covering the most recent 30 days showing year-to-date
earnings.

Where year-to-date earnings annualise more than 5% above or below the prior-year
W-2, the variance must be reconciled and the qualifying basis documented. An
increase is not automatically usable: a year-to-date figure inflated by a
non-recurring bonus, overtime or commission is averaged over 24 months, not
annualised from the current period. A decline is treated as the qualifying
figure unless the employer confirms in writing that the reduction was temporary.

Variable income — bonus, overtime, commission, tips — requires a two-year
history with the employer confirming continuance. Average it over 24 months. If
the trend is declining, use the lower recent figure rather than the average.

Self-employed borrowers require two years of signed returns, a year-to-date
profit and loss statement, and a business liquidity assessment where funds are
drawn from the business. A 4506-C transcript is required where the file relies
on tax return income.

An income variance is a reconciliation judgment, not an arithmetic error. It is
never dispositioned automatically regardless of how clear the numbers look.
""",
        ),
        "assets": _p(
            "assets", "Asset sourcing, seasoning and reserves", "fhlmc",
            """
Funds to close must be sourced and seasoned. Two consecutive monthly statements
covering 60 days are required for each account used to qualify.

Any single deposit exceeding 50% of the borrower's total monthly qualifying
income must be documented as to source. Acceptable evidence includes a payroll
record matching the amount, a documented asset sale with a bill of sale and the
corresponding withdrawal, a gift letter with a donor bank trail, or a settlement
statement from a prior sale. A borrower letter of explanation alone is not
sourcing.

Undocumented large deposits are excluded from qualifying funds. They are not a
denial in themselves — they are excluded, and the file is re-tested for
sufficiency without them.

Reserves are measured in months of PITIA after closing. Cash-out proceeds may
not count toward required reserves. Retirement funds count at 60% of vested
value net of any outstanding loan against them.
""",
        ),
        "collateral": _p(
            "collateral", "Collateral valuation and appraisal review", "fnma",
            f"""
The lesser of the appraised value and the contract price establishes LTV on a
purchase. An appraisal supporting less than the contract price does not reduce
the price — it reduces the loan, and the gap becomes the borrower's cash.

A Collateral Underwriter score above {CU_SCORE_THRESHOLD} indicates elevated
valuation risk and requires review. Escalation options are a desk or field
review, or a rebuttal with the appraiser supported by comparable sales the
original report did not consider. Ordering a second full appraisal to obtain a
higher number is value shopping and is not permitted.

An appraised value 3% or more below the contract price is a material variance.
It changes the transaction rather than the file, so it is an underwriter
decision and is never dispositioned automatically.

Appraisals are valid for 120 days for existing construction. Beyond that an
update is required; beyond 240 days a new appraisal is required.
""",
        ),
        "flood": _p(
            "flood", "Flood determination and insurance", "flood",
            """
Every file requires a current Standard Flood Hazard Determination on the FEMA
form. A determination is not optional and cannot be waived by the borrower.

Where the determination places the property in a Special Flood Hazard Area —
zones A, AE, AH, AO, AR, A1-A30, V or VE — flood insurance is mandatory for the
life of the loan. Coverage must equal the lesser of the outstanding principal,
the insurable value of the improvements, or the NFIP maximum. Zones B, C and X
are outside the SFHA and require no coverage.

A missing determination is a procedural gap: order it. A determination that
disagrees with a prior determination on the same property is a data-quality
question, and re-pulling from the authoritative source resolves the majority of
them — order the re-pull before escalating. The two are different findings with
different remedies and must not be collapsed into one.

Where a Letter of Map Amendment or Revision has removed the property from the
SFHA, the LOMA/LOMR must be in the file; the determination alone is not enough.
""",
        ),
        "identity": _p(
            "identity", "Customer Identification Program", "cip",
            """
The Customer Identification Program requires the lender to verify the identity
of each borrower and to hold, at minimum, name, date of birth, address and
taxpayer identification number.

Every identifying element must agree across the application, the government
identification and the credit file. A Social Security Number that differs
between the application and any document in the file is a Critical finding and
stops the file. Resolution requires an SSA-89 verification and a documented CIP
review; it is never a typographical correction made by whoever noticed it, and
it is never resolved without a person signing off.

Transposed digits are the most common cause and the least safe assumption. The
number is verified against the issuing authority, not reasoned about.

A borrower matching an OFAC list entry stops the file immediately and is
escalated outside the normal exception process.
""",
        ),
        "trid": _p(
            "trid", "TRID disclosure timing and fee tolerance", "trid",
            """
The Loan Estimate is delivered within three business days of application. The
Closing Disclosure is received by the borrower at least three business days
before consummation.

Fee movement between the LE and the CD is tested against three buckets. Zero
tolerance: lender charges, transfer taxes, and charges for services the borrower
could not shop for — these may not increase at all. Ten percent cumulative:
recording fees and charges for third-party services from the lender's written
list of providers — these may rise 10% in aggregate, not individually. No
tolerance: prepaid interest, property insurance premiums, escrow deposits, and
services the borrower shopped for outside the list.

An increase beyond tolerance requires a cure — a lender credit for the excess,
delivered with a corrected CD, within 60 days of consummation.

A valid changed circumstance resets the baseline, but only if it is documented
and only if a revised LE is delivered within three business days of the event.
An undocumented reset is a violation regardless of the underlying reason.
""",
        ),
    }


# ---------------------------------------------------------------------------
# Program-specific topics
# ---------------------------------------------------------------------------
def _program_passages(program: str) -> dict[str, Passage]:
    limits = PROGRAM_LIMITS[program]

    capacity = _p(
        "capacity", f"{program} — debt-to-income and loan-to-value limits",
        {"Conv": "fnma", "FHA": "fha", "VA": "va", "Jumbo": "fhlmc"}[program],
        f"""
Program: {limits.label}.
Back-end DTI cap {limits.dti_cap:.1f}%. Maximum LTV {limits.ltv_cap:.1f}%.
Minimum representative FICO {limits.fico_floor}.

Back-end DTI is total monthly obligations divided by gross monthly qualifying
income. Obligations include the proposed PITIA — principal, interest, taxes,
insurance, association dues and any mortgage insurance — plus all instalment
debt with more than ten payments remaining, revolving minimum payments, and
court-ordered obligations such as alimony or child support.

The ratio is computed from the documented figures, never read from the
application. A stated ratio that disagrees with the documented figures is itself
a finding, and the documented figures govern.

Exceeding the cap is not automatically a denial. It requires documented
compensating factors — reserves beyond the minimum, a materially lower payment
shock, a long stable employment history — and an underwriter decision. It is
never dispositioned automatically.
""",
    )

    eligibility_bodies = {
        "Conv": f"""
Conforming loan limits are set annually by FHFA and take effect 1 January.
For 2026 the one-unit baseline is ${CONFORMING_BASELINE:,}, with
high-cost county limits up to 150% of the baseline. A loan above the applicable
county limit is not conforming and cannot be delivered to the agencies
regardless of how the file is labelled.

Private mortgage insurance is required above 80% LTV and is cancellable at 80%
on borrower request with a current value, and automatically at 78% by
amortisation.

Standard eligibility requires an Approve/Eligible from Desktop Underwriter or an
Accept from Loan Product Advisor. A Refer result requires manual underwriting
against the manual guidelines, which are more restrictive than the automated
ones — a Refer is not a softer Approve.
""",
        "FHA": f"""
FHA forward mortgage limits are set annually by HUD and take effect for case
numbers assigned on or after 1 January. The 2026 floor is ${FHA_FLOOR:,},
calculated as 65% of the conforming baseline, with high-cost ceilings above it.

Upfront mortgage insurance is {FHA_UPFRONT_MIP * 100:.2f}% of the base loan
amount and may be financed. Annual MIP is {FHA_ANNUAL_MIP * 100:.2f}% for most
terms and LTVs, and remains for the life of the loan where LTV at origination
exceeded 90%.

An FHA case number must be assigned and in the file before endorsement. TOTAL
Scorecard is the automated engine; a Refer routes to manual underwriting, where
the DTI cap tightens and compensating factors must be documented explicitly.

Identity-of-interest and non-arm's-length transactions carry additional LTV
restrictions.
""",
        "VA": f"""
VA guaranteed loans require a valid Certificate of Eligibility in the file. The
COE establishes entitlement and is not substituted by a DD-214 alone.

The funding fee is {VA_FUNDING_FEE_FIRST_USE * 100:.2f}% on a first-use zero-down
purchase and may be financed. It is waived for borrowers receiving VA
compensation for a service-connected disability, and the exemption must be
evidenced in the file.

VA permits 100% financing and requires no monthly mortgage insurance. In
exchange, residual income — the balance remaining after all obligations, by
family size and region — is a hard requirement, not a compensating factor. A
file passing DTI but failing residual income does not qualify.

Appraisals are ordered through the VA portal and produce a Notice of Value.
Minimum Property Requirements apply and are not waivable by the borrower.
""",
        "Jumbo": f"""
A jumbo loan exceeds the applicable county conforming limit and is not agency
deliverable, so eligibility is governed by investor overlays rather than agency
guidelines. Overlays are more restrictive and vary by investor; the file must
satisfy the specific investor's matrix, not a generic jumbo standard.

Typical overlays: two appraisals above a stated loan amount, 12 months of PITIA
reserves at higher LTVs, full documentation with no reduced-documentation
options, and a minimum representative FICO of {limits.fico_floor}.

Verify at application that the loan genuinely exceeds the county limit. A loan
booked as jumbo that is within the limit is priced and underwritten more
restrictively than it needs to be, and the borrower pays for the error.
""",
    }

    eligibility = _p(
        "eligibility", f"{program} — program eligibility and loan limits",
        {"Conv": "fhfa", "FHA": "fha", "VA": "va", "Jumbo": "fhlmc"}[program],
        eligibility_bodies[program],
    )

    aus = _p(
        "aus", f"{program} — automated underwriting results",
        {"Conv": "fnma", "FHA": "fha", "VA": "va", "Jumbo": "fhlmc"}[program],
        """
The automated result is the file's eligibility basis and its findings report
lists the documentation the file must contain. Delivering a file that does not
match its own findings report is a repurchase risk.

Approve/Eligible and Accept mean the file may proceed on the documentation the
report specifies. Refer, Refer/Eligible and Caution mean the automated engine
declined to approve and the file must be manually underwritten against the
manual guidelines — which are more restrictive, not equivalent.

Re-running the engine after changing an input is legitimate only when the input
changed for a documented reason. Re-running to obtain a different answer, with
the same underlying facts, is not.
""",
    )

    return {p.topic: p for p in (capacity, eligibility, aus)}


def passages_for(program: str) -> dict[str, Passage]:
    """Every passage that applies to a program, keyed by topic."""
    if program not in PROGRAM_LIMITS:
        raise KeyError(f"unknown program {program!r}; known: {', '.join(PROGRAM_LIMITS)}")
    return {**_common(), **_program_passages(program)}


TOPICS = ("capacity", "eligibility", "income", "assets", "collateral",
          "flood", "identity", "trid", "aus")


def lookup(program: str, topic: str) -> Passage | None:
    """One passage, or None. `lookup_guideline` turns None into a listing."""
    return passages_for(program).get(topic)


def build_context_pack(program: str) -> str:
    """The stable, cacheable prefix for every request about this program.

    Byte-identical for every loan of the same program — that identity is the
    whole cache saving, and it is why nothing loan-specific may be added here.
    """
    parts = [
        f"AGENCY UNDERWRITING GUIDELINE EXTRACT — {PROGRAM_LIMITS[program].label}",
        f"Program: {program}. Guideline data current as of {AS_OF}.",
        "",
        "These passages are the authority for this file. Where a document in the "
        "loan contradicts a passage below, the passage governs and the "
        "contradiction is itself a finding.",
        "",
    ]
    passages = passages_for(program)
    for topic in TOPICS:
        if topic in passages:
            parts.append(passages[topic].render())
    return "\n".join(parts)
