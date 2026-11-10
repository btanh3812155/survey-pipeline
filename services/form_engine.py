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
    numbers_only: bool = False
class ImageCheckConfig(BaseModel):
    enable_blur_check: bool = True
    blur_threshold: Optional[float] = None  # None falls back to settings.BLUR_THRESHOLD (40.0)
    enable_ai_vision: bool = True           # DeepSeek / Gemini visual fact extraction
    enable_object_detection: bool = False   # Strict receipt/odometer object recognition
    enable_vehicle_match: bool = True       # Vehicle plate & type persistence matching
    enable_anti_fraud: bool = True          # Duplicate reuse / stock photo check
    require_unique: bool = True             # Enforce unique images across submissions and slots (checkbox in UI)
    enable_ai_generated_check: bool = True  # 2D FFT synthetic diffusion check
    enable_screen_photo_check: bool = False  # Moiré / screen recapture detection (default False)
class FormQuestion(BaseModel):
    id: str
    title: str
    type: QuestionType
    required: bool = True
    description: Optional[str] = None
    tooltip: Optional[str] = None # Helpful tooltip popover for collaborators/drivers
    ai_detection_prompt: Optional[str] = None # Hidden instruction to describe to AI what needs to be detected
    sample_response: Optional[str] = None
    sample_images: Optional[List[str]] = None # List of sample image URLs for visual guidance
    options: Optional[List[Any]] = None # For single_choice, multiple_choice, dropdown
    group_compatibility: Optional[Dict[str, List[str]]] = None # For multiple_choice mutual exclusivity
    validation: Optional[ValidationRule] = None
    conditional: Optional[ConditionalRule] = None # Logic branching
    image_checks: Optional[ImageCheckConfig] = None
    allow_multiple_images: bool = False
    min_images: Optional[int] = None
    max_images: Optional[int] = None
    exact_image_count: Optional[int] = None
    image_slots: Optional[List[Dict[str, Any]]] = None  # e.g. [{"slot": 0, "title": "Đồng hồ km", "role": "odometer", "required": True}, ...]
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
        matching_keys = [k for k in answers.keys() if k.startswith(f"{q.id}_")]
        if val is None and matching_keys:
            val = [answers[k] for k in sorted(matching_keys)]

        # Required check
        if q.required and (val is None or val == "" or val == []):
            return False, f"Vui lòng trả lời câu hỏi bắt buộc: '{q.title}'"

        if val is None or val == "":
            continue

        # Multi-image validation for CAMERA_WEBRTC
        if q.type == QuestionType.CAMERA_WEBRTC:
            if isinstance(val, (list, tuple)):
                count = len(val)
            elif isinstance(val, dict):
                count = len(val)
            elif matching_keys:
                count = len(matching_keys)
            elif val is not None and val != "":
                count = 1
            else:
                count = 0

            if q.exact_image_count is not None and count != q.exact_image_count:
                return False, f"Câu hỏi '{q.title}' yêu cầu chính xác {q.exact_image_count} ảnh (hiện có {count} ảnh)!"
            if q.min_images is not None and count < q.min_images:
                return False, f"Câu hỏi '{q.title}' yêu cầu tối thiểu {q.min_images} ảnh (hiện có {count} ảnh)!"
            if q.max_images is not None and count > q.max_images:
                return False, f"Câu hỏi '{q.title}' không được vượt quá {q.max_images} ảnh!"
        # Regex / Constraint checks
        if q.validation:
            val_str = str(val)
            if q.validation.numbers_only:
                if not re.match(r"^[0-9]+$", val_str):
                    return False, f"Câu hỏi '{q.title}' chỉ được chứa các chữ số!"

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
        # Mutual exclusivity check for MULTIPLE_CHOICE questions
        if q.type == QuestionType.MULTIPLE_CHOICE and q.options:
            selected_items = val if isinstance(val, list) else [val]
            if len(selected_items) > 1:
                # Build lookup from option label to group
                opt_to_group = {}
                for opt in q.options:
                    if isinstance(opt, dict):
                        lbl = str(opt.get("label", ""))
                        grp = str(opt.get("group") or "default")
                    else:
                        lbl = str(opt)
                        grp = "default"
                    opt_to_group[lbl] = grp

                # Collect unique groups present in the selected items
                selected_groups = list({
                    opt_to_group.get(item, "default")
                    for item in selected_items
                })

                compat = q.group_compatibility or {}
                # Verify pairwise compatibility among selected groups
                for i in range(len(selected_groups)):
                    for j in range(i + 1, len(selected_groups)):
                        g1 = selected_groups[i]
                        g2 = selected_groups[j]
                        is_compat = (
                            g1 == g2
                            or g2 in compat.get(g1, [])
                            or g1 in compat.get(g2, [])
                        )
                        if not is_compat:
                            return False, f"Câu hỏi '{q.title}': Các lựa chọn thuộc nhóm đối lập không thể cùng được chọn!"
    return True, None

