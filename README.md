# tiktok-live-automation

## Назначение и границы проекта

Локальная система сопровождения **собственных TikTok LIVE**. Во время эфира
на ПК работает отдельная торговая программа для Pocket Option: она сама
анализирует рынок, открывает сделки и голосом комментирует свои действия.
Эта программа — отдельная система, она не входит в репозиторий и не управляется
кодом этого проекта. Название продукта и платформа задаются конфигурацией,
а не константами в AI-классах. Знания о продукте хранятся отдельно в YAML.

Основная аудитория — англоязычная. Оператор может не знать английского и трейдинга.
Система должна отбирать сообщения, требующие ответа, и показывать оригинал,
перевод на русский, короткий естественный ответ на языке зрителя и его перевод
на русский. Факты можно брать только из утверждённой knowledge base. Запрещены
выдуманные сведения о продукте/платформе, неподтверждённые метрики доходности, гарантии прибыли,
заработка, win rate и любые финансовые обещания.

Целевое разделение: TikTokListener (сейчас LiveListener) → LiveService →
CommentProcessor → AI Reply Engine → ReplySuggestion → оператор / будущая доставка.
AI Reply Engine не зависит от TikTokLive и конкретного LLM provider.

Отдельный будущий контур: LiveSupervisor → абстракция AndroidController →
TikTok Android на нашем устройстве. AndroidController не зависит от AI.
Контроль состояния и периодические перезапуски эфира относятся к будущим этапам.
CAPTCHA, security challenge, login request, неизвестный UI и неоднозначные
состояния требуют **NEED_OPERATOR**, без самостоятельного обхода защиты.
Текущее состояние listener operator_required соответствует этому требованию.

Этап 1 готов: конфигурация, модели, чтение LIVE, консоль, JSONL, fake listener,
unit tests. Этап 2 реализован: CommentProcessor, ReplySuggestion,
AI-интерфейс, FakeAIReplyEngine, переводы для оператора и safety/business rules.
OpenAI API key не требуется. Отправка комментариев, Android automation,
перезапуски и frontend не входят в этот этап.

Stage 3A подготавливает ручную проверку существующего listener на собственном
реальном LIVE: диагностический вывод, проверка контракта TikTokLive 7.0.1,
ограниченные повторы и shutdown. Два ручных smoke test остановились на
FailedParseRoomIdError. Во втором HTML lookup получил HTTP 200, text/html,
1155 байт без ожидаемого SIGI_STATE.
В следующих операторских запусках старый детектор заблокировал ответы размером
1155 и 244370 байт; второй содержал валидный SIGI_STATE и поле roomId.
Точное совпадение старого regex не сохранялось, поэтому конкретное слово,
сработавшее на этих ответах, ретроспективно установить нельзя.

Последний предоставленный оператором smoke test 2026-10-05 подтвердил guarded API
room lookup после HTML parse failure, получение валидного roomId, live-check,
SDK start и реальный CONNECTED. Затем два UnknownEvent ошибочно вызвали
NEED_OPERATOR → STOPPED. Обработка unmapped-сообщений исправлена, но реальные
CommentEvent пока не проверены: следующий smoke test выполняет оператор вручную.

## Второй этап: предложения ответа

CommentProcessor использует простые признаки вопроса, просьбы о помощи или проблемы.
Это эвристика, а не универсальный классификатор намерений. Например, вопрос без
знака вопроса и сообщение It is not working тоже обрабатываются. Приветствия
и благодарности из fake-сценариев переводятся, но не получают ответа.

AIReplyEngine — Protocol с методом async generate(ReplyRequest) -> ReplyDraft.
Движок получает текст, настройки и knowledge base, без объектов TikTokLive.
CommentProcessor валидирует результат, применяет ReplySafetyPolicy и создаёт
ReplySuggestion. Ошибки, timeout и некорректные результаты provider не мешают
обрабатывать следующие комментарии. Отмена приложения не подавляется.

FakeAIReplyEngine — **детерминированная демонстрация**, не настоящий LLM.
Распознавание языка и переводы основаны на конечном наборе эквивалентных фраз
в config/fake_ai_examples.yaml. Поддержаны демонстрационные сценарии en/ru/es.
На неизвестный текст он возвращает detected_language=und, пустые переводы и
needs_operator: он не выдаёт исходный текст за перевод и не угадывает факты.
stream.language описывает аудиторию и не переопределяет язык комментария.

Статусы ReplySuggestion:

- ready: подготовлен ответ, should_reply=true, **requires_approval=true**;
- skipped: приветствие/благодарность, should_reply=false;
- needs_operator: неизвестный текст, отсутствующий перевод/факт, ошибка provider
  или заблокированный ответ. Текст для отправки отсутствует.

needs_operator относится к отдельному комментарию и не останавливает чтение LIVE.
Будущий NEED_OPERATOR у supervisor относится к управлению устройством и эфиром.
Отправка и интерфейс подтверждения пока отсутствуют: результат виден в консоли/JSONL.

OpenAIReplyEngine — только заглушка будущего provider. Она не импортирует SDK,
не обращается к API, не читает ключи и честно возвращает ошибку через процессор.
Если выбрать ai.provider=openai, комментарии потребуют оператора. Для работы
этого этапа оставьте ai.provider=fake. Другой provider реализует тот же Protocol.

### Настройки продукта и знания

config/project.yaml содержит настройки, не связанные с секретами:

```yaml
stream:
  language: en
product:
  name: "Trading Bot"
  platform: "Pocket Option"
ai:
  provider: fake
  max_reply_sentences: 2
  max_reply_chars: 240
  max_comment_chars: 1000
  operator_language: ru
  timeout_seconds: 10
```

