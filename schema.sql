-- ============================================================================
-- Reserved Fund — Supabase / Postgres schema
-- ============================================================================
-- Design:
--   * Money is never stored as a mutable balance column. Every change is an
--     append-only row in ledger_entries. A balance is always SUM(amount).
--   * account_type distinguishes a user's Wallet from one of their Reserves.
--     For account_type = 'reserve', reserve_id is set; for 'wallet' it's null.
--   * Only entries with status = 'completed' count toward balance. 'pending'
--     rows exist so we can reserve funds while a Flutterwave charge/transfer
--     is in flight, and 'failed'/'reversed' rows exist for audit history.
--   * All writes happen through SECURITY DEFINER functions owned by a
--     privileged role, called only by the backend using the Supabase
--     service-role key. Authenticated users can only ever SELECT their own
--     rows (enforced by RLS) — they have no INSERT/UPDATE/DELETE grants on
--     these tables at all, so a leaked anon/user JWT can't move money.
-- ============================================================================

create extension if not exists "pgcrypto";

-- ----------------------------------------------------------------------------
-- profiles — one row per Supabase Auth user (extend as needed)
-- ----------------------------------------------------------------------------
create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  display_name text,
  default_currency text not null default 'NGN',
  created_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

create policy "profiles_select_own"
  on public.profiles for select
  using (auth.uid() = id);

-- Users are allowed to update their own display name / currency preference —
-- everything money-related lives in ledger_entries, which has no such policy.
create policy "profiles_update_own_nonfinancial"
  on public.profiles for update
  using (auth.uid() = id)
  with check (auth.uid() = id);

