import json
import uuid

from flask import Blueprint, g, jsonify, request

from app import ledger
from app.auth import require_auth
from app.config import config
from app.flutterwave_client import FlutterwaveError, flutterwave

wallet_bp = Blueprint("wallet", __name__, url_prefix="/wallet")


def _debit_and_transfer(*, entry_kind: str, transfer_type: str):
    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    recipient = body.get("recipient")

    if not amount or amount <= 0:
        return jsonify(error="invalid_amount", message="amount must be a positive number"), 400
    if not recipient or not recipient.get(transfer_type):
        return jsonify(error="missing_recipient", message=f"recipient.{transfer_type} is required"), 400

    currency = body.get("currency", config.DEFAULT_CURRENCY)
    user_id = g.user_id
    reference = f"{entry_kind[:3]}{uuid.uuid4().hex}"

    try:
        ledger.assert_sufficient_wallet_balance(user_id, amount)
    except Exception:
        return jsonify(error="insufficient_funds"), 400

    ledger.record_pending_entry(
        user_id=user_id,
        account_type="wallet",
        reserve_id=None,
        entry_kind=entry_kind,
        amount=-amount,
        currency=currency,
        reference=reference,
        provider="flutterwave",
        counterparty=json.dumps(recipient),
    )

    try:
        transfer = flutterwave.create_direct_transfer(
            reference=reference,
            source_currency=currency,
            destination_currency=currency,
            amount_value=amount,
            transfer_type=transfer_type,
            recipient={
                transfer_type: recipient[transfer_type],
                "name": recipient.get("name"),
            },
        )
        print("transfer : ", transfer)
    except FlutterwaveError as exc:
        print("errror in  _debit_and_transfer", exc) 
        ledger.finalize_entry(
            reference=reference, account_type="wallet", reserve_id=None, status="failed"
        )
        return jsonify(error="flutterwave_error", message=str(exc), details=exc.payload), 502

    data = transfer.get("data", {})
    return jsonify(reference=reference, transfer_id=data.get("id"), status=data.get("status")), 202


@wallet_bp.post("/send")
@require_auth
def wallet_send():
    """Body: { amount, currency?, recipient: { type: 'bank'|'mobile_money'|'wallet', bank/mobile_money/wallet: {...} } }"""
    body = request.get_json(silent=True) or {}
    recipient = body.get("recipient") or {}
    transfer_type = recipient.get("type")
    if transfer_type not in ("bank", "mobile_money", "wallet"):
        return jsonify(error="invalid_recipient_type", message="recipient.type must be bank, mobile_money, or wallet"), 400
    return _debit_and_transfer(entry_kind="send", transfer_type=transfer_type)


@wallet_bp.post("/withdraw")
@require_auth
def wallet_withdraw():
    """Body: { amount, currency?, recipient: { type: 'bank'|'mobile_money', bank/mobile_money: {...} } }"""
    body = request.get_json(silent=True) or {}
    recipient = body.get("recipient") or {}
    transfer_type = recipient.get("type")
    if transfer_type not in ("bank", "mobile_money"):
        return jsonify(error="invalid_recipient_type", message="recipient.type must be bank or mobile_money"), 400
    return _debit_and_transfer(entry_kind="withdrawal", transfer_type=transfer_type)
