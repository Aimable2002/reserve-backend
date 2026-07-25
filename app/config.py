import os


class Config:
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    # ECC (P-256) project = asymmetric signing keys = verify via the public
    # JWKS endpoint, no shared secret involved at all.
    SUPABASE_JWT_JWKS_URL = os.environ.get(
        "SUPABASE_JWT_JWKS_URL", ""
    )  # defaults to f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if left blank
    SUPABASE_JWT_AUDIENCE = os.environ.get("SUPABASE_JWT_AUDIENCE", "authenticated")

    FLW_CLIENT_ID = os.environ.get("FLW_CLIENT_ID", "")
    FLW_CLIENT_SECRET = os.environ.get("FLW_CLIENT_SECRET", "")
    FLW_ENV = os.environ.get("FLW_ENV", "sandbox")
    FLW_API_BASE_SANDBOX = os.environ.get("FLW_API_BASE_SANDBOX", "https://developersandbox-api.flutterwave.com")
    FLW_API_BASE_LIVE = os.environ.get("FLW_API_BASE_LIVE", "https://api.flutterwave.com")
    FLW_TOKEN_URL = os.environ.get(
        "FLW_TOKEN_URL", "https://idp.flutterwave.com/realms/flutterwave/protocol/openid-connect/token"
    )
    FLW_WEBHOOK_SECRET_HASH = os.environ.get("FLW_WEBHOOK_SECRET_HASH", "")

    DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "NGN")
    CORS_ALLOWED_ORIGINS = [
        o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]

    @property
    def FLW_API_BASE(self):
        return self.FLW_API_BASE_LIVE if self.FLW_ENV == "live" else self.FLW_API_BASE_SANDBOX

    @property
    def JWKS_URL(self):
        if self.SUPABASE_JWT_JWKS_URL:
            return self.SUPABASE_JWT_JWKS_URL
        return f"{self.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"


config = Config()
