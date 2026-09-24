**ROADMAP — STORA**  
Фазиран план. Реда на модулите е по зависимост (по-надолу зависи от по-горе),  
   
 за да не се преработва вече "готова" логика. Отмятай със [x], докато Claude  
   
 Code напредва — това е живият backlog, за разлика от CLAUDE.md.  
**Фаза 1 — Одит и стабилизация на съществуващите модули**  
Цикълът за всеки app: провери бъгове → поправи → тест → докладвай статус →  
   
 оптимизирай → следващ app. (Пълните правила на процеса са в CLAUDE.md.)  
- **accounts** — auth, роли, права. Django Groups вече реално enforced  
   
 (permission_required по views, sync на role→group през сигнал),  
   
 register е Manager-only, login грешките са оправени.  
- **core** /  **products** — мъртъв дублиран код в session_service.py  
   
 махнат; permissions по products views (Cashier: view-only, Warehouse:  
   
 add/change, Manager: всичко); sell_price вече задължително;  
 Barcode.code уникален; quantity реално read-only (disabled, не  
   
 само widget attr); N+1 в списъка оправен; добавена **Product History**  
   
 страница (продажби+доставки timeline с период филтър); Barcode/Supplier  
   
 с position (primary + алтернативи); **unit_type** ** (piece/weight)** +  
 quantity навсякъде вече Decimal (виж CLAUDE.md "Известни бизнес  
   
 правила") — схемата и Products UI готови, Sales/Deliveries UI още не.  
 **Нов ред:** продукти-рецепти (Product.is_recipe + RecipeIngredient,  
   
 виж CLAUDE.md "Известни бизнес правила") — модел, миграция, Products UI  
   
 (формсет за съставки на create/edit/details/list), продажбена логика  
   
 (adjust_stock_for_sale приспада съставки вместо собствена наличност)  
   
 и фонов backfill за минали продажби — готови, покрити с тестове  
   
 (RecipeProductTests, RecipeStockDeductionTests,  
 RecipeBackfillTaskTests) и проверени на живо в браузъра. Между другото  
   
 се откри и оправи реален бъг извън обхвата на рецептите:  
 SomeTask.delay(...) е чупело всяка продажба/запис с 500 грешка, когато  
   
 Redis не тече (виж CLAUDE.md "Технически капани" — dispatch_task).  
- **deliveries** — одитиран и преработен. Намерени и оправени бъгове:  
 DeliveryItems.save() добавяше цялото delivery_quantity при ВСЯКО  
   
 save (вкл. редакция на съществуващ ред) вместо delta, и нищо не  
   
 връщаше наличността при триене на ред/цяла доставка — сега  
   
 delta-based + post_delete restore, огледално на sales/models.py  
   
 (виж CLAUDE.md "Известни бизнес правила"). Добавени permission checks  
   
 по всички delivery views (Cashier: view-only, Warehouse: add/change,  
   
 Manager: всичко) — преди само @login_required. Счупен  
 {% for %}/{% empty %} в deliveries_list.html (висящ <tr>, никога  
   
 не показваше "no deliveries") оправен. delivery_quantity в формата  
   
 мина от IntegerField на DecimalField (приема тегловни кг  
   
 количества). **Нови функции по молба на потребителя:** търсим  
   
 доставчик вместо <select> + "+ New supplier" popup; типовете  
   
 документи станаха управляем списък (DocumentType, като  
   
 Category/TaxGroup, Manager-only, без popup shortcut от формата),  
   
 вместо фиксиран choices=[...]; "+ New product" линк от екрана за  
   
 доставка; таблицата с артикули е вече Tabulator с пълен feature set  
   
 — сортиране, Excel-style филтър, column chooser, export to Excel,  
   
 пореден номер, auto-focus flow (Qty→Price при добавяне), editable  
   
 Qty/Unit Price/без-ДДС/Line Total/Sell Price/Markup %/Expiry Date,  
   
 двупосочна връзка между тях (вкл. Line Total→Unit Price backfill),  
   
 цветово оцветяване при промяна на доставна цена спрямо каталога, и  
   
 Sell Price/Markup % се записват веднага в Product.sell_price —  
   
 виж CLAUDE.md за пълната архитектура (_delivery_items_table.html).  
- **sales** — models.py вече беше поправен (delta-based stock  
   
 adjustment, restore on delete, atomic locking, signal-based total).  
 views.py/forms.py/темплейти одитирани и поправени:  
  - **Критично:** НИТО една sales view нямаше login/permission проверка  
   
 — напълно отворени за анонимен потребител (sales_add,  
 sales_draft_save, detail/list/delete). Groups (Cashier/Manager)  
   
 вече имаха правилните permissions от accounts/signals.py, просто  
   
 никой view не ги проверяваше. Добавени @login_required/  
 permission_required + StaffPermissionRequiredMixin, огледално  
   
 на products/deliveries (Cashier: пълен достъп, Warehouse: никакъв,  
   
 Manager: всичко).  
  - Теглови продукти вече реално могат да се продават през формата:  
 sale_quantity input имаше твърдо min="1" без step — браузърът  
   
 native-валидно отказваше дробна стойност (0.350 кг) при submit,  
   
 въпреки че JS-ът за везнени баркодове вече я записваше в полето.  
   
 Фикс: min/step стават 0.001 динамично при избор на unit_type= weight продукт (applyQuantityConstraints() в sale_add.html,  
   
 огледално на deliveries грида).  
  - Счупен {% for %}/{% empty %} в sales_list.html (същия бъг като  
   
 по-рано в deliveries_list.html).  
  - Излишен двоен sale.save() в sales_add махнат (безопасен, но  
   
 ненужна заявка).  
   
 23 нови/разширени теста в sales/tests.py. Везнени баркодове вече  
   
 работещи от преди — виж CLAUDE.md "Известни бизнес правила".  
- **reports** — почти нулево тестово покритие (3 реда). Пиши тестове  
   
 ПРЕДИ рефакторинг тук, не след. **Нова задача (отложена от**  
 **  
 потребителя):** справките да смятат "цена без ДДС" on-the-fly от  
 Product.tax_group.rate (виж CLAUDE.md "Известни бизнес правила" —  
   
 данъчни групи) — не е нужно ново поле в базата, само изчисление.  
**Фаза 1.5 — Sortable/filterable/export таблици (виж CLAUDE.md за конвенцията)**  
Tabulator patтern-ът (sort по клик, Excel-style checkbox филтър по колона,  
   
 export to Excel) е готов и е приложен за Products списъка и Product History.  
   
 Остава да се приложи и за:  
- Suppliers списък (products/templates/products/suppliers_list.html)  
- Categories списък — **направено.** Tabulator (сорт, Excel-филтър,
 export, клик на ред отваря Edit), мека заоблена card-обвивка на грида
 (STYLES.md, Material 3 Expressive посока) — първият екран с новите UI
 насоки. Споделеният `.pos-toggle-btn` (Show on POS) остана непроменен
 нарочно — същия компонент, ползван и в Products грида. **Финална
 итерация (консистентност с Products грида):** Edit/Delete текст
 линковете в реда паднаха изцяло (Edit е излишен — клик на ред вече
 отваря Edit); Delete мина през чекбокс-селекция на ред (макс. 1
 наведнъж) + споделения `#bulk-actions-bar` (появява се над таблицата
 при селекция, точно като Products грида), с кръгло червено delete
 бутонче (`.icon-btn--delete`, `static/delete.png` маскирана в бяло) —
 reuse на съществуващия single-object `CategoryDeleteView`/confirm
 екран, без нов bulk endpoint. "+ Add category" се качи горе до
 заглавието (`.icon-btn--add`, зелено кръгче, hover/focus разтяга се в
 pill с надпис) — първи опит беше долу под таблицата, махнато по молба
 на потребителя, за да не се чупи консистентността с останалите
 таблици. Export to Excel се премести в лентата над грида, срещу
 "Columns ▾" (`.data-table-card`/`.data-table-card__toolbar` — споделен
 клас с Products списъка, виж по-долу). Реален Tabulator бъг открит и
 оправен по пътя (checkbox колоната не спазваше лимит от 1 избран ред)
 — виж CLAUDE.md "Технически капани".
- **Products списък — bulk-bar redesign, консистентно с Categories
  (направено).** `#bulk-actions-bar` (появява се при чекбокс селекция)
  вече е pill-shaped (`.bulk-actions-bar`), не Bootstrap `.alert`.
  Category `<select>` замени се с pill search box (лупичка 🔍 вътре,
  `.pill-search-wrapper`) + "Browse" бутон, същия tree picker patтern
  като Product формата — пости категорията по ИМЕ, колкото `<select>`-а
  преди (`ProductBulkActionView.set_category` очаква име, не се пипа).
  Apply Category/Apply Price бутоните станаха кръгли `.icon-btn--apply`
  (синьо, чек-марк ✓, hover/focus разтяга в pill с надпис) — Apply
  Category стои disabled докато не избереш категория (search резултат
  или Browse), Apply Price остава винаги clickable с alert validation
  както преди. Delete Selected стана `.icon-btn--delete` (същото кръгло
  кошче като Categories). "+ New Product" стана `.icon-btn--add`
  (зелено кръгче), Export to Excel слезе в лентата над грида срещу
  "Columns ▾" (`.data-table-card`). Мъртвият `category_choices` context
  (само захранваше старото `<select>`, `json_script`-а му дори не се
  четеше от JS) — премахнат от `ProductListView`.
  **Полиране по обратна връзка от потребителя (направено):** 🔍/"+"/"✓"
  емоджита/текстови глифове замениха се с чисти flat SVG икони (Google
  Material Design paths, mask-image техника като кошчето) — `.icon-btn__glyph`
  споделена база + `--add`/`--check`/`--bin`/`--search` модификатори,
  вместо текст/емоджи, които изглеждаха "старомодно" до останалите икони.
  Открит и оправен реален CSS бъг: `.pill-input` губеше срещу глобалното
  `input[type="text"]` правило (по-висока specificity), затова полетата не
  ставаха pill-shaped въпреки `border-radius: 999px` в кода — фикс с
  `input.pill-input` + `!important`. Нов `.icon-btn--sm` вариант за
  бутоните вътре в bulk лентата (по-малки от самостоятелните header "+"
  бутони, които останаха 3rem — bulk лентата е нагъсто с pill полета,
  header бутонът е самостоятелно главно действие).
  **Categories List — свободна мулти-селекция + реален bulk delete
  (направено).** По обратна връзка ("не мога да селектна повече от
  едно") — старото ограничение до 1 избран ред (заради reuse на
  single-object `CategoryDeleteView`) паднало, чекбокс колоната вече е
  свободна както в Products. Нов `CategoryBulkDeleteView`
  (`/products/categories/bulk-delete/`) — trие по един обект в цикъл
  (огледално на `ProductBulkActionView`-ото `delete` действие), с
  `confirm()` диалог преди POST-а (изрично поискано от потребителя).
  Старият single-object `CategoryDeleteView`/confirm екран остава жив
  (валиден URL), само вече не се линква от списъка. 3 нови теста
  (`CategoryBulkDeleteViewTests`), 121/121 общо в products.
  **По-дълго поле за категория + Browse вътре в pill-а (направено).**
  Products bulk bar-ът category search полето стана по-широко (200px →
  340px), "Browse" бутонът се премести ВЪТРЕ в pill-а, закотвен в десния
  край (`.pill-search-wrapper--with-browse`/`__browse-btn`), вместо
  отделен бутон до полето. Навигационната търсачка в хедъра (base.html)
  също мина на pill shape + масикрана SVG лупичка, вместо plain Bootstrap
  `.form-control`.
- **Export to Excel / Enable Edit — иконки навсякъде, споделен JS helper
  (направено).** По молба на потребителя (референтни икони: Excel файл
  със стрелка, молив в капсула) — "Export to Excel" стана кръгла зелена
  икона (`.icon-btn--export`, download-стрелка), "Enable Edit" стана
  кръгла **outline** икона (`.icon-btn--outline`, молив,
  `.is-active` при включен режим — outline вместо плътен цвят, защото е
  ПОСТОЯНЕН toggle, не еднократно действие като останалите). Нов
  `attachTableExportToToolbar(table, cardSelector, filename, sheetName, extraButtons)` в `static/js/data-table.js` — вместо да се копира
  същия relocate-в-toolbar код във всеки темплейт, една споделена
  функция; автоматично СЪЗДАВА "Columns ▾" лентата, ако таблицата е
  построена с `columnChooser: false` (напр. sale_details.html), вместо
  тихо да пропусне Export бутона. Приложено във **всичките 17 темплейта**
  с "Export to Excel" в проекта: products_list/category_list/
  product_history (products), sales_report/deliveries_report/
  stock_as_of_report/expiring_report/sales_quantity_report/
  ai_report_detail (reports), revision_list/revision_detail/
  revision_details (revisions), sale_details (sales), delivery_details/
  _delivery_items_table (deliveries), price_list_detail/price_list_list
  (pricelists). Всяка таблица, обвита в `.data-table-card`, ако още не
  беше. Тествано: пълния test suite (products/reports/revisions/sales/
  deliveries/pricelists) + на живо в браузъра на представителна извадка
  (Product History с 2 таблици на една страница, Sale Details с
  `columnChooser: false`, Deliveries add формата с Delete Line/Discount
  до Export-а).
- Employees списък (accounts/templates/accounts/employee_list.html)  
- Sales списък — **направено**, но по различен път от Deliveries:
 самостоятелният sales_list.html е **изтрит изцяло** (не остана като
 route/redirect target), защото потребителят изрично поиска да не
 дублира с sales_report.html — **reports/sales_report.html** вече е
 единственият "browse all sales" екран (период + категория филтър,
 Tabulator: #, ID, Date, Time, Cashier, Items, Total Amount, Refund
 Status — клик на ред отваря sale_details). Всички стари линкове към
 sales_list (navbar, index.html, SalesDeleteView.success_url,
 clear_cashier_operation, sale_confirm_delete "NO") пренасочени към
 sales_report.
- Deliveries списък — вместо самостоятелния deliveries_list.html,  
 **reports/deliveries_report.html** ** пое тази роля** (Tabulator, период  
  - supplier филтър, клик на ред отваря/редактира/трие) — потребителят  
   
 реши да не дублира двата екрана. deliveries_list remains жив само  
   
 като route/redirect target, не се навигира до него от менюто повече.  
- Reports таблици (останалите — dashboard.html)  
**Фаза 2 — Редизайн на интерфейса за продажби**  
Ползвай `frontend-design` скила (виж `CLAUDE.md`) — да предложи визуална посока за одобрение преди писане на код.  
- Пълен UI/UX редизайн на sales модула — вдъхновен от реален POS  
   
 софтуер (screenshot от потребителя). Обхват потвърден: количка +  
   
 категории + checkout стъпка (плащане в брой/карта + ресто). НЕ:  
   
 клиенти/баланс/бонус точки (отделна бъдеща задача), множество  
   
 едновременни отворени кошници (само една активна продажба, старият  
   
 draft-save механизъм остава). Изгражда се на етапи:  
  - **Подкатегории** (Category.parent, self-FK) — направено като  
   
 подготовка за бутоните по категория/подкатегория. Виж CLAUDE.md за  
   
 архитектурата (cycle prevention, get_descendant_ids()).  
  - **Стъпка 1 — данни:**SaleAttributes.payment_method/amount_paid/  
 change_due (nullable), products_data+category_id, ново  
 categories_data в контекста на sales_add.  
  - **Стъпка 2 — дясна лента с категории:** папки за drill-down +  
   
 продукт-бутони (директно assign-нати на текущото ниво), клик добавя  
   
 в количката/увеличава количество, жив часовник горе ляво. Виж  
   
 CLAUDE.md за пълната архитектура. Реален бъг хванат по пътя: HTML5  
 step е относителен спрямо min, не спрямо 0 — default sale_ quantity widget-ът блокираше ВСЯКА цяла бройка (виж CLAUDE.md  
   
 "Технически капани").  
  - **Стъпка 3 — checkout модал:** Bootstrap modal, "Complete Sale"  
   
 вече отваря модал вместо direct submit. В брой/С карта бутони,  
   
 Сума/Платено/Ресто (Платено полето скрито/фиксирано на сумата при  
   
 карта — няма ресто концепция). Guard-ове: празна количка не отваря  
   
 модал, "платено < сума" при кеш не позволява потвърждение. Confirm  
   
 попълва hidden payment_method/amount_paid/change_due полета  
   
 и праща реалната форма програмно. Проверено на живо — CASH (с  
   
 ресто) и CARD (paid=total, change=0) потоци, и двата guard-а.  
   
 Реален бъг хванат: многоредов {# #} темплейт коментар изтичаше  
   
 като текст, счупи <form> селектора другаде (виж CLAUDE.md  
   
 "Технически капани"). 5 нови теста, 219/219 общо.  
 **Доизгладено веднага след, по молба на потребителя** (реален  
   
 touch-screen контекст): on-screen numeric клавиатура до Платено  
   
 полето (квадратни бутони), Ресто вдигнато под Тотал, поправена  
   
 цветова схема (открит бъг — глобално button{color:white} правило  
   
 правеше текста на всички нови бутони невидим). Виж CLAUDE.md за  
   
 и двата допълнителни реални бъга (invisible text + type="number"  
   
 тихо изчистване на непълна стойност).  
 **После, по молба на потребителя, пак — layout + комбинирано**  
 **  
 плащане:** двуколонен layout (методи вляво, суми+клавиатура  
   
 вдясно, огледално на референтния screenshot), трети MIXED  
   
 payment_method за разделено карта/кеш плащане (ново  
 SaleAttributes.card_amount, само Card amount полето е editable,  
   
 кеш остатъкът е read-only изчислен ред). Migration приложена БЕЗ  
   
 отделно ново потвърждение тази конкретна сесия (флоу-та вече бяха  
   
 одобрени наскоро преди това за същия модал) — отбелязано на  
   
 потребителя изрично, безопасна/добавяща миграция. 2 нови теста,  
   
 221/221 общо. Тествано на живо — 2-колонен layout потвърден,  
   
 и трите метода (CASH/CARD/MIXED) end-to-end.  
  - **Нова стъпка, добавена по молба на потребителя — куратиране на POS**
    **панела (show_on_pos + pinned shortcut bar):** `Category.show_on_pos`/
    `Product.show_on_pos` (опит-ин, default False) — категорийният панел
    показва само флагнати елементи. Нов `PosPin` модел (sales) — фиксирана
    лента горе, cashier-куратиран микс от папки/продукти, с "Edit
    shortcuts" режим (picker само от show_on_pos елементи). Топ-3
    най-продавани (последни 7 дни) продукта излизат първи при отваряне на
    категория. Виж CLAUDE.md за пълната архитектура (pins-only на първо
    ниво, repeat-tap пагинация, защо няма Back бутон/breadcrumb надпис).
    Продукти грида и Categories списъкът получиха и по едно toggle бутонче
    (On/Off) за show_on_pos, директно клик, без нужда от "Enable Edit".
    Тествано: 234/234 автоматични теста, плюс живо в браузъра (pin
    add/remove, repeat-tap пагинация, top-3-recent подреждане, toggle
    бутоните в двата списъка).
  - **Стъпка 4/5 — количката на Tabulator (направено):** замени
    plain-table + Django formset с Tabulator, огледално на deliveries
    грида -- едно търсене-или-скан поле добавя/качва ред (client-side
    търсене срещу целия каталог, не сървърния `ingredient_search` -- той
    изключва рецептурни продукти, а те са продаваеми), везнените
    баркодове се декодират клиентски както преди, фокуса остава в
    полето за търсене след добавяне (за разлика от Deliveries, където
    отива в Qty) -- за непрекъснато сканиране. Колони: #, Име, Кол-во,
    Единична цена, Сума -- редактируеми през custom number editor
    (нативните стрелки ↑/↓ на браузъра инкрементират/декрементират),
    Qty/Price/Sum свързани двупосочно като при Deliveries. Грида е в
    кутия с фиксирана височина, скролва вътрешно. При submit hidden
    inputs се пресъздават от Tabulator данните точно преди POST, както
    при Deliveries.
    - **Екранна клавиатура** (BG фонетична / EN превключване) до
      полето за търсене -- за тъч екран без физическа клавиатура.
    - **Бутонът за триене на ред е изместен долу** -- цъкаш ред за да
      го маркираш, после "Delete Line" или "Void Sale" (с потвърждение)
      във футъра. Всяко триене (един ред или цялата количка) се логва в
      нов модел `SaleItemVoidLog` (кой, какво, колко, кога) -- дори
      количката да е само draft state, никога незапазена продажба.
      Засега само данните се пазят, без отделен екран за преглед
      (отложено по молба на потребителя).
    - Тествано на живо: търсене+скан+везнен баркод, ръчна корекция на
      Qty/Price/Sum, скрол на дълга количка, draft save/restore,
      клавиатурата (писане, BG/EN, backspace, Enter), selection+Delete
      Line+лог запис, Void Sale с потвърждение/отказ, пълен checkout→
      submit цикъл с коректно намалена наличност. 240/240 автоматични
      теста.
  - **Стъпка 6 (направено):** 3 паралелни отворени продажби ("кошници"/
    табове) — вместо ограничението "само една активна продажба наведнъж"
    от по-рано в тази фаза. Завършена продажба вече зарежда нова
    продажба (не списъка), Change остава видим до следващата продажба,
    визуален редизайн на хедъра (без плътен син фон зад заглавието,
    табовете преместени/увеличени, различна иконка за пълна кошница,
    клавиатурна иконка вътре в полето за търсене, Cancel бутонът
    премахнат от футъра). Виж CLAUDE.md за пълната архитектура
    (`STORA.sales.tab_state`, `last_change` persistence). 53/53
    автоматични теста в sales.
  - **Многодумно търсене навсякъде** (не само в sales) — "вода пред"
    намира "Изворна вода Предела" независимо от реда на думите, във
    ВСЯка свободнотекстова търсачка в проекта (navbar, ingredient/
    supplier/batch pickers, касовата количка). Виж CLAUDE.md за
    `multi_token_icontains_q()`. Потвърдено безопасно за баркод
    сканиране (единична "дума", идентично поведение на старото).
  - **Products списък (направено):** запомнящ се изглед на колоните
    (ред/видимост/ширина) през localStorage, активна редактируема
    Markup % колонка (двупосочно свързана със Sell/Delivery Price),
    почистено форматиране (без "eur", pcs/kg, чисти числа), брояч на
    продукти под Code колонката. Опцията за запомнящ се изглед
    (`persist` в `initExcelStyleTable`) сега съществува като споделен
    helper за всяка таблица, но е включена само тук — Suppliers/
    Categories/Employees/Sales/Deliveries/Reports от Фаза 1.5 по-долу
    могат да я вземат наготово, когато им дойде редът. Виж CLAUDE.md за
    трите Tabulator re-entrancy капана + persistence merge бъга,
    открити по пътя.
- Запази/подобри draft-save механизма (в момента през JS + fetch към  
 sale_draft_save) — работи, но да се прегледа за race conditions при  
   
 бързо кликане.  
**Фаза 3 — Нови функционалности**  
- **Изписване (Write-off)** — "Stock Movements" вече обхваща и  
   
 доставка, и изписване (DeliveryAttributes.movement_type), вместо  
   
 отделен app/модел. Изписването намалява наличността (обратен знак на  
   
 delta-based логиката), пази се задължително към доставчик, с  
   
 автогенериран вътрешен номер при празно поле. Отделни nav линкове  
   
 ("Delivery"/"Write-off"/"Scrap"), споделен Tabulator грид с визуално  
   
 разграничение (червена рамка + "−" пред количество/сума на ред).  
   
 Справките за доставки explicit филтрират movement_type, за да не  
   
 изтичат изписвания в тях — виж CLAUDE.md за пълната архитектура.  
   
 Нито изписване, нито брак показват "+ New supplier"/"+ New product"  
   
 шорткътите — не можеш да изпишеш/бракуваш нещо, което тепърва създаваш.  
- **Брак (Scrap)** — трети movement_type=SCRAP, без доставчик (полето  
   
 е nullable), с управляем списък причини (ScrapReason, като  
   
 DocumentType/TaxGroup, Manager-only add/change). Два варианта за  
   
 въвеждане на едно и също "Brak" движение: (1) търсачка за вече  
   
 заредена партида по продукт (само редове с expiry_date, само от  
   
 реални доставки — BatchSearchView/batch_search endpoint), която  
   
 попълва DeliveryItems.source_item (self-FK) за проследимост назад  
   
 към оригиналния ред, и expiry_date; (2) свободно въвеждане  
   
 продукт+количество+причина (същата продуктова търсачка като  
   
 доставка/изписване), без source_item. source_item е SET_NULL,  
   
 за да не се изтрива историята при брак, ако оригиналният ред/доставка  
   
 по-късно се трие. Виж CLAUDE.md за пълната архитектура.  
- **Справка за изписвания/брак — направено, обединено с доставките.**
 Вместо отделна справка (както първоначално планирано),
 reports/deliveries_report.html вече е един общ "Stock Movements
 Report" за трите movement_type-а — селектор (Delivery/Write-off/
 Scrap) в чузера, DeliveriesReportView филтрира по GET параметър
 movement_type (по подразбиране Delivery, невалидна стойност пада
 обратно на Delivery). Капан, хванат при обединението: редът, който
 строеше deliveries_data, правеше delivery.document_type.name и
 delivery.supplier.name безусловно — и двете са None за Write-off/
 Scrap, значи щеше да гръмне веднага щом някой избере тях; сега е
 ... if delivery.document_type else ''. Menu label и dashboard линка
 преименувани на "Stock movements report".
- **Ревизии/Сторно — направено.** RefundAttributes/RefundItems
 (sales/models.py) — сторниране на цяла или част от вече завършена
 продажба, ред по ред, с ограничение да не се сторнира едно и също
 количество двукратно (refund_items сумата спрямо оригиналния ред).
 Причините съвпадат 1:1 с фискалния протокол на Дейзи (виж CLAUDE.md
 Наредба Н-18 бележката) — готово за директно подаване, когато дойде
 фискалната интеграция. "Refund" бутон (червен) в касовия екран и
 на sale_details.html вместо старото DELETE. sales_report.html
 показва статус (Not/Partial/Fully) на колонка.
- **Ревизии (Stock Revision) — направено.** Нов app `revisions` — Warehouse/Manager стартира преброяване, търси/сканира продукти (същата клиентска логика като касата, вкл. везнени баркодове), намереното количество се СЪБИРА при повторно сканиране/от друг компютър (RevisionItems.add_count, select_for_update), не се презаписва — така няколко души могат да броят паралелно в една обща ревизия. Само ЕДНА отворена ревизия наведнъж, гарантирано от DB partial unique constraint (only_one_open_revision), не само проверка на ниво код — втори опит за старт просто те присъединява към вече отворената. Живият екран за броене poll-ва сървъра на 4 сек, за да се вижда преброеното от друг компютър без ръчен reload. Колони Diff Qty/Diff Sum светват червено при разлика found != system. Complete Revision заключва и коригира Product.quantity директно на намереното (не delta), маркира ревизията COMPLETED. Cancel Revision анулира без да пипа наличността (status=CANCELLED, редовете се пазят за справка, нищо не се прилага към Product.quantity) и освобождава мястото за нова ревизия. Съзнателно НЕ е част от Stock Movements report (една ревизия може едновременно да увеличава едни продукти и да намалява други -- сегашният Delivery/Write-off модел е "един знак за целия документ", не го поддържа) -- вместо това ProductHistoryView получи трети източник на събития (завършени ревизии, само където found != system), значи историята на конкретен продукт пак показва "Revised -3" и т.н. Виж CLAUDE.md за пълната архитектура.
- **Паралелни отворени доставки ("табове", идея за бъдеще)** — по аналогия с 3-те паралелни отворени продажби на касовия екран (STORA.sales.tab_state), но за deliveries_add/deliveries. Идеята изникна от наблюдение на живо: draft-ът на текуща доставка вече преживява грешка/презареждане благодарение на сесийния draft save (get_cashier_operation_state), но само ЕДНА активна доставка наведнъж се пази в тази сесийна ключ — не е приоритет сега, но си струва списък с отворени/чернови доставки, когато дойде редът на тази фаза.
- **AI интеграция за доставки** — асистиран анализ/предложения при  
   
 въвеждане на доставка (напр. предложения за количества по история).  
- **AI режим на справките ("AI Reports") — направено.** Нов модел `AIReport` в `reports` app-а (не отделен app) — прост цикъл потребителски промпт → Claude (Anthropic API, `STORA.reports.ai_service.run_ai_report`) → `run_sql` tool, изпълняван срещу отделната `connections['readonly']` Postgres роля (виж `db_create_ai_readonly_role.sql`), докато не стигне до текстов отговор. Съзнателно НЕ минава през DRF/ORM (различно от първоначалната идея по-горе) — директен SQL е по-гъвкав за произволни аналитични въпроси, а истинската защита идва от ролята, не от ограничен endpoint. Схемата (реални имена на таблици/колони) се подава автоматично в system prompt-а през `describe_schema()` (интроспекция на Django моделите), с изричен пропуск на `password` колоната и на admin/auth/sessions/core apps. Записана справка пази ПРОМПТА + последната генерирана SQL — отварянето ѝ винаги пуска SQL-а наново срещу текущите данни (никога стар кеширан snapshot), докато самият текстов отговор/SQL се обновяват само при изричен "Regenerate" бутон (нов API разход). Manager-only (по-стеснено от Manager/Warehouse политиката на другите справки — тук AI-то може да чете буквално всяка таблица). Тествано с mock-нат `run_ai_report`/`rerun_stored_query` (истинският Anthropic API + `readonly` роля не са достъпни в тестовата база — грантовете са само върху `stora_db`, не върху Django-ъвата test база, потвърдено на живо).  
- **Превод на приложението на български (идея за бъдеще)** -- цялото UI в момента е на английски (умишлено, за консистентност -- виж CLAUDE.md "Комуникация"). Обхватът/подходът (Django i18n с {% trans %}/{% blocktrans %} + .po файлове, или директно преведени шаблони) не е решен -- за изясняване с потребителя, когато дойде редът на тази задача.
**Фаза 4 — Хардуер интеграции**  
- Интеграция с касови апарати (фискализация — да се провери конкретен  
   
 модел/протокол преди работа, силно зависи от българското  
   
 законодателство за фискални устройства).  
- Четене от баркод скенери (типично работят като HID клавиатура —  
   
 вероятно не изисква специален драйвер, но да се потвърди с  
   
 конкретния модел скенер).  
- Печат на етикети (да се избере принтер/протокол — ZPL и т.н. — преди  
   
 имплементация).  
**Отворени въпроси (за изясняване с потребителя преди съответната фаза)**  
- Multi-tenancy / повече от един физически обект — засега извън обхват,  
   
 но да се има предвид при дизайн на нови модели, за да не пречи по-късно.  
- Точен модел каса/фискален принтер за интеграцията в Фаза 4.  
