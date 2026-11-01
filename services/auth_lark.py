import time
import hashlib
import hmac
import json
import base64
from typing import Optional, Tuple
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend


def verify_lark_signature(
    timestamp: str,
    nonce: str,
    encrypt_key: str,
    raw_body: bytes,
    expected_signature: str,
) -> bool:
    """
    Verifies Bytedance Lark Base webhook signature using raw SHA256 string concatenation.
    Note: Lark does NOT use HMAC; it concatenates timestamp + nonce + key + body and hashes with SHA256.
    """
    if not (timestamp and nonce and expected_signature and encrypt_key is not None and raw_body is not None):
        return False

    if not isinstance(encrypt_key, str) or not isinstance(raw_body, (bytes, bytearray)):
        return False

    try:
        ts_int = int(timestamp)
        if abs(time.time() - ts_int) > 300:
            return False
    except (ValueError, TypeError):
        return False

    try:
        content = (
            timestamp.encode("utf-8")
            + nonce.encode("utf-8")
            + encrypt_key.encode("utf-8")
            + raw_body
        )
        computed = hashlib.sha256(content).hexdigest()
        return hmac.compare_digest(computed, expected_signature)
    except Exception:
        return False

def decrypt_lark_payload(encrypt_key: str, encrypted_b64: str) -> str:
    """
    Decrypts AES-256-CBC encrypted Lark webhook payload.
    Key is sha256(encrypt_key), IV is first 16 bytes of decoded ciphertext.
    """
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    ciphertext_raw = base64.b64decode(encrypted_b64)
    iv = ciphertext_raw[:16]
    ciphertext = ciphertext_raw[16:]

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded_data) + unpadder.finalize()
    return plaintext.decode("utf-8")


def handle_lark_challenge_handshake(
    raw_body: bytes, encrypt_key: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """
    Handles Lark's initial url_verification challenge handshake.
    Returns (is_challenge: bool, challenge_token: Optional[str]).
    """
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return False, None

    # check if encrypted challenge
    if "encrypt" in data and encrypt_key:
        try:
            decrypted_str = decrypt_lark_payload(encrypt_key, data["encrypt"])
            data = json.loads(decrypted_str)
        except Exception:
            return False, None

    if data.get("type") == "url_verification" and "challenge" in data:
        return True, data["challenge"]

    return False, None
