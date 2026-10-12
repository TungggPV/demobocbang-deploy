# Bóc Băng AI

Repository này tách biệt prototype hiện có với nền tảng production dự kiến.

## Repository structure

- `frontend/` — Production frontend mới.
- `backend/` — FastAPI backend.
- `prototype/` — Legacy HTML/CSS/JS prototype dùng làm UI reference.
- `docs/` — Architecture, API, planning và deployment documentation.
- `agent/` — Context/instructions dành cho coding agents.
- `scripts/` — Development/setup utilities.
- `docker/` — Container/deployment-related configuration.

## Documentation

- [Frontend rebuild plan](docs/planning/frontend-rebuild.md)
- [System architecture](docs/architecture/architecture.md)
- [Provider architecture](docs/architecture/provider-architecture.md)
- [ERD](docs/architecture/erd.md)
- [API specification](docs/architecture/api-spec.md)
- [Deployment design](docs/deployment/deployment.md)
- [Free demo deploy: Vercel + Render + Neon](docs/deployment/DEPLOY_VERCEL_RENDER.md)
- [Implementation plan](docs/planning/implementation-plan.md)

See [agent guidance](agent/README_AGENT.md) before making implementation changes.

## Running locally

### Recommended: run the whole application with Docker Compose

Install Docker Desktop with the Linux container engine running. With the source
and root `.env` configuration, this is all another machine needs: frontend,
backend, PostgreSQL, Redis, Python/Node dependencies and FFmpeg/ffprobe run in
containers. The first build needs internet access to download images/packages.

From the repository root, create `.env` if it does not already exist:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Set a long random `JWT_SECRET` in `.env`. Keep
`FRONTEND_ORIGIN=http://localhost:5173` and configure your Google OAuth, email
and AI provider credentials there as needed. To generate a secret without a
local Python installation:

```powershell
$jwtBytes = New-Object byte[] 48
$jwtRng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$jwtRng.GetBytes($jwtBytes)
[Convert]::ToBase64String($jwtBytes)
$jwtRng.Dispose()
```

Copy the generated value into `JWT_SECRET`, then start the stack:

```powershell
docker compose up --build -d
docker compose ps -a
docker compose logs -f frontend api worker reconciler
```

Compose starts PostgreSQL and Redis, runs `alembic upgrade head` in a one-off
`migrate` container, then starts the API, Dramatiq worker and reconciler.
The frontend starts after the API readiness check passes and serves the built
React application with Nginx at `http://localhost:5173`.
`migrate` exiting with code 0 is expected. If migration fails, inspect
`docker compose logs migrate`; application services wait for it to succeed.

Compose overrides `DATABASE_URL`, `REDIS_URL`, `QUEUE_BACKEND`,
`APP_ENVIRONMENT`, `STORAGE_BACKEND` and `MEDIA_STORAGE_PATH` for this local
stack, so the database/queue URLs in `.env` can remain blank. It uses a new
container database rather than importing an existing host database. The API
is exposed at `http://localhost:8000`; PostgreSQL and Redis remain internal
to the Compose network. API and worker share a persistent volume for uploads,
normalized audio and exports. Database and Redis data also persist in volumes.
The bundled database credentials are for this local stack only.

No separate frontend terminal or `frontend/.env` is needed for Compose.
The frontend image receives only public build arguments from the root `.env`:
`VITE_API_BASE_URL` (default `http://localhost:8000/api/v1`),
`VITE_WS_BASE_URL` (default `ws://localhost:8000/api/v1`), and upload/duration
limits from `MAX_UPLOAD_BYTES` / `MAX_MEDIA_DURATION_SECONDS`. Blank endpoint
values also use these defaults. These URLs are accessed by the browser, so use
`localhost`, not the Docker service name `api`. Backend secrets and local
`frontend/.env` files are excluded from the frontend image.

