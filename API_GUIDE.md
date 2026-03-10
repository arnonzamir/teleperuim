# Instagram API Service — Agent Integration Guide

Base URL: `https://<your-coolify-domain>`
Auth: All endpoints (except `/health`) require `Authorization: Bearer <API_KEY>` header.

## Connection Status

```
GET /api/instance/status
```

Response:
```json
{
  "status": "CONNECTED",
  "backend": "unofficial",
  "account": { "username": "myaccount", "user_id": "12345", "full_name": "My Account" },
  "uptime_seconds": 3600,
  "messages_sent_today": 5,
  "rate_limit_remaining": 15
}
```

**Always check `status` is `CONNECTED` before sending messages or posting.** If status is `DISCONNECTED`, `CHALLENGE_REQUIRED`, or `BLOCKED`, operations will fail.

## Send a DM

```
POST /api/message/send-text
Content-Type: application/json

{ "to": "<user_id_or_username>", "text": "Hello!" }
```

Response:
```json
{ "success": true, "message_id": "abc123", "queued": true, "queue_position": 1 }
```

Messages are queued and rate-limited by default (~20/hour). For immediate send, add `?sync=true` query param.

## Send Media via DM

**From file:**
```
POST /api/message/send-media
Content-Type: multipart/form-data

to: <user_id>
type: image | video
file: <binary>
caption: optional text
```

**From URL:**
```
POST /api/message/send-media-url
Content-Type: application/json

{ "to": "<user_id>", "type": "image", "url": "https://example.com/photo.jpg", "caption": "optional" }
```

## Post a Photo to Feed

```
POST /api/post/photo
Content-Type: multipart/form-data

file: <image binary>
caption: optional caption text with #hashtags
```

Response:
```json
{ "success": true, "media_id": "384959267112837", "media_url": "https://..." }
```

**Rate limits for posting:** Keep under 3 posts/day to avoid account flags. Space posts at least 15 minutes apart.

## List DM Threads

```
GET /api/chat/threads?limit=20
```

Returns list of recent DM conversations with last message and participant info.

## Get Messages in a Thread

```
GET /api/chat/messages/<thread_id>?limit=20
```

Returns messages in reverse chronological order.

## Configure Webhooks (Incoming Messages)

```
PUT /api/webhook
Content-Type: application/json

{
  "url": "https://your-system.com/webhook",
  "secret": "hmac-signing-secret",
  "events": ["message.received", "message.sent", "status.changed", "challenge.required"]
}
```

Incoming messages are delivered as POST to your webhook URL with:
- Header `X-Webhook-Signature: sha256=<HMAC-SHA256>` for verification
- Header `X-Event-Type: message.received`

Webhook payload:
```json
{
  "event": "message.received",
  "instance_id": "main",
  "timestamp": "2026-03-10T12:00:00Z",
  "data": {
    "message_id": "msg_001",
    "thread_id": "340282...",
    "from": { "user_id": "123", "username": "john", "full_name": "John Doe" },
    "type": "text",
    "text": "Hello!",
    "media": null
  }
}
```

## Instance Management

```
POST /api/instance/restart     — Reconnect to Instagram
POST /api/instance/logout      — Logout and clear session
POST /api/instance/challenge   — Submit verification code: { "code": "123456" }
```

## Health Check

```
GET /health
```

No auth required. Returns `{ "status": "ok", "uptime": 3600 }`. Use for monitoring.

## Error Format

All errors return:
```json
{
  "success": false,
  "error": {
    "code": "RATE_LIMITED | SESSION_EXPIRED | UNAUTHORIZED | INTERNAL_ERROR",
    "message": "Human-readable description",
    "details": {}
  }
}
```

## Important Constraints

- **Rate limit:** ~20 DMs/hour, ~3 feed posts/day (unofficial API safe thresholds)
- **24h window (official mode only):** Can only message users who messaged you in the last 24 hours
- **No unsolicited DMs at scale:** Instagram actively detects and blocks mass messaging
- **Session persistence:** Sessions survive container restarts. No re-login needed after deploy.
- **Swagger docs:** Available at `/docs` for interactive API exploration
