# Передача разработки — AtamuraOKK

> **ОБНОВЛЕНИЕ ПРОВАЙДЕРОВ (2026-06-04) — приоритет над деталями ниже.**
> - **Транскрипция:** faster-whisper (русский, локально/бесплатно, он же детектит язык) +
>   **Yandex SpeechKit** (казахский/«шала»). `transcription/router.py` `build_transcriber`.
> - **Оценка:** **Anthropic Claude Sonnet** по умолчанию (`scoring/anthropic.py`). Groq/YandexGPT —
>   опциональная альтернатива (`ATAMURAOKK_SCORE_PROVIDER=groq_yandex`). Рубрика `okk_meeting_v1`.
> - **Секреты для прода:** `ATAMURAOKK_ANTHROPIC_API_KEY` (скоринг), `ATAMURAOKK_YANDEX_API_KEY` +
>   `ATAMURAOKK_YANDEX_FOLDER_ID` (SpeechKit для казахского), Bitrix-вебхук со scopes
>   `crm,telephony,disk,user`. Groq-ключ нужен только при `groq_yandex`.
> - **Скрипты продаж** для проверки отклонения менеджера — Pavel пришлёт; подключаются в Anthropic-скорер.
>
> Где ниже «Groq Whisper»/«Groq+Yandex как основной скоринг» — это ранний дизайн (заменён этим блоком).

AI-автоматизация отдела контроля качества (ОКК) Атамура Групп — документация для развёртывания и продолжения разработки

| Параметр | Значение |
|---|---|
| Версия | 1.0 |
| Дата | 3 июня 2026 г. |
| Компания | Atamura Group |
| Репозиторий | `C:\AtamuraOKK` · ветка `feat/okk-scoring` |
| Статус | код пайплайна завершён (ingest→download→transcribe→score, scoring, БД, калибровка, compose worker); для боевого запуска нужны секреты (ключи + scopes) и Postgres |

---

## 1. Контекст задачи

Нужно развернуть сервис, который **полностью заменяет ручной ОКК**: забирает записи звонков из Bitrix24 (`amanat.bitrix24.kz`), транскрибирует, оценивает каждый звонок по чек-листу ОКК силами LLM и пишет баллы в PostgreSQL. На той же БД другой разработчик строит дашборд, где руководитель отдела видит оценки своих менеджеров — **дашборд не входит в этот scope**, наша граница ответственности — автоматизация + контракт схемы БД (7 таблиц).

Технологический стек:

- Python 3.12, uv (управление зависимостями), FastAPI;
- SQLAlchemy 2.0 (async) + asyncpg, PostgreSQL, Alembic, loguru;
- Bitrix24 REST API (inbound webhook);
- Groq (Whisper large-v3 + Llama-3.3-70b для русских звонков);
- YandexGPT (казахский / «шала казахский»);
- развёртывание — docker-compose на Linux VDS.

Детальное ТЗ — `docs/ТЗ_AtamuraOKK.md`. Реальность портала и статус Phase 0 — `docs/transcription-eval.md`.

---

## 2. Доступы и конфигурация

Конфигурация — через переменные окружения с префиксом `ATAMURAOKK_` (часть принимает «голое» имя как alias). Файл `.env` в корне (gitignored). Шаблон — `.env.example`. Источник истины по всем полям — `AtamuraOKK/settings.py`.

Какие секреты нужны (без них пайплайн не работает):

| Секрет | Переменная(ы) | Где взять / требования |
|---|---|---|
| Bitrix inbound-webhook | `ATAMURAOKK_BITRIX_WEBHOOK` (alias `BITRIX_WEBHOOK`) | полный URL `https://amanat.bitrix24.kz/rest/<user_id>/<token>/`; **обязательные scopes: `crm`, `telephony`, `disk`, `user`** |
| Groq API key | `ATAMURAOKK_GROQ_API_KEY` (alias `GROQ_API_KEY`) | ключ вида `gsk_...` (free tier) |
| YandexGPT | `ATAMURAOKK_YANDEX_API_KEY` (alias `YANDEX_API_KEY`) + `ATAMURAOKK_YANDEX_FOLDER_ID` (alias `YANDEX_FOLDER_ID`) | API-key Yandex Cloud + folder id |
| PostgreSQL | `ATAMURAOKK_DB_HOST` / `_PORT` / `_USER` / `_PASS` / `_BASE` | в compose значения по умолчанию `AtamuraOKK`; host = имя сервиса `AtamuraOKK-db` |

Важно по scopes (см. также `docs/transcription-eval.md`):