Open `http://localhost:5173`; API docs are at
`http://localhost:8000/api/v1/docs`. Check API/database readiness with
`http://localhost:8000/api/v1/ready`. Provider credentials are still required
for real STT/AI processing and email delivery; Docker supplies the runtime
dependencies only. If SMTP runs on the host machine, use
`EMAIL_SMTP_HOST=host.docker.internal` with Docker Desktop.

Realtime AssemblyAI defaults to `universal-3-6-pro` in `max_accuracy` mode. The
backend sends the selected meeting language as `language_codes`; automatic mode
is constrained to Vietnamese and English for native code-switching instead of
switching to a less accurate fallback recognizer. Uploaded recordings use the
same two-language steering. The browser captures mono PCM16 audio through an
AudioWorklet. Override the provider model or latency/accuracy preset with
`ASSEMBLYAI_STREAMING_MODEL` and `ASSEMBLYAI_STREAMING_MODE` in the root `.env`.

During realtime recording, users may enable transient VI ↔ EN translation from
the recording panel. Only finalized STT segments are translated; hiding the
translation stops provider calls, and enabling it again backfills untranslated
segments from the current browser session. These realtime translations are not
stored and do not replace the existing saved translation workflow.

After frontend/backend source or dependency changes, rebuild with
`docker compose up --build -d`. To explicitly apply new migrations to an
existing stack, run `docker compose run --rm migrate` before restarting the
application services. After changing backend settings in `.env`, run
`docker compose up -d` to recreate affected containers. Frontend configuration
is baked into the bundle, so after changing its URLs or upload limits run
`docker compose up --build -d`. This setup serves a built frontend; source edits
require a rebuild. For live frontend editing, use the manual Vite setup below
and stop the Compose frontend with `docker compose stop frontend` first to
release port 5173. Stop the stack with `docker compose down`;
this keeps the data. `docker compose down -v` **deletes the local database,
queue and uploaded/exported files**.

### Alternative: run the application directly on the host

The following sections describe the manual setup without the Compose stack.

### Prerequisites

- Python 3.11 or newer (a standard CPython installation with `pip`)
- Node.js with npm
- PostgreSQL reachable from the machine
- FFmpeg and `ffprobe` on `PATH` to validate uploads and normalize audio

A Redis-compatible queue, separate worker, and queue reconciler are required
to process asynchronous jobs. The API never processes media or AI jobs inline.

### Configure the environment

From the repository root, create `.env` from [`.env.example`](.env.example).
Set `DATABASE_URL` to a PostgreSQL database and set a long random `JWT_SECRET`.
`FRONTEND_ORIGIN` defaults to Vite's `http://localhost:5173`; keep it aligned
with the frontend URL for CORS and Google OAuth redirects. `MEDIA_STORAGE_PATH`
is a local filesystem directory for uploaded media.

The `VITE_*` values are public browser configuration. Do not put provider keys,
database URLs, JWT secrets, SMTP credentials, or OAuth client secrets in them.
The frontend also has its own [environment example](frontend/.env.example).

### Enable real Google sign-in

The sign-in button already uses a server-side OAuth 2.0 authorization-code
flow. To enable it for a real Google account:

