import io
import warnings
import cv2
import numpy as np
from PIL import Image, ImageOps, ImageFile
from typing import Tuple, Optional

# prevent decompression bombs while allowing normal high-res phone captures
Image.MAX_IMAGE_PIXELS = 35_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = False
warnings.simplefilter("error", Image.DecompressionBombWarning)

# attempt HEIC format registration for native iOS uploads
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass


def sanitize_and_normalize_image(
    raw_bytes: bytes,
    max_short_edge: int = 768,
    max_long_edge: int = 1024,
    max_aspect_ratio: float = 6.0,
    webp_quality: int = 82,
    max_dimension: Optional[int] = None,
) -> Tuple[bytes, Image.Image]:
    """
    Sanitizes, normalizes orientation, flattens alpha channels,
    and downscales images into lightweight WebP format.

    Guarantees:
    - Zero double-rotation (EXIF metadata stripped upon save)
    - Zero alpha blackout (RGBA, LA, and P modes composited over white canvas)
    - Memory safe (libjpeg draft scaling pre-allocated)
    """
    if max_dimension is not None:
        max_short_edge = min(max_short_edge, max_dimension)
        max_long_edge = min(max_long_edge, max_dimension)
    stream = io.BytesIO(raw_bytes)
    
    try:
        im = Image.open(stream)
        # libjpeg-turbo C-level draft scaling for large JPEG files
        if im.format == "JPEG" and min(im.size) > max_short_edge * 2:
            im.draft("RGB", (max_short_edge, max_short_edge))
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
    alpha_mask = None
    if has_alpha:
        alpha_img = im.convert("RGBA")
        alpha_mask = alpha_img.split()[3]
        canvas = Image.new("RGB", alpha_img.size, (255, 255, 255))
        canvas.paste(alpha_img, mask=alpha_mask)
        im = canvas
    elif im.mode != "RGB":
        im = im.convert("RGB")

    # 3. dual-bound aspect-ratio preserving downscale
    # accommodates long tall phone screens (20:9, 21:9) and long receipts without distortion
    w, h = im.size
    long_edge = max(w, h)
    short_edge = min(w, h)

    if short_edge > 0 and (long_edge / short_edge) > max_aspect_ratio:
        raise ValueError(f"tỷ lệ khung hình bất thường ({long_edge / short_edge:.1f}:1), vượt quá ngưỡng cho phép")

    scale_long = max_long_edge / long_edge if long_edge > max_long_edge else 1.0
    scale_short = max_short_edge / short_edge if short_edge > max_short_edge else 1.0
    scale = min(1.0, scale_long, scale_short)

    if scale < 1.0:
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
        if alpha_mask is not None:
            alpha_mask = alpha_mask.resize((new_w, new_h), Image.Resampling.NEAREST)

    if alpha_mask is not None:
        im.info["alpha_mask"] = np.array(alpha_mask)
    # 4. encode to WebP with stripped EXIF (preventing double-rotation downstream)
    output_buf = io.BytesIO()
    im.save(output_buf, format="WEBP", quality=webp_quality, exif=b"")
    webp_bytes = output_buf.getvalue()

    return webp_bytes, im


def compute_laplacian_blur_score(
    pil_img: Image.Image, threshold: float = 40.0
) -> Tuple[bool, float]:
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
    # default threshold 40.0 tolerates softer/slightly blurry mobile phone shots
    is_sharp = bool(variance >= threshold)
    return is_sharp, float(variance)


def detect_image_manipulation(raw_bytes: bytes, pil_img: Image.Image) -> Tuple[bool, str]:
    """
    Detects digital manipulation / Photoshop tampering using:
    1. Metadata forensic analysis (checks for editing software signatures).
    2. Error Level Analysis (ELA) variance across compression boundaries.
    """
    # 1. EXIF metadata software tag check
    try:
        stream = io.BytesIO(raw_bytes)
        raw_im = Image.open(stream)
        exif = raw_im.getexif()
        if exif:
            software = str(exif.get(305, "")).lower()  # tag 305 = Software
            banned_software = [
                "photoshop", "canva", "gimp", "picsart", "lightroom",
                "snapseed", "meitu", "photoscape", "facetune", "procreate"
            ]
            for bad in banned_software:
                if bad in software:
                    return True, f"phát hiện metadata phần mềm chỉnh sửa: {software}"
    except Exception:
        pass

    # 2. Error Level Analysis (ELA)
    try:
        ela_buf = io.BytesIO()
        pil_img.save(ela_buf, "JPEG", quality=95)
        ela_buf.seek(0)
        resaved_im = Image.open(ela_buf)

        # calculate pixel difference
        diff = np.abs(np.array(pil_img.convert("RGB"), dtype=np.int16) - np.array(resaved_im, dtype=np.int16))
        ela_max = np.max(diff)
        ela_mean = np.mean(diff)

        # localized extreme error spike indicates spliced/tampered region
        if ela_max > 85 and ela_mean < 8.0:
            return True, "phát hiện bất thường mức nén điểm ảnh cục bộ (ELA splice)"
    except Exception:
        pass

    return False, "clean"


def detect_optical_underexposure(pil_img: Image.Image) -> Tuple[bool, str]:
    """
    Checks for camera optical underexposure using 95th percentile dynamic range.
    Mask-aware: Completely ignores synthetic white canvas background pixels.
    """
    gray = np.array(pil_img.convert("L"))
    alpha_mask = pil_img.info.get("alpha_mask")

    if alpha_mask is not None:
        valid_pixels = gray[alpha_mask > 30]
        if len(valid_pixels) == 0:
            return True, "ảnh không chứa dữ liệu hợp lệ"
        p95 = np.percentile(valid_pixels, 95)
    else:
        p95 = np.percentile(gray, 95)

    if p95 < 35:
        return True, "ảnh chụp quá tối không đủ ánh sáng để đọc"

    return False, "clean"


def detect_specular_glare_obliteration(pil_img: Image.Image) -> Tuple[bool, str]:
    """
    Checks for specular reflection bloom that obliterates text/numbers.
    Mask-aware: Ignores synthetic white canvas so transparent images NEVER trigger false glare.
    """
    gray = np.array(pil_img.convert("L"))
    alpha_mask = pil_img.info.get("alpha_mask")

    if alpha_mask is not None:
        foreground_mask = alpha_mask > 30
        saturated_mask = (gray >= 250) & foreground_mask
        fg_pixel_count = np.sum(foreground_mask)
        if fg_pixel_count == 0:
            return False, "clean"
        saturated_ratio = np.sum(saturated_mask) / fg_pixel_count
    else:
        saturated_mask = gray >= 250
        saturated_ratio = np.mean(saturated_mask)

    if saturated_ratio < 0.08:
        return False, "clean"

    sobel_edges = cv2.Sobel(gray, cv2.CV_64F, 1, 1)
    if np.sum(saturated_mask) > 0:
        edge_density_in_white_zone = np.mean(np.abs(sobel_edges)[saturated_mask])
        if edge_density_in_white_zone > 15.0:
            return False, "clean_white_paper"

    if saturated_ratio > 0.25:
        return True, "ảnh bị lóa sáng chói đèn làm mất chữ/số"

    return False, "clean"
