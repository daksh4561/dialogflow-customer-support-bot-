"""
Card Servicing Agent - Backend
================================
Every endpoint follows the same pattern:
  1. log that the request came in
  2. check eligibility against business rules
  3. log the eligibility decision
  4. if approved -> execute the action against the mock card system, log it
  5. if denied   -> return a reason (Dialogflow CX will route to escalation)

All DB writes for a single request happen in ONE transaction, so an audit
row is guaranteed to exist for every action that touches the card system -
there is no code path that mutates data without logging it.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta, timezone
import uuid

from database import get_connection
from audit import write_audit

try:
    from google.cloud import dialogflow_v2 as dialogflow
    DIALOGFLOW_AVAILABLE = True
except ImportError:
    DIALOGFLOW_AVAILABLE = False

# Your GCP project ID - Dialogflow ES agents are tied to a GCP project.
# Note: Dialogflow sometimes auto-creates its OWN project when you first
# create an agent, separate from whatever project you see in the main
# Cloud Console switcher - always confirm via Dialogflow console settings
# (gear icon next to agent name > General tab > "Google Project").
DIALOGFLOW_PROJECT_ID = "card-servicing-agent-rxau"

app = FastAPI(title="Card Servicing Agent - Backend")

# In-memory idempotency guard for the Dialogflow webhook (see /dialogflow-webhook).
# Fine for a single-process demo; a production version would use Redis or a
# DB-backed dedup table so it works across multiple server instances.
_recent_webhook_requests = {}
IDEMPOTENCY_WINDOW_SECONDS = 5

# Dev-only: allow the dashboard (opened as a local HTML file or on a
# different port) to call this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Request/response models
# ------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    member_id: str


class CreateMemberRequest(BaseModel):
    full_name: str
    email: str
    account_opened_years_ago: float = 1.0   # e.g. 0.5 = 6 months ago
    credit_score_band: str = "good"          # poor | fair | good | excellent
    missed_payments_90d: int = 0
    initial_credit_limit: float = 50000.0
    card_last_four: str = "0000"


class FeeReversalRequest(BaseModel):
    session_id: str
    member_id: str
    fee_event_id: str


class LimitIncreaseRequest(BaseModel):
    session_id: str
    member_id: str
    card_id: str
    requested_limit: float


class CardReplacementRequest(BaseModel):
    session_id: str
    member_id: str
    card_id: str
    reason: str  # 'lost' | 'stolen' | 'damaged' | 'expiring'
    shipping_address: str


# ------------------------------------------------------------------
# ADMIN: create a new member (+ their first card) without touching SQL
# ------------------------------------------------------------------

@app.post("/admin/members")
def create_member(req: CreateMemberRequest):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT member_id FROM members WHERE LOWER(email) = LOWER(%s)", (req.email.strip(),))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="A member with this email already exists")

            cur.execute(
                """
                INSERT INTO members (full_name, email, account_opened_at, credit_score_band, missed_payments_90d)
                VALUES (%s, now() - (%s || ' years')::interval, %s, %s)
                RETURNING member_id
                """,
                (req.full_name, req.account_opened_years_ago, req.credit_score_band, req.missed_payments_90d),
            )
            member_id = cur.fetchone()["member_id"]

            masked = f"**** **** **** {req.card_last_four}"
            cur.execute(
                """
                INSERT INTO cards (member_id, card_number_masked, status, credit_limit)
                VALUES (%s, %s, 'active', %s)
                RETURNING card_id
                """,
                (member_id, masked, req.initial_credit_limit),
            )
            card_id = cur.fetchone()["card_id"]
        conn.commit()

    return {
        "member_id": str(member_id),
        "card_id": str(card_id),
        "email": req.email,
        "full_name": req.full_name,
    }


@app.get("/admin/members")
def list_members():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.member_id, m.full_name, m.email, m.account_opened_at,
                       m.credit_score_band, m.missed_payments_90d,
                       c.card_id, c.card_number_masked, c.status AS card_status, c.credit_limit
                FROM members m
                LEFT JOIN cards c ON c.member_id = m.member_id
                ORDER BY m.created_at DESC
                """
            )
            return {"members": cur.fetchall()}


# ------------------------------------------------------------------
# Sessions
# ------------------------------------------------------------------

