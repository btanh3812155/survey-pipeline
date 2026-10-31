import io
import cv2
import numpy as np
from PIL import Image, ImageOps, ImageFile, UnidentifiedImageError
from typing import Tuple

# prevent decompression bombs while allowing normal high-res phone captures
Image.MAX_IMAGE_PIXELS = 35_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = False

# attempt HEIC format registration for native iOS uploads
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass


def sanitize_and_normalize_image(
    raw_bytes: bytes, max_dimension: int = 1024, webp_quality: int = 82
) -> Tuple[bytes, Image.Image]:
    """
    Sanitizes, normalizes orientation, flattens alpha channels,
    and downscales images into lightweight WebP format.

    Guarantees:
    - Zero double-rotation (EXIF metadata stripped upon save)
    - Zero alpha blackout (RGBA, LA, and P modes composited over white canvas)
    - Memory safe (libjpeg draft scaling pre-allocated)
    """
    stream = io.BytesIO(raw_bytes)
    
    try:
        im = Image.open(stream)
        # libjpeg-turbo C-level draft scaling for large JPEG files
        if im.format == "JPEG" and max(im.size) > max_dimension * 2:
            im.draft("RGB", (max_dimension, max_dimension))
            im.load()
    except Exception as e:
        raise ValueError(f"malformed or unreadable image stream: {e}")

    # 1. auto-rotate according to EXIF tag 274
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass  # ignore corrupt maker notes

    # 2. handle alpha channels and palette transparency (the black-out bug fix)
    has_alpha = im.mode in ("RGBA", "LA") or (
        im.mode == "P" and "transparency" in im.info
    )
    if has_alpha:
        alpha_img = im.convert("RGBA")
        canvas = Image.new("RGB", alpha_img.size, (255, 255, 255))
        canvas.paste(alpha_img, mask=alpha_img.split()[3])
        im = canvas
    elif im.mode != "RGB":
        im = im.convert("RGB")

    # 3. downscale using Lanczos to bounded dimension
    if max(im.size) > max_dimension:
        im.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

    # 4. encode to WebP with stripped EXIF (preventing double-rotation downstream)
    output_buf = io.BytesIO()
    im.save(output_buf, format="WEBP", quality=webp_quality, exif=b"")
    webp_bytes = output_buf.getvalue()

    return webp_bytes, im


def compute_laplacian_blur_score(pil_img: Image.Image) -> Tuple[bool, float]:
    """
    Computes blurriness using second-order spatial derivative variance.
    Returns (is_sharp: bool, variance: float).
    """
    cv_img = np.array(pil_img)
    if len(cv_img.shape) == 3:
        gray = cv2.cvtColor(cv_img, cv2.COLOR_RGB2GRAY)
    else:
        gray = cv_img

    variance = cv2.Laplacian(gray, cv2.CV_64F).var()
    # threshold 100.0 is the standard empirical threshold for readable survey text
    is_sharp = bool(variance >= 100.0)
    return is_sharp, float(variance)
