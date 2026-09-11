# SESSION.md — STORA

Кратък чеклист "докъде спряхме", обновяван в края на всяка сесия. Различно
от `ROADMAP.md` (какво остава като задачи занапред) и `CLAUDE.md` (трайни
правила/капани) — тук пишем конкретно какво стана последната сесия, какво е
проверено как, и какво чака отговор/решение от потребителя. Най-новата
сесия е най-отгоре; по-старите се пазят под нея.

---

## 2026-09-10

**Статус:** Одитът/преработката на `deliveries` (кръгове 1-8 по-долу) е
комитната (3 комита). Девети-единадесети кръг ("Stock Movements" +
Write-off + Scrap + Enter-верига/movable columns, най-долу в тази секция)
е НЕкомитнат.

**Какво стана тази сесия:**
- Кратко обсъждане (без код) за бъдеща AI интеграция: constrained
  function-calling за промени по данни (с preview/потвърждение, през
  `.save()` не bulk, за да остане в `ProductChangeLog`), vs по-отворен
  read-only достъп за справки (отделна read-only DB роля), само за
  мениджъри. Нищо конкретно решено/имплементирано — за по-късно.
- Одит на `deliveries` (Фаза 1 от ROADMAP) — намерени и оправени бъгове:
  - **Критичен:** `DeliveryItems.save()` добавяше пълното количество към
    наличността при ВСЯКО save, включително при редакция на съществуващ
    ред (дублиране), и нищо не връщаше наличността при триене на
    ред/доставка. Фикс: delta-based + `post_delete` restore, огледално на
    `sales/models.py`. Покрито с тестове (нов ред + редакция + триене ред +
    триене цяла доставка + total_amount recalc).
  - Нямаше permission checks по delivery views (само `@login_required`) —
    добавени, по модела Cashier=view / Warehouse=add+change /
    Manager=всичко.
  - Счупен `{% for %}/{% empty %}` в `deliveries_list.html`.
  - `delivery_quantity` в формата беше `IntegerField` вместо `DecimalField`
    (не приемаше тегловни кг количества).
- Нови функции, поискани директно от потребителя:
  - Доставчик вече е търсачка (`supplier_search` endpoint) + "+ New
    supplier" popup, вместо `<select>`.
  - Типове документи станаха управляем списък (`DocumentType` модел, CRUD
    + "+ New document type" popup) вместо фиксирани `choices=[...]` —
    мигрирано през add-populate-swap серия миграции, старите данни (4 реда
    в тестовата база) се пренесоха коректно.
  - "+ New product" линк от екрана за доставка (popup таб, без postMessage
    auto-inject обратно в грида — продуктът е веднага търсим в grid-а чрез
    живо търсене в базата, но не auto-избран; споменато на потребителя като
    компромис, не е имплементирано пълно popup-postMessage за продукти
    заради по-голямата сложност на продуктовата форма).
  - Таблицата с артикули в доставка е вече Tabulator: sort, Excel-style
    филтър, search-to-add ред (reuse на `ingredient_search`), inline
    редакция на Qty/Unit Price, "Remove" бутон, bottomCalc сума. Визуалната
    таблица е 100% JS state, синхронизирана в Django hidden formset inputs
    точно преди submit — виж CLAUDE.md за пълната архитектура.
  - Баркод скенер: точен код/баркод match през Enter в search полето
    работи (тествано на живо с реален баркод); везнените 13-цифрени
    баркодове с вградено тегло НЕ са имплементирани тук още — потребителят
    изрично каза ще донесе физически скенер по-късно за тест.
