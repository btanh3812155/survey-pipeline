import re
import math
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, model_validator
class QuestionType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    DROPDOWN = "dropdown"
    CAMERA_WEBRTC = "camera_webrtc"
    SIGNATURE = "signature"

class ConditionalRule(BaseModel):
    depends_on_question_id: str
    operator: str = "equals" # "equals", "not_equals", "contains", "in"
    expected_value: Any

class ValidationRule(BaseModel):
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    regex_pattern: Optional[str] = None
    regex_error_message: Optional[str] = None
    max_length: Optional[int] = None
    min_length: Optional[int] = None

class ImageCheckConfig(BaseModel):
    enable_blur_check: bool = True
    blur_threshold: Optional[float] = None  # None falls back to settings.BLUR_THRESHOLD (40.0)
    enable_ai_vision: bool = True           # DeepSeek / Gemini visual fact extraction
    enable_vehicle_match: bool = True       # Vehicle plate & type persistence matching
    enable_anti_fraud: bool = True          # Duplicate reuse / stock photo check
    enable_ai_generated_check: bool = True  # 2D FFT synthetic diffusion check
    enable_screen_photo_check: bool = True  # Moiré / screen recapture detection

class FormQuestion(BaseModel):
    id: str
    title: str
    type: QuestionType
    required: bool = True
    description: Optional[str] = None
    sample_response: Optional[str] = None
    sample_images: Optional[List[str]] = None # List of sample image URLs for visual guidance
    options: Optional[List[str]] = None # For single_choice, multiple_choice, dropdown
    validation: Optional[ValidationRule] = None
    conditional: Optional[ConditionalRule] = None # Logic branching
    image_checks: Optional[ImageCheckConfig] = None

class FormSettings(BaseModel):
    response_limit: Optional[int] = None # Cap total responses
    current_responses: int = 0
    access_control: str = "public" # "public", "private"
    allow_anonymous: bool = False
    allow_edit_after_submit: bool = False
    notify_telegram_chat_id: Optional[int] = None
    notify_email: Optional[str] = None
    decree13_required: bool = True
    is_test_mode: bool = False
    image_checks: ImageCheckConfig = Field(default_factory=ImageCheckConfig)

MAX_QUESTIONS_PER_FORM: int = 100

class FormSchema(BaseModel):
    form_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    title: str
    description: Optional[str] = None
    settings: FormSettings = Field(default_factory=FormSettings)
    questions: List[FormQuestion]

    @model_validator(mode="after")
    def validate_schema_structure(self) -> "FormSchema":
        if len(self.questions) > MAX_QUESTIONS_PER_FORM:
            raise ValueError(f"Số lượng câu hỏi vượt quá giới hạn tối đa cho phép ({MAX_QUESTIONS_PER_FORM})")

        seen_ids = set()
        for q in self.questions:
            if q.id in seen_ids:
                raise ValueError(f"Mã câu hỏi trùng lặp: '{q.id}'")
            seen_ids.add(q.id)

        declared_ids = set()
        for q in self.questions:
            if q.conditional:
                parent_id = q.conditional.depends_on_question_id
                if parent_id == q.id:
                    raise ValueError(f"Câu hỏi '{q.id}' không thể tự phụ thuộc vào chính nó!")
                if parent_id not in declared_ids:
                    raise ValueError(f"Điều kiện câu hỏi '{q.id}' phụ thuộc vào câu hỏi chưa được khai báo trước đó: '{parent_id}'")
            declared_ids.add(q.id)

        return self
