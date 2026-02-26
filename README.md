# 🐐 GOATcommish

**The pickup game management platform that replaces your group text, spreadsheet, and reply-all email chains.**

Running a regular pickup game shouldn't require a part-time administrative job. GOATcommish handles the busywork so organizers can focus on playing — and players always know where, when, and whether they're in.

---

## Why GOATcommish?

### 🗂️ Stop Managing Spreadsheets, Start Managing Games

Every organizer knows the pain: someone changes their email, another player moves away but keeps getting messages, a third asks to switch from email to text. Multiply that across 30+ players and you're spending more time on logistics than on the court.

GOATcommish keeps a living roster. Players manage their own contact info, communication preferences, and group membership. When someone wants texts instead of emails, they change it themselves. When someone moves away, they tap "Leave Group." No more maintaining a spreadsheet that's out of date the moment you save it.

### 📣 Smart Notifications That Respect the Pecking Order

Not every player should hear about a game at the same time. Your regulars — the ones who show up rain or shine — deserve first dibs. GOATcommish sends notifications in timed waves:

- **High priority** players get notified immediately and can lock in their spot
- **Standard priority** players are notified after a configurable delay (default: 1 hour)
- **Low priority** players hear about it last (default: 24 hours later), and only if spots remain

Every notification includes full game details — date, time, location — delivered via email, SMS, or both, based on each player's own preference.

### 🎲 Fair Slot Allocation When Demand Exceeds Supply

When you have 25 players who want to play but only 12 spots, how do you decide who's in? GOATcommish offers two approaches:

- **First Come, First Served** — Spots fill in signup order. Fast fingers win.
- **Random Selection** — Everyone signs up during a window, then the system randomly selects who's in. No advantage to being glued to your phone. High-priority players can be guaranteed a spot automatically.

Both algorithms support a configurable player cap and an automatic waitlist. When someone drops out, the next player on the waitlist is promoted and notified instantly.

### 👥 Multiple Groups, One Platform

Run a Tuesday night game and a Saturday morning game with completely different rosters? GOATcommish supports multiple independent groups, each with their own players, settings, locations, and notification schedules. Players can belong to multiple groups, and organizers can manage them all from a single dashboard.

---

## Feature Overview

### For Players
- **Self-service everything** — Update your name, email, phone, and notification preferences anytime
- **Choose how you hear about games** — Email, SMS, both, or none
- **One-tap signup** — Join a game or take yourself off with a single click
- **Waitlist visibility** — See your position and get notified automatically when a spot opens
- **Multi-group membership** — Play in as many groups as you want from one account
- **Email verification** — Secure your account with verified email delivery
- **Leave anytime** — Players can remove themselves from a group without bothering the organizer

### For Organizers
- **Game creation with full control** — Set date, time, location, player cap, and selection algorithm
- **Priority tiers** — Assign high/standard/low priority per player, per group
- **Cascading notifications** — Configurable delays between priority tiers
- **Post-selection management** — Drop or add players after the game is set, with optional notifications
- **Review before publish** — For random selection games, review the selected roster before players are notified. Auto-publishes after a configurable timeout if you forget.
- **Quick add players** — Bulk add players to a game with one click, random selection, or add all
- **Player import** — Paste a CSV (or tab/semicolon-separated list) to bulk-add players to your group
- **Organizer notifications** — Get notified when players sign up or drop out
- **Game editing** — Change the date, time, or location of a game and all signed-up players are notified
- **Game cancellation** — Cancel a game and all players receive a cancellation notice
- **Multiple locations** — Save frequently used venues with addresses for quick game creation

### Platform
- **Mobile-first design** — Built for phones, works everywhere
- **Real-time updates** — Player lists, waitlists, and game status update live
- **Bounce detection** — Automatically flags undeliverable email addresses via SendGrid webhooks
- **SMS opt-out compliance** — Twilio webhook handles STOP/START for SMS preferences
- **Unsubscribe links** — Every email includes one-click unsubscribe and preference management links
- **Privacy-first** — Full privacy policy, terms of service, and SMS terms (CCPA/GDPR/CTIA compliant)
- **Background scheduler** — All timed notifications, auto-selections, and auto-publishes run reliably without manual intervention
- **Admin panel** — Superuser dashboard with full visibility into players, games, groups, and scheduler diagnostics

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python / FastAPI |
| Database | SQLite (aiosqlite) |
| Frontend | Vanilla HTML / Tailwind CSS / JavaScript |
| Email | SendGrid API |
| SMS | Twilio API |
| Scheduler | Built-in async background task |
| Hosting | DigitalOcean / Cloudflare |

---

## Quick Start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` — the frontend is served automatically.
API docs available at `http://localhost:8000/docs` (disabled in production).

### Environment Variables

```bash
# Email (SendGrid)
SENDGRID_API_KEY=your_key
SENDGRID_FROM_EMAIL=hoops@yourdomain.com
SENDGRID_FROM_NAME=GOATcommish

# SMS (Twilio)
TWILIO_ACCOUNT_SID=your_sid
TWILIO_AUTH_TOKEN=your_token
TWILIO_FROM_NUMBER=+15551234567

# App
HOOPS_BASE_URL=https://yourdomain.com
HOOPS_ENV=production
```

### Demo Credentials

| Email | Password | Role |
|---|---|---|
| ben@example.com | pass123 | Organizer |
| mason@example.com | pass123 | Organizer |
| player1@example.com | pass123 | Player |

---

## Configurable Settings (Per Group)

| Setting | Default | Description |
|---------|---------|-------------|
| Default cap | 12 | Maximum players per game |
| Default algorithm | First Come | first_come or random |
| High priority delay | 60 min | Time before standard tier is notified |
| Alternative delay | 1440 min | Additional time before low tier is notified |
| Random wait period | 60 min | Signup window before random selection runs |
| Review before publish | Off | Hold random results for organizer review |
| Auto-publish delay | 30 min | If review is on, auto-publish after this timeout |
| Notify on signup | On | Alert organizers when a player signs up |
| Notify on drop | On | Alert organizers when a player drops |

---

## Project Structure

```
hoops-backend/
├── app/
│   ├── main.py           # FastAPI app — 70+ API endpoints
│   ├── database.py        # SQLite schema, migrations, settings
│   ├── models.py          # Pydantic request/response models
│   ├── algorithms.py      # Selection algorithms (FCFS, random)
│   ├── notifications.py   # Email (SendGrid) + SMS (Twilio) dispatch
│   ├── scheduler.py       # Background job scheduler
│   ├── auth.py            # JWT auth, password hashing, email verification
│   └── security.py        # Rate limiting, input sanitization, CSRF
├── static/
│   ├── index.html         # Single-page app (HTML/JS/CSS)
│   ├── privacy.html       # Privacy policy + terms
│   ├── logo.png           # GOAT mascot branding
│   └── manifest.json      # PWA manifest
└── requirements.txt
```

---

## License

Proprietary. All rights reserved.
