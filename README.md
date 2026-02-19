# 🏀 Hoops — Pickup Basketball Game Manager

FastAPI + SQLite backend with background notification scheduler and security hardening.

## Quick Start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser — the frontend is served automatically.
API docs at `http://localhost:8000/docs` (disabled in production mode).

## Demo Credentials

| Email | Password | Role |
|---|---|---|
| ben@example.com | pass123 | Owner |
| mason@example.com | pass123 | Owner |
| alex@example.com | pass123 | Player |

## Architecture

```
app/
├── main.py            FastAPI app, all routes, middleware wiring, static serving
├── database.py        SQLite schema (11 tables), WAL mode, cleanup helpers
├── auth.py            PBKDF2-SHA512 hashing, JWT with JTI revocation
├── security.py        Rate limiting, lockout, password validation, headers
├── scheduler.py       Background notification worker (asyncio)
├── algorithms.py      Game selection: first-come, random (with priority tiers)
└── notifications.py   Email/SMS/push dispatch (logs to DB, plug in providers)
static/
└── index.html         Single-page app (vanilla JS + Tailwind) wired to API
```

### Frontend → API Wiring

The frontend (`static/index.html`) is a single-page app served by FastAPI. All data flows through `fetch()` calls:

- **Auth**: JWT stored in `localStorage`, attached as `Bearer` token on every request
- **Session**: Token validated against `/api/players/me` on page load; auto-logout on 401
- **Games**: Auto-refreshes every 30 seconds via polling
- **All CRUD**: Every action (signup, drop, create, approve, settings) hits the real API
- **Loading states**: Spinner shown during async operations
- **Error handling**: API errors surface as toast notifications

## Security

### Password Hashing
- **PBKDF2-HMAC-SHA512** with 600,000 iterations (OWASP 2023 recommendation)
- 256-bit random salt per password
- Timing-safe comparison via `hmac.compare_digest` (prevents timing attacks)
- Transparent rehashing: legacy hashes auto-upgrade on next login
- Stored format: `$pbkdf2-sha512$600000$<salt_hex>$<hash_hex>`

### Authentication & Tokens
- JWT tokens with 30-day expiry (long session per spec)
- Each token has a unique JTI (JWT ID) for revocation support
- Token blacklist table (checked on every authenticated request)
- Logout endpoint revokes the current token
- Every request verifies player still exists and is approved in the DB
- Role changes detected — forces re-login if role was modified

### Rate Limiting
- **Login**: 10 attempts per 5 minutes per IP
- **Registration**: 3 per hour per IP
- **Game signup**: 30 per minute per user
- **Global API**: 120 requests per minute per IP
- Sliding-window in-memory limiter (swap to Redis for multi-worker)
- Returns `429 Too Many Requests` with `Retry-After` header

### Account Lockout
- Locks after 5 failed login attempts within 15 minutes
- Lockout duration: 30 minutes
- All attempts logged to `login_attempts` table with IP address
- Successful login resets the lockout window

