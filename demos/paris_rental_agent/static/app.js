/* Paris Rental Agent — single-page UI.
 *
 * Views:
 *   - consent (anonymous cookie session)
 *   - onboarding (voice + text + draft profile + confirm)
 *   - dashboard (matches, saved, drafts, assistant)
 */

// Mount prefix: when the demo is mounted under a sub-path (e.g.
// /paris_rental_agent on the combined demos host), strip the SPA route
// suffix (/, /onboarding, /dashboard) to recover that prefix. Empty when
// served at the root.
const BASE = window.location.pathname.replace(/\/(onboarding|dashboard)?\/?$/, '');

const App = (() => {
    const state = {
        user: null,
        sessionToken: null,
        sessionPersisted: false,
        view: 'consent',
        intake: null,
        searchProfile: null,
        renterProfile: null,
        matches: [],
        savedListings: [],
        drafts: [],
        chatSessionId: null,
        chatLog: [],
        voice: null,
        dashboardScreen: 'feed',
        showTranscript: false,
        searching: false,
        matchesStale: false,
    };

    const AMENITY_DEFAULTS = {
        supermarket_m: 500,
        metro_m: 700,
        park_m: 1000,
        hospital_m: 2000,
        gym_m: 1500,
        school_m: 1500,
        pharmacy_m: 700,
        bakery_m: 500,
    };
    const AMENITY_LABELS = {
        supermarket_m: 'supermarket',
        metro_m: 'metro',
        park_m: 'park',
        hospital_m: 'hospital',
        gym_m: 'gym',
        school_m: 'school',
        pharmacy_m: 'pharmacy',
        bakery_m: 'bakery',
    };
    const AMENITY_ALIASES = {
        supermarket: 'supermarket_m',
        grocery: 'supermarket_m',
        groceries: 'supermarket_m',
        metro: 'metro_m',
        subway: 'metro_m',
        transit: 'metro_m',
        station: 'metro_m',
        park: 'park_m',
        hospital: 'hospital_m',
        gym: 'gym_m',
        school: 'school_m',
        pharmacy: 'pharmacy_m',
        bakery: 'bakery_m',
    };

    // ───────────────────────── API helpers ─────────────────────────
    function apiErrorMessage(data, fallback) {
        const detail = data?.detail;
        if (typeof detail === 'string') return detail;
        if (Array.isArray(detail)) {
            const messages = detail
                .map((item) => {
                    if (typeof item === 'string') return item;
                    const field = Array.isArray(item?.loc) ? item.loc.filter((part) => part !== 'body').join('.') : '';
                    const message = item?.msg || item?.message;
                    return [field, message].filter(Boolean).join(': ');
                })
                .filter(Boolean);
            if (messages.length) return messages.join(' ');
        }
        if (detail && typeof detail === 'object') {
            return detail.message || detail.error || JSON.stringify(detail);
        }
        if (typeof data?.message === 'string') return data.message;
        if (typeof data?.error === 'string') return data.error;
        return fallback;
    }

    async function api(path, opts = {}) {
        const res = await fetch(BASE + path, {
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json',
                ...(state.sessionToken ? { Authorization: `Bearer ${state.sessionToken}` } : {}),
                ...(opts.headers || {}),
            },
            ...opts,
            body: opts.body ? JSON.stringify(opts.body) : undefined,
        });
        let data = null;
        try { data = await res.json(); } catch { /* */ }
        if (!res.ok) {
            const err = new Error(apiErrorMessage(data, `HTTP ${res.status}`));
            err.status = res.status;
            err.data = data;
            throw err;
        }
        return data;
    }

    // ───────────────────────── Boot ─────────────────────────
    async function boot() {
        try {
            const me = await api('/api/auth/me');
            state.user = me;
            state.sessionPersisted = true;
            await enterSavedSession();
        } catch {
            state.view = 'consent';
            render();
        }
    }

    async function enterSavedSession() {
        await loadOnboardingState();
        await loadProfiles();
        if (state.searchProfile?.confirmation_status === 'confirmed') {
            await goDashboard();
            return;
        }
        if (!state.intake) {
            try {
                await api('/api/intake/start', { method: 'POST' });
                await loadOnboardingState();
            } catch {}
        }
        state.view = 'onboarding';
        render();
    }

    async function acceptCookieSession() {
        const me = await api('/api/auth/guest', { method: 'POST' });
        state.user = me;
        state.sessionToken = null;
        state.sessionPersisted = true;
        await enterSavedSession();
    }

    async function startTemporarySession() {
        const me = await api('/api/auth/guest?persist=false', { method: 'POST' });
        state.user = me;
        state.sessionToken = me.token || null;
        state.sessionPersisted = false;
        await enterSavedSession();
    }

    async function loadProfiles() {
        try {
            state.renterProfile = await api('/api/renter-profile');
        } catch {}
        try {
            state.searchProfile = await api('/api/search-profile');
        } catch {}
    }

    async function loadOnboardingState() {
        try {
            state.intake = await api('/api/intake/current');
            return state.intake;
        } catch {}
        return null;
    }

    function applyProfilePayload(payload) {
        if (!payload || typeof payload !== 'object') return;
        const profile = payload.draft_profile || payload.search_profile;
        if (profile && typeof profile === 'object') {
            const {
                work_location_label,
                work_location_address,
                ...searchFields
            } = profile;
            state.searchProfile = {
                ...(state.searchProfile || {}),
                ...searchFields,
                confirmation_status: payload.confirmation_status || searchFields.confirmation_status || state.searchProfile?.confirmation_status,
            };
            state.renterProfile = {
                ...(state.renterProfile || {}),
                work_location_label: work_location_label ?? state.renterProfile?.work_location_label,
                work_location_address: work_location_address ?? state.renterProfile?.work_location_address,
            };
        }
        if (payload.renter_profile) {
            state.renterProfile = { ...(state.renterProfile || {}), ...payload.renter_profile };
        }
    }

    async function refreshAfterVoiceProfileChange(msg) {
        const payload = msg?.state || {};
        state.intake = { ...(state.intake || {}), ...payload };
        applyProfilePayload(payload);

        const voiceSummary = payload.summary;
        await Promise.all([loadOnboardingState(), loadProfiles()]);
        if (voiceSummary && state.intake) state.intake.summary = voiceSummary;

        if (msg.type === 'profile_confirmed' && payload.ok) {
            await runFreshSearch();
            return;
        }
        state.view = 'onboarding';
        render();
    }

    async function loadMatches() {
        try {
            const m = await api('/api/matches?include_rejected=false&limit=20');
            state.matches = m?.matches || [];
            state.matchesStale = Boolean(m?.stale);
        } catch { state.matches = []; }
    }

    async function loadSaved() {
        try {
            const s = await api('/api/saved-listings');
            state.savedListings = s?.saved_listings || [];
        } catch { state.savedListings = []; }
    }

    async function loadDrafts() {
        try {
            const d = await api('/api/viewing-drafts');
            state.drafts = d?.drafts || [];
        } catch { state.drafts = []; }
    }

    async function goDashboard() {
        state.view = 'dashboard';
        await Promise.all([loadMatches(), loadSaved(), loadDrafts(), loadProfiles()]);
        render();
    }

    // ───────────────────────── Render ─────────────────────────
    function render() {
        const root = document.getElementById('app');
        if (!state.user) { root.innerHTML = renderConsent(); wireConsent(); return; }
        if (state.view === 'onboarding') { root.innerHTML = renderHeader() + renderOnboarding(); wireOnboarding(); return; }
        if (state.view === 'dashboard') { root.innerHTML = renderDashboard(); wireDashboard(); return; }
        root.innerHTML = renderHeader() + '<div class="container"><div class="card">Unknown view.</div></div>';
    }

    // ───────────────────────── Cookie consent view ─────────────────────────
    function renderConsent() {
        return `
            <div class="entry-shell">
                <header class="entry-header">
                    <div class="brand"><div class="brand-dot"></div> Paris Rental Agent</div>
                </header>
                <main class="entry-main">
                    <section class="entry-copy">
                        <h1>Find a Paris apartment that fits your life.</h1>
                        <p>Tell the voice scout your budget, commute, neighborhood preferences, and must-haves. It turns your brief into a search profile, finds matches, and drafts viewing messages.</p>
                        <div class="entry-actions">
                            <span>Voice intake</span>
                            <span>Search brief</span>
                            <span>Shortlist</span>
                        </div>
                    </section>
                    <section class="entry-preview" aria-label="Search preview">
                        <div class="preview-card primary">
                            <span>Current brief</span>
                            <strong>Furnished 1-bed near metro, under EUR 1,500</strong>
                        </div>
                        <div class="preview-grid">
                            <div class="preview-card"><span>Commute</span><strong>30 min</strong></div>
                            <div class="preview-card"><span>Matches</span><strong>Ready</strong></div>
                        </div>
                        <div class="preview-card">
                            <span>Next step</span>
                            <strong>Speak naturally, then review and confirm.</strong>
                        </div>
                    </section>
                </main>

                <aside class="cookie-popover" role="dialog" aria-label="Cookie preferences" aria-modal="true">
                    <div>
                        <strong>Save your progress?</strong>
                        <p>We can use one browser cookie to remember your profile, searches, shortlist, and drafts on this device. You can also continue without saving; that session starts fresh after reloads or future visits.</p>
                    </div>
                    <div class="cookie-actions">
                        <button id="skip-cookies" class="btn ghost" data-label="Decline and continue">Decline and continue</button>
                        <button id="accept-cookies" class="btn" data-label="Accept cookies">Accept cookies</button>
                    </div>
                    <div class="error-msg" id="consent-error" aria-live="polite"></div>
                </aside>
            </div>
        `;
    }

    function wireConsent() {
        const start = async (buttonId, action) => {
            const btn = document.getElementById(buttonId);
            const errEl = document.getElementById('consent-error');
            const label = btn.dataset.label || btn.textContent;
            errEl.textContent = '';
            btn.disabled = true;
            btn.textContent = 'Starting…';
            try {
                await action();
            } catch (e) {
                errEl.textContent = e.message || 'Could not start a browser session.';
                btn.disabled = false;
                btn.textContent = label;
            }
        };
        document.getElementById('accept-cookies')?.addEventListener('click', () => start('accept-cookies', acceptCookieSession));
        document.getElementById('skip-cookies')?.addEventListener('click', () => start('skip-cookies', startTemporarySession));
    }

    function renderHeader() {
        return `
            <div class="app-header">
                <div class="brand"><div class="brand-dot"></div> Paris Rental Agent</div>
                <div class="row" style="gap:12px;">
                    <div class="user-chip"><span>${state.sessionPersisted ? 'Saved in this browser' : 'Temporary session'}</span></div>
                    <button id="btn-reset-session" class="btn ghost small">Reset</button>
                </div>
            </div>
        `;
    }

    function wireHeader() {
        const reset = document.getElementById('btn-reset-session');
        if (reset) reset.addEventListener('click', async () => {
            try { await api('/api/auth/logout', { method: 'POST', body: { forget: true } }); } catch {}
            state.user = null;
            state.sessionToken = null;
            state.sessionPersisted = false;
            state.view = 'consent';
            stopVoice();
            render();
        });
    }

    // ───────────────────────── Onboarding ─────────────────────────
    function renderOnboarding() {
        const dp = state.intake?.draft_profile || {};
        const missing = state.intake?.missing_fields || [];
        const ambiguous = state.intake?.ambiguous_fields || [];
        const conf = state.intake?.field_confidence || {};
        const sources = state.intake?.field_sources || {};
        const status = state.intake?.confirmation_status || 'draft';
        const summary = state.intake?.summary || (state.intake?.raw_transcript ? '' : '');

        return `
        <div class="container">
            <div class="card" style="margin-bottom:20px;">
                <h2>Tell us what you're looking for in Paris</h2>
                <p style="color:var(--muted);margin-top:6px;">Speak naturally — budget, workplace, commute, bedrooms, furnished, and amenities. We'll fill in the form and you can correct anything.</p>
            </div>
            <div class="onboarding-grid">
                <div class="grid" style="gap:20px;">
                    <div class="card">
                        <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:14px;">
                            <div class="card-title" style="margin:0;">Voice</div>
                            <label class="toggle-pill">
                                <input type="checkbox" id="toggle-transcript" ${state.showTranscript ? 'checked' : ''}>
                                Transcript
                            </label>
                        </div>
                        <div class="voice-controls">
                            <button id="mic-btn" class="mic-btn" aria-label="Start voice">●</button>
                            <div class="voice-stack">
                                <span id="voice-status" class="voice-pill"><span class="dot"></span> Tap the mic to start</span>
                                <label style="font-size:13px;color:var(--muted);"><input type="checkbox" id="echo-cancel" checked style="width:auto;"> Echo cancellation</label>
                                <label style="font-size:13px;color:var(--muted);">Speed
                                    <input type="range" id="speed" min="0.5" max="2.0" step="0.1" value="1.0" style="width:auto;display:inline-block;vertical-align:middle;">
                                </label>
                            </div>
                        </div>
                        <p style="margin-top:12px;color:var(--muted);font-size:13px;">
                            The agent will ask you about your apartment, then fill in the form on the right.
                            You should review and correct it before confirming.
                        </p>
                    </div>

                    ${state.showTranscript ? `
                    <div class="card">
                        <div class="card-title">Transcript</div>
                        <div class="transcript" id="transcript">${state.intake?.raw_transcript ? `<div class="msg msg-user"><span class="msg-text">${escapeHtml(state.intake.raw_transcript)}</span></div>` : '<em style="color:var(--muted);">Say something like "I\'m looking for a furnished one-bedroom near République, max €1500 including charges, no more than 30 minutes by metro or bike."</em>'}</div>
                        <div class="row" style="margin-top:10px;">
                            <textarea id="manual-transcript" placeholder="…or type your description here." style="min-height:60px;"></textarea>
                        </div>
                        <div class="row" style="margin-top:8px;">
                            <button id="btn-extract" class="btn">Re-extract from text</button>
                            <span class="spacer"></span>
                        </div>
                    </div>` : ''}
                </div>

                <div class="grid" style="gap:20px;">
                    <div class="card">
                        <div class="card-title">Review the draft profile</div>
                        ${summary ? `<div class="summary-banner"><strong>Agent summary:</strong> ${escapeHtml(summary)}</div>` : ''}
                        ${renderMissingBlock(missing, ambiguous)}
                        ${renderProfileForm(dp, conf, sources, missing)}
                    </div>

                    <div class="card">
                        <div class="row" style="justify-content:space-between;">
                            <div>
                                <div class="card-title" style="margin:0;">Confirmation</div>
                                <div style="font-size:13px;color:var(--muted);margin-top:4px;">Status: <strong>${status}</strong></div>
                            </div>
                            <div class="row">
                                <button id="btn-confirm" class="btn ${missing.length ? '' : 'warm'}" ${missing.length ? 'disabled' : ''}>Confirm profile</button>
                                <button id="btn-search" class="btn ghost" ${status === 'confirmed' ? '' : 'disabled'}>Run search</button>
                            </div>
                        </div>
                        ${missing.length ? `<p style="font-size:13px;color:var(--bad);margin-top:8px;">Missing required: ${missing.join(', ')}</p>` : ''}
                    </div>
                </div>
            </div>
        </div>
        `;
    }

    function renderMissingBlock(missing, ambiguous) {
        if (!missing.length && !ambiguous.length) return '';
        return `
            <div style="display:grid;gap:6px;margin-bottom:12px;">
                ${missing.length ? `<div style="font-size:13px;color:var(--bad);"><strong>Missing:</strong> ${missing.join(', ')}</div>` : ''}
                ${ambiguous.length ? `<div style="font-size:13px;color:var(--warn);"><strong>Ambiguous:</strong> ${ambiguous.join(', ')}</div>` : ''}
            </div>
        `;
    }

    function renderProfileForm(dp, conf, sources, missing) {
        const tag = (field) => {
            const c = conf[field];
            const src = sources[field] ? `<span class="confidence-tag">${sources[field]}</span>` : '';
            if (typeof c !== 'number') return src;
            const cls = c >= 0.85 ? 'high' : c >= 0.7 ? 'med' : 'low';
            return `<span class="confidence-tag ${cls}">${Math.round(c*100)}%</span>${src}`;
        };
        const cls = (field) => missing.includes(field) ? 'field missing' : 'field';
        const arr = (v) => Array.isArray(v) ? v.join(', ') : (v || '');
        const rooms = dp.room_requirements || {};
        const nearby = dp.nearby_requirements || {};
        return `
            <div class="fields">
                <div class="${cls('work_location')} full">
                    <label>Workplace address or landmark ${tag('work_location_label')}</label>
                    <input id="f-work_location_label" value="${escapeAttr(dp.work_location_label || '')}" placeholder="e.g. République, La Défense, Station F">
                    <input id="f-work_location_address" value="${escapeAttr(dp.work_location_address || '')}" placeholder="Optional precise address" style="margin-top:6px;">
                </div>
                <div class="${cls('max_rent_including_charges_eur')}">
                    <label>Max rent incl. charges (€) ${tag('max_rent_including_charges_eur')}</label>
                    <input id="f-max_rent" type="number" min="1" value="${dp.max_rent_including_charges_eur ?? ''}">
                </div>
                <div class="field">
                    <label>Bedrooms ${tag('min_bedrooms')}</label>
                    <input id="f-min_bedrooms" type="number" min="0" value="${dp.min_bedrooms ?? ''}">
                </div>
                <div class="field">
                    <label>Minimum rooms (optional) ${tag('min_rooms')}</label>
                    <input id="f-min_rooms" type="number" min="1" value="${dp.min_rooms ?? ''}">
                </div>
                <div class="field">
                    <label>Min surface (m²) ${tag('min_surface_m2')}</label>
                    <input id="f-min_surface" type="number" min="1" value="${dp.min_surface_m2 ?? ''}">
                </div>
                <div class="field">
                    <label>Furnished ${tag('furnished_preference')}</label>
                    <select id="f-furnished">
                        <option value="" ${!dp.furnished_preference ? 'selected' : ''}>—</option>
                        <option value="required" ${dp.furnished_preference === 'required' ? 'selected' : ''}>Required</option>
                        <option value="prefer" ${dp.furnished_preference === 'prefer' ? 'selected' : ''}>Preferred</option>
                        <option value="any" ${dp.furnished_preference === 'any' ? 'selected' : ''}>Any</option>
                    </select>
                </div>
                <div class="${cls('commute_max_minutes')}">
                    <label>Commute max (minutes) ${tag('commute_max_minutes')}</label>
                    <input id="f-commute_minutes" type="number" min="1" value="${dp.commute_max_minutes ?? 30}">
                </div>
                <div class="${cls('commute_modes')}">
                    <label>Commute modes ${tag('commute_modes')}</label>
                    <div class="chip-row" id="f-commute_modes" data-value='${JSON.stringify(dp.commute_modes || ['metro','bike'])}'>
                        ${['metro','bike','walk','bus'].map(m => `<span class="chip ${(dp.commute_modes||['metro','bike']).includes(m) ? 'on' : ''}" data-mode="${m}">${m}</span>`).join('')}
                    </div>
                </div>
                <div class="field full">
                    <label>Living-room must-haves (comma-separated)</label>
                    <input id="f-lr_must" value="${escapeAttr(arr((rooms.living_room||{}).must_have))}">
                </div>
                <div class="field full">
                    <label>Living-room nice-to-haves</label>
                    <input id="f-lr_nice" value="${escapeAttr(arr((rooms.living_room||{}).nice_to_have))}">
                </div>
                <div class="field full">
                    <label>Bedroom must-haves</label>
                    <input id="f-br_must" value="${escapeAttr(arr((rooms.bedroom||{}).must_have))}">
                </div>
                <div class="field full">
                    <label>Bedroom nice-to-haves</label>
                    <input id="f-br_nice" value="${escapeAttr(arr((rooms.bedroom||{}).nice_to_have))}">
                </div>
                <div class="field full">
                    <label>Kitchen must-haves</label>
                    <input id="f-kt_must" value="${escapeAttr(arr((rooms.kitchen||{}).must_have))}">
                </div>
                <div class="field full">
                    <label>Kitchen nice-to-haves</label>
                    <input id="f-kt_nice" value="${escapeAttr(arr((rooms.kitchen||{}).nice_to_have))}">
                </div>
                <div class="field">
                    <label>Preferred arrondissements</label>
                    <input id="f-pref_arr" value="${escapeAttr(arr(dp.preferred_arrondissements))}" placeholder="e.g. 11, 3, 4">
                </div>
                <div class="field">
                    <label>Excluded arrondissements</label>
                    <input id="f-excl_arr" value="${escapeAttr(arr(dp.excluded_arrondissements))}" placeholder="e.g. 16, 8">
                </div>
                <div class="field full">
                    <label>Nearby amenities (comma-separated)</label>
                    <input id="f-nearby" value="${escapeAttr(formatNearbyAmenities(nearby))}" placeholder="park, pharmacy, metro">
                </div>
            </div>
            <div class="row" style="margin-top:14px;">
                <button id="btn-save-form" class="btn ghost">Save text edits</button>
            </div>
        `;
    }

    function wireOnboarding() {
        wireHeader();
        // Voice mic
        const mic = document.getElementById('mic-btn');
        if (mic) mic.addEventListener('click', toggleVoice);
        document.getElementById('toggle-transcript')?.addEventListener('change', (ev) => {
            state.showTranscript = ev.target.checked;
            render();
        });

        document.getElementById('btn-extract')?.addEventListener('click', async () => {
            const text = document.getElementById('manual-transcript').value.trim();
            if (!text) return;
            try {
                const res = await api('/api/intake/transcript', { method: 'POST', body: { transcript: text } });
                state.intake = res;
                appendTranscriptUser(text);
                if (res.summary) appendTranscriptAgent(res.summary);
                render();
            } catch (e) { alert('Extraction failed: ' + e.message); }
        });

        document.getElementById('btn-save-form')?.addEventListener('click', saveOnboardingForm);
        document.getElementById('speed')?.addEventListener('input', () => {
            if (voiceWS?.readyState === WebSocket.OPEN && isRecording) {
                voiceWS.send(JSON.stringify({ type: 'config', speed: readVoiceSpeed() }));
            }
        });

        document.getElementById('btn-confirm')?.addEventListener('click', async () => {
            // First save current form values
            await saveOnboardingForm({ silent: true });
            try {
                const res = await api('/api/intake/confirm', { method: 'POST' });
                if (!res.ok) {
                    alert('Could not confirm: ' + (res.missing_fields || []).join(', '));
                    return;
                }
                stopVoice();
                await runFreshSearch();
            } catch (e) {
                if (e.data?.missing_fields) alert('Missing: ' + e.data.missing_fields.join(', '));
                else alert(e.message);
            }
        });

        document.getElementById('btn-search')?.addEventListener('click', async () => {
            try {
                await runFreshSearch();
            } catch (e) { alert(e.message); }
        });

        // Commute mode chips
        const modes = document.getElementById('f-commute_modes');
        if (modes) {
            modes.querySelectorAll('.chip').forEach(chip => {
                chip.addEventListener('click', () => chip.classList.toggle('on'));
            });
        }
    }

    async function saveOnboardingForm(opts = {}) {
        const get = (id) => document.getElementById(id)?.value ?? '';
        const num = (v) => v === '' ? null : Number(v);
        const positiveNum = (v) => {
            const n = num(v);
            return n && n > 0 ? n : null;
        };
        const list = (v) => v ? v.split(',').map(s => s.trim()).filter(Boolean) : [];
        const intList = (v) => list(v).map(s => parseInt(s, 10)).filter(n => !isNaN(n));

        const modes = Array.from(document.querySelectorAll('#f-commute_modes .chip.on')).map(c => c.dataset.mode);

        const nearby = parseNearbyAmenities(get('f-nearby'));

        const patch = {
            work_location_label: get('f-work_location_label') || null,
            work_location_address: get('f-work_location_address') || null,
            max_rent_including_charges_eur: positiveNum(get('f-max_rent')),
            min_bedrooms: num(get('f-min_bedrooms')),
            min_rooms: positiveNum(get('f-min_rooms')),
            min_surface_m2: positiveNum(get('f-min_surface')),
            furnished_preference: get('f-furnished') || null,
            commute_max_minutes: positiveNum(get('f-commute_minutes')) ?? 30,
            commute_modes: modes,
            preferred_arrondissements: intList(get('f-pref_arr')),
            excluded_arrondissements: intList(get('f-excl_arr')),
            room_requirements: {
                living_room: { must_have: list(get('f-lr_must')), nice_to_have: list(get('f-lr_nice')) },
                bedroom: { must_have: list(get('f-br_must')), nice_to_have: list(get('f-br_nice')) },
                kitchen: { must_have: list(get('f-kt_must')), nice_to_have: list(get('f-kt_nice')) },
            },
            nearby_requirements: nearby,
        };

        try {
            const res = await api('/api/intake/text-update', { method: 'POST', body: { patch } });
            state.intake = res;
            applyProfilePayload(res);
            if (!opts.silent) render();
        } catch (e) {
            if (!opts.silent) alert(e.message);
        }
    }

    // ───────────────────────── Dashboard ─────────────────────────
    function renderDashboard() {
        const sp = state.searchProfile || {};
        const rp = state.renterProfile || {};
        const activeScreen = ['feed', 'search', 'assistant', 'saved', 'drafts'].includes(state.dashboardScreen)
            ? state.dashboardScreen
            : 'feed';
        const navItems = [
            ['feed', 'Listings', state.matches.length],
            ['search', 'Search Brief', ''],
            ['assistant', 'AI Scout', ''],
            ['saved', 'Shortlist', state.savedListings.length],
            ['drafts', 'Viewing Messages', state.drafts.length],
        ];
        const profileBits = [
            ['Rent', sp.max_rent_including_charges_eur ? `€${sp.max_rent_including_charges_eur}` : '—'],
            ['Commute', sp.commute_max_minutes ? `≤ ${sp.commute_max_minutes} min` : '—'],
            ['Rooms', `${sp.min_bedrooms ?? '—'} bed / ${sp.min_rooms ?? '—'} rooms`],
            ['Area', (sp.preferred_arrondissements || []).length ? (sp.preferred_arrondissements || []).join(', ') : 'any'],
        ];

        return `
        <div class="workspace-shell">
            <aside class="workspace-sidebar">
                <div class="workspace-brand"><div class="brand-dot"></div> Paris Rental Agent</div>
                <nav class="screen-nav-list" aria-label="Dashboard screens">
                    ${navItems.map(([id, label, count]) => `
                        <button class="screen-nav ${activeScreen === id ? 'active' : ''}" data-screen="${id}">
                            <span>${label}</span>
                            ${count !== '' ? `<strong>${count}</strong>` : ''}
                        </button>
                    `).join('')}
                </nav>
                <div class="sidebar-footer">
                    <div class="user-chip">${state.sessionPersisted ? 'Saved in this browser' : 'Temporary session'}</div>
                    <button id="btn-reset-session" class="btn ghost small">Reset</button>
                </div>
            </aside>

            <main class="workspace-main">
                <header class="workspace-topbar">
                    <div>
                        <h1>${screenTitle(activeScreen)}</h1>
                        <p>${screenSubtitle(activeScreen, rp)}</p>
                    </div>
                    ${activeScreen === 'feed' || activeScreen === 'search' ? `
                        <div class="topbar-actions">
                            <button id="btn-fresh-search" class="btn">Run search</button>
                            <button id="btn-edit-profile" class="btn ghost">Update</button>
                        </div>
                    ` : ''}
                </header>
                <section class="workspace-content">
                    ${renderDashboardScreen(activeScreen, profileBits)}
                </section>
            </main>
        </div>
        `;
    }

    function screenTitle(screen) {
        return {
            feed: 'Listings',
            search: 'Search Brief',
            assistant: 'AI Scout',
            saved: 'Shortlist',
            drafts: 'Viewing Messages',
        }[screen] || 'Listings';
    }

    function screenSubtitle(screen, rp) {
        if (screen === 'feed') return `${state.matches.length} current matches`;
        if (screen === 'search') return rp.work_location_label || rp.work_location_address || 'Search profile';
        if (screen === 'assistant') return 'Search by voice or text';
        if (screen === 'saved') return `${state.savedListings.length} shortlisted homes`;
        if (screen === 'drafts') return `${state.drafts.length} draft messages`;
        return '';
    }

    function renderDashboardScreen(activeScreen, profileBits) {
        if (activeScreen === 'search') return renderSearchScreen(profileBits);
        if (activeScreen === 'assistant') return renderAssistantScreen();
        if (activeScreen === 'saved') return renderSavedScreen();
        if (activeScreen === 'drafts') return renderDraftsScreen();
        return renderFeedScreen();
    }

    function renderFeedScreen() {
        if (state.searching) {
            return `
                <div class="screen-empty">
                    <strong>Searching with the latest profile…</strong>
                </div>
            `;
        }
        if (!state.matches.length) {
            return `
                <div class="screen-empty">
                    <strong>${state.matchesStale ? 'Profile updated. Run a fresh search.' : 'No matches yet'}</strong>
                    <button id="btn-fresh-search-2" class="btn">Run search</button>
                </div>
            `;
        }
        return `
            <div class="feed-toolbar">
                <span>${state.matches.length} homes scanned for this profile</span>
                <button id="btn-fresh-search-2" class="btn small">Refresh</button>
            </div>
            <div class="feed-list">
                ${state.matches.map(renderListingCard).join('')}
            </div>
        `;
    }

    function renderSearchScreen(profileBits) {
        const sp = state.searchProfile || {};
        return `
            <div class="search-screen-grid">
                <section class="profile-panel">
                    <div class="profile-chips profile-chips-large">
                        ${profileBits.map(([label, value]) => `
                            <span class="profile-chip"><strong>${label}</strong> ${escapeHtml(value)}</span>
                        `).join('')}
                    </div>
                    <dl class="profile-details">
                        <div><dt>Furnished</dt><dd>${escapeHtml(sp.furnished_preference || 'Any')}</dd></div>
                        <div><dt>Commute modes</dt><dd>${(sp.commute_modes || []).join(' / ') || '—'}</dd></div>
                        <div><dt>Min surface</dt><dd>${sp.min_surface_m2 ?? '—'} m²</dd></div>
                        <div><dt>Excluded areas</dt><dd>${(sp.excluded_arrondissements || []).join(', ') || 'none'}</dd></div>
                    </dl>
                </section>
                <section class="profile-panel muted-panel">
                    <strong>Next action</strong>
                    <p>Run a fresh search or update the profile details.</p>
                    <div class="panel-actions">
                        <button id="btn-fresh-search-2" class="btn">Run search</button>
                        <button id="btn-edit-profile-2" class="btn ghost">Update profile</button>
                    </div>
                </section>
            </div>
        `;
    }

    function renderAssistantScreen() {
        return `
            <div class="assistant-screen">
                <section class="profile-panel">
                    <div class="voice-mini">
                        <button id="mic-btn" class="mic-btn mini" aria-label="Start voice">●</button>
                        <span id="voice-status" class="voice-pill compact"><span class="dot"></span> Voice</span>
                        <label class="toggle-line"><input type="checkbox" id="echo-cancel" checked> Echo</label>
                        <label class="speed-line">Speed <input type="range" id="speed" min="0.5" max="2.0" step="0.1" value="1.0"></label>
                    </div>
                    <div class="voice-transcript compact" id="transcript" aria-live="polite"></div>
                    <div class="assistant-input compact">
                        <input id="assistant-input" placeholder="Ask or type an action">
                        <button id="assistant-send" class="btn small">Send</button>
                    </div>
                    <div class="assistant-log" id="assistant-log" aria-live="polite">
                        ${state.chatLog.length ? state.chatLog.map(m => `<div class="msg msg-${m.role === 'user' ? 'user' : 'agent'}"><span class="msg-text">${escapeHtml(m.content)}</span></div>`).join('') : '<em>Ask for new listings, your shortlist, or a viewing message.</em>'}
                    </div>
                </section>
            </div>
        `;
    }

    function renderSavedScreen() {
        if (!state.savedListings.length) {
            return `<div class="screen-empty"><strong>No shortlisted homes</strong></div>`;
        }
        return `
            <div class="feed-list">
                ${state.savedListings.map(s => renderListingCard({listing: s.listing, overall_score: '★', reasons: [], warnings: [], match_id: s.id})).join('')}
            </div>
        `;
    }

    function renderDraftsScreen() {
        if (!state.drafts.length) {
            return `<div class="screen-empty"><strong>No viewing messages</strong></div>`;
        }
        return `
            <div class="drafts-list">
                ${state.drafts.map(d => `
                    <div class="draft-card">
                        <strong>${escapeHtml(d.subject || '(no subject)')}</strong>
                        <div>${d.language.toUpperCase()} · ${escapeHtml(d.status)}</div>
                        <pre>${escapeHtml(d.body)}</pre>
                    </div>
                `).join('')}
            </div>
        `;
    }

    function renderListingCard(m) {
        const l = m.listing;
        if (!l) return '';
        const total = l.total_monthly_eur || l.rent_eur;
        const c = m.commute || {};
        const listingUrl = safeExternalUrl(l.canonical_url);
        const commuteBits = [];
        if (c.metro_min != null) commuteBits.push(`Metro ${c.metro_min} min`);
        if (c.bike_min != null) commuteBits.push(`bike ${c.bike_min} min`);
        if (c.walk_min != null) commuteBits.push(`walk ${c.walk_min} min`);
        return `
            <div class="listing-card" data-listing="${l.id}">
                <div class="header-row">
                    <h3>${escapeHtml(l.title)}</h3>
                    <div class="score">${m.overall_score}</div>
                </div>
                <div class="meta">
                    ${total ? `<span>€${total}${l.charges_eur ? ' total' : ''}</span>` : ''}
                    ${l.surface_m2 ? `<span>${l.surface_m2} m²</span>` : ''}
                    ${l.bedrooms != null ? `<span>${l.bedrooms} bed</span>` : (l.rooms ? `<span>${l.rooms} rooms</span>` : '')}
                    ${l.furnished === true ? '<span>furnished</span>' : (l.furnished === false ? '<span>unfurnished</span>' : '')}
                    ${l.arrondissement ? `<span>Paris ${String(l.arrondissement).padStart(2,'0')}</span>` : ''}
                    ${l.is_mock ? '<span class="tag-mock">mock</span>' : ''}
                </div>
                ${commuteBits.length ? `<div class="meta" style="color:var(--accent);">${commuteBits.map(s => `<span>${escapeHtml(s)}</span>`).join('')}</div>` : ''}
                ${m.reasons?.length ? `<div class="reasons">${m.reasons.slice(0,4).map(escapeHtml).join(' · ')}</div>` : ''}
                ${m.warnings?.length ? `<div class="warnings">${m.warnings.slice(0,2).map(escapeHtml).join(' · ')}</div>` : ''}
                <div class="actions">
                    <button class="btn small" data-act="save">Save</button>
                    <button class="btn ghost small" data-act="reject">Reject</button>
                    <button class="btn ghost small" data-act="explain">Explain</button>
                    <button class="btn warm small" data-act="draft">Draft message</button>
                    ${listingUrl ? `<a class="btn ghost small" href="${escapeAttr(listingUrl)}" target="_blank" rel="noopener noreferrer">Open</a>` : ''}
                </div>
            </div>
        `;
    }

    function wireDashboard() {
        wireHeader();
        document.getElementById('mic-btn')?.addEventListener('click', toggleVoice);
        document.getElementById('btn-fresh-search')?.addEventListener('click', runFreshSearch);
        document.getElementById('btn-fresh-search-2')?.addEventListener('click', runFreshSearch);
        document.getElementById('btn-edit-profile')?.addEventListener('click', () => {
            state.view = 'onboarding';
            render();
        });
        document.getElementById('btn-edit-profile-2')?.addEventListener('click', () => {
            state.view = 'onboarding';
            render();
        });
        document.querySelectorAll('.screen-nav').forEach(btn => {
            btn.addEventListener('click', () => {
                state.dashboardScreen = btn.dataset.screen;
                render();
            });
        });
        document.getElementById('speed')?.addEventListener('input', () => {
            if (voiceWS?.readyState === WebSocket.OPEN && isRecording) {
                voiceWS.send(JSON.stringify({ type: 'config', speed: readVoiceSpeed() }));
            }
        });

        // Listing card actions
        document.querySelectorAll('.listing-card').forEach(card => {
            const id = card.dataset.listing;
            card.querySelectorAll('button[data-act]').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const act = btn.dataset.act;
                    try {
                        if (act === 'save') {
                            await api(`/api/listings/${id}/save`, { method: 'POST' });
                            await loadSaved(); render();
                        } else if (act === 'reject') {
                            await api(`/api/listings/${id}/reject`, { method: 'POST', body: {} });
                            await loadMatches(); render();
                        } else if (act === 'explain') {
                            const res = await api(`/api/listings/${id}/explain`, { method: 'POST' });
                            alert(res.explanation || 'No explanation.');
                        } else if (act === 'draft') {
                            const lang = confirm('Draft in French? OK = French, Cancel = English') ? 'fr' : 'en';
                            await api(`/api/listings/${id}/draft-viewing-request`, { method: 'POST', body: { language: lang } });
                            await loadDrafts();
                            state.dashboardScreen = 'drafts';
                            render();
                        }
                    } catch (e) { alert(e.message); }
                });
            });
        });

        // Chat assistant
        const chatSend = document.getElementById('assistant-send');
        const chatInput = document.getElementById('assistant-input');
        const sendChat = async () => {
            const text = chatInput.value.trim();
            if (!text) return;
            chatInput.value = '';
            state.chatLog.push({ role: 'user', content: text });
            render();
            try {
                const res = await api('/api/assistant/chat', {
                    method: 'POST',
                    body: { message: text, conversation_session_id: state.chatSessionId },
                });
                state.chatSessionId = res.conversation_session_id;
                state.chatLog.push({ role: 'assistant', content: res.reply });
                // If a search ran, refresh matches
                if (res.tool_calls?.some(tc => tc.name === 'run_apartment_search')) {
                    await loadMatches();
                }
                if (res.tool_calls?.some(tc => tc.name === 'draft_viewing_request')) {
                    await loadDrafts();
                    state.dashboardScreen = 'drafts';
                }
                if (res.tool_calls?.some(tc => tc.name === 'save_listing')) {
                    await loadSaved();
                }
                render();
            } catch (e) {
                state.chatLog.push({ role: 'assistant', content: 'Error: ' + e.message });
                render();
            }
        };
        if (chatSend) chatSend.addEventListener('click', sendChat);
        if (chatInput) chatInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendChat(); });
    }

    async function runFreshSearch() {
        state.searching = true;
        state.matchesStale = false;
        state.matches = [];
        state.view = 'dashboard';
        state.dashboardScreen = 'feed';
        render();
        try {
            const res = await api('/api/search-runs', { method: 'POST', body: { max_results: 20 } });
            if (res?.ok) {
                state.matches = res.matches || [];
                await Promise.all([loadSaved(), loadDrafts(), loadProfiles()]);
                state.searching = false;
                render();
            }
        } catch (e) {
            state.searching = false;
            if (e.data?.error === 'search_profile_not_confirmed') {
                state.view = 'onboarding'; render();
            } else {
                alert(e.message);
                render();
            }
        }
    }

    // ───────────────────────── Voice ─────────────────────────
    let voiceWS = null;
    let player = null;
    let isRecording = false;
    let userBubble = null;
    let hadAssistantBubble = false;
    let turnBubbles = {};

    async function toggleVoice() {
        if (isRecording) { stopVoice(); return; }
        await startVoice();
    }

    function setVoiceStatus(label, kind) {
        const el = document.getElementById('voice-status');
        if (!el) return;
        el.textContent = '';
        const dot = document.createElement('span');
        dot.className = 'dot';
        el.appendChild(dot);
        el.append(' ' + label);
        el.classList.toggle('on', kind === 'on');
        el.classList.toggle('error', kind === 'error');
        const mic = document.getElementById('mic-btn');
        if (mic) mic.classList.toggle('live', kind === 'on');
    }

    async function startVoice() {
        try {
            const audioConfig = await fetch(BASE + '/api/audio-config').then(r => r.json()).catch(() => ({pcm: false}));
            const echo = document.getElementById('echo-cancel')?.checked ?? true;
            player = new SyncedAudioPlayer({
                basePath: BASE + '/static/js',
                sampleRate: 24000,
                pcmOutput: audioConfig.pcm || false,
                echoCancellation: echo,
                onEncodedAudio: (data) => {
                    if (isRecording && voiceWS?.readyState === WebSocket.OPEN) {
                        voiceWS.send(data);
                    }
                },
                onText: ({ text, turnIdx, isUser }) => {
                    appendTranscript(text, turnIdx, isUser);
                },
                onEvent: (eventType, msg) => {
                    handleVoiceEvent(msg);
                },
            });
            await player.start();

            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const tokenParam = state.sessionToken ? `?token=${encodeURIComponent(state.sessionToken)}` : '';
            const wsUrl = `${protocol}//${location.host}${BASE}/ws/voice${tokenParam}`;
            voiceWS = new WebSocket(wsUrl);
            voiceWS.onopen = () => {
                voiceWS.send(JSON.stringify({ type: 'start', speed: readVoiceSpeed(), language: 'en' }));
                isRecording = true;
                setVoiceStatus('Listening — speak naturally', 'on');
            };
            voiceWS.onmessage = (event) => {
                player?.handleMessage(event.data);
            };
            voiceWS.onclose = () => {
                stopVoice();
            };
            voiceWS.onerror = () => {
                setVoiceStatus('Voice error', 'error');
            };
        } catch (e) {
            console.error(e);
            setVoiceStatus('Voice error: ' + e.message, 'error');
        }
    }

    function stopVoice() {
        isRecording = false;
        try {
            if (voiceWS?.readyState === WebSocket.OPEN) {
                voiceWS.send(JSON.stringify({ type: 'stop' }));
            }
            voiceWS?.close();
        } catch {}
        voiceWS = null;
        try { player?.stop(); } catch {}
        player = null;
        setVoiceStatus('Tap the mic to start', '');
    }

    function readVoiceSpeed() {
        const speed = parseFloat(document.getElementById('speed')?.value || '1');
        if (Number.isNaN(speed)) return 1.0;
        return Math.min(2.0, Math.max(0.5, speed));
    }

    function getBubbleForTurn(turnIdx, isUser) {
        const transcript = document.getElementById('transcript');
        if (!transcript) return null;
        if (isUser) {
            if (userBubble && !hadAssistantBubble) return userBubble;
            hadAssistantBubble = false;
            userBubble = document.createElement('div');
            userBubble.className = 'msg msg-user';
            const tx = document.createElement('span');
            tx.className = 'msg-text';
            userBubble.appendChild(tx);
            transcript.appendChild(userBubble);
            return userBubble;
        }
        let bubble = turnBubbles[turnIdx];
        if (!bubble) {
            hadAssistantBubble = true;
            bubble = document.createElement('div');
            bubble.className = 'msg msg-agent';
            const tx = document.createElement('span');
            tx.className = 'msg-text';
            bubble.appendChild(tx);
            transcript.appendChild(bubble);
            turnBubbles[turnIdx] = bubble;
        }
        return bubble;
    }

    function appendTranscript(text, turnIdx, isUser) {
        const bubble = getBubbleForTurn(turnIdx, isUser);
        if (!bubble) return;
        bubble.querySelector('.msg-text').textContent += text + ' ';
        const transcript = document.getElementById('transcript');
        if (transcript && transcript.children.length > 60) {
            const removed = transcript.removeChild(transcript.firstChild);
            for (const k in turnBubbles) if (turnBubbles[k] === removed) delete turnBubbles[k];
            if (userBubble === removed) userBubble = null;
        }
        if (transcript) transcript.scrollTop = transcript.scrollHeight;
    }

    function appendTranscriptUser(text) {
        getBubbleForTurn(Date.now(), true);
        appendTranscript(text, Date.now(), true);
    }
    function appendTranscriptAgent(text) {
        getBubbleForTurn(Date.now(), false);
        appendTranscript(text, Date.now(), false);
    }

    function handleVoiceEvent(msg) {
        if (!msg || !msg.type) return;
        if (msg.type === 'intake_state' || msg.type === 'profile_confirmed') {
            refreshAfterVoiceProfileChange(msg);
        } else if (msg.type === 'tool_started' && msg.tool === 'run_apartment_search') {
            state.searching = true;
            state.matchesStale = false;
            state.matches = [];
            state.view = 'dashboard';
            state.dashboardScreen = 'feed';
            render();
        } else if (msg.type === 'search_results') {
            state.searching = false;
            if (msg.result?.ok) {
                state.matches = msg.result.matches || [];
                state.matchesStale = false;
                state.view = 'dashboard';
                state.dashboardScreen = 'feed';
                (async () => {
                    await Promise.all([loadSaved(), loadDrafts(), loadProfiles()]);
                    render();
                })();
            } else {
                state.matches = [];
                render();
            }
        } else if (msg.type === 'matches') {
            // no-op; rendered after next refresh
        } else if (msg.type === 'listing_saved') {
            (async () => { await loadSaved(); if (state.view === 'dashboard') render(); })();
        } else if (msg.type === 'listing_rejected') {
            (async () => { await loadMatches(); if (state.view === 'dashboard') render(); })();
        } else if (msg.type === 'draft_created') {
            (async () => {
                await loadDrafts();
                state.dashboardScreen = 'drafts';
                if (state.view === 'dashboard') render();
            })();
        }
    }

    // ───────────────────────── Utils ─────────────────────────
    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g, ch => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[ch]));
    }
    function escapeAttr(s) { return escapeHtml(s); }
    function safeExternalUrl(url) {
        try {
            const parsed = new URL(String(url || ''));
            return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
        } catch {
            return '';
        }
    }

    function normalizeAmenityToken(token) {
        return String(token || '')
            .trim()
            .toLowerCase()
            .replace(/[\s-]+/g, '_');
    }

    function amenityKeyFromLabel(token) {
        const normalized = normalizeAmenityToken(token);
        if (!normalized) return '';
        if (AMENITY_DEFAULTS[normalized] != null) return normalized;
        if (AMENITY_DEFAULTS[`${normalized}_m`] != null) return `${normalized}_m`;
        return AMENITY_ALIASES[normalized] || '';
    }

    function formatNearbyAmenities(nearby) {
        const keys = Array.isArray(nearby) ? nearby : Object.keys(nearby || {});
        return keys
            .map(key => {
                const normalized = normalizeAmenityToken(key);
                return AMENITY_LABELS[normalized] || normalized.replace(/_m$/, '').replace(/_/g, ' ');
            })
            .filter(Boolean)
            .join(', ');
    }

    function parseNearbyAmenities(text) {
        const nearby = {};
        const tokens = text ? text.split(',').map(s => s.trim()).filter(Boolean) : [];
        for (const token of tokens) {
            const key = amenityKeyFromLabel(token);
            if (key) nearby[key] = AMENITY_DEFAULTS[key] || 1000;
        }
        return nearby;
    }

    function cleanupTemporarySession() {
        if (!state.sessionToken || state.sessionPersisted) return;
        try {
            fetch(BASE + '/api/auth/logout', {
                method: 'POST',
                keepalive: true,
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${state.sessionToken}`,
                },
                body: JSON.stringify({ forget: true }),
            });
        } catch {}
    }

    window.addEventListener('pagehide', cleanupTemporarySession);

    return { boot };
})();

document.addEventListener('DOMContentLoaded', App.boot);
