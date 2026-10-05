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
ограниченные повторы и shutdown. Реальное подключение ещё не проверено.

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
- AccessControlEvent, RoomVerifyEvent, PerceptionEvent, UnauthorizedMemberEvent, UnknownEvent и
  неклассифицированные ошибки останавливают listener с operator_required.
  Это консервативная политика: даже сигнал проверки, относящийся к другому
  зрителю, может потребовать ручного разбора.
- HTTP 403/429, ошибки подписи и ограничения доступа не ретраятся автоматически.
  HTTP 500/502/503/504 и ошибки транспорта допускают повтор подключения.
- Оператор разбирает причину вручную и самостоятельно заново запускает программу.
  CLI возвращает код 2 для operator_required.
- Ctrl+C отменяет принадлежащие приложению задачи и освобождает соединения.

### Manual real LIVE smoke test

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
| Network error / ConnectError / TimeoutError | Ограниченный exponential backoff; проверьте интернет. HTTP/WS 500/502/503/504 допускают повтор, исчерпание лимита требует оператора. Ошибки сервиса подписи консервативно требуют оператора. |
| NEED_OPERATOR / AmbiguousSDKState / FailedParseRoomIdError | Чтение остановлено. Возможны проверка, login request, неизвестный формат или приостановка платформой. Разберите причину вручную в TikTok; повторный запуск — только после этого. |

Для диагностики используйте консоль и `Get-Content '.\logs\app.jsonl' -Tail 30`.
Не включайте raw/debug-логи SDK: они могут содержать секреты. Код HTTP и тип ошибки
не требуют публикации URL запроса, cookies или полного exception payload.

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
| LIVE_MODE | fake | real или fake |
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
DIAGNOSTIC=true допускается только с LIVE_MODE=real.
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
Get-Content './logs/app.jsonl' -Tail 30
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
  Локальный ReadOnlyTikTokClient получает room ID один раз и передаёт его в
  `start(room_id=..., fetch_live_check=False)` после отдельной проверки LIVE.
  HTTP response hook отклоняет ошибки и redirects до разбора ответа. Сбой
  неизвестного формата останавливает приложение, даже если SDK мог бы продолжить.
- Обёртка SDK для live-check ловит даже отмену. Адаптер вызывает её underlying
  route, сохраняя Ctrl+C и таймаут; никакого периодического мониторинга нет.
- SDK может пропускать повреждённые события и выводить raw payload. Адаптер
  останавливается на неразобранном событии; SDK-логи подавляются в рамках сессии.
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
malformed events, переполнение очереди, backoff, остановка на security/unknown,
завершение и отмена, изоляция чужих asyncio-задач, callback, JSONL/ротация,
FastAPI lifespan/health и архитектурная граница импортов. Тесты этапа 2 проверяют
отбор комментариев, языки/переводы, YAML, grounding, финансовые запреты, ограничения
длины, поддельные/ошибочные ответы provider, timeout/отмену, OpenAI-заглушку,
сквозной fake pipeline и UTF-8 в отдельном запуске CLI.
Stage 3A дополнительно проверяет диагностический CLI, реальный SDK emitter →
Comment → CommentProcessor → ReplySuggestion, последовательности состояний,
лимит повторов, HTTP/WS security errors, отсутствие fallback, ControlEvent,
повреждённые protobuf, heartbeat/signing cleanup и настоящий SIGINT в тестовом
процессе с подменённым подключением.

Тесты блокируют внешние сокеты и DNS; loopback разрешён для внутренней работы asyncio
на Windows. Они не проверяют доступность реального LIVE или сервиса подписи.

Проверено на Python 3.12.0: **144 tests passed**; fake CLI выводит все три сообщения
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
│   │   │   ├── listener.py
│   │   │   ├── models.py
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
│       ├── test_listener.py
│       ├── test_logging.py
│       └── test_service.py
├── config/
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