- сейчас на портале выданы только `crm`, `telephony`;
- **`disk`** нужен, чтобы скачивать ~63% записей через `RECORD_FILE_ID` → `disk.file.get` (без него доступны только ~37% native-Voximplant записей с прямым `CALL_RECORD_URL`);
- **`user`** нужен для `user.get` — маппинга `PORTAL_USER_ID` → менеджер (name/email/department), иначе `insufficient_scope`.

Минимальный рабочий `.env`:

```bash
ATAMURAOKK_BITRIX_WEBHOOK=https://amanat.bitrix24.kz/rest/65998/<token>/
ATAMURAOKK_GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx
ATAMURAOKK_YANDEX_API_KEY=<yandex_api_key>
ATAMURAOKK_YANDEX_FOLDER_ID=<folder_id>
ATAMURAOKK_DB_HOST=AtamuraOKK-db
ATAMURAOKK_DB_USER=AtamuraOKK
ATAMURAOKK_DB_PASS=AtamuraOKK
ATAMURAOKK_DB_BASE=AtamuraOKK
ATAMURAOKK_SCORE_RUBRIC_VERSION=tm_call_v2
ATAMURAOKK_SCORE_PASS_THRESHOLD=75
```

---

## 3. Команды развёртывания (по порядку)

### 3.1 Локально (разработка)

```bash
# 1. Установить зависимости (runtime + dev)
uv sync

# Опционально:
uv sync --group spike   # faster-whisper, jiwer, soundfile (Phase 0 WER-эвал)
uv sync --group calib   # openpyxl (калибровочный harness)

# 2. Поднять Postgres
docker compose up -d db          # или: make up

# 3. Применить миграции
uv run alembic upgrade head      # или: make migrate

# 4. Запустить API (health/monitoring)
uv run python -m AtamuraOKK
```

### 3.2 Прод (docker-compose на Linux VDS)

```bash
# 1. Положить .env рядом с docker-compose.yml (раздел 2)

# 2. Поднять БД
docker compose up -d db

# 3. Прогнать миграции (сервис migrator: alembic upgrade head, отрабатывает и завершается)
docker compose up migrator

# 4. Поднять API и воркеры
docker compose up -d api worker

# 5. Проверить статус
docker compose ps
```

`docker-compose.yml` содержит сервисы `db` (postgres:18.3), `migrator` (`alembic upgrade head`, `restart: "no"`), `api` и `worker` (`python -m AtamuraOKK.workers`, `restart: always`, том `okk-audio` для записей). `api` и `worker` зависят от `db` (healthcheck) и успешного завершения `migrator`.

---

## 4. Структура проекта

```
AtamuraOKK/
├── settings.py            # pydantic-settings, префикс ATAMURAOKK_
├── __main__.py            # запуск uvicorn (API)
├── bitrix/
│   └── client.py          # async Bitrix-клиент: call/list, пагинация, backoff
├── transcription/
│   ├── base.py            # интерфейс Transcriber + Segment/TranscriptResult
│   └── whisper.py         # FasterWhisperTranscriber (large-v3, VAD, по каналам)
├── scoring/               # подсистема оценки
│   ├── base.py            # интерфейс Scorer + CallForScoring/ScoreResult/CriterionScore
│   ├── language.py        # route(): ru -> Groq, kk/shala -> Yandex
│   ├── router.py          # LanguageRoutedScorer + build_scorer (единственный seam)
│   ├── llm.py             # BaseLLMScorer: prompt -> LLM -> retry -> assemble
│   ├── groq.py            # GroqScorer (Llama-3.3-70b, JSON mode)
│   ├── yandex.py          # YandexScorer (foundationModels completion API)
│   ├── prompts.py         # build_prompt (билингв RU/KK, из рубрики)
│   ├── schema.py          # LLMScore + parse_llm_json (толерантный парсер)
│   ├── result.py          # assemble_score (LLM + auto_check, клампы, тоталы)
│   ├── rubric.py          # Rubric/Criterion + load_rubric + валидация
│   ├── service.py         # ScoringService.score_call (оценка + запись, идемпотентно)
│   ├── errors.py          # ScoringError / MalformedOutputError / ProviderUnavailableError
│   └── rubrics/
│       ├── tm_call_v2.json       # ТМ-звонки, 21 крит, max 100
│       └── okk_meeting_v1.json   # встречи ОП, 20 крит, max 50
├── db/                    # контракт БД (дашборд читает эти таблицы)
│   ├── models/            # call, transcript, score, manager, department,
│   │                      #   rubric_version, ingest_state, enums (CallStatus)
│   ├── dao/               # call_dao, transcript_dao, score_dao (RubricVersionDAO)
│   └── migrations/versions/2026-06-03-00-00_okk_core_schema.py  # ревизия f1a2b3c4d5e6
├── calibration/           # go/no-go гейт (AI vs human)
│   ├── xlsx_loader.py     # load_human_calls (366 ручных оценок из xlsx)
│   ├── metrics.py         # MAE/RMSE/Pearson/Spearman/Cohen's kappa
│   └── harness.py         # compare() -> CalibrationReport (PASS/REVISE/FAIL)
├── spike/                 # Phase 0: CLI WER-эвала (fetch/download/transcribe/wer)
└── web/                   # FastAPI каркас (health/monitoring)
```

