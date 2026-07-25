import uuid

from flask import Blueprint, g, jsonify, request

from app import ledger
from app.auth import require_auth
from app.config import config
from app.flutterwave_client import FlutterwaveError, flutterwave

deposit_bp = Blueprint("deposit", __name__, url_prefix="/deposit")


@deposit_bp.post("/initiate")
@require_auth
def initiate_deposit():
    """Start a Flutterwave charge to fund the caller's wallet.

    Body:
      amount: number (required)
      currency: string (default: config.DEFAULT_CURRENCY)
      payment_method: object (required) — see Flutterwave orchestrator docs,
        e.g. {"type": "card", "card": {...}} or
             {"type": "mobile_money", "mobile_money": {...}}
      customer: object (required) — {"email": ..., "name": {...}, "phone": {...}}
      redirect_url: string (optional) — where to send the user after any
        bank/card redirect step
    """
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

    currency = body.get("currency", config.DEFAULT_CURRENCY)
    reference = f"dep_{uuid.uuid4().hex}"
    user_id = g.user_id

    ledger.record_pending_entry(
        user_id=user_id,
        account_type="wallet",
        reserve_id=None,
        entry_kind="deposit",
        amount=amount,
        currency=currency,
        reference=reference,
        provider="flutterwave",
        meta={"payment_method_type": payment_method.get("type")},
    )

    try:
        charge = flutterwave.create_direct_charge(
            reference=reference,
            currency=currency,
            amount=amount,
            payment_method=payment_method,
            customer=customer,
            redirect_url=body.get("redirect_url"),
            meta={"user_id": user_id},
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
