"""Configuration. Everything that differs between machines lives here."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "GNLC_DATABASE_URL", "postgresql://gnlc:gnlc@db:5432/gnlc")
    # Images are retained ONLY for wrong_read / no_read / mismatch.
    # Keeping every correct read would be tens of thousands of files that
    # answer no question anyone will ask.
    image_dir: str = os.getenv("GNLC_IMAGE_DIR", "/var/lib/gnlc/images")
    retain_images_for: tuple[str, ...] = ("wrong_read", "no_read")
    export_dir: str = os.getenv("GNLC_EXPORT_DIR", "/var/lib/gnlc/exports")
    backup_dir: str = os.getenv("GNLC_BACKUP_DIR", "/mnt/usb/gnlc-backup")
    backup_interval_minutes: int = int(os.getenv("GNLC_BACKUP_MINUTES", "15"))
    export_interval_minutes: int = int(os.getenv("GNLC_EXPORT_MINUTES", "15"))
    # Max upload after device-side crop and compression.
    max_image_bytes: int = int(os.getenv("GNLC_MAX_IMAGE_BYTES", str(900_000)))
    easyocr_gpu: bool = os.getenv("GNLC_EASYOCR_GPU", "0") == "1"


settings = Settings()
