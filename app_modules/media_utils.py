import os
import re
import uuid
from typing import Optional

import cloudinary.uploader
from flask import current_app, url_for
from werkzeug.utils import secure_filename

EMAIL_PATTERN = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)


def should_expose_reset_link() -> bool:
    """Return True when we can surface the reset link in logs/UI (dev mode)."""
    explicit = os.getenv("RESET_SHOW_DEV_LINK")
    if explicit is not None:
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    return bool(current_app.debug)


def is_valid_email(address: str) -> bool:
    """Return True when the supplied email address looks valid."""
    if not address:
        return False
    return EMAIL_PATTERN.match(address.strip()) is not None


def build_media_url(path: Optional[str]) -> str:
    """Return a public URL for either static or uploaded product assets."""
    if not path:
        return url_for("static", filename="images/product-1.jpeg")
    path_str = str(path).strip()
    if not path_str:
        return url_for("static", filename="images/product-1.jpeg")
    if path_str.startswith(("http://", "https://")):
        return path_str

    cleaned = path_str.lstrip("/")
    static_candidate = os.path.join(current_app.static_folder, cleaned)
    if os.path.isfile(static_candidate):
        return url_for("static", filename=cleaned)
    upload_root = current_app.config.get("UPLOAD_ROOT")
    if upload_root:
        upload_candidate = os.path.join(upload_root, cleaned)
        if os.path.isfile(upload_candidate):
            return url_for("serve_upload", filename=cleaned)
    return url_for("static", filename=cleaned)


def allowed_image_file(filename: str) -> bool:
    if not filename:
        return False
    allowed = current_app.config.get("ALLOWED_IMAGE_EXTENSIONS")
    if not allowed:
        allowed = {".jpg", ".jpeg", ".png", ".webp"}
    return os.path.splitext(filename)[1].lower() in allowed


def ensure_cloudinary_ready():
    if not current_app.config.get("CLOUDINARY_READY"):
        raise RuntimeError(
            "Cloudinary is not configured. Please set CLOUDINARY_URL or "
            "CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET."
        )


def upload_product_image(file_storage):
    """Upload a product image to Cloudinary."""
    ensure_cloudinary_ready()
    filename = secure_filename(file_storage.filename or "")
    if not allowed_image_file(filename):
        raise ValueError("Unsupported image format. Allowed: jpg, jpeg, png, webp.")
    unique_id = uuid.uuid4().hex
    folder = current_app.config.get("CLOUDINARY_FOLDER", "products")
    options = {
        "resource_type": "image",
        "overwrite": True,
        "folder": folder,
        "public_id": unique_id,
    }
    upload_result = cloudinary.uploader.upload(file_storage, **options)
    return upload_result.get("secure_url"), upload_result.get("public_id")


def delete_product_image(public_id: Optional[str]):
    """Delete a product asset from Cloudinary."""
    if not public_id:
        return
    try:
        cloudinary.uploader.destroy(public_id, invalidate=True)
    except Exception:
        # We intentionally swallow errors because this is non-blocking cleanup.
        current_app.logger.debug("Failed to delete Cloudinary asset %s", public_id)


def extract_public_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"/upload/(?:v\d+/)?([^/.]+)", url)
    if match:
        return match.group(1)
    return None
