# Backend

FastAPI backend for the Bóc Băng AI modular monolith. Public endpoints are
versioned under `/api/v1`; liveness/readiness, authentication, and the meeting
ownership/ACL slice are currently available. Media processing remains deferred
to a later vertical slice.

## Local development

From `backend/`, create an environment and install the project (including the
test tools), then run:

```powershell
python -m uvicorn app.main:app --reload
pytest
ruff check .
```

The API docs are served at `http://localhost:8000/api/v1/docs`.

`DATABASE_URL` is optional for process startup but required for a ready
deployment and for Alembic migrations. PostgreSQL URLs are normalized to the
`asyncpg` driver. Apply the schema from `backend/` with:

```powershell
alembic upgrade head
```

The initial revision creates the ERD v2 persistence schema, including immutable
transcript and summary version histories. `TEST_DATABASE_URL`, when configured
by a future PostgreSQL integration environment, must point to an isolated test
database; it is never read by the application itself.

STT, translation, and LLM integrations use vendor-neutral contracts in
`app/providers/contracts.py`. Vendor SDKs must remain in adapter modules, never
in domain services. Realtime adapters implement `open_stream(options)` and the
returned `STTStream`; register the adapter factory in `app/providers/stt.py`.
`STT_API_KEY` is available only to that backend factory and is never returned by
the API or sent through WebSocket messages.

## Translation and summaries

`POST /api/v1/meetings/{id}/translations` and `/summaries` validate `edit`
access, the immutable current transcript snapshot, and provider capability,
then return a durable `202` Job. A separate Dramatiq worker claims the job,
persists only normalized successful output, and records a stable failed state
with retryability otherwise. Translation output is segment-aligned and stored
separately; summary regeneration appends an immutable `SummaryVersion`.

Register deployment adapters in `app/providers/translation.py` and
`app/providers/llm.py`. The API never executes provider calls itself. Run the
independent worker with `dramatiq app.workers.worker`.

## Authentication configuration

Email/password authentication requires `DATABASE_URL` and `JWT_SECRET`. Email
verification and password reset only send mail through an explicitly configured
adapter; the recommended production adapter for free Render services is selected with
`EMAIL_PROVIDER=brevo` and requires `EMAIL_API_KEY` plus a verified `EMAIL_FROM` sender.
The SMTP adapter remains available with `EMAIL_PROVIDER=smtp` on hosts that allow outbound
SMTP traffic and requires `EMAIL_FROM` and `EMAIL_SMTP_HOST` (plus optional SMTP credentials).
The application never logs or persists a plaintext OTP.