### HTTP Security Headers
Every response includes:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()`
- `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Cache-Control: no-store` on authenticated responses

### Input Validation
- Pydantic models with length constraints and field validation
- Email format validation
- Phone number validation
- Input sanitization: null bytes, control characters stripped
- File upload size limit (1MB for CSV import, 500 row cap)
- SQL injection prevention: all queries use parameterized statements
- CHECK constraints on all enum columns in the database

### CORS
- Configurable allowed origins via `HOOPS_ALLOWED_ORIGINS` env var
- Restricted to GET/POST/PATCH/DELETE/OPTIONS
- Only `Authorization` and `Content-Type` headers allowed

## Background Scheduler

The scheduler runs as an asyncio task alongside the FastAPI server, polling every 30 seconds.

### What It Does

1. **Delayed Notifications**: Games created with `notify_future_at` stay in `created` phase until the scheduled time, then begin the notification cascade.

2. **Priority Cascade**:
   ```
   Game created
     → Notify HIGH priority players immediately (or at scheduled time)
     → Wait [high_priority_delay] minutes (default: 60)
     → Notify STANDARD priority players
     → Wait [alternative_delay] minutes (default: 1440 / 24h)
     → Notify LOW priority players (only if spots remain)
   ```

3. **Auto-Selection**: For random-algorithm games, automatically runs the selection after the configurable wait period expires.

4. **Cleanup**: Hourly purge of expired token blacklist entries and login attempts older than 7 days.

### Job Tracking

All scheduled work is tracked in the `scheduler_jobs` table:
- Status: `pending` → `running` → `completed` / `failed`
- Owners can view job status via `GET /api/admin/scheduler/jobs`
- Failed jobs include error messages for debugging

### Game Phase Lifecycle

```
created → notifying_high → notifying_standard → notifying_low → signup → active → closed
```

For first-come games, phases transition faster (no signup/selection wait).

## API Reference

### Auth
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/auth/login` | Login (rate limited, lockout protected) |
| POST | `/api/auth/register` | Register (rate limited, password validated) |
| POST | `/api/auth/logout` | Revoke current token |
| POST | `/api/auth/reset-password?email=` | Request password reset |

### Player (Self)
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/players/me` | Get current player info |
| PATCH | `/api/players/me` | Update profile (input validated) |

### Games
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/games` | List visible games (priority-filtered) |
| POST | `/api/games` | Create game (owner only, schedules notifications) |
| POST | `/api/games/{id}/signup` | Sign up (rate limited) |
| POST | `/api/games/{id}/drop` | Drop (promotes waitlist, notifies owners) |
| POST | `/api/games/{id}/run-selection` | Manual selection trigger (owner only) |
| POST | `/api/games/{id}/close` | Close game (owner only) |

### Admin (Owner only)
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/admin/players` | List players |
| GET | `/api/admin/players/pending-count` | Pending approval count |
| PATCH | `/api/admin/players/{id}` | Update priority/role/status |
| POST | `/api/admin/players/{id}/approve` | Approve with priority |
| POST | `/api/admin/players/{id}/deny` | Deny player |
| DELETE | `/api/admin/players/{id}` | Delete player |
| POST | `/api/admin/players/{id}/reset-password` | Reset password |
| POST | `/api/admin/players/add` | Add player |
| POST | `/api/admin/players/import` | Import CSV (size limited) |
| GET | `/api/admin/scheduler/jobs` | View scheduler job status |
| GET | `/api/admin/notifications` | View notification log |

### Settings (Owner only)
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/settings` | Get all settings + locations |
| PATCH | `/api/settings` | Update settings |
| POST | `/api/settings/locations` | Add location |
| DELETE | `/api/settings/locations/{name}` | Remove location |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HOOPS_DB_PATH` | `hoops.db` | SQLite database path |
| `HOOPS_SECRET_KEY` | (random dev key) | JWT signing secret — **set in production** |
| `HOOPS_DEMO_MODE` | `1` | Enables Swagger docs, relaxed password rules |
| `HOOPS_ALLOWED_ORIGINS` | `localhost:*` | CORS allowed origins (comma-separated) |

## Production Checklist

- [ ] Set `HOOPS_SECRET_KEY` to a strong random value
- [ ] Set `HOOPS_DEMO_MODE=0` to disable Swagger and enforce strict passwords
- [ ] Set `HOOPS_ALLOWED_ORIGINS` to your frontend domain(s)
- [ ] Run behind HTTPS (nginx/Caddy) so HSTS header is effective
- [ ] Swap in-memory rate limiter for Redis if running multiple workers
- [ ] Integrate real notification providers (SendGrid, Twilio, FCM)
- [ ] Set up log aggregation for notification and scheduler logs
- [ ] Consider PostgreSQL for higher concurrency needs