---

## 5. Как запустить оценку и калибровку

### 5.1 Оценить один транскрибированный звонок

`ScoringService.score_call(call)` принимает уже `TRANSCRIBED` звонок, оценивает и пишет результат идемпотентно. Обвязка (CallDAO/TranscriptDAO/ScoreDAO + `build_scorer`) собирается на сессии БД. Оценщик строится из настроек:

```python
from AtamuraOKK.scoring.router import build_scorer
from AtamuraOKK.scoring.base import CallForScoring

scorer = build_scorer()  # рубрика из ATAMURAOKK_SCORE_RUBRIC_VERSION; Groq+Yandex+router
result = await scorer.score(CallForScoring(
    text="[agent] ... [customer] ...",
    duration_sec=180,
    language="ru",
    language_probability=0.92,
))
# result.score_pct, result.passed, result.criteria, result.red_flags, ...
```

### 5.2 Phase 0 spike (WER казахского/русского)

```bash
make install-spike            # faster-whisper, jiwer, soundfile
# brew install ffmpeg / apt install ffmpeg   (split каналов + probe)

make spike-fetch              # -> calls.json (recent answered+recorded)
make spike-download           # -> audio/<call>.mp3  (нужен scope disk)
make spike-transcribe         # -> transcripts/<call>.json (стерео-сплит + merge)
# создать эталоны: $SPIKE_DIR/refs/<call_id>.txt + refs/labels.json {"<id>":"ru"|"kk"}
make spike-wer                # -> таблица WER по языкам
```

CLI: `uv run python -m AtamuraOKK.spike <fetch|download|transcribe|wer>` (см. `AtamuraOKK/spike/__main__.py`).

### 5.3 Калибровочный гейт (AI vs human)

Модуль `AtamuraOKK/calibration/` — библиотека (отдельного CLI пока нет). Алгоритм:

```python
from AtamuraOKK.calibration import load_human_calls, compare

human = load_human_calls("Чек лист встречи ОП - Январь.xlsx")   # 366 оценок
# ai: dict[crm_deal_id -> ScoreResult] — прогнать scorer по транскриптам этих же встреч
report = compare(human, ai, max_total=50, pass_threshold=75)
print(report.verdict, report.total_mae, report.passfail["kappa"], report.spearman)
```

Гейты по умолчанию: kappa ≥ 0.6, MAE ≤ 7.0, Spearman ≥ 0.7. Все пройдены → PASS, ни одного → FAIL, иначе REVISE.

---

## 6. Мониторинг и логи

- **Логи** — loguru (`AtamuraOKK/log.py`), уровень — `ATAMURAOKK_LOG_LEVEL` (по умолчанию INFO). В docker — `docker compose logs -f api` (и `worker`, когда появится).
- **Bitrix-клиент** логирует throttling/HTTP-ретраи (WARNING с методом и задержкой).
- **Scoring** логирует провал попытки (`scorer {provider} attempt {n}/{m} failed`), fallback между провайдерами (`scorer {p} unavailable; falling back`), провал оценки звонка.
- **Health-check** — FastAPI endpoint мониторинга (`AtamuraOKK/web/api/monitoring/`); порт `ATAMURAOKK_PORT` (8000).
- Полезные сигналы из БД: распределение `calls.status`, число `scores.needs_human_review = true`, `scores.meta->>'fallback_from'`, `calls.failed_stage` + `attempts` для застрявших.

Рекомендуется (раздел 10 ТЗ): ежедневная сводка (принято/транскрибировано/оценено, ошибки, минуты аудио, токены/стоимость LLM) и алерт на повторные `FAILED`.

---

## 7. Известные проблемы и решения