def get_effective_image_checks(
    schema: Optional[FormSchema],
    question_id: Optional[str] = None,
    slot_index: Optional[int] = None,
) -> ImageCheckConfig:
    if not schema:
        return ImageCheckConfig()
    base = schema.settings.image_checks.model_copy() if schema.settings and schema.settings.image_checks else ImageCheckConfig()
    if question_id:
        for q in schema.questions:
            if q.id == question_id:
                if q.image_checks is not None:
                    overrides = q.image_checks.model_dump(exclude_unset=True)
                    base = base.model_copy(update=overrides)
                if slot_index is not None and q.image_slots and 0 <= slot_index < len(q.image_slots):
                    slot = q.image_slots[slot_index]
                    if isinstance(slot, dict):
                        slot_checks = slot.get("image_checks")
                        if isinstance(slot_checks, dict):
                            clean_checks = {k: v for k, v in slot_checks.items() if v is not None}
                            base = base.model_copy(update=clean_checks)
                        flat_keys = (
                            "enable_blur_check", "blur_threshold", "enable_ai_vision",
                            "enable_vehicle_match", "enable_anti_fraud", "require_unique",
                            "enable_ai_generated_check", "enable_screen_photo_check"
                        )
                        flat_overrides = {k: slot[k] for k in flat_keys if k in slot and slot[k] is not None}
                        if flat_overrides:
                            base = base.model_copy(update=flat_overrides)
                break
    return base

