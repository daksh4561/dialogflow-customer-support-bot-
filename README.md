# CardCare — End-to-End Card Servicing Agent

An AI-powered conversational agent that resolves high-frequency credit card servicing requests — fee reversals, credit limit increases, and card replacements — in a single interaction, backed by a tamper-evident audit trail and automated human escalation.

Built to eliminate routine servicing friction: card members self-resolve common requests end-to-end through natural conversation, while every decision, action, and system call is logged in an immutable, cryptographically verifiable format.

---

## Highlights

- **83% first-contact resolution rate** across automated test conversations, measured live via a built-in metrics API
- **100% audit trail completeness** — every session has a fully logged, unbroken chain of decisions and actions
- **SHA-256 hash-chained audit log** with database-level immutability — Postgres triggers physically reject `UPDATE`/`DELETE` on audit records, making tampering structurally impossible, not just policy-forbidden
- **3 automated servicing flows** (fee reversal, credit limit increase, card replacement) powered by real NLU intent classification via Google Dialogflow ES, not keyword matching
- **Sub-second webhook-driven fulfillment** connecting conversational intent detection directly to backend business logic and a live Postgres-backed card system
- **Real-time human escalation dashboard** — every denied or low-confidence request is automatically routed to a human agent view with full conversational context and audit history, eliminating cold hand-offs

---

## Architecture

```
Card Member
     │
     ▼
Conversational UI (custom chat + Dialogflow NLU)
     │  raw text
     ▼
FastAPI Backend  ──────────────►  Dialogflow ES (intent classification,
     │  ▲                          parameter extraction, multi-turn context)
     │  │ webhook fulfillment
     ▼  │
Business Logic Layer
 (eligibility rules · idempotency guard · identity verification)
     │
     ▼
PostgreSQL
 ├─ members / cards / fee_events / limit_change_requests / replacement_orders
 └─ audit_log (hash-chained, append-only, trigger-enforced immutability)
     │
     ▼
Escalation Dashboard  ·  Metrics Dashboard  ·  Admin Panel
(live, auto-refreshing HTML/JS views into the same Postgres data)
```

---

## Core Features

### 1. Intent Classification & Routing
Member messages are classified in real time via Dialogflow ES NLU (not pattern matching), extracting structured parameters (email, requested amount, reason, address) across multi-turn conversations, with automatic routing to the correct backend resolution flow.

### 2. Conversational Agent Interface
A custom-built chat UI drives the full conversation — greeting handling, graceful cancellation, and a confirm-before-execute step for sensitive actions (credit limit changes, card replacement) so nothing is executed without explicit member confirmation.

### 3. Immutable, Verifiable Audit Trail
Every decision, action, and system call is written to a PostgreSQL `audit_log` table where:
- Each row's hash is derived from its own content **and** the previous row's hash (SHA-256 chain)
- Database triggers block all `UPDATE`/`DELETE` operations — immutability is enforced by the database engine itself, not application code
- A `verify_audit_chain()` function replays the entire chain on demand and returns any tampering — exposed via a `/audit-verify` API endpoint and visualized live in the agent dashboard

### 4. Backend Card System Integration
FastAPI endpoints execute real state changes against a simulated card-of-record system: fee waivers, credit limit adjustments, and card replacement orders (including automatic card blocking for lost/stolen cases) — each gated by eligibility rules (e.g. one fee reversal per 12 months, limit-increase caps tied to account tenure and payment history).

### 5. Human Escalation with Full Context
Denied or uncertain requests are automatically escalated with the complete conversational and decision history attached — no "please repeat your issue" moment for the human agent picking up the case.

### 6. Live Metrics
A `/metrics` endpoint computes exactly what the project is optimized against: first-contact resolution rate, audit completeness (chain integrity + per-session event completeness), and escalation context quality — visualized in a live dashboard.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Conversational AI / NLU | Google Dialogflow ES |
| Backend / API | Python, FastAPI |
| Database | PostgreSQL (hash-chained audit log via triggers + `pgcrypto`) |
| Frontend | HTML / vanilla JS (chat UI, escalation dashboard, metrics dashboard, admin panel) |
| Local tooling | Docker Compose, ngrok / Cloudflare Tunnel (webhook exposure) |

---

## Repository Structure

```
card-servicing-agent/
├── db/
│   └── init.sql              # Schema, hash-chain trigger, verify_audit_chain(), seed data
├── backend/
│   ├── main.py                # FastAPI app: routing, business logic, webhook, metrics
│   ├── audit.py                # Append-only audit log writer
│   ├── database.py              # Postgres connection handling
│   └── requirements.txt
├── frontend/
│   ├── member-chat.html         # Card member conversational interface
│   ├── agent-dashboard.html      # Human escalation handoff view
│   ├── metrics-dashboard.html     # FCR / audit completeness / escalation quality
│   └── admin.html                # Member/card management (no SQL required)
├── docker-compose.yml
└── SETUP.md                        # Full local setup walkthrough
```

---

## Key API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Real NLU-driven conversation entry point |
| `POST /dialogflow-webhook` | Dialogflow fulfillment target — routes to business logic |
| `POST /fee-reversal`, `/limit-increase`, `/card-replacement` | Direct resolution endpoints |
| `POST /execute-pending` | Executes a confirmed action after member says "yes" |
| `POST /escalate/{session_id}` | Manually or automatically triggered human handoff |
| `GET /audit/{session_id}` | Full chronological audit trail for a session |
| `GET /audit-verify` | Cryptographic verification of the entire audit chain |
| `GET /metrics` | FCR rate, audit completeness, escalation quality |
| `POST /admin/members` | Onboard a new card member without touching SQL |

---

## Setup

See [`SETUP.md`](./SETUP.md) for the complete local environment walkthrough (Docker, Postgres, FastAPI, Dialogflow, and tunnel configuration).

---

## Project Context

Built for the **End-to-End Servicing Agent** challenge: design a conversational agent that fully resolves high-frequency card servicing requests in a single interaction, maintains a verifiable audit trail of every decision and action, and hands off to a human agent with complete context when escalation is needed.

---

## Known Limitations / Future Work

- Identity verification is email-based for the demo; a production system would require OTP or session-authenticated login
- Webhook fulfillment relies on a local tunnel (ngrok/Cloudflare) for development; production deployment would use a permanent hosted endpoint (e.g. Cloud Run)
- Multi-card member support is simplified to a single primary card per member
- Rate limiting and idempotency protection are in-memory (single-process); a production deployment would use a shared store (e.g. Redis)