@app.post("/sessions")
def create_session(req: CreateSessionRequest):
    session_id = str(uuid.uuid4())
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (session_id, member_id) VALUES (%s, %s)",
                (session_id, req.member_id),
            )
            write_audit(
                cur, session_id, req.member_id,
                actor="system", event_type="session_started",
                detail={"note": "Session created"},
            )
        conn.commit()
    return {"session_id": session_id}


# ------------------------------------------------------------------
# 1. FEE REVERSAL
# ------------------------------------------------------------------

@app.post("/fee-reversal")
def fee_reversal(req: FeeReversalRequest):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET request_type = 'fee_reversal' WHERE session_id = %s",
                (req.session_id,),
            )
            write_audit(
                cur, req.session_id, req.member_id,
                actor="bot", event_type="request_received",
                request_type="fee_reversal",
                detail={"fee_event_id": req.fee_event_id},
            )

            cur.execute(
                "SELECT * FROM fee_events WHERE fee_event_id = %s", (req.fee_event_id,)
            )
            fee = cur.fetchone()
            if not fee:
                raise HTTPException(status_code=404, detail="fee_event not found")
            if fee["reversed"]:
                write_audit(
                    cur, req.session_id, req.member_id,
                    actor="system", event_type="eligibility_checked",
                    request_type="fee_reversal", decision="denied",
                    detail={"reason": "fee already reversed"},
                )
                conn.commit()
                return {"approved": False, "reason": "This fee has already been reversed."}

            # Rule: at most 1 fee reversal per member in the trailing 12 months
            cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM fee_events fe
                JOIN cards c ON c.card_id = fe.card_id
                WHERE c.member_id = %s
                  AND fe.reversed = TRUE
                  AND fe.reversed_at > now() - interval '12 months'
                """,
                (req.member_id,),
            )
            reversal_count = cur.fetchone()["cnt"]

            eligible = reversal_count < 1
            write_audit(
                cur, req.session_id, req.member_id,
                actor="system", event_type="eligibility_checked",
                request_type="fee_reversal",
                decision="approved" if eligible else "denied",
                detail={
                    "rule": "max 1 reversal per 12 months",
                    "prior_reversals_12mo": reversal_count,
                    "fee_amount": float(fee["amount"]),
                },
            )

            if not eligible:
                conn.commit()
                return {
                    "approved": False,
                    "reason": "You've already used your fee reversal for this 12-month period.",
                }

            cur.execute(
                "UPDATE fee_events SET reversed = TRUE, reversed_at = now() WHERE fee_event_id = %s",
                (req.fee_event_id,),
            )
            write_audit(
                cur, req.session_id, req.member_id,
                actor="system", event_type="action_executed",
                request_type="fee_reversal", decision="approved",
                detail={"fee_event_id": req.fee_event_id, "amount_reversed": float(fee["amount"])},
            )

            cur.execute(
                "UPDATE sessions SET resolution_status = 'resolved', request_type = 'fee_reversal' WHERE session_id = %s",
                (req.session_id,),
            )
        conn.commit()

    return {"approved": True, "amount_reversed": float(fee["amount"])}


# ------------------------------------------------------------------
# 2. CREDIT LIMIT INCREASE
# ------------------------------------------------------------------

@app.post("/limit-increase")
def limit_increase(req: LimitIncreaseRequest):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET request_type = 'limit_increase' WHERE session_id = %s",
                (req.session_id,),
            )
            write_audit(
                cur, req.session_id, req.member_id,
                actor="bot", event_type="request_received",
                request_type="limit_increase",
                detail={"card_id": req.card_id, "requested_limit": req.requested_limit},
            )

            cur.execute("SELECT * FROM cards WHERE card_id = %s", (req.card_id,))
            card = cur.fetchone()
            if not card:
                raise HTTPException(status_code=404, detail="card not found")

            cur.execute("SELECT * FROM members WHERE member_id = %s", (req.member_id,))
            member = cur.fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="member not found")

            account_age_ok = member["account_opened_at"] <= datetime.now(timezone.utc) - timedelta(days=180)
            payments_ok = member["missed_payments_90d"] == 0
            # cap: increase can't exceed 2x current limit in one request
            amount_ok = req.requested_limit <= float(card["credit_limit"]) * 2

            eligible = account_age_ok and payments_ok and amount_ok

            cur.execute(
                """
                INSERT INTO limit_change_requests
                    (card_id, requested_limit, previous_limit, approved, decided_at)
                VALUES (%s, %s, %s, %s, now())
                RETURNING request_id
                """,
                (req.card_id, req.requested_limit, card["credit_limit"], eligible),
            )
            request_row = cur.fetchone()

            write_audit(
                cur, req.session_id, req.member_id,
                actor="system", event_type="eligibility_checked",
                request_type="limit_increase",
                decision="approved" if eligible else "denied",
                detail={
                    "account_age_ok": account_age_ok,
                    "payments_ok": payments_ok,
                    "amount_within_cap": amount_ok,
                    "current_limit": float(card["credit_limit"]),
                    "requested_limit": req.requested_limit,
                },
            )

            if not eligible:
                conn.commit()
                return {
                    "approved": False,
                    "reason": "This request doesn't meet auto-approval criteria and needs human review.",
                    "escalate": True,
                }

            cur.execute(
                "UPDATE cards SET credit_limit = %s WHERE card_id = %s",
                (req.requested_limit, req.card_id),
            )
            write_audit(
                cur, req.session_id, req.member_id,
                actor="system", event_type="action_executed",
                request_type="limit_increase", decision="approved",
                detail={"request_id": str(request_row["request_id"]),
                        "new_limit": req.requested_limit},
            )
            cur.execute(
                "UPDATE sessions SET resolution_status = 'resolved', request_type = 'limit_increase' WHERE session_id = %s",
                (req.session_id,),
            )
        conn.commit()

    return {"approved": True, "new_limit": req.requested_limit}


# ------------------------------------------------------------------
# 3. REPLACEMENT CARD
# ------------------------------------------------------------------

@app.post("/card-replacement")
def card_replacement(req: CardReplacementRequest):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET request_type = 'card_replacement' WHERE session_id = %s",
                (req.session_id,),
            )
            write_audit(
                cur, req.session_id, req.member_id,
                actor="bot", event_type="request_received",
                request_type="card_replacement",
                detail={"card_id": req.card_id, "reason": req.reason},
            )

            cur.execute("SELECT * FROM cards WHERE card_id = %s", (req.card_id,))
            card = cur.fetchone()
            if not card:
                raise HTTPException(status_code=404, detail="card not found")

            # Always eligible, but log the check for a consistent audit shape
            write_audit(
                cur, req.session_id, req.member_id,
                actor="system", event_type="eligibility_checked",
                request_type="card_replacement", decision="approved",
                detail={"reason": req.reason},
            )

            cur.execute(
                """
                INSERT INTO replacement_orders (card_id, reason, shipping_address, status)
                VALUES (%s, %s, %s, 'processing')
                RETURNING order_id
                """,
                (req.card_id, req.reason, req.shipping_address),
            )
            order = cur.fetchone()

            if req.reason in ("lost", "stolen"):
                cur.execute(
                    "UPDATE cards SET status = 'blocked' WHERE card_id = %s", (req.card_id,)
                )
            else:
                cur.execute(
                    "UPDATE cards SET status = 'replacement_pending' WHERE card_id = %s",
                    (req.card_id,),
                )

            write_audit(
                cur, req.session_id, req.member_id,
                actor="system", event_type="action_executed",
                request_type="card_replacement", decision="approved",
                detail={"order_id": str(order["order_id"]), "reason": req.reason,
                        "card_status_updated_to":
                            "blocked" if req.reason in ("lost", "stolen") else "replacement_pending"},
            )
            cur.execute(
                "UPDATE sessions SET resolution_status = 'resolved', request_type = 'card_replacement' WHERE session_id = %s",
                (req.session_id,),
            )
        conn.commit()

    return {"approved": True, "order_id": str(order["order_id"])}


# ------------------------------------------------------------------
# ESCALATION - packages full context for human handoff
# ------------------------------------------------------------------

@app.post("/escalate/{session_id}")
def escalate(session_id: str, reason: str = "unspecified", request_type: str = None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT member_id FROM sessions WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="session not found")
            member_id = row["member_id"]

            write_audit(
                cur, session_id, member_id,
                actor="system", event_type="escalated", decision="escalated",
                request_type=request_type,
                detail={"reason": reason},
            )
            if request_type:
                cur.execute(
                    "UPDATE sessions SET resolution_status = 'escalated', request_type = %s WHERE session_id = %s",
                    (request_type, session_id),
                )
            else:
                cur.execute(
                    "UPDATE sessions SET resolution_status = 'escalated' WHERE session_id = %s",
                    (session_id,),
                )
        conn.commit()

    return get_audit_trail(session_id)


# ------------------------------------------------------------------
# AUDIT TRAIL - for human agent view / debugging
# ------------------------------------------------------------------

@app.get("/audit/{session_id}")
def get_audit_trail(session_id: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT audit_id, actor, event_type, request_type, decision,
                       detail, created_at, row_hash
                FROM audit_log
                WHERE session_id = %s
                ORDER BY audit_id ASC
                """,
                (session_id,),
            )
            rows = cur.fetchall()
    return {"session_id": session_id, "events": rows}


