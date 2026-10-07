/* Cloud STORA only (FISCAL_MODE=bridge) -- the browser carries fiscal
   receipts between the cloud and the till PC.

   The cloud server can't reach the fiscal printer in the shop. After a
   sale it hands the receipt's command list to the next page (base.html,
   #fiscal-bridge-job); this script POSTs it to fiscal_bridge/bridge.py
   running on this same computer, then reports the bridge's answer back to
   the cloud (fiscal_bridge_result view), which marks the sale PRINTED or
   FAILED. Texts and URLs come from #fiscal-bridge-config (context
   processor STORA.sales.context_processors.fiscal_bridge).

   Also exposed as window.StoraFiscalBridge for pages that print on demand
   (X/Z reports on fiscal_reports.html). */
(function () {
    'use strict';

    var configEl = document.getElementById('fiscal-bridge-config');
    if (!configEl) return;
    var config = JSON.parse(configEl.textContent);
    var msg = config.messages;

    function csrfToken() {
        var match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    var toastEl = null;
    var toastTimer = null;

    function showToast(text, kind) {
        if (!toastEl) {
            toastEl = document.createElement('div');
            toastEl.className = 'fiscal-toast';
            toastEl.setAttribute('role', 'status');
            // An error stays until clicked -- the cashier has to notice it.
            toastEl.addEventListener('click', function () { toastEl.hidden = true; });
            document.body.appendChild(toastEl);
        }
        clearTimeout(toastTimer);
        toastEl.textContent = text;
        toastEl.className = 'fiscal-toast fiscal-toast--' + kind;
        toastEl.hidden = false;
        if (kind === 'ok') {
            toastTimer = setTimeout(function () { toastEl.hidden = true; }, 4000);
        }
    }

    // Resolves to the bridge's own answer: {ok: true, results: [...]} or
    // {ok: false, error: '...'}. Never rejects -- a bridge that isn't
    // running becomes {ok: false} with the "unreachable" text.
    function runCommands(commands) {
        return fetch(config.bridgeUrl + '/run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({commands: commands}),
        })
            .then(function (response) { return response.json(); })
            .catch(function () { return {ok: false, error: msg.unreachable}; });
    }

    function reportResult(job, outcome) {
        return fetch(config.resultUrl, {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
            body: JSON.stringify({
                kind: job.kind,
                id: job.id,
                ok: !!outcome.ok,
                error: outcome.error || '',
                results: outcome.results || [],
            }),
        }).catch(function () { /* stays PENDING -- Retry Fiscal Print covers it */ });
    }

    function runJob(job) {
        // While this receipt is printing, a Retry click would print it twice.
        document.querySelectorAll('.fiscal-retry-form').forEach(function (form) { form.style.display = 'none'; });
        showToast(msg.printing, 'info');
        return runCommands(job.commands).then(function (outcome) {
            return reportResult(job, outcome).then(function () {
                if (outcome.ok) {
                    showToast(msg.printed, 'ok');
                } else {
                    showToast(msg.failed + ' ' + (outcome.error || ''), 'error');
                }
                // On a details page the status shown is now out of date.
                if (job.reload) window.location.reload();
                return outcome;
            });
        });
    }

    window.StoraFiscalBridge = {runCommands: runCommands, runJob: runJob, showToast: showToast, messages: msg};

    var jobEl = document.getElementById('fiscal-bridge-job');
    if (jobEl) runJob(JSON.parse(jobEl.textContent));
})();
