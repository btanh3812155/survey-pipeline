import re
import math
import unicodedata
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator
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
    operator: str = "equals" # "equals", "not_equals", "contains", "in", "empty", "not_empty"
    expected_value: Any = None # scalar, or list of accepted values (one per line in the editor)

def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False

def _rule_values(expected: Any) -> List[str]:
    if expected is None:
        return []
    if isinstance(expected, (list, tuple, set)):
        raw = list(expected)
    elif isinstance(expected, str):
        raw = expected.splitlines() if "\n" in expected else [expected]
    else:
        raw = [expected]
    return [str(v).strip() for v in raw if not _is_blank(v)]

def _answer_values(answer: Any) -> List[str]:
    if isinstance(answer, (list, tuple, set)):
        return [str(v).strip() for v in answer if not _is_blank(v)]
    if _is_blank(answer):
        return []
    return [str(answer).strip()]

def evaluate_condition(rule: ConditionalRule, answer: Any) -> bool:
    """Single source of truth for conditional branching (mirrored by dynamic_form.html)."""
    op = (rule.operator or "equals").strip().lower()
    values = _rule_values(rule.expected_value)
    if op == "empty":
        return _is_blank(answer)
    if op == "not_empty":
        return not _is_blank(answer)
    if op in ("equals", "not_equals", "in", "contains"):
        answers_seen = _answer_values(answer)
        if op == "contains":
            return any(v in a for v in values for a in answers_seen)
        if isinstance(answer, (list, tuple, set)):
            if op == "in":
                matched = bool(set(answers_seen) & set(values))
            else:  # equals: the whole selection must match the accepted set
                matched = bool(values) and set(answers_seen) == set(values)
        else:
            matched = len(answers_seen) == 1 and answers_seen[0] in values
        return (not matched) if op == "not_equals" else matched
    return not _is_blank(answer)

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
    show_sample_response: bool = True
    sample_images: Optional[List[str]] = None # List of sample image URLs for visual guidance
    options: Optional[List[Any]] = None # For single_choice, multiple_choice, dropdown
    group_compatibility: Optional[Dict[str, List[str]]] = None # For multiple_choice mutual exclusivity
    validation: Optional[ValidationRule] = None
    condition_groups: List[List[ConditionalRule]] = Field(default_factory=list) # OR of AND groups; at least one group must fully match
    is_consent_gate: bool = False # every following question depends on this answer
    consent_accept_values: List[str] = Field(default_factory=list) # empty = any non-blank answer
    image_checks: Optional[ImageCheckConfig] = None
    allow_multiple_images: bool = False
    min_images: Optional[int] = None
    max_images: Optional[int] = None
    exact_image_count: Optional[int] = None
    image_slots: Optional[List[Dict[str, Any]]] = None  # e.g. [{"slot": 0, "title": "Đồng hồ km", "role": "odometer", "required": True}, ...]

    @model_validator(mode="after")
    def _normalize_condition_groups(self) -> "FormQuestion":
        cleaned: List[List[ConditionalRule]] = []
        seen_groups = set()
        for group in self.condition_groups:
            unique: List[ConditionalRule] = []
            for rule in group:
                if rule not in unique:
                    unique.append(rule)
            if not unique:
                continue
            key = tuple((r.depends_on_question_id, r.operator, str(r.expected_value)) for r in unique)
            if key in seen_groups:
                continue
            seen_groups.add(key)
            cleaned.append(unique)
        self.condition_groups = cleaned
        return self

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_conditionals(cls, data: Any) -> Any:
        """Fold legacy `conditional` / `conditionals` into a single AND group."""
        if not isinstance(data, dict):
            return data
        folded = dict(data)
        legacy_single = folded.pop("conditional", None)
        legacy_list = folded.pop("conditionals", None)
        groups = [list(g) for g in (folded.get("condition_groups") or []) if isinstance(g, list)]
        if legacy_list:
            groups.append(list(legacy_list))
        if legacy_single:
            groups.append([legacy_single])
        folded["condition_groups"] = groups
        return folded

class FormModuleInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    source: str = "question"  # question | output | module | literal
    question_id: Optional[str] = None
    module_uid: Optional[str] = None
    output_key: Optional[str] = None
    literal: Any = None

    @model_validator(mode="after")
    def _source_contract(self) -> "FormModuleInput":
        if self.source not in {"question", "output", "module", "literal"}:
            raise ValueError("module input source must be question, output, module or literal")
        if self.source == "question" and not self.question_id:
            raise ValueError(f"module input '{self.name}' requires question_id")
        if self.source in {"output", "module"} and not self.module_uid:
            raise ValueError(f"module input '{self.name}' requires module_uid")
        if self.source == "output" and not self.output_key:
            raise ValueError(f"module input '{self.name}' requires output_key")
        return self


class FormModuleOutput(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=64)


class FormModule(BaseModel):
    uid: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(..., min_length=1, max_length=100)
    kind: str = "logic"  # logic | engine | all | distribution
    module_id: Optional[str] = None
    enabled: bool = True
    scope: str = "form"  # form | question | slot
    question_id: Optional[str] = None
    slot_index: Optional[int] = None
    inputs: List[FormModuleInput] = Field(default_factory=list)
    outputs: List[FormModuleOutput] = Field(default_factory=list)
    reject_on_false: bool = False
    reject_message: str = ""

    @model_validator(mode="after")
    def _module_contract(self) -> "FormModule":
        if self.kind not in {"logic", "engine", "all", "distribution"}:
            raise ValueError("module kind must be logic, engine, all or distribution")
        if self.kind in {"logic", "engine"} and not self.module_id:
            raise ValueError(f"module '{self.name}' requires module_id")
        if self.kind in {"all", "distribution"} and self.module_id:
            raise ValueError(f"module '{self.name}' must not set module_id")
        if self.kind == "distribution":
            if self.scope != "form":
                raise ValueError(f"distribution module '{self.name}' must use form scope")
            if self.reject_on_false:
                raise ValueError(f"distribution module '{self.name}' cannot reject submissions")
            for inp in self.inputs:
                if inp.source != "question":
                    raise ValueError(
                        f"distribution module '{self.name}' inputs must reference questions"
                    )
        if self.scope not in {"form", "question", "slot"}:
            raise ValueError("module scope must be form, question or slot")
        if self.scope == "form":
            self.question_id = None
            self.slot_index = None
        elif self.scope == "question":
            if not self.question_id:
                raise ValueError(f"question-scoped module '{self.name}' requires question_id")
            self.slot_index = None
        else:
            if not self.question_id or self.slot_index is None:
                raise ValueError(
                    f"slot-scoped module '{self.name}' requires question_id and slot_index"
                )
            if self.slot_index < 0:
                raise ValueError(f"slot-scoped module '{self.name}' has a negative slot_index")
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError(f"module '{self.name}' input names must be unique")
        keys = [item.key for item in self.outputs]
        if len(keys) != len(set(keys)):
            raise ValueError(f"module '{self.name}' output keys must be unique")
        names_out = [item.name for item in self.outputs]
        if len(names_out) != len(set(names_out)):
            raise ValueError(f"module '{self.name}' output names must be unique")
        return self


class FormSettings(BaseModel):
    response_limit: Optional[int] = None # Cap total responses
    current_responses: int = 0
    access_control: str = "public" # "public", "private"
    allow_anonymous: bool = False
    allow_edit_after_submit: bool = False
    notify_telegram_chat_id: Optional[int] = None
    notify_email: Optional[str] = None
    decree13_required: bool = True
    ctv_ref_enabled: bool = True # allow ?ref=<ctv> attribution links for this form
    # Free-form labels used by CTV quota tag scopes (normalized to lowercase slugs).
    tags: List[str] = Field(default_factory=list)
    # Who receives the Telegram verdict for a submission:
    #   "none"     -> do not notify anyone
    #   "ctv"      -> the collaborator/CTV recorded on the submission (default)
    #   "question" -> the answer of tg_response_question_id (e.g. driver phone),
    #                 resolved through authorized_users (phone -> telegram id)
    tg_response_source: str = "ctv"
    tg_response_question_id: Optional[str] = None
    is_test_mode: bool = False
    image_checks: ImageCheckConfig = Field(default_factory=ImageCheckConfig)
    modules: List[FormModule] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: Any) -> List[str]:
        """Accepts a list or a comma-separated string; stores unique lowercase slugs."""
        if value is None:
            return []
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("tags phải là danh sách hoặc chuỗi phân tách bằng dấu phẩy")
        tags: List[str] = []
        for item in value:
            tag = unicodedata.normalize("NFKD", str(item or ""))
            tag = "".join(ch for ch in tag if not unicodedata.combining(ch))
            tag = re.sub(r"[^a-z0-9_-]+", "_", tag.lower()).strip("_")
            if not tag:
                continue
            if len(tag) > 32:
                raise ValueError(f"tag quá dài (tối đa 32 ký tự): '{tag[:32]}...'")
            if tag not in tags:
                tags.append(tag)
        if len(tags) > 20:
            raise ValueError("tối đa 20 tag cho mỗi biểu mẫu")
        return tags