def validate_submission_against_schema(schema: FormSchema, answers: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validates a submitted answer dictionary against the dynamic form schema rules and constraints.
    """
    # 0. Check mandatory Decree 13 consent dissent
    if answers.get("consent_choice") == "Tôi không đồng ý":
        return False, "Biểu mẫu yêu cầu tài xế đồng ý điều khoản xử lý dữ liệu Nghị định 13 để tiếp tục!"

    # 1. Check response limit
    if schema.settings.response_limit and schema.settings.current_responses >= schema.settings.response_limit:
        return False, "Biểu mẫu đã đạt giới hạn số lượng phản hồi tối đa của chủ sở hữu!"
    has_signed = bool(answers.get("driver_signature"))
    has_consented = answers.get("consent_choice") == "Tôi đồng ý"
    is_unlocked = has_signed and has_consented

    for q in schema.questions:
        # Check conditional visibility
        if q.conditional:
            if q.conditional.depends_on_question_id == "consent_choice":
                if not is_unlocked:
                    continue # Question is hidden unless both signature and consent are satisfied
            else:
                parent_val = answers.get(q.conditional.depends_on_question_id)
                if q.conditional.operator == "equals" and parent_val != q.conditional.expected_value:
                    continue # Question is hidden, skip validation
                elif q.conditional.operator == "in" and parent_val not in q.conditional.expected_value:
                    continue
        val = answers.get(q.id)

        # Required check
        if q.required and (val is None or val == "" or val == []):
            return False, f"Vui lòng trả lời câu hỏi bắt buộc: '{q.title}'"

        if val is None or val == "":
            continue

        # Regex / Constraint checks
        if q.validation:
            val_str = str(val)
            if q.validation.regex_pattern:
                target_str = val_str[:200]
                try:
                    if not re.match(q.validation.regex_pattern, target_str):
                        err_msg = q.validation.regex_error_message or f"Dữ liệu câu hỏi '{q.title}' không đúng định dạng yêu cầu!"
                        return False, err_msg
                except (re.error, Exception):
                    return False, f"Lỗi định dạng mẫu quy chuẩn của câu hỏi '{q.title}'!"

            if q.validation.min_length and len(val_str) < q.validation.min_length:
                return False, f"Câu hỏi '{q.title}' yêu cầu tối thiểu {q.validation.min_length} ký tự!"

            if q.validation.max_length and len(val_str) > q.validation.max_length:
                return False, f"Câu hỏi '{q.title}' không được vượt quá {q.validation.max_length} ký tự!"

            if q.type == QuestionType.NUMBER:
                try:
                    num_val = float(val)
                    if math.isnan(num_val) or math.isinf(num_val):
                        return False, f"Giá trị câu hỏi '{q.title}' không được là NaN/Inf!"
                    if q.validation.min_value is not None and num_val < q.validation.min_value:
                        return False, f"Giá trị câu hỏi '{q.title}' không được nhỏ hơn {q.validation.min_value}!"
                    if q.validation.max_value is not None and num_val > q.validation.max_value:
                        return False, f"Giá trị câu hỏi '{q.title}' không được lớn hơn {q.validation.max_value}!"
                except ValueError:
                    return False, f"Câu hỏi '{q.title}' phải là số hợp lệ!"
    return True, None

def get_effective_image_checks(schema: Optional[FormSchema], question_id: Optional[str] = None) -> ImageCheckConfig:
    if not schema:
        return ImageCheckConfig()
    base = schema.settings.image_checks.model_copy() if schema.settings and schema.settings.image_checks else ImageCheckConfig()
    if question_id:
        for q in schema.questions:
            if q.id == question_id and q.image_checks is not None:
                overrides = q.image_checks.model_dump(exclude_unset=True)
                return base.model_copy(update=overrides)
    return base

# Default Production Form Schema for Trust Bridge Fleet Survey
DEFAULT_TRUST_BRIDGE_SCHEMA = FormSchema(
    form_id="income_survey",
    title="Trust Bridge - Báo Cáo Bàn Giao Ca Xe (Lark Base)",
    description="Biểu mẫu khảo sát và xác thực bàn giao ca xe GSM / VinFast từ hệ thống Lark Base",
    settings=FormSettings(
        response_limit=20000,
        access_control="public",
        allow_anonymous=False,
        allow_edit_after_submit=False,
        notify_telegram_chat_id=-1001234567890,
        decree13_required=True,
        is_test_mode=True,
    ),
    questions=[
        # Question 1: Signature
        FormQuestion(
            id="driver_signature",
            title="1. Chữ ký xác nhận bàn giao ca",
            type=QuestionType.SIGNATURE,
            required=True,
            description="Dùng ngón tay ký tên trực tiếp trên giấy điện tử để xác nhận bàn giao ca",
            sample_response="Bấm nút 'Ký tên' và vẽ chữ ký trên bảng",
        ),
        # Question 2: Consent Choice
        FormQuestion(
            id="consent_choice",
            title="2. Cam kết thông tin & Đồng ý xử lý dữ liệu Nghị định 13",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Tôi đồng ý", "Tôi không đồng ý"],
            description="Tôi xác nhận các số liệu thu nhập và hình ảnh cung cấp là trung thực, đồng ý xử lý dữ liệu theo NĐ 13",
            sample_response="Tôi đồng ý",
        ),
        # Subsequent Questions - ALL CONDITIONAL on consent_choice == 'Tôi đồng ý'
        FormQuestion(
            id="driver_phone",
            title="3. Số điện thoại tài xế",
            type=QuestionType.TEXT,
            required=True,
            description="Số điện thoại chính chủ dùng để tra cứu lịch sử nộp và đối soát ca làm việc",
            sample_response="0988123456",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
            validation=ValidationRule(
                regex_pattern=r"^0[0-9]{9,10}$",
                regex_error_message="Số điện thoại phải bắt đầu bằng số 0 và có từ 10 đến 11 chữ số",
            ),
        ),
        FormQuestion(
            id="driver_name",
            title="4. Họ và tên tài xế",
            type=QuestionType.TEXT,
            required=True,
            description="Họ tên đầy đủ theo giấy phép lái xe",
            sample_response="Nguyễn Văn An",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
        ),
        FormQuestion(
            id="license_plate",
            title="5. Biển số xe",
            type=QuestionType.TEXT,
            required=True,
            description="Biển số xe vận hành (VD: 29E-123.45 hoặc 51H12345)",
            sample_response="29E-123.45",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
        ),
        FormQuestion(
            id="vehicle_type",
            title="6. Loại phương tiện đang vận hành",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Ô tô điện VinFast (GSM)", "Xe máy điện (GSM Bike)", "Xe cá nhân khác"],
            description="Chọn đúng loại phương tiện của ca làm việc hiện tại",
            sample_response="Ô tô điện VinFast (GSM)",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
        ),
        FormQuestion(
            id="car_model",
            title="7. Dòng xe VinFast đang chạy",
            type=QuestionType.DROPDOWN,
            required=True,
            options=["VF 5 Plus", "VF e34", "VF 6", "VF 7", "VF 8", "VF 9"],
            description="Dòng xe ô tô điện cụ thể trong ca",
            sample_response="VF 5 Plus",
            conditional=ConditionalRule(
                depends_on_question_id="vehicle_type",
                operator="equals",
                expected_value="Ô tô điện VinFast (GSM)",
            ),
        ),
        FormQuestion(
            id="gross_revenue",
            title="8. Tổng doanh thu ca xe (VNĐ)",
            type=QuestionType.NUMBER,
            required=False,
            description="Tổng doanh thu hiển thị trên ứng dụng tài xế cuối ca",
            sample_response="1250000",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
            validation=ValidationRule(min_value=0, max_value=50000000),
        ),
        FormQuestion(
            id="odometer_km",
            title="9. Số km công-tơ-mét (Odometer)",
            type=QuestionType.NUMBER,
            required=False,
            description="Số km trên đồng hồ xe tại thời điểm bàn giao",
            sample_response="15200",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
            validation=ValidationRule(min_value=0),
        ),
        FormQuestion(
            id="camera_receipt",
            title="10. Chụp ảnh màn hình doanh thu hoặc đồng hồ xe",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            description="Chụp trực tiếp từ camera phần cứng, khung hình chuẩn 2:3",
            sample_response="Đặt rõ nét màn hình doanh thu hoặc đồng hồ xe vào khung",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
            sample_images=[
                "https://images.unsplash.com/photo-1554224155-8d04cb21cd6c?w=600&auto=format&fit=crop&q=80",
                "https://images.unsplash.com/photo-1580273916550-e323be2ae537?w=600&auto=format&fit=crop&q=80",
            ],
        ),
    ],
)

FORM_REGISTRY: Dict[str, FormSchema] = {
    DEFAULT_TRUST_BRIDGE_SCHEMA.form_id: DEFAULT_TRUST_BRIDGE_SCHEMA
}
