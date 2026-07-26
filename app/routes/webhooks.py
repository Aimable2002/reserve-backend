import base64
import hashlib
import hmac

from flask import Blueprint, jsonify, request

from app import ledger
from app.config import config
from app.extensions import get_supabase
from app.flutterwave_client import FlutterwaveError, flutterwave

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


def _valid_signature(raw_body: bytes, signature: str) -> bool:
    if not signature or not config.FLW_WEBHOOK_SECRET_HASH:
        return False
    computed = hmac.new(
        config.FLW_WEBHOOK_SECRET_HASH.encode("utf-8"), raw_body, hashlib.sha256
    ).digest()
    computed_b64 = base64.b64encode(computed).decode("utf-8")
    return hmac.compare_digest(computed_b64, signature)


@webhooks_bp.post("/flutterwave")
def flutterwave_webhook():
    raw_body = request.get_data()
    print("raw_body", raw_body)
    signature = request.headers.get("flutterwave-signature", "")
    print("signature :", signature)
    if not _valid_signature(raw_body, signature):
        return jsonify(error="invalid_signature"), 401

    payload = request.get_json(silent=True) or {}
    webhook_id = payload.get("webhook_id") or payload.get("id")
    event_type = payload.get("type")
    data = payload.get("data", {})
    print("data payload :", data)

    if not webhook_id or not event_type:
        return jsonify(status="ignored", reason="malformed_payload"), 200

    supabase = get_supabase()

    # Idempotency: insert-or-detect-duplicate on webhook_id.
    existing = (
        supabase.table("flutterwave_events")
        .select("webhook_id, processed")
        .eq("webhook_id", webhook_id)
        .execute()
    )
    if existing.data:
        return jsonify(status="already_processed"), 200

    supabase.table("flutterwave_events").insert(
        {"webhook_id": webhook_id, "event_type": event_type, "payload": payload}
    ).execute()

    try:
        _process_event(event_type, data)
        supabase.table("flutterwave_events").update({"processed": True}).eq(
            "webhook_id", webhook_id
        ).execute()
    except Exception as exc:  # noqa: BLE001 — log and still 200 so FLW doesn't hammer retries
        supabase.table("flutterwave_events").update(
            {"processed": False, "processing_note": str(exc)[:500]}
        ).eq("webhook_id", webhook_id).execute()

    return jsonify(status="received"), 200


def _process_event(event_type: str, data: dict):
    if event_type == "charge.completed":
        _handle_charge_completed(data)
    elif event_type == "transfer.disburse":
        _handle_transfer_disburse(data)
    # Unhandled event types (refunds, chargebacks, etc.) are logged via the
    # flutterwave_events row above but don't move any ledger balance yet.


def _handle_charge_completed(data: dict):
    reference = data.get("reference")
    charge_id = data.get("id")
    if not reference:
        return

    # Best practice: re-verify against Flutterwave directly rather than
    # trusting the webhook body alone.
    verified_status = data.get("status")
    try:
        verify_resp = flutterwave.get_charge(charge_id)
        verified_status = verify_resp.get("data", {}).get("status", verified_status)
    except FlutterwaveError:
        # Fall back to the webhook's own status if verification call fails;
        # we still only trust a signed webhook, so this is a reasonable
        # degrade-gracefully path rather than silently dropping the deposit.
        pass

    if verified_status == "succeeded":
        ledger.finalize_entry(
            reference=reference, account_type="wallet", reserve_id=None,
            status="completed", provider_tx_id=charge_id,
        )
    elif verified_status in ("failed", "cancelled"):
        ledger.finalize_entry(
            reference=reference, account_type="wallet", reserve_id=None,
            status="failed", provider_tx_id=charge_id,
        )
    # else: still pending/requires_action — leave the ledger entry pending.


def _handle_transfer_disburse(data: dict):
    reference = data.get("reference")
    transfer_id = data.get("id")
    status = data.get("status")
    if not reference:
        return

    if status == "SUCCESSFUL":
        ledger.finalize_entry(
            reference=reference, account_type="wallet", reserve_id=None,
            status="completed", provider_tx_id=transfer_id,
        )
    elif status == "FAILED":
        # The pending debit never counted toward the balance, so failing it
        # simply releases the hold — no reversal credit needed.
        ledger.finalize_entry(
            reference=reference, account_type="wallet", reserve_id=None,
            status="failed", provider_tx_id=transfer_id,
        )
    # PENDING: leave as-is, wait for a terminal webhook.
