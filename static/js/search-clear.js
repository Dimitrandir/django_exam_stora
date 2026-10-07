// Adds a small "x" clear button to every live-search input in the app,
// once it has something typed in it -- asked for live, project-wide
// ("в цялата програма ... да се появява един х лесно да триеш написаното").
//
// Scope, two kinds of input:
// 1. Any <input> that sits directly before a `.global-search-results`
//    dropdown -- that DOM pairing (input, then its own results box right
//    after it) is already the universal marker for "this is a live search
//    box" across the whole app (supplier search, product search, category
//    search, the navbar's global search, batch search, ...), regardless of
//    which visual class a given page happens to use for the input itself
//    (`.pill-input` in most places, plain Bootstrap `.form-control` in a
//    couple of older screens like order_new.html) -- so this one file covers
//    all of them without editing every template individually.
// 2. Any plain (non-search) <input class="clearable-input"> -- opted in
//    explicitly per field, for a value that can arrive pre-filled from
//    somewhere other than typing (e.g. the product form's Name field,
//    carried over by "Save and New") where a one-tap clear matters just as
//    much as it does for a search box.
//
// Loaded once from templates/base.html (like popup-tip.js), not per-page.
(function () {
    // Every one of these pages picks a search result by setting
    // `input.value = name` directly in a click handler (see supplier/
    // product/category search wiring throughout the app) -- none of them
    // dispatch a real 'input' event afterward, so a listener alone misses
    // "value changed by clicking a result" and only catches actual typing.
    // A light poll is simpler and more robust than patching every one of
    // those click handlers individually: it catches every way a field's
    // value can change (typed, picked, autofilled, restored by browser
    // back/forward) for the trivial cost of checking a handful of
    // `.value.length`s a few times a second.
    var managed = [];

    function refreshAll() {
        managed.forEach(function (pair) {
            pair.clearBtn.classList.toggle('is-visible', pair.input.value.length > 0);
        });
    }

    // Shared by both passes below -- builds the button, wires it up, and
    // returns it (or null if this input already has one). Caller decides
    // where the button ends up positioned (a "Browse" pill next to a
    // search box needs to be dodged; a plain field doesn't).
    function makeClearButton(input, parent) {
        if (input.dataset.hasClearBtn) return null;
        input.dataset.hasClearBtn = '1';

        // Every search box this targets already happens to sit inside a
        // positioned wrapper (`.pill-search-wrapper`, or a `.position-
        // relative` filter field) in practice, but forcing it here too
        // means this still works correctly even on a future page that
        // forgets to add that class itself.
        if (getComputedStyle(parent).position === 'static') {
            parent.style.position = 'relative';
        }

        var clearBtn = document.createElement('button');
        clearBtn.type = 'button';
        clearBtn.className = 'search-clear-btn';
        // Same window.DataTableI18n blob data-table.js reads (see its
        // own comment) -- base.html renders it once with real
        // {% translate %} tags, every static .js file that needs a
        // translated string just reads a key off it.
        clearBtn.setAttribute('aria-label', (window.DataTableI18n && window.DataTableI18n.clear) || 'Clear');
        clearBtn.textContent = '×';
        parent.insertBefore(clearBtn, input.nextSibling);

        function refreshVisibility() {
            clearBtn.classList.toggle('is-visible', input.value.length > 0);
        }

        input.addEventListener('input', refreshVisibility);
        refreshVisibility();
        managed.push({input: input, clearBtn: clearBtn});

        clearBtn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            input.value = '';
            // A real 'input' event, not just clearing .value -- every
            // search box's own listener is wired to 'input' already
            // (re-running the search, closing its dropdown, clearing
            // whatever hidden id field it tracks), so this one
            // dispatch is all it takes to hook into each page's
            // existing behavior without this file knowing anything
            // about it.
            input.dispatchEvent(new Event('input', {bubbles: true}));
            refreshVisibility();
            input.focus();
        });

        return clearBtn;
    }

    function attachClearButtons() {
        document.querySelectorAll('.global-search-results').forEach(function (resultsBox) {
            // Usually the input immediately before this results box, but
            // the category picker's "Browse" button also sits in between
            // the two (input, then Browse, then the results box) -- walk
            // back past anything that isn't the input itself instead of
            // assuming it's the literal previous sibling.
            var input = resultsBox.previousElementSibling;
            while (input && input.tagName !== 'INPUT') {
                input = input.previousElementSibling;
            }
            if (!input) return;
            if (input.type !== 'text' && input.type !== 'search') return;

            var parent = input.parentElement;
            var clearBtn = makeClearButton(input, parent);
            if (!clearBtn) return;

            // A "Browse" pill (category pickers, see
            // .pill-search-wrapper__browse-btn) already occupies the
            // field's own right edge -- sit just to its left instead of on
            // top of it. Measured off the real button, not a guessed
            // constant, so it still lines up if that label's width ever
            // changes.
            var browseBtn = parent.querySelector('.pill-search-wrapper__browse-btn');
            if (browseBtn) {
                clearBtn.style.right = (browseBtn.offsetWidth + 10) + 'px';
                input.classList.add('has-clear-btn--browse');
            } else {
                input.classList.add('has-clear-btn');
            }
        });

        // Plain fields that aren't a live search box at all, explicitly
        // opted in via this class -- e.g. the product form's Name field,
        // prefilled by "Save and New" (ProductCreateView.form_valid) so a
        // batch of similar products doesn't need retyping a name that
        // mostly repeats. No Browse-pill dodging needed here, just the
        // plain right-edge placement.
        document.querySelectorAll('.clearable-input').forEach(function (input) {
            var clearBtn = makeClearButton(input, input.parentElement);
            if (clearBtn) input.classList.add('has-clear-btn');
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', attachClearButtons);
    } else {
        attachClearButtons();
    }

    setInterval(refreshAll, 250);
})();