Название платформы отсутствует в AI Python-классах. ProductConfig передаётся
движку через ReplyRequest. Настройка платформы не является доказательством
её регулирования, доходности или других свойств.

config/bot_knowledge.yaml хранит факты отдельно. Вместо строк без идентификаторов
используется небольшое расширение формата для проверяемых ссылок и переводов:

```yaml
facts:
  - id: automatic_analysis
    text: "The software analyzes the market automatically."
    translations:
      ru: "Программа автоматически анализирует рынок."
      es: "El programa analiza el mercado automáticamente."
```

В этом же файле находятся forbidden_claims и нейтральные safe_responses.
Обязательные запреты нельзя отключить удалением из YAML: загрузчик сообщит ошибку.
При изменении фактов оператор должен проверить все их переводы. YAML загружается
через safe_load и Pydantic; отсутствие файлов/ошибки схемы не заменяются молча
встроенными знаниями. Секретов в этих файлах нет.

### Правила и их практические ограничения

Политика проверяет **оба** текста: ответ зрителю и перевод ответа оператору.
Проверяются язык, наличие переводов, 1–2 предложения, максимальная длина,
ссылки на существующие факты и известные формулировки финансовых обещаний.
Числовые метрики и сведения о платформе без утверждённого факта не проходят.

Сейчас фактический ответ должен точно состоять из выбранных утверждённых фраз.
Одних fact_ids недостаточно: произвольный текст с правильной ссылкой блокируется.
Для вопросов о гарантиях предусмотрен ответ об отсутствии гарантии и наличии риска;
для неизвестных метрик/свойств — честный ответ об отсутствии подтверждённых сведений.
Комментарий зрителя не может добавлять знания или изменять правила.

Это ограниченная защита этапа 2, **не универсальная семантическая проверка**.
Она предполагает доверенную, проверенную человеком базу знаний и переводов.
Проверка известных запрещённых фраз не распознаёт все возможные финансовые обещания.
Свободные перефразирования будущего LLM пока блокируются: перед их разрешением
нужен отдельный механизм проверки смысла и фактической обоснованности.

### Пример внутреннего результата

```json
{
  "username": "john92",
  "original_text": "Is this bot trading automatically?",
  "detected_language": "en",
  "operator_translation": "Этот бот торгует автоматически?",
  "should_reply": true,
  "suggested_reply": "The software analyzes the market automatically. The software opens trades automatically.",
  "reply_translation": "Программа автоматически анализирует рынок. Программа автоматически открывает сделки.",
  "reason": "Вопрос о программе; ответ основан на утверждённых фактах.",
  "status": "ready",
  "fact_ids": ["automatic_analysis", "automatic_trades"],
  "provider": "fake",
  "requires_approval": true
}
```

У фактической модели дополнительно есть timestamp в UTC.

## Windows: установка

Нужны Python 3.12+ и PowerShell. При установке Python включите Python Launcher.
Откройте PowerShell в корне репозитория и выполните команды ниже.

```powershell
py -3.12 --version
py -3.12 -m venv .venv
& './.venv/Scripts/python.exe' -m pip install -e '.[dev]'
Copy-Item .env.example .env
```

Активация venv не нужна: все команды явно используют его Python.
Менять ExecutionPolicy не требуется. Установка требует доступа к PyPI.
Не выполняйте Copy-Item поверх уже настроенного .env.

## Fake mode

```powershell
& './.venv/Scripts/python.exe' -m app.main --mode fake
```

Или после установки проекта:

```powershell
& './.venv/Scripts/tiktok-live.exe' --mode fake
```

Будут выведены три комментария и карточки для оператора, затем процесс завершится
с кодом 0. Исходные комментарии:

```text
[15:42:01] LIVE connected: @demo [fake]

[15:42:02] john92:
How does the bot work?

[15:42:03] alice:
Is it automatic?

[15:42:04] mike:
Hello!
```

Для john92 и alice готовятся ответы о самостоятельном анализе/открытии сделок,
с русскими переводами. Для mike показывается «Привет!» и «Ответ не требуется».

Перед выходом LiveService дожидается обработки всех принятых комментариев.

Для непрерывной генерации укажите в .env:

```dotenv
LIVE_MODE=fake
FAKE_REPEAT=true
FAKE_INTERVAL_SECONDS=1
```

Остановка — Ctrl+C. В fake mode TikTokLive не импортируется и сетевые
подключения к TikTok не создаются. Поле source остаётся tiktok: fake имитирует
тот же входной контракт. Признак режима доступен в статусе и логах состояния.

## Offline replay testing

Stage 3A Offline Replay Harness проверяет integration layer без настоящего LIVE.
Запускайте из корня проекта в PowerShell:

```powershell
& ".\.venv\Scripts\python.exe" -m app.main --mode replay --scenario live_with_comments
```

Доступные сценарии:

| Scenario | Ожидаемое поведение |
| --- | --- |
| html_room_success | Штатный HTML parser → live-check → CONNECTED → ENDED |
| html_fail_api_success | FailedParseRoomIdError → SAFE_API_FALLBACK → API → CONNECTED |
| html_challenge | HARD challenge → NEED_OPERATOR; API не вызывается |
| api_missing_room | HTML parse failure → API без roomId → NEED_OPERATOR |
| live_with_comments | Hello → skipped; вопрос о программе → ready, requires_approval=true |
| unknown_events_then_comments | Два UnknownEvent игнорируются → Hello: skipped → вопрос: ready → ENDED → STOPPED |
| live_end | CONNECTED → ENDED → STOPPED |
| temporary_disconnect | Transport failure → RECONNECTING → CONNECTING → CONNECTED |
| rate_limit | HTTP 429 → NEED_OPERATOR без повторов/fallback |
| security_event | CONNECTED → SDK AccessControlEvent → NEED_OPERATOR |