MAX_QUESTIONS_PER_FORM: int = 100
MAX_FORM_MODULES: int = 50

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
            for group in q.condition_groups:
                for rule in group:
                    parent_id = rule.depends_on_question_id
                    if parent_id == q.id:
                        raise ValueError(f"Câu hỏi '{q.id}' không thể tự phụ thuộc vào chính nó!")
                    if parent_id not in declared_ids:
                        raise ValueError(f"Điều kiện câu hỏi '{q.id}' phụ thuộc vào câu hỏi chưa được khai báo trước đó: '{parent_id}'")
            declared_ids.add(q.id)

        # A signature can only mean "signed": it never takes an expected value
        types = {q.id: q.type for q in self.questions}
        for q in self.questions:
            for group in q.condition_groups:
                for rule in group:
                    if types.get(rule.depends_on_question_id) == QuestionType.SIGNATURE:
                        rule.operator = "not_empty"
                        rule.expected_value = None

        modules = self.settings.modules or []
        if len(modules) > MAX_FORM_MODULES:
            raise ValueError(f"Số lượng module vượt quá giới hạn ({MAX_FORM_MODULES})")
        seen_modules = set()
        output_names = set()
        question_ids = {q.id for q in self.questions}
        questions_by_id = {q.id: q for q in self.questions}
        for module in modules:
            if module.uid in seen_modules:
                raise ValueError(f"Mã module trùng lặp: '{module.uid}'")
            if module.scope == "question" and module.question_id not in question_ids:
                raise ValueError(
                    f"Module '{module.name}' gắn vào câu hỏi chưa tồn tại: '{module.question_id}'"
                )
            if module.scope == "slot":
                target = questions_by_id.get(module.question_id or "")
                if not target:
                    raise ValueError(
                        f"Module '{module.name}' gắn vào câu hỏi chưa tồn tại: '{module.question_id}'"
                    )
                slots = target.image_slots or []
                if module.slot_index is None or module.slot_index >= len(slots):
                    raise ValueError(
                        f"Module '{module.name}' tham chiếu slot ảnh không tồn tại: "
                        f"'{module.question_id}[{module.slot_index}]'"
                    )
            for inp in module.inputs:
                if inp.source == "question" and inp.question_id not in question_ids:
                    raise ValueError(
                        f"Module '{module.name}' tham chiếu câu hỏi chưa tồn tại: '{inp.question_id}'"
                    )
                if module.kind == "distribution":
                    target = questions_by_id.get(inp.question_id or "")
                    if target is None or target.type not in {
                        QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE, QuestionType.DROPDOWN,
                    }:
                        raise ValueError(
                            f"Distribution module '{module.name}' chỉ nhận câu hỏi single/multiple choice: "
                            f"'{inp.question_id}'"
                        )
                if inp.source in {"output", "module"} and inp.module_uid not in seen_modules:
                    raise ValueError(
                        f"Module '{module.name}' chỉ được tham chiếu module nằm trước nó: '{inp.module_uid}'"
                    )
            for output in module.outputs:
                if output.name in output_names:
                    raise ValueError(f"Tên output module bị trùng: '{output.name}'")
                output_names.add(output.name)
            seen_modules.add(module.uid)

        return self
def effective_gates(schema: FormSchema) -> List[FormQuestion]:
    """Explicit consent gates only; a signature gate is satisfied by being signed."""
    return [q for q in schema.questions if q.is_consent_gate]

def gate_satisfied(gate: FormQuestion, answer: Any) -> bool:
    if gate.type == QuestionType.SIGNATURE:
        return not _is_blank(answer)
    accepted = [str(v).strip() for v in (gate.consent_accept_values or []) if not _is_blank(v)]
    if not accepted:
        return not _is_blank(answer)
    return bool(set(_answer_values(answer)) & set(accepted))

