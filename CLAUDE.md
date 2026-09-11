# CLAUDE.md — Правила за работа по проекта STORA

Този файл се чете автоматично от Claude Code при всяка сесия в тази директория.
Съдържа трайни правила за начина на работа. За текущия план със задачи виж
`ROADMAP.md`; за това докъде точно е стигнала последната сесия (какво е
тествано, какво чака отговор от потребителя) виж `SESSION.md` — обновявай го
в края на всяка сесия.

## За проекта

Django/PostgreSQL уеб приложение за управление на магазин — инвентар, доставки,
продажби, справки. Първоначално изпитен проект в СофтУни, сега се разработва
за реална употреба в физически обект(и). Собственикът (потребителят) учи
Python/Django активно — приоритет е кодът да остане разбираем и обясним,
не просто работещ.

Apps: `accounts`, `core`, `products`, `deliveries`, `sales`, `reports`.

## Основно правило: обяснявай, преди да пишеш код

Потребителят учи в процеса. При всяка промяна:
1. Кажи какъв е проблемът и защо се случва.
2. Предложи фикс с кратко обяснение защо точно така.
3. Направи промяната.
4. Обобщи какво се промени, на прост език.

Не пренаписвай мълчаливо голям блок код "защото е по-добре". Ако видиш
възможност за по-мащабна промяна извън текущата задача, спомени я отделно
и попитай, не я прави автоматично.

## Работен процес по модул

За всеки app вървим през фиксиран цикъл (виж ROADMAP.md за реда):
1. Прочети целия app (models, forms, views, templates, tests).
2. Провери за бъгове: логически грешки, race conditions, липсваща валидация,
   N+1 заявки, несъответствия между модел и форма/темплейт.
3. За всеки намерен бъг: обясни, поправи, обнови/добави тест, пусни тестовете.
4. Докладвай статус: какво е оправено, какво остава, какво не си сигурен
   дали е бъг или нарочно поведение (виж "Известни бизнес правила" по-долу).
5. Едва след потвърждение от потребителя минаваме към оптимизация на модула.
6. Едва след това преминаваме към следващия app.

## Известни бизнес правила (НЕ ги "поправяй")

- **Отрицателна наличност е позволена нарочно.** Ако стока е физически
  заредена в магазина, но доставката още не е приключена в системата,
  трябва да може да се продава — количеството на продукта временно става
  отрицателно. Целта занапред е сигнал към мениджъра, не блокиране на
  продажбата. Не добавяй валидация, която пречи на отрицателно количество,
  освен ако потребителят изрично не поиска това да се промени.
- **Продукти могат да са "бройкови" или "теглови" (`Product.unit_type`).**
  `quantity` навсякъде (Product, SaleItems.sale_quantity,
  DeliveryItems.delivery_quantity) е `DecimalField(max_digits=10,
  decimal_places=3)`, не Integer — теглови продукти (`unit_type='weight'`)
  пазят реално дробни стойности (кг до грам, 0.350 кг). Бройковите продукти
  просто винаги пазят цяло число в същото поле (5.000). Не връщай тези
  полета обратно на Integer.
- **Продукт може да е "рецепта" (`Product.is_recipe` + `RecipeIngredient`
  модел).** Направен е от други (само нерецептурни — без вложени рецепти)
  продукти вместо от собствена наличност — напр. Капучино = 0.050кг Мляко +
  0.007кг Кафе. Продажба на рецептурен продукт **не пипа неговото собствено
  `quantity`**, вместо това приспада от всяка съставка, мащабирано по
  продадено количество (`adjust_stock_for_sale` в `sales/models.py`, вика се
  и при редакция на количество в продажба, и обратно при триене на
  sale item). Ако продукт вече е имал продажби ПРЕДИ да стане рецепта,
  маркирането му като рецепта пуска еднократна фонова Celery задача
  (`backfill_recipe_ingredient_stock`), която ретроактивно приспада
  съставките за всички минали продажби на този продукт — пази се от
  повторно изпълнение през `Product.ingredients_backfilled_at` (веднъж
  зададено, задачата излиза веднага при следващо повикване). Не трий тази
  guard проверка — без нея редакция на рецепта би удвоила приспадането.
- **"Везнени" баркодове (scale barcodes) — 13 цифри, кодират количество
  директно в баркода.** Печатат се от везна: `<7 или 8-цифрен код на
  продукта><5 или 4-цифрено количество><1 контролна цифра (EAN-13
  checksum)>` = 13 цифри общо. Количеството е грамове за теглови продукт
  (превръща се в кг, ÷1000) или бройки директно за бройков — според
  `Product.unit_type` на съвпадналия продукт. Кой продукт отговаря на кой
  префикс **не може да се извади само от цифрите** (7+5 и 8+4 разфасовки
  дават различен резултат от едни и същи 13 цифри) — затова префиксът се
  регистрира изрично като `Barcode` ред с `is_scale_code=True` (чекбокс в
  баркод formset-а на продукта; `Barcode.code` за такъв ред пази само
  префикса, не пълен баркод). Логиката за декодиране е в
  `STORA/products/scale_barcode.py` (`decode_scale_barcode`,
  `is_valid_ean13`) — огледална JS версия живее в
  `sales/templates/sales/sale_add.html` (декодирането е изцяло client-side,
  като `findProductByCode`, без extra заявка към сървъра при всяко
  сканиране). Разпознато е засега само на екрана за продажби, не и при
  въвеждане на продукт извън чекбокса за маркиране.
