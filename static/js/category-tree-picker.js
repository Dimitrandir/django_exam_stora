// Shared "Choose a category" tree modal -- an alternative to typing in a
// category search box, for when it's easier to browse the hierarchy
// (Category.parent, arbitrary nesting depth) than to know the name up
// front. Builds its own modal DOM on first use (appended to <body>) so no
// template has to carry a copy of the markup -- window.openCategoryTreePicker(onSelect) is the entire public API.
(function () {
    // Same window.DataTableI18n blob data-table.js reads (see its own
    // comment) -- base.html renders it once with real {% translate %}
    // tags, every static .js file that needs a translated string just
    // reads a key off it, English literal as the fallback.
    var i18n = window.DataTableI18n || {};
    function t(key, fallback) {
        return i18n[key] || fallback;
    }

    var modalEl = null;

    function ensureModal() {
        if (modalEl) return modalEl;
        modalEl = document.createElement('div');
        modalEl.className = 'modal fade';
        modalEl.id = 'categoryTreeModal';
        modalEl.tabIndex = -1;
        modalEl.setAttribute('aria-hidden', 'true');
        modalEl.innerHTML =
            '<div class="modal-dialog">' +
                '<div class="modal-content">' +
                    '<div class="modal-header">' +
                        '<h5 class="modal-title">' + t('chooseACategory', 'Choose a category') + '</h5>' +
                        '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="' + t('close', 'Close') + '"></button>' +
                    '</div>' +
                    '<div class="modal-body category-tree-picker">' +
                        '<input type="text" class="form-control form-control-sm category-tree-picker__search" placeholder="' + t('filterCategories', 'Filter categories...') + '">' +
                        '<ul class="category-tree" role="tree"></ul>' +
                    '</div>' +
                '</div>' +
            '</div>';
        document.body.appendChild(modalEl);
        return modalEl;
    }

    function fetchCategories() {
        // NOT cached (used to be, for the page's whole lifetime) -- a
        // category created in the meantime (the "+ New category" popup,
        // which does NOT reload this page) stayed invisible in this tree
        // for the rest of the session, looking like "Browse is broken"
        // while the plain search box next to it (a fresh server query
        // every time) found the same new category fine. The view's own
        // docstring already says this is one cheap request regardless
        // (single digits to low dozens of categories), so there's no real
        // cost to just always asking the server instead of chasing down
        // every place a category can be created to invalidate a cache.
        return fetch('/products/category-tree/')
            .then(function (r) { return r.json(); })
            .then(function (data) { return data.results; });
    }

    function buildTree(categories) {
        var byId = {};
        categories.forEach(function (c) { byId[c.id] = {id: c.id, name: c.name, children: []}; });
        var roots = [];
        categories.forEach(function (c) {
            var node = byId[c.id];
            if (c.parent_id && byId[c.parent_id]) {
                byId[c.parent_id].children.push(node);
            } else {
                roots.push(node);
            }
        });
        function sortTree(nodes) {
            nodes.sort(function (a, b) { return a.name.localeCompare(b.name); });
            nodes.forEach(function (n) { sortTree(n.children); });
        }
        sortTree(roots);
        return roots;
    }

    function nodeMatchesQuery(node, query) {
        if (node.name.toLowerCase().indexOf(query) !== -1) return true;
        return node.children.some(function (c) { return nodeMatchesQuery(c, query); });
    }

    // While filtering, every branch that contains a match is force-expanded
    // -- otherwise a match nested a couple of levels deep would be hidden
    // behind a collapsed ancestor, defeating the point of searching at all.
    function renderTree(container, nodes, depth, query, onSelect) {
        nodes.forEach(function (node) {
            if (query && !nodeMatchesQuery(node, query)) return;
            var hasChildren = node.children.length > 0;

            var li = document.createElement('li');
            li.className = 'category-tree__item';

            var row = document.createElement('div');
            row.className = 'category-tree__row';
            row.style.paddingLeft = (depth * 1.25) + 'rem';

            var toggle = document.createElement('button');
            toggle.type = 'button';
            toggle.className = 'category-tree__toggle';
            var childList = null;
            if (hasChildren) {
                toggle.textContent = '›';
                if (query) toggle.classList.add('is-open');
                toggle.addEventListener('click', function (e) {
                    e.stopPropagation();
                    toggle.classList.toggle('is-open');
                    childList.classList.toggle('is-collapsed');
                });
            } else {
                toggle.style.visibility = 'hidden';
            }
            row.appendChild(toggle);

            var label = document.createElement('span');
            label.className = 'category-tree__label';
            label.textContent = node.name;
            row.appendChild(label);

            row.addEventListener('click', function () { onSelect(node); });
            li.appendChild(row);

            if (hasChildren) {
                childList = document.createElement('ul');
                childList.className = 'category-tree';
                if (!query) childList.classList.add('is-collapsed');
                renderTree(childList, node.children, depth + 1, query, onSelect);
                li.appendChild(childList);
            }

            container.appendChild(li);
        });
    }

    // onSelect(category) -- category is {id, name}. Called after the modal
    // is already hidden, so it's safe to synchronously update whatever
    // triggered the picker (a search box's value + hidden field, etc.).
    // options.categories -- an already-available [{id, name, parent_id}]
    // list to build the tree from instead of fetching every category from
    // the server -- for a caller that only wants to offer a RESTRICTED
    // subset (e.g. the POS shortcut picker only offers categories flagged
    // show_on_pos and not already pinned, a filter the server endpoint
    // knows nothing about).
    // options.excludeIds -- category ids to leave out entirely (and, since
    // buildTree falls a node with a missing/excluded parent back to being
    // its own root, this naturally also un-nests anything excluded without
    // orphaning its own children) -- for the Category form's own "Parent"
    // field, where a category can't become its own ancestor.
    // Re-entrancy guard -- root cause of "бутона се натиска но нищо не
    // изкача", finally pinned down live over Claude in Chrome by
    // dispatching two .click()s in the same tick (standing in for
    // whatever double-fires a single real click on the user's hardware --
    // never confirmed which, but the broken END STATE is 100%
    // reproducible this way): opening the picker used to dispose() any
    // existing Modal instance and build a fresh one every call, so a
    // second call arriving while the first is still mid-transition tears
    // down an instance Bootstrap hasn't finished setting up yet --
    // confirmed live that this leaves the element permanently stuck at
    // display:none/no .show class, with no error anywhere, forever (not
    // just delayed). A plain getOrCreateInstance().show() doesn't
    // double-dispose, but was tried first and rejected for a DIFFERENT
    // live symptom (heavy repeated testing in one page session eventually
    // left the single long-lived instance stuck _isTransitioning=true,
    // silently no-op-ing every later .show() on it) -- this flag instead
    // makes a second call while one is already opening a harmless no-op,
    // without reusing a possibly-stuck instance: the SAME instance stays
    // in charge of its own transition start-to-finish, and the very next
    // legitimate click (after this one finishes opening, or after it's
    // closed) is free to dispose and build fresh again.
    var isOpening = false;

    window.openCategoryTreePicker = function (onSelect, options) {
        if (isOpening) return;
        isOpening = true;

        options = options || {};
        var modal = ensureModal();
        var treeEl = modal.querySelector('.category-tree');
        var searchEl = modal.querySelector('.category-tree-picker__search');

        function renderWithFilter() {
            var query = searchEl.value.trim().toLowerCase();
            var source = options.categories ? Promise.resolve(options.categories) : fetchCategories();
            source.then(function (categories) {
                if (options.excludeIds && options.excludeIds.length) {
                    var excludeSet = new Set(options.excludeIds.map(String));
                    categories = categories.filter(function (c) { return !excludeSet.has(String(c.id)); });
                }
                treeEl.innerHTML = '';
                renderTree(treeEl, buildTree(categories), 0, query, function (node) {
                    bootstrap.Modal.getInstance(modal).hide();
                    onSelect(node);
                });
            }).catch(function (err) {
                // Previously uncaught -- a failed fetch (network hiccup,
                // session expired, a malformed category breaking buildTree)
                // left the modal open but silently empty forever, with
                // nothing in the console to explain why "Browse" looked
                // broken ("бутон брауз не работи", flagged live but not yet
                // reproduced). Doesn't fix whatever the root cause turns out
                // to be, but turns a silent dead end into a visible one, and
                // a retry (closing/reopening the modal) now has a chance to
                // recover instead of permanently showing a stale empty tree.
                console.error('Category tree picker failed to render:', err);
                treeEl.innerHTML = '';
            });
        }

        searchEl.value = '';
        searchEl.oninput = renderWithFilter;
        renderWithFilter();
        // Lazy reference -- bootstrap.bundle.min.js loads after the content
        // block in base.html, same reasoning as the POS resizer's toggle
        // button (see CLAUDE.md "Технически капани"). Dispose any existing
        // instance first (isOpening above already ruled out "one's still
        // mid-transition") so a stale instance from an EARLIER, fully-
        // finished cycle never lingers either.
        //
        // backdrop: 'static' -- a click on the dark overlay no longer
        // closes the picker (only the X button, Escape, or actually
        // picking a category do) -- unrelated hardening, asked for
        // alongside the bug above.
        var existingInstance = bootstrap.Modal.getInstance(modal);
        if (existingInstance) existingInstance.dispose();
        var instance = new bootstrap.Modal(modal, {backdrop: 'static'});
        modal.addEventListener('shown.bs.modal', function onShown() {
            modal.removeEventListener('shown.bs.modal', onShown);
            isOpening = false;
        });
        // Safety net in case 'shown.bs.modal' never fires for some reason
        // (e.g. the user navigates away mid-transition) -- without this,
        // a single failed open would brick the picker for the rest of the
        // page's life, trading one rare bug for a worse one. Self-removes
        // like the listener above, so these don't pile up across repeated
        // opens of the same long-lived modal element.
        modal.addEventListener('hidden.bs.modal', function onHidden() {
            modal.removeEventListener('hidden.bs.modal', onHidden);
            isOpening = false;
        });
        instance.show();
    };
})();
