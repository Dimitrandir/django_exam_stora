// Shared "Choose a category" tree modal -- an alternative to typing in a
// category search box, for when it's easier to browse the hierarchy
// (Category.parent, arbitrary nesting depth) than to know the name up
// front. Builds its own modal DOM on first use (appended to <body>) so no
// template has to carry a copy of the markup -- window.openCategoryTreePicker(onSelect) is the entire public API.
(function () {
    var cachedCategories = null;
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
                        '<h5 class="modal-title">Choose a category</h5>' +
                        '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
                    '</div>' +
                    '<div class="modal-body category-tree-picker">' +
                        '<input type="text" class="form-control form-control-sm category-tree-picker__search" placeholder="Filter categories...">' +
                        '<ul class="category-tree" role="tree"></ul>' +
                    '</div>' +
                '</div>' +
            '</div>';
        document.body.appendChild(modalEl);
        return modalEl;
    }

    function fetchCategories() {
        if (cachedCategories) return Promise.resolve(cachedCategories);
        return fetch('/products/category-tree/')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                cachedCategories = data.results;
                return cachedCategories;
            });
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
    window.openCategoryTreePicker = function (onSelect, options) {
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
            });
        }

        searchEl.value = '';
        searchEl.oninput = renderWithFilter;
        renderWithFilter();
        // Lazy reference -- bootstrap.bundle.min.js loads after the content
        // block in base.html, same reasoning as the POS resizer's toggle
        // button (see CLAUDE.md "Технически капани").
        bootstrap.Modal.getOrCreateInstance(modal).show();
    };
})();
