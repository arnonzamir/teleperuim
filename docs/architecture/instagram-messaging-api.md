# Instagram Messaging API Service -- Architecture Design Document

**Version**: 1.0
**Date**: 2026-03-09
**Status**: Draft
**Priority**: STABILITY > SIMPLICITY > Features

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Tech Stack Recommendation](#2-tech-stack-recommendation)
3. [Core Architecture](#3-core-architecture)
4. [Container Design](#4-container-design)
5. [Stability Measures](#5-stability-measures)
6. [API Design](#6-api-design)
7. [Risks and Mitigations](#7-risks-and-mitigations)
8. [Appendix: Research Findings](#appendix-research-findings)

---

## 1. Executive Summary

This document describes the architecture for a standalone, containerized Instagram messaging gateway service -- analogous to evolution-api for WhatsApp, but targeting Instagram Direct Messages. The service exposes a REST API for sending and receiving Instagram DMs and delivers incoming messages via webhooks.

**Dual-Mode Architecture**: The service supports two backend modes, selectable per instance:

| Mode | Backend | Use Case | Risk Level |
|------|---------|----------|------------|
| `official` | Meta Instagram Messaging API (Graph API) | Business/Creator accounts with Meta app approval | Low -- fully sanctioned |
| `unofficial` | instagrapi (Private API) | Any account, no Meta approval needed | High -- violates ToS, ban risk |

The operator chooses the mode at instance creation time. The REST API surface is identical regardless of mode, so consumers do not need to care which backend is active.

**Key Design Decisions**:
- Python with FastAPI for the application layer
- Single-instance-per-container model (one Instagram account per container)
- File-based session persistence with optional Redis
- Built-in message queue with rate limiting
- Webhook-based delivery of incoming messages

---

## 2. Tech Stack Recommendation

### Decision: Python + FastAPI

| Criterion | Python (instagrapi) | Node.js (instagram-private-api) |
|-----------|---------------------|---------------------------------|
| Library maturity | instagrapi: actively maintained, 2026-current API parity, built-in challenge resolver, comprehensive DM API | instagram-private-api: original repo stale since 2022, v3 is paid/private, community forks fragmented |
| Official API support | httpx / requests for Graph API calls -- trivial | node-fetch / axios -- trivial |
| DM feature coverage | 15+ DM methods: send text, photo, video, voice, links, story shares, thread management | Basic DM support, realtime via MQTT (complex), media support limited in free version |
| Challenge handling | Built-in challenge_code_handler with SMS/Email support | Manual implementation required |
| Session persistence | Native dump_settings / load_settings to JSON | Manual cookie/state serialization |
| Async support | aiograpi available; FastAPI is natively async | Native async, but library support is weaker |
| Container size | python:3.12-slim ~ 150MB + deps ~ 250MB total | node:20-slim ~ 180MB + deps ~ 220MB total |
| Simplicity | Single library covers everything | Multiple packages needed, more glue code |

**Rationale**: instagrapi is the clear winner for the unofficial backend. It is the only actively maintained library with comprehensive DM support, built-in challenge resolution, and native session persistence. For the official Meta API backend, Python's httpx is more than sufficient. The Node.js ecosystem for Instagram private APIs is fragmented and the best version is paywalled.

### Core Dependencies

```
# Application
fastapi==0.115.*
uvicorn[standard]==0.34.*
pydantic==2.*

# Instagram - Unofficial Backend
instagrapi==2.*

# Instagram - Official Backend
httpx==0.28.*           # For Meta Graph API calls

# Infrastructure
redis==5.*              # Optional: session store, pub/sub
apscheduler==3.*        # Scheduled health checks
python-multipart        # File uploads
pillow                  # Image processing for media sends

# Observability
structlog               # Structured logging
prometheus-client       # Metrics endpoint
```

---

## 3. Core Architecture

### 3.1 High-Level Architecture

```
                                    +---------------------------+
                                    |    Docker Container       |
                                    |                           |
   HTTP Clients ──────────────────> |  FastAPI REST API (:8080) |
                                    |         |                 |
                                    |    Router Layer           |
                                    |    /    |    \            |
                                    |   v     v     v          |
                                    | Session  Msg   Webhook   |
                                    | Mgr    Queue   Emitter   |
                                    |   |      |       |       |
                                    |   v      v       v       |
                                    | +---------------------+  |
                                    | | Instagram Backend    |  |
                                    | | (official/unofficial)|  |
                                    | +---------------------+  |
                                    |         |                 |
                                    |         v                 |
                                    |   Instagram Servers       |
                                    +-----|---------------------+
                                          |
                                    Persistent Volume
                                    (sessions, media cache)
```

### 3.2 Component Breakdown

#### A. REST API Layer (FastAPI)

Single FastAPI application with these router groups:

- `/api/instance` -- Instance lifecycle (status, logout, restart)
- `/api/message` -- Send text, media, retrieve conversations
- `/api/chat` -- List threads, mark read, thread details
- `/api/webhook` -- Configure webhook URLs for this instance

All endpoints are authenticated via API key in the `Authorization` header or `apikey` query parameter (evolution-api compatible).

#### B. Instagram Backend Abstraction

```
AbstractInstagramBackend
    |
    +-- UnofficialBackend (instagrapi)
    |       - Uses Instagram Private API
    |       - Polls for new messages
    |       - Full DM feature set
    |
    +-- OfficialBackend (Meta Graph API)
            - Uses Instagram Messaging API
            - Receives messages via Meta webhooks
            - Limited to Business/Creator accounts
            - 24-hour messaging window constraint
```

Both backends implement the same interface:

```python
class InstagramBackend(Protocol):
    async def login(self, credentials: Credentials) -> bool
    async def send_text(self, user_id: str, text: str) -> Message
    async def send_photo(self, user_id: str, photo_path: Path) -> Message
    async def send_video(self, user_id: str, video_path: Path) -> Message
    async def get_threads(self, limit: int = 20) -> list[Thread]
    async def get_messages(self, thread_id: str, limit: int = 20) -> list[Message]
    async def get_account_status(self) -> AccountStatus
    async def logout(self) -> bool
```

#### C. Session Manager

Responsible for login, session persistence, and reconnection.

**Unofficial mode**:
- On first start: login with username/password, solve challenges if needed
- Serialize session to JSON via `cl.dump_settings()`
- Store session file at `/data/sessions/{instance_id}.json`
- On restart: load session from file, validate with a lightweight API call
- If session is invalid: re-login with credentials

**Official mode**:
- Store long-lived page access token
- Refresh token before expiry (tokens last 60 days)
- No "session" concept -- stateless HTTP calls to Graph API

#### D. Message Queue (Outgoing)

Internal asyncio queue with rate-limiting to comply with Instagram limits.

```
Producer (API endpoints) --> asyncio.Queue --> Consumer (rate-limited sender)
```

**Rate limits enforced**:

| Mode | Limit | Strategy |
|------|-------|----------|
| Unofficial | ~20 msgs/hour (conservative) | 3-second minimum gap between sends, jitter of +/- 1s |
| Official | 200 msgs/hour (Meta limit) | Token bucket, 1 msg per 18 seconds sustained |

The queue persists pending messages to a local SQLite file (`/data/queue.db`) so messages survive container restarts. On startup, unprocessed messages are replayed.

#### E. Incoming Message Polling / Webhook Receiver

**Unofficial mode**: Polling-based.
- A background task polls `direct_threads()` every 10 seconds (configurable)
- Maintains a high-water-mark of last seen message timestamp per thread
- New messages are emitted to configured webhook URLs
- Polling interval increases during quiet periods (adaptive backoff: 10s -> 30s -> 60s)

**Official mode**: Webhook-based.
- FastAPI exposes a `/webhook/meta` endpoint for Meta webhook verification and event reception
- Meta sends real-time message events directly
- The service normalizes and re-emits to the configured application webhook

#### F. Webhook Emitter (Outgoing to Application)

Delivers incoming message events to the operator's application.

```python
# Delivery contract:
# - POST to configured URL with JSON payload
# - Retry 3 times with exponential backoff (2s, 8s, 32s)
# - Store failed deliveries in /data/webhook_failures/ for manual replay
# - Include HMAC-SHA256 signature header for payload verification
```

### 3.3 Data Flow: Sending a Message

```
1. Client POST /api/message/send-text {to: "user_id", text: "hello"}
2. API validates request, authenticates API key
3. Message placed in outgoing queue
4. Queue consumer waits for rate limit window
5. Backend.send_text() called
6. Response returned to queue (stored in queue.db)
7. If client used sync mode (?sync=true): response returned immediately
   If async (default): 202 Accepted with message_id, status via webhook
```

### 3.4 Data Flow: Receiving a Message

```
Unofficial mode:
1. Poller calls direct_threads() on interval
2. New messages detected (timestamp > high water mark)
3. Messages normalized to standard WebhookPayload schema
4. POST to configured webhook URL(s) with HMAC signature
5. High water mark updated

Official mode:
1. Meta POSTs to /webhook/meta
2. Event parsed and validated (signature check)
3. Message normalized to same WebhookPayload schema
4. POST to configured webhook URL(s)
```

---

## 4. Container Design

### 4.1 Dockerfile

```dockerfile
FROM python:3.12-slim AS base

# System dependencies for Pillow and video processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg62-turbo-dev \
    libffi-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Persistent data directory
RUN mkdir -p /data/sessions /data/media /data/webhook_failures

VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
```

**Why single worker**: Instagram session state is held in-process memory (the instagrapi Client object). Multiple workers would each need their own session, causing conflicts. Single worker with async concurrency is sufficient for a messaging gateway.

### 4.2 Docker Compose

```yaml
version: "3.8"

services:
  instagram-api:
    build: .
    container_name: instagram-api
    restart: unless-stopped
    ports:
      - "8080:8080"
    env_file:
      - .env
    volumes:
      - instagram_data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"]
      interval: 30s
      timeout: 10s
      retries: 3
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: "0.5"
        reservations:
          memory: 256M
          cpus: "0.25"

  # Optional: Redis for session storage and pub/sub
  redis:
    image: redis:7-alpine
    container_name: instagram-redis
    restart: unless-stopped
    command: redis-server --appendonly yes --maxmemory 128mb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    ports:
      - "6379:6379"

volumes:
  instagram_data:
  redis_data:
```

### 4.3 Environment Variables

```bash
# === Required ===
INSTAGRAM_BACKEND=unofficial              # "official" or "unofficial"
INSTAGRAM_USERNAME=your_username           # Unofficial mode
INSTAGRAM_PASSWORD=your_password           # Unofficial mode
# OR for official mode:
META_ACCESS_TOKEN=EAAx...                  # Long-lived page access token
META_APP_SECRET=abc123...                  # For webhook signature verification
INSTAGRAM_BUSINESS_ACCOUNT_ID=17841...     # Instagram Business Account ID

API_KEY=your-api-key-here                  # API authentication key

# === Optional ===
WEBHOOK_URL=https://your-app.com/webhook   # Default webhook delivery URL
WEBHOOK_SECRET=hmac-signing-secret         # HMAC-SHA256 secret for webhook payloads
LOG_LEVEL=INFO                             # DEBUG, INFO, WARNING, ERROR
POLL_INTERVAL_SECONDS=10                   # Message polling interval (unofficial)
RATE_LIMIT_PER_HOUR=20                     # Outgoing message rate limit
REDIS_URL=redis://redis:6379/0             # Redis connection (optional)
DATA_DIR=/data                             # Persistent data directory
PROXY_URL=                                 # HTTP proxy for Instagram requests
CHALLENGE_EMAIL_IMAP=                      # IMAP server for email challenge codes
CHALLENGE_EMAIL_USER=                      # Email username for challenge codes
CHALLENGE_EMAIL_PASSWORD=                  # Email password for challenge codes
```

### 4.4 Resource Requirements

| Component | CPU | Memory | Disk |
|-----------|-----|--------|------|
| instagram-api | 0.25-0.5 cores | 256-512 MB | 100 MB app + session data |
| redis (optional) | 0.1 cores | 128 MB | Minimal |
| **Total** | **0.5 cores** | **512 MB** | **< 500 MB** |

---

## 5. Stability Measures

### 5.1 Session Persistence Strategy

```
Startup Sequence:
    1. Check /data/sessions/{instance}.json exists
    2. If yes: load session, call lightweight endpoint to validate
    3. If session valid: enter CONNECTED state
    4. If session invalid or missing: full login flow
    5. On successful login: dump session to file immediately
    6. During operation: re-dump session every 30 minutes (settings can drift)
```

**Session file contents** (instagrapi format): device info, cookies, authorization data, user agent, and other state that Instagram uses to identify the "device." Preserving this prevents Instagram from seeing each restart as a new device login.

### 5.2 Challenge / Checkpoint Handling

Instagram may trigger challenges (verification prompts) during login or operation. The service handles these as follows:

```
Challenge Detected
    |
    +-- Type: EMAIL_CODE
    |       If IMAP credentials configured: auto-fetch code from email
    |       If not: emit webhook event "challenge.required" with instructions
    |       Set instance status to CHALLENGE_REQUIRED
    |
    +-- Type: SMS_CODE
    |       Emit webhook event "challenge.required"
    |       Expose POST /api/instance/challenge endpoint for code submission
    |       Set instance status to CHALLENGE_REQUIRED
    |
    +-- Type: RECAPTCHA / UNKNOWN
            Emit webhook event "challenge.failed"
            Set instance status to BLOCKED
            Log error, do not retry automatically
```

The API exposes a `POST /api/instance/challenge` endpoint so the operator can submit verification codes programmatically when auto-resolution is not possible.

### 5.3 Rate Limiting and Backoff

**Outgoing messages** (unofficial mode):

```
Base gap:        3 seconds between messages
Jitter:          +/- 1 second (random)
Burst penalty:   After 5 messages in 60 seconds, increase gap to 10 seconds for 5 minutes
Hourly ceiling:  Hard stop at configured RATE_LIMIT_PER_HOUR (default: 20)
Daily ceiling:   Hard stop at 100 messages/day (configurable)
Cooldown:        If any 429/throttle response, pause all sends for 5 minutes
```

**Incoming polling** (unofficial mode):

```
Active period:   Poll every POLL_INTERVAL_SECONDS (default: 10)
Quiet period:    If no new messages for 5 minutes, increase to 30s
Very quiet:      If no new messages for 30 minutes, increase to 60s
Reset:           Any new message resets to active period
Error backoff:   On API error, exponential backoff: 30s, 60s, 120s, 300s
```

### 5.4 Automatic Reconnection

```python
# State machine for connection lifecycle:
#
#   DISCONNECTED ──login()──> CONNECTING ──success──> CONNECTED
#        ^                        |                      |
#        |                     failure                error/timeout
#        |                        |                      |
#        +──max retries───── RECONNECTING <──────────────+
#                                 |
#                           challenge──> CHALLENGE_REQUIRED
#                                 |
#                           permanent──> BLOCKED
```

Reconnection policy:
- On transient error: retry immediately, then 5s, 15s, 30s, 60s, 300s
- Maximum 10 consecutive reconnection attempts before entering DISCONNECTED
- On checkpoint/challenge: stop reconnection, emit webhook, wait for operator
- Health check endpoint reports current state

### 5.5 Logging

Structured JSON logging via structlog. Every log entry includes:

```json
{
    "timestamp": "2026-03-09T12:00:00Z",
    "level": "info",
    "event": "message_sent",
    "instance_id": "main",
    "thread_id": "340282366841710300949128139266382528326",
    "message_id": "abc123",
    "duration_ms": 450
}
```

Sensitive data (passwords, session tokens, message content) is never logged. Message content is replaced with `"[REDACTED len=42]"` in logs.

### 5.6 Error Handling Patterns

| Error Type | Response | Recovery |
|------------|----------|----------|
| Network timeout | Retry with backoff | Auto |
| 429 Rate Limited | Pause sends, extend intervals | Auto after cooldown |
| 401 Session Expired | Re-login from credentials | Auto |
| Challenge Required | Pause, notify via webhook | Manual or auto (email) |
| 5xx Instagram Error | Retry with backoff | Auto |
| Login Failed (bad creds) | Return error, set BLOCKED | Manual -- fix credentials |
| Unknown/Unhandled | Log full context, continue | Auto (skip message) |

---

## 6. API Design

### 6.1 Authentication

All API requests must include the API key via one of:

```
Header:  Authorization: Bearer <API_KEY>
Header:  apikey: <API_KEY>
Query:   ?apikey=<API_KEY>
```

This matches evolution-api's authentication pattern.

### 6.2 Endpoints

#### Instance Management

```
GET    /health
       No auth required.
       Response: { "status": "ok", "uptime": 3600 }

GET    /api/instance/status
       Returns current instance state and account info.
       Response:
       {
           "status": "CONNECTED",          // DISCONNECTED | CONNECTING | CONNECTED | CHALLENGE_REQUIRED | BLOCKED
           "backend": "unofficial",
           "account": {
               "username": "myaccount",
               "user_id": "12345678",
               "full_name": "My Account"
           },
           "uptime_seconds": 3600,
           "messages_sent_today": 15,
           "rate_limit_remaining": 5
       }

POST   /api/instance/logout
       Logs out and clears session.
       Response: { "success": true }

POST   /api/instance/restart
       Re-initializes the Instagram connection.
       Response: { "success": true, "status": "CONNECTING" }

POST   /api/instance/challenge
       Submit a challenge verification code.
       Body: { "code": "123456" }
       Response: { "success": true, "status": "CONNECTED" }
```

#### Messaging

```
POST   /api/message/send-text
       Body:
       {
           "to": "target_user_id",          // Instagram user ID or username
           "text": "Hello there"
       }
       Response:
       {
           "success": true,
           "message_id": "abc123",
           "queued": true,                   // true if async, false if sync
           "queue_position": 3
       }

POST   /api/message/send-media
       Content-Type: multipart/form-data
       Fields:
           to: "target_user_id"
           type: "image" | "video"
           file: <binary>
           caption: "optional caption"       // optional
       Response:
       {
           "success": true,
           "message_id": "abc123",
           "queued": true
       }

POST   /api/message/send-media-url
       Body:
       {
           "to": "target_user_id",
           "type": "image" | "video",
           "url": "https://example.com/photo.jpg",
           "caption": "optional"
       }
       Response: same as send-media
```

#### Conversations

```
GET    /api/chat/threads?limit=20
       List recent DM threads.
       Response:
       {
           "threads": [
               {
                   "thread_id": "340282...",
                   "participants": [
                       { "user_id": "123", "username": "john", "full_name": "John Doe" }
                   ],
                   "last_message": {
                       "text": "Hey!",
                       "timestamp": "2026-03-09T11:30:00Z",
                       "from_me": false
                   },
                   "unread_count": 2
               }
           ]
       }

GET    /api/chat/messages/{thread_id}?limit=20
       Retrieve messages from a specific thread.
       Response:
       {
           "thread_id": "340282...",
           "messages": [
               {
                   "message_id": "msg_001",
                   "thread_id": "340282...",
                   "from": {
                       "user_id": "123",
                       "username": "john"
                   },
                   "timestamp": "2026-03-09T11:30:00Z",
                   "type": "text",
                   "text": "Hey!",
                   "media_url": null
               }
           ]
       }
```

#### Webhook Configuration

```
PUT    /api/webhook
       Body:
       {
           "url": "https://your-app.com/webhook",
           "secret": "hmac-secret",
           "events": ["message.received", "message.sent", "status.changed", "challenge.required"]
       }
       Response: { "success": true }

GET    /api/webhook
       Response: { "url": "https://...", "events": [...], "active": true }

DELETE /api/webhook
       Response: { "success": true }
```

### 6.3 Webhook Payload Format

All webhook deliveries use the same envelope:

```
POST {webhook_url}
Headers:
    Content-Type: application/json
    X-Webhook-Signature: sha256=<HMAC-SHA256 of body using secret>
    X-Event-Type: message.received
    X-Instance-Id: main
```

#### message.received

```json
{
    "event": "message.received",
    "instance_id": "main",
    "timestamp": "2026-03-09T11:30:00Z",
    "data": {
        "message_id": "msg_001",
        "thread_id": "340282...",
        "from": {
            "user_id": "123",
            "username": "john",
            "full_name": "John Doe",
            "profile_pic_url": "https://..."
        },
        "type": "text",
        "text": "Hello!",
        "media": null,
        "reply_to_message_id": null
    }
}
```

#### message.sent

```json
{
    "event": "message.sent",
    "instance_id": "main",
    "timestamp": "2026-03-09T11:30:05Z",
    "data": {
        "message_id": "msg_002",
        "thread_id": "340282...",
        "to": {
            "user_id": "123",
            "username": "john"
        },
        "type": "text",
        "text": "Hi John!",
        "status": "delivered"
    }
}
```

#### status.changed

```json
{
    "event": "status.changed",
    "instance_id": "main",
    "timestamp": "2026-03-09T11:35:00Z",
    "data": {
        "previous": "CONNECTED",
        "current": "CHALLENGE_REQUIRED",
        "reason": "Email verification required"
    }
}
```

#### challenge.required

```json
{
    "event": "challenge.required",
    "instance_id": "main",
    "timestamp": "2026-03-09T11:35:00Z",
    "data": {
        "type": "email",
        "contact_point": "j***@gmail.com",
        "instructions": "Submit the verification code via POST /api/instance/challenge"
    }
}
```

### 6.4 Error Response Format

All errors follow a consistent shape:

```json
{
    "success": false,
    "error": {
        "code": "RATE_LIMITED",
        "message": "Hourly message limit reached. Next send available in 1423 seconds.",
        "details": {
            "limit": 20,
            "sent": 20,
            "reset_at": "2026-03-09T13:00:00Z"
        }
    }
}
```

Standard error codes: `UNAUTHORIZED`, `RATE_LIMITED`, `SESSION_EXPIRED`, `CHALLENGE_REQUIRED`, `ACCOUNT_BLOCKED`, `INVALID_REQUEST`, `USER_NOT_FOUND`, `INTERNAL_ERROR`.

---

## 7. Risks and Mitigations

### 7.1 Instagram Account Bans (Unofficial Mode)

**Risk**: HIGH. Instagram banned 1.2 billion accounts in 2025. Using the private API violates Instagram's Terms of Service. Automated messaging is the highest-risk activity.

**Mitigations**:
- Conservative rate limits well below Instagram's detection thresholds (20/hour vs detection at ~60/hour)
- Randomized timing with jitter to avoid pattern detection
- Session persistence to avoid repeated logins (each login is a detection signal)
- Proxy support to avoid datacenter IP flagging (residential proxies recommended)
- Warm-up period: first 7 days after setup, limit to 5 messages/hour
- Recommendation: use aged accounts (>6 months) with established activity history
- **Ultimate mitigation**: use Official mode for production business use cases

### 7.2 Private API Breaking Changes

**Risk**: MEDIUM. Instagram updates its mobile app every 1-2 weeks, potentially breaking the private API contract.

**Mitigations**:
- Pin instagrapi to a specific minor version; do not auto-upgrade
- Health check validates session and DM capability on startup
- Structured error handling catches API signature changes and reports them clearly
- instagrapi maintainers typically patch within days of Instagram updates
- The Official backend mode is entirely immune to this risk

### 7.3 Session Invalidation

**Risk**: MEDIUM. Instagram may invalidate sessions due to: IP changes, suspicious activity, security sweeps, or user password changes.

**Mitigations**:
- Session file stored on persistent volume (survives container restarts)
- Automatic re-login when session validation fails
- Webhook notification on session state changes
- Stable proxy IP recommended to avoid IP-change triggers
- Session re-dump every 30 minutes to capture cookie refreshes

### 7.4 Scaling Limitations

**Risk**: LOW (by design). This architecture intentionally limits to one Instagram account per container.

**Realities**:
- Instagram enforces per-account rate limits; parallelism does not help
- Each account needs its own device fingerprint and session state
- Horizontal scaling = deploy more containers with different accounts

**For multi-account deployments**:

```
                    +-- [instagram-api:8081] Account A
Load Balancer ──────+-- [instagram-api:8082] Account B
                    +-- [instagram-api:8083] Account C
```

Each container is fully independent. A thin orchestration layer (not in scope) can route by account.

### 7.5 Risk Summary Matrix

| Risk | Likelihood | Impact | Mitigation Effectiveness |
|------|-----------|--------|-------------------------|
| Account ban (unofficial) | High | High | Medium -- reduces but cannot eliminate |
| API breaking change | Medium | Medium | High -- library community + official fallback |
| Session invalidation | Medium | Low | High -- auto re-login |
| Scaling bottleneck | Low | Low | High -- horizontal scaling by design |
| Data loss (messages) | Low | Medium | High -- queue persistence + webhook retry |
| Meta API policy change (official) | Low | Medium | Medium -- must comply with policy updates |

---

## Appendix: Research Findings

### A. instagrapi Library Assessment

- Actively maintained for 2026, reverse-engineering verified as of May 2025+
- 15+ Direct Message methods covering text, photo, video, stories, profiles
- Built-in challenge resolver for email (IMAP) and SMS verification
- Native session serialization via `dump_settings()` / `load_settings()`
- Warning from maintainers: "more suits for testing or research than a working business"
- Async variant available: aiograpi

### B. Evolution API Architecture Reference

Evolution API (for WhatsApp) provided the structural inspiration:
- Node.js/Express on port 8080 with API key auth
- Instance-based model (one connection per instance)
- Session stored as JSON in database (Prisma/PostgreSQL)
- Webhook delivery for incoming events
- Supports multiple event transports: webhooks, WebSocket, RabbitMQ, Kafka, SQS
- WAMonitoringService singleton manages all instance lifecycles
- Docker deployment with persistent volumes for instance data

### C. Instagram Anti-Bot Measures (2026)

Five simultaneous detection systems:
1. **Device fingerprinting**: Canvas hash, WebGL, fonts, screen, timezone hashed to device ID
2. **IP reputation scoring**: Datacenter IPs score <10/100; residential mobile IPs score 85-99
3. **Behavioral ML analysis**: Scroll speed, tap patterns, timing consistency
4. **Session metadata tracking**: API call patterns, headers, cookie chains, WebSocket behavior
5. **Cross-account linking**: Shared fingerprints or signals trigger bulk enforcement

Key numbers: 200 DMs/hour official limit, 1.2B accounts banned in 2025.

### D. Meta Official Instagram Messaging API

- Available only for Business and Creator accounts via Meta app review
- 200 messages/hour rate limit
- 24-hour messaging window (can only message users who engaged recently)
- Webhook-based incoming message delivery
- Supports text, images, and structured message templates
- No risk of account ban when used within policy

---

## Sources

- [instagrapi Documentation](https://subzeroid.github.io/instagrapi/)
- [instagrapi GitHub Repository](https://github.com/subzeroid/instagrapi)
- [instagrapi Direct Message API](https://subzeroid.github.io/instagrapi/usage-guide/direct.html)
- [Evolution API GitHub](https://github.com/EvolutionAPI/evolution-api)
- [Evolution API Architecture (DeepWiki)](https://deepwiki.com/EvolutionAPI/evolution-api)
- [Evolution API Docker Documentation](https://doc.evolution-api.com/v2/en/install/docker)
- [Evolution API Webhooks Documentation](https://doc.evolution-api.com/v1/en/configuration/webhooks)
- [Instagram Proxy Guide 2026 - Anti-Bot Measures](https://www.proxies.sx/blog/instagram-proxy-automation-guide-2026)
- [Instagram API Rate Limits: 200 DMs/Hour Explained](https://creatorflow.so/blog/instagram-api-rate-limits-explained/)
- [Instagram Graph API Developer Guide 2026](https://elfsight.com/blog/instagram-graph-api-complete-developer-guide-for-2026/)
- [Meta Instagram Platform Documentation](https://developers.facebook.com/docs/instagram-platform)
- [Node.js instagram-private-api (npm)](https://www.npmjs.com/package/instagram-private-api)
- [How Instagram Handles Automation in 2026](https://storrito.com/resources/instagram-penalizes-aggressive-automation/)