Все сценарии конечные. Успешные соединения завершаются синтетическим LiveEndEvent.
`--scenario` обязателен для replay и запрещён для других режимов. Эквивалентные
переменные настроек — LIVE_MODE=replay и REPLAY_SCENARIO=<name>. Handle всегда
`@replay`, provider всегда `fake`, даже если в project.yaml выбран другой provider.
YAML не меняется. `--diagnostic` для replay не требуется: безопасная telemetry и
операторский формат вывода включаются автоматически.

Общая цепочка: установленные SDK routes/parsers → RoomConnection (security guards,
guarded fallback, live-check) → LiveListener → Comment → LiveService →
CommentProcessor → FakeAIReplyEngine → ReplySuggestion. State machine, reconnect
и cleanup не копируются. Replay использует настоящие классы SDK-событий на входе
существующих callbacks; в AI передаётся только внутренняя модель Comment.
Временные задержки backoff сокращены до 1–4 мс, алгоритм и конечный бюджет сохраняются.

`config/replay_scenarios/*.json` — минимальные синтетические HTML/JSON и события,
с вымышленным положительным ID 101. В них нет сохранённых TikTok responses, cookies,
реальных headers, URL или аккаунтов. Только синтетический Content-Type создаётся
в памяти. ReplayWeb подставляется под штатные SDK routes и возвращает Response
в памяти: HTTP-клиенты, signer и WebSocket вообще не создаются. Неожиданный путь,
порядок запросов или лишняя сессия приводят к FAIL, а не сетевому fallback.
Тесты запрещают sockets/DNS/HTTP-клиенты внутри уже работающего event loop;
служебный socketpair самого asyncio на Windows создаётся до этого запрета.

В конце выводится `REPLAY RESULT: PASS` или `FAIL`, имя сценария, фактический порядок
состояний, число обработанных комментариев/предложений и конечное состояние.
PASS требует совпадения состояний, запросов, статусов предложений, provider/approval,
числа сессий, потребления fixtures и завершения принадлежащих replay задач.
Сценарий с ожидаемым NEED_OPERATOR также возвращает PASS и код 0.
Несовпадение ожиданий — код 1; ошибка конфигурации — код 2. В real-режиме
NEED_OPERATOR по-прежнему возвращает код 2. При отмене незавершённый replay
не печатает PASS. Полный отчёт также записывается как `replay_result` в JSONL.

Запуск всех сценариев:

```powershell
$replayScenarios = @(
    "html_room_success", "html_fail_api_success", "html_challenge", "api_missing_room",
    "live_with_comments", "live_end", "temporary_disconnect", "rate_limit", "security_event",
    "unknown_events_then_comments"
)
foreach ($replayScenario in $replayScenarios) {
    & ".\.venv\Scripts\python.exe" -m app.main --mode replay --scenario $replayScenario
    if ($LASTEXITCODE -ne 0) { throw "Replay failed: $replayScenario" }
}
```

Replay проверяет архитектуру и поведение integration layer, **не доказывает текущую
совместимость с реальным TikTok**. Он не проверяет настоящие HTTP-ответы, подпись,
WebSocket handshake или сетевую доставку protobuf. Финальный manual real smoke test
на собственном LIVE всё равно обязателен. Ни один предложенный ответ не отправляется.

## Собственный реальный LIVE

Запустите эфир вручную. В .env укажите свой handle:

```dotenv
LIVE_MODE=real
TIKTOK_USERNAME=your_account
```

```powershell
& './.venv/Scripts/python.exe' -m app.main
```

Либо используйте CLI-переопределения:

```powershell
& './.venv/Scripts/python.exe' -m app.main --mode real --username '@your_account'
```

Логин, пароль, cookies и sessionid приложению не передаются.
Handle задаётся без URL; начальный @ разрешён.
Приложение не подтверждает владение аккаунтом: используйте свой.

Поведение listener:

- ConnectEvent меняет состояние на connected.
- CommentEvent преобразуется в Comment; повреждённое событие пропускается с логом.
- Временные сетевые ошибки и offline вызывают повтор подключения с задержкой
  2, 4, 8, …, максимум 60 секунд. Задержки настраиваются. По умолчанию допускается
  пять повторов за запуск после исходной попытки; затем NEED_OPERATOR → STOPPED.
  Счётчик и задержка не сбрасываются после короткого подключения, чтобы разрывы
  соединения не создавали бесконечный цикл запросов.
- При обычном разрыве соединения чтение подключается заново. Возможны
  повторные комментарии и пропуски во время разрыва: replay/deduplication не реализованы.
- LiveEndEvent завершает чтение: ENDED → очистка ресурсов → STOPPED.
  Эфир не перезапускается.
- AccessControlEvent, RoomVerifyEvent, PerceptionEvent, UnauthorizedMemberEvent и
  неклассифицированные ошибки останавливают listener с operator_required.
  Это консервативная политика: даже сигнал проверки, относящийся к другому
  зрителю, может потребовать ручного разбора.
- UnknownEvent означает неизвестный библиотеке тип webcast message. Listener пишет
  UNKNOWN_EVENT_IGNORED и продолжает работу; Comment и вызов AI не создаются.
  STREAM_SUSPENDED и неизвестный ControlAction по-прежнему требуют NEED_OPERATOR.
