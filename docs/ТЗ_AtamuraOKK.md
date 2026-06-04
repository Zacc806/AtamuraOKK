# Техническое задание — AtamuraOKK

> **ФИНАЛЬНЫЙ СТЕК ПРОВАЙДЕРОВ (2026-06-04) — приоритет над деталями ниже.**
> - **Транскрипция:** **OpenAI gpt-4o-transcribe** для русского + **Yandex SpeechKit** для казахского /
>   «шала казахского». Реализация: `transcription/router.py` (`build_transcriber` →
>   `LanguageRoutedTranscriber`: транскрибирует на OpenAI, эскалирует казах-сигнал в Yandex),
>   `transcription/openai_transcribe.py` (OpenAI), `transcription/yandex_speech.py` (SpeechKit).
>   faster-whisper остался ТОЛЬКО для оффлайн WER-спайка (`uv sync --group spike`).
> - **Оценка:** **Anthropic Claude Sonnet** (`scoring/anthropic.py`, модель
>   `ATAMURAOKK_ANTHROPIC_MODEL=claude-sonnet-4-6`). Claude тянет ru+kk одной моделью —
>   отдельный язык-роутер для скоринга не нужен.
> - **Groq полностью удалён** из стека (и транскрипция, и скоринг).
> - **Рубрика по умолчанию:** `tm_call_v3` (обновлённый чек-лист звонка из xlsx, 100 баллов).
> - **Ключи для прода:** `ATAMURAOKK_OPENAI_API_KEY` (транскрипция ru), `ATAMURAOKK_YANDEX_API_KEY`+
>   `_FOLDER_ID` (SpeechKit), `ATAMURAOKK_ANTHROPIC_API_KEY` (скоринг), Bitrix-вебхук со scopes.
> - **Скрипт-отклонение:** ИИ дополнительно сверяет разговор со скриптом продаж (скрипты Pavel
>   пришлёт) и оценивает отклонение — добавляется в Anthropic-промт/результат отдельным измерением.
>
> Разделы ниже, где упомянуты «Groq Whisper» для транскрипции и «Groq/YandexGPT» как основной
> скоринг — это ранний дизайн (заменён настоящим блоком).

Автоматизация отдела контроля качества (ОКК) Атамура Групп: AI-оценка звонков и встреч на базе Bitrix24

| Параметр | Значение |
|---|---|
| Версия | 1.0 |
| Дата | 3 июня 2026 г. |
| Компания | Atamura Group |
| Портал | amanat.bitrix24.kz |
| Репозиторий | `C:\AtamuraOKK` (ветка `feat/okk-scoring`) |
| Стек | FastAPI · uv · SQLAlchemy 2.0 (async) · PostgreSQL · Alembic · loguru · Python 3.12 |

---

## 1. Общее описание

### 1.1 Назначение системы

AtamuraOKK полностью заменяет ручной труд отдела контроля качества. Сегодня сотрудники ОКК прослушивают звонки менеджеров вручную и проставляют баллы по чек-листу в Excel. Система автоматизирует весь этот процесс:

1. забирает записи звонков из Bitrix24 (телефония Voximplant + внешние интеграции);
2. транскрибирует речь (Groq Whisper large-v3, при необходимости — Yandex для казахского);
3. оценивает каждый звонок по чек-листу ОКК силами LLM, возвращая структурированный балл по каждому критерию;
4. записывает результат в PostgreSQL.

Итог: вместо ручного выборочного контроля 100% звонков получают объективную, воспроизводимую оценку.

### 1.2 Границы ответственности (scope)

В **scope** этого проекта:

- автоматизация (ingestion → transcription → scoring → persist);
- контракт схемы БД (7 таблиц), на который опирается дашборд;
- калибровочный гейт (сверка AI-оценок с ручными оценками ОКК).

**Вне scope** (делает другой разработчик в том же репозитории):

- дашборд, на котором руководитель отдела видит оценки своих менеджеров;
- права доступа / row-level access по отделам в самом дашборде.

