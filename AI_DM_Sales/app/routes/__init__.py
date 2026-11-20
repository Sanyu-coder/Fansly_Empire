from flask import Flask


def register_routes(app: Flask) -> None:
    from app.routes.dashboard import dashboard_bp
    from app.routes.vault_routes import vault_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(vault_bp)