- HTTP 403/429, ошибки подписи и ограничения доступа не ретраятся автоматически.
  На этапе room lookup HTTP/transport errors требуют оператора без повторов.
  В остальных фазах HTTP 500/502/503/504 и ошибки транспорта допускают повтор подключения.
- Оператор разбирает причину вручную и самостоятельно заново запускает программу.
  CLI возвращает код 2 для operator_required.
- Ctrl+C отменяет принадлежащие приложению задачи и освобождает соединения.

### Manual real LIVE smoke test

После исправления UnknownEvent следующий ручной запуск для собственного @antilame:

```powershell
& ".\.venv\Scripts\python.exe" -m app.main --mode real --username "@antilame" --diagnostic
```

Проверьте, что UNKNOWN_EVENT_IGNORED не меняет CONNECTED, а следующий реальный
вопрос создаёт ReplySuggestion с provider=fake и requires_approval=true.

Выполняйте команды в Windows PowerShell. Нужна установленная локальная `.venv`
из раздела установки. `ACCOUNT` замените на handle своего аккаунта вручную.
Диагностика всегда использует FakeAIReplyEngine, даже если в project.yaml выбран
другой provider; YAML при этом не изменяется. Обычный режим сохраняет выбор из YAML.
Ни одно предложение ответа не отправляется в TikTok.

1. Откройте PowerShell в корне репозитория.
2. Запустите собственный TikTok LIVE вручную.
3. С другого TikTok-аккаунта откройте этот LIVE.
4. Выполните:

   ```powershell
   & ".\.venv\Scripts\python.exe" -m app.main --mode real --username "@ACCOUNT" --diagnostic
   ```

5. Убедитесь, что после STARTING и CONNECTING появилось CONNECTED.
6. С другого аккаунта напишите `Hello`.
7. Проверьте появление комментария в консоли. Приветствие получает `skipped`.
8. Напишите `Is this bot trading automatically?`.
9. Проверьте ReplySuggestion: `status: ready`, перевод `Этот бот торгует автоматически?`,
   ответ на английском, перевод ответа на русский, `provider: fake`,
   `requires_approval: true`. Это результат существующего CommentProcessor.
10. Завершите LIVE вручную.
11. Проверьте `ENDED` с причиной `LiveEndEvent`.
12. Приложение завершится автоматически после очистки и обработки принятых
    комментариев. Если оно ещё работает, нажмите Ctrl+C. Для отдельной проверки
    Ctrl+C повторите запуск во время собственного LIVE и прервите приложение.
13. Проверьте `STOPPED`, возврат приглашения PowerShell и отсутствие traceback.
    `$LASTEXITCODE` должен быть `0`; при NEED_OPERATOR приложение возвращает `2`.

Пример последовательности:

```text
[16:30:01] STARTING @account
[16:30:02] CONNECTING @account
[16:30:05] CONNECTED @account
[16:31:14] @john92:
Is this bot trading automatically?
[16:31:14] ReplySuggestion @john92:
  status: ready
  translation: Этот бот торгует автоматически?
  suggested_reply: The software analyzes the market automatically. The software opens trades automatically.
  reply_translation: Программа автоматически анализирует рынок. Программа автоматически открывает сделки.
  provider: fake
  requires_approval: true
[16:35:01] ENDED @account (LiveEndEvent)
[16:35:01] STOPPED @account (LiveEndEvent)
```

Временный разрыв: CONNECTED → RECONNECTING → CONNECTING → CONNECTED.
`reconnect_scheduled` показывает `attempt`, `delay_seconds`, `error_type`, без текста
исключения. Ошибка/ограничение: NEED_OPERATOR → очистка → STOPPED без повторов.
В JSONL и `/health` состояния записываются в нижнем регистре; `operator_required`
сохранён для совместимости и отображается как NEED_OPERATOR в диагностике.
`terminal_state` сохраняет `ended` или `operator_required` после STOPPED.
Статус `needs_operator` отдельной карточки не останавливает listener: неизвестные
FakeAIReplyEngine фразы требуют ручного перевода и ответа.

### Troubleshooting

| Симптом | Действие оператора |
| --- | --- |
| Account offline / UserOfflineError | Убедитесь вручную, что собственный LIVE действительно идёт. Приложение повторяет подключение с backoff в пределах лимита; эфир не запускает. |
| Username not found / UserNotFoundError | Проверьте handle без URL и доступность LIVE в приложении TikTok. Ошибка может означать ограничение, поэтому автоматического fallback нет. |
| HTTP 403 | NEED_OPERATOR: доступ запрещён. Проверьте доступность вручную; не добавляйте cookies/sessionid и не обходите ограничение. |
| HTTP 429 | NEED_OPERATOR: ограничение частоты. Не запускайте частые повторы; дождитесь снятия ограничения и решите вручную, когда повторить smoke test. |
| Signature error / SignAPIError | NEED_OPERATOR: не удалось получить подпись. Проверьте доступность сервиса и совместимость установленного SDK вручную. Ключи, fingerprint и sessionid автоматически не подбираются. |
| Network error / ConnectError / TimeoutError | Ошибка HTTP/транспорта или connect deadline во время room lookup требует оператора без fallback и повторов. В остальных фазах действует bounded backoff для transient errors; исчерпание лимита требует оператора. Ошибки сервиса подписи консервативно требуют оператора. |
| NEED_OPERATOR / AmbiguousSDKState / FailedParseRoomIdError | Чтение остановлено. Возможны проверка, login request, неизвестный формат или приостановка платформой. Разберите причину вручную в TikTok; повторный запуск — только после этого. |

