/**
 * Shared helper for rendering a sortable, per-column-filterable, Excel-
 * exportable table (via Tabulator) from JSON data a Django view embedded
 * with {{ data|json_script:"some-id" }}.
 *
 * Every column gets the same "Excel-style" header filter: a small dropdown
 * with a checkbox per unique value found in that column. Column sorting is
 * Tabulator's built-in click-to-sort on the header title. A "Columns ▾"
 * toggle above the table lets the user show/hide columns. Any column with
 * `bottomCalc` set gets a totals row automatically (built into Tabulator).
 */
(function (global) {
    // Every open toggle/panel pair, so closeAllPanels can tell a click
    // *inside* one of them apart from a click elsewhere on the page --
    // more robust than scattering stopPropagation() on each new child
    // element (easy to miss one and have the panel close under you while
    // still trying to check boxes inside it).
    var openWidgets = [];

    function closeAllPanels(e) {
        openWidgets.slice().forEach(function (widget) {
            if (e && (widget.panel.contains(e.target) || widget.toggle.contains(e.target))) {
                return; // clicked inside this panel (or its own toggle) -- leave it open
            }
            widget.panel.classList.remove('is-open');
            var idx = openWidgets.indexOf(widget);
            if (idx > -1) openWidgets.splice(idx, 1);
        });
    }

    document.addEventListener('click', closeAllPanels);
    // No `true` (capture) here on purpose: with capture:true this would also
    // catch 'scroll' events from *any* nested scrollable element on the page
    // -- including Tabulator's own internal (virtualized) row viewport,
    // which fires one whenever it adjusts its scroll position after a
    // filter/redraw. That closed an open filter panel out from under
    // whoever was mid-click on a checkbox inside it, but only when the row
    // count/previous scroll position happened to require that adjustment --
    // hence it appearing to work "most of the time" and randomly not.
    // Plain (bubble-phase) 'scroll' on window only fires for genuine page
    // scrolling, which is the only case we actually want to react to here
    // (the floating panel is position:fixed, so it visually detaches from
    // its "▾" toggle once the page scrolls under it).
    window.addEventListener('scroll', closeAllPanels);
    window.addEventListener('resize', closeAllPanels);

    // Small "▾ toggle -> floating checkbox panel" widget, shared by the
    // per-column Excel-style filter and the column-visibility chooser.
    // The panel is appended to <body> (not wherever the toggle lives) so it
    // floats freely on top of the page instead of being clipped/pushed by
    // an ancestor with overflow:hidden (Tabulator's header cells do this).
    function createFloatingToggle(label, title) {
        var wrapper = document.createElement('div');
        wrapper.className = 'excel-filter';

        var toggle = document.createElement('span');
        toggle.className = 'excel-filter-toggle';
        toggle.setAttribute('role', 'button');
        toggle.setAttribute('tabindex', '0');
        toggle.title = title || '';
        toggle.textContent = label;
        toggle.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                toggle.click();
            }
        });

        var panel = document.createElement('div');
        panel.className = 'excel-filter-panel';
        document.body.appendChild(panel);

        var widget = {wrapper: wrapper, toggle: toggle, panel: panel};

        function positionPanel() {
            var rect = toggle.getBoundingClientRect();
            panel.style.top = rect.bottom + 'px';
            panel.style.left = rect.left + 'px';
        }

        toggle.addEventListener('click', function (e) {
            e.stopPropagation();
            var isOpen = panel.classList.contains('is-open');
            closeAllPanels();
            if (!isOpen) {
                positionPanel();
                panel.classList.add('is-open');
                openWidgets.push(widget);
                wrapper.dispatchEvent(new Event('panelopen'));
            }
        });
        panel.addEventListener('click', function (e) { e.stopPropagation(); });

        wrapper.appendChild(toggle);
        return widget;
    }

    // Tabulator's fitColumns layout builds each column's header twice during
    // startup (once to measure, once with final widths) -- calling our
    // editor function again for the same field. Without this cache that
    // creates a second, fully independent toggle+panel stacked on top of
    // the first one: clicking "the arrow" then opens whichever of the two
    // Tabulator happened to leave on top, so unchecking a value sometimes
    // landed on a *different* (fresh, closed) panel than the one visibly
    // open -- explains the "closes only sometimes" report. Keyed per-table
    // (see initExcelStyleTable) so two tables on one page can't collide on
    // a shared field name.
    function makeExcelStyleFilterEditor(widgetCache) {
        return function excelStyleFilterEditor(cell, onRendered, success) {
            var column = cell.getColumn();
            var field = column.getField();
            var widget = widgetCache[field];
            if (!widget) {
                widget = createFloatingToggle('▾', 'Filter');
                // null = no filter (everything included); otherwise the
                // list of values still checked. Remembered across panel
                // close/reopen (not just within one open session) so the
                // checkboxes reflect what's actually applied, and so the
                // toggle can show "this column has an active filter" at a
                // glance without opening the panel.
                widget.selectedValues = null;
                widgetCache[field] = widget;
            }

            function currentValues() {
                return column.getTable().getData().map(function (row) {
                    var v = row[field];
                    return v === null || v === undefined || v === '' ? '(blank)' : String(v);
                });
            }

            function buildPanel() {
                widget.panel.innerHTML = '';
                var unique = Array.from(new Set(currentValues())).sort();
                var checkboxes = [];
                var rows = []; // {label, value} -- for the search box below to show/hide

                function apply() {
                    var checked = checkboxes.filter(function (cb) { return cb.checked; }).map(function (cb) { return cb.value; });
                    widget.selectedValues = checked.length === unique.length ? null : checked;
                    widget.toggle.classList.toggle('is-filtered', widget.selectedValues !== null);
                    success(widget.selectedValues === null ? '' : widget.selectedValues);
                }

                var search = document.createElement('input');
                search.type = 'text';
                search.placeholder = 'Search...';
                search.className = 'excel-filter-search';
                widget.panel.appendChild(search);

                var allLabel = document.createElement('label');
                var allCb = document.createElement('input');
                allCb.type = 'checkbox';
                allCb.checked = widget.selectedValues === null;
                allLabel.appendChild(allCb);
                allLabel.appendChild(document.createTextNode(' (All)'));
                widget.panel.appendChild(allLabel);

                unique.forEach(function (val) {
                    var label = document.createElement('label');
                    var cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.checked = widget.selectedValues === null || widget.selectedValues.indexOf(val) > -1;
                    cb.value = val;
                    checkboxes.push(cb);
                    cb.addEventListener('change', function () {
                        allCb.checked = checkboxes.every(function (c) { return c.checked; });
                        apply();
                    });
                    label.appendChild(cb);
                    label.appendChild(document.createTextNode(val));
                    widget.panel.appendChild(label);
                    rows.push({label: label, value: val});
                });

                allCb.addEventListener('change', function () {
                    checkboxes.forEach(function (cb) { cb.checked = allCb.checked; });
                    apply();
                });

                // Only hides rows from view -- doesn't touch their checked
                // state, so narrowing the search then clicking "(All)" still
                // only (de)selects what's currently visible would be
                // surprising; instead "(All)" always means literally all rows,
                // matching what a user expects from a checkbox labelled that.
                search.addEventListener('click', function (e) { e.stopPropagation(); });
                // Multi-word, order-independent matching -- same idea as
                // the project's multi_token_icontains_q helper (see
                // CLAUDE.md) used everywhere else free-text search
                // happens, just a client-side version here since this
                // panel filters values already loaded in the browser, not
                // a server queryset. "сол хрус" -> ["сол","хрус"], both
                // must appear somewhere in the value (any order) for it to
                // match -- so it finds "Солети Хрус Хрус" (flagged live:
                // the plain single-substring check before this needed the
                // typed text to appear as one continuous run, so "сол
                // хрус" itself never matched anything).
                search.addEventListener('input', function () {
                    var tokens = search.value.toLowerCase().split(/\s+/).filter(Boolean);
                    rows.forEach(function (row) {
                        var value = row.value.toLowerCase();
                        var matches = tokens.every(function (t) { return value.indexOf(t) > -1; });
                        row.label.style.display = matches ? '' : 'none';
                    });
                });
                setTimeout(function () { search.focus(); }, 0);
            }

            // Tabulator re-invoking this editor for an already-cached field
            // (see comment above) would otherwise stack a second 'panelopen'
            // listener bound to this call's *stale* `success`/`column` --
            // swap it out instead of piling on.
            if (widget.buildPanel) {
                widget.wrapper.removeEventListener('panelopen', widget.buildPanel);
            }
            widget.buildPanel = buildPanel;
            widget.wrapper.addEventListener('panelopen', buildPanel);
            onRendered(function () {});
            return widget.wrapper;
        };
    }

    function excelStyleFilterFunc(headerValue, rowValue) {
        if (headerValue === '' || !headerValue) return true;
        var display = rowValue === null || rowValue === undefined || rowValue === '' ? '(blank)' : String(rowValue);
        return headerValue.indexOf(display) > -1;
    }

    // "Columns ▾" toggle placed just above the table: a checkbox per
    // hideable column to show/hide it via Tabulator's own column.toggle().
    function addColumnChooser(containerElement, table, columns) {
        var bar = document.createElement('div');
        bar.className = 'mb-2';
        var widget = createFloatingToggle('Columns ▾', 'Choose visible columns');
        // Restyled as a round icon-btn (eye glyph, static/visibility.png)
        // instead of the plain text "Columns ▾" link -- className is
        // REPLACED outright (not appended alongside excel-filter-toggle),
        // since click/keydown/positioning on this widget are all wired to
        // the element itself in createFloatingToggle, not to that class --
        // mixing it with icon-btn's own sizing rules would risk the exact
        // same cross-class specificity fight the bin icon hit earlier
        // (see .icon-btn__glyph--bin's comment in style.css).
        widget.toggle.className = 'icon-btn icon-btn--outline icon-btn--sm';
        widget.toggle.innerHTML =
            '<span class="icon-btn__icon"><span class="icon-btn__glyph icon-btn__glyph--visibility"></span></span>' +
            '<span class="icon-btn__label">Columns</span>';
        bar.appendChild(widget.wrapper);
        containerElement.parentNode.insertBefore(bar, containerElement);

        function buildPanel() {
            widget.panel.innerHTML = '';
            columns.forEach(function (col) {
                if (col.hideable === false || !col.field) return;
                var label = document.createElement('label');
                var cb = document.createElement('input');
                cb.type = 'checkbox';
                var column = table.getColumn(col.field);
                cb.checked = column.isVisible();
                cb.addEventListener('change', function () {
                    if (cb.checked) {
                        column.show();
                    } else {
                        column.hide();
                    }
                });
                label.appendChild(cb);
                label.appendChild(document.createTextNode(' ' + (col.title || col.field)));
                widget.panel.appendChild(label);
            });
        }

        widget.wrapper.addEventListener('panelopen', buildPanel);
    }

    global.initExcelStyleTable = function (elementSelector, dataScriptId, columns, options) {
        options = options || {};
        var data = JSON.parse(document.getElementById(dataScriptId).textContent);
        var excelStyleFilterEditor = makeExcelStyleFilterEditor({});

        columns.forEach(function (col) {
            if (col.headerFilter === false) return;
            col.headerFilter = excelStyleFilterEditor;
            col.headerFilterFunc = excelStyleFilterFunc;
        });

        var tableConfig = {
            data: data,
            layout: 'fitColumns',
            columns: columns,
            placeholder: options.placeholder || 'No data found.',
            // Opt-in per table (options.height, e.g. 'calc(100vh - 320px)',
            // same idea as the POS cart's own fixed height in sale_add.html).
            // Tabulator only switches to virtual-DOM row rendering (only the
            // rows actually scrolled into view get real DOM nodes) when the
            // table has a bounded height -- without one it falls back to
            // "auto" and renders every row for real, which is fine for a
            // few hundred rows but froze the browser tab outright at
            // 3,000-10,000 (confirmed live: even a trivial JS call timed
            // out for minutes). Left off by default -- a short table looks
            // worse with an empty scroll box than just growing with its
            // content, so this is only worth it for tables expected to
            // hold a lot of rows.
            height: options.height || undefined,
            // Drag a header left/right to reorder columns -- applies to
            // every table built through this helper (see CLAUDE.md table
            // convention). Tabulator keeps track of the new order itself
            // (table.getColumns() reflects it afterwards); nothing here
            // needs to persist it.
            movableColumns: true,
        };

        // Opt-in per table (options.persist: true, or a string to use as
        // the storage key instead of deriving one from elementSelector) --
        // remembers column order/visibility/width in localStorage across
        // visits, using Tabulator's own built-in persistence module. Off
        // by default: turning this on for every table at once would be a
        // bigger behavior change than whatever one table actually asked
        // for it.
        if (options.persist) {
            // `{columns: true}` looks equivalent to explicitly listing the
            // properties, but isn't: Tabulator's persistence module only
            // restores a saved property if that key ALREADY exists on the
            // column's original static definition object (it merges by
            // walking Object.keys(originalDef), not the saved data). Most
            // columns here never write `visible: true` explicitly (it's
            // just the default when the key is absent), so a saved
            // `visible: false` for one of THOSE columns was silently
            // dropped on reload -- confirmed live: exactly the columns
            // with no explicit `visible` in their definition were the ones
            // that wouldn't stay hidden after a refresh. Naming the
            // properties explicitly forces the merge to always consider
            // them, regardless of what the original definition happens to
            // declare.
            tableConfig.persistence = {columns: ['title', 'width', 'visible']};
            tableConfig.persistenceID = typeof options.persist === 'string'
                ? options.persist : elementSelector.replace(/^#/, '');
        }

        var table = new Tabulator(elementSelector, tableConfig);

        // `layout: 'fitColumns'` only fits column widths to the container
        // ONCE, at initial render -- this vendored Tabulator build does NOT
        // recalculate them on its own if the container is resized
        // afterwards (window resized, sidebar toggled, etc.). Without this,
        // a column built wide on a wide window stays that wide (and
        // overflows) after the window is narrowed, and vice versa -- the
        // wrap-instead-of-truncate CSS above can't help either, since the
        // column itself never actually gets narrower to force a wrap.
        // Confirmed live: table.redraw(true) is what actually recalculates
        // it; a plain window 'resize' event firing on its own does nothing.
        var resizeRedrawTimer = null;
        window.addEventListener('resize', function () {
            clearTimeout(resizeRedrawTimer);
            resizeRedrawTimer = setTimeout(function () {
                table.redraw(true);
                // redraw(true) recalculates COLUMN widths but not ROW
                // heights -- a row already rendered once (at the old
                // width) keeps its old cached height even if a cell's
                // text now needs to wrap onto another line at the new
                // (narrower) width, silently clipping that extra line
                // (overflow: hidden on the cell). normalizeHeight(true)
                // is the actual Tabulator internal call that re-measures
                // every row's real content height -- found by grepping
                // the vendored source (rowManager.normalizeHeight), not
                // documented as public API, but the only thing that
                // fixed this when tested live.
                table.rowManager.normalizeHeight(true);
            }, 150);
        });

        // A column can ALSO be resized directly -- dragging its header
        // border -- with the window never changing size at all, so no
        // 'resize' event fires for that case; needs its own listener.
        // 'columnResized' (fired only on drag-release) turned out
        // unreliable to hook (never fired in testing, possibly a Pointer
        // vs Mouse Events mismatch in this vendored build) -- 'columnWidth'
        // is a lower-level event that reliably fires for every width
        // change regardless of cause, confirmed live. Only normalizeHeight
        // is needed here (not a full redraw) -- the column's own new width
        // is already correct; redraw() would recalculate fitColumns and
        // fight the drag the user just did.
        var columnWidthTimer = null;
        table.on('columnWidth', function () {
            clearTimeout(columnWidthTimer);
            columnWidthTimer = setTimeout(function () {
                table.rowManager.normalizeHeight(true);
            }, 150);
        });

        if (options.columnChooser !== false) {
            table.on('tableBuilt', function () {
                addColumnChooser(document.querySelector(elementSelector), table, columns);
            });
        }

        return table;
    };

    /**
     * Moves "Export to Excel" (round green icon-btn, see style.css) into
     * the "Columns ▾" toolbar bar this file's own addColumnChooser()
     * inserts just above the grid, instead of leaving it as a separate
     * button up in the page header -- one shared implementation instead of
     * copy-pasting the same relocation snippet into every table's own
     * inline <script> (originally written per-page for Products/Categories
     * List, then asked to roll out to every Tabulator table in the app).
     *
     * Call AFTER initExcelStyleTable() returns (so this file's own
     * tableBuilt listener for the Columns chooser -- registered inside
     * initExcelStyleTable, before it returns -- runs first and the bar
     * already exists by the time this one fires).
     *
     * @param table          the Tabulator instance from initExcelStyleTable
     * @param cardSelector   CSS selector for the .data-table-card wrapping
     *                       this table (the toolbar bar is inserted as
     *                       that element's direct child)
     * @param filename       xlsx filename to download as
     * @param sheetName      sheet name inside the workbook
     * @param extraButtons   optional array of extra DOM elements (e.g. an
     *                       "Enable Edit" icon-btn) to insert into the same
     *                       bar, before the Export button
     * @param titleElement   optional DOM element (e.g. a page title +
     *                       popup-tip) inserted as a third child between
     *                       "Columns ▾" and the actions group -- lets a
     *                       page fold its own heading into this bar instead
     *                       of a separate block above the table (first
     *                       used by products_list.html, to reclaim the
     *                       vertical space a standalone header took up).
     */
    global.attachTableExportToToolbar = function (table, cardSelector, filename, sheetName, extraButtons, titleElement) {
        table.on('tableBuilt', function () {
            var card = document.querySelector(cardSelector);
            if (!card) return;
            var bar = card.querySelector(':scope > .mb-2');
            if (!bar) {
                // No "Columns ▾" bar to dock into -- happens on tables built
                // with `columnChooser: false` (e.g. sale_details.html, a
                // short fixed-column list with nothing worth hiding). Build
                // the same bar addColumnChooser() would have, so Export
                // still gets a home instead of silently never appearing.
                bar = document.createElement('div');
                bar.className = 'mb-2';
                card.insertBefore(bar, card.firstElementChild);
                // .data-table-card__toolbar is `justify-content:
                // space-between`, which spreads however many direct
                // children it has evenly -- fine with two (Columns ▾ +
                // this actions group), but this freshly-built bar only
                // ever gets the one actions group, and space-between pins
                // a lone child to the START (left) instead of the end.
                // Confirmed live on refund_new.html (Select All/Export
                // landed top-left instead of top-right). Explicit
                // flex-end only for this lone-child case -- the two-child
                // bar built by addColumnChooser() is untouched.
                bar.style.justifyContent = 'flex-end';
            }
            bar.classList.add('data-table-card__toolbar');

            if (titleElement) bar.appendChild(titleElement);

            // Grouped into ONE wrapper (not appended to the bar individually)
            // -- the bar is `justify-content: space-between`, which spreads
            // however many direct children it has evenly across its width.
            // With Columns ▾ as one child and Edit/Export appended as two
            // MORE separate children, Edit landed floating in the middle
            // instead of sitting next to Export (flagged live). A single
            // wrapper holding both keeps the bar down to two children --
            // Columns ▾ on the left, this whole group on the right -- with
            // Edit and Export adjacent to each other inside it.
            var actionsGroup = document.createElement('div');
            actionsGroup.className = 'data-table-card__toolbar-actions';

            (extraButtons || []).forEach(function (btn) { actionsGroup.appendChild(btn); });

            var exportBtn = document.createElement('button');
            exportBtn.type = 'button';
            exportBtn.className = 'icon-btn icon-btn--export icon-btn--sm';
            // A real (small, flat) Excel file icon, not the white-on-color
            // mask every other icon-btn glyph uses -- Excel's own green/
            // white is the whole point here (flagged live, twice: first
            // wanted the real Excel look instead of a generic download
            // arrow, then specifically the "open book" style -- green
            // cover with a bold white X on the left face, a spreadsheet
            // grid on the right face -- so it's redrawn to match that
            // shape, not just any green document). Inline multi-color SVG,
            // not a mask; the circle behind it stays light/white (see
            // .icon-btn--export in style.css) so these colors don't fight
            // a colored background the way they would on the solid green
            // "+" circle. The reference also had a separate arrow next to
            // the book -- left out here, there's no room for it at icon-
            // btn size and the hover label already says "Export to Excel".
            exportBtn.innerHTML =
                '<span class="icon-btn__icon">' +
                '<svg viewBox="0 0 30 24" width="27" height="21.6" xmlns="http://www.w3.org/2000/svg">' +
                '<polygon points="1,6 15,1 15,23 1,18" fill="#7DBB42"/>' +
                '<path d="M5,7 L11,17 M11,7 L5,17" stroke="#ffffff" stroke-width="2.3" stroke-linecap="round"/>' +
                '<rect x="14.5" y="3" width="14.5" height="18" rx="1.5" fill="#ffffff" stroke="#7DBB42" stroke-width="1.2"/>' +
                '<rect x="14.9" y="3.8" width="13.7" height="3.2" fill="#7DBB42"/>' +
                '<line x1="21.7" y1="7.5" x2="21.7" y2="20.2" stroke="#7DBB42" stroke-width="1"/>' +
                '<line x1="14.9" y1="11.2" x2="29" y2="11.2" stroke="#7DBB42" stroke-width="1"/>' +
                '<line x1="14.9" y1="14.6" x2="29" y2="14.6" stroke="#7DBB42" stroke-width="1"/>' +
                '<line x1="14.9" y1="18" x2="29" y2="18" stroke="#7DBB42" stroke-width="1"/>' +
                '</svg>' +
                '</span>' +
                '<span class="icon-btn__label">Export to Excel</span>';
            exportBtn.addEventListener('click', function () {
                table.download('xlsx', filename, {sheetName: sheetName});
            });
            actionsGroup.appendChild(exportBtn);

            bar.appendChild(actionsGroup);
        });
    };
})(window);
