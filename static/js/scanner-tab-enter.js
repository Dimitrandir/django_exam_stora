// Barcode scanners that end a code with Tab instead of Enter (the Zebra
// DS2208 at one shop, confirmed live with a keydown log: fast digits, then
// Tab) -- every scan path in the app (POS search, the deliveries grid, the
// "scan anywhere" listeners, the product's barcode rows) waits for Enter,
// so with Tab the code just sat there and the cursor moved on.
//
// Turns such a Tab into an Enter, page-wide: when Tab comes right after a
// fast burst of 4+ characters (each under 30ms apart -- the same scanner
// threshold as looksLikeScannerInput), the Tab is swallowed and an Enter
// keydown is sent to whatever has focus instead. A person pressing Tab
// never qualifies (nobody types 4 characters that fast), so normal Tab
// navigation is untouched.
//
// Listens on `window` in the capture phase, which runs before every
// document-level listener -- the scan-anywhere listeners must never see
// the Tab (it would reset their buffer), only the Enter that replaces it.
// The synthetic Enter runs the page's own Enter handlers; it does NOT
// trigger browser defaults like submitting a form.
(function () {
    var SCANNER_GAP_MS = 30;
    var MIN_CODE_LENGTH = 4;
    var burstLength = 0;
    var lastKeyTime = 0;

    window.addEventListener('keydown', function (e) {
        if (!e.isTrusted) return;
        // Modifiers and Ctrl/Alt/Meta combos (e.g. the QW2100's Ctrl+B
        // "start of text" prefix) are not part of the code.
        if (e.ctrlKey || e.altKey || e.metaKey ||
                ['Control', 'Alt', 'Shift', 'Meta', 'AltGraph'].indexOf(e.key) !== -1) {
            return;
        }
        var now = performance.now();
        var gap = now - lastKeyTime;
        lastKeyTime = now;

        if (e.key === 'Tab') {
            var isScan = !e.shiftKey && burstLength >= MIN_CODE_LENGTH && gap < SCANNER_GAP_MS;
            burstLength = 0;
            if (!isScan) return;
            e.preventDefault();
            e.stopImmediatePropagation();
            var target = document.activeElement || document.body;
            target.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true,
            }));
            return;
        }

        if (e.key.length === 1) {
            burstLength = (gap < SCANNER_GAP_MS || burstLength === 0) ? burstLength + 1 : 1;
        } else {
            burstLength = 0;
        }
    }, true);
})();
