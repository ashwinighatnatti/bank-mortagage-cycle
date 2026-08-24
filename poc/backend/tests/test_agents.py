"""Tests for the agent loop, with a scripted client and no network.

Everything here runs against a fake that returns pre-written responses. That is
deliberate: the loop's job is to be correct about turn structure, tool
dispatch, budget and cache placement, and none of those need a real model to
verify. Pointing the suite at Foundry would make it slow, non-deterministic and
expensive, and would test Anthropic's inference rather than our loop.

What a real model is needed for is whether the *prompts* work. That is what
scripts/run_agents.py is for, and it is not a unit test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlmodel import select

from app import agents, store, tools
from app.models import (
    ApprovalStatus,
    Confirmation,
    ExceptionRecord,
    ExceptionStatus,
    Loan,
    Run,
    ToolCall,
)
from app.policy import RunBudget


# ---------------------------------------------------------------------------
# A scripted client
# ---------------------------------------------------------------------------
def text_block(s: str):
    return SimpleNamespace(type="text", text=s)


def thinking_block(s: str = "considering the file"):
    return SimpleNamespace(type="thinking", thinking=s)


def tool_block(name: str, tool_input: dict, block_id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def response(content, stop_reason="tool_use", input_tokens=1000, output_tokens=200,
             cache_read=0):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
        ),
    )


class ScriptedClient:
    """Returns queued responses and records every request it was given."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.queue:
            return response([text_block("done")], stop_reason="end_turn")
        return self.queue.pop(0)


DONE = "end_turn"


@pytest.fixture()
def scripted(loan):
    """A client that says something and stops."""
    return ScriptedClient(response([text_block("Nothing to do.")], stop_reason=DONE))


def run(session, client, agent="validation", loan_id="LN-TEST-0001", **kw):
    return agents.run_agent(
        session, agent=agent, loan_id=loan_id, run_id=kw.pop("run_id", "RUN-A1"),
        client=client, **kw,
    )


# ---------------------------------------------------------------------------
# Request construction — where the money is
# ---------------------------------------------------------------------------
def test_the_cache_breakpoint_is_on_the_last_stable_block(loan):
    blocks = agents.build_system("validation", "Conv")
    assert len(blocks) == 2
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}


def test_system_blocks_contain_nothing_loan_specific(loan):
    """One loan id up here would silently stop the cache ever hitting."""
    blocks = agents.build_system("validation", "Conv")
    blob = blocks[0]["text"] + blocks[1]["text"]
    assert loan.id not in blob
    assert loan.borrowers not in blob
    assert "412,000" not in blob and "412000" not in blob


def test_two_loans_of_the_same_program_get_byte_identical_system_blocks():
    """Byte-identical is the entire cache saving; near-identical saves nothing."""
    a = agents.build_system("validation", "FHA")
    b = agents.build_system("validation", "FHA")
    assert a == b
    assert a != agents.build_system("validation", "Conv")


def test_the_loan_id_is_in_the_user_message_where_it_belongs(session, scripted, loan):
    run(session, scripted)
    req = scripted.requests[0]
    assert loan.id in req["messages"][0]["content"]


def test_thinking_is_adaptive_and_effort_is_set(session, scripted, loan):
    """Omitting `thinking` on Opus 4.7 means no thinking at all."""
    run(session, scripted)
    req = scripted.requests[0]
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"]["effort"] == "high"
    assert "budget_tokens" not in str(req["thinking"])


def test_an_agent_is_sent_only_the_tools_it_holds(session, scripted, loan):
    run(session, scripted, agent="summarizer")
    sent = {t["name"] for t in scripted.requests[0]["tools"]}
    assert sent == {t["name"] for t in tools.tool_schemas_for("summarizer")}
    assert "raise_exception" not in sent
    assert "apply_auto_repair" not in sent


def test_tools_are_sent_in_a_stable_order(session, loan):
    """Reordering the tool list invalidates the cache on every later request."""
    first = ScriptedClient(response([text_block("a")], stop_reason=DONE))
    second = ScriptedClient(response([text_block("a")], stop_reason=DONE))
    run(session, first, run_id="RUN-O1")
    run(session, second, run_id="RUN-O2")
    assert [t["name"] for t in first.requests[0]["tools"]] == \
           [t["name"] for t in second.requests[0]["tools"]]


