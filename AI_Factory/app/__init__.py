from flask import Flask
from app import config
from app.services.character_service import migrate_legacy_structure
from app.routes import register_routes


def create_app():
    app = Flask(__name__, template_folder="templates")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    config.ensure_directories()
    migrate_legacy_structure()
    register_routes(app)

    @app.after_request
    def add_api_no_cache_headers(response):
        from flask import request
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    return app
