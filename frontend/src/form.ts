import {
  FormSchema,
  FormQuestion,
  FormAnswers,
  AnswerValue,
  GpsCoordinates,
  QuestionType
} from './types';
import { I18N, SupportedLanguage } from './i18n';

export interface FormManagerOptions {
  containerId: string;
  schema: FormSchema;
  initialLang?: SupportedLanguage;
  onAnswersChange?: (answers: FormAnswers) => void;
  onSubmit?: (answers: FormAnswers, gps?: GpsCoordinates | null) => Promise<void>;
}

export class FormManager {
  private schema: FormSchema;
  private answers: FormAnswers = {};
  private currentLang: SupportedLanguage;
  private container: HTMLElement | null;
  private gpsCoordinates: GpsCoordinates | null = null;
  private onAnswersChange?: (answers: FormAnswers) => void;
  private onSubmit?: (answers: FormAnswers, gps?: GpsCoordinates | null) => Promise<void>;

  constructor(options: FormManagerOptions) {
    this.schema = options.schema;
    this.currentLang = options.initialLang || 'vi';
    this.container = document.getElementById(options.containerId);
    this.onAnswersChange = options.onAnswersChange;
    this.onSubmit = options.onSubmit;
  }

  public getAnswers(): FormAnswers {
    return { ...this.answers };
  }

  public setAnswer(questionId: string, value: AnswerValue): void {
    this.answers[questionId] = value;
    this.evaluateConditionals();
    if (this.onAnswersChange) {
      this.onAnswersChange(this.getAnswers());
    }
  }

  public setLanguage(lang: SupportedLanguage): void {
    this.currentLang = lang;
    this.render();
  }

  public setGpsCoordinates(coords: GpsCoordinates): void {
    this.gpsCoordinates = coords;
  }

  public isUnlocked(): boolean {
    const hasSigned = Boolean(this.answers['driver_signature']);
    const hasConsented = this.answers['consent_choice'] === 'Tôi đồng ý';
    return hasSigned && hasConsented;
  }

  public evaluateConditionals(): void {
    const isUnlocked = this.isUnlocked();

    // Toggle lockedQuestionsNotice banner
    const noticeEl = document.getElementById('lockedQuestionsNotice');
    if (noticeEl) {
      noticeEl.style.display = isUnlocked ? 'none' : 'flex';
    }

    this.schema.questions.forEach((q) => {
      if (q.conditional) {
        const parentVal = this.answers[q.conditional.depends_on_question_id];
        const card = document.getElementById(`card_${q.id}`);
        if (!card) return;

        let isMatch = false;
        if (q.conditional.depends_on_question_id === 'consent_choice') {
          isMatch = isUnlocked;
        } else if (q.conditional.operator === 'equals') {
          isMatch = parentVal === q.conditional.expected_value;
        } else if (q.conditional.operator === 'not_equals') {
          isMatch = parentVal !== q.conditional.expected_value;
        } else if (q.conditional.operator === 'in') {
          isMatch =
            Array.isArray(q.conditional.expected_value) &&
            q.conditional.expected_value.some((expected) => expected === parentVal);
        } else if (q.conditional.operator === 'contains') {
          if (Array.isArray(parentVal)) {
            isMatch = parentVal.includes(q.conditional.expected_value as string);
          } else if (typeof parentVal === 'string') {
            isMatch = parentVal.includes(String(q.conditional.expected_value));
          }
        }

        if (isMatch) {
          card.classList.remove('hidden');
        } else {
          card.classList.add('hidden');
          delete this.answers[q.id];
        }
      }
    });
  }

