// ============================================
// Reasoning Toggle Module
// Per-model extended thinking toggles with capability detection
// ============================================

(function() {
    'use strict';

    // Module state
    let modelCapabilities = {};  // base_model_name -> {supports_reasoning, full_id}
    let capabilitiesLoaded = false;
    const debounceTimers = {};   // inputId -> timer

    // Background model inputs - don't show "slower" hint for these (background processing)
    const MEMORY_MODEL_INPUTS = new Set([
        'chapterModel',
        'proseModel',
        'graphitiModel',
        'graphitiSmallModel',
        'rerankerModel',
        'owlPostOrchestratorModel',
        'owlPostMailModel',
        'owlPostBoardModel'
    ]);

    // ============================================
    // Capability Fetching
    // ============================================

    /**
     * Fetch model capabilities from server (strips provider prefixes)
     * @returns {Promise<Object>} Map of base model names to capabilities
     */
    async function fetchModelCapabilities() {
        try {
            const resp = await fetch('/api/model-capabilities');
            if (resp.ok) {
                modelCapabilities = await resp.json();
                capabilitiesLoaded = true;
                console.log(`[ReasoningToggle] Loaded capabilities for ${Object.keys(modelCapabilities).length} models`);
                return modelCapabilities;
            }
        } catch (e) {
            console.warn('[ReasoningToggle] Failed to fetch model capabilities:', e);
        }
        return {};
    }

    /**
     * Check if a model supports reasoning
     * Handles both full IDs (openai/gpt-4o) and base names (gpt-4o)
     * Also strips OpenRouter modifiers like :nitro, :free, etc.
     * @param {string} modelName - Model name to check
     * @returns {boolean}
     */
    function supportsReasoning(modelName) {
        if (!modelName || !capabilitiesLoaded) return false;

        // Strip OpenRouter modifiers like :nitro, :free, etc.
        let cleanName = modelName.split(':')[0];

        // Strip provider prefix if present (user might enter "openai/gpt-4o")
        const baseName = cleanName.includes('/') ? cleanName.split('/').pop() : cleanName;

        // Direct lookup by base name
        if (modelCapabilities[baseName]) {
            return modelCapabilities[baseName].supports_reasoning;
        }

        // Try the full name as entered (in case it's already in the list)
        if (modelCapabilities[cleanName]) {
            return modelCapabilities[cleanName].supports_reasoning;
        }

        return false;
    }

    // ============================================
    // Toast Notifications
    // ============================================

    /**
     * Show reasoning toggle toast notification
     * @param {boolean} enabled - Whether reasoning is now enabled
     * @param {string} modelName - Name of the model
     */
    function showReasoningToast(enabled, modelName) {
        const container = document.getElementById('toastContainer');
        if (!container) return;

        const toast = document.createElement('div');
        toast.className = 'toast reasoning';

        const icon = enabled ? '&#10038;' : '&#10005;';
        const title = enabled ? 'Extended Thinking Enabled' : 'Extended Thinking Disabled';
        const displayName = modelName || 'Model';
        const desc = enabled
            ? `${displayName} will use deeper reasoning`
            : `${displayName} returns to standard mode`;

        toast.innerHTML = `
            <span class="toast-icon">${icon}</span>
            <div class="toast-text">
                <span class="toast-title">${title}</span>
                <span class="toast-desc">${desc}</span>
            </div>
        `;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 2500);
    }

    // ============================================
    // Thinking Hint Management
    // ============================================

    /**
     * Update the "thinking is slower" hint visibility
     * Only shown for non-memory models when thinking is active
     * @param {HTMLElement} wrapper - The input-with-toggle wrapper
     * @param {boolean} showHint - Whether to show the hint
     */
    function updateThinkingHint(wrapper, showHint) {
        const input = wrapper?.querySelector('input');
        if (!input) return;

        // Don't show hint for memory models (background processing, speed doesn't matter)
        if (MEMORY_MODEL_INPUTS.has(input.id)) {
            showHint = false;
        }

        let hint = wrapper.querySelector('.thinking-hint');

        if (showHint) {
            if (!hint) {
                hint = document.createElement('p');
                hint.className = 'thinking-hint field-hint';
                hint.textContent = 'Thinking takes longer. Keep it off for faster replies.';
                wrapper.appendChild(hint);
            }
            hint.style.display = '';
        } else if (hint) {
            hint.style.display = 'none';
        }
    }

    // ============================================
    // Toggle State Management
    // ============================================

    /**
     * Get the setting path for a model's reasoning toggle
     * e.g., "conv_chat_model" -> "conversation.chat_model_reasoning"
     *
     * IMPORTANT: Keep in sync with REASONING_CONTEXT_SETTINGS in utils/settings.py
     *
     * @param {string} inputId - The input element ID
     * @returns {string} Settings path for reasoning toggle
     */
    function getReasoningSettingPath(inputId) {
        // Map input IDs to their setting paths
        // See utils/settings.py REASONING_CONTEXT_SETTINGS for backend mapping
        const pathMap = {
            'conv_chat_model': 'conversation.chat_model_reasoning',
            'conv_target_model': 'conversation.target_selection_model_reasoning',
            'conv_interjection_model': 'conversation.interjection_model_reasoning',
            'conv_input_correction_model': 'conversation.input_correction_model_reasoning',
            'agent_vision_llm_model': 'agents.vision_reasoning',
            'owlPostOrchestratorModel': 'owl_post.orchestrator_model_reasoning',
            'owlPostMailModel': 'owl_post.mail_model_reasoning',
            'owlPostBoardModel': 'owl_post.board_model_reasoning',
            'chapterModel': 'memory.chapter_model_reasoning',
            'proseModel': 'memory.prose_model_reasoning',
            'graphitiModel': 'memory.graphiti_model_reasoning',
            'graphitiSmallModel': 'memory.graphiti_small_model_reasoning',
            'rerankerModel': 'memory.reranker_model_reasoning'
        };
        return pathMap[inputId] || null;
    }

    /**
     * Update toggle visual state based on model capability
     * @param {HTMLElement} toggleEl - The toggle switch element
     * @param {string} modelName - Current model name
     * @param {boolean} [savedState] - Optional saved reasoning state
     */
    function updateToggleState(toggleEl, modelName, savedState) {
        const supported = supportsReasoning(modelName);
        const wrapper = toggleEl.closest('.input-with-toggle');
        let isActive = false;

        if (supported) {
            toggleEl.classList.remove('disabled');
            toggleEl.onclick = () => handleToggleClick(toggleEl);
            toggleEl.title = 'Toggle extended thinking';

            // Restore saved state if provided, otherwise preserve current visual state
            if (savedState === true) {
                toggleEl.classList.add('active');
                isActive = true;
            } else if (savedState === undefined) {
                // No savedState provided - preserve current toggle state
                isActive = toggleEl.classList.contains('active');
            }
        } else {
            toggleEl.classList.add('disabled');
            toggleEl.classList.remove('active');
            toggleEl.onclick = null;
            toggleEl.title = 'This model does not support extended thinking';
        }

        // Update hint visibility
        updateThinkingHint(wrapper, isActive);
    }

    /**
     * Handle toggle click
     * @param {HTMLElement} toggleEl - The toggle element
     */
    function handleToggleClick(toggleEl) {
        if (toggleEl.classList.contains('disabled')) return;

        const isActive = toggleEl.classList.toggle('active');
        const wrapper = toggleEl.closest('.input-with-toggle');
        const input = wrapper?.querySelector('input');
        const modelName = input?.value || 'Model';

        // Save to settings
        const settingPath = getReasoningSettingPath(input?.id);
        if (settingPath && typeof updateSetting === 'function') {
            updateSetting(settingPath, isActive);
        }

        // Update hint visibility
        updateThinkingHint(wrapper, isActive);

        showReasoningToast(isActive, modelName);
    }

    /**
     * Handle model input change (debounced)
     * @param {HTMLInputElement} input - The model input element
     */
    function handleModelInputChange(input) {
        const inputId = input.id;

        // Clear existing debounce timer
        if (debounceTimers[inputId]) {
            clearTimeout(debounceTimers[inputId]);
        }

        // Debounce to avoid rapid API-like checks while typing
        debounceTimers[inputId] = setTimeout(() => {
            const wrapper = input.closest('.input-with-toggle');
            const toggleEl = wrapper?.querySelector('.reasoning-toggle-switch');
            if (toggleEl) {
                const wasActive = toggleEl.classList.contains('active');
                updateToggleState(toggleEl, input.value);

                // If it was active but model changed to unsupported, update setting
                if (wasActive && !supportsReasoning(input.value)) {
                    const settingPath = getReasoningSettingPath(inputId);
                    if (settingPath && typeof updateSetting === 'function') {
                        updateSetting(settingPath, false);
                    }
                }
            }
        }, 300);
    }

    // ============================================
    // DOM Manipulation
    // ============================================

    /**
     * Create a reasoning toggle element
     * @returns {HTMLElement}
     */
    function createToggleElement() {
        const toggle = document.createElement('div');
        toggle.className = 'reasoning-toggle-switch disabled';
        toggle.title = 'Loading model capabilities...';
        toggle.innerHTML = `
            <span class="switch-label">Think</span>
            <div class="mini-switch"></div>
        `;
        return toggle;
    }

    /**
     * Wrap an input with the toggle structure
     * @param {HTMLInputElement} input - The input to wrap
     * @returns {HTMLElement} The wrapper element
     */
    function wrapInputWithToggle(input) {
        // Check if already wrapped
        if (input.closest('.input-with-toggle')) {
            return input.closest('.input-with-toggle');
        }

        const wrapper = document.createElement('div');
        wrapper.className = 'input-with-toggle';

        // Insert wrapper before input, then move input into wrapper
        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(input);

        // Add toggle
        const toggle = createToggleElement();
        wrapper.appendChild(toggle);

        return wrapper;
    }

    /**
     * Initialize reasoning toggle for a model input
     * @param {string} inputId - ID of the model input element
     * @param {boolean} savedState - Saved reasoning state from config
     */
    function initializeToggle(inputId, savedState) {
        const input = document.getElementById(inputId);
        if (!input) return;

        const wrapper = wrapInputWithToggle(input);
        const toggleEl = wrapper.querySelector('.reasoning-toggle-switch');

        // Set up input change listener
        input.addEventListener('input', () => handleModelInputChange(input));
        input.addEventListener('change', () => handleModelInputChange(input));

        // Initial state update
        updateToggleState(toggleEl, input.value, savedState);
    }

    // ============================================
    // Public API
    // ============================================

    /**
     * Initialize all reasoning toggles on the page
     * Call this after config is loaded
     *
     * IMPORTANT: Keep modelInputs in sync with:
     * - getReasoningSettingPath() above
     * - REASONING_CONTEXT_SETTINGS in utils/settings.py
     *
     * @param {Object} config - The loaded config object
     */
    async function initReasoningToggles(config) {
        // Fetch capabilities first
        await fetchModelCapabilities();

        // Model inputs to enhance with reasoning toggles
        // See utils/settings.py REASONING_CONTEXT_SETTINGS for backend mapping
        const modelInputs = [
            { id: 'conv_chat_model', path: 'conversation.chat_model_reasoning' },
            { id: 'conv_target_model', path: 'conversation.target_selection_model_reasoning' },
            { id: 'conv_interjection_model', path: 'conversation.interjection_model_reasoning' },
            { id: 'conv_input_correction_model', path: 'conversation.input_correction_model_reasoning' },
            { id: 'agent_vision_llm_model', path: 'agents.vision_reasoning' },
            { id: 'owlPostOrchestratorModel', path: 'owl_post.orchestrator_model_reasoning' },
            { id: 'owlPostMailModel', path: 'owl_post.mail_model_reasoning' },
            { id: 'owlPostBoardModel', path: 'owl_post.board_model_reasoning' },
            { id: 'chapterModel', path: 'memory.chapter_model_reasoning' },
            { id: 'proseModel', path: 'memory.prose_model_reasoning' },
            { id: 'graphitiModel', path: 'memory.graphiti_model_reasoning' },
            { id: 'graphitiSmallModel', path: 'memory.graphiti_small_model_reasoning' },
            { id: 'rerankerModel', path: 'memory.reranker_model_reasoning' }
        ];

        for (const { id, path } of modelInputs) {
            // Get saved reasoning state from config
            const parts = path.split('.');
            let savedState = config;
            for (const part of parts) {
                savedState = savedState?.[part];
            }

            initializeToggle(id, savedState === true);
        }
    }

    /**
     * Refresh all reasoning toggles based on current input values.
     * Call this after programmatic changes (presets, form population, etc.)
     */
    function refreshAllToggles() {
        if (!capabilitiesLoaded) return;

        document.querySelectorAll('.input-with-toggle').forEach(wrapper => {
            const input = wrapper.querySelector('input');
            const toggleEl = wrapper.querySelector('.reasoning-toggle-switch');
            if (input && toggleEl) {
                const supported = supportsReasoning(input.value);

                // Get saved reasoning state from config
                const settingPath = getReasoningSettingPath(input.id);
                let savedState = false;
                if (settingPath && typeof config !== 'undefined') {
                    const parts = settingPath.split('.');
                    let value = config;
                    for (const part of parts) {
                        value = value?.[part];
                    }
                    savedState = value === true;
                }

                let isActive = false;
                if (supported) {
                    toggleEl.classList.remove('disabled');
                    toggleEl.onclick = () => handleToggleClick(toggleEl);
                    toggleEl.title = 'Toggle extended thinking';

                    // Apply saved state from config
                    if (savedState) {
                        toggleEl.classList.add('active');
                        isActive = true;
                    } else {
                        toggleEl.classList.remove('active');
                    }
                } else {
                    toggleEl.classList.add('disabled');
                    toggleEl.classList.remove('active');
                    toggleEl.onclick = null;
                    toggleEl.title = 'This model does not support extended thinking';

                    // If model doesn't support reasoning, ensure setting is false
                    if (settingPath && typeof updateSetting === 'function') {
                        updateSetting(settingPath, false);
                    }
                }

                // Update hint visibility
                updateThinkingHint(wrapper, isActive);
            }
        });
    }

    // Track master toggle state
    let masterEnabled = true;

    /**
     * Set master reasoning toggle state.
     * When OFF, all per-model toggles are greyed out (but state preserved).
     * When ON, per-model toggles work based on model capability.
     * @param {boolean} enabled - Master toggle state
     */
    function setMasterEnabled(enabled) {
        masterEnabled = enabled;

        document.querySelectorAll('.input-with-toggle').forEach(wrapper => {
            const toggleEl = wrapper.querySelector('.reasoning-toggle-switch');
            if (!toggleEl) return;

            if (!enabled) {
                // Master OFF: grey out all toggles, disable interaction
                toggleEl.classList.add('master-disabled');
                toggleEl.onclick = null;
                toggleEl.title = 'Enable reasoning in LLM Provider settings first';
            } else {
                // Master ON: restore normal behavior
                toggleEl.classList.remove('master-disabled');
                // Refresh to restore correct state based on model capability
                const input = wrapper.querySelector('input');
                if (input) {
                    const supported = supportsReasoning(input.value);
                    if (supported) {
                        toggleEl.classList.remove('disabled');
                        toggleEl.onclick = () => handleToggleClick(toggleEl);
                        toggleEl.title = 'Toggle extended thinking';
                    } else {
                        toggleEl.classList.add('disabled');
                        toggleEl.onclick = null;
                        toggleEl.title = 'This model does not support extended thinking';
                    }
                }
            }
        });
    }

    /**
     * Check if master toggle is enabled
     * @returns {boolean}
     */
    function isMasterEnabled() {
        return masterEnabled;
    }

    // Demo function for prototype section (can be removed later)
    function toggleReasoningDemo(toggleEl) {
        if (toggleEl.classList.contains('disabled')) return;
        const isActive = toggleEl.classList.toggle('active');
        const input = toggleEl.closest('.input-with-toggle')?.querySelector('input');
        showReasoningToast(isActive, input?.value || 'Model');
    }

    // Expose to global scope
    window.ReasoningToggle = {
        init: initReasoningToggles,
        refresh: refreshAllToggles,
        setMasterEnabled: setMasterEnabled,
        isMasterEnabled: isMasterEnabled,
        fetchCapabilities: fetchModelCapabilities,
        supportsReasoning: supportsReasoning,
        showToast: showReasoningToast
    };

    // Keep demo function global for prototype
    window.toggleReasoningDemo = toggleReasoningDemo;

})();
