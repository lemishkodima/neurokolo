# Neurokolo — контекст проєкту

Оновлено: 2026-07-23.

Цей файл призначений для наступного чату/розробника. Перед будь-якими змінами потрібно
прочитати його разом із `README.md`, після чого перевірити актуальний стан коду і Docker.

## Призначення

Neurokolo — Telegram-бот закритого клубу з щомісячною підпискою через WayForPay.
Користувач переходить із сайту в Telegram, оплачує підписку на платіжній сторінці
WayForPay і після підтвердженого callback отримує персональні кнопки-посилання на
приватні канали та групи клубу.

Якщо користувач скасовує підписку, повторні списання припиняються, але доступ
залишається до кінця вже оплаченого періоду. Після завершення періоду та налаштованого
grace period worker автоматично видаляє користувача з усіх ресурсів тарифу.

## Технології та процеси

- Python 3.12+
- aiogram 3 — Telegram-бот
- FastAPI — checkout API і webhooks
- PostgreSQL + SQLAlchemy async
- Alembic — міграції
- WayForPay — перша оплата й рекурентні платежі
- Docker Compose — локальний і серверний запуск
- pytest, Ruff, mypy — перевірки

Окремі процеси:

- `bot` — long polling для локального/поточного запуску;
- `api` — FastAPI для checkout і callback WayForPay/Telegram;
- `worker` — закінчення підписок, видалення доступу, розсилки;
- `db` — PostgreSQL;
- `migrate` — застосування Alembic-міграцій перед запуском.

Не можна одночасно запускати polling і Telegram webhook для одного bot token.
WayForPay callback завжди потребує публічної адреси API.

## Реалізована бізнес-логіка

### Користувачі та підписки

- У користувача є Telegram-кабінет із постійним меню.
- Якщо активний лише один тариф, вибір тарифу користувачеві не показується.
- Архітектура БД і сервісів підтримує декілька тарифів.
- Тариф складається з довільного набору Telegram-каналів і supergroup/forum-груп.
- Після підтвердженої оплати бот надсилає повідомлення про активну підписку.
- Під повідомленням створюються персональні invite-кнопки з діамантом для кожного
  ресурсу тарифу.
- Invite link має обмежений строк дії та створює Telegram join request.
- Бот автоматично підтверджує заявку лише коли Telegram ID заявника збігається
  з власником invite і чинний entitlement покриває конкретний ресурс.
- Чужа заявка відхиляється, а скомпрометований invite одразу відкликається.
- Повторний callback не повинен повторно активувати або дублювати оплату: обробка
  ідемпотентна.
- Сума й валюта snapshot-яться під час створення checkout. Навіть валідно підписаний
  callback не активує/не продовжує підписку, якщо його сума або валюта не збігається.
- Видалення доступу перевіряє всі інші чинні підписки користувача й не забирає ресурс,
  який усе ще покритий іншим entitlement.
- Кнопка скасування викликає WayForPay `SUSPEND`, але entitlement діє до
  `current_period_end`.
- Після закінчення entitlement worker забирає доступ у Telegram.
- Передбачені `past_due` і grace period для невдалого повторного платежу.

### Адміністрування

Постійний bootstrap-адміністратор: Telegram ID `402152266`.
Вхід до панелі: команда `/admin`.

Реалізовано:

- додавання та видалення інших адміністраторів;
- автоматичне збереження каналу/supergroup у БД, коли бота додають адміністратором;
- сповіщення адміністраторам про доданий ресурс;
- створення тарифів і формування їх із зареєстрованих ресурсів;
- редагування назви/ціни тарифу, безпечне архівування та відновлення неосновних тарифів;
- приховування тарифу в користувацькому UI, якщо він один;
- створення rich-text розсилок зі збереженням Telegram entities;
- одиночне медіа та media groups;
- додавання URL-кнопок до розсилки;
- preview і постановка розсилки в чергу;
- HTML-звіт зі статистикою;
- редагування назв основних кнопок;
- редагування форматованого стартового повідомлення та повідомлення після успішної оплати;
- окремий WayForPay test mode на 30 хвилин, керований з адмін-панелі;
- CRUD HTML-сторінок вступу з публічними URL `/join/<slug>`, безпечними
  плейсхолдерами та аватаром із профілю Telegram-бота без розкриття bot token;
- attribution кожного Telegram `/start landing_<slug>` із загальними/унікальними
  переходами, approved-payment conversion та списком останніх відвідувачів у адмінці;
