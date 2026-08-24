"""Day-1 Foundry spike.

Run this BEFORE writing anything that depends on a beta feature. It answers
one question: which parts of the Claude API does *your* Foundry deployment
actually accept?

On Microsoft Foundry these are GA:      messages, streaming, tool use
On Microsoft Foundry these are BETA:    structured outputs / strict tools,
                                        adaptive thinking + effort, prompt
                                        caching, memory tool, 1M context
Not available on Foundry at all:        Managed Agents, Message Batches,
                                        Models API, server-side `fallbacks`

Usage:
    cd poc
    python -m venv .venv && .venv/Scripts/activate     # Windows
    pip install -r backend/requirements.txt
    cp .env.example .env                                # then fill it in
    python scripts/spike_foundry.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from anthropic import AnthropicFoundry, APIStatusError, APIConnectionError  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.config import get_settings  # noqa: E402

settings = get_settings()
client = AnthropicFoundry(
    api_key=settings.foundry_api_key,
    **settings.foundry_client_kwargs(),   # resource= OR base_url=, never both
)
MODEL = settings.model_id

PASS, FAIL, WARN = "  PASS ", "  FAIL ", "  WARN "
results: list[tuple[str, bool, str]] = []


def check(name: str, required: bool = False):
    """Decorator that runs one probe and records the verdict."""

    def wrap(fn):
        def run():
            t0 = time.time()
            try:
                detail = fn() or ""
                ms = int((time.time() - t0) * 1000)
                print(f"{PASS} {name}  ({ms} ms)  {detail}")
                results.append((name, True, detail))
            except APIStatusError as e:
                msg = f"HTTP {e.status_code}: {str(e)[:160]}"
                print(f"{FAIL if required else WARN} {name}  {msg}")
                results.append((name, False, msg))
            except APIConnectionError as e:
                print(f"{FAIL} {name}  connection: {e}")
                results.append((name, False, f"connection: {e}"))
            except Exception as e:  # noqa: BLE001 - spike script, report anything
                print(f"{FAIL if required else WARN} {name}  {type(e).__name__}: {e}")
                results.append((name, False, f"{type(e).__name__}: {e}"))

        run.__name__ = fn.__name__
        return run

    return wrap


# --------------------------------------------------------------------------
# 1 — the model resolves and answers at all
# --------------------------------------------------------------------------
@check("messages · basic call", required=True)
def probe_basic():
    r = client.messages.create(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
    )
    if r.stop_reason == "refusal":  # always guard before reading content
        raise RuntimeError(f"refused: {r.stop_details}")
    text = next(b.text for b in r.content if b.type == "text")
    return f"model={r.model!r} reply={text.strip()!r} in={r.usage.input_tokens} out={r.usage.output_tokens}"


# --------------------------------------------------------------------------
# 2 — adaptive thinking + effort (beta on Foundry)
#     NOTE: on Opus 4.7, omitting `thinking` means NO thinking, and
#     `budget_tokens` is removed entirely (400). Must be adaptive.
# --------------------------------------------------------------------------
@check("thinking · adaptive + effort")
def probe_thinking():
    r = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": "What is 17% of 6,520? Answer with the number only."}],
    )
    kinds = {b.type for b in r.content}
    return f"blocks={sorted(kinds)}"


# --------------------------------------------------------------------------
# 3 — structured outputs (beta on Foundry). The preferred way to get findings.
# --------------------------------------------------------------------------
class IncomeCheck(BaseModel):
    monthly_income: float
    confidence: int
    basis: str


@check("structured outputs · messages.parse")
def probe_structured():
    r = client.messages.parse(
        model=MODEL,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        messages=[
            {
                "role": "user",
                "content": "Paystub shows YTD gross $64,200 over 6 months. "
                "Give monthly income, a 0-100 confidence, and the basis.",
            }
        ],
        output_format=IncomeCheck,
    )
    out = r.parsed_output
    return f"parsed={out.monthly_income} conf={out.confidence}"


# --------------------------------------------------------------------------
# 4 — strict tool use (beta on Foundry). Fallback if #3 is unavailable.
# --------------------------------------------------------------------------
@check("strict tool use")
def probe_strict_tool():
    r = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        tools=[
            {
                "name": "raise_exception",
                "description": "Record a loan file exception.",
                "strict": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["Low", "Medium", "High", "Critical"],
                        },
                        "confidence": {"type": "integer"},
                    },
                    "required": ["type", "severity", "confidence"],
                    "additionalProperties": False,
                },
            }
        ],
        messages=[
            {
                "role": "user",
                "content": "FHA loan, back-end DTI is 47% against a 43% cap. "
                "Raise the exception.",
            }
        ],
    )
    calls = [b for b in r.content if b.type == "tool_use"]
    if not calls:
        raise RuntimeError("model returned no tool_use block")
    return f"tool={calls[0].name} input={calls[0].input}"


# --------------------------------------------------------------------------
# 5 — prompt caching (beta on Foundry). The main cost lever: the guideline
#     pack is identical across every loan of a program.
# --------------------------------------------------------------------------
@check("prompt caching")
def probe_caching():
    # Two things this probe learned the hard way:
    #   1. Use an EXPLICIT breakpoint on the system block, not top-level
    #      cache_control. That pins the cache boundary at the end of the stable
    #      prefix, which is exactly where the context pack wants it.
    #   2. A cache write is not instantly readable. Firing two calls back to
    #      back can report a miss on the second even though caching works. Make
    #      three attempts with a short pause before concluding anything.
    guideline = (
        "AGENCY UNDERWRITING GUIDELINE EXTRACT.\n"
        + "Qualifying income must be stable, predictable and likely to continue. "
        "Document with W-2s covering two years plus the most recent 30 days of "
        "paystubs. Where year-to-date earnings diverge from the prior W-2 by more "
        "than five percent, reconcile the variance and document the basis. "
        * 40
    )
    system_blocks = [
        {"type": "text", "text": guideline, "cache_control": {"type": "ephemeral"}}
    ]

    best_read = 0
    for i in range(3):
        r = client.messages.create(
            model=MODEL,
            max_tokens=32,
            system=system_blocks,
            messages=[{"role": "user", "content": f"Reply: {i}"}],
        )
        read = getattr(r.usage, "cache_read_input_tokens", 0) or 0
        best_read = max(best_read, read)
        if best_read:
            break
        time.sleep(1.5)

    if not best_read:
        raise RuntimeError(
            "no cache hit across 3 attempts — caching appears unavailable here"
        )
    return f"cache_read={best_read} tokens (~90% cheaper on the cached prefix)"


# --------------------------------------------------------------------------
# 6 — streaming (GA). Needed for the live agent log.
# --------------------------------------------------------------------------
@check("streaming")
def probe_streaming():
    chunks = 0
    with client.messages.stream(
        model=MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": "Count to five."}],
    ) as stream:
        for _ in stream.text_stream:
            chunks += 1
        final = stream.get_final_message()
    return f"{chunks} text deltas, stop={final.stop_reason}"


# --------------------------------------------------------------------------
# 7 — tool runner (SDK beta helper). Our agent loop.
# --------------------------------------------------------------------------
@check("tool runner loop")
def probe_tool_runner():
    from anthropic import beta_tool

    calls: list[str] = []

    @beta_tool
    def get_loan_amount(loan_id: str) -> str:
        """Return the principal amount for a loan.

        Args:
            loan_id: The loan identifier, e.g. LN-2026-0002.
        """
        calls.append(loan_id)
        return "315000"

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=2048,
        tools=[get_loan_amount],
        messages=[
            {"role": "user", "content": "What is the amount on LN-2026-0002? Use the tool."}
        ],
    )
    turns = 0
    for _msg in runner:
        turns += 1
        if turns > 6:
            break
    return f"{turns} turns, tool called with {calls}"


def main() -> int:
    print("\nFoundry spike")
    print("-" * 72)
    for k, v in settings.safe_summary().items():
        print(f"  {k:20} {v}")
    print("-" * 72)

    for probe in (
        probe_basic,
        probe_thinking,
        probe_structured,
        probe_strict_tool,
        probe_caching,
        probe_streaming,
        probe_tool_runner,
    ):
        probe()

    print("-" * 72)
    ok = sum(1 for _, passed, _ in results if passed)
    print(f"  {ok}/{len(results)} probes passed\n")

    failed = [n for n, passed, _ in results if not passed]
    if failed:
        print("  Unavailable here — use the documented fallback for each:")
        for name in failed:
            print(f"    · {name}")
        print(
            "\n  structured outputs -> fall back to a strict `raise_exception` tool\n"
            "  prompt caching     -> still works, just costs more; shrink the guideline pack\n"
            "  tool runner        -> fall back to a manual while-loop over stop_reason\n"
        )
    # Probe failures are information, not an error — always exit 0.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
