"""Thin client for the Flutterwave v4 API.

- Auth: OAuth2 client_credentials against FLW_TOKEN_URL, token cached in
  memory and refreshed a little before it actually expires.
- Collections: the orchestrator `direct-charges` endpoint (creates customer +
  payment method + charge in one call).
- Payouts: the orchestrator `direct-transfers` endpoint.
- Every mutating call gets a fresh X-Trace-Id and the caller-supplied
  X-Idempotency-Key (we pass our own ledger `reference` for this, so retried
  requests can't double-charge/double-pay).
"""
import time
import uuid

import requests

from app.config import config


class FlutterwaveError(Exception):
    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class FlutterwaveClient:
    def __init__(self):
        self._access_token = None
        self._expires_at = 0

    # -- auth ---------------------------------------------------------------
    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 30:
            return self._access_token

        resp = requests.post(
            config.FLW_TOKEN_URL,
            data={
                "client_id": config.FLW_CLIENT_ID,
                "client_secret": config.FLW_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise FlutterwaveError(
                f"Failed to obtain Flutterwave access token: {resp.status_code}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 300)
        return self._access_token

    # -- low-level request ----------------------------------------------------
    def _request(self, method, path, idempotency_key=None, **kwargs):
        url = f"{config.FLW_API_BASE}{path}"
        headers = kwargs.pop("headers", {})
        headers.update(
            {
                "Authorization": f"Bearer {self._get_access_token()}",
                "Content-Type": "application/json",
                "X-Trace-Id": str(uuid.uuid4()),
            }
        )
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key

        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        body = _safe_json(resp)
        if resp.status_code >= 400:
            # Flutterwave v4 nests error detail under `error`, e.g.
            # {"status": "failed", "error": {"type": ..., "code": ..., "message": ..., "validation_errors": [...]}}
            # Older/other shapes sometimes use a top-level "message" instead, so fall back to that.
            error_obj = body.get("error") if isinstance(body, dict) else None
            message = None
            if isinstance(error_obj, dict):
                message = error_obj.get("message")
                if not message and error_obj.get("validation_errors"):
                    message = "; ".join(
                        f"{e.get('field_name')}: {e.get('message')}" for e in error_obj["validation_errors"]
                    )
            if not message and isinstance(body, dict):
                message = body.get("message")
            if not message:
                message = f"Flutterwave error {resp.status_code}"

            raise FlutterwaveError(
                message,
                status_code=resp.status_code,
                payload=body,
            )
        return body

    # -- collections ----------------------------------------------------------
    def create_direct_charge(self, *, reference: str, currency: str, amount, payment_method: dict,
                              customer: dict, redirect_url: str = None, meta: dict = None):
        """POST /orchestration/direct-charges

        `payment_method` and `customer` must follow the shapes documented at
        https://developer.flutterwave.com/docs/payment-orchestrator-flow —
        the frontend collects these fields and passes them straight through
        to /deposit/initiate.
        """
        payload = {
            "reference": reference,
            "currency": currency,
            "amount": amount,
            "payment_method": payment_method,
            "customer": customer,
        }
        if redirect_url:
            payload["redirect_url"] = redirect_url
        if meta:
            payload["meta"] = meta
        return self._request(
            "POST", "/orchestration/direct-charges", idempotency_key=reference, json=payload
        )

    def get_charge(self, charge_id: str):
        return self._request("GET", f"/charges/{charge_id}")

    # -- payouts ----------------------------------------------------------------
    def create_direct_transfer(self, *, reference: str, source_currency: str, destination_currency: str,
                                amount_value, transfer_type: str, recipient: dict, action: str = "instant"):
        """POST /direct-transfers

        transfer_type: 'bank' | 'mobile_money' | 'wallet'
        recipient: shape depends on transfer_type, see
        https://developer.flutterwave.com/docs/direct-transfer-flow
        """
        payload = {
            "action": action,
            "type": transfer_type,
            "reference": reference,
            "payment_instruction": {
                "source_currency": source_currency,
                "destination_currency": destination_currency,
                "amount": {"applies_to": "destination_currency", "value": amount_value},
                "recipient": recipient,
            },
        }
        return self._request("POST", "/direct-transfers", idempotency_key=reference, json=payload)

    def get_transfer(self, transfer_id: str):
        return self._request("GET", f"/transfers/{transfer_id}")


def _safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


flutterwave = FlutterwaveClient()