- **Данъчни групи (`TaxGroup`) + цени с/без ДДС — `Product.delivery_price`/
  `sell_price` си остават с ДДС (gross), "без ДДС" НЕ се пази в базата.**
  `TaxGroup` е управляем списък (име + `rate` %), като Category/Suppliers —
  не фиксиран набор от 20%/9%/0%. `Product.tax_group` е nullable FK
  (`SET_NULL`, изтриване на данъчна група просто обезличава продукта, не
  блокира). В create/edit на продукт има "Prices" секция с чекбокс "Show
  prices without VAT too" — скрива/показва по 2 допълнителни UI-only полета
  (delivery/sell без ДДС), изчислени живо чрез JS и данъчния % на
  избраната група (`tax_group_rates` json_script blob в контекста,
  огледален на `products_data`/`barcodes_data` патърна). Двупосочно: пишеш
  без ДДС → смята с ДДС (и обратно), досущ като Markup % ↔ Selling Price
  патърна. Справките ще смятат "без ДДС" on-the-fly от `tax_group.rate`,
  когато им дойде редът — не е имплементирано още.
- **История на промените по продукт (`ProductChangeLog`) — по един ред за
  всяко реално сменено поле, не snapshot на целия продукт.** Следи се:
  `internal_code`, `name`, `unit_type`, `delivery_price`, `sell_price`,
  `category`, `tax_group`, `is_recipe` — умишлено НЕ `quantity` (тя си има
  собствена история през продажби/доставки, виж `ProductHistoryView`).
  Пълни се автоматично през `pre_save`/`post_save` сигнали на `Product` в
  `products/models.py` (сравнява старата стойност от базата с новата) —
  хваща едновременно редакции през Edit формата и inline редакцията в
  Products грида, без да пипаш всеки view поотделно. **Кой е направил
  промяната сигналът не знае сам** — `ProductUpdateView`/
  `ProductInlineUpdateView` слагат `form.instance._changed_by =
  request.user` точно преди `form.save()`; ако добавиш нов view, който
  записва Product директно, направи същото, иначе `changed_by` ще излезе
  празен. **Bulk действията в Products грида (`ProductBulkActionView`)
  НЕ се хващат** — ползват `QuerySet.update()`, което заобикаля Django
  сигналите изцяло; известно ограничение, не е бъг. Показва се като втора
  таблица на пълната History страница (`product_history.html`), филтрирана
  по същия период като продажби/доставки.

- **Доставки (`DeliveryItems`) наличността се смята delta-based, огледално
  на `sales/models.py::SaleItems`.** `save()` лочва продукта с
  `select_for_update()`, сравнява `self.delivery_quantity` с предишната
  стойност от базата (ако редът вече съществува) и прилага само РАЗЛИКАТА
  към `Product.quantity` — НЕ цялото ново количество при всяко save (иначе
  редакция на съществуващ ред дублира наличността). `post_delete` сигнал
  връща наличността назад, ако ред/цяла доставка се изтрие (CASCADE-ва през
  всеки `DeliveryItems`). `DeliveryAttributes.total_amount` се преизчислява
  автоматично през `post_save` сигнал на `DeliveryItems`
  (`recalculate_total()`), не ръчно в `save()`.
- **Типове документи за доставка (`DeliveryAttributes.document_type`) —
  управляем списък (`DocumentType` модел), не фиксирани Python `choices`.**
  Същия patтern като Category/TaxGroup — CRUD екран
  (`deliveries/document-types/`), но **само Manager** може да добавя/редактира
  (`add_documenttype`/`change_documenttype` НЕ са в Warehouse group-а
  нарочно — списъкът е кратък и рядко се сменя; Warehouse само избира от
  него). Няма "+ New document type" shortcut popup от формата за доставка
  (изрично поискано от потребителя да се маха) — управлява се само през
  собствената си страница. Мигрирано от старото `CharField(choices=[...])`
  през add-populate-swap миграционна серия (виж
  `deliveries/migrations/0004-0008`) — старите `'INVOICE'`/`'DELIVERY_NOTE'`
  стойности са преместени към `DocumentType` редове с
  `name='Invoice'`/`'Delivery Note'`.