- Втори кръг допълнения по грида (поискани веднага след първия round):
  - Автоматичен focus flow: добавиш продукт → Qty полето е директно в edit
    режим; след въвеждане на количество → автоматично преминава на Unit
    Price (`row.getCell(...).edit(true)` във всеки `cellEdited`).
  - Нови колони: Unit Price без ДДС, Line Total без ДДС (скрити по
    подразбиране, `visible:false`, достъпни през Columns ▾), Sell Price,
    Markup %, Expiry Date (нов модел field `DeliveryItems.expiry_date`,
    per ред не per продукт — миграция направена), пореден номер `#`.
  - Пет полета свързани двупосочно през обща `recalcRow(row, source)`:
    Unit Price ↔ без ДДС (по `tax_group.rate`), Sell Price ↔ Markup %,
    и Line Total → Unit Price (въвеждаш тотала от фактурата за дадено
    количество, системата смята обратно единичната цена).
  - Unit Price клетката светва червено/зелено при промяна спрямо текущата
    `Product.delivery_price` в системата.
  - Sell Price/Markup % се записват ВЕДНАГА в `Product.sell_price` (същия
    `product_inline_update` endpoint като "Enable Edit" в Products
    грида) — независимо от submit на доставката; revert на клетката при
    грешка от сървъра.
  - Export to Excel + Columns ▾ вече активни (бяха изключени в първия
    round); Qty колоната сумира долу (`bottomCalc:'sum'`).
  - Потребителят изрично поиска "+ New document type" popup-a да се
    МАХНЕ от формата за доставка, и само Manager (не Warehouse) да може
    да добавя/редактира типове документи — Warehouse пази само view права,
    popup handling-ът в `DocumentTypeCreateView` е премахнат изцяло
    (недостижим от никъде вече).
- Трети кръг фийдбек (веднага след като показах втория round):
  - Махнат отделния "Transaction Total" под таблицата — дублираше
    bottomCalc-а на Line Total колоната; добавен bottomCalc и на Line Total
    (no VAT), за симетрия.
  - Пълна клавиатурна навигация в search dropdown-а: стрелки
    надолу/нагоре местят маркировка между резултатите (първият винаги
    авто-маркиран), Enter добавя маркирания — не просто "първия що падне".
    Ако dropdown е затворен, Enter пада на exact code/barcode match, както
    преди.
  - **Реален бъг, хванат по време на тестване:** резултатите от
    `ingredient_search` (endpoint-ът за search dropdown-а) не носят
    `sell_price`/`tax_group__rate` — построен е бил за recipe picker-а,
    който не ги ползва. Добавянето на продукт през dropdown-а следователно
    пишеше Sell Price=0 и Markup=-100% на новия ред, вместо реалните
    стойности. Фикс: `addProductRow()` вече винаги презарязва подадения
    продукт с пълния запис от `productsById` (страницата's own
    `products_data`) по id, преди да построи реда — виж CLAUDE.md.
- Четвърти кръг (докладвано от потребителя след реално тестване с мишка/
  клавиатура, не през автоматизация): Qty клетката ИЗГЛЕЖДАШЕ фокусирана
  след добавяне на продукт, но реално не приемаше клавиатурно въвеждане
  докато не цъкнеш с мишката върху нея — `cell.edit(true)` рендира
  `<input>`-а, но не винаги реално го фокусира (особено при извикване от
  Promise `.then()`, какъвто е случаят при `table.addRow(...).then(...)`).
  Фикс: нов `focusCellEditor(cell)` helper, вика `.edit(true)` + explicit
  `.focus()`/`.select()` върху рендернатия `<input>` — заменя голото
  `.edit(true)` навсякъде. Проверено на живо: `document.activeElement`
  реално е input-ът на Qty клетката след Enter, и въвеждане в него без
  ръчен `.focus()` работи коректно.
- Пети кръг (веднага след): "сега работи, но като натисна Enter, страницата
  се рефрешва и всичко се прецаква." Два отделни проблема, и двата
  оправени:
  1. Enter в поле вътре в `<form>` праща формата по браузърен default —
     цялата доставка се submit-ваше по средата на въвеждане. Fix:
     delegated `keydown` listener на `.delivery-form__items`,
     `preventDefault()` безусловно за Enter във всеки input/textarea там.
  2. Открито при дебъг: вграденият Tabulator `editor:'number'` НЯМА
     никаква Enter логика изобщо (потвърдено срещу vendored source —
     keybindings мапват само Tab/стрелки) — затова auto-advance
     Qty→Price никога реално не се задействаше през истинска Enter
     клавиша (само през директен `cell.setValue()` API извикване, което
     заблуди по-ранните тестове тази сесия). Фикс: собствен
     `numberEditor` за всички число-колони, с explicit Enter→`success()`
     handling — виж CLAUDE.md. Освен това `focusCellEditor` разширен да
     взима `(row, field)` вместо готова `cell` референция — старата
     референция понякога е ставала невалидна след `row.update()`
     преизрисуване, и добавен `setTimeout(0)` да изчака Tabulator да
     приключи текущия redraw, преди да отвори следващата клетка.
  Проверено на живо, стъпка по стъпка с реални DOM Enter събития (не
  `setValue()` shortcut): търсене→Enter избира продукт→Qty реално
  фокусиран→писане на количество→Enter коректно преизчислява (delta,
  markup, без-ДДС) И премества фокуса на Unit Price реално (`document.
  activeElement` потвърдено) → писане на цена→Enter коректно commit-ва,
  НИКЪДЕ страницата не презарежда (маркер в `window` оцелява през целия
  flow).