Для диагностики используйте консоль и `Get-Content '.\logs\app.jsonl' -Encoding UTF8 -Tail 30`.

### Диагностика unmapped webcast messages

В установленном TikTokLive 7.0.1 `_parse_webcast_response()` выдаёт ConnectEvent,
если outer response имеет `is_first`, затем разбирает каждое внутреннее сообщение.
`_parse_webcast_response_message()` ищет `message.method` в `EVENT_MAPPINGS`;
если соответствия нет, возвращает WebsocketResponseEvent и UnknownEvent.
UnknownEvent хранит только внешний `ProtoMessageFetchResult` в `raw`, поэтому
конкретный породивший его inner message по самому событию определить нельзя.

Прежняя регистрация UnknownEvent в `on_security` останавливала LIVE сразу после
подключения. Теперь отдельный обработчик игнорирует его без изменения состояния,
очереди Comment и AI pipeline. Название метода, даже со словами security/login,
не является security-сигналом. Известные security events и структурные control
guards работают независимо и сохраняют NEED_OPERATOR.

В real `--diagnostic` и replay событие `UNKNOWN_EVENT_IGNORED` содержит:

- `unknown_method_candidates`: первые 16 уникальных unmapped-имён из outer response,
  после исключения методов из EVENT_MAPPINGS. Допустимы 1–96 ASCII-символов:
  первая латинская буква, далее буквы, цифры и `_`. Остальное заменяется на
  `invalid_or_redacted`; длинные значения не обрезаются до потенциального секрета.
- `payload_length`: сумма длин payload всех unmapped messages внешнего ответа,
  без декодирования содержимого. Это не размер одного точно определённого события.
- `unknown_event_count`: число проигнорированных UnknownEvent за жизнь listener,
  включая повторные подключения.
- `is_first_response`: фактический `raw.is_first` установленного SDK.

Несколько UnknownEvent из одного outer response повторяют кандидатов и суммарный
размер; их нельзя суммировать как уникальный сетевой трафик. Кандидаты могут быть
усечены до 16, размер учитывает все unmapped messages. В обычном real-режиме
логируется только счётчик. Payload bytes, protobuf body, raw event/response,
cursor, ws_params, URL, cookies, headers, tokens, roomId и данные пользователей
в эти записи не включаются.
Не включайте raw/debug-логи SDK: они могут содержать секреты. Код HTTP и тип ошибки
не требуют публикации URL запроса, cookies или полного exception payload.

### Safe room ID diagnostics

Только в real mode с `--diagnostic` появляются `room_lookup_started`,
`room_lookup_result` и `SAFE_API_FALLBACK`. Проверки безопасности и условный
fallback действуют также без diagnostic; сам вывод не создаёт новых запросов.
Команда из PowerShell, уже открытого в корне репозитория:

```powershell
& ".\.venv\Scripts\python.exe" -m app.main --mode real --username "@ACCOUNT" --diagnostic
```

`room_lookup_strategy` — `html` или `api`; `request_stage` показывает последний
достигнутый этап: `request_started`, `http_response`, `sdk_parse`, `lookup_complete`.
Логируются `http_status`, `content_type`, `response_category`, `response_length`
(число байт декодированного HTTP body), `redirect`, `blocked_or_challenge`,
`challenge_reason_codes`, `parsed_room_id` и
`exception_type`. До получения ответа неизвестные поля равны `null`.
Content-Type нормализуется по белому списку MIME-типов; параметры заголовка
отбрасываются, незнакомое значение заменяется на `other`, отсутствие — `missing`.

Категории: `html`, `json`, `redirect`, `blocked_or_challenge`, `empty`, `unexpected`.
`blocked_or_challenge` теперь означает только HARD сигнал. Раньше regex по всему
body превращал даже обычное слово login или ссылку на security.js в блокировку,
до запуска SDK parser и независимо от LIVE-структуры. Это и создавало возможность
false positive. Raw HTML не сохранялся; конкретный маркер прежнего ответа неизвестен.

Классификация основана на структуре, а не размере HTML:

- HARD: HTTP 401/403/429; явные CAPTCHA-контейнеры, verification/challenge формы
  и заголовки; активные флаги `captcha_required=true`, `verification_required=true`
  и аналогичные явные состояния. Они блокируют даже страницу с валидным LIVE state.
  SDK AccessControl/security events по-прежнему требуют оператора.
- Login wall (заголовок входа, форма с action `/login` или явный wall-контейнер)
  и общий interstitial-контейнер требуют оператора при отсутствии валидного LIVE
  state. Для этого SIGI_STATE должен содержать положительный roomId; наличие
  ключа со значением null не считается валидным LIVE state.
- SOFT: generic login/auth UI, слова captcha/security/verification и ссылки на
  JS-файлы. Они сами по себе не блокируют SDK parser и guarded fallback.
  Отключённые флаги и словари переводов не считаются активным challenge-state.

`challenge_reason_codes` содержит только фиксированные идентификаторы из
`live/security.py`, без совпавшего текста: `http_401`, `http_403`, `http_429`,
`captcha_marker`, `verification_marker`, `security_challenge_marker`,
`login_interstitial_marker`, `challenge_interstitial_marker`,
`access_restriction_marker`, `rate_limit_marker`; soft-коды —
`generic_login_text`, `generic_captcha_text`, `generic_verification_text`,
`generic_security_text`, `generic_rate_limit_text`.
В одном ответе могут быть и HARD, и SOFT коды; любой HARD побеждает.
Это ограниченный структурный детектор, а не универсальная проверка безопасности.

