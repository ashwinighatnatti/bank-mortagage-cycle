"""Market constants for synthetic data generation.

Every value is either RESEARCHED (with a source and an as-of date) or marked
ESTIMATED. Nothing here is invented silently — a mortgage professional looking
at the demo will check the loan limits first, and being wrong there costs more
credibility than everything else in the build combined.

Researched 2026-08-20. Re-verify the CLL and FHA blocks each January; they
change on 1 January every year and stale limits are the most visible possible
error in a mortgage demo.
"""

from __future__ import annotations

from dataclasses import dataclass

AS_OF = "2026-08-20"

# ---------------------------------------------------------------------------
# Conforming loan limits — RESEARCHED
# Source: FHFA, "Conforming Loan Limit Values for 2026", effective 2026-01-01.
# Baseline rose 3.26% from 2025's $806,500.
# ---------------------------------------------------------------------------
CONFORMING_BASELINE = 832_750
CONFORMING_CEILING = 1_249_125          # high-cost areas, 150% of baseline
CONFORMING_SPECIAL_AREAS = 1_249_125    # AK, HI, GU, VI baseline

# County limits above baseline — RESEARCHED for the metros used in the book.
# Any county not listed uses CONFORMING_BASELINE.
COUNTY_CONFORMING_LIMIT: dict[str, int] = {
    "San Diego, CA": 1_104_100,
    "Boston, MA": 920_500,
    "Seattle, WA": 1_069_350,
}

# ---------------------------------------------------------------------------
# FHA limits — RESEARCHED
# Source: HUD-25-145, FHA CY2026 forward mortgage limits, effective for case
# numbers assigned on or after 2026-01-01. Floor = 65% of the CLL baseline.
# ---------------------------------------------------------------------------
FHA_FLOOR = 541_287
FHA_CEILING = 1_249_125

# ---------------------------------------------------------------------------
# Rates — RESEARCHED
# Source: Freddie Mac PMMS, week of 2026-08-13.
# FHA/VA note rates typically price 25-50 bps below conventional; the spreads
# below are ESTIMATED from that convention, not separately surveyed.
# ---------------------------------------------------------------------------
RATE_30Y_FIXED = 6.67
RATE_15Y_FIXED = 5.96
RATE_FHA_30Y = 6.29          # ESTIMATED: conventional less ~38 bps
RATE_VA_30Y = 6.21           # ESTIMATED: conventional less ~46 bps
RATE_JUMBO_30Y = 6.74        # ESTIMATED: conventional plus ~7 bps

# ---------------------------------------------------------------------------
# Mortgage insurance and funding fees — RESEARCHED
# FHA: 1.75% upfront MIP; 0.55% annual for the life of the loan on most loans.
# VA: 2.15% funding fee, first use, zero down.
# ---------------------------------------------------------------------------
FHA_UPFRONT_MIP = 0.0175
FHA_ANNUAL_MIP = 0.0055
VA_FUNDING_FEE_FIRST_USE = 0.0215
VA_FUNDING_FEE_SUBSEQUENT = 0.033

# ---------------------------------------------------------------------------
# Median home prices — MIXED
# National and Austin are RESEARCHED (Redfin June 2026 national median
# $408,776; Austin $414,950 Feb 2026). The remainder are ESTIMATED from
# well-known relative metro positioning, anchored to the national figure.
# They only need to be plausible enough that a loan amount is not absurd
# against its property.
# ---------------------------------------------------------------------------
NATIONAL_MEDIAN_PRICE = 408_776          # RESEARCHED

METRO_MEDIAN_PRICE: dict[str, int] = {
    "Austin, TX": 414_950,               # RESEARCHED
    "Phoenix, AZ": 445_000,              # ESTIMATED
    "San Diego, CA": 1_010_000,          # ESTIMATED
    "Tampa, FL": 385_000,                # ESTIMATED
    "Denver, CO": 585_000,               # ESTIMATED
    "Seattle, WA": 815_000,              # ESTIMATED
    "Atlanta, GA": 395_000,              # ESTIMATED
    "Boston, MA": 760_000,               # ESTIMATED
    "San Antonio, TX": 295_000,          # ESTIMATED
    "Portland, OR": 545_000,             # ESTIMATED
    "Charlotte, NC": 405_000,            # ESTIMATED
    "Las Vegas, NV": 460_000,            # ESTIMATED
}

# ---------------------------------------------------------------------------
# Program limits used by the rules engine — from agency guidelines
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProgramLimits:
    dti_cap: float
    ltv_cap: float
    fico_floor: int
    label: str


PROGRAM_LIMITS: dict[str, ProgramLimits] = {
    "Conv": ProgramLimits(50.0, 95.0, 620, "Fannie Mae / Freddie Mac conforming"),
    "FHA": ProgramLimits(43.0, 96.5, 580, "FHA Title II forward"),
    "VA": ProgramLimits(50.0, 100.0, 620, "VA guaranteed"),
    "Jumbo": ProgramLimits(43.0, 80.0, 700, "Non-agency jumbo, typical overlay"),
}

CU_SCORE_THRESHOLD = 2.5      # Collateral Underwriter; above this flags valuation risk


def conforming_limit(metro: str) -> int:
    """The applicable one-unit conforming limit for a metro."""
    return COUNTY_CONFORMING_LIMIT.get(metro, CONFORMING_BASELINE)


def is_jumbo(amount: int, metro: str) -> bool:
    """A loan is jumbo when it exceeds the county's conforming limit.

    This is the check the reference design would fail: at 2026 limits, its
    'Jumbo' loans of $625,000 and $720,000 are comfortably conforming
    everywhere in the country.
    """
    return amount > conforming_limit(metro)


def note_rate(program: str) -> float:
    return {
        "Conv": RATE_30Y_FIXED,
        "FHA": RATE_FHA_30Y,
        "VA": RATE_VA_30Y,
        "Jumbo": RATE_JUMBO_30Y,
    }[program]


def monthly_pi(principal: int, annual_rate_pct: float, years: int = 30) -> float:
    """Standard amortising payment. Used to make DTI arithmetic self-consistent."""
    r = annual_rate_pct / 100 / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


PROVENANCE = {
    "researched": [
        ("Conforming loan limits 2026", "FHFA, effective 2026-01-01"),
        ("FHA floor / ceiling 2026", "HUD-25-145, effective 2026-01-01"),
        ("County limits: San Diego, Boston, Seattle", "FHFA county tables 2026"),
        ("30y / 15y fixed rates", "Freddie Mac PMMS, week of 2026-08-13"),
        ("FHA MIP, VA funding fee", "HUD / VA published 2026 schedules"),
        ("National median price", "Redfin, June 2026"),
        ("Austin median price", "Austin market report, Feb 2026"),
    ],
    "estimated": [
        ("FHA / VA / Jumbo note rates", "conventional +/- conventional spread"),
        ("Metro median prices except Austin", "relative positioning vs national median"),
    ],
    "as_of": AS_OF,
}
