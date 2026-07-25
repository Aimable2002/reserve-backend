from supabase import create_client, Client

from app.config import config

_supabase_client: Client = None


def get_supabase() -> Client:
    """Returns a Supabase client authenticated with the SERVICE ROLE key.

    This bypasses Row Level Security entirely, which is exactly why every
    call site in this backend must independently check that the JWT-derived
    user actually owns whatever they're asking to move (see app/auth.py and
    the ownership checks inside each route / SQL function).
    """
    global _supabase_client
    if _supabase_client is None:
        if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not configured")
        _supabase_client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)
    return _supabase_client