HARD приводит к NEED_OPERATOR без fallback. HTTP-ошибки сохраняют свой тип;
структурные блокировки дают `AmbiguousSDKState`. При отсутствии HARD HTML
передаётся штатному SDK parser: валидный roomId → live-check; обычная ошибка
извлечения → проверка условий guarded fallback. Отдельного порога размера нет.

HTML-поля:

- `expected_embedded_json_marker_found`: найден **точный** шаблон `SIGI_STATE`
  установленного SDK, включая порядок атрибутов и формат тега;
- `embedded_json_valid`: JSON внутри этого тега разобран;
- `expected_room_id_marker_found`: в нём есть путь
  `LiveRoom.liveRoomUserInfo.user.roomId`;
- `parsed_room_id`: SDK-метод вернул положительный числовой ID. Сам ID не выводится.

API-поля: `valid_json`, `data_present`, `user_present`, `room_id_field_present`
для пути `data.user.roomId`, а также `room_id_value_valid` и `parsed_room_id`.
`data_present` и `user_present` означают наличие объектов ожидаемого типа.
ID должен быть положительным целым или строкой ASCII-цифр; null, bool, дробь,
ноль, отрицательные значения и строки с пробелами отклоняются. Сам ID не выводится.

При исключении из room parser также выводятся `sdk_error_site`
(`html.parse_room_id` или `api.parse_room_id`) и `sdk_error_line` — номер строки
именно установленного пакета. Текст исключения, traceback, локальные переменные,
body, headers, cookies, query parameters и URL не выводятся и не сохраняются
диагностикой. После успешного lookup отдельный `connection_stage` показывает
переход к `live_check`, затем к `sdk_start`.

### Guarded room ID strategy

```text
HTML
  ├─ success → live_check
  └─ normal 200 HTML parse failure
        ↓
     SAFE_API_FALLBACK (один запрос)
        ├─ roomId found → live_check → sdk_start → CONNECTED
        └─ failure → NEED_OPERATOR → STOPPED
```

Fallback разрешён только при `FailedParseRoomIdError` именно из
`FetchRoomIdLiveHTMLRoute.parse_room_id()` и одновременно: HTTP 200,
`response_category=html`, `redirect=false`, `blocked_or_challenge=false`,
отсутствие HARD security-сигналов и сигнала остановки
listener. Без наблюдённого HTTP-ответа переход к API запрещён.

Вызывается установленный `client.web.fetch_room_id_from_api(username)`
(TikTokLive 7.0.1 `FetchRoomIdAPIRoute.__call__`): `/api-live/user/room/`,
путь `data.user.roomId`. Пригоден только HTTP 200 JSON с корректным положительным ID.
Non-JSON, отсутствующие data/user/roomId, null, некорректный ID, security response,
redirect и HTTP/transport errors завершают lookup без других способов и повторов.
HTTP 401/403/429, неизвестные категории и security events никогда не разрешают fallback.

Это частичное восстановление штатного поведения TikTokLive 7.0.1 с дополнительными
security guards. Оригинальный SDK пробует API после любого HTML Exception,
кроме UserOfflineError; такой безусловный путь остаётся отключённым.
После положительного ID адаптер выполняет `/webcast/room/check_alive/`, затем
`start(room_id=..., fetch_live_check=False)`. Существующий pipeline комментариев
и обязательное одобрение предложений оператором сохранены.

Второй операторский smoke test установил точное место прежнего сбоя:
`html.parse_room_id`, строка 61 установленного SDK — отсутствует точный тег
SIGI_STATE. Причина сокращённого HTML-ответа пока не установлена. Последний
операторский smoke test уже подтвердил API fallback и CONNECTED на реальном LIVE;
проверка реальных комментариев остаётся ручным действием оператора. Поле
`challenge_reason_codes` позволит различать конкретные правила классификатора,
не публикуя HTML или roomId.

На этом этапе видны только сигналы, которые отдаёт библиотека. Диалоги Android
и CAPTCHA на экране телефона не обнаруживаются: Android UI ещё не подключён.
Никакого обхода проверок нет. Отсутствие LIVE не доказывает, что требуется перезапуск.

## Конфигурация

Приоритет: аргументы CLI → переменные среды → .env → значения по умолчанию.
.env и относительный LOG_DIR разрешаются от текущей рабочей папки.
Другой env-файл: --env-file config/local.env (локальный файл, не коммитить).
Этот приоритет относится к Settings и путям конфигурации. Содержимое project.yaml
не переопределяется переменной AI_PROVIDER: меняйте ai.provider в YAML.
YAML-файлы также загружаются относительно текущей рабочей папки; запускайте из корня.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| LIVE_MODE | fake | real, fake или replay |
| REPLAY_SCENARIO | не задано | Обязательное имя fixture только для replay; CLI --scenario |
| TIKTOK_USERNAME | пусто | Обязателен только для real |
| DIAGNOSTIC | false | Диагностика real LIVE с FakeAIReplyEngine; CLI --diagnostic включает её |
| LOG_LEVEL | INFO | Уровень файла: DEBUG/INFO/WARNING/ERROR |
| LOG_DIR | logs | Каталог JSONL-логов |
| LOG_MAX_BYTES | 5242880 | Размер файла до ротации |
| LOG_BACKUP_COUNT | 3 | Количество резервных файлов |
| QUEUE_SIZE | 1000 | Максимум комментариев в очереди |
| RECONNECT_INITIAL_SECONDS | 2 | Начальная задержка повтора |
| RECONNECT_MAX_SECONDS | 60 | Верхняя граница задержки |
| RECONNECT_MAX_ATTEMPTS | 5 | Максимум повторов за запуск (1–100), затем NEED_OPERATOR |
| CONNECT_TIMEOUT_SECONDS | 30 | Таймаут до ConnectEvent, включая HTTP и WebSocket handshake |
| SHUTDOWN_TIMEOUT_SECONDS | 5 | Лимит каждого шага освобождения ресурсов |
| FAKE_INTERVAL_SECONDS | 1 | Пауза между fake-комментариями |
| FAKE_REPEAT | false | Повторять три сообщения |
| PROJECT_CONFIG_PATH | config/project.yaml | Настройки продукта, потока и AI |
| KNOWLEDGE_PATH | config/bot_knowledge.yaml | Утверждённые факты и запреты |
| FAKE_EXAMPLES_PATH | config/fake_ai_examples.yaml | Демонстрационные фразы/переводы |