-- ----------------------------------------------------------------------------
-- reserves — a labeled savings goal belonging to a user. Balance itself is
-- derived from ledger_entries, not stored here.
-- ----------------------------------------------------------------------------
create table if not exists public.reserves (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  name text not null,
  target_type text not null check (target_type in ('survival_days', 'amount')),
  target_value numeric(18, 2) not null check (target_value >= 0),
  currency text not null default 'NGN',
  daily_burn_rate numeric(18, 2), -- only meaningful when target_type = 'survival_days'
  archived boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.reserves enable row level security;

create policy "reserves_select_own"
  on public.reserves for select
  using (auth.uid() = user_id);

-- Creating/renaming/re-targeting a reserve never touches a balance, so it's
-- safe to let the frontend do it directly against Supabase.
create policy "reserves_insert_own"
  on public.reserves for insert
  with check (auth.uid() = user_id);

create policy "reserves_update_own"
  on public.reserves for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Deletes are blocked unless the reserve's balance is zero (trigger below).
create policy "reserves_delete_own"
  on public.reserves for delete
  using (auth.uid() = user_id);

-- ----------------------------------------------------------------------------
-- ledger_entries — the single source of truth for every balance
-- ----------------------------------------------------------------------------
create table if not exists public.ledger_entries (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  account_type text not null check (account_type in ('wallet', 'reserve')),
  reserve_id uuid references public.reserves (id),
  entry_kind text not null check (entry_kind in (
    'deposit',            -- Flutterwave collection landed in the wallet
    'withdrawal',         -- Flutterwave payout out of the wallet
    'send',               -- Flutterwave transfer to another person/account
    'receive',            -- credit from someone else's 'send'
    'move_to_reserve',    -- wallet -> reserve (DB-only)
    'move_from_reserve',  -- reserve -> wallet (DB-only)
    'adjustment'          -- manual/reconciliation correction
  )),
  amount numeric(18, 2) not null,       -- signed: credit > 0, debit < 0
  currency text not null default 'NGN',
  status text not null default 'completed'
    check (status in ('pending', 'completed', 'failed', 'reversed')),
  reference text not null,              -- our own idempotency key for this op
  provider text,                        -- e.g. 'flutterwave'
  provider_tx_id text,                  -- Flutterwave charge/transfer id
  counterparty text,                    -- free-form: recipient/sender info
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- One row per (reference, account_type, reserve_id) — lets us safely upsert
-- pending -> completed/failed without creating duplicate movements, and lets
-- move-to/move-from write their wallet leg and reserve leg under the same
-- reference without colliding.
create unique index if not exists ledger_entries_reference_leg_uidx
  on public.ledger_entries (reference, account_type, coalesce(reserve_id, '00000000-0000-0000-0000-000000000000'));

create index if not exists ledger_entries_user_wallet_idx
  on public.ledger_entries (user_id) where account_type = 'wallet';

create index if not exists ledger_entries_reserve_idx
  on public.ledger_entries (reserve_id) where account_type = 'reserve';

alter table public.ledger_entries enable row level security;

create policy "ledger_select_own"
  on public.ledger_entries for select
  using (auth.uid() = user_id);

-- Deliberately NO insert/update/delete policies for ledger_entries: the
-- table is writable only by the service role (which bypasses RLS entirely),
-- i.e. only through the backend's SECURITY DEFINER functions below.

-- ----------------------------------------------------------------------------
-- flutterwave_events — webhook idempotency log
-- ----------------------------------------------------------------------------
create table if not exists public.flutterwave_events (
  webhook_id text primary key,
  event_type text not null,
  payload jsonb not null,
  received_at timestamptz not null default now(),
  processed boolean not null default false,
  processing_note text
);

alter table public.flutterwave_events enable row level security;
-- No policies at all: this table is backend-only, never read by clients.

-- ----------------------------------------------------------------------------
-- Balance helpers
-- ----------------------------------------------------------------------------
create or replace function public.wallet_balance(p_user_id uuid)
returns numeric
language sql
stable
as $$
  select coalesce(sum(amount), 0)
  from public.ledger_entries
  where user_id = p_user_id
    and account_type = 'wallet'
    and status = 'completed';
$$;

create or replace function public.reserve_balance(p_reserve_id uuid)
returns numeric
language sql
stable
as $$
  select coalesce(sum(amount), 0)
  from public.ledger_entries
  where reserve_id = p_reserve_id
    and account_type = 'reserve'
    and status = 'completed';
$$;

-- Block deleting a reserve that still holds money.
create or replace function public.enforce_reserve_delete_zero_balance()
returns trigger
language plpgsql
as $$
begin
  if public.reserve_balance(old.id) <> 0 then
    raise exception 'Cannot delete reserve %: balance is not zero', old.id;
  end if;
  return old;
end;
$$;

drop trigger if exists trg_reserve_delete_zero_balance on public.reserves;
create trigger trg_reserve_delete_zero_balance
  before delete on public.reserves
  for each row execute function public.enforce_reserve_delete_zero_balance();

-- ============================================================================
-- Backend-only write functions (SECURITY DEFINER).
-- Only the service_role should ever call these — enforce that in application
-- code (the anon/authenticated keys never reach these routes), and optionally
-- also `revoke execute ... from authenticated, anon;` below.
-- ============================================================================

-- Insert or no-op a pending leg. Used at the start of deposit/send/withdraw
-- before we know the Flutterwave outcome. Idempotent on
-- (reference, account_type, reserve_id).
create or replace function public.record_pending_entry(
  p_user_id uuid,
  p_account_type text,
  p_reserve_id uuid,
  p_entry_kind text,
  p_amount numeric,
  p_currency text,
  p_reference text,
  p_provider text,
  p_counterparty text,
  p_meta jsonb
) returns public.ledger_entries
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.ledger_entries;
begin
  insert into public.ledger_entries (
    user_id, account_type, reserve_id, entry_kind, amount, currency,
    status, reference, provider, counterparty, meta
  ) values (
    p_user_id, p_account_type, p_reserve_id, p_entry_kind, p_amount, p_currency,
    'pending', p_reference, p_provider, p_counterparty, coalesce(p_meta, '{}'::jsonb)
  )
  on conflict (reference, account_type, coalesce(reserve_id, '00000000-0000-0000-0000-000000000000'))
  do nothing
  returning * into v_row;

  if v_row.id is null then
    select * into v_row from public.ledger_entries
      where reference = p_reference
        and account_type = p_account_type
        and coalesce(reserve_id, '00000000-0000-0000-0000-000000000000')
            = coalesce(p_reserve_id, '00000000-0000-0000-0000-000000000000');
  end if;

  return v_row;
end;
$$;

-- Flip a pending entry to completed/failed once Flutterwave confirms it.
-- Idempotent: calling this twice with the same terminal status is a no-op.
create or replace function public.finalize_entry(
  p_reference text,
  p_account_type text,
  p_reserve_id uuid,
  p_status text,
  p_provider_tx_id text
) returns public.ledger_entries
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.ledger_entries;
begin
  if p_status not in ('completed', 'failed', 'reversed') then
    raise exception 'invalid terminal status %', p_status;
  end if;

  update public.ledger_entries
  set status = p_status,
      provider_tx_id = coalesce(p_provider_tx_id, provider_tx_id),
      updated_at = now()
  where reference = p_reference
    and account_type = p_account_type
    and coalesce(reserve_id, '00000000-0000-0000-0000-000000000000')
        = coalesce(p_reserve_id, '00000000-0000-0000-0000-000000000000')
    and status = 'pending'
  returning * into v_row;

  if v_row.id is null then
    -- Either already finalized (retry/duplicate webhook) or never existed.
    select * into v_row from public.ledger_entries
      where reference = p_reference
        and account_type = p_account_type
        and coalesce(reserve_id, '00000000-0000-0000-0000-000000000000')
            = coalesce(p_reserve_id, '00000000-0000-0000-0000-000000000000');
  end if;

  return v_row;
end;
$$;

-- Direct credit with no pending step (e.g. reconciliation adjustment, or a
-- 'receive' leg for someone else's completed 'send').
create or replace function public.record_completed_entry(
  p_user_id uuid,
  p_account_type text,
  p_reserve_id uuid,
  p_entry_kind text,
  p_amount numeric,
  p_currency text,
  p_reference text,
  p_provider text,
  p_provider_tx_id text,
  p_counterparty text,
  p_meta jsonb
) returns public.ledger_entries
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.ledger_entries;
begin
  insert into public.ledger_entries (
    user_id, account_type, reserve_id, entry_kind, amount, currency,
    status, reference, provider, provider_tx_id, counterparty, meta
  ) values (
    p_user_id, p_account_type, p_reserve_id, p_entry_kind, p_amount, p_currency,
    'completed', p_reference, p_provider, p_provider_tx_id, p_counterparty, coalesce(p_meta, '{}'::jsonb)
  )
  on conflict (reference, account_type, coalesce(reserve_id, '00000000-0000-0000-0000-000000000000'))
  do nothing
  returning * into v_row;

  if v_row.id is null then
    select * into v_row from public.ledger_entries
      where reference = p_reference
        and account_type = p_account_type
        and coalesce(reserve_id, '00000000-0000-0000-0000-000000000000')
            = coalesce(p_reserve_id, '00000000-0000-0000-0000-000000000000');
  end if;

  return v_row;
end;
$$;

-- Wallet -> Reserve. Pure DB reallocation, atomic, balance-checked.
create or replace function public.move_wallet_to_reserve(
  p_user_id uuid,
  p_reserve_id uuid,
  p_amount numeric,
  p_reference text,
  p_currency text
) returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_wallet_balance numeric;
  v_owner uuid;
begin
  if p_amount <= 0 then
    raise exception 'amount must be positive';
  end if;

  select user_id into v_owner from public.reserves where id = p_reserve_id;
  if v_owner is null or v_owner <> p_user_id then
    raise exception 'reserve % does not belong to user %', p_reserve_id, p_user_id;
  end if;

  select public.wallet_balance(p_user_id) into v_wallet_balance;
  if v_wallet_balance < p_amount then
    raise exception 'insufficient wallet balance: have %, need %', v_wallet_balance, p_amount;
  end if;

  insert into public.ledger_entries
    (user_id, account_type, reserve_id, entry_kind, amount, currency, status, reference, provider)
  values
    (p_user_id, 'wallet', null, 'move_to_reserve', -p_amount, p_currency, 'completed', p_reference, 'internal')
  on conflict (reference, account_type, coalesce(reserve_id, '00000000-0000-0000-0000-000000000000')) do nothing;

  insert into public.ledger_entries
    (user_id, account_type, reserve_id, entry_kind, amount, currency, status, reference, provider)
  values
    (p_user_id, 'reserve', p_reserve_id, 'move_to_reserve', p_amount, p_currency, 'completed', p_reference, 'internal')
  on conflict (reference, account_type, coalesce(reserve_id, '00000000-0000-0000-0000-000000000000')) do nothing;
end;
$$;

-- Reserve -> Wallet. Pure DB reallocation, atomic, balance-checked.
create or replace function public.move_reserve_to_wallet(
  p_user_id uuid,
  p_reserve_id uuid,
  p_amount numeric,
  p_reference text,
  p_currency text
) returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_reserve_balance numeric;
  v_owner uuid;
begin
  if p_amount <= 0 then
    raise exception 'amount must be positive';
  end if;

  select user_id into v_owner from public.reserves where id = p_reserve_id;
  if v_owner is null or v_owner <> p_user_id then
    raise exception 'reserve % does not belong to user %', p_reserve_id, p_user_id;
  end if;

  select public.reserve_balance(p_reserve_id) into v_reserve_balance;
  if v_reserve_balance < p_amount then
    raise exception 'insufficient reserve balance: have %, need %', v_reserve_balance, p_amount;
  end if;

  insert into public.ledger_entries
    (user_id, account_type, reserve_id, entry_kind, amount, currency, status, reference, provider)
  values
    (p_user_id, 'reserve', p_reserve_id, 'move_from_reserve', -p_amount, p_currency, 'completed', p_reference, 'internal')
  on conflict (reference, account_type, coalesce(reserve_id, '00000000-0000-0000-0000-000000000000')) do nothing;

  insert into public.ledger_entries
    (user_id, account_type, reserve_id, entry_kind, amount, currency, status, reference, provider)
  values
    (p_user_id, 'wallet', null, 'move_from_reserve', p_amount, p_currency, 'completed', p_reference, 'internal')
  on conflict (reference, account_type, coalesce(reserve_id, '00000000-0000-0000-0000-000000000000')) do nothing;
end;
$$;

-- Balance check used before sending a wallet debit to Flutterwave (send /
-- withdraw). Raises if insufficient; caller (backend) does this check first,
-- then records a pending debit via record_pending_entry.
create or replace function public.assert_sufficient_wallet_balance(
  p_user_id uuid,
  p_amount numeric
) returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_balance numeric;
begin
  select public.wallet_balance(p_user_id) into v_balance;
  if v_balance < p_amount then
    raise exception 'insufficient wallet balance: have %, need %', v_balance, p_amount;
  end if;
end;
$$;

-- Lock down execute grants: only service_role (used exclusively by the
-- backend) may call the write functions. Balance helpers stay readable by
-- authenticated users since they're pure reads over RLS-protected data.
revoke all on function public.record_pending_entry from public;
revoke all on function public.finalize_entry from public;
revoke all on function public.record_completed_entry from public;
revoke all on function public.move_wallet_to_reserve from public;
revoke all on function public.move_reserve_to_wallet from public;
revoke all on function public.assert_sufficient_wallet_balance from public;

grant execute on function public.record_pending_entry to service_role;
grant execute on function public.finalize_entry to service_role;
grant execute on function public.record_completed_entry to service_role;
grant execute on function public.move_wallet_to_reserve to service_role;
grant execute on function public.move_reserve_to_wallet to service_role;
grant execute on function public.assert_sufficient_wallet_balance to service_role;

grant execute on function public.wallet_balance to authenticated, service_role;
grant execute on function public.reserve_balance to authenticated, service_role;
