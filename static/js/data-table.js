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
    function closeAllPanels() {
        document.querySelectorAll('.excel-filter-panel.is-open').forEach(function (panel) {
            panel.classList.remove('is-open');
        });
    }

    document.addEventListener('click', closeAllPanels);
    window.addEventListener('scroll', closeAllPanels, true);
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
                wrapper.dispatchEvent(new Event('panelopen'));
            }
        });
        panel.addEventListener('click', function (e) { e.stopPropagation(); });

        wrapper.appendChild(toggle);
        return {wrapper: wrapper, toggle: toggle, panel: panel};
    }

    function excelStyleFilterEditor(cell, onRendered, success) {
        var column = cell.getColumn();
        var field = column.getField();
        var widget = createFloatingToggle('▾', 'Filter');

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
                success(checked.length === unique.length ? '' : checked);
            }

            var search = document.createElement('input');
            search.type = 'text';
            search.placeholder = 'Search...';
            search.className = 'excel-filter-search';
            widget.panel.appendChild(search);

            var allLabel = document.createElement('label');
            var allCb = document.createElement('input');
            allCb.type = 'checkbox';
            allCb.checked = true;
            allLabel.appendChild(allCb);
            allLabel.appendChild(document.createTextNode(' (All)'));
            widget.panel.appendChild(allLabel);

            unique.forEach(function (val) {
                var label = document.createElement('label');
                var cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = true;
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

        widget.wrapper.addEventListener('panelopen', buildPanel);
        onRendered(function () {});
        return widget.wrapper;
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