def test_unknown_agent_is_rejected(session, scripted, loan):
    with pytest.raises(KeyError, match="unknown agent"):
        run(session, scripted, agent="supervisor_agent")


# ---------------------------------------------------------------------------
# Turn mechanics
# ---------------------------------------------------------------------------
def test_a_tool_call_is_dispatched_and_the_result_fed_back(session, loan, docs_on_disk):
    client = ScriptedClient(
        response([tool_block("list_documents", {"loan_id": loan.id})]),
        response([text_block("Nine documents on file.")], stop_reason=DONE),
    )
    result = run(session, client)

    assert result.stopped == "end_turn"
    assert result.turns == 2
    assert result.text == "Nine documents on file."

    follow_up = client.requests[1]["messages"]
    assert follow_up[1]["role"] == "assistant"
    assert follow_up[2]["role"] == "user"
    assert follow_up[2]["content"][0]["type"] == "tool_result"
    assert follow_up[2]["content"][0]["is_error"] is False


def test_parallel_tool_calls_come_back_in_one_user_message(session, loan, docs_on_disk):
    """Splitting them across messages trains the model out of parallel calls."""
    client = ScriptedClient(
        response([
            tool_block("list_documents", {"loan_id": loan.id}, "tu_a"),
            tool_block("get_loan", {"loan_id": loan.id}, "tu_b"),
        ]),
        response([text_block("ok")], stop_reason=DONE),
    )
    run(session, client)

    user_msgs = [m for m in client.requests[1]["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 2                       # the task, then one results message
    assert len(user_msgs[1]["content"]) == 2         # both results together
    assert {b["tool_use_id"] for b in user_msgs[1]["content"]} == {"tu_a", "tu_b"}


def test_assistant_content_is_echoed_back_unchanged_including_thinking(
    session, loan, docs_on_disk
):
    """Thinking blocks must go back as they came, on the same model."""
    blocks = [thinking_block(), tool_block("get_loan", {"loan_id": loan.id})]
    client = ScriptedClient(
        response(blocks),
        response([text_block("ok")], stop_reason=DONE),
    )
    run(session, client)

    echoed = client.requests[1]["messages"][1]
    assert echoed["role"] == "assistant"
    assert echoed["content"] is blocks


def test_a_denied_call_is_returned_as_an_error_and_the_loop_continues(session, loan):
    """The validation agent holds no repair tool. It should learn that and move on."""
    client = ScriptedClient(
        response([tool_block("apply_auto_repair",
                             {"loan_id": loan.id, "exception_id": "EX-1", "action": "x"})]),
        response([text_block("Understood — that is not mine to do.")], stop_reason=DONE),
    )
    result = run(session, client)

    block = client.requests[1]["messages"][2]["content"][0]
    assert block["is_error"] is True
    assert "does not hold this capability" in block["content"]
    assert result.stopped == "end_turn"
    assert any(e.kind == "denied" for e in result.events)

    recorded = session.exec(select(ToolCall)).all()
    assert len(recorded) == 1 and recorded[0].ok is False


def test_max_turns_terminates_a_loop_that_will_not_stop(session, loan, docs_on_disk):
    client = ScriptedClient(*[
        response([tool_block("list_documents", {"loan_id": loan.id}, f"tu_{i}")])
        for i in range(20)
    ])
    result = run(session, client, max_turns=4)
    assert result.stopped == "max_turns"
    assert result.turns == 4


def test_hitting_max_tokens_stops_rather_than_looping(session, loan):
    client = ScriptedClient(response([text_block("truncated...")], stop_reason="max_tokens"))
    result = run(session, client)
    assert result.stopped == "max_tokens"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------
def test_the_budget_stops_the_loop_and_the_run_is_marked(session, loan, docs_on_disk):
    client = ScriptedClient(*[
        response([tool_block("list_documents", {"loan_id": loan.id}, f"tu_{i}")],
                 input_tokens=50_000, output_tokens=5_000)
        for i in range(10)
    ])
    budget = RunBudget(max_tool_calls=99, max_tokens=60_000, max_seconds=600, max_usd=99.0)
    result = run(session, client, budget=budget)

    assert result.stopped == "budget"
    assert "tokens" in result.error
    assert session.get(Run, "RUN-A1").status == "budget_exceeded"


def test_spend_is_recorded_every_turn_not_only_on_a_clean_exit(session, loan, docs_on_disk):
    """A run that dies expensively must not be the one with no cost recorded."""
    client = ScriptedClient(*[
        response([tool_block("list_documents", {"loan_id": loan.id}, f"tu_{i}")],
                 input_tokens=40_000, output_tokens=4_000)
        for i in range(10)
    ])
    budget = RunBudget(max_tool_calls=99, max_tokens=90_000, max_seconds=600, max_usd=99.0)
    result = run(session, client, budget=budget)

    row = session.get(Run, "RUN-A1")
    assert result.stopped == "budget"
    assert row.input_tokens > 0 and row.output_tokens > 0
    assert row.usd > 0


def test_cache_reads_are_accumulated_onto_the_run(session, loan, docs_on_disk):
    client = ScriptedClient(
        response([tool_block("get_loan", {"loan_id": loan.id})], cache_read=3_591),
        response([text_block("ok")], stop_reason=DONE, cache_read=3_591),
    )
    run(session, client)
    assert session.get(Run, "RUN-A1").cache_read_tokens == 7_182


def test_a_client_failure_ends_the_run_without_killing_the_caller(session, loan):
    class Broken:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._boom)

        def _boom(self, **kwargs):
            raise RuntimeError("connection reset")

    result = run(session, Broken())
    assert result.stopped == "error"
    assert "connection reset" in result.error
    assert session.get(Run, "RUN-A1").status == "error"


# ---------------------------------------------------------------------------
# Human approval reaches the agent
# ---------------------------------------------------------------------------
def test_an_approved_confirmation_is_loaded_into_the_run_context(session, loan,
                                                                 docs_on_disk):
    """This is the whole propose/approve round trip, from the agent's side."""
    args = {"loan_id": loan.id, "service": "appraisal", "reason": "collateral review"}
    from app.gate import confirmation_token

    session.add(Confirmation(
        token=confirmation_token("order_vendor_service", args),
        run_id="RUN-EARLIER", loan_id=loan.id, tool="order_vendor_service",
        args=args, requested_by="processing", status=ApprovalStatus.APPROVED,
        confirmed_by="supervisor.raj",
    ))
    session.commit()

    client = ScriptedClient(
        response([tool_block("order_vendor_service", args)]),
        response([text_block("Ordered.")], stop_reason=DONE),
    )
    result = agents.run_agent(session, agent="processing", loan_id=loan.id,
                              run_id="RUN-P1", client=client)

    block = client.requests[1]["messages"][2]["content"][0]
    assert block["is_error"] is False
    assert '"status": "placed"' in block["content"]
    assert result.stopped == "end_turn"


def test_a_pending_confirmation_does_not_authorise_anything(session, loan, docs_on_disk):
    args = {"loan_id": loan.id, "service": "appraisal", "reason": "collateral review"}
    from app.gate import confirmation_token

    session.add(Confirmation(
        token=confirmation_token("order_vendor_service", args),
        run_id="RUN-EARLIER", loan_id=loan.id, tool="order_vendor_service",
        args=args, requested_by="processing", status=ApprovalStatus.PENDING,
    ))
    session.commit()

    client = ScriptedClient(
        response([tool_block("order_vendor_service", args)]),
        response([text_block("Queued.")], stop_reason=DONE),
    )
    agents.run_agent(session, agent="processing", loan_id=loan.id, run_id="RUN-P2",
                     client=client)

    block = client.requests[1]["messages"][2]["content"][0]
    assert block["is_error"] is True
    assert "human confirmation" in block["content"]


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------
def test_the_pipeline_runs_the_agents_in_dependency_order(session, loan, docs_on_disk):
    client = ScriptedClient(*[
        response([text_block("nothing to do")], stop_reason=DONE) for _ in range(4)
    ])
    out = agents.run_pipeline(session, loan.id, run_prefix="RUN-PIPE", client=client)

    assert [r.agent for r in out.results] == list(agents.PIPELINE)
    runs = session.exec(select(Run).order_by(Run.started_at)).all()
    assert [r.agent for r in runs] == list(agents.PIPELINE)


def test_each_agent_gets_its_own_run_and_budget(session, loan, docs_on_disk):
    client = ScriptedClient(*[
        response([text_block("x")], stop_reason=DONE) for _ in range(4)
    ])
    out = agents.run_pipeline(session, loan.id, run_prefix="RUN-PIPE", client=client)
    assert len({r.run_id for r in out.results}) == 4
    assert len({id(r.budget) for r in out.results}) == 4


def test_intake_marks_the_loan_scanned(session, loan, docs_on_disk):
    """An unscanned loan is never ready, however clean it looks."""
    loan.scanned = False
    session.add(loan)
    session.commit()

    client = ScriptedClient(*[
        response([text_block("x")], stop_reason=DONE) for _ in range(4)
    ])
    agents.run_pipeline(session, loan.id, run_prefix="RUN-PIPE", client=client)
    assert session.get(Loan, loan.id).scanned is True


def test_the_summary_is_carried_out_of_the_pipeline(session, loan, docs_on_disk):
    client = ScriptedClient(
        response([text_block("intake")], stop_reason=DONE),
        response([text_block("validation")], stop_reason=DONE),
        response([text_block("processing")], stop_reason=DONE),
        response([text_block("Income is documented; collateral is not.")],
                 stop_reason=DONE),
    )
    out = agents.run_pipeline(session, loan.id, run_prefix="RUN-PIPE", client=client)
    assert out.summary == "Income is documented; collateral is not."


def test_the_summary_is_persisted_onto_the_loan(session, loan, docs_on_disk):
    """It must survive past the live log, not just the in-memory result."""
    client = ScriptedClient(
        response([text_block("intake")], stop_reason=DONE),
        response([text_block("validation")], stop_reason=DONE),
        response([text_block("processing")], stop_reason=DONE),
        response([text_block("Income is documented; collateral is not.")],
                 stop_reason=DONE),
    )
    agents.run_pipeline(session, loan.id, run_prefix="RUN-PIPE", client=client)
    assert session.get(Loan, loan.id).summary == "Income is documented; collateral is not."


def test_a_budget_or_turn_capped_summary_is_still_persisted(session, loan, docs_on_disk):
    """Partial text beats no text — same tolerance `mark_scanned` gets."""
    client = ScriptedClient(
        response([text_block("intake")], stop_reason=DONE),
        response([text_block("validation")], stop_reason=DONE),
        response([text_block("processing")], stop_reason=DONE),
        response([text_block("truncated...")], stop_reason="max_tokens"),
    )
    agents.run_pipeline(session, loan.id, run_prefix="RUN-PIPE", client=client)
    assert session.get(Loan, loan.id).summary == "truncated..."


def test_a_second_summary_overwrites_the_first(session, loan, docs_on_disk):
    """Unlike `scanned`, this reflects the latest read — it is not one-way."""
    for text in ("first pass", "second pass, file has changed"):
        client = ScriptedClient(*[
            response([text_block(text)], stop_reason=DONE) for _ in range(4)
        ])
        agents.run_pipeline(session, loan.id, run_prefix=f"RUN-{text[:4]}", client=client)
    assert session.get(Loan, loan.id).summary == "second pass, file has changed"


def test_a_failing_agent_does_not_stop_the_pipeline(session, loan, docs_on_disk):
    """One agent dying must not cost the file the other three."""
    class HalfBroken:
        def __init__(self):
            self.calls = 0
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            self.calls += 1
            if self.calls == 2:      # validation
                raise RuntimeError("upstream 503")
            return response([text_block("ok")], stop_reason=DONE)

    out = agents.run_pipeline(session, loan.id, run_prefix="RUN-PIPE", client=HalfBroken())
    assert len(out.results) == 4
    assert [r.agent for r in out.failed] == ["validation"]
    assert out.results[3].stopped == "end_turn"


def test_the_audit_chain_survives_a_whole_pipeline(session, loan, docs_on_disk):
    client = ScriptedClient(
        response([tool_block("list_documents", {"loan_id": loan.id})]),
        response([text_block("done")], stop_reason=DONE),
        *[response([text_block("x")], stop_reason=DONE) for _ in range(3)],
    )
    agents.run_pipeline(session, loan.id, run_prefix="RUN-PIPE", client=client)
    ok, broken = store.verify_audit_chain(session)
    assert ok, f"chain broke at {broken}"


def test_a_model_raised_finding_is_dispositioned_by_policy_not_the_model(
    session, loan, docs_on_disk
):
    """End to end: the model asks for auto, policy decides otherwise."""
    client = ScriptedClient(
        response([tool_block("raise_exception", {
            "loan_id": loan.id, "stage": 1,
            "exception_type": "income_variance",     # never auto, whatever the score
            "label": "Income variance", "severity": "High", "confidence": 99,
            "rationale": "Paystub annualises above the W-2.",
            "evidence_doc_id": f"{loan.id}-paystub",
            "evidence_quote": "DOCUMENT TYPE: PAYSTUB",
        })]),
        response([text_block("Raised.")], stop_reason=DONE),
    )
    run(session, client)

    exc = session.exec(select(ExceptionRecord)).one()
    assert exc.confidence == 99
    assert exc.lane == "hitl"
    assert exc.queue == "A"


def test_a_failing_event_callback_does_not_kill_the_run(session, loan, docs_on_disk):
    """A display problem must never cost a run that worked.

    The first real pipeline run died exactly here: the summarizer wrote a
    right-arrow, the Windows console could not encode it, and the resulting
    UnicodeEncodeError propagated out of the emit callback into the agent loop.
    """
    def explode(ev):
        raise UnicodeEncodeError("charmap", "\u2192", 0, 1, "undefined")

    client = ScriptedClient(
        response([tool_block("get_loan", {"loan_id": loan.id})]),
        response([text_block("Summary complete.")], stop_reason=DONE),
    )
    result = run(session, client, on_event=explode)

    assert result.stopped == "end_turn"
    assert result.text == "Summary complete."
    assert len(result.events) > 0        # still recorded, just not displayed


# ---------------------------------------------------------------------------
# Status-aware dispatch — what a selection-triggered scan runs, if anything,
# before any agent is ever called
# ---------------------------------------------------------------------------
def make_exc(loan, *, exception_type, severity, confidence, exc_id="EX-1", stage=1):
    return ExceptionRecord.from_finding(
        id=exc_id, loan_id=loan.id, stage=stage, exception_type=exception_type,
        label="test finding", severity=severity, confidence=confidence,
    )


def test_unscanned_loan_gets_the_full_pipeline(loan):
    loan.scanned = False
    assert agents.select_agents_for_loan(loan, []) == agents.PIPELINE


def test_scanned_loan_with_open_auto_lane_exception_retries_processing(loan):
    exc = make_exc(loan, exception_type="missing_document", severity="Medium", confidence=95)
    assert exc.status == ExceptionStatus.PREDICTED
    assert agents.select_agents_for_loan(loan, [exc]) == ("processing", "summarizer")


def test_scanned_loan_never_gets_validation_again(loan):
    """Re-running validation on an already-scanned loan risks duplicate
    exceptions — `raise_exception` has no dedup guard."""
    exc = make_exc(loan, exception_type="missing_document", severity="Medium", confidence=95)
    assert "validation" not in agents.select_agents_for_loan(loan, [exc])


def test_scanned_loan_with_only_hitl_exceptions_has_nothing_outstanding(loan):
    exc = make_exc(loan, exception_type="income_variance", severity="High", confidence=99)
    assert exc.status == ExceptionStatus.ROUTED
    assert agents.select_agents_for_loan(loan, [exc]) == ()


def test_clean_scanned_loan_has_nothing_outstanding(loan):
    assert agents.select_agents_for_loan(loan, []) == ()


def test_outstanding_work_message_names_the_human_queue(loan):
    exc = make_exc(loan, exception_type="income_variance", severity="High", confidence=99)
    msg = agents.outstanding_work_message(loan, [exc])
    assert "1" in msg and "human" in msg


def test_outstanding_work_message_says_ready_when_ready(loan):
    loan.ready = True
    msg = agents.outstanding_work_message(loan, [])
    assert "ready for underwriting" in msg


def test_outstanding_work_message_falls_back_when_neither(loan):
    msg = agents.outstanding_work_message(loan, [])
    assert msg == "No outstanding AI work for this loan."