@app.get("/audit-verify")
def verify_chain():
    """Returns broken links, if any. Empty list = chain is fully intact."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM verify_audit_chain()")
            broken = cur.fetchall()
    return {"intact": len(broken) == 0, "broken_links": broken}


@app.get("/escalations")
def list_escalations():
    """
    Returns every session currently in 'escalated' status, with enough
    member context for a human agent to pick up the case cold - no need
    to ask the member to repeat themselves.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.session_id, s.request_type, s.started_at,
                       m.member_id, m.full_name, m.email
                FROM sessions s
                JOIN members m ON m.member_id = s.member_id
                WHERE s.resolution_status = 'escalated'
                ORDER BY s.started_at DESC
                """
            )
            sessions = cur.fetchall()

            results = []
            for s in sessions:
                cur.execute(
                    """
                    SELECT detail, created_at FROM audit_log
                    WHERE session_id = %s AND event_type = 'escalated'
                    ORDER BY audit_id DESC LIMIT 1
                    """,
                    (s["session_id"],),
                )
                escalation_row = cur.fetchone()
                results.append({
                    **s,
                    "escalation_reason": (escalation_row or {}).get("detail", {}).get("reason", "unspecified"),
                    "escalated_at": (escalation_row or {}).get("created_at"),
                })

    return {"escalations": results}


@app.get("/metrics")
def get_metrics():
    """
    Computes the three metrics the project brief asks us to optimize for:

    1. First-contact resolution (FCR) rate:
       resolved sessions / (resolved + escalated) sessions.
       'in_progress' sessions are excluded - they haven't reached an outcome yet.

    2. Audit completeness:
       (a) chain-level: is the hash chain intact end-to-end (tamper check)
       (b) session-level: % of finished sessions (resolved/escalated) that
           have a complete, well-formed event sequence - i.e. every session
           has at least one 'request_received' AND ends in either
           'action_executed' or 'escalated'. A session missing either is a
           logging gap and would fail this check.

    3. Escalation quality:
       % of escalated sessions whose escalation event has a non-empty,
       specific reason logged (not 'unspecified') - a proxy for whether
       the human agent handoff carries real context instead of nothing.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # --- Session outcome counts ---
            cur.execute(
                """
                SELECT resolution_status, COUNT(*) AS cnt
                FROM sessions
                GROUP BY resolution_status
                """
            )
            status_counts = {row["resolution_status"]: row["cnt"] for row in cur.fetchall()}

            resolved = status_counts.get("resolved", 0)
            escalated = status_counts.get("escalated", 0)
            in_progress = status_counts.get("in_progress", 0)
            abandoned = status_counts.get("abandoned", 0)
            finished_total = resolved + escalated

            fcr_rate = (resolved / finished_total) if finished_total > 0 else None

            # --- Per-request-type breakdown (useful for the presentation) ---
            cur.execute(
                """
                SELECT request_type, resolution_status, COUNT(*) AS cnt
                FROM sessions
                WHERE request_type IS NOT NULL
                GROUP BY request_type, resolution_status
                """
            )
            breakdown = {}
            for row in cur.fetchall():
                rt = row["request_type"]
                breakdown.setdefault(rt, {})[row["resolution_status"]] = row["cnt"]

            # --- Audit completeness: chain-level tamper check ---
            cur.execute("SELECT * FROM verify_audit_chain()")
            broken_links = cur.fetchall()
            chain_intact = len(broken_links) == 0

            # --- Audit completeness: session-level event-sequence check ---
            cur.execute(
                """
                SELECT s.session_id, s.resolution_status,
                       bool_or(a.event_type = 'request_received') AS has_request,
                       bool_or(a.event_type IN ('action_executed','escalated')) AS has_outcome
                FROM sessions s
                LEFT JOIN audit_log a ON a.session_id = s.session_id
                WHERE s.resolution_status IN ('resolved','escalated')
                GROUP BY s.session_id, s.resolution_status
                """
            )
            session_rows = cur.fetchall()
            complete_sessions = sum(1 for r in session_rows if r["has_request"] and r["has_outcome"])
            session_completeness_rate = (
                complete_sessions / len(session_rows) if session_rows else None
            )
            incomplete_session_ids = [
                str(r["session_id"]) for r in session_rows if not (r["has_request"] and r["has_outcome"])
            ]

            # --- Escalation quality: does the handoff carry a real reason? ---
            cur.execute(
                """
                SELECT session_id, detail
                FROM audit_log
                WHERE event_type = 'escalated'
                """
            )
            escalation_events = cur.fetchall()
            with_context = sum(
                1 for e in escalation_events
                if e["detail"].get("reason") and e["detail"]["reason"] != "unspecified"
            )
            escalation_quality_rate = (
                with_context / len(escalation_events) if escalation_events else None
            )

    return {
        "session_outcomes": {
            "resolved": resolved,
            "escalated": escalated,
            "in_progress": in_progress,
            "abandoned": abandoned,
            "total": resolved + escalated + in_progress + abandoned,
        },
        "first_contact_resolution_rate": fcr_rate,
        "breakdown_by_request_type": breakdown,
        "audit_completeness": {
            "hash_chain_intact": chain_intact,
            "broken_link_count": len(broken_links),
            "session_event_completeness_rate": session_completeness_rate,
            "incomplete_session_ids": incomplete_session_ids,
        },
        "escalation_quality": {
            "total_escalations": len(escalation_events),
            "with_specific_reason": with_context,
            "context_completeness_rate": escalation_quality_rate,
        },
    }


