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

# Default Production Form Schema for Trust Bridge Fleet Survey
DEFAULT_TRUST_BRIDGE_SCHEMA = FormSchema(
    form_id="income_survey",
    title="Khảo Sát Thu Nhập & Hoạt Động Vận Tải",
    description="Biểu mẫu khảo sát doanh thu, thu nhập thực tế và ca làm việc của tài xế và shipper",
    settings=FormSettings(
        response_limit=20000,
        access_control="public",
        allow_anonymous=False,
        allow_edit_after_submit=False,
        notify_telegram_chat_id=-1001234567890,
        decree13_required=True,
        is_test_mode=True
    ),
    questions=[
        # Question 1: Signature
        FormQuestion(
            id="driver_signature",
            title="1. Chữ ký xác nhận",
            type=QuestionType.SIGNATURE,
            required=True,
            description="Ký tên bằng ngón tay để xác nhận đồng ý tham gia khảo sát thu nhập",
            sample_response="Bấm nút 'Ký tên' và vẽ chữ ký trên bảng"
        ),
        # Question 2: Consent Choice
        FormQuestion(
            id="consent_choice",
            title="2. Cam kết thông tin thu nhập",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Tôi đồng ý", "Tôi không đồng ý"],
            description="Tôi xác nhận các số liệu thu nhập và hình ảnh ứng dụng cung cấp là trung thực",
            sample_response="Tôi đồng ý"
        ),
        # Subsequent Questions - ALL CONDITIONAL on consent_choice == 'Tôi đồng ý'
        FormQuestion(
            id="driver_phone",
            title="3. Số điện thoại nhận thù lao khảo sát",
            type=QuestionType.TEXT,
            required=True,
            description="Số điện thoại nhận tiền phụ cấp khảo sát (qua ví điện tử hoặc chuyển khoản)",
            sample_response="0988123456",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            ),
            validation=ValidationRule(
                regex_pattern=r"^0[0-9]{9,10}$",
                regex_error_message="Số điện thoại phải bắt đầu bằng số 0 và có từ 10 đến 11 chữ số"
            )
        ),
        FormQuestion(
            id="driver_name",
            title="4. Họ và tên người tham gia",
            type=QuestionType.TEXT,
            required=True,
            description="Họ và tên đầy đủ của tài xế hoặc shipper",
            sample_response="Nguyễn Văn An",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            )
        ),
        FormQuestion(
            id="platform_app",
            title="5. Ứng dụng / Nền tảng hoạt động chính",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Grab", "Be", "Xanh SM", "ShopeeFood", "Ahamove / Gojek", "Taxi truyền thống", "Khác"],
            description="Chọn ứng dụng mang lại phần lớn thu nhập của bạn",
            sample_response="Grab",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            )
        ),
        FormQuestion(
            id="vehicle_category",
            title="6. Loại phương tiện làm việc",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            options=["Xe máy 2 bánh", "Ô tô 4 - 7 chỗ", "Xe tải nhỏ giao hàng"],
            description="Phương tiện bạn sử dụng để chạy chở khách hoặc giao hàng",
            sample_response="Xe máy 2 bánh",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            )
        ),
        FormQuestion(
            id="working_hours_day",
            title="7. Thời gian chạy xe trung bình mỗi ngày",
            type=QuestionType.DROPDOWN,
            required=True,
            options=["Dưới 4 tiếng (Bán thời gian)", "4 - 8 tiếng (Tiêu chuẩn)", "8 - 12 tiếng (Toàn thời gian)", "Trên 12 tiếng (Tăng ca)"],
            description="Tổng số giờ mở ứng dụng nhận cuốc / đơn hàng mỗi ngày",
            sample_response="8 - 12 tiếng (Toàn thời gian)",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            )
        ),
        FormQuestion(
            id="daily_net_income",
            title="8. Thu nhập thực nhận trung bình mỗi ngày (VNĐ)",
            type=QuestionType.NUMBER,
            required=True,
            description="Số tiền thực tế cầm về sau khi trừ chiết khấu ứng dụng và tiền xăng/điện",
            sample_response="650000",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            ),
            validation=ValidationRule(min_value=0, max_value=10000000)
        ),
        FormQuestion(
            id="monthly_gross_revenue",
            title="9. Tổng doanh thu tháng gần nhất trên ứng dụng (VNĐ)",
            type=QuestionType.NUMBER,
            required=False,
            description="Tổng số tiền hiển thị trên mục Báo cáo tháng của ứng dụng tài xế",
            sample_response="18500000",
            conditional=ConditionalRule(
                depends_on_question_id="consent_choice",
                operator="equals",
                expected_value="Tôi đồng ý"
            ),
            validation=ValidationRule(min_value=0, max_value=100000000)
        ),
        FormQuestion(
            id="camera_receipt",
            title="10. Chụp ảnh màn hình mục Thu nhập hoặc Ví tài xế",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            description="Chụp trực tiếp màn hình mục Báo cáo Thu nhập / Ví tiền trên ứng dụng tài xế của bạn",
            sample_response="Đặt rõ nét màn hình báo cáo thu nhập hoặc sao kê ví vào khung",
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

FORM_REGISTRY: Dict[str, FormSchema] = {
    DEFAULT_TRUST_BRIDGE_SCHEMA.form_id: DEFAULT_TRUST_BRIDGE_SCHEMA
}