# Default Production Form Schema for Trust Bridge Fleet Survey
DEFAULT_TRUST_BRIDGE_SCHEMA = FormSchema(
    form_id="income_survey",
    title="Trust Bridge - Khảo Sát Thu Nhập Tài Xế GSM",
    description="Biểu mẫu khảo sát và xác thực thu nhập tài xế taxi / xe máy GSM",
    settings=FormSettings(
        response_limit=20000,
        access_control="public",
        allow_anonymous=False,
        allow_edit_after_submit=False,
        notify_telegram_chat_id=-1001234567890,
        decree13_required=True,
        is_test_mode=False,
        image_checks=ImageCheckConfig(
            enable_blur_check=True,
            blur_threshold=15.0,
            enable_ai_vision=True,
            enable_object_detection=False,
            enable_vehicle_match=False,
            enable_anti_fraud=True,
            enable_ai_generated_check=True,
            enable_screen_photo_check=False,
        ),
    ),
    questions=[
        # Question 1: Signature
        FormQuestion(
            id="driver_signature",
            title="1. Chữ ký xác nhận khảo sát",
            type=QuestionType.SIGNATURE,
            required=True,
            description="Dùng ngón tay ký tên trực tiếp trên giấy điện tử để xác nhận thông tin khảo sát",
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
        FormQuestion(
            id="driver_phone",
            title="3. Số điện thoại tài xế (Không bắt buộc)",
            type=QuestionType.TEXT,
            required=False,
            description="Số điện thoại dùng để tra cứu lịch sử nộp và đối soát ca làm việc (có thể để trống)",
            sample_response="0988123456",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
            validation=ValidationRule(
                numbers_only=True,
                regex_pattern=r"^0[0-9]{9,10}$",
                regex_error_message="Số điện thoại phải bắt đầu bằng số 0 và có từ 10 đến 11 chữ số",
            ),
        ),
        FormQuestion(
            id="vehicle_type",
            title="Câu 1: Hiện tại anh đang chạy dòng xe nào cho hệ thống GSM ạ?",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Xe máy", "Ô tô"],
            description="Chọn dòng phương tiện đang vận hành cho hệ thống GSM",
            sample_response="Ô tô",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
        ),
        FormQuestion(
            id="car_model",
            title="Dòng xe VinFast đang chạy",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["VF 5 Plus", "VF e34", "VF 6", "VF 7", "VF 8", "VF 9"],
            description="Dòng xe ô tô điện cụ thể trong ca (nếu chạy ô tô điện)",
            sample_response="VF 5 Plus",
            conditional=ConditionalRule(
                depends_on_question_id="vehicle_type",
                operator="equals",
                expected_value="Ô tô",
            ),
        ),
        FormQuestion(
            id="gsm_duration",
            title="Câu 2: Anh đã chạy xe cho GSM được bao lâu rồi ạ?",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Dưới 3 tháng", "Từ 3 – 6 tháng", "Từ 6 – 12 tháng", "Từ 1 – 2 năm", "Trên 2 năm"],
            description="Thời gian đã cộng tác chạy xe cùng hệ thống GSM",
            sample_response="Từ 6 – 12 tháng",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
        ),
        FormQuestion(
            id="gsm_contract_type",
            title="Câu 3: Anh đang chạy theo hình thức nào cho GSM ạ?",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=[
                "Xe Partner (Hợp tác kinh doanh)",
                "Xe tự mua & chạy",
                "Xe Taxi xanh GSM",
                "Xe RTO (Xe thuê mua)",
                "Xe thuê tháng của GSM",
                "Khác",
            ],
            description="Hình thức hợp tác hoặc sở hữu xe với GSM",
            sample_response="Xe Taxi xanh GSM",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
        ),
        FormQuestion(
            id="daily_trips",
            title="Câu 5: Trung bình 1 ngày anh hoàn thành khoảng bao nhiêu chuyến xe? (Số chuyến trung bình / ngày)",
            type=QuestionType.TEXT,
            required=True,
            description="Số chuyến xe hoàn thành trung bình mỗi ngày",
            sample_response="18",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
            validation=ValidationRule(
                numbers_only=True,
                min_value=1,
                max_value=200,
            ),
        ),
        FormQuestion(
            id="monthly_income_range",
            title="Câu 6: Sau khi trừ chi phí (hoặc theo mức thực nhận chuyển về tài khoản), thu nhập bình quân 1 tháng từ công việc này của anh dao động ở mức nào?",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Dưới 3 triệu VNĐ", "3 – 6 triệu VNĐ", "7 – 12 triệu VNĐ", "12 – 20 triệu VNĐ", "Trên 20 triệu VNĐ"],
            description="Thu nhập thực nhận bình quân hàng tháng sau khi trừ chi phí",
            sample_response="12 – 20 triệu VNĐ",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
        ),
        FormQuestion(
            id="gsm_incentive_policy",
            title="Câu 7: Về phía GSM, công ty hiện có áp dụng các yêu cầu về tiến độ/chỉ tiêu và chính sách thưởng hiệu suất như thế nào?",
            type=QuestionType.MULTIPLE_CHOICE,
            required=True,
            options=[
                {"label": "Có áp dụng chỉ tiêu", "group": "applicable"},
                {"label": "Có khung giờ vàng", "group": "applicable"},
                {"label": "Có chương trình thưởng", "group": "applicable"},
                {"label": "Khác", "group": "applicable"},
                {"label": "Không có", "group": "none_exclusive"},
            ],
            description="Chính sách thưởng hiệu suất và yêu cầu chỉ tiêu của GSM",
            sample_response="Có áp dụng chỉ tiêu",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
        ),
        FormQuestion(
            id="camera_receipt",
            title="Chụp ảnh xác thực 7 bước: Hồ sơ App, Phương tiện & Doanh thu 5 tuần Tháng 10",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            description="Chụp trực tiếp từ camera phần cứng: Ảnh 1 là Hồ sơ tài xế, Ảnh 2 là Xe vận hành (thân xe, đầu xe hoặc vô lăng khoang lái), Ảnh 3-7 là thống kê thu nhập 5 tuần Tháng 10.",
            tooltip="Chụp rõ nét màn hình điện thoại và phương tiện. Xe máy hoặc ô tô chụp góc rõ thân xe, đầu xe hoặc vô lăng khoang lái; màn hình thu nhập chuyển lần lượt qua từng tuần trong Tháng 10.",
            ai_detection_prompt=(
                "Slot 0 (app_profile): Verify authentic Xanh SM / GSM driver profile and account screen.\n"
                "Slot 1 (vehicle_exterior_or_interior): Vehicle exterior body, front, or steering wheel/dashboard. Pass without strict block for post-audit verification.\n"
                "Slots 2 to 6 (app_income_oct_week_1 to 5): Weekly earnings statements for October 2026. Verify authentic Xanh SM UI, legible revenue figures, and non-tampered numbers."
            ),
            sample_response="Chụp đủ 7 ảnh theo hướng dẫn của từng ô",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý",
            ),
            sample_images=[
                "https://images.unsplash.com/photo-1554224155-8d04cb21cd6c?w=600&auto=format&fit=crop&q=80",
                "https://images.unsplash.com/photo-1580273916550-e323be2ae537?w=600&auto=format&fit=crop&q=80",
            ],
            allow_multiple_images=True,
            min_images=7,
            max_images=7,
            exact_image_count=7,
            image_slots=[
                {"slot": 0, "title": "Ảnh 1: Màn hình hồ sơ ứng dụng", "role": "app_profile", "required": True, "require_unique": False, "ai_check": True, "tooltip": ""},
                {"slot": 1, "title": "Ảnh 2: Thân xe, đầu xe hoặc vô lăng khoang lái phương tiện", "role": "vehicle_exterior_or_interior", "required": True, "require_unique": False, "ai_check": False, "tooltip": ""},
                {"slot": 2, "title": "Ảnh 3: Doanh thu Tuần 1 Tháng 10 - 28/09 đến 04/10", "role": "app_income_week_1", "required": True, "require_unique": True, "ai_check": True, "tooltip": ""},
                {"slot": 3, "title": "Ảnh 4: Doanh thu Tuần 2 Tháng 10 - 05/10 đến 11/10", "role": "app_income_week_2", "required": True, "require_unique": True, "ai_check": True, "tooltip": ""},
                {"slot": 4, "title": "Ảnh 5: Doanh thu Tuần 3 Tháng 10 - 12/10 đến 18/10", "role": "app_income_week_3", "required": True, "require_unique": True, "ai_check": True, "tooltip": ""},
                {"slot": 5, "title": "Ảnh 6: Doanh thu Tuần 4 Tháng 10 - 19/10 đến 25/10", "role": "app_income_week_4", "required": True, "require_unique": True, "ai_check": True, "tooltip": ""},
                {"slot": 6, "title": "Ảnh 7: Doanh thu Tuần 5 Tháng 10 - 26/10 đến 01/11", "role": "app_income_week_5", "required": True, "require_unique": True, "ai_check": True, "tooltip": ""},
            ],
        ),
    ],
)

# Test mode clone with test banner enabled
TEST_TRUST_BRIDGE_SCHEMA = DEFAULT_TRUST_BRIDGE_SCHEMA.model_copy(deep=True)
TEST_TRUST_BRIDGE_SCHEMA.form_id = "income_survey_test"
TEST_TRUST_BRIDGE_SCHEMA.title = "Trust Bridge - Khảo Sát Thu Nhập (Thử Nghiệm / Test Mode)"
TEST_TRUST_BRIDGE_SCHEMA.settings.is_test_mode = True

FORM_REGISTRY: Dict[str, FormSchema] = {
    DEFAULT_TRUST_BRIDGE_SCHEMA.form_id: DEFAULT_TRUST_BRIDGE_SCHEMA,
    TEST_TRUST_BRIDGE_SCHEMA.form_id: TEST_TRUST_BRIDGE_SCHEMA,
}