- Шести кръг — два дребни, но реални UX бъга:
  1. Supplier search dropdown-ът излизаше позициониран под "Document
     Number", не под "Supplier" — контейнерът му беше `<span
     class="position-relative d-inline-block">` **вложен вътре в
     `<p>`** — невалиден HTML (`<span>` е ОК в `<p>`, но `d-inline-block`
     + `position:absolute; left:0; right:0` дете вътре създава известни
     shrink-to-fit ширина edge cases). Фикс: сменен на пълен block-level
     `<div class="position-relative">` в собствен `<div class="mb-3">`
     контейнер (не `<p>`) — огледално на вече проверения patтern от
     ingredient/product picker-ите. Проверено на живо с
     `getBoundingClientRect()`: dropdown-ът сега е точно под input-а
     (top съвпада с input bottom), далеч над Document Number.
  2. `{{ form.errors }}` се извеждаше като чист неоцветен текст горе на
     страницата — потребителят буквално не забелязал грешка "забравен
     доставчик" при реален тест. Фикс: `.form-errors` в `static/style.css`
     вече е визуален "danger alert" (червен фон/рамка, удебелен текст,
     `::before` заглавие "⚠ Please fix the following:"). Това е глобален
     CSS клас, споделен и от `sales/sale_add.html` — подобрението важи и
     там автоматично, не само за deliveries.

- Седми кръг — layout + `deliveries_report` overhaul, поискано директно:
  - `delivery_add.html`/`delivery_edit.html` вече `main-wide` +
    `container-fluid` (заема цялата ширина, таблицата с артикули има
    повече място).
  - `reports/deliveries_report.html` (view + template): plain HTML
    таблицата заменена с пълен Tabulator — sort, Excel-филтър, column
    chooser, export to Excel, ред за пореден номер, bottomCalc на
    сумите. Нови колони: Document Number, Document Date, Total (no VAT,
    скрита по подразбиране). Клик на ред отваря `delivery_details` (edit/
    delete оттам, не дублирано в грида). **Реален бъг, открит и оправен
    докато пишех тестовете преди рефакторинга (нулево покритие преди
    това, виж CLAUDE.md правилото):** `supplier` полето на filter формата
    съществуваше в темплейта от преди, но `DeliveriesReportView` никога
    не го прилагаше към queryset-а — избор на доставчик тихо не правеше
    нищо. Оправено + supplier полето вече е searchable picker (като
    навсякъде другаде), не `<select>`.
  - "Deliveries list" (обикновения списък в `deliveries` app-а) премахнат
    от навигацията — вече дублираше `deliveries_report`, който е
    по-богат и интерактивен. URL/view-то `deliveries_list` е ОСТАВЕНО
    живо нарочно (използва се за redirect targets на няколко места:
     след create, след delete, cancel бутон) — само навигационния линк е
    махнат, не самата страница/route.
  - Тествано: 6 нови теста в `STORA/reports/tests.py` (преди тази сесия
    почти нулево покритие на reports app-а) — period filter, supplier
    filter (регресионен тест за бъга по-горе), shape на JSON данните,
    коректно изчисление на "без ДДС" при смесени данъчни групи в една
    доставка, summary totals. 170/170 общо. На живо: колоните се
    рендират с реални данни, row-click отваря правилната доставка,
    supplier search picker връща правилния id.
  - Веднага след: за доставчик пикъра на самия `deliveries_report`
    филтър (не delivery формата) — сменен на searchable picker по същия
    patтern, плюс Document Number/Document Date колони в грида.

- Осми кръг — `delivery_details.html` (страницата "Details for Delivery")
  премина от плоска 5-колонна таблица към същия богат read-only Tabulator
  изглед като editable грида на Add/Edit (# , Code, Name, Qty, Unit Price,
  Unit Price без ДДС, Line Total (двете), Sell Price, Markup %, Expiry
  Date — без редактори, само formatters + column chooser + export).
  `DeliveryDetailView._item_row()` смята без-ДДС/markup по продуктовата
  tax_group на реда; Sell Price/Markup % са ЖИВ snapshot на текущата
  `Product.sell_price`, не историческа стойност от момента на доставката.
  main-wide + container-fluid добавени и тук. 2 нови теста (с/без
  tax_group на продукта). 172/172 общо. Проверено на живо с реални данни.

- Девети кръг (нова задача, след комит на горното) — "Движение на стоки":
  потребителят иска `deliveries` да покрие и изписване (към доставчик,
  намалява наличност) и по-късно брак (без доставчик, за изтекъл срок).
  Тази сесия имплементира ИЗПИСВАНЕТО; брак е отложен за следваща стъпка
  (виж ROADMAP.md).
  - `DeliveryAttributes.movement_type` (DELIVERY/WRITE_OFF, фиксирани
    choices, не управляем списък) — изписването е СЪЩИЯТ модел/таблица,
    само знакът на наличността се обръща в `DeliveryItems.save()`/
    `_restore_stock_on_delete`. Потребителят винаги въвежда положително
    количество, знакът е чисто вътрешна логика.
  - `document_type` вече nullable (изписване няма входящ документ),
    `document_number` разширен на 20 символа и опционален за изписване —
    ако е празен, автогенерира се вътрешен номер
    (`WO-20260910-001`, поредност по ден+тип).
  - Нова `WriteOffForm` (без document_type), споделен `deliveries_add`
    view през `movement_type` kwarg на два URL-а (`delivery_add`/
    `writeoff_add`); Edit е един и същ URL за двата типа (формата се
    избира от `delivery.movement_type` на инстанцията).
  - Nav менюто вече "Движение на стоки" с отделни "Доставка"/"Изписване"
    линкове (по изрична молба на потребителя).
  - Визуално разграничение: червена лява рамка + "−" пред заглавието
    (`.delivery-form--writeoff`), Qty/Line Total колоните в грида (и в
    add/edit, и в read-only delivery_details) с червен "-" префикс —
    НЕ Unit Price/Sell Price (те са цена за бройка, не сума за изваждане;
    хванат и оправен реален бъг по време на тестване, където първата
    версия грешно негираше и Sell Price).
  - `DeliveriesReportView`/`ReportsDashboardView`/`DeliveryListView`
    всички вече explicit филтрират `movement_type=DELIVERY` — иначе
    изписвания биха изтекли в справката за доставки (и биха гръмнали на
    `document_type.name`, който е `None` при изписване).
  - Миграция `0011_deliveryattributes_movement_type_and_more` приложена
    (потвърдено с потребителя преди `migrate`) — добавя поле с default,
    разширява/nullable-ва съществуващи полета, нищо не трие.
  - Справка конкретно за изписвания — отложена изрично от потребителя
    ("после"), не е имплементирана.

**Тествано:**
- Автоматични тестове: 164/164 (`python manage.py test STORA`) — нови
  тестове за stock delta/restore, permissions (вкл. новото Manager-only
  за DocumentType create), DocumentType CRUD, пълен POST цикъл
  create/edit/delete-item с точната hidden-input форма, която JS-ът
  реално праща.
- На живо в браузъра (през временни `_qa_temp_manager*` акаунти, изтрити
  след тестовете, срещу реалния сървър на потребителя — read-only, не е
  пипан процесът): search-to-add продукт, inline редакция на Qty (през
  Tabulator API, еквивалентно на истинска редакция — `cell.setValue()`
  извиква същия код path като реален blur/commit), Remove бутон,
  submit-rebuild на hidden inputs, търсачка за доставчик + избор, пълен
  create flow (реална доставка #5 създадена и изтрita накрая), зареждане
  на съществуващи артикули в grid-а при Edit, no-op resave не дублира
  наличността, баркод сканиране (Enter) намира продукт по баркод, ДДС
  изчисление (12.00 с ДДС ÷ 1.20 = 10.00 без ДДС коректно), Line
  Total→Unit Price backfill (66.00 ÷ 5 = 13.20 коректно), цветово
  оцветяване при промяна на цена (13.20 vs. каталожни 12.00 → червено),
  Sell Price inline-save (потвърдено директно в базата), Expiry Date
  editor, "#" колона + Qty bottomCalc, "+ New document type" линк
  потвърдено ГО НЯМА вече в HTML-а.
  Хванат и оправен на живо реален бъг в процеса: `Suppliers.objects.filter
  (pk='')` хвърля `ValueError` вместо празен queryset, чупило е
  draft-restore/invalid-resubmit пътя с празен supplier — виж CLAUDE.md.
- Девети кръг (изписване): автоматични тестове 184/184 (`python manage.py
  test`) — нови класове `WriteOffStockAdjustmentTests` (наличност пада,
  restore при триене, delta при edit, отрицателна наличност позволена),
  `WriteOffDocumentNumberTests` (авто-номер, поредност, explicit номер не
  се презаписва, обикновена доставка не получава изфабрикуван номер),
  `WriteOffViewTests` (permission, пълен POST cycle), плюс регресионен
  тест в `reports/tests.py` че изписване не се показва в
  `deliveries_report`. На живо в браузъра (през `_qa_temp_manager_wo`,
  изтрит след теста, плюс тестов продукт/доставчик, изтрити накрая): пълен
  keyboard flow (търсене→Enter→Qty→Enter→Unit Price→Enter→submit),
  наличност 20→15 в базата, `document_number` автогенериран коректно,
  "Details for Write-off ID" + "Edit Write-off" хедъри, червена рамка +
  "−" визуализация потвърдени през computed styles, Deliveries Report
  потвърдено НЕ показва изписването. Хванат и оправен на живо реален бъг:
  първата версия на minus-форматера негираше и Sell Price (трябва да
  остане каталожна положителна цена) — разделено на `moneyFormatter`
  (плейн) vs `movementMoneyFormatter` (само Qty/Line Total).

- Десети кръг (Брак + консистентност): потребителят поиска менюто да е на
  английски (за консистентност с останалия UI, който вече е изцяло
  английски) и Брак да се добави по плана. Направено:
  - Меню: "Движение на стоки"/"Доставка"/"Изписване" → "Stock
    Movements"/"Delivery"/"Write-off", плюс нов "Scrap" + "Scrap Reasons".
  - `MOVEMENT_SCRAP` трети movement_type, `supplier` вече nullable (брак
    няма доставчик), нов `ScrapReason` модел (управляем списък, Manager-only,
    като DocumentType). Нови полета на `DeliveryItems`: `source_item`
    (self-FK, `SET_NULL`, за проследимост назад към оригинала доставка) и
    `scrap_reason` (FK).
  - Два варианта въвеждане на един екран: (1) търсачка за конкретна
    заредена партида по продукт (`BatchSearchView`/`batch_search`, само
    `DELIVERY` редове с `expiry_date`) → попълва `source_item`+`expiry_date`
    автоматично; (2) свободно продукт+количество+причина (същата
    `ingredient_search` търсачка), без `source_item`. Нова колона "Scrap
    Reason" в грида (Tabulator `list` editor), само при `is_scrap`.
  - `DeliveryAttributes.OUTGOING_MOVEMENT_TYPES = (WRITE_OFF, SCRAP)` —
    сменено WRITE_OFF-специфичните проверки в `DeliveryItems.save()`/
    `_restore_stock_on_delete`/JS (`isOutgoing`) да ползват тази обща
    константа вместо да проверяват WRITE_OFF изрично.
  - Веднага след показването: потребителят поиска "+ New supplier"/
    "+ New product" шорткътите да ги няма на изписване/брак ("не може да
    изписваш нещо което вече го няма") — `+ New supplier` скрит само за
    WRITE_OFF (SCRAP няма supplier поле въобще), `+ New product` скрит и
    за WRITE_OFF, и за SCRAP.
  - Тествано: 200/200 автоматични теста (нови класове
    `ScrapStockAdjustmentTests`, `ScrapBatchTraceabilityTests`,
    `ScrapReasonCRUDTests`, `BatchSearchViewTests`, `ScrapViewTests` — и
    двата варианта end-to-end, плюс регресия в `reports/tests.py`). На
    живо в браузъра (през `_qa_temp_manager_scrap`, изтрит след теста):
    batch picker намира партида по продукт, избор попълва expiry + source
    batch автоматично, Scrap Reason dropdown (Tabulator `list` editor —
    отвори се само след explicit mousedown+focus dispatch, не само
    `.click()` на клетката), пълен submit → "Details for Scrap ID"
    хедър, `SCRAP-20260910-001` авто-номер, наличност коректно намалена,
    Edit Scrap презарежда съществуващия ред с source_item/reason, обикновена
    доставка/изписване непроменени (регресия проверена визуално).
- Единадесети кръг — две допълнителни молби по грида:
  1. **Enter да напредва по ЦЕЛИЯ ред до края**, не само Qty→Unit Price —
     случай от реалната работа: складовият няма цената с ДДС на
     фактурата (само без ДДС), иска Enter от Qty да кацне на Unit Price,
     после БЕЗ да го пипа пак Enter да го прехвърли на полето без ДДС,
     попълва го, после Enter директно на Expiry Date (ако Line
     Total/Sell Price/Markup % са скрити през Columns ▾). Hairy бъг,
     открит по време на тестване: първоначалната имплементация окачи
     advance-логиката на Tabulator-ското `cellEdited` събитие — работеше
     за Qty→Price (стойността там реално се сменя), но Tabulator **не
     гърми `cellEdited`, ако commit-натата стойност е същата като преди**
     — точно случаят "Enter без да пипаш полето", т.е. именно сценарият
     който потребителят описа. Фикс: `advanceToNext(cell)` се вика
     директно от Enter keydown handler-а на `numberEditor`/`dateEditor`
     (не от `cellEdited`), `nextVisibleField()` обхожда
     `table.getColumns()` (текущ визуален ред + видимост), прескача
     скрити и нередактируеми колони (`total_without_vat` няма editor,
     винаги се прескача). Тествано на живо стъпка по стъпка с реални
     Enter dispatch-и (не `setValue()`) — пълната верига Qty→Price→Line
     Total→Sell Price→Markup%→Expiry Date потвърдена, плюс конкретния
     "скрий Line Total/Sell Price/Markup%, покажи без-ДДС" сценарий на
     потребителя потвърден дума по дума.
  2. **Местене на колони чрез влачене** (`movableColumns: true` в
     `initExcelStyleTable`, `static/js/data-table.js`) — важи за всички
     таблици в проекта наведнъж (споделен helper), не само за deliveries
     грида. Не persist-ва между reload (само текущата сесия на
     страницата).
  200/200 автоматични теста (без промяна — тази задача е чисто JS, не
  пипа Python/модели).

**Отворени въпроси / чака потребителя:**
- "+ New product" от доставката не auto-инжектира новия продукт обратно в
  grid-а (изисква по-сложна popup+postMessage поддръжка на
  `ProductCreateView`, което никой друг Create view в проекта няма засега)
  — работи, но изисква ръчно търсене след създаване. Кажи ако искаш пълна
  auto-inject версия.
- Баркод скенер тест — чака физическия скенер на потребителя.
- Deliveries LIST страницата (не items-грида вътре в една доставка) остава
  plain HTML table — Tabulator-ификацията й е в Фаза 1.5 на ROADMAP,
  отделно от днешната работа.

**Следваща стъпка (предложение):**
- Комит на Stock Movements (изписване + брак, девети+десети кръг), после
  `sales` одит (следващ неотметнат app във Фаза 1 на ROADMAP). Потребителят
  попита и как работи expiry tracker-ът — обяснено в чата, няма код промяна
  от това (виж CLAUDE.md ако питане се повтори).

---

## 2026-09-09

**Статус:** Некомитнато е последното (данъчни групи/ДДС + детайлна страница
+ история на промените) — виж по-долу; всичко преди него е комитнато на
`main` (виж `git log`).

**Какво стана тази сесия:**
- Добавени тестове за продукти-рецепти (модел, продажбена логика, backfill
  задача) — feature-ът беше имплементиран в предходна сесия, тук се покри с
  тестове и се провери на живо в браузъра.
- Заедно с това се откри и оправи реален (пред-съществуващ) бъг:
  `task.delay(...)` е чупел всяка продажба/запис с 500 грешка, когато Redis
  не тече — виж `dispatch_task` в `core/utils.py`.
- Ingredient picker за рецепти: search+филтър по категория вместо `<select>`
  с всички продукти (не мащабира); жива себестойност/марж докато пишеш.
- "Last Delivery Price" за recipe продукт вече се смята автоматично от
  съставките (не се пише ръчно), плюс Markup % поле до Selling Price
  (двупосочно с цената).
- "+ New category"/"+ New supplier" вече отварят popup таб и връщат новата
  стойност обратно без презареждане (bug fix — преди трябваше ръчен reload).
- Два bug fix-а, докладвани директно от потребителя: категория погрешно
  беше задължителна в браузъра (тихо блокираше Save без грешка), и празен
  "alternate supplier" ред погрешно искаше стойност заради JS-managed
  `position` полето.
- Нова концепция: "везнени" баркодове (13 цифри с вградено тегло/бройка) —
  разпознаване на екрана за продажби. Виж `CLAUDE.md` за пълния формат.
- Данъчни групи (`TaxGroup`, управляем списък) + "Prices" секция в
  create/edit на продукт с чекбокс "Show prices without VAT too" —
  двупосочно изчисление с/без ДДС за delivery_price и sell_price (UI-only,
  нищо ново не се пази в базата освен `Product.tax_group`). Виж CLAUDE.md.
- Детайлната страница на продукт: обсъдихме дали изобщо трябва да я има —
  решихме да остане (Cashier роля вижда, но не редактира — нужна е
  read-only страница, иначе 403 на всеки клик). Добавени: цена без ДДС в
  скоби до Delivery/Selling Price, Markup %, Tax Group ред, мини-списък
  "Recent Activity" (последни 5 продажби/доставки) с линк към пълната
  история.
- Нова функция, поискана директно от потребителя: **история на промените
  по продукт** (`ProductChangeLog`) — кой е сменил име/цена/категория/и
  т.н. и кога, показва се като втора таблица на пълната History страница.
  Виж CLAUDE.md за обхват/ограничения (bulk действията в грида не се
  хващат).

**Тествано:**
- Автоматични тестове: 142/142 (`python manage.py test`).
- На живо в браузъра (през временен `_qa_temp_manager` акаунт, изтрит след
  тестовете): recipe create/edit flow, ingredient picker, markup
  калкулатор, category/supplier/tax-group popup, везнени баркодове (теглови
  и бройков продукт, грешна контролна цифра, нов ред), ДДС двупосочно
  изчисление (delivery и sell price, в двете посоки, плюс live recalculation
  при смяна на данъчна група), детайлна страница (ДДС/markup/tax group
  показани коректно), история на промените (редакция на име+цена, вижда се
  коректно в History таблицата с автора).

**Отворени въпроси / чака потребителя:**
- Потребителят изрично отложи за по-късно: (1) линк от Deliveries към
  `product_create` за бързо добавяне на нов продукт от приемчика, (2)
  справките да смятат цена без ДДС on-the-fly от `tax_group.rate`. И двете
  са записани в `ROADMAP.md`, не чакат отговор — просто не са свършени още.

**Следваща стъпка (предложение):**
- Комит на данъчните групи/ДДС + детайлна страница + история на промените
  (виж git status), после продължаване по `ROADMAP.md` Фаза 1 —
  `deliveries` и `sales` одит, или Фаза 1.5 (Tabulator таблици за
  Suppliers/Categories/Employees/Sales/Deliveries/Tax Groups).

---

<!--
Шаблон за нов запис по-долу — копирай, попълни, сложи най-отгоре:

## YYYY-MM-DD

**Статус:** [комитнато / некомитнато, защо]

**Какво стана тази сесия:**
-

**Тествано:**
- Автоматични тестове: X/X
- На живо в браузъра: ...

**Отворени въпроси / чака потребителя:**
-

**Следваща стъпка (предложение):**
-
-->
