import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app.config import settings

Image.MAX_IMAGE_PIXELS = settings.max_image_pixels


FORMAT_DETAILS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


class ImageValidationError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class SanitizedImage:
    content: bytes
    content_type: str
    extension: str
    width: int
    height: int


def _safe_mode(image: Image.Image, image_format: str) -> Image.Image:
    if image_format == "JPEG":
        return image.convert("RGB")
    if image.mode in {"RGB", "RGBA", "L", "LA"}:
        return image.copy()
    return image.convert("RGBA" if "transparency" in image.info else "RGB")


def validate_and_sanitize_image(content: bytes, claimed_content_type: str | None) -> SanitizedImage:
    if not content:
        raise ImageValidationError(422, "Файл изображения пуст")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as probe:
                image_format = (probe.format or "").upper()
                if image_format not in FORMAT_DETAILS:
                    raise ImageValidationError(415, "Поддерживаются только JPEG, PNG и WebP")
                expected_content_type, extension = FORMAT_DETAILS[image_format]
                if claimed_content_type != expected_content_type:
                    raise ImageValidationError(422, "Content-Type не соответствует фактическому формату изображения")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ImageValidationError(422, "Анимированные изображения не поддерживаются")
                probe.verify()

            with Image.open(BytesIO(content)) as decoded:
                decoded.load()
                width, height = decoded.size
                if not (
                    settings.min_image_dimension <= width <= settings.max_image_dimension
                    and settings.min_image_dimension <= height <= settings.max_image_dimension
                ):
                    raise ImageValidationError(
                        422,
                        f"Допустимый размер изображения: от {settings.min_image_dimension} до "
                        f"{settings.max_image_dimension} пикселей по каждой стороне",
                    )
                if width * height > settings.max_image_pixels:
                    raise ImageValidationError(422, "Изображение содержит слишком много пикселей")

                clean = _safe_mode(decoded, image_format)
                output = BytesIO()
                save_options: dict[str, object] = {}
                if image_format == "JPEG":
                    save_options = {"quality": 90, "optimize": True}
                elif image_format == "WEBP":
                    save_options = {"quality": 90, "method": 4}
                clean.save(output, format=image_format, **save_options)
                clean.close()
    except ImageValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageValidationError(422, "Изображение отклонено как decompression bomb") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageValidationError(422, "Файл не является корректным изображением") from exc

    return SanitizedImage(
        content=output.getvalue(),
        content_type=expected_content_type,
        extension=extension,
        width=width,
        height=height,
    )
