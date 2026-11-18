/*
 * static/js/api-client.js — one tiny JSON fetch wrapper for the owner pages.
 *
 * WHY THIS EXISTS
 * ---------------
 * Every page rolled its own `fetch` + `res.json()` + `json.error || json.detail`
 * ladder, and they drifted. Some never checked `res.ok`. Some printed
 * "[object Object]" when FastAPI answered 422, because a validation `detail` is
 * a list, not a string. Some did `res.json()` on a 303 to /login, which is HTML,
 * and reported "Unexpected token <". A shared client keeps the three things that
 * actually matter in one place: session cookies, one error shape, and a timeout.
 *
 * SURFACE
 * -------
 *   BZApi.getJSON(url[, opts])         -> Promise<parsed JSON | null>
 *   BZApi.postJSON(url, body[, opts])  -> Promise<parsed JSON | null>
 *   BZApi.lastError                    -> Error from the last failed call, else null
 *   BZApi.timeoutMs                    -> per-request budget, default 20000
 *
 * opts (both methods): { signal: AbortSignal, timeoutMs: number }
 *
 * Every rejection is an Error with:
 *   .name    'BZApiError'
 *   .status  HTTP status, or 0 when no response happened (offline, abort, timeout)
 *   .payload parsed JSON body when there was one, else null
 *   .url     the request URL
 * plus .aborted / .timedOut on cancellations and .cause on a network failure.
 * A string is never thrown, and nothing is logged unless window.BZ_DEBUG is set.
 *
 * NOTE ON BODY TYPES
 * ------------------
 * postJSON sends `Content-Type: application/json`. It refuses FormData,
 * URLSearchParams and Blob bodies instead of silently sending a broken request:
 * /api/copilot and most of /api/crew/app/* read `Form(...)`, so they need
 * multipart/form-urlencoded and are not usable through postJSON as-is.
 */
