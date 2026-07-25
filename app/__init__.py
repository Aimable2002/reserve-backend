from flask import Flask, jsonify

from app.config import config
from app.routes.deposit import deposit_bp
from app.routes.reserve import reserve_bp
from app.routes.wallet import wallet_bp
from app.routes.webhooks import webhooks_bp


def create_app():
    app = Flask(__name__)

    if config.CORS_ALLOWED_ORIGINS:
        _apply_cors(app)

    app.register_blueprint(deposit_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(wallet_bp)
    app.register_blueprint(reserve_bp)

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app


def _apply_cors(app):
    @app.after_request
    def add_cors_headers(resp):
        origin = None
        from flask import request

        req_origin = request.headers.get("Origin")
        if req_origin in config.CORS_ALLOWED_ORIGINS:
            origin = req_origin
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    @app.before_request
    def handle_preflight():
        from flask import request

        if request.method == "OPTIONS":
            return "", 204
