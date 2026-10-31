import base64
import time
import os
import json
from typing import List, Optional
from pydantic import BaseModel, Field
from openai import AsyncOpenAI


class ReceiptLineItem(BaseModel):
    item_name: str
    amount: int


class ExtractedReceiptFacts(BaseModel):
    vendor_name: Optional[str] = None
    tax_id: Optional[str] = None
    receipt_serial: Optional[str] = None
    line_items: List[ReceiptLineItem] = Field(default_factory=list)
    raw_subtotal: int = 0
    vat_amount: int = 0
    raw_total: int = 0
    has_signature: bool = False
    unchecked_boxes_count: int = 0
    is_screen_photo: bool = False
    notes: Optional[str] = None


class DualModelVisionClient:
    """
    Manages dual-model AI vision extraction with automated failover:
    - Primary: Gemini 2.5 Flash (thinking_budget=0, 2.0s hard cutoff)
    - Fallback: DeepSeek Flash (via Alibaba Cloud Singapore ap-southeast-1 or official)
    - Circuit breaker: 5 consecutive failures opens breaker for 60s
    """

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
        deepseek_api_key: Optional[str] = None,
        deepseek_base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 2.0,
    ):
        self.timeout_seconds = timeout_seconds
        self.gemini_client = (
            AsyncOpenAI(api_key=gemini_api_key, base_url=gemini_base_url)
            if gemini_api_key
            else None
        )
        self.deepseek_client = (
            AsyncOpenAI(api_key=deepseek_api_key, base_url=deepseek_base_url)
            if deepseek_api_key
            else None
        )

        # circuit breaker state
        self.consecutive_failures = 0
        self.circuit_open_until = 0.0

    async def extract_facts(self, webp_bytes: bytes) -> ExtractedReceiptFacts:
        # offline / development mock mode
        if os.environ.get("MOCK_AI") == "1" or (not self.gemini_client and not self.deepseek_client):
            return ExtractedReceiptFacts(
                vendor_name="CỬA HÀNG MẪU VIỆT NAM",
                tax_id="0100109106",
                receipt_serial="VN-9921",
                line_items=[
                    ReceiptLineItem(item_name="Dịch vụ khảo sát", amount=150000)
                ],
                raw_subtotal=150000,
                vat_amount=15000,
                raw_total=165000,
                has_signature=True,
                unchecked_boxes_count=0,
                is_screen_photo=False,
                notes="Offline Mock Fact Extraction",
            )

        b64_img = base64.b64encode(webp_bytes).decode("utf-8")
        messages = [
            {
                "role": "system",
                "content": (
                    "you are an observational document transcription engine. "
                    "extract visual facts from this survey/receipt photo strictly into JSON format. "
                    "do NOT perform mathematical verification or compliance judgment. "
                    "output fields: vendor_name, tax_id, receipt_serial, line_items [{item_name, amount}], "
                    "raw_subtotal (int), vat_amount (int), raw_total (int), has_signature (bool), "
                    "unchecked_boxes_count (int), is_screen_photo (bool), notes."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "extract document facts:"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/webp;base64,{b64_img}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ]

        now = time.time()
        # check if circuit breaker is open (bypass Gemini directly to DeepSeek)
        if now < self.circuit_open_until and self.deepseek_client:
            return await self._call_deepseek(messages)

        # attempt primary: Gemini 2.5 Flash
        if self.gemini_client:
            try:
                resp = await self.gemini_client.chat.completions.create(
                    model="gemini-2.5-flash",
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.0,
                    max_tokens=300,
                    timeout=self.timeout_seconds,
                )
                self.consecutive_failures = 0
                return ExtractedReceiptFacts.model_validate_json(
                    resp.choices[0].message.content
                )
            except Exception as e:
                self.consecutive_failures += 1
                if self.consecutive_failures >= 5:
                    self.circuit_open_until = time.time() + 60.0

        # failover: DeepSeek Flash
        if self.deepseek_client:
            return await self._call_deepseek(messages)

        raise RuntimeError("both primary and fallback vision providers failed")

    async def _call_deepseek(self, messages: list) -> ExtractedReceiptFacts:
        resp = await self.deepseek_client.chat.completions.create(
            model="deepseek-flash",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=300,
            timeout=self.timeout_seconds + 0.5,
        )
        content = resp.choices[0].message.content
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()
        return ExtractedReceiptFacts.model_validate_json(content)
