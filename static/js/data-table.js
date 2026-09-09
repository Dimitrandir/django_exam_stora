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
                search.addEventListener('input', function () {
                    var q = search.value.toLowerCase();
                    rows.forEach(function (row) {
                        row.label.style.display = row.value.toLowerCase().indexOf(q) > -1 ? '' : 'none';
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

        var table = new Tabulator(elementSelector, {
            data: data,
            layout: 'fitColumns',
            columns: columns,
            placeholder: options.placeholder || 'No data found.',
        });

        if (options.columnChooser !== false) {
            table.on('tableBuilt', function () {
                addColumnChooser(document.querySelector(elementSelector), table, columns);
            });
        }

        return table;
    };
})(window);
