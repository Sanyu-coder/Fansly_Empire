import os
from flask import Blueprint, send_from_directory
from app.services.character_service import get_char_paths
from app.services.file_service import resolve_queue_folder_path

media_bp = Blueprint("media", __name__)


@media_bp.route("/images/<char_id>/candidates/<folder>/<filename>")
def serve_candidate_image(char_id, folder, filename):
    paths = get_char_paths(char_id)
    return send_from_directory(os.path.join(paths["candidates"], folder), filename)


@media_bp.route("/images/<char_id>/queue/<path:item_key>/<filename>")
def serve_queue_image(char_id, item_key, filename):
    dir_path = resolve_queue_folder_path(char_id, item_key)
    if not dir_path or not os.path.isdir(dir_path):
        return "Not Found", 404
    return send_from_directory(dir_path, filename)
