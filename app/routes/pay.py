import secrets
import uuid

from flask import Blueprint, g, jsonify, request

from app import ledger
from app.auth import require_auth
from app.config import config
from app.extensions import get_supabase
from app.flutterwave_client import FlutterwaveError, flutterwave

pay_bp = Blueprint("pay", __name__)


def _generate_receive_code() -> str:
    # Short, URL-safe, unguessable. Collision odds are negligible, but we
    # still retry on the unique-constraint just in case.
    return secrets.token_urlsafe(9).replace("_", "").replace("-", "")


@pay_bp.get("/wallet/receive-code")
@require_auth
def get_or_create_receive_code():
    """Authenticated. Returns the caller's permanent receive code, creating
    one on first call. No expiry — this is meant to be shared indefinitely
    as a QR code / link (see receive_code column comment in schema.sql).

    Uses upsert (insert-if-missing) rather than a bare UPDATE: a `profiles`
    row is NOT guaranteed to exist for every authenticated user (no signup
    trigger creates one), and `.update().eq("id", user_id)` silently affects
    zero rows when the row doesn't exist yet instead of raising — meaning a
    code could be returned to the frontend that was never actually saved,
    so /pay/<code> would later 404 with "invalid or no longer active" for
    every payer who tried to use it.
    """
    supabase = get_supabase()
    user_id = g.user_id

    existing = (
        supabase.table("profiles").select("receive_code").eq("id", user_id).execute()
    )
    if existing.data and existing.data[0].get("receive_code"):
        return jsonify(receive_code=existing.data[0]["receive_code"]), 200

    for _ in range(5):
        code = _generate_receive_code()
        try:
            supabase.table("profiles").upsert(
                {"id": user_id, "receive_code": code}, on_conflict="id"
            ).execute()
            return jsonify(receive_code=code), 200
        except Exception as exc:  # noqa: BLE001 — likely a unique-constraint collision, retry
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise

    return jsonify(error="code_generation_failed"), 500


@pay_bp.get("/pay/<code>")
def resolve_receive_code(code):
    """Public, no auth. Lets the payer's page show who they're paying
    before they submit anything. Only ever returns non-financial,
    non-sensitive display info — never balances, email, phone, etc."""
    supabase = get_supabase()
    resp = (
        supabase.table("profiles")
        .select("id, display_name, default_currency")
        .eq("receive_code", code)
        .execute()
    )
    if not resp.data:
        return jsonify(error="not_found", message="This payment link is invalid or no longer active"), 404

    profile = resp.data[0]
    return jsonify(
        display_name=profile.get("display_name") or "Fortress user",
        currency=profile.get("default_currency", config.DEFAULT_CURRENCY),
    ), 200


@pay_bp.post("/pay/<code>")
def pay_via_receive_code(code):
    """Public, no auth. The payer needs no account at all — they just supply
    their own payment_method + customer details and an amount they choose
    (point 3: amount is always editable by the payer, never fixed). This
    mirrors /deposit/initiate almost exactly, except the wallet that gets
    credited belongs to the receive-code owner, not whoever is paying.
    """
    supabase = get_supabase()
    resp = (
        supabase.table("profiles").select("id, default_currency").eq("receive_code", code).execute()
    )
    if not resp.data:
        return jsonify(error="not_found", message="This payment link is invalid or no longer active"), 404

    owner_id = resp.data[0]["id"]

    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    payment_method = body.get("payment_method")
    customer = body.get("customer")

    if not amount or amount <= 0:
        return jsonify(error="invalid_amount", message="amount must be a positive number"), 400
    if not payment_method:
        return jsonify(error="missing_payment_method"), 400
    if not customer:
        return jsonify(error="missing_customer"), 400

    currency = body.get("currency", resp.data[0].get("default_currency", config.DEFAULT_CURRENCY))
    reference = f"rcv{uuid.uuid4().hex}"

    ledger.record_pending_entry(
        user_id=owner_id,
        account_type="wallet",
        reserve_id=None,
        entry_kind="receive",
        amount=amount,
        currency=currency,
        reference=reference,
        provider="flutterwave",
        counterparty=customer.get("name", {}).get("first") if isinstance(customer.get("name"), dict) else None,
        meta={"payment_method_type": payment_method.get("type"), "via": "receive_link"},
    )

    try:
        charge = flutterwave.create_direct_charge(
            reference=reference,
            currency=currency,
            amount=amount,
            payment_method=payment_method,
            customer=customer,
            redirect_url=body.get("redirect_url"),
            # Tag the charge with the code OWNER's id, not the payer's — this
            # is what routes the webhook's charge.completed credit to the
            # right wallet, exactly the same way /deposit/initiate does it
            # for a self-deposit.
            meta={"user_id": owner_id},
        )
    except FlutterwaveError as exc:
        ledger.finalize_entry(
            reference=reference, account_type="wallet", reserve_id=None, status="failed"
        )
        return jsonify(error="flutterwave_error", message=str(exc), details=exc.payload), 502

    data = charge.get("data", {})
    return jsonify(
        reference=reference,
        charge_id=data.get("id"),
        status=data.get("status"),
        next_action=data.get("next_action"),
    ), 202
