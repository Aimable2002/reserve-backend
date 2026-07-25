import uuid

from flask import Blueprint, g, jsonify, request

from app import ledger
from app.auth import require_auth
from app.config import config

reserve_bp = Blueprint("reserve", __name__, url_prefix="/reserve")


def _validate_reserve_ownership(reserve_id, user_id):
    reserve = ledger.get_reserve(reserve_id)
    if not reserve:
        return None, (jsonify(error="reserve_not_found"), 404)
    if reserve["user_id"] != user_id:
        return None, (jsonify(error="forbidden"), 403)
    return reserve, None


@reserve_bp.post("/move-to")
@require_auth
def move_to_reserve():
    """Body: { reserve_id, amount }. Wallet -> Reserve, DB-only, no Flutterwave call."""
    body = request.get_json(silent=True) or {}
    reserve_id = body.get("reserve_id")
    amount = body.get("amount")

    if not reserve_id or not amount or amount <= 0:
        return jsonify(error="invalid_request", message="reserve_id and a positive amount are required"), 400

    user_id = g.user_id
    reserve, error = _validate_reserve_ownership(reserve_id, user_id)
    if error:
        return error

    currency = body.get("currency", reserve.get("currency", config.DEFAULT_CURRENCY))
    reference = f"mvto_{uuid.uuid4().hex}"

    try:
        ledger.move_wallet_to_reserve(
            user_id=user_id, reserve_id=reserve_id, amount=amount, reference=reference, currency=currency
        )
    except Exception as exc:
        message = str(exc)
        if "insufficient" in message.lower():
            return jsonify(error="insufficient_funds"), 400
        return jsonify(error="move_failed", message=message), 400

    return jsonify(
        reference=reference,
        wallet_balance=ledger.wallet_balance(user_id),
        reserve_balance=ledger.reserve_balance(reserve_id),
    ), 200


@reserve_bp.post("/move-from")
@require_auth
def move_from_reserve():
    """Body: { reserve_id, amount }. Reserve -> Wallet, DB-only, no Flutterwave call."""
    body = request.get_json(silent=True) or {}
    reserve_id = body.get("reserve_id")
    amount = body.get("amount")

    if not reserve_id or not amount or amount <= 0:
        return jsonify(error="invalid_request", message="reserve_id and a positive amount are required"), 400

    user_id = g.user_id
    reserve, error = _validate_reserve_ownership(reserve_id, user_id)
    if error:
        return error

    currency = body.get("currency", reserve.get("currency", config.DEFAULT_CURRENCY))
    reference = f"mvfr_{uuid.uuid4().hex}"

    try:
        ledger.move_reserve_to_wallet(
            user_id=user_id, reserve_id=reserve_id, amount=amount, reference=reference, currency=currency
        )
    except Exception as exc:
        message = str(exc)
        if "insufficient" in message.lower():
            return jsonify(error="insufficient_funds"), 400
        return jsonify(error="move_failed", message=message), 400

    return jsonify(
        reference=reference,
        wallet_balance=ledger.wallet_balance(user_id),
        reserve_balance=ledger.reserve_balance(reserve_id),
    ), 200