- **Таблицата с артикули в доставка (`_delivery_items_table.html`) е
  Tabulator, не plain HTML table + Django formset rows** — sort, Excel-style
  филтър, column chooser, export to Excel, ред с пореден номер (`#`,
  built-in `formatter: 'rownum'`). Визуалната таблица е 100% JS state, с
  ред за търсене отгоре (reuse на `products.ingredient_search` endpoint —
  trigram search, вече изключва `is_recipe` продукти, което се оказва точно
  правилно и за доставки, тъй като рецептурен продукт никога не се доставя
  directly), с пълна клавиатурна навигация — пишеш, стрелки надолу/нагоре
  местят маркировката между резултатите (първият е авто-маркиран щом
  списъкът се появи), Enter добавя маркирания в грида. Ако dropdown-ът е
  затворен/празен, Enter пада обратно на точен код/баркод match (за скенер
  convenience, работи и с реален баркод — везнените 13-цифрени баркодове с
  вградено тегло още НЕ се декодират тук, чака физически скенер за тест, за
  разлика от Sales екрана, където вече работи). **Капан:** резултатите от
  `ingredient_search` НЕ носят `sell_price`/`tax_group__rate` (ендпойнтът е
  правен за recipe picker-а, който не ги ползва) — `addProductRow()` затова
  винаги си донаписва пълния продукт от `productsById` (страницата's own
  `products_data` dump) по id, преди да построи реда, вместо да се
  доверява директно на каквото dropdown/баркод match-ът е подал.
  **Колоните Qty/Unit Price/Unit Price (no VAT)/Line Total/Sell
  Price/Markup %/Expiry Date всички са editable**, с автоматичен фокус flow
  — добавяш продукт → Qty полето влиза в edit режим директно, след
  въвеждане на количество и Enter автоматично преминава към Unit Price.
  **Два свързани капана, открити живо от потребителя (изглеждаше
  фокусирано, но реално не приемаше писане; после Enter презареждаше
  цялата страница):**
  1. Голото `cell.edit(true)` рендира `<input>`-а в DOM-а, но не винаги
     реално го фокусира — особено когато клетката, върху която се вика, е
     "стара" референция, хваната ПРЕДИ `row.update()` (recalcRow) да е
     предизвикал Tabulator да я преизрисува, или когато се вика синхронно
     отвътре в ЧУЖД `cellEdited` handler (Tabulator още не е приключил
     вътрешния cleanup на предишната клетка). Фикс: `focusCellEditor(row,
     field)` helper — вика `setTimeout(..., 0)` (изчаква Tabulator да си
     довърши текущия redraw), после ПРЕСЪздава клетката свежо през
     `row.getCell(field)` (не разчита на стара `cell` референция), вика
     `.edit(true)`, после explicit намира `<input>`-а и вика `.focus()` (+
     `.select()`) върху него ръчно.
  2. **Вграденият `editor: 'number'` на Tabulator изобщо няма Enter-key
     логика** (потвърдено срещу vendored build-а — keybinding таблицата му
     мапва само Tab/Shift-Tab/стрелки, никога Enter/13) — нито commit,
     нито navigation. Затова Enter в число-поле просто си минава през
     browser-a необработен и си остава ЕДИНСТВЕНИЯТ handler: браузърният
     "Enter в поле вътре в `<form>` праща формата" — цялата доставка
     реално се submit-ва по средата на въвеждане, страницата презарежда.
     Фикс, на два пласта: (a) delegated `keydown` listener на цялата
     `.delivery-form__items` секция, `e.preventDefault()` за Enter във
     всеки `<input>`/`<textarea>` вътре — блокира implicit submit
     безусловно, като safety net; (b) собствен `numberEditor(cell,
     onRendered, success, cancel, editorParams)` за ВСИЧКИ число-колони
     (замества голото `editor: 'number'`) — explicit `keydown` handler
     вътре в него, Enter вика `success(value)` директно (документираният
     Tabulator API за коммит на custom editor), Escape вика `cancel()`;
     вече не разчита на недокументираното вътрешно blur/Enter поведение
     на вградения editor изобщо.
  3. **Enter напредва по ЦЕЛИЯ ред до края, не само Qty→Unit Price**
     (добавено по изрична молба — складовият няма цената с ДДС на
     фактурата, само без ДДС, и иска Enter да го прекара направо до
     следващото поле, което МУ трябва, прескачайки скритите междинни
     колони). `nextVisibleField(currentField)` обхожда `table.getColumns()`
     (текущия визуален ред на колоните — вижда drag-преместване и
     Columns ▾ show/hide автоматично, без отделен списък за
     поддържане) и връща следващото поле, което има `editor` И е
     `isVisible()`. **Капан, открит на живо:** логиката първоначално
     живееше в `table.on('cellEdited', ...)` — изглеждаше да работи за
     Qty→Price (стойността там винаги реално се сменя), но Tabulator
     **изобщо не гърми `cellEdited`, ако commit-натата стойност е същата
     като преди** (напр. Enter върху Unit Price без да го пипнеш, защото
     искаш направо да отидеш на полето без ДДС) — Enter в такъв случай
     тихо не правеше нищо. Фикс: `advanceToNext(cell)` се вика директно
     от Enter-handler-а на `numberEditor`/`dateEditor` (след `commit()`),
     не от `cellEdited` — `cellEdited` пазим само за recalc/persist
     страничните ефекти (`recalcRow`/`persistSellPrice`), които реално
     имат смисъл само при истинска промяна. Вграденият Tabulator `'list'`
     editor (Scrap Reason) не минава през тази верига — приема се, защото
     той е последна колона в реда навсякъде, където се използва.
  Пет полета са свързани двупосочно през общата
  `recalcRow(row, source)` функция (JS "source of truth" патърн — кое поле
  току-що е пипнато решава кои други да се преизчислят, за да няма
  безкраен цикъл): Unit Price ↔ Unit Price без ДДС (по `Product.tax_group`
  на реда, ако продуктът няма данъчна група — "без ДДС" просто отразява
  "с ДДС"), Sell Price ↔ Markup % (спрямо Unit Price), и **Line Total →
  Unit Price** (въвеждаш общата сума от фактурата за известно количество,
  системата смята обратно единичната цена — не само Qty×Price→Total в
  другата посока). Unit Price клетката светва в червено, ако е различна от
  текущата `Product.delivery_price` в системата (по-скъпо) или зелено
  (по-евтино) — сравнение спрямо каталожната цена в момента на зареждане на
  реда, не спрямо предишна доставка. **Sell Price/Markup % се записват
  веднага в `Product.sell_price`** през същия `product_inline_update`
  endpoint, който ползва "Enable Edit" в Products грида — независимо дали
  цялата доставка изобщо ще бъде submit-ната; при грешка от сървъра клетката
  се връща на старата стойност. `Expiry Date` е custom Tabulator editor
  (`<input type="date">`, няма вграден "date" editor в тази версия на
  Tabulator) — пази се на `DeliveryItems.expiry_date` (per ред, не per
  продукт — една доставка може да съдържа партиди с различен срок).

  `DeliveryItemForm`-ите са изцяло `HiddenInput` полета — нищо от тях не се
  рендира директно; JS пресъздава `items-<n>-<field>` hidden inputs точно
  преди истинския form submit (`rebuildHiddenInputs()`), базирано на
  текущото Tabulator state (`table.getData()`). Само реалните
  `DeliveryItems` полета минават през този rebuild
  (`delivery_item`/`delivery_quantity`/`price_at_delivery`/
  `total_price_row`/`expiry_date`) — Sell Price/Markup %/без-ДДС колоните
  НЕ са модели полета на `DeliveryItems`, не се пращат тук. Съществуващ ред,
  който потребителят маха от грида, **не просто се маха** — пази се в
  `pendingDeletions` и се препраща с `DELETE=on` + непроменените му стари
  стойности (Django формсет иначе никога не разбира, че трябва да го
  изтрие, а и валидацията на "delete"-ran форма пак изисква валидни
  required полета). Нов (никога незапазен) ред при махане просто се
  премахва напълно — няма нищо за триене в базата.
