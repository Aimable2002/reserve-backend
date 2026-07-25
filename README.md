# Reserved Fund — Backend

Thin Flask backend connecting the `reserve-guardian` frontend to Flutterwave
(money movement) and Supabase (source of truth for who owns what). See
`schema.sql` for the full data model — read it first, it documents the
design in comments.

## How it fits together

- **Reads** (balance, reserves, transaction history) happen directly from
  the frontend to Supabase, protected by Row Level Security. This backend
  has no GET/read routes for that data on purpose.
- **Writes** all go through this backend:
  - `POST /deposit/initiate` — start a Flutterwave charge to fund the wallet
  - `POST /webhooks/flutterwave` — Flutterwave's async confirmation
  - `POST /wallet/send` — transfer to another person/account
  - `POST /wallet/withdraw` — payout to bank/mobile money
  - `POST /reserve/move-to` — wallet → reserve (DB-only, no Flutterwave call)
  - `POST /reserve/move-from` — reserve → wallet (DB-only, no Flutterwave call)
- Every write route requires `Authorization: Bearer <supabase-access-token>`
  from the logged-in user. Your Supabase project uses an ECC (P-256) signing
  key, so verification is against the project's public JWKS endpoint — no
  shared secret involved (see `app/auth.py`). The JWT tells the backend *who*
  is asking; it then uses the Supabase **service-role** key to actually
  write. The anon/user key never gets write access to money tables at all
  (enforced by Postgres grants in `schema.sql`, not just by convention).
  On the frontend: use the Supabase client's current session token
  (`supabase.auth.getSession()`, not a token cached at login time) when
  calling this API — the client refreshes it automatically, so as long as
  you read it fresh right before each request you won't send an expired one.
- Balance is never a stored number — it's always `SUM(amount)` over
  `ledger_entries` where `status = 'completed'`. `pending` rows exist so we
  can reserve funds while a Flutterwave call is in flight without a race.

## Setup

1. **Database**: run `schema.sql` against your Supabase project (SQL Editor,
   or `psql "$SUPABASE_DB_URL" -f schema.sql`).
2. **Env vars**: copy `.env.example` to `.env` and fill in:
   - Supabase URL, service-role key, JWT secret (or JWKS URL — see comments
     in `.env.example` for which one your project uses)
   - Flutterwave Client ID/Secret and your dashboard-configured webhook
     secret hash
3. **Install & run**:
   ```bash
   pip install -r requirements.txt
   python run.py
   # or in production:
   gunicorn -w 4 -b 0.0.0.0:8080 run:app
   ```
4. **Webhook**: in the Flutterwave dashboard, point the webhook URL at
   `https://your-backend/webhooks/flutterwave` and set the same secret hash
   in both places.

## What's deliberately NOT built yet

- **Reconciliation** (`app/reconciliation.py`) sums the ledger correctly but
  the Flutterwave-side balance fetch is a stub — v4's exact balance endpoint
  wasn't confirmed against current docs; check your dashboard/API reference
  and fill in `fetch_flutterwave_balance()`.
- **Payment method collection details** (card encryption, mobile money
  provider list, etc.) — `/deposit/initiate` expects the frontend to already
  have built a valid `payment_method` object per the [Flutterwave
  orchestrator docs](https://developer.flutterwave.com/docs/payment-orchestrator-flow);
  this backend passes it through as-is rather than duplicating that logic.
- **P2P Lending** — explicitly deferred per the project plan.
- Rate limiting, request logging/observability, and a job queue for webhook
  processing (currently handled inline, which is fine at low volume but
  should move to a queue before this hits production traffic).

## Frontend integration

The frontend's `src/lib/store.tsx` currently fakes every mutation in memory.
Swap each of these for a `fetch()` to this backend (with the user's Supabase
session token as the Bearer token), and swap all the *read* paths (reserves
list, transaction history, balances) for direct Supabase queries instead of
the mock arrays in `src/lib/reserve-data.ts`:

| store.tsx function     | Replace with                          |
|-------------------------|----------------------------------------|
| `depositToUnallocated`  | `POST /deposit/initiate`               |
| `walletSend`            | `POST /wallet/send`                    |
| `walletToReserve`       | `POST /reserve/move-to`                |
| `reserveToWallet`       | `POST /reserve/move-from`              |
| `withdrawFromReserve`*  | `POST /wallet/withdraw` (after moving reserve→wallet, if withdrawing straight from a reserve) |

\* Depending on the exact UX, a "withdraw from reserve" action may need to
call `move-from` then `withdraw` as two calls, or you may want a combined
endpoint — flag it if you want that added.