(function (window) {
    'use strict';

    if (!window || typeof window.fetch !== 'function') return;
    if (window.BZApi) return;                       // already loaded; keep the first

    var DEFAULT_TIMEOUT_MS = 20000;
    var timeoutMs = DEFAULT_TIMEOUT_MS;
    var lastError = null;

    function debug() {
        if (!window.BZ_DEBUG) return;
        try {
            console.warn.apply(console, ['[BZApi]'].concat(Array.prototype.slice.call(arguments)));
        } catch (e) { /* console unavailable; never let logging break a request */ }
    }

    function apiError(message, status, payload, url) {
        var err = new Error(message);
        err.name = 'BZApiError';
        err.status = status;
        err.payload = payload === undefined ? null : payload;
        err.url = url || '';
        return err;
    }

    function isApiError(err) {
        return !!err && err.name === 'BZApiError';
    }

    // FastAPI's `detail` is a string for HTTPException and a list of
    // {loc, msg, type} objects for a 422. Both have to read as a sentence.
    function detailText(detail) {
        if (typeof detail === 'string') return detail.trim();
        if (Array.isArray(detail)) {
            var parts = [];
            for (var i = 0; i < detail.length; i++) {
                var item = detail[i];
                if (typeof item === 'string') {
                    if (item.trim()) parts.push(item.trim());
                    continue;
                }
                if (!item || typeof item !== 'object') continue;
                var msg = typeof item.msg === 'string' ? item.msg.trim() : '';
                if (!msg) continue;
                // loc is ["body", "message"]; the field name is the useful part.
                var loc = Array.isArray(item.loc)
                    ? item.loc.filter(function (x) { return x !== 'body' && x !== 'query' && x !== 'path'; }).join('.')
                    : '';
                parts.push(loc ? loc + ': ' + msg : msg);
            }
            return parts.join('; ');
        }
        if (detail && typeof detail === 'object') {
            try { return JSON.stringify(detail); } catch (e) { return ''; }
        }
        return '';
    }

    // Preference order: reason (this app's newer shape), detail (FastAPI),
    // error / message (the shapes this app already returns from the Copilot,
    // crew app and lead routes), then the bare HTTP status.
    function messageFor(payload, status, statusText) {
        if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
            if (typeof payload.reason === 'string' && payload.reason.trim()) {
                return payload.reason.trim();
            }
            var detail = detailText(payload.detail);
            if (detail) return detail;
            if (typeof payload.error === 'string' && payload.error.trim()) {
                return payload.error.trim();
            }
            if (typeof payload.message === 'string' && payload.message.trim()) {
                return payload.message.trim();
            }
        }
        var base = 'HTTP ' + status;
        if (statusText) base += ' ' + statusText;
        return base;
    }

    // Read the body once, as text, and try to parse it. `parsed` distinguishes
    // "valid JSON null" from "not JSON at all" (an HTML login page, a proxy
    // error page) which is the failure that used to surface as "Unexpected
    // token <" and told nobody anything.
    function readBody(res) {
        return res.text().then(function (text) {
            if (!text) return { parsed: false, payload: null };
            try {
                return { parsed: true, payload: JSON.parse(text) };
            } catch (e) {
                return { parsed: false, payload: null };
            }
        }, function () {
            return { parsed: false, payload: null };
        });
    }

    function looksLikeHtml(res) {
        var type = String((res.headers && res.headers.get && res.headers.get('content-type')) || '');
        return /text\/html/i.test(type);
    }

    function request(url, opts) {
        var target = String(url || '');
        var options = opts || {};
        var method = String(options.method || 'GET').toUpperCase();
        var hasBody = options.body !== undefined && options.body !== null;
        var headers = { 'Accept': 'application/json' };
        if (hasBody) headers['Content-Type'] = 'application/json';

        var budget = options.timeoutMs === undefined ? timeoutMs : Number(options.timeoutMs);
        if (!isFinite(budget) || budget < 0) budget = DEFAULT_TIMEOUT_MS;

        var controller = null;
        var timer = null;
        var timedOut = false;
        var external = options.signal;

        if (typeof window.AbortController === 'function') {
            controller = new window.AbortController();
            if (external) {
                if (external.aborted) {
                    controller.abort();
                } else if (typeof external.addEventListener === 'function') {
                    external.addEventListener('abort', function () { controller.abort(); });
                }
            }
            if (budget > 0 && typeof window.setTimeout === 'function') {
                timer = window.setTimeout(function () {
                    timedOut = true;
                    controller.abort();
                }, budget);
            }
        }

        var init = {
            method: method,
            credentials: 'same-origin',   // session cookie, always
            headers: headers,
            cache: 'no-store'             // a polled tally must never be a cached one
        };
        if (hasBody) init.body = options.body;
        if (controller) init.signal = controller.signal;

        function clearTimer() {
            if (timer) {
                window.clearTimeout(timer);
                timer = null;
            }
        }

        return window.fetch(target, init).then(function (res) {
            return readBody(res).then(function (body) {
                if (!res.ok) {
                    throw apiError(
                        messageFor(body.payload, res.status, res.statusText),
                        res.status,
                        body.payload,
                        target
                    );
                }
                if (res.status === 204 || res.status === 205) return null;
                if (!body.parsed) {
                    // A 2xx that is not JSON on these pages is almost always the
                    // login page after the session expired: fetch follows the
                    // 303, so status alone cannot tell us.
                    throw apiError(
                        res.redirected && looksLikeHtml(res)
                            ? 'Signed out — the server sent the login page instead of JSON. Reload and sign in.'
                            : 'Expected JSON from ' + target + ' but got ' +
                              String((res.headers && res.headers.get && res.headers.get('content-type')) || 'an empty body') + '.',
                        res.status,
                        null,
                        target
                    );
                }
                return body.payload;
            });
        }).then(function (value) {
            clearTimer();
            lastError = null;
            return value;
        }, function (err) {
            clearTimer();
            var failure;
            if (isApiError(err)) {
                failure = err;
            } else if (err && (err.name === 'AbortError' || err.code === 20)) {
                var elapsed = budget > 0 && budget < 1000
                    ? budget + 'ms'
                    : (Math.round(budget / 100) / 10) + 's';
                failure = apiError(
                    timedOut ? 'Request timed out after ' + elapsed + '.' : 'Request cancelled.',
                    0,
                    null,
                    target
                );
                failure.aborted = true;
                failure.timedOut = !!timedOut;
                failure.cause = err;
            } else {
                // fetch only rejects for network/DNS/CORS faults — no status.
                failure = apiError('Network error talking to ' + target + '.', 0, null, target);
                failure.cause = err;
            }
            lastError = failure;
            debug(method, target, failure.status, failure.message, failure.payload);
            throw failure;
        });
    }

    function isFormLike(body) {
        if (!body || typeof body !== 'object') return false;
        return (typeof window.FormData === 'function' && body instanceof window.FormData) ||
            (typeof window.URLSearchParams === 'function' && body instanceof window.URLSearchParams) ||
            (typeof window.Blob === 'function' && body instanceof window.Blob) ||
            (typeof window.ArrayBuffer === 'function' && body instanceof window.ArrayBuffer);
    }

    function getJSON(url, opts) {
        var options = opts || {};
        return request(url, { method: 'GET', signal: options.signal, timeoutMs: options.timeoutMs });
    }

    function postJSON(url, body, opts) {
        var options = opts || {};
        if (isFormLike(body)) {
            var wrongType = apiError(
                'BZApi.postJSON only sends JSON. Use fetch() directly for FormData, ' +
                'URLSearchParams or file uploads (/api/copilot and /api/crew/app/* read Form fields).',
                0,
                null,
                String(url || '')
            );
            lastError = wrongType;
            debug('POST', String(url || ''), 0, wrongType.message);
            return Promise.reject(wrongType);
        }
        var json;
        try {
            json = JSON.stringify(body === undefined ? {} : body);
        } catch (e) {
            var unserializable = apiError('Could not encode the request body as JSON: ' + e.message, 0, null, String(url || ''));
            lastError = unserializable;
            debug('POST', String(url || ''), 0, unserializable.message);
            return Promise.reject(unserializable);
        }
        return request(url, { method: 'POST', body: json, signal: options.signal, timeoutMs: options.timeoutMs });
    }

    var api = {
        getJSON: getJSON,
        postJSON: postJSON,
        isApiError: isApiError
    };

    Object.defineProperty(api, 'lastError', {
        enumerable: true,
        get: function () { return lastError; }
    });

    Object.defineProperty(api, 'timeoutMs', {
        enumerable: true,
        get: function () { return timeoutMs; },
        set: function (value) {
            var next = Number(value);
            if (isFinite(next) && next >= 0) timeoutMs = next;
        }
    });

    window.BZApi = api;
})(typeof window !== 'undefined' ? window : this);