Граница между подсистемами — **схема БД**. Дашборд читает таблицы `calls`, `transcripts`, `scores`, `managers`, `departments`; мы их наполняем. Менять форму этих таблиц без согласования нельзя — это сломает дашборд (см. раздел 5).

### 1.3 Ключевые ограничения

- **Источник истины по звонкам** — Bitrix24 (cloud, REST API через inbound-webhook).
- **Объём** — ~1 640 call-events/день на портале (15 421 за 9 дней к 2026-06-03). Многие — пропущенные (duration 0, `CALL_FAILED_CODE=304`); отвеченных и записанных — подмножество, но всё равно существенно больше первоначальной оценки в ~200/день. Сайзинг и retention должны исходить из ~10–20k *оценённых* звонков/месяц.
- **Языки** — смешанные: русский и казахский, включая «шала казахский» (русско-казахская смесь). Качество распознавания и оценки казахского — главный технический риск, поэтому есть отдельная языковая маршрутизация (раздел 4) и калибровочный гейт (раздел 7).
- **Записи звонков смешанные** — ~37% имеют прямой `CALL_RECORD_URL`, ~63% доступны только через `RECORD_FILE_ID` (файл Bitrix Drive), что требует scope `disk` (раздел 9).

---

## 2. Архитектура системы

### 2.1 Поток данных

```
Bitrix24 (amanat.bitrix24.kz)
   │  voximplant.statistic.get + user.get + disk.file.get
   ▼
[Ingestion]      → calls (status=NEW)                — upsert по bitrix_call_id, маппинг менеджера
   ▼
[Download]       → audio_path (status=DOWNLOADED)    — CALL_RECORD_URL ИЛИ RECORD_FILE_ID→DOWNLOAD_URL
   ▼
[Transcription]  → transcripts (status=TRANSCRIBED)  — Groq Whisper large-v3, стерео-сплит каналов
   ▼
[Scoring]        → scores (status=SCORED)            — language-routed LLM по чек-листу ОКК
   ▼
PostgreSQL
   ▼
[Dashboard]  — другой разработчик; читает нашу БД, не входит в наш scope
```

Транскрипция и оценка спрятаны за интерфейсами `Transcriber` (`AtamuraOKK/transcription/base.py`) и `Scorer` (`AtamuraOKK/scoring/base.py`), поэтому провайдера можно сменить, не трогая пайплайн.

### 2.2 Компоненты

| Компонент | Расположение | Назначение | Статус |
|---|---|---|---|
| Настройки | `AtamuraOKK/settings.py` | pydantic-settings, префикс `ATAMURAOKK_` | готово |
| Bitrix-клиент | `AtamuraOKK/bitrix/client.py` | async-обёртка над inbound-webhook: `call`, `list` (пагинация по `start`), backoff на throttling/429/5xx | готово |
| Транскрипция | `AtamuraOKK/transcription/` | интерфейс `Transcriber` + `FasterWhisperTranscriber` (large-v3, VAD, по каналам) | интерфейс + whisper готовы |
| Оценка (scoring) | `AtamuraOKK/scoring/` | интерфейс `Scorer`, language-router, Groq/Yandex провайдеры, рубрики, prompt, парсинг JSON | готово |
| Сервис оценки | `AtamuraOKK/scoring/service.py` | `ScoringService.score_call`: транскрипт → балл → запись, идемпотентно | готово |
| Схема БД | `AtamuraOKK/db/models/`, `AtamuraOKK/db/dao/` | 7 таблиц + DAO (контракт дашборда) | готово |
| Миграции | `AtamuraOKK/db/migrations/` | Alembic; ревизия `f1a2b3c4d5e6` (OKK core schema) | готово |
| Калибровка | `AtamuraOKK/calibration/` | загрузка ручных оценок из xlsx, метрики, harness go/no-go | готово (библиотека) |
| Spike (Phase 0) | `AtamuraOKK/spike/` | CLI оценки WER по казахскому/русскому | готово |
| Ingestion-воркер | `AtamuraOKK/workers/ingest.py`, `ingestion/` | пуллинг `voximplant.statistic.get`, маппинг менеджеров, upsert | готово (код) |
| Download-воркер | `AtamuraOKK/workers/download.py` | две схемы скачивания записей, NEW→DOWNLOADED | готово (код) |
| Transcription-воркер | `AtamuraOKK/workers/transcribe.py` | разбор очереди DOWNLOADED, GroqWhisper, стерео-сплит | готово (код) |
| Scoring-воркер | `AtamuraOKK/workers/score.py` | разбор очереди TRANSCRIBED (вызывает `ScoringService`) | готово (код) |
| Планировщик | `AtamuraOKK/workers/runner.py` | APScheduler, отдельный процесс `python -m AtamuraOKK.workers` | готово (код) |
| Web (FastAPI) | `AtamuraOKK/web/` | health-check / monitoring | каркас из шаблона |

