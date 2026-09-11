# ROADMAP — STORA

Фазиран план. Реда на модулите е по зависимост (по-надолу зависи от по-горе),
за да не се преработва вече "готова" логика. Отмятай със `[x]`, докато Claude
Code напредва — това е живият backlog, за разлика от `CLAUDE.md`.

## Фаза 1 — Одит и стабилизация на съществуващите модули

Цикълът за всеки app: провери бъгове → поправи → тест → докладвай статус →
оптимизирай → следващ app. (Пълните правила на процеса са в `CLAUDE.md`.)

- [x] **accounts** — auth, роли, права. Django Groups вече реално enforced
      (permission_required по views, sync на role→group през сигнал),
      register е Manager-only, login грешките са оправени.
- [x] **core** / **products** — мъртъв дублиран код в `session_service.py`
      махнат; permissions по products views (Cashier: view-only, Warehouse:
      add/change, Manager: всичко); `sell_price` вече задължително;
      `Barcode.code` уникален; `quantity` реално read-only (`disabled`, не
      само widget attr); N+1 в списъка оправен; добавена **Product History**
      страница (продажби+доставки timeline с период филтър); Barcode/Supplier
      с `position` (primary + алтернативи); **`unit_type` (piece/weight)** +
      `quantity` навсякъде вече Decimal (виж CLAUDE.md "Известни бизнес
      правила") — схемата и Products UI готови, Sales/Deliveries UI още не.
      **Нов ред:** продукти-рецепти (`Product.is_recipe` + `RecipeIngredient`,
      виж CLAUDE.md "Известни бизнес правила") — модел, миграция, Products UI
      (формсет за съставки на create/edit/details/list), продажбена логика
      (`adjust_stock_for_sale` приспада съставки вместо собствена наличност)
      и фонов backfill за минали продажби — готови, покрити с тестове
      (`RecipeProductTests`, `RecipeStockDeductionTests`,
      `RecipeBackfillTaskTests`) и проверени на живо в браузъра. Между другото
      се откри и оправи реален бъг извън обхвата на рецептите:
      `SomeTask.delay(...)` е чупело всяка продажба/запис с 500 грешка, когато
      Redis не тече (виж CLAUDE.md "Технически капани" — `dispatch_task`).
- [x] **deliveries** — одитиран и преработен. Намерени и оправени бъгове:
      `DeliveryItems.save()` добавяше цялото `delivery_quantity` при ВСЯКО
      save (вкл. редакция на съществуващ ред) вместо delta, и нищо не
      връщаше наличността при триене на ред/цяла доставка — сега
      delta-based + `post_delete` restore, огледално на `sales/models.py`
      (виж CLAUDE.md "Известни бизнес правила"). Добавени permission checks
      по всички delivery views (Cashier: view-only, Warehouse: add/change,
      Manager: всичко) — преди само `@login_required`. Счупен
      `{% for %}/{% empty %}` в `deliveries_list.html` (висящ `<tr>`, никога
      не показваше "no deliveries") оправен. `delivery_quantity` в формата
      мина от `IntegerField` на `DecimalField` (приема тегловни кг
      количества). **Нови функции по молба на потребителя:** търсим
      доставчик вместо `<select>` + "+ New supplier" popup; типовете
      документи станаха управляем списък (`DocumentType`, като
      Category/TaxGroup, Manager-only, без popup shortcut от формата),
      вместо фиксиран `choices=[...]`; "+ New product" линк от екрана за
      доставка; таблицата с артикули е вече Tabulator с пълен feature set
      — сортиране, Excel-style филтър, column chooser, export to Excel,
      пореден номер, auto-focus flow (Qty→Price при добавяне), editable
      Qty/Unit Price/без-ДДС/Line Total/Sell Price/Markup %/Expiry Date,
      двупосочна връзка между тях (вкл. Line Total→Unit Price backfill),
      цветово оцветяване при промяна на доставна цена спрямо каталога, и
      Sell Price/Markup % се записват веднага в `Product.sell_price` —
      виж CLAUDE.md за пълната архитектура (`_delivery_items_table.html`).
- [x] **sales** — `models.py` вече беше поправен (delta-based stock
      adjustment, restore on delete, atomic locking, signal-based total).
      `views.py`/`forms.py`/темплейти одитирани и поправени:
      - **Критично:** НИТО една sales view нямаше login/permission проверка
        — напълно отворени за анонимен потребител (`sales_add`,
        `sales_draft_save`, detail/list/delete). Groups (Cashier/Manager)
        вече имаха правилните permissions от `accounts/signals.py`, просто
        никой view не ги проверяваше. Добавени `@login_required`/
        `permission_required` + `StaffPermissionRequiredMixin`, огледално
        на products/deliveries (Cashier: пълен достъп, Warehouse: никакъв,
        Manager: всичко).
      - Теглови продукти вече реално могат да се продават през формата:
        `sale_quantity` input имаше твърдо `min="1"` без `step` — браузърът
        native-валидно отказваше дробна стойност (0.350 кг) при submit,
        въпреки че JS-ът за везнени баркодове вече я записваше в полето.
        Фикс: `min`/`step` стават 0.001 динамично при избор на `unit_type=
        weight` продукт (`applyQuantityConstraints()` в `sale_add.html`,
        огледално на deliveries грида).
      - Счупен `{% for %}/{% empty %}` в `sales_list.html` (същия бъг като
        по-рано в `deliveries_list.html`).
      - Излишен двоен `sale.save()` в `sales_add` махнат (безопасен, но
        ненужна заявка).
      23 нови/разширени теста в `sales/tests.py`. Везнени баркодове вече
      работещи от преди — виж CLAUDE.md "Известни бизнес правила".
- [ ] **reports** — почти нулево тестово покритие (3 реда). Пиши тестове
      ПРЕДИ рефакторинг тук, не след. **Нова задача (отложена от
      потребителя):** справките да смятат "цена без ДДС" on-the-fly от
      `Product.tax_group.rate` (виж CLAUDE.md "Известни бизнес правила" —
      данъчни групи) — не е нужно ново поле в базата, само изчисление.

## Фаза 1.5 — Sortable/filterable/export таблици (виж CLAUDE.md за конвенцията)

Tabulator patтern-ът (sort по клик, Excel-style checkbox филтър по колона,
export to Excel) е готов и е приложен за Products списъка и Product History.
Остава да се приложи и за:

- [ ] Suppliers списък (`products/templates/products/suppliers_list.html`)
- [ ] Categories списък (`products/templates/products/category_list.html`)
- [ ] Employees списък (`accounts/templates/accounts/employee_list.html`)
- [ ] Sales списък (`sales`)
- [x] Deliveries списък — вместо самостоятелния `deliveries_list.html`,
      **`reports/deliveries_report.html` пое тази роля** (Tabulator, период
      + supplier филтър, клик на ред отваря/редактира/трие) — потребителят
      реши да не дублира двата екрана. `deliveries_list` remains жив само
      като route/redirect target, не се навигира до него от менюто повече.
- [ ] Reports таблици (останалите — `sales_report.html`, `dashboard.html`)

## Фаза 2 — Редизайн на интерфейса за продажби

- [ ] Пълен UI/UX редизайн на `sales` модула — вдъхновен от реален POS
      софтуер (screenshot от потребителя). Обхват потвърден: количка +
      категории + checkout стъпка (плащане в брой/карта + ресто). НЕ:
      клиенти/баланс/бонус точки (отделна бъдеща задача), множество
      едновременни отворени кошници (само една активна продажба, старият
      draft-save механизъм остава). Изгражда се на етапи:
  - [x] **Подкатегории** (`Category.parent`, self-FK) — направено като
        подготовка за бутоните по категория/подкатегория. Виж CLAUDE.md за
        архитектурата (cycle prevention, `get_descendant_ids()`).
  - [x] **Стъпка 1 — данни:** `SaleAttributes.payment_method`/`amount_paid`/
        `change_due` (nullable), `products_data`+`category_id`, ново
        `categories_data` в контекста на `sales_add`.
  - [x] **Стъпка 2 — дясна лента с категории:** папки за drill-down +
        продукт-бутони (директно assign-нати на текущото ниво), клик добавя
        в количката/увеличава количество, жив часовник горе ляво. Виж
        CLAUDE.md за пълната архитектура. Реален бъг хванат по пътя: HTML5
        `step` е относителен спрямо `min`, не спрямо 0 — default `sale_
        quantity` widget-ът блокираше ВСЯКА цяла бройка (виж CLAUDE.md
        "Технически капани").
  - [ ] **Стъпка 3 — checkout модал:** метод на плащане (В брой/С карта) +
        Сума/Платено/Ресто калкулатор. Не е започнато.
  - [ ] **Стъпка 4 — количката на Tabulator:** заменя сегашния plain-table
        + vanilla JS с Tabulator (редактируем Qty inline и т.н.), огледално
        на deliveries грида. Най-рисковата стъпка (заменя работещ, тестван
        код) — изрично отложена докато стъпки 1-3 не са готови и потвърдени.
- [ ] Запази/подобри draft-save механизма (в момента през JS + fetch към
      `sale_draft_save`) — работи, но да се прегледа за race conditions при
      бързо кликане.

## Фаза 3 — Нови функционалности

- [x] **Изписване (Write-off)** — "Stock Movements" вече обхваща и
      доставка, и изписване (`DeliveryAttributes.movement_type`), вместо
      отделен app/модел. Изписването намалява наличността (обратен знак на
      delta-based логиката), пази се задължително към доставчик, с
      автогенериран вътрешен номер при празно поле. Отделни nav линкове
      ("Delivery"/"Write-off"/"Scrap"), споделен Tabulator грид с визуално
      разграничение (червена рамка + "−" пред количество/сума на ред).
      Справките за доставки explicit филтрират movement_type, за да не
      изтичат изписвания в тях — виж CLAUDE.md за пълната архитектура.
      Нито изписване, нито брак показват "+ New supplier"/"+ New product"
      шорткътите — не можеш да изпишеш/бракуваш нещо, което тепърва създаваш.
- [x] **Брак (Scrap)** — трети `movement_type=SCRAP`, без доставчик (полето
      е nullable), с управляем списък причини (`ScrapReason`, като
      DocumentType/TaxGroup, Manager-only add/change). Два варианта за
      въвеждане на едно и също "Brak" движение: (1) търсачка за вече
      заредена партида по продукт (само редове с `expiry_date`, само от
      реални доставки — `BatchSearchView`/`batch_search` endpoint), която
      попълва `DeliveryItems.source_item` (self-FK) за проследимост назад
      към оригиналния ред, и `expiry_date`; (2) свободно въвеждане
      продукт+количество+причина (същата продуктова търсачка като
      доставка/изписване), без `source_item`. `source_item` е `SET_NULL`,
      за да не се изтрива историята при брак, ако оригиналният ред/доставка
      по-късно се трие. Виж CLAUDE.md за пълната архитектура.
- [ ] **Справка за изписвания** — отделна справка (аналог на
      `deliveries_report.html`) само за WRITE_OFF редове. Отложено изрично
      от потребителя ("после") — не е имплементирано.
- [ ] **Ревизии** — корекции/сторно на вече завършени продажби (различно от
      редакция на чернова) — да се дефинира точен workflow с потребителя
      преди implementation.
- [ ] **AI интеграция за доставки** — асистиран анализ/предложения при
      въвеждане на доставка (напр. предложения за количества по история).
- [ ] **AI интеграция за справки** — заявки на естествен език към
      съществуващите модели/DRF endpoints (не пренаписване на reports app,
      а нов слой върху него).

## Фаза 4 — Хардуер интеграции

- [ ] Интеграция с касови апарати (фискализация — да се провери конкретен
      модел/протокол преди работа, силно зависи от българското
      законодателство за фискални устройства).
- [ ] Четене от баркод скенери (типично работят като HID клавиатура —
      вероятно не изисква специален драйвер, но да се потвърди с
      конкретния модел скенер).
- [ ] Печат на етикети (да се избере принтер/протокол — ZPL и т.н. — преди
      имплементация).

## Отворени въпроси (за изясняване с потребителя преди съответната фаза)

- Multi-tenancy / повече от един физически обект — засега извън обхват,
  но да се има предвид при дизайн на нови модели, за да не пречи по-късно.
- Точен модел каса/фискален принтер за интеграцията в Фаза 4.
