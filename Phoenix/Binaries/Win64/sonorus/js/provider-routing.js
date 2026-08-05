// ============================================
// OpenRouter Provider Routing Module
// Per-model provider order drawers for static model inputs
// ============================================

(function() {
    'use strict';

    const ROUTING_INPUTS = [
        { id: 'conv_chat_model', path: 'conversation.chat_model_providers' },
        { id: 'conv_target_model', path: 'conversation.target_selection_model_providers' },
        { id: 'conv_interjection_model', path: 'conversation.interjection_model_providers' },
        { id: 'background_commentary_model', path: 'conversation.commentary_model_providers' },
        { id: 'conv_input_correction_model', path: 'conversation.input_correction_model_providers' },
        { id: 'agent_vision_llm_model', path: 'agents.vision.llm.providers' },
        { id: 'owlPostOrchestratorModel', path: 'owl_post.orchestrator_model_providers' },
        { id: 'owlPostMailModel', path: 'owl_post.mail_model_providers' },
        { id: 'owlPostBoardModel', path: 'owl_post.board_model_providers' },
        { id: 'owlPostSummarizeModel', path: 'owl_post.summarize_model_providers' },
        { id: 'chapterModel', path: 'memory.chapter_model_providers' },
        { id: 'proseModel', path: 'memory.prose_model_providers' },
        { id: 'graphitiModel', path: 'memory.graphiti_model_providers' },
        { id: 'graphitiSmallModel', path: 'memory.graphiti_small_model_providers' },
        { id: 'rerankerModel', path: 'memory.reranker_model_providers' },
        { id: 'commitment_location_resolver_model', path: 'commitment.location_resolver_model_providers' }
    ];
    const providerMetadataCache = new Map();     // clean model -> provider metadata list
    const providerMetadataPromises = new Map();  // clean model -> fetch promise
    const modelInputDebounceTimers = {};

    function getPathValue(root, path) {
        const parts = path.split('.');
        let value = root;
        for (const part of parts) {
            value = value?.[part];
        }
        return value;
    }

    function normalizeProviders(value) {
        let providers = [];
        if (Array.isArray(value)) {
            providers = value.filter(provider => provider != null)
                .map(provider => String(provider).trim())
                .filter(Boolean);
        } else if (typeof value === 'string') {
            providers = value.split(',').map(provider => provider.trim()).filter(Boolean);
        }

        const seen = new Set();
        return providers.filter(provider => {
            const key = provider.toLowerCase();
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }

    function getModelForInput(input) {
        return (input?.value || input?.placeholder || '').trim();
    }

    function cleanModelForProviderLookup(model) {
        const value = (model || '').trim();
        return value.includes(':') ? value.split(':')[0] : value;
    }

    function getProviderTokenBounds(input) {
        const value = input?.value || '';
        const caret = Number.isInteger(input?.selectionStart) ? input.selectionStart : value.length;
        const start = value.lastIndexOf(',', Math.max(0, caret - 1)) + 1;
        const nextComma = value.indexOf(',', caret);
        const end = nextComma === -1 ? value.length : nextComma;
        return {
            start,
            end,
            token: value.slice(start, end).trim()
        };
    }

    function getProviderSearchToken(input) {
        return getProviderTokenBounds(input).token;
    }

    function replaceProviderToken(input, value) {
        const current = input?.value || '';
        const bounds = getProviderTokenBounds(input);
        const before = current.slice(0, bounds.start);
        const after = current.slice(bounds.end);
        const leadingSpace = before && before.trimEnd().endsWith(',') && !/\s$/.test(before) ? ' ' : '';
        const replacement = `${leadingSpace}${value}`;

        if (after.trimStart().startsWith(',')) {
            input.value = before + replacement + after;
            input.selectionStart = input.selectionEnd = before.length + replacement.length;
            return;
        }

        input.value = before + replacement + ', ' + after.replace(/^\s+/, '');
        input.selectionStart = input.selectionEnd = before.length + replacement.length + 2;
    }

    function updateHint(panel, message) {
        const hint = panel.querySelector('.provider-routing-hint');
        if (hint) {
            hint.textContent = message || 'OpenRouter providers in order. Fallback behavior is controlled in OpenRouter settings.';
        }
    }

    function setAwesompleteList(input, providers) {
        if (input?._providerAwesomplete) {
            input._providerMetadataByValue = new Map((providers || []).map(provider => [provider.value, provider]));
            input._providerAwesomplete.list = providers || [];
        }
    }

    function getProviderOption(input, text) {
        const value = text?.value || '';
        return input?._providerMetadataByValue?.get(value) || text || {};
    }

    function formatCompactNumber(value, fractionDigits = 4) {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) return null;
        return parsed.toFixed(fractionDigits).replace(/\.?0+$/, '');
    }

    function appendDetailPart(container, valueText, suffixText = '') {
        if (!valueText) return false;
        if (container.dataset.hasDetailPart === 'true') {
            container.appendChild(document.createTextNode(', '));
        }
        const strong = document.createElement('strong');
        strong.className = 'provider-routing-metric-value';
        strong.textContent = valueText;
        container.appendChild(strong);
        if (suffixText) {
            container.appendChild(document.createTextNode(suffixText));
        }
        container.dataset.hasDetailPart = 'true';
        return true;
    }

    function appendProviderDetail(container, option) {
        const detail = document.createElement('span');
        detail.className = 'provider-routing-option-detail';

        const prompt = formatCompactNumber(option.prompt_per_million);
        appendDetailPart(detail, prompt ? `$${prompt}/m` : '', ' in');

        const completion = formatCompactNumber(option.completion_per_million);
        appendDetailPart(detail, completion ? `$${completion}/m` : '', ' out');

        const latency = formatCompactNumber(option.latency_seconds, 2);
        appendDetailPart(detail, latency ? `${latency}s` : '');

        const throughput = formatCompactNumber(option.throughput_tokens_per_second, 1);
        appendDetailPart(detail, throughput ? `${throughput}t/s` : '');

        const rawUptime = Number(option.uptime_last_30m);
        const uptimePct = Number.isFinite(rawUptime) ? (rawUptime <= 1 ? rawUptime * 100 : rawUptime) : null;
        const uptime = formatCompactNumber(uptimePct, 1);
        appendDetailPart(detail, uptime ? `${uptime}%` : '', ' uptime');

        if (detail.dataset.hasDetailPart === 'true') {
            container.appendChild(document.createTextNode(' ('));
            container.appendChild(detail);
            container.appendChild(document.createTextNode(')'));
            return;
        }

        if (option.detail) {
            container.appendChild(document.createTextNode(` (${option.detail})`));
        }
    }

    function createProviderItem(text, input, index) {
        const option = getProviderOption(this?.input, text);
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', 'false');
        li.setAttribute('tabindex', '-1');
        li.id = `awesomplete_list_${this?.count || 'provider'}_item_${index}`;

        const label = document.createElement('span');
        label.className = 'provider-routing-option-label';
        const name = document.createElement('span');
        name.className = 'provider-routing-option-name';
        name.textContent = option.provider_name || option.label || option.value || String(text || '');
        label.appendChild(name);

        const badges = Array.isArray(option.badges) ? option.badges : [];
        badges.forEach(badge => {
            const badgeEl = document.createElement('span');
            badgeEl.className = 'provider-routing-badge';
            badgeEl.title = badge.title || badge.label || '';
            if (badge.icon) {
                const iconEl = document.createElement('span');
                iconEl.className = 'provider-routing-badge-icon';
                iconEl.textContent = badge.icon;
                badgeEl.appendChild(iconEl);
            }
            if (badge.label) {
                const textEl = document.createElement('span');
                textEl.className = 'provider-routing-badge-text';
                textEl.textContent = badge.label;
                badgeEl.appendChild(textEl);
            }
            label.appendChild(badgeEl);
        });

        appendProviderDetail(label, option);
        li.appendChild(label);

        const value = option.value || text?.value || '';
        if (value && value !== option.label) {
            const code = document.createElement('span');
            code.className = 'provider-routing-option-value';
            code.textContent = value;
            li.appendChild(code);
        }

        return li;
    }

    function ensureProviderAwesomplete(input) {
        if (!window.Awesomplete || !input || input._providerAwesomplete) return;

        input._providerAwesomplete = new Awesomplete(input, {
            list: [],
            minChars: 0,
            maxItems: 20,
            autoFirst: false,
            sort: false,
            filter: (text, userInput) => {
                const token = getProviderSearchToken(input);
                if (!token) return true;
                const option = getProviderOption(input, text);
                const badges = Array.isArray(option.badges) ? option.badges.map(badge => badge.label || '').join(' ') : '';
                const haystack = `${option.label || ''} ${option.provider_name || ''} ${option.value || ''} ${option.detail || ''} ${badges}`;
                return haystack.toLowerCase().includes(token.toLowerCase());
            },
            item: createProviderItem,
            replace: function(text) {
                replaceProviderToken(this.input, text.value);
            }
        });

        input.addEventListener('awesomplete-selectcomplete', () => {
            input.dispatchEvent(new Event('change', { bubbles: true }));
        });
    }

    async function fetchProviderMetadata(model, forceRefresh = false) {
        const cleanModel = cleanModelForProviderLookup(model);
        if (!cleanModel || !cleanModel.includes('/')) return [];

        if (!forceRefresh && providerMetadataCache.has(cleanModel)) {
            return providerMetadataCache.get(cleanModel);
        }
        if (!forceRefresh && providerMetadataPromises.has(cleanModel)) {
            return providerMetadataPromises.get(cleanModel);
        }

        const url = `/api/openrouter-model-providers?model=${encodeURIComponent(cleanModel)}${forceRefresh ? '&refresh=1' : ''}`;
        const promise = fetch(url, { cache: 'no-store' })
            .then(resp => {
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return resp.json();
            })
            .then(data => {
                const providers = Array.isArray(data) ? data : [];
                providerMetadataCache.set(cleanModel, providers);
                return providers;
            })
            .catch(err => {
                console.warn('[ProviderRouting] Failed to load providers:', err);
                return providerMetadataCache.get(cleanModel) || [];
            })
            .finally(() => {
                providerMetadataPromises.delete(cleanModel);
            });

        providerMetadataPromises.set(cleanModel, promise);
        return promise;
    }

    async function loadProvidersForPanel(panel, forceRefresh = false) {
        const modelInput = document.getElementById(panel.dataset.inputId);
        const providerInput = panel.querySelector('.provider-routing-input');
        if (!modelInput || !providerInput || !isOpenRouterActive()) return [];

        const model = getModelForInput(modelInput);
        const cleanModel = cleanModelForProviderLookup(model);
        if (!cleanModel || !cleanModel.includes('/')) {
            setAwesompleteList(providerInput, []);
            updateHint(panel, 'Choose an OpenRouter model first.');
            return [];
        }

        ensureProviderAwesomplete(providerInput);
        updateHint(panel, 'Loading OpenRouter providers...');
        const providers = await fetchProviderMetadata(cleanModel, forceRefresh);
        setAwesompleteList(providerInput, providers);
        panel.dataset.loadedModel = cleanModel;
        updateHint(panel, providers.length
            ? 'OpenRouter providers in order. Fallback behavior is controlled in OpenRouter settings.'
            : 'No provider metadata found for this model.');
        return providers;
    }

    function openProviderSuggestions(input) {
        const awesomplete = input?._providerAwesomplete;
        if (!awesomplete) return;
        input.focus();
        awesomplete.evaluate();
        if (awesomplete.ul.childNodes.length > 0 && awesomplete.ul.hasAttribute('hidden')) {
            awesomplete.open();
        }
    }

    function getProviders(path) {
        if (typeof config === 'undefined') return [];
        return normalizeProviders(getPathValue(config, path));
    }

    function isOpenRouterActive() {
        return typeof config !== 'undefined' && config.llm?.provider === 'openrouter';
    }

    function getHostElement(input) {
        return input.closest('.model-autocomplete-combobox')
            || input.closest('.input-with-toggle')
            || input;
    }

    function formatSummary(providers) {
        if (!providers.length) return 'Providers';
        if (providers.length <= 2) return `Providers: ${providers.join(', ')}`;
        return `Providers: ${providers.length} selected`;
    }

    function hasVisibleOpenProviderPanel(scope) {
        return Array.from(scope.querySelectorAll('.provider-routing-panel.open')).some(openPanel => openPanel.style.display !== 'none');
    }

    function updateOverflowScopes(panel) {
        const scopes = [panel.closest('.sub-panel'), panel.closest('.chapter')].filter(Boolean);
        scopes.forEach(scope => {
            scope.classList.toggle('provider-routing-overflow-open', hasVisibleOpenProviderPanel(scope));
        });
    }

    function updatePanelState(panel, providers, forceOpenForSaved) {
        const input = panel.querySelector('.provider-routing-input');
        const summary = panel.querySelector('.provider-routing-summary');
        const toggle = panel.querySelector('.provider-routing-toggle');
        const drawer = panel.querySelector('.provider-routing-drawer');

        if (input) input.value = providers.join(', ');
        if (summary) summary.textContent = formatSummary(providers);

        if (forceOpenForSaved) {
            panel.classList.toggle('open', providers.length > 0);
        }

        const isOpen = panel.classList.contains('open');
        if (toggle) toggle.setAttribute('aria-expanded', String(isOpen));
        if (drawer) drawer.hidden = !isOpen;
        updateOverflowScopes(panel);
    }

    function saveProviders(path, value) {
        const providers = normalizeProviders(value);
        if (typeof updateSetting === 'function') {
            updateSetting(path, providers);
        }
        return providers;
    }

    function loadAndOpenProviderSuggestions(panel, input) {
        loadProvidersForPanel(panel).then(() => openProviderSuggestions(input));
    }

    function syncProviderInputs() {
        document.querySelectorAll('.provider-routing-panel').forEach(panel => {
            const input = panel.querySelector('.provider-routing-input');
            const path = panel.dataset.settingPath;
            if (!input || !path) return;
            const providers = saveProviders(path, input.value);
            updatePanelState(panel, providers, false);
        });
    }

    function createPanel(input, path) {
        const panel = document.createElement('div');
        panel.className = 'provider-routing-panel';
        panel.dataset.inputId = input.id;
        panel.dataset.settingPath = path;

        panel.innerHTML = `
            <button type="button" class="provider-routing-toggle" aria-expanded="false">
                <span class="provider-routing-summary">Providers</span>
                <span class="provider-routing-caret">&#9662;</span>
            </button>
            <div class="provider-routing-drawer" hidden>
                <input type="text" class="provider-routing-input" data-multiple placeholder="deepinfra/fp8, mistral" autocomplete="off" spellcheck="false">
                <p class="provider-routing-hint field-hint">OpenRouter providers in order. Fallback behavior is controlled in OpenRouter settings.</p>
            </div>
        `;

        const toggle = panel.querySelector('.provider-routing-toggle');
        const providerInput = panel.querySelector('.provider-routing-input');

        toggle.addEventListener('click', () => {
            panel.classList.toggle('open');
            updatePanelState(panel, normalizeProviders(providerInput.value), false);
            if (panel.classList.contains('open')) {
                loadAndOpenProviderSuggestions(panel, providerInput);
            }
        });

        providerInput.addEventListener('focus', () => {
            loadAndOpenProviderSuggestions(panel, providerInput);
        });

        providerInput.addEventListener('click', () => {
            loadAndOpenProviderSuggestions(panel, providerInput);
        });

        providerInput.addEventListener('keyup', event => {
            if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
                openProviderSuggestions(providerInput);
            }
        });

        providerInput.addEventListener('input', () => {
            const providers = normalizeProviders(providerInput.value);
            const summary = panel.querySelector('.provider-routing-summary');
            if (summary) summary.textContent = formatSummary(providers);
        });

        providerInput.addEventListener('change', () => {
            const providers = saveProviders(path, providerInput.value);
            updatePanelState(panel, providers, false);
        });

        const host = getHostElement(input);
        host.parentNode.insertBefore(panel, host.nextSibling);

        input.addEventListener('input', () => {
            if (modelInputDebounceTimers[input.id]) {
                clearTimeout(modelInputDebounceTimers[input.id]);
            }
            modelInputDebounceTimers[input.id] = setTimeout(() => {
                if (panel.classList.contains('open')) {
                    loadProvidersForPanel(panel, true);
                }
            }, 300);
        });

        return panel;
    }

    function initializePanel(id, path) {
        const input = document.getElementById(id);
        if (!input) return;

        let panel = document.querySelector(`.provider-routing-panel[data-input-id="${id}"]`);
        if (!panel) {
            panel = createPanel(input, path);
        }

        const providers = getProviders(path);
        panel.style.display = isOpenRouterActive() ? '' : 'none';
        updatePanelState(panel, providers, true);
    }

    function initProviderRouting() {
        for (const { id, path } of ROUTING_INPUTS) {
            initializePanel(id, path);
        }
        setTimeout(() => precacheProviderMetadata(), 0);
    }

    function refreshProviderRouting() {
        for (const { id, path } of ROUTING_INPUTS) {
            const panel = document.querySelector(`.provider-routing-panel[data-input-id="${id}"]`);
            if (!panel) {
                initializePanel(id, path);
                continue;
            }

            panel.style.display = isOpenRouterActive() ? '' : 'none';
            updatePanelState(panel, getProviders(path), true);
        }
        setTimeout(() => precacheProviderMetadata(), 0);
    }

    function getUniqueConfiguredModels() {
        const models = [];
        const seen = new Set();
        for (const { id } of ROUTING_INPUTS) {
            const input = document.getElementById(id);
            const cleanModel = cleanModelForProviderLookup(getModelForInput(input));
            if (!cleanModel || !cleanModel.includes('/')) continue;
            const key = cleanModel.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            models.push(cleanModel);
        }
        return models;
    }

    async function precacheProviderMetadata() {
        if (!isOpenRouterActive()) return;
        const models = getUniqueConfiguredModels();
        let index = 0;

        async function worker() {
            while (index < models.length) {
                const model = models[index++];
                await fetchProviderMetadata(model);
            }
        }

        await Promise.all([worker(), worker()]);
    }

    window.ProviderRouting = {
        init: initProviderRouting,
        refresh: refreshProviderRouting,
        sync: syncProviderInputs,
        normalizeProviders: normalizeProviders,
        precache: precacheProviderMetadata
    };
})();