Пайплайн реализован полностью (ingest → download → transcribe → score). Код собран и проходит ruff + mypy --strict + юнит-тесты на моках; боевой прогон требует только секретов (ключи Groq/Yandex, вебхук со scopes) и поднятого Postgres.

---

## 3. Рубрики и критерии оценки

Рубрика — это версионированный чек-лист в JSON; он же источник истины и для prompt-а LLM, и для снимка в таблице `rubric_versions`. Загрузка и валидация — `AtamuraOKK/scoring/rubric.py` (`load_rubric(version)`); валидатор проверяет, что сумма `max_score` критериев равна `max_total_score` и что id критериев уникальны.

Активная рубрика задаётся `ATAMURAOKK_SCORE_RUBRIC_VERSION` (по умолчанию `tm_call_v2`). Порог прохождения — `ATAMURAOKK_SCORE_PASS_THRESHOLD=75` (на шкале 0–100).

### 3.1 tm_call_v2 — ТМ-звонки

Файл: `AtamuraOKK/scoring/rubrics/tm_call_v2.json`. Источник: Чек-лист ОКК Біржана (Апрель ТМ 2026).

- 21 критерий, `max_total_score = 100`.
- Блоки: Приветствие (1–3), Выявление потребности (4–7), Презентация (8–9), Закрытие на КЭВ (10–13), Отработка возражений (14–17), Софт-скилы (18), CRM и выполнение задач (19–20), Дожим (21).
- Самые «тяжёлые» критерии: «Несколько попыток закрытия на встречу» (id 10, макс 15) и «Целенаправленно дожимал клиента на встречу» (id 21, макс 12).

### 3.2 okk_meeting_v1 — встречи ОП

Файл: `AtamuraOKK/scoring/rubrics/okk_meeting_v1.json`. Источник: Чек-лист встречи ОП (Январь 2026), отдел ОКК.

- 20 критериев, `max_total_score = 50` → нормализуется в `score_pct` (×2 до шкалы 0–100, как и tm_call_v2).
- Блоки: Установление контакта (1–2), Выявление потребностей (3–7), Презентация (8–11), Возражения (12–15), Закрытие сделки (16–18), Софт скилы (19–20).

### 3.3 Авто-критерии (auto_check) и условные блоки

Часть критериев не оценивается LLM, а разрешается детерминированно (`Rubric.auto_scores`, `AtamuraOKK/scoring/result.py`):

- `duration <= 300` — полный балл, если длительность звонка ≤ 5 минут (tm_call_v2, критерий 7).
- `default_full` — полный балл по умолчанию (критерии, которые нельзя проверить по транскрипту: внесение данных в CRM, качество касаний — tm_call_v2, критерии 19–20).

**Блок возражений** (`objection_block` в JSON) — условный: если в звонке возражений не было, по этим критериям ставится полный балл (это передаётся LLM в правилах prompt-а, `AtamuraOKK/scoring/prompts.py`).

