"""Python-side wrappers around the SQL functions in schema.sql.

Every write goes through Supabase's service-role client calling an RPC
(SECURITY DEFINER Postgres function), never a raw table insert — that keeps
the balance-check-and-write atomic on the DB side and keeps the "only the
backend can move money" guarantee enforced by GRANT/REVOKE in schema.sql
rather than by convention alone.
"""
from app.extensions import get_supabase


def wallet_balance(user_id: str) -> float:
    resp = get_supabase().rpc("wallet_balance", {"p_user_id": user_id}).execute()
    return float(resp.data or 0)


def reserve_balance(reserve_id: str) -> float:
    resp = get_supabase().rpc("reserve_balance", {"p_reserve_id": reserve_id}).execute()
    return float(resp.data or 0)


def get_reserve(reserve_id: str):
    resp = get_supabase().table("reserves").select("*").eq("id", reserve_id).single().execute()
    return resp.data


def record_pending_entry(*, user_id, account_type, reserve_id, entry_kind, amount, currency,
                          reference, provider=None, counterparty=None, meta=None):
    resp = get_supabase().rpc(
        "record_pending_entry",
        {
            "p_user_id": user_id,
            "p_account_type": account_type,
            "p_reserve_id": reserve_id,
            "p_entry_kind": entry_kind,
            "p_amount": amount,
            "p_currency": currency,
            "p_reference": reference,
            "p_provider": provider,
            "p_counterparty": counterparty,
            "p_meta": meta or {},
        },
    ).execute()
    return resp.data


def finalize_entry(*, reference, account_type, reserve_id, status, provider_tx_id=None):
    resp = get_supabase().rpc(
        "finalize_entry",
        {
            "p_reference": reference,
            "p_account_type": account_type,
            "p_reserve_id": reserve_id,
            "p_status": status,
            "p_provider_tx_id": provider_tx_id,
        },
    ).execute()
    return resp.data


def record_completed_entry(*, user_id, account_type, reserve_id, entry_kind, amount, currency,
                            reference, provider=None, provider_tx_id=None, counterparty=None, meta=None):
    resp = get_supabase().rpc(
        "record_completed_entry",
        {
            "p_user_id": user_id,
            "p_account_type": account_type,
            "p_reserve_id": reserve_id,
            "p_entry_kind": entry_kind,
            "p_amount": amount,
            "p_currency": currency,
            "p_reference": reference,
            "p_provider": provider,
            "p_provider_tx_id": provider_tx_id,
            "p_counterparty": counterparty,
            "p_meta": meta or {},
        },
    ).execute()
    return resp.data


def assert_sufficient_wallet_balance(user_id: str, amount: float):
    # Raises (via PostgREST error) if insufficient — let the route catch it.
    get_supabase().rpc(
        "assert_sufficient_wallet_balance", {"p_user_id": user_id, "p_amount": amount}
    ).execute()


def move_wallet_to_reserve(*, user_id, reserve_id, amount, reference, currency):
    get_supabase().rpc(
        "move_wallet_to_reserve",
        {
            "p_user_id": user_id,
            "p_reserve_id": reserve_id,
            "p_amount": amount,
            "p_reference": reference,
            "p_currency": currency,
        },
    ).execute()


def move_reserve_to_wallet(*, user_id, reserve_id, amount, reference, currency):
    get_supabase().rpc(
        "move_reserve_to_wallet",
        {
            "p_user_id": user_id,
            "p_reserve_id": reserve_id,
            "p_amount": amount,
            "p_reference": reference,
            "p_currency": currency,
        },
    ).execute()


def find_ledger_entry_by_reference(reference: str, account_type: str = "wallet"):
    resp = (
        get_supabase()
        .table("ledger_entries")
        .select("*")
        .eq("reference", reference)
        .eq("account_type", account_type)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None
