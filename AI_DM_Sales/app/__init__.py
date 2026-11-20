from flask import Flask

from app.core import config
from app.core.database import init_db
from app.routes import register_routes
from app.services.character_loader import list_character_ids


def create_app(seed_demo: bool = False) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    config.ensure_directories()
    list_character_ids()
    init_db(seed_demo=seed_demo)
    register_routes(app)
    return app


