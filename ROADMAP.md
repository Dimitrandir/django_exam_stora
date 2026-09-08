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
- [ ] **deliveries** — зарежда склада. Тестовете тук вече са сравнително
      добри (77 реда) — добра основа за разширяване, не пренаписване от нула.
      **Нов ред от Fase "products":** `delivery_quantity` вече Decimal
      (max_digits=10, decimal_places=3) в модела, но формата/темплейта още
      очакват цяло число визуално (step, placeholder) — трябва UI за
      теглови доставки (кг) при одита на този app.
- [ ] **sales** — вече започнахме: поправени bugs в `models.py` (delta-based
      stock adjustment, restore on delete, atomic locking, signal-based
      total). Остава: `forms.py`, `views.py`, пълен преглед на error
      handling в темплейтите (частично оправено). **Нов ред:**
      `sale_quantity` вече Decimal в модела — трябва UI за продажба на
      теглови артикули (кг) при одита на този app.
- [ ] **reports** — почти нулево тестово покритие (3 реда). Пиши тестове
      ПРЕДИ рефакторинг тук, не след.

## Фаза 1.5 — Sortable/filterable/export таблици (виж CLAUDE.md за конвенцията)

Tabulator patтern-ът (sort по клик, Excel-style checkbox филтър по колона,
export to Excel) е готов и е приложен за Products списъка и Product History.
Остава да се приложи и за:

- [ ] Suppliers списък (`products/templates/products/suppliers_list.html`)
- [ ] Categories списък (`products/templates/products/category_list.html`)
- [ ] Employees списък (`accounts/templates/accounts/employee_list.html`)
- [ ] Sales списък (`sales`)
- [ ] Deliveries списък (`deliveries`)
- [ ] Reports таблици (`reports/templates/reports/*.html`)

## Фаза 2 — Редизайн на интерфейса за продажби

- [ ] Пълен UI/UX редизайн на `sales` модула (потребителят иска това
      конкретно преработено, не само closed bugs).
- [ ] Запази/подобри draft-save механизма (в момента през JS + fetch към
      `sale_draft_save`) — работи, но да се прегледа за race conditions при
      бързо кликане.

## Фаза 3 — Нови функционалности

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
