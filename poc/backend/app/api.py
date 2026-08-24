"""The HTTP API the React app talks to.

EVERY MUTATING ENDPOINT RE-CHECKS THE ROLE. The UI hides what a role cannot do,
because showing it would be bad design — but that is presentation. `rbac.require`
runs inside `human.py` before any write, so posting directly to an endpoint with
a valid token for the wrong role is refused exactly as an agent is refused a tool
it does not hold. The tests post as the wrong role on purpose.

Errors are translated once, here: `rbac.Denied` becomes 403, `LookupError`
becomes 404, `policy.InvariantViolation` becomes 409. Handlers therefore raise
domain exceptions and never build HTTP responses, which keeps `human.py` usable
from a script, a test and a future queue worker without dragging FastAPI along.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from sse_starlette.sse import EventSourceResponse

from . import agents, human, rbac, reporting, store
from .config import get_settings
from .db import get_session, init_db
from .models import (
    Approval,
    ApprovalStatus,
    AuditEntry,
    Confirmation,
    ExceptionRecord,
    Loan,
    Run,
)
from .policy import InvariantViolation
from .rbac import Action, User

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create any missing tables at startup. Create-only, never drop."""
    init_db()
    yield


app = FastAPI(
    title="AI-Native Mortgage POC",
    description="Agentic loan origination with a human in the loop.",
    version="0.6.0",
    lifespan=lifespan,
)

# The React dev server runs on another origin. Locked to localhost — in the
# container the app is served from the same origin and this is not used.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


GATE_COOKIE_NAME = "site_gate"
GATE_TTL_DAYS = 30


@app.middleware("http")
async def site_gate(request: Request, call_next: Any) -> Response:
    """A one-time gate in front of the whole app, before even the sign-in
    screen — a session cookie, not HTTP Basic Auth.

    Basic Auth was the first cut at this and turned out to be the wrong
    tool: browsers do not reliably reuse cached Basic credentials across
    every request type this app makes, and the EventSource-based agent log
    stream cannot carry an Authorization header at all (the same limitation
    `user_from_query_token`'s docstring already documents for the app's own
    auth). A cookie is sent automatically with every request type, including
    EventSource, so the visitor only has to answer once per browser instead
    of being re-challenged mid-session.

    Opt-in: enforced only when both BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD
    are set (see config.py) — unset by default, so local dev is never gated
    by a control nobody there has configured.
    """
    settings = get_settings()
    if not (settings.basic_auth_username and settings.basic_auth_password):
        return await call_next(request)

    if request.url.path == "/gate":
        return await call_next(request)

    token = request.cookies.get(GATE_COOKIE_NAME)
    valid = False
    if token:
        try:
            jwt.decode(token, settings.jwt_secret, algorithms=[rbac.ALGORITHM])
            valid = True
        except JWTError:
            valid = False

    if not valid:
        if request.method == "GET":
            return RedirectResponse(url="/gate", status_code=status.HTTP_303_SEE_OTHER)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    return await call_next(request)