1. In [Google Cloud Console](https://console.cloud.google.com/), create or
   select a project. Configure its Google Auth Platform branding and audience.
   For local testing, add the Google account you will use as a test user. For
   public use, publish the app; the current flow requests only `openid`,
   `email`, and `profile`.
2. Create an OAuth client of type **Web application**. Add this exact
   authorized redirect URI for local development:

   ```text
   http://localhost:8000/api/v1/auth/google/callback
   ```

   In production, add the public API equivalent, for example
   `https://api.example.com/api/v1/auth/google/callback`. The URI in Google
   Cloud must exactly match `GOOGLE_REDIRECT_URI`, including scheme, host,
   path, port, and trailing slash.
3. Put the generated values in the root `.env` file only (never in
   `frontend/.env` or source control):

   ```dotenv
   GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your-client-secret
   GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback
   ```

4. Restart the API and open the login or registration page. Choose the Google
   button and complete the Google account selection. The account is created or
   linked by verified email and returns to the frontend already signed in.

For production, set `FRONTEND_ORIGIN` to the exact HTTPS frontend origin and
set `GOOGLE_REDIRECT_URI` to the matching HTTPS API callback. Do not use a
browser-embedded OAuth window. Google permits `http` for localhost only; public
callbacks must use HTTPS. See Google's [web-server OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server) and
[audience guidance](https://support.google.com/cloud/answer/15549945).

Email OTP and password reset need an email provider configuration. Leave
provider variables unset when those integrations are not available; their
server-side features report an unavailable/failed state rather than returning
fabricated results.

### Start the application

Open four terminals from a fresh clone:

```powershell
# Terminal 1 — install backend packages and apply the database schema
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
alembic upgrade head
python -m uvicorn app.main:app --reload
```

```powershell
# Terminal 2 — consume queued jobs
cd backend
.\.venv\Scripts\Activate.ps1
dramatiq app.workers.worker
```

```powershell
# Terminal 3 — repair jobs committed during a Redis/API interruption
cd backend
.\.venv\Scripts\Activate.ps1
python -m app.workers.reconciler
```

```powershell
# Terminal 4 — install and run the frontend
cd frontend
copy .env.example .env
npm install
npm run dev
```

The frontend is at `http://localhost:5173`, the API documentation is at
`http://localhost:8000/api/v1/docs`, and health endpoints are exposed under
`/api/v1/health`.

### Two-terminal local demo

For a shorter local demo setup, keep Redis running in Docker and use two
visible PowerShell terminals. The worker runs as a background job in the API
terminal; PostgreSQL, Redis, and the root `.env` configuration are still
required.

```powershell
# Terminal 1 — API plus background queue worker
cd backend
.\.venv\Scripts\Activate.ps1
alembic upgrade head

$backendPath = (Get-Location).Path
$worker = Start-Job -ArgumentList $backendPath -ScriptBlock {
  param($backendPath)
  Set-Location $backendPath
  & .\.venv\Scripts\dramatiq.exe app.workers.worker
}

python -m uvicorn app.main:app --reload
```

```powershell
# Terminal 2 — frontend
cd frontend
npm run dev
```

Open `http://localhost:5173`. After stopping the API with `Ctrl+C`, stop the
background worker from Terminal 1:

```powershell
Stop-Job $worker
Remove-Job $worker
```

The reconciler can be omitted for an ordinary local demo while Redis remains
available. It is responsible for recovering queued jobs after an API or Redis
interruption, so run it for the full four-process setup. If the worker is also
omitted, the UI can load but uploads, AI processing, and exports will not
complete.

### Local smoke test

1. Register, verify the OTP delivered by the configured email provider, then log in.
2. Create a meeting and upload a valid file; `ffprobe` must be installed.
3. View and edit a persisted transcript, rename speakers, add comments, run an
   export and download it once its job completes.
4. If a streaming STT adapter is configured, create a realtime meeting, grant
   microphone access, start recording, confirm partial text, then stop and
   confirm final persisted segments.

The AssemblyAI and Gemini adapters activate only through their respective
backend environment variables. With no batch STT configured uploads fail with
a provider-capability error once claimed; with no translation/LLM provider the endpoints report capability
unavailable. This is a backend capability limitation, not a frontend fallback. If the
frontend cannot reach the API, first verify `VITE_API_BASE_URL`,
`VITE_WS_BASE_URL`, `FRONTEND_ORIGIN`, PostgreSQL connectivity, migrations, and
that the API, Redis, worker, and reconciler are running. A queued media job
means delivery is awaiting Redis/worker recovery; a rejected upload usually means `ffprobe` is absent or the file
violates configured limits.
