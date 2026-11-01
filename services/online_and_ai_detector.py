import io
import json
import numpy as np
from PIL import Image
from typing import Tuple, List, Optional
from pydantic import BaseModel


class SyntheticImageDetectionResult(BaseModel):
    is_flagged: bool
    status_code: str  # APPROVED, STOCK_IMAGE_DETECTED, AI_GENERATED_DETECTED
    confidence_score: float
    flag_reason: Optional[str] = None


def compute_dhash_int(pil_img: Image.Image, hash_size: int = 8) -> int:
    """Computes difference hash (dHash) as an integer for fast bitwise comparison."""
    resized = pil_img.convert("L").resize(
        (hash_size + 1, hash_size), Image.Resampling.BILINEAR
    )
    arr = np.array(resized)
    diff = arr[:, 1:] > arr[:, :-1]
    decimal_val = 0
    for bit in diff.flatten():
        decimal_val = (decimal_val << 1) | int(bit)
    return decimal_val


def hamming_distance(h1: int, h2: int) -> int:
    return bin(h1 ^ h2).count("1")


def detect_ai_generated_fft_anomalies(pil_img: Image.Image) -> Tuple[bool, float]:
    """
    Detects diffusion model generation (Midjourney, DALL-E, Flux, Stable Diffusion)
    via Fast Fourier Transform (FFT) 2D power spectrum analysis.
    AI generative models exhibit synthetic checkerboard / periodic artifacts in high frequencies.
    """
    gray = np.array(pil_img.convert("L").resize((256, 256)))
    f = np.fft.fft2(gray)
    fshift = np.fft.fftshift(f)
    magnitude_spectrum = 20 * np.log(np.abs(fshift) + 1e-8)

    # analyze high-frequency quadrant energy ratio
    h, w = magnitude_spectrum.shape
    center_y, center_x = h // 2, w // 2
    r = 30  # radius of low frequencies

    # mask out low frequencies
    y, x = np.ogrid[:h, :w]
    mask = (x - center_x) ** 2 + (y - center_y) ** 2 > r**2
    high_freq_energy = np.mean(magnitude_spectrum[mask])
    total_energy = np.mean(magnitude_spectrum)

    ratio = high_freq_energy / (total_energy + 1e-8)
    # natural photos have decaying high-frequency spectrum; synthetic diffusion has elevated grid peaks
    is_suspicious_ai = bool(ratio > 1.35)
    return is_suspicious_ai, float(ratio)


class OnlineStockAndAIDetector:
    def __init__(self, stock_hash_db_path: Optional[str] = None):
        self.known_stock_hashes: List[int] = []
        if stock_hash_db_path:
            try:
                with open(stock_hash_db_path, "r") as f:
                    self.known_stock_hashes = json.load(f)
            except Exception:
                pass

    def register_stock_image(self, pil_img: Image.Image):
        h = compute_dhash_int(pil_img)
        self.known_stock_hashes.append(h)

    def evaluate_image(
        self,
        pil_img: Image.Image,
        ai_detected_flag: bool = False,
        ai_evidence_note: Optional[str] = None,
    ) -> SyntheticImageDetectionResult:
        """
        Evaluates image against:
        1. Known online stock images database (dHash hamming distance <= 6).
        2. Forensic FFT frequency spectrum anomalies.
        3. Multimodal AI generative inspection flags.
        """
        # 1. Online Stock Image Database Check
        target_hash = compute_dhash_int(pil_img)
        for stock_hash in self.known_stock_hashes:
            if hamming_distance(target_hash, stock_hash) <= 6:
                return SyntheticImageDetectionResult(
                    is_flagged=True,
                    status_code="STOCK_IMAGE_DETECTED",
                    confidence_score=0.95,
                    flag_reason="Ảnh trùng khớp với kho ảnh xe stock / quảng cáo trên mạng (dHash match).",
                )

        # 2. AI Generative Inspection (Combined Visual AI + FFT)
        is_fft_ai, fft_score = detect_ai_generated_fft_anomalies(pil_img)

        if ai_detected_flag:
            return SyntheticImageDetectionResult(
                is_flagged=True,
                status_code="AI_GENERATED_DETECTED",
                confidence_score=0.92,
                flag_reason=f"Phát hiện ảnh do AI tạo (Midjourney/DALL-E/Flux): {ai_evidence_note or 'chi tiết dị thường trên logo/vành xe'}.",
            )

        if is_fft_ai:
            return SyntheticImageDetectionResult(
                is_flagged=True,
                status_code="AI_GENERATED_DETECTED",
                confidence_score=0.85,
                flag_reason=f"Phát hiện phổ tần số nhân tạo đặc trưng của mô hình khuếch tán AI (FFT score {fft_score:.2f}).",
            )

        return SyntheticImageDetectionResult(
            is_flagged=False,
            status_code="APPROVED",
            confidence_score=0.0,
            flag_reason=None,
        )


detector = OnlineStockAndAIDetector()