Числа, режим и username валидируются при старте. Бесконечные и отрицательные
интервалы не допускаются. При FAKE_REPEAT=true интервал должен быть больше нуля.
Для CLI передаваемые аргументы --mode/--username/--diagnostic имеют самый высокий приоритет.
DIAGNOSTIC=true допускается с LIVE_MODE=real или replay; replay и без флага
выводит безопасную lookup telemetry и использует fake provider.
Консоль показывает INFO и выше независимо от уровня файла.

## Логи

logs/app.jsonl содержит отдельный JSON на строку: timestamp, level, event, logger
и поля события. Время в JSON — UTC, в консоли — локальное время компьютера.
Комментатор и текст сохраняются в логе; храните каталог локально.
События подключения, отключения, ошибок, переполнения очереди и остановки логируются.
reply_suggestion содержит полную карточку в поле suggestion; reply_blocked содержит
идентификаторы нарушенных проверок, ai_provider_failed — тип ошибки без секретов.
LOG_LEVEL=INFO сохраняет все эти действия. Более высокий уровень сокращает лог.

Ротация ограничивает объём. .env и содержимое logs исключены из Git.
Мы не сохраняем raw SDK events, cookies или тексты сетевых исключений.
Управляющие символы комментариев обезвреживаются при выводе в терминал.
Перенаправляемый stdout настроен на UTF-8 для русских переводов под Windows.
Поиск ошибок:

```powershell
Get-Content './logs/app.jsonl' -Encoding UTF8 -Tail 30
```

## Архитектура

```text
TikTokLive → LiveListener ──────┐
                              ├→ asyncio.Queue[Comment] → LiveService
            FakeLiveListener ─┘                              ↓
                                                     CommentProcessor
                                                            ↓
                                                      AIReplyEngine
                                                            ↓
                                                     ReplySafetyPolicy
                                                            ↓
                                                     ReplySuggestion
                                                            ↓
                                                     оператор / логи

Будущий независимый контур (ещё не реализован):
LiveSupervisor → AndroidController (абстракция) → TikTok Android
```

Все production-импорты TikTokLive находятся в live/listener.py.
LiveService принимает необязательный async callback с нашим Comment.
main.py подключает CommentProcessor.handle через этот callback. Сам LiveService
не знает ни provider, ни knowledge base. AI-модули не импортируют TikTok SDK.

Comment: username, опциональный строковый user_id, text, timezone-aware timestamp,
source="tiktok". Timestamp — время получения локальным приложением в UTC.
В 7.0.1 используем user.display_id, user.id и event.content. Если display_id
отсутствует, показывается nickname; user_id=0 трактуется как отсутствующий ID.

Очередь ограничена. При переполнении real listener отбрасывает новый комментарий
и записывает счётчик потерь. Это предотвращает накопление неограниченного числа
задач SDK. Fake listener ждёт свободного места. Ошибка callback логируется,
следующие комментарии продолжают обрабатываться. Доставка не является надёжной
очередью сообщений: при Ctrl+C оставшиеся элементы могут быть не обработаны.

FastAPI предоставляет только основу backend и GET /health:

```powershell
& './.venv/Scripts/python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

В другом PowerShell:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/health'
```

Lifespan запускает LiveService и останавливает его при завершении сервера.
health содержит listener_running и live.state; status=ok означает работоспособность
backend, а не наличие эфира. После конечного fake-прогона сервер остаётся доступен,
но listener_running=false. Для длительной разработки задайте FAKE_REPEAT=true.
Не используйте несколько workers: каждый создал бы собственное LIVE-подключение.

## Проверенный API

2026-10-05 проверены README из metadata установленного TikTokLive 7.0.1,
сигнатуры и исходники установленного клиента, события и v3-модели.
TikTokLive закреплён точной версией в pyproject.toml.

