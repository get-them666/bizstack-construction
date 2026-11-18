/* Outbound calling — owner portal client.
 *
 * Why this is a separate file
 * ---------------------------
 * The call button and the status cell are rendered by the leads board, but the
 * call itself is a POST that can take seconds (SignalWire dial, suppression
 * checks, daily caps). Inline handlers would have to duplicate the in-flight
 * guard and the polling setup on every page that shows a phone number. This
 * file owns both, once, and degrades to "nothing happens" when the markup is
 * absent.
 *
 * Expected markup (all optional; anything missing is skipped):
 *
 *   <button type="button" data-call-lead="123">Call</button>
 *   <span data-call-status="123">Not called</span>
 *   <p data-outbound-summary></p>
 *
 * `data-call-status` may hold the lead id (as above) or be a plain marker; if
 * it is a marker the lead id is read from the nearest `[data-lead-id]`, then
 * from the row's `[data-call-lead]`, then from a trailing number in its id.
 * A `[data-call-error]` element inside the row, when present, receives the
 * server's failure reason instead of an alert.
 *
 * API contract (owned by the backend):
 *   POST /api/outbound/call          {lead_id} -> {ok, call_sid?, reason?}
 *   GET  /api/outbound/status?lead_id=N -> {ok, status, last_at?}
 *   GET  /api/outbound/campaign      -> {ok, dialed_today, daily_cap, enabled, cold_calling}
 *
 * No globals: everything below stays inside the IIFE. Debug output is silent
 * unless `window.BZ_DEBUG` is truthy, because this ships to production.
 */
