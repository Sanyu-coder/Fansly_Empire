from app.routes.main_routes import main_bp
from app.routes.character_routes import character_bp
from app.routes.media_routes import media_bp
from app.routes.mosaic_routes import mosaic_bp
from app.routes.worker_routes import worker_bp
from app.routes.queue_routes import queue_bp


def register_routes(app):
    app.register_blueprint(main_bp)
    app.register_blueprint(character_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(mosaic_bp)
    app.register_blueprint(worker_bp)
    app.register_blueprint(queue_bp)