Google OAuth uses the standard Google authorization-code flow. Set
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` to enable
it. The callback checks a signed, HttpOnly state cookie and redirects to the
configured frontend origin with a URL fragment; the frontend immediately exchanges
the refresh token for its normal session response.

## Meeting ACL

Every meeting endpoint requires a verified bearer token. A meeting is visible
only to its owner or an explicit collaborator. `read`, `comment`, and `edit`
collaborators can read the meeting; `edit` can update meeting metadata; only the
owner can delete a meeting or manage members and share links. A collaborator
must already have an account and can be invited by email or user ID. Share-link
tokens use high-entropy random values, are stored as SHA-256 hashes, and are
returned only by the creation response; deletion revokes rather than exposes or
hard-deletes a link.

## Media upload and jobs

`POST /meetings/{id}/media` returns `202 Accepted` after streaming a multipart file to temporary storage,
enforces `MAX_UPLOAD_BYTES`, validates its readable duration with the `ffprobe`
binary, then persists it through the storage boundary. `STORAGE_BACKEND=local`
uses `MEDIA_STORAGE_PATH` for development and tests. `STORAGE_BACKEND=s3` uses
the configured private S3-compatible bucket (AWS S3, Cloudflare R2, or MinIO)
and fails at startup if its required settings are missing. Database fields store
opaque object keys, never container paths.

For S3 storage, set `S3_ENDPOINT_URL` (optional for AWS),
`S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`, and `S3_REGION`
(use `auto` for R2). Keep the bucket private and put these values only in the
backend deployment environment; never use `VITE_*` variables. Authenticated
playback/download requests return short-lived signed GET URLs in S3 mode; local
development continues to proxy the existing authenticated stream/download flow.

The endpoint commits a persisted `media_processing` job before publishing only
its ID to Redis. `GET /api/v1/jobs/{id}` exposes canonical PostgreSQL state:
`queued`, `processing`, `retrying`, `completed`, or `failed`. A queue publish
failure remains a queued `QUEUE_ENQUEUE_FAILED` job so it can be recovered,
rather than silently discarding durable work.
Ensure `ffprobe` is on the API PATH and both `ffprobe` and `ffmpeg` are on the
worker PATH before enabling uploads.

## Queue worker

Set backend-only `REDIS_URL` and `QUEUE_BACKEND=redis` for both API and worker.
Production rejects a missing Redis URL; there is no inline fallback. Use
`WORKER_MAX_RETRIES` (default 3) and bounded backoff settings for transient
retry delivery. Queue messages contain only `job_id`; workers load inputs from
PostgreSQL and private object storage and use temporary local files only while
processing. The worker needs the same database, storage, provider secrets, and
FFmpeg runtime as the API image, but never a shared filesystem.

Run the recovery loop as a companion production process:

```powershell
python -m app.workers.reconciler
```

It scans bounded batches every `WORKER_RECONCILE_INTERVAL_SECONDS` (15 seconds
by default), republishing queued work after the delivery visibility timeout
(`WORKER_DELIVERY_TIMEOUT_SECONDS`, 300 seconds by default) and only retryable
work whose `next_attempt_at` is due. Delivery is at-least-once; durable
lifecycle claims make duplicate messages safe no-ops.

## Realtime transcription

`WS /api/v1/realtime/{meeting_id}?access_token=<access JWT>` requires `edit`
access and a configured STT provider whose `streaming` capability is true. The
client sends binary audio chunks and optional JSON commands
`recording.pause`, `recording.resume`, and `recording.stop`. It receives
`session.ready`, `transcript.partial`, `transcript.final`, `meeting.status`, or
`error` events. `error` includes a stable code and `retryable` boolean.

Transient realtime translation reuses this socket. The client sends
`translation.enable` with the automatic VI ↔ EN target and any missing logical
segment IDs, or `translation.disable`. The server emits `translation.status`,
`translation.final`, and non-terminal `translation.error` events. Translation
runs in a per-connection background queue so provider latency does not block
audio/STT. Only final segments are translated, requested backfill IDs are
loaded from the meeting's trusted current transcript, and results are not
persisted. The existing asynchronous translation REST APIs remain unchanged.

Only normalized `transcript.final` results create `TranscriptVersion` and
`SegmentVersion` rows; partial results are socket-only. An explicit stop waits
for the provider's final turns and speaker revisions, then changes a meeting
with transcript content to `completed`. A disconnect leaves the meeting in
`processing` so it can reconnect; no detected speech and non-retryable provider
errors change it to `failed`. Realtime sessions do not fabricate upload jobs.

## Production provider adapters

Set `STT_PROVIDER=assemblyai` and `STT_API_KEY` to enable AssemblyAI batch and
realtime STT. The media worker downloads an object to a temporary file,
normalizes it to mono PCM WAV at 16 kHz using `ffmpeg`, stores the normalized object, and persists normalized STT
segments and speaker labels. The browser sends 16 kHz mono PCM16 over the API
WebSocket; the AssemblyAI credential always stays on the server. `ffmpeg` and
`ffprobe` must both be present on PATH. Digital-silent normalized tracks are
rejected before a paid STT request with `MEDIA_NO_AUDIBLE_AUDIO`. Upload media
is accepted only for `source_type=upload`; the realtime WebSocket accepts only
`source_type=realtime`, and a completed realtime meeting cannot be reopened.

Set `TRANSLATION_PROVIDER=gemini` with `TRANSLATION_API_KEY` (or `LLM_API_KEY`)
for segment-aligned VI ↔ EN translations. To fall back to Groq for translation,
set `TRANSLATION_FALLBACK_PROVIDER=groq` with `GROQ_API_KEY`; Groq is called
only after the primary translator returns an error. Set `LLM_PROVIDER=gemini` with
`LLM_API_KEY` for structured `general`, `briefing`, and `voting` summaries.
`GEMINI_MODEL` defaults to `gemini-2.5-flash`; `LLM_MODEL` overrides it for
summary generation. Provider keys are backend-only values.
To fall back from Gemini to Groq, set `LLM_FALLBACK_PROVIDER=groq` and provide
`GROQ_API_KEY`. Groq is called only after the primary provider returns an
error. `GROQ_MODEL` defaults to `openai/gpt-oss-20b` and can be overridden.
Groq may also be the primary summary provider with `LLM_PROVIDER=groq`.
`STT_MODEL` defaults to `universal-3-5-pro` with `universal-2` as the ordered
fallback. `ASSEMBLYAI_STREAMING_MODEL` defaults to `universal-3-6-pro` and is
independent from the batch model. `ASSEMBLYAI_STREAMING_MODE` defaults to
`max_accuracy`; meeting turn silence defaults to 560/2000 ms. Realtime sessions
declare PCM16 encoding, steer to the selected language (or the VI/EN pair in
automatic mode), enable diarization only when requested, and use meeting
title/description as provider context. Uploaded recordings use the same VI/EN
steering in automatic mode instead of making one language guess for the whole
file. Final speaker revisions update the persisted STT segment before
completion. Transcript confidence is averaged from word confidence and never
from turn-end confidence.
Summary requests
require `output_language` set to `vi` or `en`.
`ASSEMBLYAI_API_BASE_URL`, `ASSEMBLYAI_STREAMING_BASE_URL`, and
`GEMINI_API_BASE_URL` / `GROQ_API_BASE_URL` default to their official endpoints
and may be overridden for a regional endpoint, proxy, or integration test.