(function () {
    'use strict';

    var POLL_MS = 5000;             // status refresh cadence while the tab is visible
    var CAMPAIGN_MS = 30000;        // the campaign counters move at most this often
    var STATUS_TIMEOUT_MS = 8000;   // a hung GET must not wedge the next cycle
    var CALL_TIMEOUT_MS = 15000;    // dialling is slower than reading a status
    var ERROR_HIDE_MS = 10000;      // inline error text clears itself

    var STATUS_LABELS = {
        not_called: 'Not called',
        dialed: 'Dialed',
        in_progress: 'Ringing',
        answered: 'Answered',
        opted_out: 'Opted out'
    };
    // Same scale as the rest of the app: slate for idle, indigo in flight,
    // emerald for a human, rose for do-not-call. These classes already exist in
    // the committed Tailwind build; do not add arbitrary values here without
    // running `npm run css`.
    var STATUS_CLASSES = {
        not_called: 'text-slate-500',
        dialed: 'text-indigo-600',
        in_progress: 'text-indigo-600',
        answered: 'text-emerald-600',
        opted_out: 'text-rose-600'
    };
    var STATUS_COLOR_CLASSES = [
        'text-slate-400', 'text-slate-500', 'text-indigo-600',
        'text-emerald-600', 'text-rose-600'
    ];

    var pollTimer = null;
    var pollInFlight = false;
    var pollingHalted = false;        // set when the session is gone; only a reload recovers
    var campaignFetchedAt = 0;

    var callsInFlight = {};          // lead ids with a POST outstanding
    var statusPending = {};          // lead id -> AbortController (or true)
    var statusToken = {};            // lead id -> newest status request

    function debug() {
        if (!window.BZ_DEBUG) return;
        try {
            var args = Array.prototype.slice.call(arguments);
            args.unshift('[outbound-calls]');
            console.log.apply(console, args);
        } catch (e) { /* a console shim is not worth crashing over */ }
    }

    function readJson(res) {
        return res.json().then(function (data) { return data; }, function () { return null; });
    }

    // One place that sets credentials and an abort timer. A caller that needs
    // to cancel on visibilitychange passes its own controller.
    function fetchJson(url, options, timeoutMs, controller) {
        var opts = {};
        var key;
        options = options || {};
        for (key in options) {
            if (Object.prototype.hasOwnProperty.call(options, key)) opts[key] = options[key];
        }
        opts.credentials = 'same-origin';
        opts.headers = {};
        var headers = options.headers || {};
        for (key in headers) {
            if (Object.prototype.hasOwnProperty.call(headers, key)) opts.headers[key] = headers[key];
        }
        opts.headers.Accept = 'application/json';

        var timer = null;
        if (!controller && typeof AbortController !== 'undefined') {
            controller = new AbortController();
        }
        if (controller) {
            opts.signal = controller.signal;
            timer = window.setTimeout(function () {
                try { controller.abort(); } catch (e) { /* already settled */ }
            }, timeoutMs || STATUS_TIMEOUT_MS);
        }

        return fetch(url, opts).then(
            function (res) {
                if (timer) { window.clearTimeout(timer); timer = null; }
                return res;
            },
            function (err) {
                if (timer) { window.clearTimeout(timer); timer = null; }
                throw err;
            }
        );
    }

    function stoppedByRedirect(res) {
        // FastAPI returns 303 to /login when the session has gone. Continuing
        // to poll would fill the console with redirects for the rest of the visit.
        return !!(res && (res.redirected || res.status === 401 || res.status === 303));
    }

    // ── lead / element resolution ────────────────────────────────────────────

    function toLeadId(value) {
        if (value === null || value === undefined) return null;
        var s = String(value).trim();
        if (!/^\d+$/.test(s)) return null;
        var n = Number(s);
        return (Number.isSafeInteger(n) && n > 0) ? n : null;
    }

    // The status element can carry the id itself (data-call-status="12"), sit
    // inside a row that does (data-lead-id="12"), or share a row with the call
    // button. Accept all three so the template author does not have to guess.
    function statusLeadId(el) {
        if (!el) return null;

        var own = toLeadId(el.getAttribute('data-call-status'));
        if (own) return own;

        var explicit = toLeadId(el.getAttribute('data-lead-id'));
        if (explicit) return explicit;

        var holder = el.closest ? el.closest('[data-lead-id]') : null;
        if (holder) {
            var held = toLeadId(holder.getAttribute('data-lead-id'));
            if (held) return held;
        }

        var row = el.closest ? el.closest('tr, li, [data-lead-row], [data-call-row]') : null;
        if (row) {
            var button = row.querySelector('[data-call-lead]');
            var fromButton = button ? toLeadId(button.getAttribute('data-call-lead')) : null;
            if (fromButton) return fromButton;
        }

        // "call-status-12", "outbound-status-12" — a last resort for rows with
        // no data attributes at all.
        var idMatch = /(\d+)\s*$/.exec(el.id || '');
        if (idMatch) return toLeadId(idMatch[1]);

        return null;
    }

    function isRendered(el) {
        if (!el || !el.getClientRects) return false;
        return el.getClientRects().length > 0;
    }

    function statusElementsFor(leadId) {
        var found = [];
        var els = document.querySelectorAll('[data-call-status]');
        for (var i = 0; i < els.length; i++) {
            if (statusLeadId(els[i]) === leadId) found.push(els[i]);
        }
        return found;
    }

    // Walk out from the clicked control and take the first ancestor that holds
    // a status element. That keeps a card's status from being confused with a
    // neighbouring row's when neither carries a lead id.
    function findStatusNear(trigger, leadId) {
        // A data-call-lead placed on the row itself will contain its status.
        if (trigger.querySelector) {
            var inner = trigger.querySelectorAll('[data-call-status]');
            for (var j = 0; j < inner.length; j++) {
                if (statusLeadId(inner[j]) === leadId) return inner[j];
            }
            if (inner.length) return inner[0];
        }

        var node = trigger.parentElement;
        var depth = 0;
        while (node && node !== document.documentElement && depth < 12) {
            var candidates = node.querySelectorAll('[data-call-status]');
            if (candidates.length) {
                for (var i = 0; i < candidates.length; i++) {
                    if (statusLeadId(candidates[i]) === leadId) return candidates[i];
                }
                return candidates[0];
            }
            node = node.parentElement;
            depth++;
        }
        return null;
    }

    function findErrorEl(trigger, leadId) {
        var statusEl = findStatusNear(trigger, leadId);
        if (statusEl && statusEl.parentElement) {
            var near = statusEl.parentElement.querySelector('[data-call-error]');
            if (near) return near;
        }
        var row = trigger.closest ? trigger.closest('tr, li, [data-lead-row], [data-call-row]') : null;
        return row ? row.querySelector('[data-call-error]') : null;
    }

    // ── rendering ────────────────────────────────────────────────────────────

    // The documented enum is not_called|dialed|in_progress|answered|opted_out.
    // A new value must never leave the cell showing yesterday's text, so an
    // unrecognised status is humanised instead of ignored.
    function statusLabel(status) {
        if (STATUS_LABELS[status]) return STATUS_LABELS[status];
        var s = String(status || '').replace(/_/g, ' ').trim();
        if (!s) return '';
        return s.charAt(0).toUpperCase() + s.slice(1);
    }

    function applyStatus(el, status, lastAt) {
        if (!el) return;
        var label = statusLabel(status);
        if (!label) return;

        if (el.textContent !== label) el.textContent = label;
        el.setAttribute('data-status', status);
        // Wrapping a two-word status onto a second line is the one way this
        // cell can change the row height; keep it on one line.
        el.classList.add('whitespace-nowrap');

        for (var i = 0; i < STATUS_COLOR_CLASSES.length; i++) {
            el.classList.remove(STATUS_COLOR_CLASSES[i]);
        }
        el.classList.add(STATUS_CLASSES[status] || 'text-slate-500');

        var when = '';
        if (lastAt) {
            var date = new Date(String(lastAt));
            if (isNaN(date.getTime())) {
                when = String(lastAt);
            } else {
                try { when = date.toLocaleString(); } catch (e) { when = String(lastAt); }
            }
        }
        if (when) el.setAttribute('title', 'Last call: ' + when);
    }

    function hideError(trigger, leadId) {
        try {
            var inline = findErrorEl(trigger, leadId);
            if (!inline) return;
            inline.textContent = '';
            inline.hidden = true;
            inline.classList.add('hidden');
        } catch (e) { debug('could not clear error', e); }
    }

    function showFailure(trigger, leadId, message) {
        try {
            var inline = findErrorEl(trigger, leadId);
            if (inline) {
                inline.textContent = message;
                inline.hidden = false;
                inline.classList.remove('hidden');
                if (!inline.getAttribute('role')) inline.setAttribute('role', 'status');
                window.setTimeout(function () {
                    if (inline.textContent === message) {
                        inline.textContent = '';
                        inline.hidden = true;
                        inline.classList.add('hidden');
                    }
                }, ERROR_HIDE_MS);
                return;
            }
            window.alert(message);
        } catch (e) { debug('could not show failure', e); }
    }

    function formatCount(n) {
        try { return n.toLocaleString(); } catch (e) { return String(n); }
    }

    function toCount(value) {
        var n = typeof value === 'number' ? value : Number(value);
        if (!isFinite(n) || n < 0) return null;
        return Math.floor(n);
    }

    function renderCampaign(data) {
        if (!data || typeof data !== 'object' || data.ok === false) return;

        var els = document.querySelectorAll('[data-outbound-summary]');
        if (!els.length) return;

        var enabled = data.enabled !== false;
        var text;
        if (!enabled) {
            text = 'Outbound calling is turned off.';
        } else {
            var dialed = toCount(data.dialed_today);
            var cap = toCount(data.daily_cap);
            if (dialed !== null && cap !== null && cap > 0) {
                text = formatCount(dialed) + ' of ' + formatCount(cap) +
                    (cap === 1 ? ' call' : ' calls') + ' placed today';
            } else if (dialed !== null) {
                text = formatCount(dialed) + (dialed === 1 ? ' call' : ' calls') + ' placed today';
            } else {
                text = 'Outbound calling is on.';
            }
        }

        // cold_calling is a sentence, not a boolean ("DNC scrub 3 day(s) old").
        // It is context, so it goes in the tooltip rather than the count line.
        var cold = typeof data.cold_calling === 'string' ? data.cold_calling.trim() : '';

        for (var i = 0; i < els.length; i++) {
            if (els[i].textContent !== text) els[i].textContent = text;
            if (cold) els[i].setAttribute('title', cold);
        }
    }

    // ── campaign ─────────────────────────────────────────────────────────────

    function maybeRefreshCampaign(force) {
        var now = Date.now();
        if (!force && now - campaignFetchedAt < CAMPAIGN_MS) return;
        campaignFetchedAt = now;

        var request;
        try {
            request = fetchJson('/api/outbound/campaign', { method: 'GET' }, STATUS_TIMEOUT_MS);
        } catch (err) {
            debug('campaign could not start', err);
            return;
        }

        return request
            .then(function (res) {
                if (stoppedByRedirect(res)) { haltPolling(); return null; }
                return readJson(res).then(function (data) {
                    if (!res.ok || !data) {
                        debug('campaign failed', res.status);
                        return null;
                    }
                    renderCampaign(data);
                    return data;
                });
            })
            .catch(function (err) {
                // A failed count is not worth an alert; the next window retries.
                debug('campaign error', err);
                return null;
            });
    }

    // ── status polling ───────────────────────────────────────────────────────

    function refreshStatus(leadId, knownEls) {
        if (document.visibilityState !== 'visible') return Promise.resolve();

        var els = (knownEls && knownEls.length) ? knownEls : statusElementsFor(leadId);
        if (!els.length) return Promise.resolve();
        if (statusPending[leadId]) return Promise.resolve();

        var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        var handle = controller || true;
        statusPending[leadId] = handle;
        statusToken[leadId] = (statusToken[leadId] || 0) + 1;
        var token = statusToken[leadId];

        function release() {
            if (statusPending[leadId] === handle) delete statusPending[leadId];
        }

        var request;
        try {
            request = fetchJson(
                '/api/outbound/status?lead_id=' + encodeURIComponent(String(leadId)),
                { method: 'GET' },
                STATUS_TIMEOUT_MS,
                controller
            );
        } catch (err) {
            debug('status request could not start', err);
            release();
            return Promise.resolve();
        }

        return request.then(function (res) {
            if (stoppedByRedirect(res)) { haltPolling(); return null; }
            return readJson(res).then(function (data) {
                if (!res.ok || !data || data.ok === false) {
                    debug('status failed', leadId, res.status);
                    return;
                }
                if (statusToken[leadId] !== token) return; // a newer request won
                var status = String(data.status || '');
                for (var i = 0; i < els.length; i++) {
                    applyStatus(els[i], status, data.last_at);
                }
            });
        }).catch(function (err) {
            if (err && err.name === 'AbortError') return; // hidden tab or new request
            debug('status error', leadId, err);
        }).then(release, release);
    }

    function collectVisible() {
        var byLead = {};
        var els = document.querySelectorAll('[data-call-status]');
        for (var i = 0; i < els.length; i++) {
            if (!isRendered(els[i])) continue;
            var leadId = statusLeadId(els[i]);
            if (!leadId) continue;
            if (!byLead[leadId]) byLead[leadId] = [];
            byLead[leadId].push(els[i]);
        }
        return byLead;
    }

    function pollOnce() {
        if (document.visibilityState !== 'visible' || pollInFlight) return;

        pollInFlight = true;
        try {
            maybeRefreshCampaign(false);

            var byLead = collectVisible();
            var leads = Object.keys(byLead);
            var work = [];
            for (var i = 0; i < leads.length; i++) {
                work.push(refreshStatus(Number(leads[i]), byLead[leads[i]]));
            }

            Promise.all(work).then(function () {
                pollInFlight = false;
            }, function (err) {
                debug('poll tick', err);
                pollInFlight = false;
            });
        } catch (e) {
            // A malformed row must not wedge every future tick.
            debug('poll setup failed', e);
            pollInFlight = false;
        }
    }

    function startPolling() {
        if (pollingHalted) return;
        if (document.visibilityState !== 'visible') return;
        if (pollTimer) return;
        pollOnce();
        pollTimer = window.setInterval(pollOnce, POLL_MS);
    }

    function stopPolling() {
        if (pollTimer) {
            window.clearInterval(pollTimer);
            pollTimer = null;
        }
        // Cancel reads only. A POST already in flight is left alone: the dial
        // may have happened, and aborting it would not un-dial the phone.
        var key;
        for (key in statusPending) {
            if (!Object.prototype.hasOwnProperty.call(statusPending, key)) continue;
            var controller = statusPending[key];
            if (controller && typeof controller.abort === 'function') {
                try { controller.abort(); } catch (e) { /* settled */ }
            }
        }
        statusPending = {};
    }

    // A redirect to /login means the cookie is gone. Polling again on focus
    // would just replay the redirect; a reload after signing in is the cure.
    function haltPolling() {
        pollingHalted = true;
        stopPolling();
    }

    function onVisibilityChange() {
        if (document.visibilityState === 'visible') startPolling();
        else stopPolling();
    }

    // ── click-to-call ────────────────────────────────────────────────────────

    function markDialed(trigger, leadId) {
        hideError(trigger, leadId);
        var els = statusElementsFor(leadId);
        for (var i = 0; i < els.length; i++) applyStatus(els[i], 'dialed', null);
        // The backend records the call after returning; one quick refresh shows
        // "Ringing" without waiting for the next 5s tick.
        window.setTimeout(function () { refreshStatus(leadId); }, 2500);
    }

    function placeCall(trigger, leadId) {
        if (callsInFlight[leadId]) return Promise.resolve();
        callsInFlight[leadId] = true;

        var hadDisabledProp = 'disabled' in trigger;
        var wasDisabled = hadDisabledProp ? trigger.disabled === true : false;
        var hadAriaDisabled = trigger.getAttribute('aria-disabled');
        var addedOpacity = !trigger.classList.contains('opacity-50');

        function restore() {
            delete callsInFlight[leadId];
            try {
                if (hadDisabledProp && !wasDisabled) trigger.disabled = false;
                if (hadAriaDisabled === null) trigger.removeAttribute('aria-disabled');
                else trigger.setAttribute('aria-disabled', hadAriaDisabled);
                trigger.removeAttribute('aria-busy');
                if (addedOpacity) trigger.classList.remove('opacity-50');
            } catch (e) { debug('could not restore trigger', e); }
        }

        try {
            if (hadDisabledProp) trigger.disabled = true;
            trigger.setAttribute('aria-disabled', 'true');
            trigger.setAttribute('aria-busy', 'true');
            if (addedOpacity) trigger.classList.add('opacity-50');
        } catch (e) { debug('could not disable trigger', e); }

        var request;
        try {
            request = fetchJson('/api/outbound/call', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lead_id: leadId })
            }, CALL_TIMEOUT_MS);
        } catch (err) {
            // A synchronous failure (fetch missing, bad URL) must still put the
            // button back; otherwise it stays disabled for the whole visit.
            debug('call request could not start', err);
            showFailure(trigger, leadId, 'Network error — the call was not placed.');
            restore();
            return Promise.resolve();
        }

        return request.then(function (res) {
            if (stoppedByRedirect(res)) {
                haltPolling();
                showFailure(trigger, leadId, 'Your session expired — reload the page and sign in.');
                return null;
            }
            return readJson(res).then(function (data) {
                if (!res.ok || !data || data.ok === false) {
                    var reason = (data && typeof data.reason === 'string') ? data.reason.trim() : '';
                    if (!reason) reason = 'The call could not be placed (' + res.status + ').';
                    showFailure(trigger, leadId, reason);
                    return null;
                }
                debug('call placed', leadId, data.call_sid || '');
                markDialed(trigger, leadId);
                return data;
            });
        }).catch(function (err) {
            if (err && err.name === 'AbortError') {
                showFailure(trigger, leadId,
                    'The call request timed out. Check the status before trying again.');
            } else {
                debug('call error', err);
                showFailure(trigger, leadId, 'Network error — the call was not placed.');
            }
            return null;
        }).then(restore, function (err) {
            debug('call cleanup', err);
            restore();
        });
    }

    function onClick(event) {
        if (!event.target || !event.target.closest) return;
        var trigger = event.target.closest('[data-call-lead]');
        if (!trigger) return;

        // A data-call-lead on a link or an accidental submit button must not
        // navigate or reload the board mid-dial.
        if (trigger.tagName === 'A' ||
            (trigger.tagName === 'BUTTON' && String(trigger.getAttribute('type') || '').toLowerCase() === 'submit')) {
            event.preventDefault();
        }

        if (trigger.getAttribute('aria-disabled') === 'true') return;
        if ('disabled' in trigger && trigger.disabled) return;

        var leadId = toLeadId(trigger.getAttribute('data-call-lead'));
        if (!leadId) {
            debug('not a lead id', trigger.getAttribute('data-call-lead'));
            return;
        }
        if (callsInFlight[leadId]) return; // second click while the POST is out

        try {
            placeCall(trigger, leadId);
        } catch (err) {
            // placeCall restores the button itself on every path it controls;
            // this is the last line of defence against a stuck disabled state.
            delete callsInFlight[leadId];
            debug('call start failed', err);
        }
    }

    // ── boot ─────────────────────────────────────────────────────────────────

    function init() {
        try {
            document.addEventListener('click', onClick, false);
            document.addEventListener('visibilitychange', onVisibilityChange, false);

            // One read on load so the summary is populated before the first
            // status tick (which is gated to the 30s campaign window).
            maybeRefreshCampaign(true);

            if (document.visibilityState === 'visible') startPolling();
            debug('ready');
        } catch (e) {
            debug('init failed', e);
        }
    }

    try {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', init, { once: true });
        } else {
            init();
        }
    } catch (e) {
        // A template without this file's markup is a no-op, not an error page.
        debug('boot failed', e);
    }
})();
