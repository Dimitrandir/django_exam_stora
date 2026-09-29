// Global on-screen keyboard -- a Windows touchscreen till doesn't pop up its
// own on-screen keyboard on tapping a text field the way Android does (the
// reason this exists at all: confirmed live, 2026-09-29). "Touch Mode"
// (navbar toggle, persisted per browser/device in localStorage) makes ANY
// qualifying text field anywhere in the app show this keyboard the instant
// it's focused -- a single delegated `focusin` listener, so a brand new
// field added on some future page needs zero extra wiring to get this for
// free, including a Tabulator cell editor's dynamically-created <input>.
//
// This is the SHARED version of the keyboard sale_add.html built first
// (bespoke, page-specific) and refund_find.html then copy-pasted with
// per-field target-tracking bolted on -- extracted here instead of a third
// copy, so every OTHER page's text fields (Products/Deliveries/Revisions
// search boxes, the login form, ...) get the same capability with no
// per-page code at all. sale_add.html's own search box and the checkout
// modal's Paid/Card fields keep their own existing, already-tuned
// keyboard/keypad untouched (see their own `data-osk-skip` attribute) --
// this module skips any field carrying that attribute, so the two systems
// never show two keyboards stacked on top of each other for the same field.
//
// A page can still force this keyboard open for one specific field
// regardless of whether Touch Mode is on, via `OnscreenKeyboard.toggleFor
// (inputEl)` -- refund_find.html's own "⌨ Show keyboard" button uses this.
(function () {
    var TOUCH_MODE_KEY = 'stora_touch_mode';

    var KEYBOARD_LAYOUTS = {
        bg: [
            ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
            ['я', 'в', 'е', 'р', 'т', 'ъ', 'у', 'и', 'о', 'п'],
            ['а', 'с', 'д', 'ф', 'г', 'х', 'й', 'к', 'л'],
            ['з', 'ь', 'ц', 'ж', 'б', 'н', 'м'],
            ['ч', 'ш', 'щ', 'ю'],
        ],
        en: [
            ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
            ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
            ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
            ['z', 'x', 'c', 'v', 'b', 'n', 'm'],
        ],
    };

    // Types this makes no sense for at all -- a native control already
    // handles them fine on touch (date picker, checkbox, select, ...).
    var EXCLUDED_TYPES = ['date', 'datetime-local', 'month', 'week', 'time',
        'checkbox', 'radio', 'hidden', 'file', 'color', 'range',
        'submit', 'button', 'reset', 'image'];

    var panel = null;
    var activeInput = null;
    var keyboardLanguage = 'bg';

    function isTouchModeOn() {
        return localStorage.getItem(TOUCH_MODE_KEY) === '1';
    }

    function setTouchMode(on) {
        localStorage.setItem(TOUCH_MODE_KEY, on ? '1' : '0');
        document.querySelectorAll('.touch-mode-toggle-btn').forEach(function (btn) {
            btn.classList.toggle('is-active', on);
        });
        if (!on) hideKeyboard();
    }

    function isQualifyingField(el) {
        if (!el || !el.tagName) return false;
        if (el.hasAttribute('data-osk-skip')) return false;
        if (el.disabled || el.readOnly) return false;
        if (el.tagName === 'TEXTAREA') return true;
        if (el.tagName !== 'INPUT') return false;
        var type = (el.type || 'text').toLowerCase();
        return EXCLUDED_TYPES.indexOf(type) === -1;
    }

    function isNumericField(el) {
        var type = (el.type || '').toLowerCase();
        var mode = (el.getAttribute('inputmode') || '').toLowerCase();
        return type === 'number' || mode === 'decimal' || mode === 'numeric';
    }

    function buildPanel() {
        panel = document.createElement('div');
        panel.className = 'sale-keyboard sale-keyboard--floating';
        panel.style.display = 'none';
        document.body.appendChild(panel);
    }

    // Inserts at the current cursor position (not just appended) -- matches
    // how typing on a real keyboard behaves if the cashier taps back into
    // the middle of what they've already typed. Same as the two bespoke
    // keyboards this replaces.
    function insertAtCursor(text) {
        if (!activeInput) return;
        var start = activeInput.selectionStart ?? activeInput.value.length;
        var end = activeInput.selectionEnd ?? activeInput.value.length;
        activeInput.value = activeInput.value.slice(0, start) + text + activeInput.value.slice(end);
        activeInput.setSelectionRange(start + text.length, start + text.length);
        activeInput.focus();
        activeInput.dispatchEvent(new Event('input', {bubbles: true}));
    }

    function backspace() {
        if (!activeInput) return;
        var start = activeInput.selectionStart ?? activeInput.value.length;
        var end = activeInput.selectionEnd ?? activeInput.value.length;
        if (start === end && start > 0) {
            activeInput.value = activeInput.value.slice(0, start - 1) + activeInput.value.slice(end);
            activeInput.setSelectionRange(start - 1, start - 1);
        } else {
            activeInput.value = activeInput.value.slice(0, start) + activeInput.value.slice(end);
            activeInput.setSelectionRange(start, start);
        }
        activeInput.focus();
        activeInput.dispatchEvent(new Event('input', {bubbles: true}));
    }

    function makeKey(label, onClick, extraClass) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'sale-keyboard-key' + (extraClass ? ' ' + extraClass : '');
        btn.textContent = label;
        btn.addEventListener('click', onClick);
        return btn;
    }

    function makeRow(keys) {
        var rowEl = document.createElement('div');
        rowEl.className = 'sale-keyboard-row';
        keys.forEach(function (key) {
            rowEl.appendChild(makeKey(key, function () { insertAtCursor(key); }));
        });
        return rowEl;
    }

    function renderKeyboard() {
        panel.innerHTML = '';

        if (isNumericField(activeInput)) {
            panel.appendChild(makeRow(['1', '2', '3', '4', '5', '6', '7', '8', '9', '0']));
            var controls = document.createElement('div');
            controls.className = 'sale-keyboard-row';
            controls.appendChild(makeKey('.', function () { insertAtCursor('.'); }));
            controls.appendChild(makeKey('←', backspace, 'sale-keyboard-key--backspace'));
            controls.appendChild(makeKey('↵', submitActiveInput, 'sale-keyboard-key--enter'));
            panel.appendChild(controls);
            return;
        }

        KEYBOARD_LAYOUTS[keyboardLanguage].forEach(function (rowKeys) {
            panel.appendChild(makeRow(rowKeys));
        });

        var controlsRow = document.createElement('div');
        controlsRow.className = 'sale-keyboard-row';

        var langBtn = makeKey(keyboardLanguage === 'bg' ? 'EN' : 'BG', function () {
            keyboardLanguage = keyboardLanguage === 'bg' ? 'en' : 'bg';
            renderKeyboard();
            if (activeInput) activeInput.focus();
        }, 'sale-keyboard-key--lang');
        langBtn.title = 'Switch keyboard language';
        controlsRow.appendChild(langBtn);

        controlsRow.appendChild(makeKey('Space', function () { insertAtCursor(' '); }, 'sale-keyboard-key--space'));
        controlsRow.appendChild(makeKey('←', backspace, 'sale-keyboard-key--backspace'));
        controlsRow.appendChild(makeKey('↵', submitActiveInput, 'sale-keyboard-key--enter'));

        panel.appendChild(controlsRow);
    }

    // Reuses whatever real Enter-key handling the field already has (e.g.
    // a search box's own keydown listener calling preventDefault() to do
    // its own thing) -- dispatchEvent's return value is false exactly
    // when something called preventDefault() on it, same signal a real
    // keypress's default action would check. If nothing handled it, falls
    // back to actually submitting the field's own <form> -- a synthetic
    // KeyboardEvent does NOT trigger a browser's native "Enter submits
    // the form" behavior the way a real, trusted keypress does, so a
    // plain GET-search field (no listener of its own, e.g. refund_find.html)
    // would otherwise do nothing at all when this key is tapped.
    function submitActiveInput() {
        if (!activeInput) return;
        activeInput.focus();
        var notPrevented = activeInput.dispatchEvent(
            new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})
        );
        if (notPrevented && activeInput.form) activeInput.form.requestSubmit();
    }

    function showFor(input) {
        if (!panel) buildPanel();
        activeInput = input;
        renderKeyboard();
        panel.style.display = '';
    }

    function hideKeyboard() {
        if (panel) panel.style.display = 'none';
        activeInput = null;
    }

    function isShowing() {
        return !!panel && panel.style.display !== 'none';
    }

    // Explicit per-field toggle, independent of the Touch Mode setting --
    // for a page that wants its own always-available keyboard button on a
    // specific field (see refund_find.html) without requiring the cashier
    // to have Touch Mode switched on globally first.
    function toggleFor(input) {
        if (isShowing() && activeInput === input) {
            hideKeyboard();
        } else {
            showFor(input);
            input.focus();
        }
    }

    document.addEventListener('focusin', function (e) {
        if (!isTouchModeOn()) return;
        if (panel && panel.contains(e.target)) return; // a key button itself
        if (isQualifyingField(e.target)) {
            showFor(e.target);
        } else if (isShowing()) {
            hideKeyboard();
        }
    });

    // Tapping something that isn't a text field and isn't the keyboard
    // itself closes it -- covers Touch Mode being off (focusin above never
    // ran) but a page forced the keyboard open via toggleFor(). A tap on
    // ANOTHER qualifying field is handled by focusin above instead (moves
    // the keyboard there), not here.
    document.addEventListener('mousedown', function (e) {
        if (!isShowing()) return;
        if (panel.contains(e.target)) return;
        if (e.target === activeInput) return;
        if (!isQualifyingField(e.target)) hideKeyboard();
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && isShowing()) hideKeyboard();
    });

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.touch-mode-toggle-btn').forEach(function (btn) {
            btn.classList.toggle('is-active', isTouchModeOn());
            btn.addEventListener('click', function () { setTouchMode(!isTouchModeOn()); });
        });
    });

    window.OnscreenKeyboard = {
        isTouchModeOn: isTouchModeOn,
        setTouchMode: setTouchMode,
        toggleFor: toggleFor,
        hide: hideKeyboard,
    };
})();
