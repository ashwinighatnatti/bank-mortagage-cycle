"""API tests, with the wrong role on purpose.

The UI hides what a role cannot do. These tests ignore the UI entirely and post
straight at the endpoints with a valid token for the wrong role, because that is
the only way to find out whether the control is real or decorative.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import rbac
from app.api import app
from app.db import get_session
from app.models import (
    Approval,
    ApprovalStatus,
    Confirmation,
    ExceptionRecord,
    ExceptionStatus,
    Loan,
    Note,
    Run,
)
from app.policy import Severity

PASSWORD = rbac.DEMO_PASSWORD


@pytest.fixture()
def client(session, loan):
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def token(client, username: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def auth(client, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(client, username)}"}


def make_exception(session, loan, **over) -> ExceptionRecord:
    kwargs = dict(
        id="EX-001", loan_id=loan.id, stage=1,
        exception_type="flood_determination_mismatch",
        label="Flood zone determination mismatch", severity=Severity.LOW,
        confidence=71, evidence_doc_id=f"{loan.id}-flood_cert", raised_by="validation",
    )
    kwargs.update(over)
    exc = ExceptionRecord.from_finding(**kwargs)
    session.add(exc)
    session.commit()
    return exc


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_login_returns_a_token_and_the_users_capabilities(client):
    r = client.post("/api/auth/login",
                    json={"username": "supervisor", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["name"] == "Marcus Webb"
    assert "approve" in body["user"]["can"]


def test_a_wrong_password_is_rejected(client):
    r = client.post("/api/auth/login",
                    json={"username": "supervisor", "password": "nope"})
    assert r.status_code == 401


def test_an_unknown_user_is_rejected_the_same_way(client):
    """The response must not reveal which usernames exist."""
    unknown = client.post("/api/auth/login",
                          json={"username": "ghost", "password": PASSWORD})
    wrong = client.post("/api/auth/login",
                        json={"username": "supervisor", "password": "nope"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_no_token_is_401_not_403(client):
    assert client.get("/api/kpis").status_code == 401


def test_a_forged_role_claim_does_not_grant_the_role(client):
    """The role comes from the roster, never from the token body.

    Anyone who could mint a token would otherwise be able to mint a supervisor.
    """
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    from app.config import get_settings

    now = datetime.now(timezone.utc)
    forged = jwt.encode(
        {"sub": "analyst1", "role": "supervisor", "name": "Priya Nair",
         "queue": "A", "iat": int(now.timestamp()),
         "exp": int((now + timedelta(hours=1)).timestamp())},
        get_settings().jwt_secret, algorithm=rbac.ALGORITHM,
    )
    r = client.get("/api/approvals", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 403
    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {forged}"}).json()["role"] \
        == "analyst"


# ---------------------------------------------------------------------------
# RBAC — posting as the wrong role
# ---------------------------------------------------------------------------
def test_an_analyst_cannot_see_the_approvals_queue(client):
    assert client.get("/api/approvals", headers=auth(client, "analyst1")).status_code == 403


def test_an_underwriter_cannot_work_a_hitl_case(client, session, loan):
    make_exception(session, loan)
    r = client.post("/api/exceptions/EX-001/act",
                    json={"action": "verify"}, headers=auth(client, "underwriter"))
    assert r.status_code == 403
    assert "may not work_exception" in r.json()["detail"]


def test_a_supervisor_cannot_render_an_underwriting_decision(client, session, loan):
    """Approving a fix and deciding a loan are different jobs."""
    loan.ready = True
    session.add(loan)
    session.commit()
    r = client.post(f"/api/loans/{loan.id}/decision",
                    json={"decision": "approve"}, headers=auth(client, "supervisor"))
    assert r.status_code == 403


def test_an_analyst_cannot_act_on_another_analysts_queue(client, session, loan):
    make_exception(session, loan)                       # queue B
    r = client.post("/api/exceptions/EX-001/act",
                    json={"action": "verify"}, headers=auth(client, "analyst1"))  # queue A
    assert r.status_code == 403
    assert "belongs to another analyst" in r.json()["detail"]


def test_an_analyst_may_view_the_whole_board(client, session, loan):
    """Viewing is open; acting is queue-scoped. The reference works this way."""
    make_exception(session, loan)
    r = client.get("/api/exceptions", headers=auth(client, "analyst1"))
    assert r.status_code == 200 and len(r.json()) == 1


def test_only_a_supervisor_may_confirm_a_gated_call(client, session, loan):
    session.add(Confirmation(
        token="tok-1", run_id="R", loan_id=loan.id, tool="order_vendor_service",
        args={"loan_id": loan.id}, requested_by="processing",
        status=ApprovalStatus.PENDING,
    ))
    session.commit()

    for who in ("analyst2", "underwriter"):
        r = client.post("/api/confirmations/tok-1/decide",
                        json={"decision": "approved"}, headers=auth(client, who))
        assert r.status_code == 403, who

    r = client.post("/api/confirmations/tok-1/decide",
                    json={"decision": "approved"}, headers=auth(client, "supervisor"))
    assert r.status_code == 200
    assert session.get(Confirmation, "tok-1").status == ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# The propose / approve round trip
# ---------------------------------------------------------------------------
def test_an_analyst_resolves_a_case_that_needs_no_sign_off(client, session, loan):
    make_exception(session, loan)                       # queue B, no sign-off
    r = client.post("/api/exceptions/EX-001/act",
                    json={"action": "recalc", "note": "Re-pulled the determination"},
                    headers=auth(client, "analyst2"))
    assert r.status_code == 200
    assert r.json()["status"] == ExceptionStatus.RESOLVED
    assert r.json()["resolved_by"] == "analyst2"


def test_a_human_resolution_becomes_operational_memory(client, session, loan):
    """The only source this system accepts for a note."""
    make_exception(session, loan)
    client.post("/api/exceptions/EX-001/act",
                json={"action": "recalc", "note": "Re-pull resolved it"},
                headers=auth(client, "analyst2"))
    note = session.exec(select(Note).where(Note.loan_id == loan.id)).first()
    assert note is not None and note.source == "human_resolution"
    assert "Lena Rossi" not in note.text and "Arjun Mehta" in note.text


def test_a_sign_off_case_is_proposed_not_resolved(client, session, loan):
    make_exception(session, loan, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)
    r = client.post("/api/exceptions/EX-001/act",
                    json={"action": "override", "note": "Compensating factors"},
                    headers=auth(client, "analyst1"))            # dti_breach -> queue A
    assert r.status_code == 200
    assert r.json()["status"] == ExceptionStatus.PENDING
    assert r.json()["resolved_by"] is None

    approval = session.exec(select(Approval)).one()
    assert approval.status == ApprovalStatus.PENDING
    assert approval.proposed_by == "analyst1"


def test_a_supervisor_approval_closes_the_exception(client, session, loan):
    make_exception(session, loan, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)
    client.post("/api/exceptions/EX-001/act", json={"action": "override"},
                headers=auth(client, "analyst1"))
    approval = session.exec(select(Approval)).one()

    r = client.post(f"/api/approvals/{approval.id}/decide",
                    json={"decision": "approved", "note": "Reserves support it"},
                    headers=auth(client, "supervisor"))
    assert r.status_code == 200
    exc = session.get(ExceptionRecord, "EX-001")
    assert exc.status == ExceptionStatus.APPROVED
    assert exc.resolved_by == "supervisor"


def test_a_rejection_sends_it_back_rather_than_closing_it(client, session, loan):
    """The problem is still there; it just is not fixed that way."""
    make_exception(session, loan, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)
    client.post("/api/exceptions/EX-001/act", json={"action": "override"},
                headers=auth(client, "analyst1"))
    approval = session.exec(select(Approval)).one()

    client.post(f"/api/approvals/{approval.id}/decide",
                json={"decision": "rejected", "note": "Need the LOX first"},
                headers=auth(client, "supervisor"))
    exc = session.get(ExceptionRecord, "EX-001")
    assert exc.status == ExceptionStatus.ROUTED
    assert "Rejected: Need the LOX first" in exc.resolution_note


def test_an_approval_cannot_be_decided_twice(client, session, loan):
    make_exception(session, loan, exception_type="dti_breach", label="DTI",
                   severity=Severity.HIGH, confidence=79)
    client.post("/api/exceptions/EX-001/act", json={"action": "override"},
                headers=auth(client, "analyst1"))
    approval = session.exec(select(Approval)).one()
    head = auth(client, "supervisor")

    assert client.post(f"/api/approvals/{approval.id}/decide",
                       json={"decision": "approved"}, headers=head).status_code == 200
    again = client.post(f"/api/approvals/{approval.id}/decide",
                        json={"decision": "rejected"}, headers=head)
    assert again.status_code == 403 and "already" in again.json()["detail"]


# ---------------------------------------------------------------------------
# The underwriting decision
# ---------------------------------------------------------------------------
def test_a_loan_that_is_not_ready_cannot_be_decided(client, session, loan):
    make_exception(session, loan)                       # open, gating
    r = client.post(f"/api/loans/{loan.id}/decision",
                    json={"decision": "approve"}, headers=auth(client, "underwriter"))
    assert r.status_code == 403
    assert "not ready for underwriting" in r.json()["detail"]


def test_a_ready_loan_is_decided_and_delivered(client, session, loan):
    make_exception(session, loan)
    client.post("/api/exceptions/EX-001/act", json={"action": "verify"},
                headers=auth(client, "analyst2"))
    assert session.get(Loan, loan.id).ready is True

    r = client.post(f"/api/loans/{loan.id}/decision",
                    json={"decision": "approve-conditions", "note": "Standard conditions"},
                    headers=auth(client, "underwriter"))
    assert r.status_code == 200
    assert r.json()["decision"] == "approve-conditions"
    assert session.get(Loan, loan.id).delivered is True


def test_an_unknown_decision_is_refused(client, session, loan):
    loan.ready = True
    session.add(loan)
    session.commit()
    r = client.post(f"/api/loans/{loan.id}/decision",
                    json={"decision": "probably-fine"}, headers=auth(client, "underwriter"))
    assert r.status_code == 403 and "unknown decision" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------
def test_the_rules_panel_reports_three_outcomes_not_two(client, session, loan):
    """Collapsing INDETERMINATE into pass or fail would be the easy lie here."""
    body = client.get(f"/api/loans/{loan.id}", headers=auth(client, "underwriter")).json()
    outcomes = {r["outcome"] for r in body["rules"]}
    assert "indeterminate" in outcomes
    missing = next(r for r in body["rules"] if r["outcome"] == "indeterminate")
    assert missing["missing"]


def test_kpis_are_derived_from_the_rows_they_summarise(client, session, loan):
    make_exception(session, loan, id="EX-A", confidence=92)      # auto
    make_exception(session, loan, id="EX-B", confidence=60)      # hitl
    body = client.get("/api/kpis", headers=auth(client, "analyst1")).json()
    assert body["exceptions"] == 2
    assert body["auto_total"] == 1
    assert body["open_hitl"] == 1


def test_the_audit_endpoint_reports_chain_integrity(client, session, loan):
    make_exception(session, loan)
    client.post("/api/exceptions/EX-001/act", json={"action": "verify"},
                headers=auth(client, "analyst2"))
    body = client.get("/api/audit", headers=auth(client, "analyst2")).json()
    assert body["chain_intact"] is True
    assert any(e["kind"] == "human" for e in body["entries"])


def test_human_and_ai_actions_are_distinguishable_in_the_trail(client, session, loan):
    """The governance story is a query, not an archaeology project."""
    from app import store

    with store.guarded_write(session, loan_id=loan.id, actor="validation",
                             role="Validation Agent", kind="ai", action="raise_exception"):
        pass
    make_exception(session, loan)
    client.post("/api/exceptions/EX-001/act", json={"action": "verify"},
                headers=auth(client, "analyst2"))

    entries = client.get("/api/audit", headers=auth(client, "analyst2")).json()["entries"]
    kinds = {e["kind"] for e in entries}
    assert {"ai", "human"} <= kinds


def test_site_gate_is_off_by_default(client):
    """Unset in local dev / tests -- must never lock out someone who never
    configured it."""
    r = client.get("/api/auth/personas")
    assert r.status_code == 200


def test_site_gate_when_configured(client, monkeypatch):
    """The whole-app gate, in front of even the sign-in screen.

    A session cookie, not HTTP Basic Auth -- Basic Auth does not reliably
    reach every request type this app makes (in particular the SSE agent-log
    stream, which cannot carry an Authorization header at all), so a browser
    kept getting re-challenged mid-session instead of asking once.
    """
    from app.config import get_settings

    monkeypatch.setenv("BASIC_AUTH_USERNAME", "gatekeeper")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "letmein-strong")
    get_settings.cache_clear()
    try:
        # No cookie yet: a browser navigation (GET) is sent to the gate page;
        # anything else (an API call) is refused outright.
        redirected = client.get("/api/auth/personas", follow_redirects=False)
        assert redirected.status_code == 303
        assert redirected.headers["location"] == "/gate"
        assert client.post("/api/auth/login", json={}).status_code == 401

        # Wrong credentials: no cookie is set, still refused.
        wrong = client.post("/gate", data={"username": "gatekeeper", "password": "nope"},
                            follow_redirects=False)
        assert wrong.status_code == 401
        assert "site_gate" not in wrong.cookies

        # Right credentials: a cookie is set, and this client's cookie jar
        # carries it into every request from here on -- including the ones
        # that were refused above.
        right = client.post("/gate", data={"username": "gatekeeper", "password": "letmein-strong"},
                            follow_redirects=False)
        assert right.status_code == 303
        assert right.headers["location"] == "/"
        assert "site_gate" in right.cookies

        assert client.get("/api/auth/personas").status_code == 200
    finally:
        get_settings.cache_clear()


def test_a_missing_loan_is_404(client):
    assert client.get("/api/loans/LN-NOPE",
                      headers=auth(client, "analyst1")).status_code == 404


def test_only_a_supervisor_may_start_a_scan(client, loan):
    """The scan endpoint takes its token in the query string; check it still gates."""
    for who in ("analyst1", "underwriter"):
        r = client.get(f"/api/loans/{loan.id}/scan?token={token(client, who)}")
        assert r.status_code == 403, who


def test_scan_on_a_ready_loan_says_so_and_spends_nothing(client, session, loan):
    """A ready loan has no outstanding AI work — selecting it must not run agents."""
    loan.ready = True
    session.add(loan)
    session.commit()

    r = client.get(f"/api/loans/{loan.id}/scan?token={token(client, 'supervisor')}")
    assert r.status_code == 200
    assert "ready for underwriting" in r.text
    assert session.exec(select(Run)).all() == []


def test_scan_on_a_loan_with_only_hitl_open_says_so_and_spends_nothing(client, session, loan):
    """Nothing an agent can do while an exception sits with a human queue."""
    make_exception(session, loan, exception_type="income_variance", label="Income variance",
                   severity=Severity.HIGH, confidence=99)

    r = client.get(f"/api/loans/{loan.id}/scan?token={token(client, 'supervisor')}")
    assert r.status_code == 200
    assert "waiting on a human queue" in r.text
    assert session.exec(select(Run)).all() == []


# ---------------------------------------------------------------------------
# One proposal per exception
# ---------------------------------------------------------------------------
def test_a_second_proposal_on_the_same_exception_is_refused(client, session, loan):
    """Three cards for one case is what this prevents.

    A sign-off exception moves to PENDING, which is still *open*, so without an
    explicit check it can be worked again and again — each time minting another
    approval for the same finding. Approving one then closes the exception and
    leaves the rest pointing at a decided case.
    """
    make_exception(session, loan, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)

    first = client.post("/api/exceptions/EX-001/act", json={"action": "override"},
                        headers=auth(client, "analyst1"))
    assert first.status_code == 200

    second = client.post("/api/exceptions/EX-001/act", json={"action": "verify"},
                         headers=auth(client, "analyst1"))
    assert second.status_code == 403
    assert "already has a proposal awaiting sign-off" in second.json()["detail"]
    assert "AP-001" in second.json()["detail"]

    assert len(session.exec(select(Approval)).all()) == 1


def test_a_supervisor_cannot_add_a_second_proposal_either(client, session, loan):
    """A supervisor works any queue, so they can reach the same path."""
    make_exception(session, loan, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)
    client.post("/api/exceptions/EX-001/act", json={"action": "override"},
                headers=auth(client, "analyst1"))

    again = client.post("/api/exceptions/EX-001/act", json={"action": "verify"},
                        headers=auth(client, "supervisor"))
    assert again.status_code == 403
    assert len(session.exec(select(Approval)).all()) == 1


def test_a_rejection_reopens_the_case_for_a_new_proposal(client, session, loan):
    """The door closes on duplicates, not on second attempts."""
    make_exception(session, loan, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)
    client.post("/api/exceptions/EX-001/act", json={"action": "override"},
                headers=auth(client, "analyst1"))
    approval = session.exec(select(Approval)).one()

    client.post(f"/api/approvals/{approval.id}/decide",
                json={"decision": "rejected", "note": "Need the LOX first"},
                headers=auth(client, "supervisor"))

    retry = client.post("/api/exceptions/EX-001/act",
                        json={"action": "recalc", "note": "LOX received"},
                        headers=auth(client, "analyst1"))
    assert retry.status_code == 200
    assert len(session.exec(select(Approval)).all()) == 2


def test_an_approval_pointing_at_a_closed_case_cannot_be_actioned(client, session, loan):
    """Defence in depth for any stale proposal already in the queue."""
    from app.models import ExceptionStatus

    make_exception(session, loan, exception_type="dti_breach", label="DTI breach",
                   severity=Severity.HIGH, confidence=79)
    client.post("/api/exceptions/EX-001/act", json={"action": "override"},
                headers=auth(client, "analyst1"))
    approval = session.exec(select(Approval)).one()

    exc = session.get(ExceptionRecord, "EX-001")
    exc.status = ExceptionStatus.APPROVED
    exc.resolved_by = "supervisor"
    session.add(exc)
    session.commit()

    r = client.post(f"/api/approvals/{approval.id}/decide",
                    json={"decision": "approved"}, headers=auth(client, "supervisor"))
    assert r.status_code == 403
    assert "has been decided" in r.json()["detail"]