### 3.4 Дополнительные поля оценки

Помимо баллов по критериям, LLM возвращает (схема `LLMScore`, `AtamuraOKK/scoring/schema.py`):

- `client_agreed_meeting` — клиент явно согласился на встречу/визит;
- `manager_tone` — «вежливый» / «нейтральный» / «грубый» / «неуверенный»;
- `red_flags_found` — список красных флагов из рубрики (грубость, ложные скидки, неверная информация о цене/локации/ипотеке и т.п.);
- `summary` — краткое резюме звонка.

---

## 4. Языковая маршрутизация провайдеров

Whisper нестабильно размечает «шала казахский» (часто как `ru` с низкой уверенностью), поэтому маршрутизация — это комбинация определённого языка + вероятности + дешёвой проверки по казахским буквам/словам (`AtamuraOKK/scoring/language.py`, чистая функция `route`).

Правила маршрутизации:

| Условие | Маршрут | Провайдер |
|---|---|---|
| `language` начинается с `kk` | `kk` | YandexGPT |
| в тексте есть казахский сигнал (буквы `әғқңөұүһі` или казахские служебные слова) | `shala` | YandexGPT |
| `language` начинается с `ru` и `language_probability ≥ ATAMURAOKK_SCORE_LANG_CONFIDENCE` (0.75) | `ru` | Groq |
| `language` начинается с `ru`, но уверенность ниже порога | `shala` | YandexGPT |
| иначе (нет сигнала) | `ru` | Groq |

Почему так:

- **Русский → Groq Llama-3.3-70b** (`AtamuraOKK/scoring/groq.py`, OpenAI-совместимый API, JSON mode).
- **Казахский / «шала казахский» → YandexGPT** (`AtamuraOKK/scoring/yandex.py`, foundationModels completion API). Остальные модели плохо справляются с казахским.

Маршрутизатор — `LanguageRoutedScorer` (`AtamuraOKK/scoring/router.py`), за единым интерфейсом `Scorer`. При недоступности основного провайдера (`ProviderUnavailableError`) автоматически переключается на второй, помечает `needs_human_review=True` и пишет `meta["fallback_from"]`. Смена провайдера — это изменение только `build_scorer`, больше ничего.

Оба провайдера используют один и тот же prompt и один и тот же контракт JSON, общий retry/backoff (`BaseLLMScorer`, `AtamuraOKK/scoring/llm.py`) и единую сборку результата (`assemble_score`): баллы клампятся в `[0, max_score]`, пропущенные критерии помечаются (если пропущено ≥3 → `needs_human_review`).

---

## 5. Схема БД (контракт для дашборда)

7 таблиц. Модели — `AtamuraOKK/db/models/`, миграция — `AtamuraOKK/db/migrations/versions/2026-06-03-00-00_okk_core_schema.py` (ревизия `f1a2b3c4d5e6`). **Это контракт между нашей автоматизацией и дашбордом другого разработчика.**

| Таблица | Назначение | Ключевые поля |
|---|---|---|
| `departments` | отдел Bitrix (для прав доступа в дашборде) | `bitrix_dept_id` (unique), `name`, `head_bitrix_user_id` |
| `managers` | менеджер (оцениваемый), по `PORTAL_USER_ID` | `bitrix_user_id` (unique), `name`, `email`, `department_id` → departments |
| `calls` | очередь работ; одна строка = один звонок Bitrix | `bitrix_call_id` (unique), `manager_id`, `direction` (1 исх / 2 вх), `started_at`, `duration_sec`, `record_url`, `record_file_id`, `audio_path`, `crm_entity_*`, `status`, `error`, `failed_stage`, `attempts` |
| `transcripts` | транскрипт (1:1 с call) | `call_id` (unique), `language`, `language_probability`, `full_text`, `segments` (JSONB), `model` |
| `scores` | оценка звонка (повторная оценка по версии рубрики допускается) | `call_id`, `transcript_id`, `rubric_version`, `total_score`, `max_total`, `score_pct`, `passed`, `criteria` (JSONB), `client_agreed_meeting`, `manager_tone`, `red_flags` (JSONB), `summary`, `language`, `provider`, `model`, `needs_human_review`, `meta` (JSONB) |
| `rubric_versions` | замороженный снимок рубрики (воспроизводимость исторических оценок) | `version` (unique), `definition` (JSONB), `active` |
| `ingest_state` | курсор ingestion по источнику | `key` (unique), `last_call_id`, `last_window_end` |