- **`delivery_details.html` (страницата "Details for Delivery") показва
  същите колони като editable грида на Add/Edit, но read-only.**
  `DeliveryDetailView._item_row()` смята същата без-ДДС/markup математика
  (по продуктовата `tax_group` на реда), само formatters, без editors —
  никакъв `numberEditor`/`dateEditor`, никакъв hidden-input rebuild (тази
  страница не submit-ва форма). **Внимание:** Sell Price/Markup % тук са
  ЖИВ snapshot на текущата `Product.sell_price` в момента на разглеждане
  на страницата, не историческата цена от момента на самата доставка —
  ако продажната цена е сменена след доставката (напр. през Products
  грида), тази страница показва новата, не старата.
- **Доставчик на формата за доставка е търсачка (`supplier_search`
  endpoint в products/views.py), не `<select>`.** Същия trigram-search
  patтern като ingredient/tax-group picker-ите; скрито поле (`HiddenInput`)
  пази реалния pk, видимото поле показва името. Попълва се или от
  `form.instance.supplier.name` (edit), или от сървърно изчислен
  `selected_supplier_name` (add-страницата, където няма `instance` —
  трябва explicit `Suppliers.objects.filter(pk=...)` lookup; **пази се** да
  не подадеш празен string като pk — `.filter(pk='')` хвърля `ValueError`,
  не връща празен queryset, затова винаги се проверява `if posted_id else
  None` преди lookup-а).