- [README версии 7.0.1](https://github.com/isaackogan/TikTokLive/blob/v7.0.1/README.md)
- [Исходники клиента 7.0.1](https://github.com/isaackogan/TikTokLive/blob/v7.0.1/TikTokLive/client/client.py)
- [События 7.0.1](https://github.com/isaackogan/TikTokLive/blob/v7.0.1/TikTokLive/events/proto_events.py)
- [PyPI](https://pypi.org/project/TikTokLive/7.0.1/)

Используются add_listener, await start() → Task, await disconnect() и
await client.web.close(). client.connect()/run()/close() не используются:
в 7.0.1 их путь cleanup вызывает run_until_complete и затрагивает чужие задачи.
Это проверяется тестом на установленном клиенте, без сети.

Review Stage 3A по metadata/README и исходникам установленной версии выявил:

- `start()` автоматически пробует API после ошибки HTML, включая security/unknown.
  Локальный ReadOnlyTikTokClient разрешает только описанный выше guarded fallback
  и передаёт проверенный ID в
  `start(room_id=..., fetch_live_check=False)` после отдельной проверки LIVE.
  HTTP response hook отклоняет ошибки и redirects до разбора ответа. Сбой
  неизвестного формата останавливает приложение, даже если SDK мог бы продолжить.
- Обёртка SDK для live-check ловит даже отмену. Адаптер вызывает её underlying
  route, сохраняя Ctrl+C и таймаут; никакого периодического мониторинга нет.
- SDK может пропускать повреждённые события и выводить raw payload. Адаптер
  останавливается на повреждённом известном событии; unmapped UnknownEvent
  безопасно игнорируется. SDK-логи подавляются в рамках сессии.
  Приложение пишет только тип исключения и свои структурированные события.
- SDK при ControlEvent сам создаёт disconnect task; адаптер оставляет завершение
  listener. STREAM_ENDED даёт LiveEndEvent, STREAM_SUSPENDED и неизвестные действия
  требуют оператора. Известные pause/unpause не запускают управление эфиром.
- Cleanup SDK не гарантирует освобождение heartbeat при исключении и не закрывает
  отдельный HTTP-пул подписи. Listener отменяет heartbeat именно своего клиента,
  закрывает оба HTTP-пула и свой socket task. Чужие задачи не перечисляются и
  не отменяются. Доступ к `_ws._ping_loop` и signer `_async_client` изолирован
  в listener и проверяется контрактными тестами закреплённой версии.

Версия SDK и зависимости не менялись. Архитектурная цепочка и AI-контракты сохранены.

Библиотека неофициальная, зависит от изменений TikTok и внешнего сервиса подписи.
Реальное соединение нельзя гарантировать по результатам unit tests.
Реальный LIVE в этой сессии не подключался. Лицензия библиотеки указана на PyPI
как Modified AGPL-3.0; исходный LICENSE доступен в её репозитории.

## Тесты

```powershell
& './.venv/Scripts/python.exe' -m pytest -q
& './.venv/Scripts/python.exe' -m pip check
```

Проверяются настройки и приоритеты, модели, реальный формат SDK-событий,
malformed events, переполнение очереди, backoff, остановка на security/unknown control,
завершение и отмена, изоляция чужих asyncio-задач, callback, JSONL/ротация,
FastAPI lifespan/health и архитектурная граница импортов. Тесты этапа 2 проверяют
отбор комментариев, языки/переводы, YAML, grounding, финансовые запреты, ограничения
длины, поддельные/ошибочные ответы provider, timeout/отмену, OpenAI-заглушку,
сквозной fake pipeline и UTF-8 в отдельном запуске CLI.
Stage 3A дополнительно проверяет диагностический CLI, реальный SDK emitter →
Comment → CommentProcessor → ReplySuggestion, последовательности состояний,
лимит повторов, HTTP/WS security errors, условный fallback и запреты на него, ControlEvent,
повреждённые protobuf, heartbeat/signing cleanup и настоящий SIGINT в тестовом
процессе с подменённым подключением.
Регрессии UnknownEvent проверяют реальный SDK parser и emitter, продолжение
после одного/двух событий, отсутствие Comment/AI для них, следующую карточку
ready/fake/approval, сохранение security/control guards и редактирование metadata.

Тесты блокируют внешние сокеты и DNS; loopback разрешён для внутренней работы asyncio
на Windows. Они не проверяют доступность реального LIVE или сервиса подписи.

Проверено на Python 3.12.0: **316 tests passed**; все 10 replay-сценариев отдельно
запущены через CLI и завершились с PASS; fake CLI выводит все три сообщения
и карточки предложений; editable installation проекта 0.2.0 выполнена;
pip check сообщает No broken requirements found. У зависимостей TikTokLive
есть два DeprecationWarning, связанных с websockets legacy API.
Проверенные версии: TikTokLive 7.0.1, TikTokLiveProto 0.2.2, FastAPI 0.142.2,
Pydantic Settings 2.15.0, pytest 9.1.1, pytest-asyncio 1.4.0, websockets 17.2.
Добавленная runtime-зависимость: PyYAML 6.0.3; сборка проверена с setuptools 84.0.0.

## Файлы

```text
tiktok-live-automation/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── logging_config.py
│   │   ├── live/
│   │   │   ├── __init__.py
│   │   │   ├── diagnostics.py
│   │   │   ├── listener.py
│   │   │   ├── models.py
│   │   │   ├── replay.py
│   │   │   ├── security.py
│   │   │   └── service.py
│   │   ├── ai/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py
│   │   │   ├── factory.py
│   │   │   ├── fake_engine.py
│   │   │   ├── knowledge.py
│   │   │   ├── models.py
│   │   │   ├── openai_provider.py
│   │   │   ├── processor.py
│   │   │   └── safety.py
│   │   ├── android/__init__.py
│   │   └── supervisor/__init__.py
│   └── tests/
│       ├── conftest.py
│       ├── test_ai.py
│       ├── test_config_models.py
│       ├── test_diagnostic.py
│       ├── test_room_lookup_diagnostics.py
│       ├── test_replay.py
│       ├── test_listener.py
│       ├── test_logging.py
│       └── test_service.py
├── config/
│   ├── replay_scenarios/
│   ├── .gitkeep
│   ├── project.yaml
│   ├── bot_knowledge.yaml
│   └── fake_ai_examples.yaml
├── logs/.gitkeep
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
└── AGENTS.md
```

Runtime-файлы logs/app.jsonl, __pycache__ и .pytest_cache в дерево не включены.
Переход к следующему этапу — только по отдельной команде пользователя.