Индексы, важные для дашборда и воркеров (см. `AtamuraOKK/db/models/call.py`):

- `ix_calls_status_id (status, id)` — FIFO-выборка по стадиям + `FOR UPDATE SKIP LOCKED`.
- `ix_calls_manager_started (manager_id, started_at)` — окна дашборда по менеджеру/времени.
- `ix_scores_score_pct`, `ix_scores_rubric_version` — фильтрация оценок.

DAO (`AtamuraOKK/db/dao/`): `CallDAO.claim_batch` (`FOR UPDATE SKIP LOCKED`), `ScoreDAO.create_from_result`/`exists` (идемпотентность по `call_id`+`rubric_version`), `TranscriptDAO`, `RubricVersionDAO.upsert`.

---

## 6. Жизненный цикл звонка

Статусы — `CallStatus` (`AtamuraOKK/db/models/enums.py`):

```
NEW ──download──► DOWNLOADED ──transcribe──► TRANSCRIBED ──score──► SCORED   (терминальный успех)
 │                    │                           │
 └────────────────────┴───────────────────────────┴────► FAILED   (на ошибке стадии)
                                                   └────► SKIPPED  (отвечен, но не оценивается)
```

| Статус | Значение |
|---|---|
| `NEW` | звонок принят (ingestion), запись ещё не скачана |
| `DOWNLOADED` | аудио скачано, ждёт транскрипции |
| `TRANSCRIBED` | транскрипт сохранён, ждёт оценки |
| `SCORED` | оценка записана — терминальный успех |
| `FAILED` | стадия упала (см. `error`, `failed_stage`, `attempts`) |
| `SKIPPED` | отвечен, но не оценивается (слишком короткий / нет записи / пустой транскрипт) |

Правила перехода в `SKIPPED`/`FAILED` для стадии оценки (`ScoringService.score_call`):

- транскрипт пуст или короче 100 символов → `SKIPPED` (`error="empty transcript"`);
- уже есть оценка по текущей версии рубрики → no-op (`ALREADY_SCORED`), идемпотентно;
- `ScoringError` после всех ретраев → `FAILED` (`failed_stage="score"`, `attempts += 1`).

Дополнительно: звонки короче `ATAMURAOKK_SCORE_MIN_DURATION_SEC` (60 c) предполагается флагать на ручной просмотр, а не отдавать LLM (политика на стадии оценки).

---

## 7. Калибровка (go/no-go гейт)

Калибровка отвечает на главный вопрос проекта: **согласуется ли AI-оценщик с живыми оценщиками ОКК?** Пока гейт не пройден, систему нельзя запускать на замену людям.

Модуль: `AtamuraOKK/calibration/` (зависимости группы `calib`: openpyxl).

### 7.1 Источник эталона

`AtamuraOKK/calibration/xlsx_loader.py` (`load_human_calls`) парсит ручные оценки из `Чек лист встречи ОП - Январь.xlsx`: по листу на менеджера, лист «Сводная» пропускается. Каждый звонок — группа из 3 колонок (да/нет, оценка, комментарий), 20 критериев в строках 6–26, итог (0–50) в строке 27, ID сделки извлекается из CRM-URL (`/deal/details/<id>`). Извлекается **366 человеческих оценок**.

### 7.2 Сравнение и вердикт

