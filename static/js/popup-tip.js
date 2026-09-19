// Shared "Popup Tip" widget (NN/g pattern: a small "?" next to a field
// label that reveals its explanation in a speech-bubble on demand, instead
// of permanent text under the field taking up room whether anyone needs it
// or not). One delegated listener handles every .popup-tip on the page --
// works for tips added after load too (e.g. a formset's "+ Add another"
// row), no per-instance wiring needed.
//
// Click/tap to open, not hover -- this app is touch-first (see the POS
// screen conventions elsewhere), and hover never fires on a touchscreen.
(function () {
    function closeAll(except) {
        document.querySelectorAll('.popup-tip.is-open').forEach(function (tip) {
            if (tip !== except) tip.classList.remove('is-open');
        });
    }

    document.addEventListener('click', function (e) {
        var trigger = e.target.closest('.popup-tip__trigger');
        if (trigger) {
            var tip = trigger.closest('.popup-tip');
            var wasOpen = tip.classList.contains('is-open');
            closeAll();
            tip.classList.toggle('is-open', !wasOpen);
            e.preventDefault();
            return;
        }
        if (!e.target.closest('.popup-tip__bubble')) closeAll();
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeAll();
    });
})();
