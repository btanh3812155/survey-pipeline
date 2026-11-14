# -*- coding: utf-8 -*-
"""
Helper utility to load actual GSM driver test images from the 1.3GB dataset:
(GSM) 16th Oct 26-20261031T084345Z-1-001/(GSM) 16th Oct 26/
"""

import os
import glob
from typing import List, Tuple
from services.normalizer import sanitize_and_normalize_image
from services.duplicate_detector import compute_dhash_int, hamming_distance

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GSM_DATASET_DIR = os.path.join(
    REPO_ROOT,
    "(GSM) 16th Oct 26-20261031T084345Z-1-001",
    "(GSM) 16th Oct 26",
)
if not os.path.exists(GSM_DATASET_DIR):
    alt_dir = os.path.join(
        os.path.dirname(REPO_ROOT),
        "survey-pipeline",
        "(GSM) 16th Oct 26-20261031T084345Z-1-001",
        "(GSM) 16th Oct 26",
    )
    if os.path.exists(alt_dir):
        GSM_DATASET_DIR = alt_dir

_CACHED_DISTINCT_PHOTOS: List[Tuple[str, bytes]] = []


def get_available_driver_folders() -> List[str]:
    """Returns all available driver subfolder names in the GSM dataset."""
    if not os.path.exists(GSM_DATASET_DIR):
        return []
    folders = [
        os.path.basename(d)
        for d in sorted(glob.glob(os.path.join(GSM_DATASET_DIR, "*")))
        if os.path.isdir(d)
    ]
    return folders


def get_real_gsm_driver_photos(folder_name: str = "01 - Bike") -> List[Tuple[str, bytes]]:
    """
    Loads authentic driver JPEG photos from a specific driver folder in the 1.3GB GSM folder.
    Returns list of (filename, file_bytes).
    """
    target_dir = os.path.join(GSM_DATASET_DIR, folder_name)
    if not os.path.exists(target_dir):
        avail = get_available_driver_folders()
        if avail:
            target_dir = os.path.join(GSM_DATASET_DIR, avail[0])
        else:
            return []

    pattern_jpg = glob.glob(os.path.join(target_dir, "*.jpg"))
    pattern_jpeg = glob.glob(os.path.join(target_dir, "*.jpeg"))
    all_files = sorted(pattern_jpg + pattern_jpeg)

    results = []
    for fpath in all_files:
        fname = os.path.basename(fpath)
        with open(fpath, "rb") as fp:
            results.append((fname, fp.read()))
    return results


def get_real_distinct_test_photos(count: int = 7) -> List[Tuple[str, bytes]]:
    """
    Grabs authentic driver JPEG photos across different driver folders
    guaranteed to be non-duplicate (dHash distance > 8).
    Cached in-memory for instant test performance.
    """
    global _CACHED_DISTINCT_PHOTOS
    if _CACHED_DISTINCT_PHOTOS and len(_CACHED_DISTINCT_PHOTOS) >= count:
        return _CACHED_DISTINCT_PHOTOS[:count]

    selected = []
    dhashes = []
    for folder in get_available_driver_folders():
        photos = get_real_gsm_driver_photos(folder)
        if not photos:
            continue
        fname, b = photos[0]
        try:
            norm_b, im = sanitize_and_normalize_image(b, max_dimension=1024)
            dh = compute_dhash_int(im)
            if not any(hamming_distance(dh, existing) <= 8 for existing in dhashes):
                selected.append((fname, norm_b))
                dhashes.append(dh)
                if len(selected) >= count:
                    break
        except Exception:
            continue

    _CACHED_DISTINCT_PHOTOS = selected
    return _CACHED_DISTINCT_PHOTOS[:count]


def get_real_sample_photo(index: int = 0) -> bytes:
    """Returns raw bytes of an authentic driver photo from the dataset."""
    distinct = get_real_distinct_test_photos(7)
    if distinct:
        return distinct[index % len(distinct)][1]
    photos = get_real_gsm_driver_photos("01 - Bike")
    if photos:
        return photos[index % len(photos)][1]
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