- окремий rich-text/медіа/альбом/URL-кнопки для дій «Про клуб»,
  «Доєднатися», «Матеріали», «Техпідтримка» та інших основних пунктів.

### Майбутні можливості, під які закладено структуру

- кілька публічних тарифів;
- реферальні коди та attribution;
- винагорода за приведеного платного користувача;
- бонусний місяць, знижка або credit ledger;
- масштабування API окремо від worker.

## Платіжний сценарій

1. Кнопка оплати в боті містить короткоживучий HMAC-підписаний owner token.
   `GET /checkout` перевіряє його server-side та створює checkout, уже прив'язаний
   до Telegram-користувача, не розкриваючи внутрішній API key. Альтернативно backend
   окремого сайту викликає `POST /api/v1/checkout-sessions`.
2. Внутрішній endpoint захищається заголовком `X-Internal-API-Key`.
3. API повертає `gateway_url`, підписані `gateway_fields`,
   `bot_claim_url` і `order_reference`.
4. Підписана форма невидимо й автоматично відправляється на платіжну сторінку
   WayForPay; видима кнопка залишається fallback для браузерів без JavaScript.
5. WayForPay викликає `POST /webhooks/wayforpay`.
6. Callback приймається тільки після перевірки HMAC-MD5.
7. Approved-платіж активує/продовжує підписку.
8. Для персонального checkout підписка активується одразу в callback, а бот
   автоматично надсилає підтвердження та персональні invite-кнопки.
9. `GET` або provider `POST` на `/checkout/complete` є інформаційним і не видає
   доступ. Для анонімного checkout `bot_claim_url` залишається fallback-прив'язкою.

Тестовий режим WayForPay:

- зберігається в `AppSetting` як час автоматичного завершення;
- впливає лише на checkout, створені після ввімкнення;
- використовує офіційні тестові реквізити WayForPay та префікс `TEST-*`;
- production checkout має префікс `CLUB-*`;
- callback і відповідь провайдеру підписуються клієнтом, вибраним за префіксом order reference;
- тестовий approved callback проходить повний application-flow та може видати Telegram-доступ,
  але не списує реальні кошти.

WayForPay API adapter:
`src/club_bot/integrations/wayforpay.py`.

Поточна регулярність checkout — `monthly`. API регулярних платежів підтримує
`STATUS`, `SUSPEND` і `RESUME`.

## Основні модулі

```text
src/club_bot/
├── api.py                         FastAPI routes і webhooks
├── bot/
│   ├── routers.py                 користувацькі handlers
│   ├── admin_router.py            адмін-панель
│   ├── system_router.py           chat/member updates
│   ├── keyboards.py               користувацькі клавіатури
│   └── admin_keyboards.py         адмін-клавіатури
├── domain/                        enums і чисті правила
├── integrations/wayforpay.py      підписи та regularApi
├── services/
│   ├── access.py                  invite/revoke Telegram-доступу
│   ├── subscriptions.py           lifecycle підписки
│   ├── subscription_notifications.py
│   ├── admin.py
│   ├── broadcasts.py
│   ├── stats.py
│   └── telegram_content.py
├── models.py                      SQLAlchemy models
├── repositories.py               DB queries
├── container.py                  dependency composition
├── worker.py                      expiration/revocation/broadcast jobs
└── config.py                      typed environment settings
```

## Конфігурація та секрети

Робочі значення знаходяться тільки у `.env`; файл виключений із Git.
Не друкувати й не копіювати в документацію:

- `BOT_TOKEN`;
- `BOT_WEBHOOK_SECRET`;
- `INTERNAL_API_KEY`;
- `WAYFORPAY_SECRET_KEY`;
- `WAYFORPAY_MERCHANT_PASSWORD`.

Merchant Login, Secret Key і Merchant Password WayForPay вже внесені локально.
На момент створення цього документа ще не налаштовані:

- `WAYFORPAY_MERCHANT_DOMAIN`;
- `PUBLIC_BASE_URL`;
- реальна `MEMBERSHIP_SITE_URL`;
- DNS і HTTPS production-сервера.

Домен користувач оформлює. Після отримання домену рекомендована схема:

- `PUBLIC_BASE_URL=https://api.<domain>`;
- `WAYFORPAY_MERCHANT_DOMAIN=<domain>`;
- callback: `https://api.<domain>/webhooks/wayforpay`.