| Проблема | Причина | Решение |
|---|---|---|
| Скачиваются только ~37% записей | Webhook без scope `disk`; ~63% звонков отдают только `RECORD_FILE_ID` | Добавить scope `disk`; качать через `disk.file.get` → `DOWNLOAD_URL` (а не только `CALL_RECORD_URL`) |
| `user.get` → `insufficient_scope` | Webhook без scope `user` | Добавить scope `user`; смапить `PORTAL_USER_ID` → `managers` |
| Пропускаются/дублируются звонки при пагинации | `voximplant.statistic.get` игнорирует `ORDER`, строки идут по возрастанию `ID` | Не полагаться на курсор; идемпотентность — на unique `calls.bitrix_call_id` (upsert). `ingest_state` — только окно дат |
| Bitrix отдаёт `QUERY_LIMIT_EXCEEDED` / 429 / 5xx | Rate-limit портала при высоком объёме (~1640/день) | Уже решено: `BitrixClient` ретраит `call_raw`/`list` с экспоненциальным backoff (`bitrix_max_retries`, `bitrix_retry_base_delay`). Это пофикшено в коммите `a8bd8d6` |
| Стерео-каналы дублируются (моно) | Внешние интеграции могут писать моно, а не агент/клиент по каналам | `probe_channels` проверяет число каналов; моно идёт одним `unknown`-спикером (диаризация `pyannote` — опционально позже). Split каналов — через `ffmpeg -af pan=mono` (НЕ устаревший `-map_channel`, удалён в ffmpeg 7.0) |
| Whisper метит «шала казахский» как `ru` с низкой уверенностью | Модель плохо различает русско-казахскую смесь | Маршрутизация комбинирует язык+вероятность с проверкой по казахским буквам/словам (`scoring/language.py`); порог `ATAMURAOKK_SCORE_LANG_CONFIDENCE` |
| LLM возвращает «болтливый» JSON / markdown-фенсы | Модель добавляет текст вокруг JSON | `parse_llm_json` толерантен: снимает фенсы и вытягивает первый `{...}`; невалидный JSON → ретрай (`MalformedOutputError`) |
| LLM ставит балл вне диапазона / пропускает критерий | Галлюцинации модели | `assemble_score` клампит в `[0, max_score]`; пропуски в `meta["missing_criteria"]`, при ≥3 → `needs_human_review` |
| Провайдер недоступен | rate-limit / сеть / 5xx | `LanguageRoutedScorer` падает на второй провайдер, ставит `needs_human_review=True`, пишет `meta["fallback_from"]` |
| `UnicodeEncodeError` в консоли Windows | Кодировка консоли (Cyrillic) | Запускать с `PYTHONUTF8=1` |

---

## 8. Что осталось доделать

Готово (код, ruff + mypy --strict чисто, юнит-тесты на моках): подсистема transcription (интерфейс, faster-whisper для спайка, **GroqWhisperTranscriber** для прода, стерео-сплит через `pan`-фильтр), подсистема scoring (router, Groq, Yandex, 2 рубрики, prompt, парсинг, `ScoringService`), контракт БД (7 таблиц + DAO + миграция `f1a2b3c4d5e6`), калибровочный harness, **сквозной пайплайн воркеров** (`AtamuraOKK/workers/`: ingest → download → transcribe → score на APScheduler), Bitrix-клиент с backoff, сервис `worker` в `docker-compose.yml`.

Осталось — упирается в секреты/окружение (код готов):

1. **Ключи провайдеров** `ATAMURAOKK_GROQ_API_KEY`, `ATAMURAOKK_YANDEX_API_KEY` (+ `_FOLDER_ID`) — без них воркеры transcribe/score не запустятся.
2. **Scopes вебхука** `disk` + `user` — для скачивания ~63% записей и маппинга менеджеров (иначе работает только fallback `data/tm_managers.json`).
3. **Реальный прогон + DB-тесты** — поднять Postgres, `alembic upgrade head`, прогнать пайплайн на дне звонков одного отдела; DB-интеграционные тесты выполняются при доступном Postgres.
4. **CLI калибровки** — тонкая обёртка вокруг `compare`, чтобы прогнать гейт одной командой и сохранить отчёт (логика `compare` готова и протестирована).
5. **Пройти калибровочный гейт** на реальных данных (нужны ключи + транскрипты размеченных встреч) и зафиксировать вердикт PASS/REVISE/FAIL — go/no-go перед заменой людей.
6. **Phase 0 WER** — разблокировать (scopes, окружение Whisper+ffmpeg, ручные эталоны), заполнить `docs/transcription-eval.md`, выбрать `ATAMURAOKK_KAZAKH_TRANSCRIBER`.

Статус деплоя на момент передачи: код пайплайна **завершён и собран** (`docker compose up -d db api worker` после `migrator`); для боевого запуска нужны только секреты (.env: ключи + вебхук со scopes crm/telephony/disk/user) и поднятый Postgres.

---

*Документ составлен 3 июня 2026 г. · Atamura Group · Версия 1.0*
