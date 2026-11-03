import re
import json
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field

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

class FormSchema(BaseModel):
    form_id: str
    title: str
    description: Optional[str] = None
    settings: FormSettings = Field(default_factory=FormSettings)
    questions: List[FormQuestion]

def validate_submission_against_schema(schema: FormSchema, answers: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validates a submitted answer dictionary against the dynamic form schema rules and constraints.
    """
    # 1. Check response limit
    if schema.settings.response_limit and schema.settings.current_responses >= schema.settings.response_limit:
        return False, "Biểu mẫu đã đạt giới hạn số lượng phản hồi tối đa của chủ sở hữu!"

    for q in schema.questions:
        # Check conditional visibility
        if q.conditional:
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
                if not re.match(q.validation.regex_pattern, val_str):
                    err_msg = q.validation.regex_error_message or f"Dữ liệu câu hỏi '{q.title}' không đúng định dạng yêu cầu!"
                    return False, err_msg

            if q.validation.min_length and len(val_str) < q.validation.min_length:
                return False, f"Câu hỏi '{q.title}' yêu cầu tối thiểu {q.validation.min_length} ký tự!"

            if q.validation.max_length and len(val_str) > q.validation.max_length:
                return False, f"Câu hỏi '{q.title}' không được vượt quá {q.validation.max_length} ký tự!"

            if q.type == QuestionType.NUMBER:
                try:
                    num_val = float(val)
                    if q.validation.min_value is not None and num_val < q.validation.min_value:
                        return False, f"Giá trị câu hỏi '{q.title}' không được nhỏ hơn {q.validation.min_value}!"
                    if q.validation.max_value is not None and num_val > q.validation.max_value:
                        return False, f"Giá trị câu hỏi '{q.title}' không được lớn hơn {q.validation.max_value}!"
                except ValueError:
                    return False, f"Câu hỏi '{q.title}' phải là số hợp lệ!"

    return True, None

# Default Production Form Schema for Trust Bridge Fleet Survey
DEFAULT_TRUST_BRIDGE_SCHEMA = FormSchema(
    form_id="gsm_fleet_survey",
    title="Trust Bridge - Khảo Sát Phương Tiện & Doanh Thu GSM",
    description="Biểu mẫu kiểm tra phương tiện, doanh thu ca xe và chụp ảnh trực tiếp qua WebRTC",
    settings=FormSettings(
        response_limit=20000,
        access_control="public",
        allow_anonymous=False,
        allow_edit_after_submit=False,
        notify_telegram_chat_id=-1001234567890,
        decree13_required=True
    ),
    questions=[
        # Question 1: Signature First!
        FormQuestion(
            id="driver_signature",
            title="1. Chữ ký điện tử của tài xế",
            type=QuestionType.SIGNATURE,
            required=True,
            description="Dùng ngón tay ký tên trên tờ giấy điện tử để xác thực người lập phiếu",
            sample_response="Bấm nút 'Ký tên' và vẽ chữ ký ngón tay trên bảng pop-up"
        ),
        # Question 2: Consent Choice
        FormQuestion(
            id="consent_choice",
            title="2. Điều khoản Cam kết & Xử lý Dữ liệu (Nghị định 13)",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Tôi đồng ý", "Tôi không đồng ý"],
            description="Bạn bắt buộc phải chọn 'Tôi đồng ý' để mở khóa các câu hỏi tiếp theo của khảo sát",
            sample_response="Tôi đồng ý"
        ),
        # Subsequent Questions - ALL CONDITIONAL on consent_choice == 'Tôi đồng ý'
        FormQuestion(
            id="driver_phone",
            title="3. Số điện thoại tài xế",
            type=QuestionType.TEXT,
            required=True,
            description="Số điện thoại chính chủ dùng để nhận mã xác nhận và thanh toán phụ cấp",
            sample_response="0988123456 hoặc 0912345678",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            ),
            validation=ValidationRule(
                regex_pattern=r"^0[0-9]{9,10}$",
                regex_error_message="Số điện thoại phải bắt đầu bằng số 0 và có từ 10 đến 11 chữ số!"
            )
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
                expected_value="Tôi đồng ý"
            )
        ),
        FormQuestion(
            id="vehicle_type",
            title="5. Loại phương tiện đang vận hành",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Ô tô điện VinFast (GSM)", "Xe máy điện (GSM Bike)", "Xe cá nhân khác"],
            description="Chọn đúng loại phương tiện của ca làm việc hiện tại",
            sample_response="Ô tô điện VinFast (GSM)",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            )
        ),
        # Nested Conditional: Only shows if vehicle_type == "Ô tô điện VinFast (GSM)"
        FormQuestion(
            id="car_model",
            title="6. Dòng xe VinFast đang chạy",
            type=QuestionType.DROPDOWN,
            required=True,
            options=["VF 5 Plus", "VF e34", "VF 6", "VF 7", "VF 8", "VF 9"],
            description="Dòng xe ô tô điện cụ thể trong ca",
            sample_response="VF 5 Plus",
            conditional=ConditionalRule(
                depends_on_question_id="vehicle_type",
                operator="equals",
                expected_value="Ô tô điện VinFast (GSM)"
            )
        ),
        FormQuestion(
            id="license_plate",
            title="7. Biển số xe",
            type=QuestionType.TEXT,
            required=True,
            description="Biển số xe đăng ký (dạng 29E-xxx.xx hoặc 51G-xxx.xx)",
            sample_response="29E-123.45",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            ),
            validation=ValidationRule(
                regex_pattern=r"^[0-9]{2}[A-Z]{1,2}-[0-9]{3}\.[0-9]{2}$|^[0-9]{2}[A-Z]{1,2}[0-9]{4,5}$",
                regex_error_message="Biển số xe không đúng định dạng chuẩn (VD: 29E-123.45 hoặc 51H12345)!"
            )
        ),
        FormQuestion(
            id="gross_revenue",
            title="8. Tổng doanh thu ca xe (VNĐ)",
            type=QuestionType.NUMBER,
            required=False,
            description="Tổng số tiền hiển thị trên ứng dụng tài xế cuối ca",
            sample_response="1250000",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            ),
            validation=ValidationRule(min_value=0, max_value=50000000)
        ),
        FormQuestion(
            id="camera_receipt",
            title="9. Chụp ảnh màn hình doanh thu hoặc đồng hồ xe",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            description="Chụp trực tiếp từ camera phần cứng, khóa chức năng chọn ảnh thư viện để đảm bảo tính xác thực",
            sample_response="Đặt hóa đơn hoặc đồng hồ xe rõ nét trong khung căn chỉnh",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            ),
            sample_images=[
                "https://images.unsplash.com/photo-1554224155-8d04cb21cd6c?w=600&auto=format&fit=crop&q=80",
                "https://images.unsplash.com/photo-1580273916550-e323be2ae537?w=600&auto=format&fit=crop&q=80"
            ],
        )
    ]
)