def validate_submission_against_schema(schema: FormSchema, answers: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validates a submitted answer dictionary against the dynamic form schema rules and constraints.
    """
    # 0. Consent gates: an explicit refusal rejects the submission outright
    positions = {q.id: i for i, q in enumerate(schema.questions)}
    gates = sorted(((positions[g.id], g) for g in effective_gates(schema)), key=lambda entry: entry[0])
    for _, gate in gates:
        answer = answers.get(gate.id)
        if not _is_blank(answer) and not gate_satisfied(gate, answer):
            return False, "Biểu mẫu yêu cầu tài xế đồng ý điều khoản xử lý dữ liệu Nghị định 13 để tiếp tục!"

    # 1. Check response limit
    if schema.settings.response_limit and schema.settings.current_responses >= schema.settings.response_limit:
        return False, "Biểu mẫu đã đạt giới hạn số lượng phản hồi tối đa của chủ sở hữu!"

    for idx, q in enumerate(schema.questions):
        # Every question after an unsatisfied gate is hidden; all conditionals must match (AND)
        if any(gi < idx and not gate_satisfied(gate, answers.get(gate.id)) for gi, gate in gates):
            continue
        if q.condition_groups and not any(
            all(evaluate_condition(rule, answers.get(rule.depends_on_question_id)) for rule in group)
            for group in q.condition_groups
        ):
            continue
        val = answers.get(q.id)
        matching_keys = [k for k in answers.keys() if k.startswith(f"{q.id}_")]
        if val is None and matching_keys:
            val = [answers[k] for k in sorted(matching_keys)]

        # Required check
        if q.required and _is_blank(val):
            return False, f"Vui lòng trả lời câu hỏi bắt buộc: '{q.title}'"

        if _is_blank(val):
            continue
        if q.type == QuestionType.NUMBER:
            try:
                num_val = float(val)
            except (TypeError, ValueError, OverflowError):
                return False, f"Câu hỏi '{q.title}' phải là số hợp lệ!"
            if not math.isfinite(num_val):
                return False, f"Giá trị câu hỏi '{q.title}' không được là NaN/Inf!"


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
                if q.validation.min_value is not None and num_val < q.validation.min_value:
                    return False, f"Giá trị câu hỏi '{q.title}' không được nhỏ hơn {q.validation.min_value}!"
                if q.validation.max_value is not None and num_val > q.validation.max_value:
                    return False, f"Giá trị câu hỏi '{q.title}' không được lớn hơn {q.validation.max_value}!"
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
    title="Khảo Sát Thu Nhập Tài Xế GSM",
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
            is_consent_gate=True,
            description="Dùng ngón tay ký tên trực tiếp trên giấy điện tử để xác nhận thông tin khảo sát",
            sample_response="Bấm nút 'Ký tên' và vẽ chữ ký trên bảng",
        ),
        # Question 2: Consent Choice
        FormQuestion(
            id="consent_choice",
            title="2. Cam kết thông tin & Đồng ý xử lý dữ liệu Nghị định 13",
            type=QuestionType.SINGLE_CHOICE,
            required=True,
            is_consent_gate=True,
            consent_accept_values=["Tôi đồng ý"],
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
                {"slot": 0, "title": "Ảnh 1: Màn hình hồ sơ ứng dụng", "role": "app_profile", "object_type": "app_profile", "object_config": {"require_authentic": True, "require_member_tier": False, "allowed_apps": "XANH_SM"}, "required": True, "require_unique": False, "ai_check": True, "ai_detection_prompt": "Màn hình hồ sơ ứng dụng tài xế Xanh SM / GSM. Trích xuất mã đối tác, tên tài xế, và hạng thành viên.", "allow_upload": True, "tooltip": ""},
                {"slot": 1, "title": "Ảnh 2: Thân xe, đầu xe hoặc vô lăng khoang lái phương tiện", "role": "vehicle_exterior_or_interior", "object_type": "vehicle_exterior_or_interior", "object_config": {"require_plate": False, "allowed_views": "EXTERIOR,COCKPIT,STEERING_WHEEL,ODOMETER"}, "required": True, "require_unique": False, "ai_check": False, "ai_detection_prompt": "Ảnh chụp phương tiện thực tế: thân xe, đầu xe hoặc vô lăng khoang lái xe điện GSM.", "allow_upload": True, "tooltip": ""},
                {"slot": 2, "title": "Ảnh 3: Doanh thu Tuần 1 Tháng 10 - 28/09 đến 04/10", "role": "app_income_week_1", "object_type": "weekly_income", "object_config": {"period_type": "WEEKLY", "constraints": [{"field": "period_months", "op": "contains", "value": 10}, {"field": "period_years", "op": "contains", "value": 2026}]}, "required": True, "require_unique": True, "ai_check": True, "ai_detection_prompt": "Màn hình thu nhập TUẦN 1 Tháng 10 (28/09 - 04/10). Bắt buộc là báo cáo doanh thu theo TUẦN, cấm ảnh báo cáo tổng cả tháng.", "allow_upload": True, "tooltip": ""},
                {"slot": 3, "title": "Ảnh 4: Doanh thu Tuần 2 Tháng 10 - 05/10 đến 11/10", "role": "app_income_week_2", "object_type": "weekly_income", "object_config": {"period_type": "WEEKLY", "constraints": [{"field": "period_months", "op": "contains", "value": 10}, {"field": "period_years", "op": "contains", "value": 2026}]}, "required": True, "require_unique": True, "ai_check": True, "ai_detection_prompt": "Màn hình thu nhập TUẦN 2 Tháng 10 (05/10 - 11/10). Bắt buộc là báo cáo doanh thu theo TUẦN, cấm ảnh báo cáo tổng cả tháng.", "allow_upload": True, "tooltip": ""},
                {"slot": 4, "title": "Ảnh 5: Doanh thu Tuần 3 Tháng 10 - 12/10 đến 18/10", "role": "app_income_week_3", "object_type": "weekly_income", "object_config": {"period_type": "WEEKLY", "constraints": [{"field": "period_months", "op": "contains", "value": 10}, {"field": "period_years", "op": "contains", "value": 2026}]}, "required": True, "require_unique": True, "ai_check": True, "ai_detection_prompt": "Màn hình thu nhập TUẦN 3 Tháng 10 (12/10 - 18/10). Bắt buộc là báo cáo doanh thu theo TUẦN, cấm ảnh báo cáo tổng cả tháng.", "allow_upload": True, "tooltip": ""},
                {"slot": 5, "title": "Ảnh 6: Doanh thu Tuần 4 Tháng 10 - 19/10 đến 25/10", "role": "app_income_week_4", "object_type": "weekly_income", "object_config": {"period_type": "WEEKLY", "constraints": [{"field": "period_months", "op": "contains", "value": 10}, {"field": "period_years", "op": "contains", "value": 2026}]}, "required": True, "require_unique": True, "ai_check": True, "ai_detection_prompt": "Màn hình thu nhập TUẦN 4 Tháng 10 (19/10 - 25/10). Bắt buộc là báo cáo doanh thu theo TUẦN, cấm ảnh báo cáo tổng cả tháng.", "allow_upload": True, "tooltip": ""},
                {"slot": 6, "title": "Ảnh 7: Doanh thu Tuần 5 Tháng 10 - 26/10 đến 01/11", "role": "app_income_week_5", "object_type": "weekly_income", "object_config": {"period_type": "WEEKLY", "constraints": [{"field": "period_months", "op": "contains", "value": 10}, {"field": "period_years", "op": "contains", "value": 2026}]}, "required": True, "require_unique": True, "ai_check": True, "ai_detection_prompt": "Màn hình thu nhập TUẦN 5 Tháng 10 (26/10 - 01/11). Bắt buộc là báo cáo doanh thu theo TUẦN, cấm ảnh báo cáo tổng cả tháng.", "allow_upload": True, "tooltip": ""},
            ],
        ),
    ],
)

# Test mode clone with test banner enabled
TEST_TRUST_BRIDGE_SCHEMA = DEFAULT_TRUST_BRIDGE_SCHEMA.model_copy(deep=True)
TEST_TRUST_BRIDGE_SCHEMA.form_id = "income_survey_test"
TEST_TRUST_BRIDGE_SCHEMA.title = "Khảo Sát Thu Nhập (Thử Nghiệm / Test Mode)"
TEST_TRUST_BRIDGE_SCHEMA.settings.is_test_mode = True

FORM_REGISTRY: Dict[str, FormSchema] = {
    DEFAULT_TRUST_BRIDGE_SCHEMA.form_id: DEFAULT_TRUST_BRIDGE_SCHEMA,
    TEST_TRUST_BRIDGE_SCHEMA.form_id: TEST_TRUST_BRIDGE_SCHEMA,
}

