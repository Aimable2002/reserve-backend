"""Periodic reconciliation: the sum of every user's wallet + reserve
balances in Supabase should equal Flutterwave's actual account balance.

Run this on a schedule (cron, APScheduler, etc.) — it does NOT modify any
data, it only reports discrepancies so a human can investigate. A mismatch
usually means a webhook was missed or a manual DB edit happened outside the
ledger functions.

NOTE: Flutterwave v4's exact "get balance" endpoint wasn't confirmed against
current docs at the time this was written (v3 exposed
`GET /v3/balances`; v4's equivalent should be confirmed in your dashboard's
API reference before wiring this up against a live account). Fill in
`fetch_flutterwave_balance()` once you've confirmed the v4 endpoint/response
shape for your account.
"""
from app.extensions import get_supabase


def sum_all_ledger_balances(currency: str) -> float:
    supabase = get_supabase()
    resp = (
        supabase.table("ledger_entries")
        .select("amount")
        .eq("status", "completed")
        .eq("currency", currency)
        .execute()
    )
    return sum(float(row["amount"]) for row in (resp.data or []))


def fetch_flutterwave_balance(currency: str) -> float:
    raise NotImplementedError(
        "Confirm the v4 balance-retrieval endpoint for your account in the Flutterwave "
        "dashboard/API reference, then implement this to call it and return the "
        "available balance for `currency`."
    )


def reconcile(currency: str = "NGN"):
    ledger_total = sum_all_ledger_balances(currency)
    try:
        flw_total = fetch_flutterwave_balance(currency)
    except NotImplementedError as exc:
        print(f"[reconciliation] SKIPPED: {exc}")
        print(f"[reconciliation] Ledger total for {currency}: {ledger_total}")
        return

    diff = flw_total - ledger_total
    if abs(diff) > 0.01:
        print(
            f"[reconciliation] MISMATCH for {currency}: "
            f"Flutterwave={flw_total} Ledger={ledger_total} diff={diff}"
        )
    else:
        print(f"[reconciliation] OK for {currency}: {ledger_total}")


if __name__ == "__main__":
    reconcile()