  public render(): void {
    if (!this.container) return;
    this.container.innerHTML = '';
    const t = I18N[this.currentLang];

    this.schema.questions.forEach((q, idx) => {
      const card = document.createElement('div');
      card.className = 'question-card';
      card.id = `card_${q.id}`;
      card.dataset.qid = q.id;

      const cleanTitle = q.title.replace(/^[0-9]+[.:\s-]+/, '');
      const tooltipBtn = q.tooltip
        ? `<span class="q-tooltip-trigger" title="${q.tooltip}">ℹ️</span>`
        : '';

      card.innerHTML = `
        <div class="q-header">
          <div class="q-title-row">
            <span class="q-title">${idx + 1}. ${cleanTitle} ${q.required ? '<span class="q-required">*</span>' : ''} ${tooltipBtn}</span>
          </div>
          ${q.description ? `<p class="q-desc">${q.description}</p>` : ''}
        </div>
        <div class="q-body" id="body_${q.id}" style="width:100%;"></div>
        <div class="q-error" id="err_${q.id}"></div>
      `;

      this.container!.appendChild(card);

      if (q.id === 'consent_choice') {
        const banner = document.createElement('div');
        banner.id = 'lockedQuestionsNotice';
        banner.className = 'locked-notice';
        banner.style.display = this.isUnlocked() ? 'none' : 'flex';
        banner.textContent = t.locked_notice;
        this.container!.appendChild(banner);
      }

      const bodyEl = card.querySelector<HTMLElement>(`#body_${q.id}`);
      if (bodyEl) {
        this.renderQuestionInput(q, bodyEl);
      }
    });

    this.evaluateConditionals();
  }

  private renderQuestionInput(q: FormQuestion, parent: HTMLElement): void {
    if (q.type === 'text' || q.type === 'number') {
      const inp = document.createElement('input');
      inp.className = 'q-input';
      inp.placeholder = '...';
      if (q.type === 'number') {
        inp.type = 'number';
        inp.placeholder = '0';
      } else {
        const isNumbersOnly = q.id === 'driver_phone' || Boolean(q.validation?.numbers_only);
        inp.type = isNumbersOnly ? 'tel' : 'text';
      }
      inp.value = (this.answers[q.id] as string) || '';
      inp.oninput = (e: Event) => {
        const target = e.target as HTMLInputElement;
        this.setAnswer(q.id, target.value);
      };
      parent.appendChild(inp);
    } else if (q.type === 'dropdown') {
      const sel = document.createElement('select');
      sel.className = 'q-input';
      const defaultOpt = document.createElement('option');
      defaultOpt.value = '';
      defaultOpt.textContent = '-- Chọn --';
      sel.appendChild(defaultOpt);
      (q.options || []).forEach((opt) => {
        const o = document.createElement('option');
        o.value = opt;
        o.textContent = opt;
        if (this.answers[q.id] === opt) o.selected = true;
        sel.appendChild(o);
      });
      sel.onchange = (e: Event) => {
        const target = e.target as HTMLSelectElement;
        this.setAnswer(q.id, target.value);
      };
      parent.appendChild(sel);
    } else if (q.type === 'single_choice') {
      const list = document.createElement('div');
      list.className = 'choices-list';
      (q.options || []).forEach((opt) => {
        const item = document.createElement('div');
        item.className = 'choice-item' + (this.answers[q.id] === opt ? ' selected' : '');
        item.textContent = opt;
        item.onclick = () => {
          this.setAnswer(q.id, opt);
          list.querySelectorAll('.choice-item').forEach((el) => el.classList.remove('selected'));
          item.classList.add('selected');
        };
        list.appendChild(item);
      });
      parent.appendChild(list);
    } else if (q.type === 'multiple_choice') {
      const list = document.createElement('div');
      list.className = 'choices-list';
      const curSelected = Array.isArray(this.answers[q.id])
        ? (this.answers[q.id] as string[])
        : [];
      (q.options || []).forEach((opt) => {
        const item = document.createElement('div');
        const isSel = curSelected.includes(opt);
        item.className = 'choice-item' + (isSel ? ' selected' : '');
        item.textContent = opt;
        item.onclick = () => {
          let updated = Array.isArray(this.answers[q.id])
            ? [...(this.answers[q.id] as string[])]
            : [];
          if (updated.includes(opt)) {
            updated = updated.filter((x) => x !== opt);
            item.classList.remove('selected');
          } else {
            updated.push(opt);
            item.classList.add('selected');
          }
          this.setAnswer(q.id, updated);
        };
        list.appendChild(item);
      });
      parent.appendChild(list);
    }
  }
}
