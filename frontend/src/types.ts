export type QuestionType =
  | 'text'
  | 'number'
  | 'single_choice'
  | 'multiple_choice'
  | 'dropdown'
  | 'camera_webrtc'
  | 'signature';

export type ConditionalOperator = 'equals' | 'not_equals' | 'contains' | 'in';

export interface ConditionalRule {
  depends_on_question_id: string;
  operator: ConditionalOperator;
  expected_value: string | number | boolean | (string | number)[];
}

export interface ValidationRule {
  min_value?: number | null;
  max_value?: number | null;
  regex_pattern?: string | null;
  regex_error_message?: string | null;
  max_length?: number | null;
  min_length?: number | null;
  numbers_only?: boolean;
}

export interface ImageSlot {
  slot: number;
  title: string;
  role: string;
  required: boolean;
  ai_check?: boolean;
  tooltip?: string;
  allow_upload?: boolean;
  require_unique?: boolean;
  ai_detection_prompt?: string;
  object_type?: string;
  object_config?: Record<string, unknown>;
}

export interface ImageCheckConfig {
  enable_blur_check?: boolean;
  blur_threshold?: number | null;
  enable_ai_vision?: boolean;
  enable_object_detection?: boolean;
  enable_vehicle_match?: boolean;
  enable_anti_fraud?: boolean;
  enable_ai_generated_check?: boolean;
  enable_screen_photo_check?: boolean;
}

export type QuestionOption = string;

export interface FormQuestion {
  id: string;
  title: string;
  type: QuestionType;
  required: boolean;
  description?: string | null;
  tooltip?: string | null;
  ai_detection_prompt?: string | null;
  sample_response?: string | null;
  sample_images?: string[] | null;
  options?: QuestionOption[] | null;
  validation?: ValidationRule | null;
  conditional?: ConditionalRule | null;
  image_checks?: ImageCheckConfig | null;
  allow_multiple_images?: boolean;
  min_images?: number | null;
  max_images?: number | null;
  exact_image_count?: number | null;
  image_slots?: ImageSlot[] | null;
}

export interface FormSettings {
  response_limit?: number | null;
  current_responses?: number;
  access_control?: 'public' | 'private';
  allow_anonymous?: boolean;
  allow_edit_after_submit?: boolean;
  notify_telegram_chat_id?: number | null;
  notify_email?: string | null;
  decree13_required?: boolean;
  is_test_mode?: boolean;
  image_checks?: ImageCheckConfig;
}

export interface FormSchema {
  form_id: string;
  title: string;
  description?: string | null;
  settings?: FormSettings;
  questions: FormQuestion[];
}

export type AnswerValue = string | number | boolean | string[] | Record<number, string> | null | undefined;

export type FormAnswers = Record<string, AnswerValue>;

export interface GpsCoordinates {
  latitude: number;
  longitude: number;
  accuracy: number;
  city?: string;
}