- **"Stock Movements" (менюто е на английски, за консистентност с
  останалия UI — само тук в чата пазим решението защо) —
  `DeliveryAttributes.movement_type` (DELIVERY / WRITE_OFF / SCRAP) вместо
  отделен app.** Изписването е същият модел/таблица като доставка, само
  посоката на наличността е
  обратна — не е предефинирана `DocumentType`-подобна управляема стойност,
  а фиксирани Python choices, защото директно определя знака в
  `DeliveryItems.save()`/`_restore_stock_on_delete` (нов `movement_type`
  би изисквал код промяна и без друго). Продавачът/складовият винаги
  въвежда ПОЛОЖИТЕЛНО количество в грида — знакът се решава само вътрешно
  според `self.delivery.movement_type`, никога не се пита потребителя за
  отрицателно число. `document_type` вече е nullable (изписването няма
  входящ документ за класифициране), `document_number` е разширено на 20
  символа и по избор — ако е празно при WRITE_OFF/SCRAP,
  `DeliveryAttributes.save()` генерира вътрешен номер сам
  (`_generate_internal_document_number`, формат `WO-20260910-001`,
  поредност по `movement_type`+`document_date` за деня; не е защитено от
  race condition при две едновременни изписвания — приемливо за чисто
  показен вътрешен номер). Отделна `WriteOffForm` (без `document_type`,
  `document_number` optional, `document_date` auto = днес) вместо да се
  претоварва `DeliveryForms` с условна логика. Add-екранът е един и същ
  view (`deliveries_add(request, movement_type=...)`) с два URL-а
  (`delivery_add`/`writeoff_add`), избран чрез `path()`-extra-kwargs, а не
  два отделни view-а — Edit пък изобщо няма нужда от втори URL, защото
  `delivery.movement_type` вече е на инстанцията, само формата се сменя.
  Draft-резюмето (session key `cashier_last_operation`) е СПОДЕЛЕН слот
  между sale/delivery/write_off (`get_cashier_operation_type(path)` в
  `core/utils.py`) — само ЕДНА незавършена операция наведнъж, нарочно
  (складов/касиер работи по една операция) — `delivery_draft_save`
  затова взима `path` от JS payload-а (`window.location.pathname`), не
  го хардкоди, иначе чернова от екрана за изписване би се записала като
  тип "delivery". Визуално разграничение (по молба на потребителя, за да
  не се бърка с доставка): `.delivery-form--writeoff` CSS клас (червена
  лява рамка + "−" пред заглавието), плюс Qty/Line Total колоните в грида
  (и в `_delivery_items_table.html`, и в read-only `delivery_details.html`)
  се показват с червен "-" префикс — **но НЕ Unit Price/Unit Price без
  ДДС/Sell Price** (`moneyFormatter` е плейн, отделна
  `movementMoneyFormatter` носи минуса) — те са цена за бройка/каталожна
  цена, не сума, която се изважда, знакът там би бил подвеждащ. Справките
  (`DeliveriesReportView`, `ReportsDashboardView`, `DeliveryListView`)
  всички explicit филтрират `movement_type=DELIVERY` — иначе изписване би
  се появило в справка за доставки (и `document_type.name` би гръмнало,
  защото е `None` за изписване). Справка конкретно за изписвания е
  отложена по молба на потребителя ("после"), не е имплементирана.
  `DeliveryAttributes.OUTGOING_MOVEMENT_TYPES = (WRITE_OFF, SCRAP)` е
  споделената константа, която `DeliveryItems.save()`/
  `_restore_stock_on_delete` и JS-ът (`isOutgoing` в
  `_delivery_items_table.html`/`delivery_details.html`) ползват вместо да
  проверяват WRITE_OFF изрично — добавяш нов "излизащ" тип движение само
  на едно място.
  **Нито изписване, нито брак показват "+ New supplier"/"+ New product"**
  шорткътите от формата (по изрична молба на потребителя: "не може да
  изписваш нещо което вече го няма") — и двете движения само СМЪКВАТ
  наличност от вече съществуващи продукти/доставчици, никога не създават
  нов ресурс в движение. `+ New supplier` е скрит за WRITE_OFF (`{% if not
  is_write_off %}` около линка, не около цялото supplier поле — самото
  търсене на съществуващ доставчик си остава); SCRAP изобщо няма supplier
  поле, така че въпросът не стои. `+ New product` е скрит и за WRITE_OFF,
  и за SCRAP (`{% if movement_type == 'DELIVERY' %}`) — видим само при
  истинска доставка.
  **Брак (`MOVEMENT_SCRAP`) — трети movement_type, без доставчик
  (`supplier` вече nullable), управляем списък причини `ScrapReason`
  (Manager-only add/change, като DocumentType).** Document number auto-gen
  (`_generate_internal_document_number`) вече поддържа и `SCRAP-...`
  префикс (общата логика е преизползвана, само `prefix` клонът). Два
  начина да добавиш ред в грида, вместо един:
  1. **Вариант 1 — избор на конкретна партида.** Отделна търсачка над
     таблицата (`#delivery-batch-search-input`, видима само когато
     `is_scrap`), ползва нов `BatchSearchView`/`batch_search` endpoint —
     търси в `DeliveryItems` с `delivery__movement_type=DELIVERY` И
     `expiry_date__isnull=False` (написано, все още неизтекли доставки с
     партида, не изписвания/други бракове — бракуване на брак няма смисъл).
     Избор попълва `source_item` (self-FK на `DeliveryItems`, `SET_NULL`)
     сочещ назад към оригиналния delivery ред, плюс `expiry_date` копирано
     от там — `DeliveryItems.scrapped_as` е reverse accessor-ът
     (`related_name`).
  2. **Вариант 2 — свободно въвеждане.** Същата продуктова търсачка
     (`ingredient_search`) като при доставка/изписване — за брак преди
     изтичане на срок, когато няма смисъл/желание да се сочи конкретна
     партида. `source_item` остава празно.
  И двата варианта водят в един и същ Tabulator ред; `Scrap Reason`
  е допълнителна колона (само при `is_scrap`), Tabulator `editor: 'list'`
  с `values: scrapReasonValues` ({id: name}) — клетъчната стойност е pk-то
  директно (за разлика от Category inline-edit patтern-а, който е по
  ИМЕ — тук не е нужно, защото `scrap_reason` отива направо в
  `DeliveryItemForm`, не през отделен product-inline-update endpoint).

- **`Category.parent` — self-FK, подкатегории с произволна дълбочина** (не
  ограничено до 2 нива), подготовка за предстоящия редизайн на касиерския
  интерфейс (POS бутони групирани по категория/подкатегория). `on_delete=
  SET_NULL` като `Product.category` — триене на родителска категория само
  обезродителява подкатегориите, не ги трие с нея. Защита от цикъл
  (категория да стане дете на собствен наследник) е изцяло през
  queryset exclusion в `CategoryForm.__init__` — `Category.
  get_descendant_ids()` (BFS по `parent_id`) маха себе си + всички
  наследници от избора на родител при edit, вместо отделен `clean_parent`
  walk; невалидна стойност (ако някой директно пусне POST с изключен pk)
  просто пропада на Django ModelChoiceField-овата "not a valid choice"
  проверка автоматично. `category_list.html` показва родителя по име
  (plain таблица, все още не Tabulator — тази Tabulator-ификация е
  отделна Фаза 1.5 задача). Product-ът си остава с `category` FK към
  произволно ниво от йерархията (не само листови категории) — нарочно
  не е ограничено.
- **Касиерски екран (Фаза 2, разработва се на етапи, `sales/sale_add.html`)
  — вдъхновен от реален касов софтуер (screenshot от потребителя), не от
  нулата.** Обхват, изрично потвърден с потребителя: количка + категории +
  checkout стъпка. НЕ влиза в тази задача (отложено): клиенти/баланс/
  бонус точки, множество едновременни отворени кошници (табовете 1/2/3 на
  референтния скрийншот) — само ЕДНА активна продажба наведнъж, старият
  draft-save механизъм си остава непроменен.
  - Ред на изграждане (нарочно на стъпки, всяка тествана отделно, преди
    следващата): (1) данни — `SaleAttributes.payment_method`/`amount_paid`/
    `change_due` (nullable, за да не строши нищо преди checkout модала да
    съществува реално), `products_data` вече носи `category_id`,
    ново `categories_data` (id/name/parent_id) в контекста; (2) дясната
    лента с категории/подкатегории — направено; (3) checkout модал —
    предстои; (4) количката на Tabulator — предстои, най-рисковата стъпка
    (замества работещ, тестван код), изрично поискано отделно "давай".
  - Категорийният панел показва **едновременно** подкатегориите (папки за
    drill-down) И директно assign-натите продукти на текущото ниво (не само
    на leaf ниво) — същата логика като Product.category да сочи произволно
    ниво. Клик на продукт-бутон = добавя в количката; ако продуктът вече е
    в количката, **увеличава количеството с 1** вместо да дублира ред
    (същото поведение като сканиране на баркод втори път).
  - Жив часовник (`setInterval` всяка секунда) до името на оператора,
    горе вляво — по изричен избор на потребителя (не статично време от
    зареждането).
  - Методи на плащане, потвърдени с потребителя: само `CASH`/`CARD` —
    НЕ ваучери (референтният софтуер ги има, но магазинът не ги ползва).
    `amount_paid`/`change_due` се пазят и за двата метода (за CARD просто
    paid=total, change=0) — за да не се налага branch по метод в бъдещи
    справки.

(Добавяй нови правила тук, когато изникнат в разговор с потребителя, за да
не се преоткриват на всяка сесия.)

## Технически капани (открити наскоро, лесно се повтарят)

- **Django `{# ... #}` темплейт коментар, разпънат на няколко реда, НЕ се
  маха коректно — изтича като буквален текст в страницата.** Открито в
  `sales/sale_add.html`: многоредов `{# Checkout modal -- ... #}` коментар
  над checkout модала се показа буквално в рендернатата страница, и понеже
  съдържаше `<form>` вътре в текста си, браузърът го парсна като истински
  (невидим) `<form>` таг — създаде втори, объркващ form елемент в DOM-а,
  който чупеше `document.querySelector('.sale-form__form')` другаде в
  скрипта (виждаше се като загадъчна `Cannot read properties of null`
  грешка в конзолата, без ясна връзка с реалната причина). Фикс: `{%
  comment %}...{% endcomment %}` вместо `{# #}` за всеки коментар, който
  минава на повече от един ред — `{# #}` е безопасен само на един ред.

- **HTML5 `<input type="number">` `step` валидацията е относителна спрямо
  `min`, не спрямо 0.** `min="0.001" step="1"` прави 1, 2, 3... ВСИЧКИ
  невалидни (само 0.001, 1.001, 2.001... минават) — браузърът тихо блокира
  submit с "Please enter a valid value. The two nearest valid values are
  0.001 and 1.001", без JS грешка, без Django да види заявката изобщо
  (`form.checkValidity()` връща `false` преди мрежовата заявка въобще да
  тръгне). Хванато в `sales/forms.py::SaleItemForm` — default widget-ът за
  `sale_quantity` трябваше `min="1" step="1"` (нормалния "бройков продукт"
  случай), не `min="0.001"` — грешка направена докато се оправяше точно
  обратния бъг (теглови продукти да приемат дробно количество). JS-ът
  (`applyQuantityConstraints()` в `sale_add.html`) after правилно сменя на
  `min="0.001" step="0.001"` САМО когато избраният продукт е
  `unit_type=weight` — редовете, които никога не минават през тази функция
  (напр. празния "extra" ред на Django formset-а, който никой не пипа),
  остават на default-а, затова default-ът трябва да е правилен сам по себе
  си, не само "ще се overwrite-не после". Django test client НЕ хваща този
  клас бъгове изобщо (не изпълнява реална HTML5 constraint validation) —
  нужен е реален браузър тест (`element.checkValidity()`/
  `form.checkValidity()`) всеки път, когато се пипа `min`/`step` на number
  input.

- **`ModelForm.Meta.fields = '__all__'` НЕ изключва M2M поле с `through=`
  автоматично**, въпреки че такова поле не може смислено да се редактира
  през обикновен `<select multiple>`. Django просто го рендира като нормално
  поле. Ако някой `CreateView`/`UpdateView` вика `form.save()` собственоръчно
  И после `super().form_valid(form)` (който ПАК вика `form.save()` през
  `ModelFormMixin`) — вторият `save()` презаписва M2M-то с празна стойност
  (защото формата никога не е получавала стойност за него) и **трие тихо**
  каквото formset-ът току-що е записал. Видяно с `Product.supplier` след
  като мина на `through='ProductSupplier'` — фикс: изричен `fields = [...]`
  списък в `ProductForms.Meta`, БЕЗ M2M-through полето; управлява се изцяло
  през отделен formset (виж `ProductSupplierFormSet`). Ако добавяш нов M2M
  с `through=` някъде другаде в проекта, провери това първо.
- Колона със зададен Tabulator `editor` спира propagation-а на клика дори
  когато `editable()` връща false — вижда се в детайли в раздела за
  "Конвенция за таблици" по-долу (т.8).
- **`STORA/core/` нямаше `__init__.py`** цялата сесия до сега — работеше
  като namespace package (imports вървяха, `python manage.py test` без
  аргументи работеше), но `python manage.py test STORA.core` (изричен
  label) чупеше с `TypeError` при discovery, а `core/tests.py`
  (`IndexPageTests`) така и не се беше изпълнявал реално — хвана се остарял
  тест, който вече не отговаряше на текущото поведение (`index` изисква
  login). Ако добавяш нов Python пакет в проекта, провери за `__init__.py`.
- **`Decimal * float` хвърля `TypeError`** (не автоматично се преобразува,
  за разлика от `int * float`). Откри се, след като `sale_quantity`/
  `delivery_quantity` минаха от Integer на Decimal — `SaleItems.save()`/
  `DeliveryItems.save()` умножават quantity по price директно. През реална
  форма няма проблем (Django коригира до Decimal при `_post_clean()`), но
  ако пишеш тест/скрипт, който създава `SaleItems`/`DeliveryItems`/`Product`
  директно през `.objects.create(...)`, задавай **и двете** страни на
  умножението (quantity и price_at_sale/price_at_delivery) като `Decimal`
  или string, никога bare Python float, за да не гръмне.
- **`SomeTask.delay(...)` гърми синхронно (не тихо), ако Redis не тече** —
  открито на живо в браузъра, не само в тестова среда: `.delay()` първо се
  опитва да отвори връзка към Redis (за resultbackend pub/sub bookkeeping,
  дори когато никой не вика `.get()` на резултата), и това хвърля
  `RuntimeError`/`OperationalError` директно в `form_valid()`/view-a, преди
  въобще да опита да пусне съобщението към broker-а. Ефектът е грозен: заявка
  (напр. запис на продажба или на рецептурен продукт) минава 500 грешка **въпреки
  че данните вече са запазени** в базата (save-ът е преди `.delay()`, няма
  `atomic()` около двете) — потребителят вижда срив и може да опита пак,
  създавайки дубликат. Фикс, приложен навсякъде в проекта:
  1. Всяка задача, чийто резултат никой не чете, взима `@shared_task(ignore_result=True)`
     (виж `sales/tasks.py`) — спира ненужната pub/sub връзка при всяко `.delay()`.
  2. Всяко извикване на `.delay()` минава през `dispatch_task(task, *args,
     **kwargs)` (`core/utils.py`) вместо директно `task.delay(...)` — увива в
     `try/except`, логва warning при грешка, но никога не троши заявката. Ако
     добавяш нова фонова задача някъде, ползвай `dispatch_task`, не голия
     `.delay()`.
  Redis не тече по подразбиране локално (виж "Среда" по-долу) — това не е
  ръбест случай, а нормалното състояние при разработка.

## Глобална търсачка (navbar)

`GlobalSearchView` в `core/views.py` (`/search/?q=...`, JSON) търси
едновременно в Products (име/код/баркод), Suppliers (име/БУЛСТАТ),
Categories (име), Employees (име/username). Използва Postgres `pg_trgm`
(активиран еднократно през `core/migrations/0001_enable_pg_trgm.py` —
изисква DB роля с право да създава extensions) + `GinIndex(...,
opclasses=['gin_trgm_ops'])` на всяко търсено поле във всеки модел — бързо
дори при голяма база, без нов search engine. `TrigramSimilarity` ползва се
за ranking (не само `icontains` match). Ново поле, което искаш търсимо —
добави `GinIndex` в модела му (миграцията му трябва `dependencies` към
`('core', '0001_enable_pg_trgm')`) и добави метод `_search_<нещо>` в
`GlobalSearchView`. UI-ът е в `templates/base.html` (input в navbar-а +
debounce fetch + групиран dropdown) — важи за всяка страница автоматично.

## Конвенция за таблици: сортиране + Excel-style филтри + export

Всяка таблица в проекта (списъци, история и т.н.) трябва да поддържа:
- Сортиране по колона (клик върху header).
- Excel-style филтър по колона (▾ до заглавието → checkbox списък с
  уникалните стойности в тази колона, плюс търсачка отгоре за дълги
  списъци — вградено в `excelStyleFilterEditor`, автоматично за всяка
  колона, нищо допълнително не се пише на ниво темплейт).
- Бутон "Export to Excel".
- Влачене на header, за да разместиш реда на колоните (`movableColumns:
  true` в `initExcelStyleTable`, важи за всички таблици наведнъж, няма
  отделен опция-флаг на ниво темплейт). Tabulator пази новия ред сам в
  паметта за текущата сесия на страницата (не persist-ва между reload) —
  ако някой поиска да се помни между визити, трябва `localStorage`/backend
  persist, не е направено.

Реализирано е с **Tabulator** (чист JS, без jQuery), свален локално в
`static/vendor/tabulator/` (+ `static/vendor/sheetjs/` за Excel export) —
**не през CDN**, защото системата е за реален магазин и не бива да зависи
от външен интернет за да зареди таблица. (Bootstrap в момента се тегли през
CDN в `templates/base.html` — стар избор отпреди тази конвенция, отделен
проблем, не Tabulator-свързан.)

Споделеният helper е в `static/js/data-table.js`
(`initExcelStyleTable(selector, jsonScriptId, columns, options)`). Патърн за
нова таблица (виж `products/templates/products/products_list.html` или
`product_history.html` като еталон):
1. View-то сериализира данните в plain dict-ове (без model инстанции —
   `str()`/`float()` преди сериализация) в context, напр. `products_data`.
2. Темплейтът: `{% block extra_css %}` за `tabulator_bootstrap5.min.css`,
   `{{ products_data|json_script:"products-table-data" }}` +
   `<div id="..." class="table-sm">` (`table-sm` е вграден в bootstrap5
   темата на Tabulator — прави header/редовете компактни; без него header-ът
   излиза неразумно висок), `{% block extra_js %}` зарежда
   `xlsx.full.min.js` → `tabulator.min.js` → `{% static_v 'js/data-table.js' %}`
   (виж по-долу защо `static_v`, не `static`), после `initExcelStyleTable(...)`.
3. Отделен бутон извиква `table.download('xlsx', 'name.xlsx', {...})`.
4. Всяка колона взима `width: <px>` изрично в дефиницията ѝ (виж
   `products_list.html`/`product_history.html`) — без това `layout:
   'fitColumns'` ги разпределя равномерно, което рядко изглежда добре
   (напр. "Code" не му трябва толкова място, колкото "Name").
5. За таблица, на която ѝ трябва цялата ширина на екрана (не центрирана
   "картичка" с празно място отляво/дясно): `{% block main_class %}main-wide{%
   endblock %}` + `{% block container_class %}container-fluid{% endblock %}`
6. **"Columns ▾" chooser** — автоматичен, добавя се сам над всяка таблица
   (`initExcelStyleTable` го слага освен ако подадеш `{columnChooser: false}`
   в options). Колона, която не бива да се крие (напр. action-колоната с
   "View" линка), взима `hideable: false`. Колона, скрита по подразбиране
   (напр. Delivery Price, по-рядко нужна от Sell Price) — `visible: false`.
7. **Сума/среднa отдолу на колона** — вградено Tabulator `bottomCalc: 'sum'`
   (или `'avg'`/`'max'`/...) в дефиницията на колоната; `bottomCalcFormatter`
   за custom текст на резултата (виж `quantity_change`/`unit_price` в
   `product_history.html`).
8. **Inline редакция направо в грида** ("Enable Edit" бутон, виж
   `products_list.html` като еталон):
   - Отделна `ModelForm` само с редактируемите полета (напр.
     `ProductInlineEditForm` в `products/forms.py`) — **никога** цялата
     `ProductForms`, за да не се пипат полета като `quantity` (правило:
     наличност се променя само през Deliveries/Sales) или M2M relations
     (`supplier`) — ModelForm.save() изчиства M2M, който не е в `Meta.fields`,
     ако формата съдържа целия модел.
   - Отделен `View` (напр. `ProductInlineUpdateView`) с
     `permission_required` = същото като главната Edit страница,
     приема POST `field`+`value`, връща JSON `{error: ...}` при invalid.
   - JS: **НЕ** дръж `editor` статично зададен с отделна `editable()`
     функция, която връща `editMode` — Tabulator спира propagation-а на
     клика за клетка с зададен `editor`, дори когато `editable()` връща
     false, което тихо чупи "клик на реда отваря продукта" (виж по-долу).
     Вместо това колоните тръгват **без** `editor`; "Enable Edit" бутонът
     сам добавя/маха editor-а динамично през
     `column.updateDefinition({editor: ...})` / `updateDefinition({editor:
     false})` за всяко поле.
   - `cellEdited` handler POST-ва през `fetch` с `X-CSRFToken` header
     (cookie `csrftoken`); при грешка от сървъра — `alert` +
     `cell.setValue(cell.getOldValue())` (връща старата стойност).
   - FK колона (напр. Category) се редактира по **име**, не по pk — съвпада
     с това, което вече показва Excel-филтъра за същата колона; view-то
     resolve-ва името обратно към instance.
9. **Клик на реда отваря записа** (вместо отделна "View" колона) —
   `table.on('rowClick', function(e, row) { window.location = row.getData().view_url; })`.
   Пази се от: (а) селект-чекбокс колоната (няма `field`, проверявай
   `e.target.closest('.tabulator-cell').getAttribute('tabulator-field')`),
   (б) Edit mode включен (виж т.8 по-горе — клетки с `editor` така или иначе
   не пропускат клика).
10. **Multi-select + bulk действия** (checkbox колона
    `formatter: 'rowSelection'` + table опция `selectableRows: true`, виж
    `products_list.html`):
    - Скрита лента (`bulk-actions-bar`), появява се при
      `table.on('rowSelectionChanged', ...)` когато `data.length > 0`.
    - Един bulk endpoint (`ProductBulkActionView`) приема `action` +
      `ids[]` (+ `value` при нужда) и проверява permission **по action**
      (`change_product` за category/price, `delete_product` за delete) —
      не един статичен `permission_required`, защото различните действия
      изискват различни права.
    - Bulk delete трие **по един** обект в цикъл, не `queryset.delete()` —
      един `ProtectedError` в bulk `.delete()` би спрял/анулирал цялото
      изтриване, дори за необвързаните продукти.
    - След успешен bulk action: прост `location.reload()` (по-просто и
      по-сигурно от ръчна синхронизация на локалните данни в грида).

**Кеш на статични файлове при разработка:** `style.css` и `data-table.js` се
редактират често в тази фаза, а браузърите ги кешират упорито — стар CSS/JS
изглежда като "нищо не се промени". За да не се разчита на ръчен hard
refresh, зареждай ги с `{% load cache_bust %}` + `{% static_v 'path' %}`
(дефиниран в `core/templatetags/cache_bust.py`) вместо голия `{% static %}` —
добавя `?v=<mtime на файла>`, така браузърът вижда нов URL при всяка промяна
и презарежда автоматично. `base.html` вече го ползва за `style.css`.
Vendor файловете (`tabulator.min.js`, `xlsx.full.min.js`) не се редактират от
нас, могат да си останат на обикновен `{% static %}`.

Статус на роло-аута виж в `ROADMAP.md` — направено за Products списъка и
Product History; остават Suppliers, Categories, Employees, Sales, Deliveries,
Reports.

## Тестове

Всеки app си има `tests.py`. Преди да декларираш бъг за оправен:
```bash
python manage.py test <app_name>
```
Ако фиксваш логика, която няма покритие — добави тест, който би хванал
стария бъг, преди да пишеш фикса (или веднага след).

`reports` app в момента има почти нулево тестово покритие — трети по-голямо
внимание там, пиши тестове преди рефакторинг, не само след.

## Git дисциплина

- Отделен branch на модул/задача, не директно в `main`.
- Малки, логически commit-и — една поправка = един commit с ясно съобщение.
- Никакъв force-push.
- **Миграции само след явно съгласие от потребителя** — те пипат реална
  база данни. Обясни каква миграция ще генерираш и защо, преди да пуснеш
  `makemigrations`.
- Не трий и не пренаписвай съществуващи миграции без изрична молба.

## Среда

- Виртуална среда: `.venv` (активирай преди всякакви pip/manage.py команди).
- Зависимости: `pip install -r requirements.txt`.
- Celery изисква Redis на `localhost:6379` само за реално изпълнение на
  задачи (worker) — не е нужен за `migrate`/`makemigrations`.

## Комуникация

Пиши на български, освен ако потребителят изрично не поиска друго. Кратко и
конкретно — не разтягай обяснения, но не пропускай "защо" зад промяна.