Не змішувати тестові й production-реквізити WayForPay. Для E2E-тестування бажано
додати явний test environment або окремий `.env.test-wayforpay`.

## Поточний локальний стан

- Docker запускав `db`, `bot`, `worker`.
- Alembic застосований до `c3d4e5f60718 (head)`; schema drift відсутній.
- Автоматичний backup і повне тестове restore локальної PostgreSQL перевірені.
- Regression та симульований lifecycle E2E проходять:
  checkout → callback → claim → invite → renewal → cancel → expiration → revoke.
- `api` локально не був запущений постійно, оскільки публічний домен ще не готовий.
- Бот працює через polling.
- Поточні дані PostgreSQL потрібно зберігати при перенесенні/перезапуску.
- Docker Compose project name зафіксовано як `neurokolo`.
- Проєкт знаходиться в `/Users/mac/Documents/Project/neurokolo`.
- Папку позначено у Finder як «Keep Downloaded», щоб iCloud не вивантажував код.
- Локальний virtualenv навмисно зберігається поза синхронізованою папкою:
  `/Users/mac/Library/Caches/neurokolo/venv`.

## Команди

Запуск:

```bash
docker compose up -d db bot worker
```

Після налаштування публічного API:

```bash
docker compose up -d db api worker
```

Статус і логи:

```bash
docker compose ps
docker compose logs --tail=100 bot worker api
```

Міграції:

```bash
docker compose run --rm migrate
```

Перевірки:

```bash
NEUROKOLO_VENV=/Users/mac/Library/Caches/neurokolo/venv
"$NEUROKOLO_VENV/bin/python" -m ruff check .
"$NEUROKOLO_VENV/bin/python" -m mypy src
"$NEUROKOLO_VENV/bin/python" -m pytest
```

## Правила продовження розробки

1. Не видавати доступ на основі redirect/return URL — тільки після валідного
   підписаного callback WayForPay.
2. WayForPay не керує Telegram напряму: callback змінює subscription state,
   а `AccessService` застосовує entitlement.
3. Зберігати ідемпотентність callback, worker jobs і розсилок.
4. Не показувати вибір тарифу, доки активний один тариф.
5. Не видаляти користувача одразу після cancel — тільки після оплаченого періоду.
6. Не зберігати завантажені Telegram-медіа на сервері без потреби: використовувати
   `file_id` і entities.
7. Не додавати секрети до Git, логів, тестових fixtures або цього файла.
8. Перед зміною схем БД створювати Alembic-міграцію.
9. Перед завершенням змін запускати pytest, Ruff і mypy.
10. Для production налаштувати firewall, HTTPS, автоматичний PostgreSQL backup,
    моніторинг і регулярне оновлення системи.

## Підготовлений production-контур

- `compose.production.yml` не публікує PostgreSQL або порт API.
- Caddy автоматично термінує HTTPS на 80/443.
- Секрети монтуються файлами з `secrets/`, а не потрапляють до Docker build context.
- Backup-контейнер щодня створює dump і відновлює його в тимчасову БД для перевірки.
- Prometheus збирає API-метрики й контролює unmatched approved payments.
- Python-залежності й базові Docker images зафіксовані lock/hash/digest.
- Production-конфігурація fail-fast відхиляє placeholder-домени та dev credentials.
- Вбудований checkout рендерить підписану WayForPay-форму server-side, автоматично
  відправляє її до WayForPay та повертає користувача до персонального Telegram claim URL.
- Адмін може редагувати тариф, архівувати/відновлювати неосновні тарифи, змінювати
  `/start` і post-payment повідомлення та вмикати ізольоване 30-хвилинне тестове вікно WayForPay.

## Найближчі кроки

1. Отримати домен і вибрати піддомен API.
2. Замовити VPS (рекомендовано Hetzner CX23, Ubuntu 24.04, IPv4, backups).
3. Розгорнути Docker Compose на сервері.
4. Налаштувати DNS; Caddy автоматично отримає HTTPS-сертифікат.
5. Заповнити production URL у `.env.production`.
6. Створити нові production secrets за `secrets/README.md`.
7. Провести зовнішній staging E2E з WayForPay: checkout → callback → Telegram claim →
   confirmation → invite → cancel → expiration → revoke.
8. Після тесту перевипустити секрети, які могли бути передані через чат.
9. Провести одну контрольну production-оплату та перевірити server-side backup restore.