def _gate_page(error: bool = False) -> str:
    err_html = (
        '<p class="err">That username and password do not match.</p>' if error else ""
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AI-Native Mortgage</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0A2140; color: #fff;
          display: flex; align-items: center; justify-content: center;
          min-height: 100vh; margin: 0; }}
  form {{ background: #fff; color: #0A2140; padding: 32px 36px; border-radius: 14px;
          box-shadow: 0 12px 28px rgba(0,0,0,.35); width: 320px; }}
  h1 {{ font-size: 18px; margin: 0 0 18px; font-weight: 700; }}
  label {{ display: block; font-size: 13px; font-weight: 600; margin: 14px 0 6px; }}
  input {{ width: 100%; padding: 9px 10px; border: 1px solid #ccc; border-radius: 8px;
           font-size: 14px; box-sizing: border-box; }}
  button {{ margin-top: 20px; width: 100%; padding: 10px; border: 0; border-radius: 999px;
            background: #F06048; color: #fff; font-weight: 700; font-size: 14px;
            cursor: pointer; }}
  .err {{ color: #D8362B; font-size: 13px; margin: 10px 0 0; }}
</style></head>
<body>
  <form method="post" action="/gate">
    <h1>AI-Native Mortgage</h1>
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password">
    <button type="submit">Continue</button>
    {err_html}
  </form>
</body></html>"""


@app.get("/gate", include_in_schema=False)
def gate_form() -> HTMLResponse:
    return HTMLResponse(_gate_page())


@app.post("/gate", include_in_schema=False)
async def gate_submit(request: Request) -> Response:
    form = await request.form()
    settings = get_settings()
    ok = (
        secrets.compare_digest(str(form.get("username", "")), settings.basic_auth_username or "")
        and secrets.compare_digest(str(form.get("password", "")), settings.basic_auth_password or "")
    )
    if not ok:
        return HTMLResponse(_gate_page(error=True), status_code=status.HTTP_401_UNAUTHORIZED)

    token = jwt.encode(
        {"gate": True, "exp": datetime.now(timezone.utc) + timedelta(days=GATE_TTL_DAYS)},
        settings.jwt_secret, algorithm=rbac.ALGORITHM,
    )
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        GATE_COOKIE_NAME, token, max_age=GATE_TTL_DAYS * 86400,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    return resp


bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    try:
        return rbac.read_token(creds.credentials)
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def user_from_query_token(token: str) -> User:
    """EventSource cannot send an Authorization header.

    So the SSE endpoint takes the token as a query parameter. That is a real
    tradeoff: query strings land in access logs and browser history in a way
    headers do not. Acceptable for a demo on localhost with an 8-hour token;
    before production this wants either a short-lived single-use stream ticket
    or a cookie-authenticated endpoint.
    """
    try:
        return rbac.read_token(token)
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginBody) -> dict[str, Any]:
    user = rbac.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return {"token": rbac.issue_token(user), "user": _me(user)}


def _me(user: User) -> dict[str, Any]:
    return {
        "username": user.username,
        "name": user.name,
        "role": str(user.role),
        "queue": user.queue,
        # The UI uses this to hide what it should hide. It is a convenience —
        # the server re-checks every one of these on the way in.
        "can": sorted(str(a) for a in rbac.CAPABILITIES[user.role]),
    }


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return _me(user)


@app.get("/api/auth/personas")
def personas() -> list[dict[str, Any]]:
    """The demo roster, for the login screen's quick-switch buttons."""
    return [
        {"username": u.username, "name": u.name, "role": str(u.role), "queue": u.queue}
        for u in rbac.USERS.values()
    ]


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------
@app.exception_handler(rbac.Denied)
def _denied(request: Request, exc: rbac.Denied):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(LookupError)
def _not_found(request: Request, exc: LookupError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(InvariantViolation)
def _conflict(request: Request, exc: InvariantViolation):
    from fastapi.responses import JSONResponse

    # 409, not 500. The request was well formed and the caller was permitted;
    # the system refused because the result would have been inconsistent.
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
@app.get("/api/kpis")
def get_kpis(user: User = Depends(current_user),
             session: Session = Depends(get_session)) -> dict[str, Any]:
    return reporting.kpis(session)


@app.get("/api/loans")
def list_loans(user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    loans = session.exec(select(Loan).order_by(Loan.id)).all()  # type: ignore[arg-type]
    return [reporting.loan_summary(session, l) for l in loans]


@app.get("/api/loans/{loan_id}")
def get_loan(loan_id: str, user: User = Depends(current_user),
             session: Session = Depends(get_session)) -> dict[str, Any]:
    return reporting.hub(session, loan_id)


@app.get("/api/exceptions")
def list_exceptions(
    queue: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    severity: str | None = None,
    open_only: bool = False,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Every analyst may VIEW the whole board; acting is queue-scoped."""
    stmt = select(ExceptionRecord).order_by(
        ExceptionRecord.loan_id, ExceptionRecord.id  # type: ignore[arg-type]
    )
    if queue:
        stmt = stmt.where(ExceptionRecord.queue == queue)
    if status_filter:
        stmt = stmt.where(ExceptionRecord.status == status_filter)
    if severity:
        stmt = stmt.where(ExceptionRecord.severity == severity)
    rows = session.exec(stmt).all()
    if open_only:
        rows = [e for e in rows if e.is_open]
    return [reporting.exception_view(e) for e in rows]


@app.get("/api/approvals")
def list_approvals(user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rbac.require(user, Action.VIEW_APPROVALS)
    rows = session.exec(select(Approval).order_by(Approval.created_at)).all()  # type: ignore[arg-type]
    return [
        {
            "id": a.id, "exception_id": a.exception_id, "loan_id": a.loan_id,
            "exception_type": a.exception_type, "proposed_by": a.proposed_by,
            "proposed_action": a.proposed_action, "ai_recommendation": a.ai_recommendation,
            "queue": a.queue, "status": a.status, "decided_by": a.decided_by,
            "note": a.note, "created_at": a.created_at,
        }
        for a in rows
    ]


@app.get("/api/confirmations")
def list_confirmations(user: User = Depends(current_user),
                       session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Gated tool calls waiting on a person — money and borrower contact."""
    rbac.require(user, Action.VIEW_APPROVALS)
    rows = session.exec(select(Confirmation).order_by(Confirmation.requested_at)).all()  # type: ignore[arg-type]
    return [
        {
            "token": c.token, "loan_id": c.loan_id, "tool": c.tool, "args": c.args,
            "requested_by": c.requested_by, "requested_at": c.requested_at,
            "status": c.status, "confirmed_by": c.confirmed_by,
        }
        for c in rows
    ]


@app.get("/api/audit")
def get_audit(
    case_id: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rbac.require(user, Action.VIEW_AUDIT)
    stmt = select(AuditEntry).order_by(AuditEntry.id.desc())  # type: ignore[union-attr]
    if case_id:
        stmt = stmt.where(AuditEntry.case_id == case_id)
    if kind:
        stmt = stmt.where(AuditEntry.kind == kind)
    rows = session.exec(stmt.limit(limit)).all()
    ok, broken = store.verify_audit_chain(session)
    return {
        "chain_intact": ok,
        "first_broken_hash": broken,
        "entries": [
            {
                "id": r.id, "at": r.at, "actor": r.actor, "role": r.role,
                "kind": r.kind, "action": r.action, "case_id": r.case_id,
                "run_id": r.run_id, "detail": r.detail, "hash": r.hash,
            }
            for r in rows
        ],
    }


@app.get("/api/runs")
def list_runs(user: User = Depends(current_user),
              session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = session.exec(select(Run).order_by(Run.started_at.desc()).limit(50)).all()  # type: ignore[union-attr]
    return [
        {
            "run_id": r.run_id, "agent": r.agent, "loan_id": r.loan_id,
            "status": r.status, "tool_calls": r.tool_calls, "usd": round(r.usd, 4),
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "cache_read_tokens": r.cache_read_tokens, "started_at": r.started_at,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Human actions
# ---------------------------------------------------------------------------
class ActBody(BaseModel):
    action: str = Field(description="verify | recalc | request | override")
    note: str = ""


@app.post("/api/exceptions/{exception_id}/claim")
def claim(exception_id: str, user: User = Depends(current_user),
          session: Session = Depends(get_session)) -> dict[str, Any]:
    return reporting.exception_view(human.claim_exception(session, user, exception_id))


@app.post("/api/exceptions/{exception_id}/act")
def act(exception_id: str, body: ActBody, user: User = Depends(current_user),
        session: Session = Depends(get_session)) -> dict[str, Any]:
    exc = human.act_on_exception(session, user, exception_id, body.action, body.note)
    return reporting.exception_view(exc)


class DecideBody(BaseModel):
    decision: str = Field(description="approved | rejected")
    note: str = ""


@app.post("/api/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, body: DecideBody,
                    user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict[str, Any]:
    a = human.decide_approval(session, user, approval_id, body.decision, body.note)
    return {"id": a.id, "status": a.status, "decided_by": a.decided_by}


@app.post("/api/confirmations/{token}/decide")
def decide_confirmation(token: str, body: DecideBody,
                        user: User = Depends(current_user),
                        session: Session = Depends(get_session)) -> dict[str, Any]:
    c = human.decide_confirmation(session, user, token, body.decision)
    return {"token": c.token, "status": c.status, "confirmed_by": c.confirmed_by}


class LoanDecisionBody(BaseModel):
    decision: str = Field(description="approve | approve-conditions | suspend | deny")
    note: str = ""


@app.post("/api/loans/{loan_id}/decision")
def decide_loan(loan_id: str, body: LoanDecisionBody,
                user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> dict[str, Any]:
    loan = human.decide_loan(session, user, loan_id, body.decision, body.note)
    return reporting.loan_summary(session, loan)


# ---------------------------------------------------------------------------
# The scan — agents, streamed
# ---------------------------------------------------------------------------
@app.get("/api/loans/{loan_id}/scan")
async def scan(loan_id: str, token: str,
               session: Session = Depends(get_session)) -> EventSourceResponse:
    """Run whatever agent work is still outstanding for a loan, streaming the
    live log as SSE.

    Which agents run — none, an initial full pass, or a retry of auto-repair —
    depends on the loan's current status; see `agents.select_agents_for_loan`.
    When nothing is outstanding (ready, or waiting on a human), this streams a
    single explanatory line and completes without spending any agent budget.

    The pipeline is synchronous and blocking, so it runs in a worker thread and
    the events cross back on a queue. Doing it inline on the event loop would
    freeze every other request for the length of a scan — including the polls
    the dashboard is making while it watches this one.
    """
    user = user_from_query_token(token)
    rbac.require(user, Action.START_SCAN)
    loan = session.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such loan: {loan_id}")

    excs = store.exceptions_for(session, loan_id)
    subset = agents.select_agents_for_loan(loan, excs)

    if not subset:
        message = agents.outstanding_work_message(loan, excs)

        async def stream_nothing_outstanding() -> Any:
            yield {"event": "log", "data": _json({
                "agent": "system", "kind": "say", "text": message,
                "tool": None, "ok": True,
            })}
            yield {"event": "complete", "data": _json({"loan_id": loan_id})}

        return EventSourceResponse(stream_nothing_outstanding())

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")

    def emit(ev: agents.Event) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {
            "agent": ev.agent, "kind": ev.kind, "text": ev.text,
            "tool": ev.tool, "ok": ev.ok,
        })

    def work() -> None:
        # A fresh session: this runs on another thread, and a SQLModel session
        # is not safe to share across threads.
        from .db import session_scope

        try:
            with session_scope() as s:
                agents.run_pipeline(s, loan_id, run_prefix=f"RUN-{stamp}-{loan_id[-4:]}",
                                    agents=subset, on_event=emit)
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, {
                "agent": "system", "kind": "error", "text": f"{type(exc).__name__}: {exc}",
                "tool": None, "ok": False,
            })
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    async def stream() -> Any:
        await loop.run_in_executor(None, lambda: None)  # ensure the loop is live
        task = loop.run_in_executor(None, work)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield {"event": "log", "data": _json(item)}
            yield {"event": "complete", "data": _json({"loan_id": loan_id})}
        finally:
            await task

    return EventSourceResponse(stream())


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, default=str)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": app.version}


# ---------------------------------------------------------------------------
# The built React app
#
# Mounted last, so every /api route above wins. In development the Vite dev
# server serves the UI and proxies /api here, so this mount is inert and the
# missing dist/ directory is not an error -- it is the normal state.
# ---------------------------------------------------------------------------
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

if DIST.is_dir():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        """Serve index.html for any non-API path.

        The UI keeps its screen in React state rather than the URL, so there are
        no client routes to fall through to -- but a refresh on any path must
        still return the app rather than a 404.
        """
        return FileResponse(DIST / "index.html")


def iter_session() -> Iterator[Session]:  # pragma: no cover - re-export for tests
    yield from get_session()