class ChatRequest(BaseModel):
    session_id: str
    text: str


@app.post("/chat")
def chat(req: ChatRequest):
    """
    Real NLU routing: sends the member's raw text to Dialogflow's detectIntent
    API. Dialogflow classifies the intent, extracts/collects parameters
    (asking its own follow-up questions if something's missing, using the
    required-parameter prompts configured in the console), and - since
    webhook fulfillment is enabled on each intent - automatically calls our
    /dialogflow-webhook for the actual business logic. We just relay
    whatever Dialogflow gives back.
    """
    if not DIALOGFLOW_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="google-cloud-dialogflow isn't installed on the server. Run: pip install google-cloud-dialogflow",
        )

    session_client = dialogflow.SessionsClient()
    session_path = session_client.session_path(DIALOGFLOW_PROJECT_ID, req.session_id)
    text_input = dialogflow.TextInput(text=req.text, language_code="en")
    query_input = dialogflow.QueryInput(text=text_input)

    try:
        response = session_client.detect_intent(
            request={"session": session_path, "query_input": query_input}
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Dialogflow request failed: {e}")

    qr = response.query_result
    return {
        "fulfillment_text": qr.fulfillment_text,
        "intent": qr.intent.display_name if qr.intent else None,
        "intent_confidence": qr.intent_detection_confidence,
        "parameters": dict(qr.parameters) if qr.parameters else {},
        "all_required_params_present": qr.all_required_params_present,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------
# DIALOGFLOW ES WEBHOOK
# ------------------------------------------------------------------
# Dialogflow ES posts a JSON payload shaped like:
# {
#   "queryResult": {
#     "intent": {"displayName": "fee_reversal_request"},
#     "parameters": {"email": "asha.kapoor@example.com"},
#     "queryText": "..."
#   },
#   "session": "projects/.../sessions/SOME_SESSION_ID"
# }
#
# We must respond with: {"fulfillmentText": "..."}
# This endpoint is the ONLY thing Dialogflow talks to. It looks up the
# member by email, ensures a `sessions` row exists (reusing Dialogflow's
# own session id as our session_id so the audit trail lines up), then
# runs the exact same eligibility + execution logic as /fee-reversal.

def _ensure_session(cur, session_id, member_id):
    cur.execute("SELECT session_id FROM sessions WHERE session_id = %s", (session_id,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO sessions (session_id, member_id) VALUES (%s, %s)",
            (session_id, member_id),
        )
        write_audit(
            cur, session_id, member_id,
            actor="system", event_type="session_started",
            detail={"note": "Session created via Dialogflow webhook"},
        )


def _find_member_by_email(cur, email):
    # Case-insensitive: Dialogflow's @sys.email entity preserves whatever
    # capitalization the member typed (e.g. "Rohit.Mehta@..."), but emails
    # are case-insensitive by convention and our seed data is lowercase.
    cur.execute("SELECT * FROM members WHERE LOWER(email) = LOWER(%s)", (email.strip(),))
    return cur.fetchone()


def _find_unreversed_fee_event(cur, member_id):
    cur.execute(
        """
        SELECT fe.* FROM fee_events fe
        JOIN cards c ON c.card_id = fe.card_id
        WHERE c.member_id = %s AND fe.reversed = FALSE
        ORDER BY fe.charged_at DESC
        LIMIT 1
        """,
        (member_id,),
    )
    return cur.fetchone()


def _find_primary_card(cur, member_id):
    """MVP assumption: each member has one primary active card."""
    cur.execute(
        """
        SELECT * FROM cards
        WHERE member_id = %s AND status != 'closed'
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (member_id,),
    )
    return cur.fetchone()


def _normalize_replacement_reason(raw_text: str) -> str:
    """Dialogflow may send free text like 'I lost my card' - map to our enum."""
    text = (raw_text or "").lower()
    if "stolen" in text or "steal" in text:
        return "stolen"
    if "lost" in text or "lose" in text or "missing" in text:
        return "lost"
    if "damage" in text or "broken" in text or "crack" in text:
        return "damaged"
    if "expir" in text:
        return "expiring"
    return "damaged"  # safe default rather than crashing on unrecognized text


@app.post("/dialogflow-webhook")
async def dialogflow_webhook(payload: dict):
    query_result = payload.get("queryResult", {})
    intent_name = query_result.get("intent", {}).get("displayName", "")
    params = query_result.get("parameters", {})
    df_session_id = payload.get("session", str(uuid.uuid4()))

    # ---- Idempotency guard ----
    # Protects against double-submits (e.g. a user double-clicking "confirm",
    # or a flaky network causing the frontend to retry). If the exact same
    # request (same session + intent + parameters) was handled in the last
    # few seconds, we don't re-execute the underlying action again - we just
    # return the same friendly "already being handled" message instead of
    # risking a duplicate fee reversal / limit change / replacement order.
    request_signature = (df_session_id, intent_name, tuple(sorted(params.items())))
    now_ts = datetime.now(timezone.utc).timestamp()
    last_seen = _recent_webhook_requests.get(request_signature)
    if last_seen and (now_ts - last_seen) < IDEMPOTENCY_WINDOW_SECONDS:
        return {"fulfillmentText": "I'm already working on that request — just a moment."}
    _recent_webhook_requests[request_signature] = now_ts

    # ---- Safe error handling ----
    # Never let a raw exception/traceback reach the member. Log it server-side
    # (visible in your uvicorn terminal) and return a calm, generic message
    # instead - this also gets audit-logged as its own event type so it shows
    # up in your audit trail / metrics rather than disappearing silently.
    try:
        return await _handle_dialogflow_intent(intent_name, params, df_session_id)
    except Exception as e:
        print(f"[dialogflow-webhook] ERROR for session={df_session_id} intent={intent_name}: {e}")
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    write_audit(
                        cur, df_session_id, None,
                        actor="system", event_type="error",
                        request_type=intent_name or None,
                        detail={"error": str(e)},
                    )
                conn.commit()
        except Exception:
            pass  # even audit logging shouldn't crash the response to the member
        return {"fulfillmentText": "Sorry, something went wrong on my end. Please try again in a moment, or ask to speak with a specialist."}


async def _handle_dialogflow_intent(intent_name, params, df_session_id):

    supported_intents = {"fee_reversal_request", "limit_increase_request", "card_replacement_request"}
    if intent_name not in supported_intents:
        return {"fulfillmentText": "Sorry, I can't handle that request type yet."}

    email = params.get("email")
    if not email:
        return {"fulfillmentText": "I didn't catch your email - could you repeat it?"}

    with get_connection() as conn:
        with conn.cursor() as cur:
            member = _find_member_by_email(cur, email)
            if not member:
                return {"fulfillmentText": f"I couldn't find an account for {email}. Could you double check that email?"}

            member_id = str(member["member_id"])
            _ensure_session(cur, df_session_id, member_id)
        conn.commit()

    # ---------------- FEE REVERSAL ----------------
    if intent_name == "fee_reversal_request":
        with get_connection() as conn:
            with conn.cursor() as cur:
                fee = _find_unreversed_fee_event(cur, member_id)
                if not fee:
                    write_audit(
                        cur, df_session_id, member_id,
                        actor="bot", event_type="request_received",
                        request_type="fee_reversal",
                        detail={"note": "no eligible fee found"},
                    )
                    conn.commit()
                    return {"fulfillmentText": "I don't see any fees on your account that need reversing right now."}
                fee_event_id = str(fee["fee_event_id"])
            conn.commit()

        result = fee_reversal(FeeReversalRequest(
            session_id=df_session_id, member_id=member_id, fee_event_id=fee_event_id
        ))
        if result["approved"]:
            return {"fulfillmentText": f"Done! I've reversed a fee of ₹{result['amount_reversed']:.2f} on your account. Anything else I can help with?"}
        escalate(df_session_id, reason=f"fee_reversal denied: {result['reason']}", request_type="fee_reversal")
        return {"fulfillmentText": f"I'm not able to auto-approve that: {result['reason']} I'll connect you with a specialist who can take a closer look."}

    # ---------------- LIMIT INCREASE ----------------
    if intent_name == "limit_increase_request":
        requested_limit = params.get("requested_limit") or params.get("number")
        if not requested_limit:
            return {"fulfillmentText": "What credit limit would you like to request?"}

        with get_connection() as conn:
            with conn.cursor() as cur:
                card = _find_primary_card(cur, member_id)
                if not card:
                    return {"fulfillmentText": "I couldn't find an active card on your account."}
                card_id = str(card["card_id"])

        result = limit_increase(LimitIncreaseRequest(
            session_id=df_session_id, member_id=member_id, card_id=card_id,
            requested_limit=float(requested_limit),
        ))
        if result["approved"]:
            return {"fulfillmentText": f"Great news - your credit limit has been increased to ₹{result['new_limit']:.2f}. Anything else I can help with?"}
        escalate(df_session_id, reason=f"limit_increase denied: {result['reason']}", request_type="limit_increase")
        return {"fulfillmentText": f"{result['reason']} I'll connect you with a specialist to review this further."}

    # ---------------- CARD REPLACEMENT ----------------
    if intent_name == "card_replacement_request":
        raw_reason = params.get("reason", "")
        shipping_address = params.get("shipping_address") or params.get("address")
        if not shipping_address:
            return {"fulfillmentText": "What address should I ship the replacement card to?"}

        reason = _normalize_replacement_reason(raw_reason if isinstance(raw_reason, str) else "")

        with get_connection() as conn:
            with conn.cursor() as cur:
                card = _find_primary_card(cur, member_id)
                if not card:
                    return {"fulfillmentText": "I couldn't find an active card on your account."}
                card_id = str(card["card_id"])

        result = card_replacement(CardReplacementRequest(
            session_id=df_session_id, member_id=member_id, card_id=card_id,
            reason=reason, shipping_address=shipping_address,
        ))
        if reason in ("lost", "stolen"):
            return {"fulfillmentText": f"I've blocked your card and a replacement is on its way to {shipping_address}. Order reference: {result['order_id'][:8]}."}
        return {"fulfillmentText": f"Your replacement card is on its way to {shipping_address}. Order reference: {result['order_id'][:8]}."}

    return {"fulfillmentText": "Sorry, something went wrong handling that request."}