`AtamuraOKK/calibration/harness.py` (`compare`) джойнит AI- и человеческие оценки по `crm_deal_id` и считает метрики (`AtamuraOKK/calibration/metrics.py`, чистые функции):

- по итоговому баллу: MAE, RMSE, Pearson, Spearman (на нормализованной шкале 0–100);
- по решению pass/fail: confusion matrix, accuracy/precision/recall, Cohen's kappa;
- по каждому критерию: Cohen's kappa (бинарно: балл > 0).

Пороги (`DEFAULT_GATES`, переопределяемы):

| Гейт | Порог |
|---|---|
| `passfail_kappa_min` | ≥ 0.6 |
| `total_mae_max` | ≤ 7.0 |
| `spearman_min` | ≥ 0.7 |

Вердикт: все гейты пройдены → **PASS**; ни одного → **FAIL**; частично → **REVISE**.

Чистая часть (загрузка + сравнение + вердикт) полностью тестируема на синтетике. Для реального прогона нужны ключи провайдеров и транскрипты оценённых встреч (`ai`-карта `ScoreResult` по `crm_deal_id`).

---

## 8. Конфигурация

Все переменные с префиксом `ATAMURAOKK_` (часть принимает и «голое» имя как alias). Источник истины — `AtamuraOKK/settings.py`; пример — `.env.example`.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ATAMURAOKK_HOST` / `_PORT` | 127.0.0.1 / 8000 | хост/порт API |
| `ATAMURAOKK_ENVIRONMENT` | dev | окружение |
| `ATAMURAOKK_LOG_LEVEL` | INFO | уровень логов |
| `ATAMURAOKK_DB_HOST` / `_PORT` / `_USER` / `_PASS` / `_BASE` | localhost / 5432 / AtamuraOKK ×3 | Postgres (в compose host = имя сервиса `AtamuraOKK-db`) |
| `ATAMURAOKK_BITRIX_WEBHOOK` (alias `BITRIX_WEBHOOK`) | — | полный URL inbound-webhook, scopes **crm, telephony, disk, user** |
| `ATAMURAOKK_BITRIX_MAX_RETRIES` / `_RETRY_BASE_DELAY` | 5 / 1.0 | backoff на throttling |
| `ATAMURAOKK_GROQ_API_KEY` (alias `GROQ_API_KEY`) | — | ключ Groq (`gsk_...`) |
| `ATAMURAOKK_GROQ_WHISPER_MODEL` | whisper-large-v3 | модель транскрипции |
| `ATAMURAOKK_GROQ_SCORING_MODEL` | llama-3.3-70b-versatile | модель оценки русских звонков |
| `ATAMURAOKK_YANDEX_API_KEY` (alias `YANDEX_API_KEY`) | — | ключ Yandex |
| `ATAMURAOKK_YANDEX_FOLDER_ID` (alias `YANDEX_FOLDER_ID`) | — | folder id Yandex Cloud |
| `ATAMURAOKK_YANDEX_GPT_MODEL` | yandexgpt/latest | модель оценки казахских звонков |
| `ATAMURAOKK_KAZAKH_TRANSCRIBER` | groq | чем транскрибировать казахский: groq \| yandex (решается после WER-гейта) |
| `ATAMURAOKK_SCORE_RUBRIC_VERSION` | tm_call_v2 | активная рубрика |
| `ATAMURAOKK_SCORE_PASS_THRESHOLD` | 75 | порог прохождения (0–100) |
| `ATAMURAOKK_SCORE_LANG_CONFIDENCE` | 0.75 | порог уверенности для маршрута `ru` |
| `ATAMURAOKK_SCORE_MAX_RETRIES` / `_RETRY_BASE_DELAY` | 5 / 1.0 | ретраи оценки |
| `ATAMURAOKK_SCORE_CONCURRENCY` / `_TRANSCRIBE_CONCURRENCY` | 4 / 6 | параллелизм воркеров |
| `ATAMURAOKK_SCORE_MIN_DURATION_SEC` | 60 | минимум для LLM-оценки (короче → ручной просмотр) |
| `ATAMURAOKK_SCORE_MAX_TRANSCRIPT_CHARS` | 24000 | потолок символов транскрипта в LLM (cost guard) |
| `ATAMURAOKK_WHISPER_MODEL` / `_DEVICE` / `_COMPUTE_TYPE` | large-v3 / auto / default | локальный faster-whisper для spike |
| `ATAMURAOKK_SPIKE_DIR` | `$TMPDIR/atamura_spike` | каталог артефактов spike |

---

## 9. Известные ограничения и блокеры

| # | Блокер | Влияние | Действие |
|---|---|---|---|
| 1 | Нет ключей провайдеров (`GROQ_API_KEY`, `YANDEX_API_KEY` + `FOLDER_ID`) | Без них нельзя транскрибировать/оценивать и прогнать калибровочный гейт | Получить ключи у оператора |
| 2 | Webhook без scope `disk` | Скачиваются только ~37% native-Voximplant записей; ~63% (через `RECORD_FILE_ID`) недоступны | Добавить scope `disk` в inbound-webhook |
| 3 | Webhook без scope `user` | `user.get` → `insufficient_scope`; нельзя смапить `PORTAL_USER_ID` → менеджер (name/email/department) | Добавить scope `user` |
| 4 | Сейчас выданы только `crm`, `telephony` | Требуется полный набор **crm, telephony, disk, user** | Перевыпустить webhook с нужными scopes |
| 5 | Не пройден WER-гейт по казахскому (Phase 0) | Не подтверждено качество распознавания казахского; нужны окружение Whisper + ffmpeg + ручные эталоны | См. `docs/transcription-eval.md` |
| 6 | Postgres должен быть поднят и мигрирован | Без БД пайплайн не работает | `docker compose up -d db` → `alembic upgrade head` |
| 7 | Диаризация не полностью устранима | Native-звонки стерео (агент/клиент по каналам), но внешние интеграции могут быть моно → `pyannote` fallback вероятно нужен | Учесть в transcription-воркере |
| 8 | `voximplant.statistic.get` игнорирует `ORDER` | Строки приходят по возрастанию `ID`; курсор `ingest_state` ненадёжен | Корректность держится на unique-upsert по `calls.bitrix_call_id`, не на курсоре |

---

## 10. Рекомендации по развитию

Пайплайн (ingest → download → transcribe → score) и сервис `worker` в compose уже
реализованы. Ниже — что нужно для боевого запуска и дальнейшего развития:

1. **Выдать секреты**: ключи `ATAMURAOKK_GROQ_API_KEY` / `ATAMURAOKK_YANDEX_API_KEY` (+ `_FOLDER_ID`) и вебхук со scopes `crm,telephony,disk,user`; поднять Postgres.
2. **Пройти калибровочный гейт** на реальных данных до запуска на замену людям; зафиксировать вердикт PASS/REVISE/FAIL и метрики (нужны ключи + транскрипты размеченных встреч). CLI-обёртка вокруг `compare` — тонкая, осталось добавить.
3. **Решить судьбу казахской транскрипции** по результатам WER (Groq vs Yandex SpeechKit), выставить `ATAMURAOKK_KAZAKH_TRANSCRIBER`.
4. **DB-интеграционные тесты** прогнать на поднятом Postgres (в dev-среде сейчас недоступен).
5. **Наблюдаемость**: ежедневная сводка (принято/транскрибировано/оценено, ошибки, минуты аудио, токены LLM, оценка стоимости) и алерты на повторные `FAILED`.
6. **Ретеншн и комплаенс**: политика хранения записей/транскриптов с учётом закона РК о персональных данных; согласие на запись.
7. **Диаризация** для моно-записей внешних интеграций (`pyannote`) — если калибровка покажет необходимость.

---

*Документ составлен 3 июня 2026 г. · Atamura Group · Версия 1.0*
