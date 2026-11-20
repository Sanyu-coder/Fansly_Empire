import os
from flask import Blueprint, render_template
from app.config import BASE_DIR

main_bp = Blueprint("main", __name__)


def _load_user_manual_markdown() -> str:
    for p in (
        os.path.join(BASE_DIR, "docs", "USER_MANUAL.md"),
        os.path.join(BASE_DIR, "USER_MANUAL.md"),
    ):
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    return ""


@main_bp.route("/")
def index():
    return render_template("index.html", initial_manual_md=_load_user_manual_markdown())
