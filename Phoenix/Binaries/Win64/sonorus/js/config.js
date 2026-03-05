// ============================================
// Provider Registry - Data-driven UI generation
// ============================================
const LANGUAGE_OPTIONS = [
    { value: "EN_US", label: "English (US)" },
    { value: "DE_DE", label: "German" },
    { value: "ES_ES", label: "Spanish" },
    { value: "FR_FR", label: "French" },
    { value: "IT_IT", label: "Italian" },
    { value: "PT_BR", label: "Portuguese (Brazil)" },
    { value: "JA_JP", label: "Japanese" },
    { value: "KO_KR", label: "Korean" },
    { value: "ZH_CN", label: "Chinese (Simplified)" }
];

function validateInworldApiKey(value) {
    if (!value || value === '********') return null;
    if (!/^[A-Za-z0-9+/]+=+$/.test(value)) {
        return '\u26a0\ufe0f This doesn\'t look like a valid Inworld key. It should be a Base64 string ending in "=" — make sure you\'re not pasting your LLM key here.';
    }
    return null;
}

const TTS_PROVIDERS = {
    none: {
        label: "Disabled (Subtitles Only)",
        description: "No voice synthesis. NPC responses shown as subtitles with lip sync animation. Useful for players who prefer reading or have limited bandwidth.",
        fields: []
    },
    inworld: {
        label: "Inworld AI (Recommended)",
        description: `New to Inworld? <a href="https://inworld.ai/signup?ref=7HQNN63N" target="_blank">Sign up with our link</a> to get $2 free credit (~3 hours of audio)! Then <a href="https://platform.inworld.ai" target="_blank">get your API key</a> from the Inworld Platform.<br>
                    <b>\u26a0\ufe0f You must select "Write" access when creating your key (not "Read")</b>.`,
        fields: [
            { id: "api_url", type: "text", label: "API URL", placeholder: "https://api.inworld.ai", default: "https://api.inworld.ai", hint: "Base URL for the Inworld API (leave default unless using a proxy)" },
            { id: "workspace_id", type: "text", label: "Workspace ID", placeholder: "default-xxxxx", hint: "Find this in the API Keys section (bottom left of sidebar) in the Inworld console" },
            { id: "api_key", type: "password", label: "API Key", placeholder: "Base64 encoded key", hint: "Use Basic (Base64) key, not JWT — <strong>\u26a0\ufe0f select \"Write\" access when creating (not \"Read\")</strong>", validate: validateInworldApiKey },
            { id: "model", type: "text", label: "TTS Model", placeholder: "inworld-tts-1.5-max", default: "inworld-tts-1.5-max", hint: "<strong>inworld-tts-1.5-max</strong> (highest quality, recommended), <strong>inworld-tts-1.5-mini</strong> (cheaper and slightly faster)", onChange: "onInworldModelChange" },
            { id: "temperature", type: "range", label: "TTS Temperature", hint: "Higher = more expressive but can cause instability/artifacts. Default is tuned for balance. For per-NPC adjustments, use <a href=\"#chapterCharacters\" onclick=\"scrollToSection('chapterCharacters')\">Characters</a>.", min: 0.1, max: 2.0, step: 0.1, default: 1.1 },
            {
                id: "sample_rate", type: "select", label: "Sample Rate", options: [
                    { value: 22050, label: "22050 Hz" },
                    { value: 24000, label: "24000 Hz" },
                    { value: 44100, label: "44100 Hz" },
                    { value: 48000, label: "48000 Hz" }
                ], default: 48000
            },
            { id: "localize_audio_tags", type: "toggle", label: "Localize Audio Tags", hint: "Translate [sigh], [laugh] etc. to language-specific equivalents for non-English languages. Disable if tags aren't being spoken correctly.", default: true },
            { id: "emotion_delivery", type: "toggle", label: "Emotion & Delivery Control", hint: "Experimental. TTS 1.5+ models only. Enables emotion/delivery style tags like [happy], [whispering] in the AI prompt.", default: false }
        ]
    },
    elevenlabs: {
        label: "ElevenLabs",
        description: "Pro plan recommended for optimal experience. Lower plans have reduced audio quality and fewer cloned voices (Free: 3, Starter: 10, Creator: 30, Pro: 100+). When voice limit is reached, least recently used clones are auto-deleted. Monthly voice clone operations: Free 55, Starter 65, Creator 95, Pro 290.",
        fields: [
            { id: "api_url", type: "text", label: "API URL", placeholder: "https://api.elevenlabs.io", default: "https://api.elevenlabs.io", hint: "Base URL for the ElevenLabs API (leave default unless using a proxy)" },
            { id: "api_key", type: "password", label: "API Key", placeholder: "xi-xxxxxxxx", hint: "Make sure your API key has Read + Write access" },
            {
                id: "plan", type: "select", label: "Plan", hint: "Determines max audio quality and voice slots", options: [
                    { value: "free", label: "Free (16kHz)" },
                    { value: "starter", label: "Starter (22kHz)" },
                    { value: "creator", label: "Creator (24kHz)" },
                    { value: "pro", label: "Pro (44.1kHz)" },
                    { value: "scale", label: "Scale (44.1kHz)" },
                    { value: "business", label: "Business (44.1kHz)" }
                ], default: "creator", onChange: "updateElevenLabsQuality"
            },
            { id: "model", type: "text", label: "Model", placeholder: "eleven_v3", default: "eleven_v3" },
            { id: "stability", type: "range", label: "Stability", hint: "Higher = more consistent, Lower = more expressive", min: 0, max: 1, step: 0.05, default: 0.5 },
            { id: "similarity_boost", type: "range", label: "Clarity + Similarity", min: 0, max: 1, step: 0.05, default: 0.75 },
            {
                id: "sample_rate", type: "select", label: "Sample Rate", hint: "Max rate depends on plan", options: [
                    { value: 16000, label: "16000 Hz" },
                    { value: 22050, label: "22050 Hz" },
                    { value: 24000, label: "24000 Hz" },
                    { value: 44100, label: "44100 Hz" }
                ], default: 24000
            }
        ]
    },
    pocket: {
        label: "Pocket TTS (Local, English Only)",
        description: "Local text-to-speech using the Pocket TTS model. Lightweight CPU-based synthesis with voice cloning support. No API key required. English only at the moment.",
        fields: [
            { id: "streaming", type: "toggle", label: "Streaming Mode", hint: "Disable if you experience audio hitching or game lag during speech", default: true }
        ]
    }
};

const LLM_PROVIDERS = {
    gemini: {
        label: "Google Gemini",
        fields: [
            { id: "reasoning_enabled", type: "toggle", label: "Enable Reasoning", hint: "Master switch for extended thinking. Enable per-model toggles below.", default: true }
        ]
    },
    openrouter: {
        label: "OpenRouter",
        fields: [
            { id: "reasoning_enabled", type: "toggle", label: "Enable Reasoning", hint: "Master switch for extended thinking. Enable per-model toggles below.", default: true }
        ]
    },
    openai: {
        label: "OpenAI",
        fields: [
            { id: "api_url", type: "text", label: "API URL (Optional)", placeholder: "https://api.openai.com/v1", hint: "Leave empty to use default OpenAI endpoint", onChange: "onOpenAIUrlChange" },
            { id: "responses_api", type: "toggle", label: "Use Responses API", hint: "Enable for endpoints that support the Responses API. Required for reasoning.", default: false },
            { id: "reasoning_enabled", type: "toggle", label: "Enable Reasoning", hint: "Master switch for extended thinking. Enable per-model toggles below.", default: true }
        ]
    }
};

// --- OpenAI Responses API toggle visibility ---

function isCustomNonOpenAIUrl() {
    const apiUrl = (config.llm?.openai?.api_url || '').trim();
    return apiUrl !== '' && !apiUrl.toLowerCase().includes('openai.com');
}

function applyOpenAIResponsesApiVisibility() {
    const responsesApiField = document.getElementById('llm_openai_responses_api');
    const responsesApiGroup = responsesApiField?.closest('.field-group');
    const reasoningField = document.getElementById('llm_openai_reasoning_enabled');
    const reasoningGroup = reasoningField?.closest('.field-group');

    if (!responsesApiGroup) return;

    const isCustomUrl = isCustomNonOpenAIUrl();

    if (isCustomUrl) {
        // Show responses_api toggle for custom non-OpenAI endpoints
        responsesApiGroup.style.display = '';

        // Strict true check: undefined/missing = false for custom URLs
        const responsesApiEnabled = config.llm?.openai?.responses_api === true;

        if (!responsesApiEnabled && reasoningField && reasoningGroup) {
            // Force reasoning OFF and disable (set config directly to avoid marking dirty on page load)
            reasoningField.checked = false;
            reasoningField.disabled = true;
            reasoningGroup.style.opacity = '0.5';
            if (config.llm?.openai) config.llm.openai.reasoning_enabled = false;

            // Add notice
            let notice = reasoningGroup.querySelector('.responses-api-notice');
            if (!notice) {
                notice = document.createElement('p');
                notice.className = 'field-hint responses-api-notice';
                notice.style.color = 'var(--warning, #f0ad4e)';
                notice.textContent = 'Responses API required for reasoning';
                reasoningGroup.appendChild(notice);
            }

            if (window.ReasoningToggle) {
                ReasoningToggle.setMasterEnabled(false);
            }
        } else {
            // Responses API ON - reasoning works normally
            if (reasoningField) reasoningField.disabled = false;
            if (reasoningGroup) reasoningGroup.style.opacity = '';
            const notice = reasoningGroup?.querySelector('.responses-api-notice');
            if (notice) notice.remove();

            // Restore master reasoning toggle to current checkbox state
            if (window.ReasoningToggle && reasoningField) {
                ReasoningToggle.setMasterEnabled(reasoningField.checked);
            }
        }
    } else {
        // Default OpenAI URL - hide responses_api toggle, force it ON
        responsesApiGroup.style.display = 'none';
        if (config.llm?.openai) {
            config.llm.openai.responses_api = true;
        }

        // Restore reasoning toggle to normal
        if (reasoningField) reasoningField.disabled = false;
        if (reasoningGroup) reasoningGroup.style.opacity = '';
        const notice = reasoningGroup?.querySelector('.responses-api-notice');
        if (notice) notice.remove();
    }
}

function onOpenAIUrlChange(value) {
    applyOpenAIResponsesApiVisibility();
}

function onResponsesApiToggle(enabled) {
    applyOpenAIResponsesApiVisibility();
}

// Deepgram language options by model capability
const DEEPGRAM_LANGUAGES_NOVA3 = [
    { value: "multi", label: "Multilingual (EN, ES, FR, DE, HI, RU, PT, JA, IT, NL)" },
    { value: "bg", label: "Bulgarian" },
    { value: "ca", label: "Catalan" },
    { value: "cs", label: "Czech" },
    { value: "da", label: "Danish" },
    { value: "da-DK", label: "Danish (Denmark)" },
    { value: "nl", label: "Dutch" },
    { value: "en", label: "English" },
    { value: "en-US", label: "English (US)" },
    { value: "en-AU", label: "English (Australia)" },
    { value: "en-GB", label: "English (UK)" },
    { value: "en-IN", label: "English (India)" },
    { value: "en-NZ", label: "English (New Zealand)" },
    { value: "et", label: "Estonian" },
    { value: "fi", label: "Finnish" },
    { value: "nl-BE", label: "Flemish" },
    { value: "fr", label: "French" },
    { value: "fr-CA", label: "French (Canada)" },
    { value: "de", label: "German" },
    { value: "de-CH", label: "German (Switzerland)" },
    { value: "el", label: "Greek" },
    { value: "hi", label: "Hindi" },
    { value: "hu", label: "Hungarian" },
    { value: "id", label: "Indonesian" },
    { value: "it", label: "Italian" },
    { value: "ja", label: "Japanese" },
    { value: "ko", label: "Korean" },
    { value: "ko-KR", label: "Korean (South Korea)" },
    { value: "lv", label: "Latvian" },
    { value: "lt", label: "Lithuanian" },
    { value: "ms", label: "Malay" },
    { value: "no", label: "Norwegian" },
    { value: "pl", label: "Polish" },
    { value: "pt", label: "Portuguese" },
    { value: "pt-BR", label: "Portuguese (Brazil)" },
    { value: "pt-PT", label: "Portuguese (Portugal)" },
    { value: "ro", label: "Romanian" },
    { value: "ru", label: "Russian" },
    { value: "sk", label: "Slovak" },
    { value: "es", label: "Spanish" },
    { value: "es-419", label: "Spanish (Latin America)" },
    { value: "sv", label: "Swedish" },
    { value: "sv-SE", label: "Swedish (Sweden)" },
    { value: "tr", label: "Turkish" },
    { value: "uk", label: "Ukrainian" },
    { value: "vi", label: "Vietnamese" }
];

const DEEPGRAM_LANGUAGES_NOVA2 = [
    { value: "multi", label: "Multilingual (EN, ES)" },
    { value: "bg", label: "Bulgarian" },
    { value: "ca", label: "Catalan" },
    { value: "zh-HK", label: "Chinese (Cantonese, Hong Kong)" },
    { value: "zh-CN", label: "Chinese (Mandarin, Mainland)" },
    { value: "zh-TW", label: "Chinese (Traditional, Taiwan)" },
    { value: "cs", label: "Czech" },
    { value: "da", label: "Danish" },
    { value: "nl", label: "Dutch" },
    { value: "nl-BE", label: "Dutch (Belgium)" },
    { value: "en", label: "English" },
    { value: "en-AU", label: "English (Australia)" },
    { value: "en-IN", label: "English (India)" },
    { value: "en-NZ", label: "English (New Zealand)" },
    { value: "en-GB", label: "English (United Kingdom)" },
    { value: "et", label: "Estonian" },
    { value: "fi", label: "Finnish" },
    { value: "fr", label: "French" },
    { value: "fr-CA", label: "French (Canada)" },
    { value: "de", label: "German" },
    { value: "de-CH", label: "German (Switzerland)" },
    { value: "hi", label: "Hindi" },
    { value: "hi-Latn", label: "Hindi (Latin)" },
    { value: "hu", label: "Hungarian" },
    { value: "id", label: "Indonesian" },
    { value: "it", label: "Italian" },
    { value: "ja", label: "Japanese" },
    { value: "ko", label: "Korean" },
    { value: "lv", label: "Latvian" },
    { value: "lt", label: "Lithuanian" },
    { value: "ms", label: "Malay" },
    { value: "el", label: "Modern Greek" },
    { value: "no", label: "Norwegian" },
    { value: "pl", label: "Polish" },
    { value: "pt", label: "Portuguese" },
    { value: "pt-BR", label: "Portuguese (Brazil)" },
    { value: "pt-PT", label: "Portuguese (Portugal)" },
    { value: "ro", label: "Romanian" },
    { value: "ru", label: "Russian" },
    { value: "sk", label: "Slovak" },
    { value: "es", label: "Spanish" },
    { value: "es-419", label: "Spanish (Latin America)" },
    { value: "sv", label: "Swedish" },
    { value: "taq", label: "Tamasheq" },
    { value: "ta", label: "Tamil" },
    { value: "th", label: "Thai" },
    { value: "tr", label: "Turkish" },
    { value: "uk", label: "Ukrainian" },
    { value: "vi", label: "Vietnamese" }
];

const WHISPER_LANGUAGES = [
    { value: "", label: "Auto-detect (Recommended)" },
    { value: "af", label: "Afrikaans" },
    { value: "ar", label: "Arabic" },
    { value: "hy", label: "Armenian" },
    { value: "az", label: "Azerbaijani" },
    { value: "be", label: "Belarusian" },
    { value: "bs", label: "Bosnian" },
    { value: "bg", label: "Bulgarian" },
    { value: "ca", label: "Catalan" },
    { value: "zh", label: "Chinese" },
    { value: "hr", label: "Croatian" },
    { value: "cs", label: "Czech" },
    { value: "da", label: "Danish" },
    { value: "nl", label: "Dutch" },
    { value: "en", label: "English" },
    { value: "et", label: "Estonian" },
    { value: "fi", label: "Finnish" },
    { value: "fr", label: "French" },
    { value: "gl", label: "Galician" },
    { value: "de", label: "German" },
    { value: "el", label: "Greek" },
    { value: "he", label: "Hebrew" },
    { value: "hi", label: "Hindi" },
    { value: "hu", label: "Hungarian" },
    { value: "is", label: "Icelandic" },
    { value: "id", label: "Indonesian" },
    { value: "it", label: "Italian" },
    { value: "ja", label: "Japanese" },
    { value: "kn", label: "Kannada" },
    { value: "kk", label: "Kazakh" },
    { value: "ko", label: "Korean" },
    { value: "lv", label: "Latvian" },
    { value: "lt", label: "Lithuanian" },
    { value: "mk", label: "Macedonian" },
    { value: "ms", label: "Malay" },
    { value: "mr", label: "Marathi" },
    { value: "mi", label: "Maori" },
    { value: "ne", label: "Nepali" },
    { value: "no", label: "Norwegian" },
    { value: "fa", label: "Persian" },
    { value: "pl", label: "Polish" },
    { value: "pt", label: "Portuguese" },
    { value: "ro", label: "Romanian" },
    { value: "ru", label: "Russian" },
    { value: "sr", label: "Serbian" },
    { value: "sk", label: "Slovak" },
    { value: "sl", label: "Slovenian" },
    { value: "es", label: "Spanish" },
    { value: "sw", label: "Swahili" },
    { value: "sv", label: "Swedish" },
    { value: "tl", label: "Tagalog" },
    { value: "ta", label: "Tamil" },
    { value: "th", label: "Thai" },
    { value: "tr", label: "Turkish" },
    { value: "uk", label: "Ukrainian" },
    { value: "ur", label: "Urdu" },
    { value: "vi", label: "Vietnamese" },
    { value: "cy", label: "Welsh" }
];

const STT_PROVIDERS = {
    none: {
        label: "Disabled",
        description: "Voice input is disabled. Use text chat to talk to NPCs.",
        fields: []
    },
    canary: {
        label: "Canary (Local, Recommended)",
        description: `Local speech recognition using NVIDIA Canary 180M Flash. No API key needed &mdash; runs entirely on your machine. Supports English, German, Spanish, and French. Requires ~250 MB of available RAM. Model is downloaded on first use.`,
        fields: []
    },
    parakeet: {
        label: "Parakeet (Local)",
        description: `Local speech recognition using NVIDIA Parakeet TDT 0.6B V3. No API key needed &mdash; runs entirely on your machine. Multilingual with automatic language detection. Requires ~1.5 GB of available RAM. Model is downloaded on first use.`,
        fields: []
    },
    moonshine: {
        label: "Moonshine (Local, English Only)",
        description: `A lighter-weight alternative to Parakeet. Local speech recognition using Moonshine Base. No API key needed &mdash; runs entirely on your machine. English only. Requires ~250 MB of available RAM. Model is downloaded on first use.`,
        fields: []
    },
    deepgram: {
        label: "Deepgram (Best)",
        description: `<a href="https://console.deepgram.com" target="_blank">Get your API key</a> from the Deepgram Console.<br>
                    New to Deepgram? <a href="https://console.deepgram.com/signup" target="_blank">Sign up</a> to get $200 free credit!`,
        fields: [
            { id: "api_key", type: "password", label: "API Key", placeholder: "your-deepgram-api-key" },
            { id: "model", type: "text", label: "Model", placeholder: "nova-3", default: "nova-3", hint: "nova-3 (recommended), nova-2 (more languages)" },
            { id: "language", type: "deepgram_language", label: "Language", default: "en-US" },
            { id: "model_improvement", type: "toggle", label: "Model Improvement Program (50% off)", hint: "Allow Deepgram to improve their services using your voice input data for 50% discount on API costs.", default: false, description_html: "<a href='https://developers.deepgram.com/docs/the-deepgram-model-improvement-partnership-program' target='_blank'>Learn more about the program</a>" }
        ]
    },
    whisper: {
        label: "Whisper",
        description: `Uses OpenAI's Whisper API for speech recognition. <a href="https://auth.openai.com/create-account" target="_blank">Create account</a>, then get API key from <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com</a>. Requires prepaid credits ($5 minimum top-up). Can also be used with local Whisper servers.`,
        fields: [
            { id: "api_key", type: "password", label: "API Key", placeholder: "API key" },
            { id: "api_url", type: "text", label: "API URL", placeholder: "https://api.openai.com/v1", default: "https://api.openai.com/v1", hint: "Default: OpenAI. Change for local Whisper or compatible endpoints." },
            { id: "model", type: "text", label: "Model", placeholder: "whisper-1", default: "whisper-1" },
            { id: "language", type: "select", label: "Language", options: WHISPER_LANGUAGES, default: "" }
        ]
    }
};

// ============================================
// Agent Configurations
// ============================================
const AGENT_CONFIGS = {
    vision: {
        label: "Vision Agent",
        icon: "&#128065;",
        description: "Captures screenshots and describes the scene to enrich conversation context",
        fields: [
            { id: "enabled", type: "toggle", label: "Enable Vision Agent", default: true },
            { id: "cooldown_seconds", type: "range", label: "Cooldown (seconds)", hint: "Minimum time between captures when starting voice/chat input", min: 1, max: 30, step: 1, default: 5 },
            { id: "wait_for_capture", type: "toggle", label: "Wait for Capture", hint: "Wait for vision capture to complete before AI responds. Disable if using a fast model.", default: true }
        ],
        llm: {
            fields: [
                { id: "model", type: "text", label: "Vision Model", hint: "Use a fast model for quick scene descriptions.", placeholder: "gemini-2.5-flash-lite", default: "gemini-2.5-flash-lite" },
                { id: "temperature", type: "range", label: "Temperature", min: 0, max: 2, step: 0.1, default: 0.7 },
                { id: "max_tokens", type: "range", label: "Max Tokens", hint: "High default accounts for reasoning budgets. Reduce if errors occur.", min: 128, max: 16384, step: 128, default: 8192 }
            ]
        }
    }
};

// ============================================
// Dynamic Field Rendering
// ============================================
function renderField(field, category, providerId) {
    const settingPath = `${category}.${providerId}.${field.id}`;
    const fieldId = `${category}_${providerId}_${field.id}`;
    const currentValue = config[category]?.[providerId]?.[field.id] ?? field.default ?? '';

    let html = `<div class="field-group">`;
    html += `<label class="field-label">${escapeHtml(field.label)}</label>`;

    if (field.hint) {
        html += `<p class="field-hint">${field.hint}</p>`;
    }

    switch (field.type) {
        case 'text':
            const textExtraHandler = field.onChange ? `; ${field.onChange}(this.value)` : '';
            html += `<input type="text" id="${fieldId}"
                             placeholder="${escapeHtml(field.placeholder || '')}"
                             value="${escapeHtml(currentValue)}"
                             onchange="updateProviderSetting('${category}', '${providerId}', '${field.id}', this.value)${textExtraHandler}">`;
            break;

        case 'password':
            const displayValue = currentValue ? '********' : '';
            const pwHandler = `updateProviderSetting('${category}', '${providerId}', '${field.id}', this.value)`;
            html += `<input type="password" id="${fieldId}"
                             placeholder="${escapeHtml(field.placeholder || '')}"
                             value="${escapeHtml(displayValue)}"
                             autocomplete="off"
                             autocorrect="off"
                             autocapitalize="off"
                             spellcheck="false"
                             data-lpignore="true"
                             data-1p-ignore="true"
                             data-form-type="other"
                             oninput="${pwHandler}"
                             onchange="${pwHandler}">`;
            if (field.validate) {
                html += `<p class="field-hint" id="${fieldId}_validation" style="color: var(--danger); margin-top: 6px; display: none;"></p>`;
            }
            break;

        case 'select':
            const onChangeHandler = field.onChange
                ? `updateProviderSetting('${category}', '${providerId}', '${field.id}', this.value); ${field.onChange}(this.value)`
                : `updateProviderSetting('${category}', '${providerId}', '${field.id}', this.value)`;
            html += `<select id="${fieldId}" onchange="${onChangeHandler}">`;
            for (const opt of field.options) {
                const selected = String(opt.value) === String(currentValue) ? 'selected' : '';
                html += `<option value="${opt.value}" ${selected}>${escapeHtml(opt.label)}</option>`;
            }
            html += `</select>`;
            break;

        case 'range':
            const rangeValue = currentValue !== '' ? currentValue : field.default;
            html += `<div class="range-wrapper">
                        <input type="range" id="${fieldId}"
                               min="${field.min}" max="${field.max}" step="${field.step}" value="${rangeValue}"
                               oninput="updateRangeValue('${fieldId}', this.value); updateProviderSetting('${category}', '${providerId}', '${field.id}', parseFloat(this.value))">
                        <span class="range-value" id="${fieldId}Value">${rangeValue}</span>
                    </div>`;
            break;

        case 'toggle':
            const checked = currentValue !== '' ? currentValue : field.default;
            // Special handlers for LLM toggles
            let extraHandler = '';
            if (category === 'llm' && field.id === 'reasoning_enabled') {
                extraHandler = '; if (window.ReasoningToggle) ReasoningToggle.setMasterEnabled(this.checked)';
            } else if (category === 'llm' && field.id === 'responses_api') {
                extraHandler = '; onResponsesApiToggle(this.checked)';
            } else if (field.onChange) {
                extraHandler = `; ${field.onChange}(this.checked)`;
            }
            html += `<div class="toggle-wrapper" style="padding: 0;">
                        <label class="toggle">
                            <input type="checkbox" id="${fieldId}" ${checked ? 'checked' : ''}
                                   onchange="updateProviderSetting('${category}', '${providerId}', '${field.id}', this.checked)${extraHandler}">
                            <span class="toggle-track">
                                <span class="toggle-thumb"></span>
                            </span>
                        </label>
                    </div>`;
            if (field.description_html) {
                html += `<p class="field-hint" style="margin-top: var(--space-xs);">${field.description_html}</p>`;
            }
            break;

        case 'deepgram_language':
            // Dynamic language dropdown that updates based on model
            const langValue = currentValue !== '' ? currentValue : field.default;
            html += `<select id="${fieldId}" onchange="updateProviderSetting('${category}', '${providerId}', '${field.id}', this.value)">`;
            // Will be populated by updateDeepgramLanguages() after render
            html += `</select>`;
            break;
    }

    html += `</div>`;
    return html;
}

function renderProviderSettings(category, providerId) {
    const providers = category === 'tts' ? TTS_PROVIDERS : {};
    const providerConfig = providers[providerId];
    const container = document.getElementById(`${category}ProviderSettings`);

    if (!providerConfig || !container) {
        console.warn(`No config for ${category}/${providerId}`);
        return;
    }

    let html = '';
    if (providerConfig.description) {
        html += `<p class="field-hint" style="margin-bottom: var(--space-md);">${providerConfig.description}</p>`;
    }
    html += providerConfig.fields.map(f => renderField(f, category, providerId)).join('');
    container.innerHTML = html;
}

function switchProvider(category, providerId) {
    updateSetting(`${category}.provider`, providerId);
    renderProviderSettings(category, providerId);

    // Handle TTS-specific UI updates
    if (category === 'tts') {
        updatePlayerVoiceSectionState(providerId);
        updateVramMonitoring();
        updateRamMonitoring();
    }
}

// Disable/enable player voice and pronunciation sections based on TTS provider
function updatePlayerVoiceSubSettings(enabled) {
    const container = document.getElementById('playerVoiceSubSettings');
    if (container) {
        container.style.opacity = enabled ? '1' : '0.5';
        container.style.pointerEvents = enabled ? 'auto' : 'none';
    }
}

function updatePlayerVoiceSectionState(providerId) {
    const isDisabled = providerId === 'none';
    const section = document.getElementById('playerVoiceSection');
    const toggle = document.getElementById('playerVoiceEnabled');
    const input = document.getElementById('playerVoiceName');
    const pronunciationSection = document.getElementById('pronunciationSection');
    const pronunciationTextarea = document.getElementById('pronunciationReplacements');

    if (section) {
        section.style.opacity = isDisabled ? '0.5' : '1';
        section.style.pointerEvents = isDisabled ? 'none' : 'auto';
    }
    if (toggle) {
        toggle.disabled = isDisabled;
    }
    if (input) {
        input.disabled = isDisabled;
    }
    if (pronunciationSection) {
        pronunciationSection.style.opacity = isDisabled ? '0.5' : '1';
        pronunciationSection.style.pointerEvents = isDisabled ? 'none' : 'auto';
    }
    if (pronunciationTextarea) {
        pronunciationTextarea.disabled = isDisabled;
    }

    // Update sub-settings (spatial, voice override) based on player voice toggle
    if (!isDisabled && toggle) {
        updatePlayerVoiceSubSettings(toggle.checked);
    }
}

// ============================================
// VRAM Monitoring for NeuTTS GPU mode
// ============================================
let vramMonitorInterval = null;

function updateVramMonitoring() {
    const provider = config.tts?.provider;
    const uiDevice = config.tts?.neutts?.device;

    // Only monitor when NeuTTS + GPU selected in UI
    if (provider !== 'neutts' || uiDevice !== 'cuda') {
        stopVramMonitoring();
        return;
    }

    startVramMonitoring();
}

function startVramMonitoring() {
    const indicator = document.getElementById('vramIndicator');
    if (indicator) {
        indicator.style.display = 'block';
    }

    // Poll immediately, then every 2s
    fetchVramStatus();
    if (!vramMonitorInterval) {
        vramMonitorInterval = setInterval(fetchVramStatus, 2000);
    }
}

function stopVramMonitoring() {
    if (vramMonitorInterval) {
        clearInterval(vramMonitorInterval);
        vramMonitorInterval = null;
    }
    const indicator = document.getElementById('vramIndicator');
    if (indicator) {
        indicator.style.display = 'none';
    }
}

async function fetchVramStatus() {
    try {
        const resp = await fetch('/api/tts/vram-status');
        const data = await resp.json();
        updateVramDisplay(data);
    } catch (e) {
        console.error('[VRAM] Status check failed:', e);
    }
}

function updateVramDisplay(data) {
    const fill = document.getElementById('vramFill');
    const value = document.getElementById('vramValue');
    const status = document.getElementById('vramStatus');
    const label = document.getElementById('vramLabel');

    if (!fill || !value || !status || !label) return;

    if (!data.cuda_available) {
        label.textContent = 'GPU VRAM';
        value.textContent = 'No CUDA GPU detected';
        status.textContent = 'No CUDA GPU found. Please select CPU mode instead.';
        status.style.color = 'var(--error)';
        fill.style.width = '0%';
        fill.className = 'vram-fill';
        return;
    }

    const free = data.vram_free_gb;
    const total = data.vram_total_gb;
    const used = data.vram_used_gb;
    const usedPercent = (used / total) * 100;

    // Update label based on model state
    if (data.model_on_gpu) {
        label.textContent = 'GPU VRAM (NeuTTS loaded)';
        value.textContent = `${used.toFixed(1)} / ${total.toFixed(1)} GB used`;
    } else {
        label.textContent = 'GPU VRAM Available';
        value.textContent = `${free.toFixed(1)} / ${total.toFixed(1)} GB free`;
    }

    // Update bar (shows usage, so higher = more used)
    fill.style.width = usedPercent + '%';

    // Color based on FREE space (danger zone is <3GB free)
    fill.className = 'vram-fill';
    if (free >= 5) {
        fill.classList.add('green');
    } else if (free >= 3) {
        fill.classList.add('yellow');
    } else {
        fill.classList.add('red');
    }

    // Status message
    if (data.model_on_gpu) {
        // Model already on GPU - warn if <1GB free
        if (free < 1) {
            status.textContent = 'Very low VRAM! May cause crashes.';
            status.style.color = 'var(--error)';
        } else if (free < 2) {
            status.textContent = 'Low VRAM remaining.';
            status.style.color = 'var(--warning)';
        } else {
            status.textContent = 'Model loaded on GPU.';
            status.style.color = 'var(--success)';
        }
    } else {
        // Model NOT on GPU - warn if <3GB available
        if (free < 3) {
            status.textContent = 'CPU recommended - insufficient VRAM for GPU mode (~3GB needed).';
            status.style.color = 'var(--error)';
        } else if (free < 4) {
            status.textContent = 'Tight fit - GPU mode may work but monitor usage.';
            status.style.color = 'var(--warning)';
        } else {
            status.textContent = 'Sufficient VRAM for GPU mode.';
            status.style.color = 'var(--success)';
        }
    }
}

// ============================================
// RAM Monitoring for local models (Parakeet STT, Pocket TTS)
// ============================================
let ramMonitorInterval = null;

// Which RAM indicators are currently active
const RAM_INDICATORS = {
    stt: { id: 'ramIndicator', active: false, label: 'Parakeet', needed: 1.5 },
    stt_canary: { id: 'canaryRamIndicator', active: false, label: 'Canary', needed: 0.25 },
    stt_moonshine: { id: 'moonshineRamIndicator', active: false, label: 'Moonshine', needed: 0.25 },
    tts: { id: 'ttsRamIndicator', active: false, label: 'Pocket TTS', needed: 0.5 }
};

function updateRamMonitoring() {
    const sttNeedsRam = config.stt?.provider === 'parakeet';
    const sttCanaryNeedsRam = config.stt?.provider === 'canary';
    const sttMoonshineNeedsRam = config.stt?.provider === 'moonshine';
    const ttsNeedsRam = config.tts?.provider === 'pocket';

    RAM_INDICATORS.stt.active = sttNeedsRam;
    RAM_INDICATORS.stt_canary.active = sttCanaryNeedsRam;
    RAM_INDICATORS.stt_moonshine.active = sttMoonshineNeedsRam;
    RAM_INDICATORS.tts.active = ttsNeedsRam;

    // Show/hide each indicator
    for (const info of Object.values(RAM_INDICATORS)) {
        const el = document.getElementById(info.id);
        if (el) el.style.display = info.active ? 'block' : 'none';
    }

    // Start or stop the shared polling interval
    const anyActive = sttNeedsRam || sttCanaryNeedsRam || sttMoonshineNeedsRam || ttsNeedsRam;
    if (anyActive) {
        fetchRamStatus();
        if (!ramMonitorInterval) {
            ramMonitorInterval = setInterval(fetchRamStatus, 2000);
        }
    } else {
        if (ramMonitorInterval) {
            clearInterval(ramMonitorInterval);
            ramMonitorInterval = null;
        }
    }
}

async function fetchRamStatus() {
    try {
        const resp = await fetch('/api/system/ram-status');
        const data = await resp.json();
        for (const [key, info] of Object.entries(RAM_INDICATORS)) {
            if (info.active) updateRamIndicator(info, data);
        }
    } catch (e) {
        console.error('[RAM] Status check failed:', e);
    }
}

function updateRamIndicator(info, data) {
    const el = document.getElementById(info.id);
    if (!el) return;

    const fill = el.querySelector('.vram-fill');
    const value = el.querySelector('.vram-value');
    const status = el.querySelector('.field-hint');
    const label = el.querySelector('.field-label');

    if (!fill || !value || !status || !label) return;

    const free = data.ram_free_gb;
    const total = data.ram_total_gb;
    const used = data.ram_used_gb;
    if (!total) return;
    const usedPercent = (used / total) * 100;
    const needed = info.needed;

    label.textContent = 'System RAM';
    value.textContent = `${free.toFixed(1)} / ${total.toFixed(1)} GB free`;

    fill.style.width = usedPercent + '%';

    fill.className = 'vram-fill';
    if (free >= needed * 2.5) {
        fill.classList.add('green');
    } else if (free >= needed) {
        fill.classList.add('yellow');
    } else {
        fill.classList.add('red');
    }

    if (free < needed) {
        status.textContent = `Insufficient RAM for ${info.label} (~${needed} GB needed). Close other applications to free memory.`;
        status.style.color = 'var(--error)';
    } else if (free < needed * 2) {
        status.textContent = `Low RAM available. ${info.label} should work but performance may be affected.`;
        status.style.color = 'var(--warning)';
    } else {
        status.textContent = `Sufficient RAM available for ${info.label}.`;
        status.style.color = 'var(--success)';
    }
}

const LLM_PROVIDER_HINTS = {
    gemini: 'Google\'s Gemini API with free tier. <a href="https://aistudio.google.com/app/apikey" target="_blank">Get your free API key</a>. If you experience errors, you may be at the daily limit, in which case we recommend you switch to OpenRouter.',
    openrouter: '<strong>(Recommended)</strong> Access 100+ AI models through one API. <a href="https://openrouter.ai/" target="_blank">Sign up at openrouter.ai</a> and add credits ($5 minimum purchase - generally lasts a long time).',
    openai: 'Direct access to OpenAI models (GPT-5, etc). <a href="https://auth.openai.com/create-account" target="_blank">Create account</a>, then get API key from <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com</a>. Requires prepaid credits ($5 minimum top-up).'
};

const LLM_API_KEY_VALIDATORS = {
    gemini: { prefix: 'AIza', label: 'Gemini' },
    openrouter: { prefix: 'sk-or-v1-', label: 'OpenRouter' }
};

function validateLLMApiKey(value, providerId) {
    if (!value || value === '********') return null;
    const rule = LLM_API_KEY_VALIDATORS[providerId];
    if (!rule) return null;
    // Still typing the prefix — don't flag yet
    if (value.length < rule.prefix.length && rule.prefix.startsWith(value)) return null;
    if (!value.startsWith(rule.prefix)) {
        return `\u26a0\ufe0f ${rule.label} keys start with "${rule.prefix}" — this doesn't look right. Make sure you're not pasting your Voice/TTS key here.`;
    }
    return null;
}

// Gemini 3 Flash - switches to GA version after June 2026
const GEMINI_3_GA_DATE = new Date('2026-06-01');
const GEMINI_3_IS_GA = new Date() >= GEMINI_3_GA_DATE;
const GEMINI_3_FLASH = GEMINI_3_IS_GA ? 'gemini-3-flash' : 'gemini-3-flash-preview';
const GEMINI_3_FLASH_OR = GEMINI_3_IS_GA ? 'google/gemini-3-flash' : 'google/gemini-3-flash-preview';

// Model presets per provider - loaded from shared JSON file (with fallback)
let MODEL_PRESETS = null;
let MODEL_FIELDS_PATHS = null;

async function loadModelPresets() {
    if (MODEL_PRESETS !== null) return MODEL_PRESETS;

    try {
        const response = await fetch('/data/model_presets.json');
        const data = await response.json();

        // Extract presets (skip underscore-prefixed keys)
        MODEL_PRESETS = {};
        for (const [key, value] of Object.entries(data)) {
            if (!key.startsWith('_')) {
                MODEL_PRESETS[key] = value;
            }
        }

        // Extract field paths
        MODEL_FIELDS_PATHS = data._model_fields || {};

        // Apply Gemini 3 GA date logic (always, to ensure consistency)
        if (MODEL_PRESETS.gemini) {
            MODEL_PRESETS.gemini.chat = GEMINI_3_FLASH;
        }
        if (MODEL_PRESETS.openrouter) {
            MODEL_PRESETS.openrouter.chat = GEMINI_3_FLASH_OR;
        }

        console.log('[ModelPresets] Loaded presets for providers:', Object.keys(MODEL_PRESETS));
        return MODEL_PRESETS;
    } catch (e) {
        console.error('[ModelPresets] Failed to load presets, using fallback:', e);
        // Fallback to hardcoded defaults if JSON load fails
        MODEL_PRESETS = getHardcodedPresets();
        return MODEL_PRESETS;
    }
}

function getHardcodedPresets() {
    // Fallback presets if JSON fails to load (must match model_presets.json)
    return {
        gemini: {
            chat: GEMINI_3_FLASH,
            vision: 'gemini-2.5-flash-lite',
            target: 'gemini-2.5-flash-lite',
            interjection: 'gemini-2.5-flash-lite',
            inputCorrection: 'gemini-2.5-flash-lite',
            chapter: 'gemini-2.5-flash-lite',
            prose: 'gemini-2.5-flash-lite',
            graphiti: 'gemini-2.5-flash-lite',
            graphitiSmall: 'gemini-2.5-flash-lite',
            reranker: 'gemini-2.5-flash-lite'
        },
        openrouter: {
            chat: GEMINI_3_FLASH_OR,
            vision: 'google/gemini-2.5-flash-lite:nitro',
            target: 'google/gemini-2.5-flash-lite:nitro',
            interjection: 'google/gemini-2.5-flash-lite:nitro',
            inputCorrection: 'meta-llama/llama-3.1-8b-instruct:nitro',
            chapter: 'x-ai/grok-4.1-fast',
            prose: 'x-ai/grok-4.1-fast',
            graphiti: 'x-ai/grok-4.1-fast',
            graphitiSmall: 'google/gemini-2.5-flash-lite:nitro',
            reranker: 'meta-llama/llama-3.1-8b-instruct:nitro'
        },
        openai: {
            chat: 'gpt-5-mini',
            vision: 'gpt-5-nano',
            target: 'gpt-5-nano',
            interjection: 'gpt-5-nano',
            inputCorrection: 'gpt-4.1-nano',
            chapter: 'gpt-5-nano',
            prose: 'gpt-5-nano',
            graphiti: 'gpt-5-nano',
            graphitiSmall: 'gpt-5-nano',
            reranker: 'gpt-4.1-nano'
        }
    };
}

// Model field mappings: { key: { settingPath, elementId, isAgent } }
// Easy to extend with new model fields
const MODEL_FIELDS = {
    chat: { path: 'conversation.chat_model', elementId: 'conv_chat_model' },
    vision: { path: 'agents.vision.llm.model', elementId: 'agent_vision_llm_model', isAgent: true, agentId: 'vision', prefix: 'llm', fieldId: 'model' },
    target: { path: 'conversation.target_selection_model', elementId: 'conv_target_model' },
    interjection: { path: 'conversation.interjection_model', elementId: 'conv_interjection_model' },
    inputCorrection: { path: 'conversation.input_correction_model', elementId: 'conv_input_correction_model' },
    chapter: { path: 'memory.chapter_model', elementId: 'chapterModel' },
    prose: { path: 'memory.prose_model', elementId: 'proseModel' },
    graphiti: { path: 'memory.graphiti_model', elementId: 'graphitiModel' },
    graphitiSmall: { path: 'memory.graphiti_small_model', elementId: 'graphitiSmallModel' },
    reranker: { path: 'memory.reranker_model', elementId: 'rerankerModel' }
};

// Default reasoning toggle states per provider and model field
// Only specified fields will have reasoning enabled by default
const REASONING_DEFAULTS = {
    gemini: {
        graphiti: true,          // gemini-2.5-flash-lite
        graphitiSmall: true      // gemini-2.5-flash-lite
    },
    openrouter: {
        graphiti: false,         // x-ai/grok-4.1-fast
        graphitiSmall: true      // google/gemini-2.5-flash-lite:nitro
    },
    openai: {
        graphiti: true,          // gpt-5-nano
        graphitiSmall: true      // gpt-5-nano
    }
};

// Default feature toggle states per provider
// Maps setting path -> { default, elementId } per provider
const FEATURE_DEFAULTS = {
    gemini: {
        'conversation.input_correction_enabled': { default: false, elementId: 'conv_input_correction_enabled' }  // Gemini free tier has rate limits
    },
    openrouter: {
        'conversation.input_correction_enabled': { default: true, elementId: 'conv_input_correction_enabled' }
    },
    openai: {
        'conversation.input_correction_enabled': { default: true, elementId: 'conv_input_correction_enabled' }
    }
};

// Update model placeholders only (for page load) - doesn't change values
async function updateModelPlaceholders(providerId) {
    await loadModelPresets();
    const presets = MODEL_PRESETS[providerId];
    if (!presets) return;

    for (const [key, field] of Object.entries(MODEL_FIELDS)) {
        const newModel = presets[key];
        if (!newModel) continue;

        const element = document.getElementById(field.elementId);
        if (!element) continue;

        // Only update placeholder, not the value
        element.placeholder = newModel;
    }
}

// Apply model presets when switching providers - always resets to provider defaults
async function applyModelPresets(newProviderId) {
    await loadModelPresets();
    const presets = MODEL_PRESETS[newProviderId];
    if (!presets) return;

    for (const [key, field] of Object.entries(MODEL_FIELDS)) {
        const newModel = presets[key];
        if (!newModel) continue;

        const element = document.getElementById(field.elementId);
        if (!element) continue;

        // Always reset to provider's default model
        element.placeholder = newModel;
        const oldValue = element.value;
        element.value = newModel;

        // Use appropriate update function based on field type
        if (field.isAgent) {
            updateAgentSetting(field.agentId, field.prefix, field.fieldId, newModel);
        } else {
            updateSetting(field.path, newModel);
        }

        if (oldValue !== newModel) {
            console.log(`[ModelPresets] ${key}: ${oldValue} -> ${newModel}`);
        }
    }

    // Apply reasoning defaults for this provider
    const reasoningDefaults = REASONING_DEFAULTS[newProviderId];
    if (reasoningDefaults) {
        for (const [key, shouldEnable] of Object.entries(reasoningDefaults)) {
            const field = MODEL_FIELDS[key];
            if (!field) continue;

            // Build the reasoning setting path (e.g., "memory.graphiti_model_reasoning")
            const reasoningPath = field.path + '_reasoning';
            updateSetting(reasoningPath, shouldEnable);

            console.log(`[ModelPresets] ${key} reasoning: ${shouldEnable}`);
        }
    }

    // Apply feature defaults for this provider (e.g., input_correction_enabled)
    const featureDefaults = FEATURE_DEFAULTS[newProviderId];
    if (featureDefaults) {
        for (const [path, info] of Object.entries(featureDefaults)) {
            updateSetting(path, info.default);
            // Update corresponding checkbox if it exists
            const checkbox = document.getElementById(info.elementId);
            if (checkbox) {
                checkbox.checked = info.default;
            }
            console.log(`[ModelPresets] ${path}: ${info.default}`);
        }
    }

    // Refresh reasoning toggles after model changes
    if (window.ReasoningToggle) {
        ReasoningToggle.refresh();
    }

    updateGemini3TempHint();
}

// API key placeholders per LLM provider
const LLM_API_KEY_PLACEHOLDERS = {
    gemini: 'AIza...',
    openrouter: 'sk-or-v1-...',
    openai: 'API key'
};

function updateLLMApiKey(value) {
    const normalized = typeof value === 'string' ? value.trim() : value;

    // Don't save masked placeholder values
    if (normalized === '********') return;

    const provider = document.getElementById('llmProvider').value;
    // Store in provider-specific location
    if (!config.llm) config.llm = {};
    if (!config.llm[provider]) config.llm[provider] = {};
    config.llm[provider].api_key = normalized;
    // Also update legacy field for backwards compatibility
    config.llm.api_key = normalized;
    updateLLMApiKeyValidation(provider);
    markDirty();
}

function refreshLLMApiKeyField(providerId) {
    const keyField = document.getElementById('llmApiKey');
    if (!keyField) return;

    // Show provider key; fallback to legacy only when no provider-specific keys exist.
    const providerKey = config.llm?.[providerId]?.api_key;
    const legacyKey = config.llm?.api_key;
    const hasProviderSpecificKeys = Boolean(
        config.llm?.gemini?.api_key || config.llm?.openrouter?.api_key || config.llm?.openai?.api_key
    );
    const hasKey = providerKey || (legacyKey && !hasProviderSpecificKeys);

    // Show masked value if key exists for this provider, empty otherwise
    keyField.value = hasKey ? '********' : '';
    keyField.placeholder = LLM_API_KEY_PLACEHOLDERS[providerId] || '';
    updateLLMApiKeyValidation(providerId);
}

async function switchLLMProvider(providerId) {
    updateSetting('llm.provider', providerId);
    await applyModelPresets(providerId);
    refreshLLMApiKeyField(providerId);
    renderLLMProviderSettings(providerId);
    updateLLMProviderHint(providerId);
    updateConcurrencyHints(providerId);

    // Update master reasoning toggle based on new provider's setting
    if (window.ReasoningToggle) {
        let masterEnabled = config.llm?.[providerId]?.reasoning_enabled === true;
        // For OpenAI with custom URL and responses_api OFF, force reasoning off
        if (providerId === 'openai' && isCustomNonOpenAIUrl() && config.llm?.openai?.responses_api !== true) {
            masterEnabled = false;
        }
        ReasoningToggle.setMasterEnabled(masterEnabled);
    }

    // Disable long-term memory for Gemini due to embedding incompatibility
    updateMemoryAvailability(providerId);
}

function updateLLMProviderHint(providerId) {
    const hintEl = document.getElementById('llmProviderHint');
    if (hintEl) {
        hintEl.innerHTML = LLM_PROVIDER_HINTS[providerId] || '';
    }
}

function updateLLMApiKeyValidation(providerId) {
    const hintEl = document.getElementById('llmApiKeyHint');
    if (!hintEl) return;
    const keyField = document.getElementById('llmApiKey');
    const val = keyField?.value || '';
    if (!val || val === '********') {
        hintEl.textContent = '';
        hintEl.style.color = '';
        return;
    }
    const error = validateLLMApiKey(val, providerId);
    if (error) {
        hintEl.textContent = error;
        hintEl.style.color = 'var(--danger)';
    } else {
        hintEl.textContent = '';
        hintEl.style.color = '';
    }
}

function renderLLMProviderSettings(providerId) {
    const providerConfig = LLM_PROVIDERS[providerId];
    const container = document.getElementById('llmProviderSettings');

    if (!providerConfig || !container) {
        console.warn(`No config for llm/${providerId}`);
        return;
    }

    container.innerHTML = providerConfig.fields.map(f => renderField(f, 'llm', providerId)).join('');

    // OpenAI-specific: conditional visibility of responses_api toggle
    if (providerId === 'openai') {
        applyOpenAIResponsesApiVisibility();
    }
}

function switchSTTProvider(providerId) {
    updateSetting('stt.provider', providerId);
    renderSTTProviderSettings(providerId);
    updateRamMonitoring();
}

function renderSTTProviderSettings(providerId) {
    const providerConfig = STT_PROVIDERS[providerId];
    const container = document.getElementById('sttProviderSettings');

    if (!providerConfig || !container) {
        console.warn(`No config for stt/${providerId}`);
        return;
    }

    let html = '';
    if (providerConfig.description) {
        let desc = providerConfig.description;
        // Show spell casting note for local STT providers when voice_spells is enabled
        if ((providerId === 'parakeet' || providerId === 'canary' || providerId === 'moonshine') && config.stt?.voice_spells !== false) {
            const providerName = providerId === 'parakeet' ? 'Parakeet' : providerId === 'canary' ? 'Canary' : 'Moonshine';
            desc += `<br><br><strong>Note:</strong> ${providerName} cannot use keyword lists, so spell names may be mistranscribed. Common misspellings are auto-corrected, but for best spell casting accuracy consider <a href="javascript:switchSTTProvider('deepgram'); document.getElementById('sttProvider').value='deepgram';">Deepgram</a>.`;
        }
        html += `<p class="field-hint" style="margin-bottom: var(--space-md);">${desc}</p>`;
    }
    html += providerConfig.fields.map(f => renderField(f, 'stt', providerId)).join('');
    container.innerHTML = html;

    // Show/hide mic gain boost (visible for all active STT providers)
    const micGainGroup = document.getElementById('sttMicGain');
    if (micGainGroup) {
        micGainGroup.style.display = providerId === 'none' ? 'none' : 'block';
    }

    // Handle disabled state for hotkey selects
    const isDisabled = providerId === 'none';
    const sttHotkey = document.getElementById('stt_hotkey');
    const inputSttHotkey = document.getElementById('input_stt_hotkey');
    const sttHotkeyGroup = sttHotkey?.closest('.field-group');
    const inputSttHotkeyGroup = inputSttHotkey?.closest('.field-group');

    if (sttHotkey) {
        sttHotkey.disabled = isDisabled;
        sttHotkey.style.opacity = isDisabled ? '0.5' : '1';
        sttHotkey.style.cursor = isDisabled ? 'not-allowed' : 'pointer';
    }
    if (inputSttHotkey) {
        inputSttHotkey.disabled = isDisabled;
        inputSttHotkey.style.opacity = isDisabled ? '0.5' : '1';
        inputSttHotkey.style.cursor = isDisabled ? 'not-allowed' : 'pointer';
    }
    if (sttHotkeyGroup) {
        sttHotkeyGroup.style.opacity = isDisabled ? '0.5' : '1';
    }
    if (inputSttHotkeyGroup) {
        inputSttHotkeyGroup.style.opacity = isDisabled ? '0.5' : '1';
    }

    // Special handling for Deepgram: set up model -> language dependency
    if (providerId === 'deepgram') {
        const modelInput = document.getElementById('stt_deepgram_model');
        const currentModel = config.stt?.deepgram?.model || 'nova-3';
        const currentLang = config.stt?.deepgram?.language || 'en-US';

        // Initialize language dropdown with correct options
        updateDeepgramLanguages(currentModel);

        // Set the saved language value
        const langSelect = document.getElementById('stt_deepgram_language');
        if (langSelect) {
            const languages = getDeepgramLanguagesForModel(currentModel);
            const validValues = languages.map(l => l.value);
            langSelect.value = validValues.includes(currentLang) ? currentLang : languages[0].value;
        }

        // Listen for model changes to update language options
        if (modelInput) {
            modelInput.addEventListener('input', (e) => {
                updateDeepgramLanguages(e.target.value);
            });
        }
    }
}

// Language prefixes not supported by Parakeet STT (no CJK or Arabic coverage)
const PARAKEET_UNSUPPORTED_PREFIXES = ['JA', 'KO', 'ZH', 'AR'];

// Language prefixes supported by Canary STT (English, German, Spanish, French)
const CANARY_SUPPORTED_PREFIXES = ['EN', 'DE', 'ES', 'FR'];

function generateSTTProviderDropdown(currentProvider) {
    let html = '';
    const gameLanguage = config.setup?.language || 'EN_US';
    for (const [id, cfg] of Object.entries(STT_PROVIDERS)) {
        const selected = id === currentProvider ? 'selected' : '';
        let disabled = '';
        // Disable Parakeet for unsupported languages
        if (id === 'parakeet' && PARAKEET_UNSUPPORTED_PREFIXES.includes(gameLanguage.split('_')[0])) {
            disabled = 'disabled';
        }
        // Disable Canary for unsupported languages
        if (id === 'canary' && !CANARY_SUPPORTED_PREFIXES.includes(gameLanguage.split('_')[0])) {
            disabled = 'disabled';
        }
        // Disable Moonshine for non-English game languages
        if (id === 'moonshine' && gameLanguage.split('_')[0] !== 'EN') {
            disabled = 'disabled';
        }
        html += `<option value="${id}" ${selected} ${disabled}>${escapeHtml(cfg.label)}</option>`;
    }
    return html;
}

function updateParakeetSTTAvailability(language) {
    const sttDropdown = document.getElementById('sttProvider');
    if (!sttDropdown) return;

    const currentProvider = sttDropdown.value;
    const isUnsupported = PARAKEET_UNSUPPORTED_PREFIXES.includes(language.split('_')[0]);

    // If switching to unsupported language and parakeet is selected, change to "none"
    if (isUnsupported && currentProvider === 'parakeet') {
        switchSTTProvider('none');
        showToast('Parakeet does not support this language. Switched to Disabled.', 'warning');
    }

    // Regenerate dropdown to update disabled state
    sttDropdown.innerHTML = generateSTTProviderDropdown(sttDropdown.value);
}

function updateCanarySTTAvailability(language) {
    const sttDropdown = document.getElementById('sttProvider');
    if (!sttDropdown) return;

    const currentProvider = sttDropdown.value;
    const isSupported = CANARY_SUPPORTED_PREFIXES.includes(language.split('_')[0]);

    // If switching to unsupported language and canary is selected, change to "none"
    if (!isSupported && currentProvider === 'canary') {
        switchSTTProvider('none');
        showToast('Canary does not support this language. Switched to Disabled.', 'warning');
    }

    // Regenerate dropdown to update disabled state
    sttDropdown.innerHTML = generateSTTProviderDropdown(sttDropdown.value);
}

function updateMoonshineSTTAvailability(language) {
    const sttDropdown = document.getElementById('sttProvider');
    if (!sttDropdown) return;

    const currentProvider = sttDropdown.value;
    const isEnglish = language.split('_')[0] === 'EN';

    // If switching to non-English and moonshine is selected, change to "none"
    if (!isEnglish && currentProvider === 'moonshine') {
        switchSTTProvider('none');
        showToast('Moonshine only supports English. Switched to Disabled.', 'warning');
    }

    // Regenerate dropdown to update disabled state
    sttDropdown.innerHTML = generateSTTProviderDropdown(sttDropdown.value);
}

function updateSTTHotkey(value) {
    // Sync both STT hotkey dropdowns
    const sttHotkey = document.getElementById('stt_hotkey');
    const inputSttHotkey = document.getElementById('input_stt_hotkey');
    if (sttHotkey) sttHotkey.value = value;
    if (inputSttHotkey) inputSttHotkey.value = value;
    updateSetting('stt.hotkey', value);
}

function toggleOpenMicSettings(enabled) {
    // Show/hide open mic advanced settings based on enable toggle
    const openMicSettings = document.getElementById('open_mic_settings');
    const openMicEndpointingSettings = document.getElementById('open_mic_endpointing_settings');
    const openMicTimeoutSettings = document.getElementById('open_mic_timeout_settings');
    if (openMicSettings) {
        openMicSettings.style.display = enabled ? 'block' : 'none';
    }
    if (openMicEndpointingSettings) {
        openMicEndpointingSettings.style.display = enabled ? 'block' : 'none';
    }
    if (openMicTimeoutSettings) {
        openMicTimeoutSettings.style.display = enabled ? 'block' : 'none';
    }

    // Update hotkey label text based on mode
    const hotkeyLabel = document.querySelector('#stt_hotkey')?.closest('.field-group')?.querySelector('.field-label');
    if (hotkeyLabel) {
        hotkeyLabel.textContent = enabled ? 'Toggle Hotkey' : 'Hold-to-Talk Hotkey';
    }
    const hotkeyHint = document.querySelector('#stt_hotkey')?.closest('.field-group')?.querySelector('.field-hint');
    if (hotkeyHint) {
        hotkeyHint.innerHTML = enabled
            ? 'Press to toggle open mic on/off. <a href="#chapterInput" onclick="scrollToSection(\'chapterInput\')">More input settings</a>'
            : 'Hold this key while speaking to record your voice. Release to send. <a href="#chapterInput" onclick="scrollToSection(\'chapterInput\')">More input settings</a>';
    }
}

function restartSTTCapture() {
    // Tell server to restart STT capture with new settings
    fetch('/api/stt/restart', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                console.log('[Config] STT capture restarted');
            } else {
                console.error('[Config] Failed to restart STT:', data.error);
            }
        })
        .catch(err => console.error('[Config] Error restarting STT:', err));

    // Also update UI state
    const enabled = document.getElementById('open_mic_enabled')?.checked;
    toggleOpenMicSettings(enabled);
}

function getDeepgramLanguagesForModel(model) {
    // nova-3 only supports English
    if (model && model.toLowerCase().startsWith('nova-3')) {
        return DEEPGRAM_LANGUAGES_NOVA3;
    }
    // nova-2 and all other models get the full list
    return DEEPGRAM_LANGUAGES_NOVA2;
}

function updateDeepgramLanguages(model) {
    const langSelect = document.getElementById('stt_deepgram_language');
    if (!langSelect) return;

    const currentValue = langSelect.value;
    const languages = getDeepgramLanguagesForModel(model);

    // Rebuild options
    langSelect.innerHTML = '';
    for (const opt of languages) {
        const option = document.createElement('option');
        option.value = opt.value;
        option.textContent = opt.label;
        langSelect.appendChild(option);
    }

    // Try to preserve current selection, otherwise use first valid option
    const validValues = languages.map(l => l.value);
    if (validValues.includes(currentValue)) {
        langSelect.value = currentValue;
    } else {
        // For nova-3, default to en; for others, keep first option
        langSelect.value = model && model.toLowerCase().startsWith('nova-3') ? 'en' : languages[0].value;
        // Update config with new value
        updateProviderSetting('stt', 'deepgram', 'language', langSelect.value);
    }
}

// ============================================
// Provider Field Validation
// ============================================
const PROVIDER_REGISTRIES = { tts: () => TTS_PROVIDERS, stt: () => STT_PROVIDERS };

function getProviderField(category, providerId, fieldId) {
    const providers = PROVIDER_REGISTRIES[category]?.() || {};
    return providers[providerId]?.fields?.find(f => f.id === fieldId) || null;
}

function showFieldValidation(category, providerId, fieldId, error) {
    const el = document.getElementById(`${category}_${providerId}_${fieldId}_validation`);
    if (!el) return;
    el.textContent = error || '';
    el.style.display = error ? 'block' : 'none';
}

function validateProviderField(category, providerId, fieldId, value) {
    const field = getProviderField(category, providerId, fieldId);
    if (!field?.validate) return null;
    const error = field.validate(value);
    showFieldValidation(category, providerId, fieldId, error);
    return error;
}

function validateActiveProviderFields() {
    const errors = [];

    // Validate LLM API key
    const llmProvider = config.llm?.provider || 'gemini';
    const llmKey = config.llm?.[llmProvider]?.api_key || config.llm?.api_key;
    const llmError = validateLLMApiKey(llmKey, llmProvider);
    updateLLMApiKeyValidation(llmProvider);
    if (llmError) errors.push(llmError);

    // Validate TTS/STT provider fields
    for (const [category, getProviders] of Object.entries(PROVIDER_REGISTRIES)) {
        const activeProvider = config[category]?.provider;
        if (!activeProvider) continue;
        const providerDef = getProviders()[activeProvider];
        if (!providerDef?.fields) continue;
        for (const field of providerDef.fields) {
            if (!field.validate) continue;
            const value = config[category]?.[activeProvider]?.[field.id];
            const error = field.validate(value);
            showFieldValidation(category, activeProvider, field.id, error);
            if (error) errors.push(error);
        }
    }
    return errors;
}

function updateProviderSetting(category, providerId, fieldId, value) {
    // Initialize nested structure if needed
    if (!config[category]) config[category] = {};
    if (!config[category][providerId]) config[category][providerId] = {};

    // Don't save masked password values
    if (value === '********') return;

    validateProviderField(category, providerId, fieldId, value);

    config[category][providerId][fieldId] = value;
    markDirty();
}

// ElevenLabs plan -> sample rate mapping
const ELEVENLABS_PLAN_RATES = {
    'free': 16000,
    'starter': 22050,
    'creator': 24000,
    'pro': 44100,
    'scale': 44100,
    'business': 44100
};

function updateElevenLabsQuality(plan) {
    const sampleRate = ELEVENLABS_PLAN_RATES[plan] || 24000;
    const sampleRateSelect = document.getElementById('tts_elevenlabs_sample_rate');
    if (sampleRateSelect) {
        sampleRateSelect.value = sampleRate;
        updateProviderSetting('tts', 'elevenlabs', 'sample_rate', sampleRate);
    }
}

function onInworldModelChange(modelValue) {
    const toggle = document.getElementById('tts_inworld_emotion_delivery');
    const group = toggle?.closest('.field-group');
    if (!toggle || !group) return;

    const is1_5Plus = modelValue && modelValue.includes('1.5');
    toggle.disabled = !is1_5Plus;
    group.style.opacity = is1_5Plus ? '1' : '0.5';

    // Auto-disable if model doesn't support it
    if (!is1_5Plus && toggle.checked) {
        toggle.checked = false;
        updateProviderSetting('tts', 'inworld', 'emotion_delivery', false);
    }
}

function generateProviderDropdown(category, currentProvider) {
    const providers = category === 'tts' ? TTS_PROVIDERS : {};
    let html = '';
    for (const [id, cfg] of Object.entries(providers)) {
        const selected = id === currentProvider ? 'selected' : '';
        // Disable pocket/neutts TTS for non-English game languages
        let disabled = '';
        if (category === 'tts' && (id === 'pocket' || id === 'neutts')) {
            const gameLanguage = config.setup?.language || 'EN_US';
            if (gameLanguage !== 'EN_US') {
                disabled = 'disabled';
            }
        }
        html += `<option value="${id}" ${selected} ${disabled}>${escapeHtml(cfg.label)}</option>`;
    }
    return html;
}

// ============================================
// Agent Field Rendering
// ============================================
function renderAgentField(field, agentId, prefix = '') {
    const settingPath = prefix ? `agents.${agentId}.${prefix}.${field.id}` : `agents.${agentId}.${field.id}`;
    const fieldId = `agent_${agentId}_${prefix ? prefix + '_' : ''}${field.id}`;
    const configPath = prefix ? config.agents?.[agentId]?.[prefix] : config.agents?.[agentId];
    const currentValue = configPath?.[field.id] ?? field.default ?? '';

    let html = `<div class="field-group">`;
    html += `<label class="field-label">${escapeHtml(field.label)}</label>`;

    if (field.hint) {
        html += `<p class="field-hint">${field.hint}</p>`;
    }

    switch (field.type) {
        case 'text':
            html += `<input type="text" id="${fieldId}"
                             placeholder="${escapeHtml(field.placeholder || '')}"
                             value="${escapeHtml(currentValue)}"
                             onchange="updateAgentSetting('${agentId}', '${prefix}', '${field.id}', this.value)">`;
            break;

        case 'password':
            const displayValue = currentValue ? '********' : '';
            html += `<input type="password" id="${fieldId}"
                             placeholder="${escapeHtml(field.placeholder || '')}"
                             value="${escapeHtml(displayValue)}"
                             autocomplete="off"
                             onchange="updateAgentSetting('${agentId}', '${prefix}', '${field.id}', this.value)">`;
            break;

        case 'select':
            html += `<select id="${fieldId}" onchange="updateAgentSetting('${agentId}', '${prefix}', '${field.id}', this.value)">`;
            for (const opt of field.options) {
                const selected = opt.value === currentValue ? 'selected' : '';
                html += `<option value="${opt.value}" ${selected}>${escapeHtml(opt.label)}</option>`;
            }
            html += `</select>`;
            break;

        case 'range':
            const rangeValue = currentValue !== '' ? currentValue : field.default;
            html += `<div class="range-wrapper">
                        <input type="range" id="${fieldId}"
                               min="${field.min}" max="${field.max}" step="${field.step}" value="${rangeValue}"
                               oninput="updateRangeValue('${fieldId}', this.value); updateAgentSetting('${agentId}', '${prefix}', '${field.id}', parseFloat(this.value))">
                        <span class="range-value" id="${fieldId}Value">${rangeValue}</span>
                    </div>`;
            break;

        case 'toggle':
            const checked = currentValue !== '' ? currentValue : field.default;
            html += `<div class="toggle-wrapper" style="padding: 0;">
                        <label class="toggle">
                            <input type="checkbox" id="${fieldId}" ${checked ? 'checked' : ''}
                                   onchange="updateAgentSetting('${agentId}', '${prefix}', '${field.id}', this.checked)">
                            <span class="toggle-track">
                                <span class="toggle-thumb"></span>
                            </span>
                        </label>
                    </div>`;
            break;
    }

    html += `</div>`;
    return html;
}

function renderAgentSettings(agentId) {
    const agentConfig = AGENT_CONFIGS[agentId];
    const container = document.getElementById(`agent_${agentId}_settings`);
    if (!agentConfig || !container) return;

    let html = '';

    // Main fields
    html += agentConfig.fields.map(f => renderAgentField(f, agentId)).join('');

    // LLM settings if present
    if (agentConfig.llm) {
        html += `<div class="divider">&#9672; Vision LLM Settings &#9672;</div>`;
        html += agentConfig.llm.fields.map(f => renderAgentField(f, agentId, 'llm')).join('');
    }

    container.innerHTML = html;
}

function updateAgentSetting(agentId, prefix, fieldId, value) {
    // Don't save masked password values
    if (value === '********') return;

    // Initialize nested structure
    if (!config.agents) config.agents = {};
    if (!config.agents[agentId]) config.agents[agentId] = {};

    if (prefix) {
        if (!config.agents[agentId][prefix]) config.agents[agentId][prefix] = {};
        config.agents[agentId][prefix][fieldId] = value;
    } else {
        config.agents[agentId][fieldId] = value;
    }

    markDirty();
}

// ============================================
// Configuration state
// ============================================
let config = {};
let dirty = false;
let isInitializing = true;

// In-flight request guards (prevent stacking when server is offline)
let statusCheckInFlight = false;
let historyLoadInFlight = false;
let eventsLoadInFlight = false;
let restartInProgress = false;  // Prevents status polling from resetting restart button
let restartWentOffline = false; // Tracks if server went offline during restart

// Setup wizard state
let setupStatus = null;
let setupPollingInterval = null;
let setupConfettiShown = false;  // Only show confetti once per session when setup completes
let setupBannerDismissed = false;  // User manually dismissed the banner this session
let lastLocalizationStatus = null;  // Track for language sync on completion

// Language-specific TTS test texts
const TTS_TEST_TEXTS = {
    'EN_US': 'Hello, this is a test of the voice synthesis system.',
    'DE_DE': 'Hallo, dies ist ein Test des Sprachsynthesesystems.',
    'ES_ES': 'Hola, esta es una prueba del sistema de síntesis de voz.',
    'ES_MX': 'Hola, esta es una prueba del sistema de síntesis de voz.',
    'FR_FR': 'Bonjour, ceci est un test du système de synthèse vocale.',
    'IT_IT': 'Ciao, questo è un test del sistema di sintesi vocale.',
    'PT_BR': 'Olá, este é um teste do sistema de síntese de voz.',
    'JA_JP': 'こんにちは、これは音声合成システムのテストです。',
    'KO_KR': '안녕하세요, 이것은 음성 합성 시스템의 테스트입니다.',
    'PL_PL': 'Witam, to jest test systemu syntezy mowy.',
    'RU_RU': 'Здравствуйте, это тест системы синтеза речи.',
    'ZH_CN': '你好，这是语音合成系统的测试。',
    'ZH_TW': '你好，這是語音合成系統的測試。',
    'AR_AE': 'مرحبا، هذا اختبار لنظام تركيب الكلام.'
};

// Fetch with timeout helper (default 2s)
function fetchWithTimeout(url, options = {}, timeoutMs = 2000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { ...options, signal: controller.signal })
        .finally(() => clearTimeout(timeoutId));
}

// ============================================
// Setup Wizard Functions
// ============================================
async function checkSetupStatus(language = null) {
    try {
        // Optionally query status for a specific language
        const url = language ? `/api/setup/status?language=${language}` : '/api/setup/status';
        const response = await fetchWithTimeout(url, {}, 5000);
        if (response.ok) {
            const previousStatus = setupStatus;
            setupStatus = await response.json();
            updateSetupUI(setupStatus, previousStatus);
            return setupStatus;
        }
    } catch (e) {
        console.error('Failed to check setup status:', e);
    }
    return null;
}

function updateSetupConfigLinks() {
    // Hide "Configure TTS settings first" if TTS is configured
    const ttsLink = document.getElementById('setupTtsConfigLink');
    if (ttsLink) {
        const ttsProvider = config.tts?.provider || 'inworld';
        // Hide if: disabled, pocket/neutts (no API key needed), or has API key set
        const ttsConfigured = (
            ttsProvider === 'none' ||
            ttsProvider === 'pocket' ||
            ttsProvider === 'neutts' ||
            config.tts?.[ttsProvider]?.api_key
        );
        ttsLink.style.display = ttsConfigured ? 'none' : 'flex';
    }

    // Hide "Configure LLM settings first" if LLM is configured
    const llmLink = document.getElementById('setupLlmConfigLink');
    if (llmLink) {
        const llmProvider = config.llm?.provider || 'gemini';
        // Hide if: provider key is set, OR legacy-only key exists, OR (openai provider AND api_url is set)
        const legacyKey = config.llm?.api_key;
        const hasProviderSpecificKeys = Boolean(
            config.llm?.gemini?.api_key || config.llm?.openrouter?.api_key || config.llm?.openai?.api_key
        );
        const hasApiKey = config.llm?.[llmProvider]?.api_key || (legacyKey && !hasProviderSpecificKeys);
        const isLocalOpenAI = llmProvider === 'openai' && config.llm?.openai?.api_url;
        const llmConfigured = hasApiKey || isLocalOpenAI;
        llmLink.style.display = llmConfigured ? 'none' : 'flex';
    }
}

function showSetupCompleteModal() {
    if (document.getElementById('setupReadyOverlay')) return;

    const overlay = document.createElement('div');
    overlay.id = 'setupReadyOverlay';
    overlay.style.cssText = `
        position: fixed; inset: 0; z-index: 99999;
        background: rgba(10, 8, 6, 0.92);
        display: flex; align-items: center; justify-content: center;
        font-family: var(--font-body, 'Crimson Text', serif);
        padding: 1.5rem;
    `;

    const modal = document.createElement('div');
    modal.style.cssText = `
        background: var(--leather-mid, #2a1f1a);
        border: 2px solid var(--gold-dark, #8b6914);
        border-radius: 12px;
        max-width: 560px; width: 100%;
        padding: 2rem;
        color: var(--parchment-light, #f4e4c1);
        box-shadow: 0 0 40px rgba(0,0,0,0.6), 0 0 8px rgba(212,168,75,0.2);
        text-align: center;
    `;

    modal.innerHTML = `
        <div style="display:flex;justify-content:center;margin-bottom:0.8rem;color:var(--gold-bright, #d4a84b);">
            <i data-lucide="sparkles"></i>
        </div>
        <h2 style="font-family: var(--font-display, 'Cinzel', serif); color: var(--gold-bright, #d4a84b); margin: 0 0 1rem;">
            Setup Complete
        </h2>
        <p style="line-height: 1.6; margin: 0 0 0.8rem;">
            Sonorus is ready to play.
        </p>
        <p style="line-height: 1.6; margin: 0 0 1.5rem; opacity: 0.9;">
            The default settings are already tuned for most players, so you can jump in now and tweak things later only if you want to.
        </p>
        <button id="setupReadyClose" style="
            font-family: var(--font-display, 'Cinzel', serif);
            background: linear-gradient(135deg, var(--gold-dark, #8b6914), var(--gold-bright, #d4a84b));
            color: var(--ink-black, #1a1410);
            border: none; border-radius: 6px;
            padding: 0.7rem 1.6rem; font-size: 1rem;
            cursor: pointer; font-weight: 600;
        ">Ready to Play</button>
    `;

    const closeModal = () => {
        overlay.remove();
    };

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    if (window.lucide) {
        lucide.createIcons({ nodes: [overlay] });
    }

    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeModal();
    });

    const closeBtn = document.getElementById('setupReadyClose');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeModal);
    }
}

function updateSetupUI(status, previousStatus = null) {
    const setupSection = document.getElementById('chapterSetup');
    const navSetup = document.getElementById('navSetup');
    const setupTitle = document.getElementById('setupTitle');
    const setupIntro = document.getElementById('setupIntro');

    // Always show setup section and nav
    setupSection.style.display = 'block';
    navSetup.style.display = 'list-item';

    // Update language dropdown - use local config value if available (user may have just changed it)
    // Otherwise fall back to server status or default
    const currentLanguage = config.setup?.language || status.language || 'EN_US';
    document.getElementById('setupLanguage').value = currentLanguage;

    // Update TTS test text to match current language (only if not already set)
    const ttsTextInput = document.getElementById('setupTtsText');
    if (ttsTextInput && !ttsTextInput.value) {
        ttsTextInput.value = TTS_TEST_TEXTS[currentLanguage] || TTS_TEST_TEXTS['EN_US'];
    }

    // Show/hide warnings banner
    const warningsDiv = document.getElementById('setupWarnings');
    if (warningsDiv) {
        if (status.warnings && status.warnings.length > 0) {
            warningsDiv.textContent = status.warnings.join('\n');
            warningsDiv.style.display = 'block';
        } else {
            warningsDiv.style.display = 'none';
        }
    }

    if (status.complete) {
        // Setup complete: rename, collapse, hide intro
        setupTitle.textContent = 'Setup';
        setupIntro.style.display = 'none';

        // Collapse the section (use class only, not inline style)
        setupSection.classList.add('collapsed');

        stopSetupPolling();
        hideSetupError();

        // Detect real transition from incomplete -> complete
        const justCompleted = Boolean(previousStatus && !previousStatus.complete && status.complete);

        // Celebrate with confetti when setup completes (only once per session)
        if (justCompleted && !setupConfettiShown && typeof confetti === 'function') {
            confetti();
            setupConfettiShown = true;
        }

        // Show "ready to play" modal on completion transition
        if (justCompleted) {
            showSetupCompleteModal();
        }

        // Still update test steps so they show "Retest"
        updateSetupStep(3, status.steps.tts);
        updateSetupStep(4, status.steps.llm);

        // Populate LLM models list for retesting
        if (status.steps.llm && status.steps.llm.models) {
            populateLlmModels(status.steps.llm.models);
        }

        // Update "configure settings first" link visibility
        updateSetupConfigLinks();

    } else {
        // Setup incomplete: show intro, expand
        setupTitle.textContent = 'Initial Setup';
        setupIntro.style.display = 'block';

        // Expand the section
        setupSection.classList.remove('collapsed');

        // Update all steps
        updateSetupStep(1, status.steps.localization);
        updateSetupStep(2, status.steps.voices);
        updateSetupStep(3, status.steps.tts);
        updateSetupStep(4, status.steps.llm);

        // Step 2 requires step 1 to be complete
        if (status.steps.localization?.status !== 'complete') {
            const step2Btn = document.getElementById('setupStep2Btn');
            const step2SkipBtn = document.getElementById('setupStep2SkipBtn');
            if (step2Btn) step2Btn.disabled = true;
            if (step2SkipBtn) step2SkipBtn.disabled = true;
        }

        // Populate LLM models list if not tested yet
        if (status.steps.llm && status.steps.llm.models && status.steps.llm.status !== 'complete') {
            populateLlmModels(status.steps.llm.models);
        }

        // Update "configure settings first" link visibility
        updateSetupConfigLinks();

        // Show/hide error
        if (status.last_error) {
            showSetupError(status.last_error);
        } else {
            hideSetupError();
        }

        // Start polling if any step is running
        if (status.running_command) {
            startSetupPolling();
        }
    }

    // Update sticky setup banner
    updateSetupBanner(status.complete);
}

function syncSetupBannerOffset() {
    const banner = document.getElementById('setupBanner');
    const show = document.body.classList.contains('setup-incomplete');
    const height = (banner && show) ? Math.ceil(banner.getBoundingClientRect().height) : 0;
    document.documentElement.style.setProperty('--setup-banner-height', `${height}px`);
}

function updateSetupBanner(isComplete) {
    const show = !isComplete && !setupBannerDismissed;
    document.body.classList.toggle('setup-incomplete', show);

    const banner = document.getElementById('setupBanner');
    if (banner && show && window.lucide) {
        lucide.createIcons({ nodes: [banner] });
    }

    syncSetupBannerOffset();
    if (show) {
        requestAnimationFrame(syncSetupBannerOffset);
    }
}

function dismissSetupBanner() {
    setupBannerDismissed = true;
    document.body.classList.remove('setup-incomplete');
    syncSetupBannerOffset();
}

window.addEventListener('resize', () => {
    if (document.body.classList.contains('setup-incomplete')) {
        syncSetupBannerOffset();
    }
});

function updateSetupStep(stepNum, stepData) {
    const stepEl = document.getElementById(`setupStep${stepNum}`);
    const statusEl = document.getElementById(`setupStep${stepNum}Status`);
    const btn = document.getElementById(`setupStep${stepNum}Btn`);
    const btnText = document.getElementById(`setupStep${stepNum}BtnText`);
    const skipBtn = document.getElementById(`setupStep${stepNum}SkipBtn`);

    if (!stepEl || !stepData) return;

    // Get button text based on step number
    const buttonLabels = {
        1: { default: 'Extract Localization', running: 'Extracting...', mismatch: 'Re-extract' },
        2: { default: 'Extract Voices', running: 'Extracting...', mismatch: 'Re-extract' },
        3: { default: 'Test Voice', running: 'Testing...', mismatch: 'Retest' },
        4: { default: 'Test Models', running: 'Testing...', mismatch: 'Retest' }
    };
    const labels = buttonLabels[stepNum] || { default: 'Run', running: 'Running...', mismatch: 'Retry' };

    // Clear previous classes
    stepEl.classList.remove('complete', 'running', 'error', 'partial');
    statusEl.classList.remove('not-started', 'running', 'complete', 'error', 'skipped', 'partial');

    // For step 2 (voices), show dual progress: extracted and referenced
    const isVoiceStep = (stepNum === 2 && stepData.total > 0);

    // Update voice progress display element if it exists
    if (isVoiceStep) {
        updateVoiceProgress(stepData);
    }

    switch (stepData.status) {
        case 'not_started':
            statusEl.textContent = isVoiceStep ? `0/${stepData.total}` : 'Not Started';
            statusEl.classList.add('not-started');
            btn.disabled = false;
            btnText.textContent = labels.default;
            if (skipBtn) skipBtn.disabled = false;
            break;

        case 'running':
            stepEl.classList.add('running');
            statusEl.textContent = isVoiceStep ? `${stepData.referenced}/${stepData.total}` : 'Running...';
            statusEl.classList.add('running');
            btn.disabled = true;
            btnText.innerHTML = `<span class="spinner"></span> ${labels.running}`;
            if (skipBtn) skipBtn.disabled = true;
            break;

        case 'partial':
            stepEl.classList.add('partial');
            statusEl.textContent = `${stepData.referenced}/${stepData.total}`;
            statusEl.classList.add('partial');
            btn.disabled = false;
            btnText.textContent = 'Resume';
            if (skipBtn) skipBtn.disabled = false;
            break;

        case 'complete':
            stepEl.classList.add('complete');
            statusEl.textContent = isVoiceStep ? `${stepData.referenced}/${stepData.total}` : 'Complete';
            statusEl.classList.add('complete');
            // Steps 3 & 4 (TTS/LLM tests) can be retested
            if (stepNum === 3 || stepNum === 4) {
                btn.disabled = false;
                btnText.textContent = 'Retest';
            } else {
                btn.disabled = true;
                btnText.textContent = 'Complete';
            }
            if (skipBtn) skipBtn.style.display = 'none';
            break;

        case 'error':
            stepEl.classList.add('error');
            statusEl.textContent = isVoiceStep ? `Error (${stepData.referenced}/${stepData.total})` : 'Error';
            statusEl.classList.add('error');
            btn.disabled = false;
            btnText.textContent = 'Retry';
            if (skipBtn) skipBtn.disabled = false;
            break;

        case 'skipped':
            statusEl.textContent = 'Skipped';
            statusEl.classList.add('skipped');
            btn.disabled = true;
            btnText.textContent = 'Skipped';
            if (skipBtn) skipBtn.style.display = 'none';
            break;

        case 'language_mismatch':
            // Completed but for wrong language - need to redo for new language
            stepEl.classList.add('partial');
            statusEl.textContent = 'Language Changed';
            statusEl.classList.add('partial');
            btn.disabled = false;
            btnText.textContent = labels.mismatch;
            if (skipBtn) skipBtn.disabled = false;
            break;
    }
}

function updateVoiceProgress(stepData) {
    let progressEl = document.getElementById('voiceProgressBars');
    if (!progressEl) return;

    const { total, referenced } = stepData;
    const pct = total > 0 ? (referenced / total * 100).toFixed(0) : 0;

    progressEl.innerHTML = `
                <div class="voice-progress-row">
                    <div class="voice-progress-bar">
                        <div class="voice-progress-fill reference" style="width: ${pct}%"></div>
                    </div>
                    <span class="voice-progress-count">${referenced}/${total}</span>
                </div>
            `;
}

function showSetupError(message) {
    const errorDiv = document.getElementById('setupError');
    const errorMsg = document.getElementById('setupErrorMessage');

    // Convert URLs to clickable links while escaping other HTML
    const urlRegex = /(https?:\/\/[^\s]+)/g;
    const escapedMessage = message.replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const messageWithLinks = escapedMessage.replace(urlRegex, '<a href="$1" target="_blank" style="color: var(--gold); text-decoration: underline;">$1</a>');

    errorMsg.innerHTML = messageWithLinks;
    errorDiv.style.display = 'block';

    // Scroll error into view with padding so user can see it
    setTimeout(() => {
        const rect = errorDiv.getBoundingClientRect();
        const bottomPadding = 100;
        const targetY = window.scrollY + rect.bottom + bottomPadding - window.innerHeight;
        if (targetY > window.scrollY) {
            window.scrollTo({ top: targetY, behavior: 'smooth' });
        }
    }, 50);  // Small delay to ensure element is rendered
}

function hideSetupError() {
    document.getElementById('setupError').style.display = 'none';
}

function startSetupPolling() {
    if (!setupPollingInterval) {
        setupPollingInterval = setInterval(checkSetupStatus, 2000);
    }
}

function stopSetupPolling() {
    if (setupPollingInterval) {
        clearInterval(setupPollingInterval);
        setupPollingInterval = null;
    }
}

async function updateSetupLanguage(language) {
    console.log('Setup language changed to:', language);

    // Save language setting immediately to server
    updateSetting('setup.language', language);
    await saveSettings();

    // Update TTS test text to match selected language
    const ttsTextInput = document.getElementById('setupTtsText');
    if (ttsTextInput) {
        ttsTextInput.value = TTS_TEST_TEXTS[language] || TTS_TEST_TEXTS['EN_US'];
    }

    // Update local TTS/STT availability based on new language
    updatePocketTTSAvailability(language);
    updateParakeetSTTAvailability(language);
    updateCanarySTTAvailability(language);
    updateMoonshineSTTAvailability(language);

    // Check status for selected language (server now has updated value)
    await checkSetupStatus(language);
}

async function startLocalizationExtraction() {
    const language = document.getElementById('setupLanguage').value;
    const btn = document.getElementById('setupStep1Btn');
    const btnText = document.getElementById('setupStep1BtnText');

    btn.disabled = true;
    btnText.innerHTML = '<span class="spinner"></span> Starting...';
    hideSetupError();

    try {
        const response = await fetch('/api/setup/extract-localization', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language })
        });

        if (response.ok) {
            showToast('Localization extraction started...', 'success');
            startSetupPolling();
            checkSetupStatus();
        } else {
            const data = await response.json();
            showSetupError(data.error || 'Failed to start extraction');
            btn.disabled = false;
            btnText.textContent = 'Retry';
        }
    } catch (e) {
        showSetupError('Network error: Could not connect to server');
        btn.disabled = false;
        btnText.textContent = 'Retry';
    }
}

async function startVoiceExtraction() {
    const language = document.getElementById('setupLanguage').value;
    const btn = document.getElementById('setupStep2Btn');
    const btnText = document.getElementById('setupStep2BtnText');

    btn.disabled = true;
    btnText.innerHTML = '<span class="spinner"></span> Starting...';
    hideSetupError();

    try {
        const response = await fetch('/api/setup/extract-voices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language })
        });

        if (response.ok) {
            showToast('Voice extraction started (this may take several minutes)...', 'success');
            startSetupPolling();
            checkSetupStatus();
        } else {
            const data = await response.json();
            showSetupError(data.error || 'Failed to start voice extraction');
            btn.disabled = false;
            btnText.textContent = 'Retry';
        }
    } catch (e) {
        showSetupError('Network error: Could not connect to server');
        btn.disabled = false;
        btnText.textContent = 'Retry';
    }
}

async function startTtsTest() {
    // Require saved configuration before testing
    if (dirty) {
        showToast('Please save your configuration before testing', 'error');
        return;
    }

    const btn = document.getElementById('setupStep3Btn');
    const btnText = document.getElementById('setupStep3BtnText');
    const text = document.getElementById('setupTtsText').value;

    btn.disabled = true;
    btnText.innerHTML = '<span class="spinner"></span> Testing...';
    hideSetupError();

    try {
        const response = await fetch('/api/setup/test-tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text })
        });
        const data = await response.json();

        if (data.success) {
            showToast(`TTS working! Voice: ${data.voice_used}`, 'success');
            checkSetupStatus();
        } else {
            showSetupError(`TTS Error: ${data.error}`);
            btn.disabled = false;
            btnText.textContent = 'Retry';
        }
    } catch (e) {
        const isOffline = e instanceof TypeError || e.message === 'Failed to fetch';
        const msg = isOffline
            ? 'Server is not running. Make sure the game is open before testing.'
            : `Network error: ${e.message}`;
        showSetupError(msg);
        btn.disabled = false;
        btnText.textContent = 'Retry';
    }
}

async function startLlmTest() {
    // Require saved configuration before testing
    if (dirty) {
        showToast('Please save your configuration before testing', 'error');
        return;
    }

    const btn = document.getElementById('setupStep4Btn');
    const btnText = document.getElementById('setupStep4BtnText');

    btn.disabled = true;
    btnText.innerHTML = '<span class="spinner"></span> Testing...';
    hideSetupError();

    try {
        const response = await fetch('/api/setup/test-llm', { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            // Update model list with results only on success
            if (data.results) {
                updateLlmModelResults(data.results);
            }
            showToast('All models working!', 'success');
            checkSetupStatus();
        } else {
            // Update model list if we have partial results
            if (data.results) {
                updateLlmModelResults(data.results);
            }
            showSetupError(`LLM Error: ${data.error}`);
            btn.disabled = false;
            btnText.textContent = 'Retry';
        }
    } catch (e) {
        const isOffline = e instanceof TypeError || e.message === 'Failed to fetch';
        const msg = isOffline
            ? 'Server is not running. Make sure the game is open before testing.'
            : `Network error: ${e.message}`;
        showSetupError(msg);
        btn.disabled = false;
        btnText.textContent = 'Retry';
    }
}

function updateLlmModelResults(results) {
    const container = document.getElementById('setupLlmModels');
    container.innerHTML = '';

    for (const [modelId, info] of Object.entries(results)) {
        const div = document.createElement('div');
        div.className = 'setup-model-item ' + (info.success ? 'success' : 'error');
        div.innerHTML = `
                    <span class="setup-model-status">${info.success ? '✓' : '✗'}</span>
                    <span class="setup-model-name">${modelId}</span>
                    <span class="setup-model-uses">(${info.used_for.join(', ')})</span>
                    ${info.error ? `<div class="setup-model-error">${info.error}</div>` : ''}
                `;
        container.appendChild(div);
    }
}

function populateLlmModels(models) {
    const container = document.getElementById('setupLlmModels');
    if (!container) return;

    // Deduplicate - merge uses for same model
    const unique = {};
    for (const [use, model] of Object.entries(models)) {
        if (!unique[model]) unique[model] = [];
        unique[model].push(use);
    }

    container.innerHTML = '';
    for (const [model, uses] of Object.entries(unique)) {
        const div = document.createElement('div');
        div.className = 'setup-model-item';
        div.innerHTML = `
                    <span class="setup-model-status">○</span>
                    <span class="setup-model-name">${model}</span>
                    <span class="setup-model-uses">(${uses.join(', ')})</span>
                `;
        container.appendChild(div);
    }
}

// ============================================
//  Character Search/Filter System
// ============================================

const CharacterSearch = (() => {
    let searchInput;
    let clearButton;
    let resultsDisplay;
    let countDisplay;

    /**
     * Perform the search and filter character cards
     */
    function filterCharacters(searchTerm) {
        const cards = document.querySelectorAll('#bioList .character-card');
        const normalizedSearch = searchTerm.toLowerCase().trim();

        let visibleCount = 0;
        let totalCount = cards.length;

        cards.forEach(card => {
            // Skip player card from filtering
            if (card.classList.contains('player-card')) {
                visibleCount++;
                return;
            }

            // Get searchable content
            const nameInput = card.querySelector('.character-name-input');
            const guidanceInput = card.querySelector('.character-guidance-input');
            const titleText = card.querySelector('.character-title-text');

            const name = (nameInput?.value || titleText?.textContent || '').toLowerCase();
            const guidance = (guidanceInput?.value || '').toLowerCase();

            // Check if search term matches name or guidance
            const matchesName = name.includes(normalizedSearch);
            const matchesGuidance = guidance.includes(normalizedSearch);
            const isVisible = normalizedSearch === '' || matchesName || matchesGuidance;

            // Apply filter with animation
            if (isVisible) {
                card.classList.remove('filtered-hidden');
                visibleCount++;
            } else {
                card.classList.add('filtered-hidden');
            }
        });

        // Update results display
        updateResultsDisplay(visibleCount, totalCount, normalizedSearch);
    }

    /**
     * Update the results counter display
     */
    function updateResultsDisplay(visible, total, searchTerm) {
        if (!resultsDisplay || !countDisplay) return;

        if (searchTerm === '') {
            // No search active
            resultsDisplay.classList.remove('visible');
            countDisplay.textContent = '';
        } else {
            // Show filtered results count
            resultsDisplay.classList.add('visible');

            if (visible === 0) {
                countDisplay.textContent = 'No characters found';
                countDisplay.style.color = 'var(--ember-red)';
            } else if (visible === total) {
                countDisplay.textContent = `All ${total} character${total !== 1 ? 's' : ''} shown`;
                countDisplay.style.color = 'var(--success)';
            } else {
                countDisplay.textContent = `${visible} of ${total} character${total !== 1 ? 's' : ''}`;
                countDisplay.style.color = 'var(--gold-dark)';
            }
        }
    }

    /**
     * Clear the search and show all characters
     */
    function clearSearch() {
        if (searchInput) {
            searchInput.value = '';
            filterCharacters('');
            searchInput.focus();
        }
    }

    /**
     * Initialize the search system
     */
    function init() {
        searchInput = document.getElementById('characterSearch');
        clearButton = document.getElementById('characterSearchClear');
        resultsDisplay = document.getElementById('characterSearchResults');
        countDisplay = document.getElementById('characterSearchCount');

        if (!searchInput) return;

        // Real-time filtering on input
        searchInput.addEventListener('input', (e) => {
            filterCharacters(e.target.value);
        });

        // Clear button handler
        if (clearButton) {
            clearButton.addEventListener('click', clearSearch);
        }

        // Allow Escape key to clear search
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                clearSearch();
                e.preventDefault();
            }
        });

        // Initial update (in case there are pre-existing characters)
        setTimeout(() => {
            if (searchInput.value) {
                filterCharacters(searchInput.value);
            }
        }, 100);
    }

    /**
     * Refresh the search (useful when characters are added/removed)
     */
    function refresh() {
        if (searchInput) {
            filterCharacters(searchInput.value);
        }
    }

    // Public API
    return {
        init,
        refresh,
        clearSearch
    };
})();

// ============================================
//  Auto-Expanding Textarea System
// ============================================

/**
 * Robust auto-expanding textarea implementation
 * Handles all edge cases: typing, pasting, programmatic updates, dynamic additions
 */
const AutoExpandTextarea = (() => {
    // Track which textareas we've initialized to prevent double-binding
    const initializedTextareas = new WeakSet();

    /**
     * Calculate and set the height for a textarea based on its content
     */
    function resizeTextarea(textarea) {
        if (!textarea) return;

        // Store current scroll position to prevent jumping
        const scrollTop = window.pageYOffset || document.documentElement.scrollTop;

        // Reset height to recalculate (allows shrinking)
        textarea.style.height = 'auto';

        // Calculate required height
        // scrollHeight includes padding, so we need the actual content height
        const computedStyle = window.getComputedStyle(textarea);
        const borderHeight = parseInt(computedStyle.borderTopWidth) + parseInt(computedStyle.borderBottomWidth);
        const paddingHeight = parseInt(computedStyle.paddingTop) + parseInt(computedStyle.paddingBottom);

        // Get the minimum height from CSS (if set)
        const minHeight = parseInt(computedStyle.minHeight) || 60;

        // Calculate ideal height
        let newHeight = textarea.scrollHeight;

        // Ensure we don't go below min-height
        newHeight = Math.max(newHeight, minHeight);

        // Set the height
        textarea.style.height = newHeight + 'px';

        // Restore scroll position to prevent page jumping
        window.scrollTo(0, scrollTop);
    }

    /**
     * Initialize auto-expand for a single textarea
     */
    function initTextarea(textarea) {
        // Skip if already initialized
        if (initializedTextareas.has(textarea)) return;
        initializedTextareas.add(textarea);

        // Set initial size
        resizeTextarea(textarea);

        // Handle input events (typing, delete, cut, paste via keyboard)
        textarea.addEventListener('input', () => resizeTextarea(textarea));

        // Handle paste event explicitly for better immediate feedback
        textarea.addEventListener('paste', () => {
            // Small delay to let paste complete
            setTimeout(() => resizeTextarea(textarea), 0);
        });

        // Handle when content is set programmatically
        // Use MutationObserver to detect value changes
        const observer = new MutationObserver(() => resizeTextarea(textarea));
        observer.observe(textarea, {
            attributes: true,
            attributeFilter: ['value']
        });

        // Store observer for potential cleanup
        textarea._autoExpandObserver = observer;
    }

    /**
     * Initialize all textareas matching our selectors
     */
    function initAll() {
        // Default character prompt (static)
        const defaultPrompt = document.getElementById('defaultPrompt');
        if (defaultPrompt) {
            initTextarea(defaultPrompt);
        }

        const worldLore = document.getElementById('worldLore');
        if (worldLore) {
            initTextarea(worldLore);
        }

        // Agent prompt textareas (static)
        const sceneContinuationPrompt = document.getElementById('sceneContinuationPrompt');
        if (sceneContinuationPrompt) {
            initTextarea(sceneContinuationPrompt);
        }
        const interjectionPromptMode = document.getElementById('interjectionPromptMode');
        if (interjectionPromptMode) {
            initTextarea(interjectionPromptMode);
        }

        // Pronunciation replacements textarea
        const pronunciationReplacements = document.getElementById('pronunciationReplacements');
        if (pronunciationReplacements) {
            initTextarea(pronunciationReplacements);
        }

        // Character bio/guidance textareas (dynamic)
        document.querySelectorAll('.character-guidance-input').forEach(textarea => {
            initTextarea(textarea);
        });
    }

    /**
     * Watch for dynamically added textareas
     */
    function watchForNewTextareas() {
        const bioList = document.getElementById('bioList');
        if (!bioList) return;

        const observer = new MutationObserver(mutations => {
            mutations.forEach(mutation => {
                // Check added nodes for textareas
                mutation.addedNodes.forEach(node => {
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        // Check if node itself is a textarea
                        if (node.matches && node.matches('.character-guidance-input')) {
                            initTextarea(node);
                        }
                        // Check descendants
                        node.querySelectorAll?.('.character-guidance-input').forEach(textarea => {
                            initTextarea(textarea);
                        });
                    }
                });
            });
        });

        observer.observe(bioList, {
            childList: true,
            subtree: true
        });
    }

    /**
     * Force resize all active textareas
     * Useful when visibility changes or layout shifts
     */
    function resizeAll() {
        const defaultPrompt = document.getElementById('defaultPrompt');
        if (defaultPrompt) resizeTextarea(defaultPrompt);
        const worldLore = document.getElementById('worldLore');
        if (worldLore) resizeTextarea(worldLore);

        // Agent prompt textareas
        const sceneContinuationPrompt2 = document.getElementById('sceneContinuationPrompt');
        if (sceneContinuationPrompt2) resizeTextarea(sceneContinuationPrompt2);
        const interjectionPromptMode = document.getElementById('interjectionPromptMode');
        if (interjectionPromptMode) resizeTextarea(interjectionPromptMode);
        const pronunciationReplacements = document.getElementById('pronunciationReplacements');
        if (pronunciationReplacements) resizeTextarea(pronunciationReplacements);

        document.querySelectorAll('.character-guidance-input').forEach(textarea => {
            resizeTextarea(textarea);
        });
    }

    // Public API
    return {
        init: () => {
            initAll();
            watchForNewTextareas();

            // Resize on window resize (in case font size changes)
            let resizeTimeout;
            window.addEventListener('resize', () => {
                clearTimeout(resizeTimeout);
                resizeTimeout = setTimeout(resizeAll, 150);
            });
        },
        initTextarea,
        resizeAll
    };
})();

// Initialize on load
document.addEventListener('DOMContentLoaded', async () => {
    // Inter-tab communication: close old tabs when new one opens (unless they have unsaved changes)
    if (window.BroadcastChannel) {
        const channel = new BroadcastChannel('sonorus-config');
        const tabId = Date.now() + Math.random();  // Unique ID for this tab
        let waitingForResponses = false;

        channel.onmessage = (e) => {
            if (e.data?.type === 'new-tab-opened' && e.data.tabId !== tabId) {
                // Another tab opened - check if we have unsaved changes
                if (dirty) {
                    // Tell new tab to close instead
                    channel.postMessage({ type: 'has-dirty', tabId: tabId });
                } else {
                    // We're clean, close ourselves
                    const closed = window.close();
                    if (!closed && document.body) {
                        const overlay = document.createElement('div');
                        overlay.innerHTML = `
                            <div style="position:fixed;inset:0;background:rgba(0,0,0,0.9);z-index:99999;
                                display:flex;flex-direction:column;align-items:center;justify-content:center;
                                font-family:Cinzel,serif;color:#c9a227;text-align:center;padding:2rem;">
                                <div style="font-size:2rem;margin-bottom:1rem;">Tab Superseded</div>
                                <div style="font-size:1rem;color:#a0a0a0;max-width:400px;">
                                    A new Sonorus configuration page has been opened.<br>
                                    You can safely close this tab.
                                </div>
                            </div>`;
                        document.body.appendChild(overlay);
                        for (let i = 1; i < 99999; i++) window.clearInterval(i);
                    }
                }
            } else if (e.data?.type === 'has-dirty' && waitingForResponses) {
                // An old tab has unsaved changes - we (new tab) should close
                waitingForResponses = false;
                const closed = window.close();
                if (!closed && document.body) {
                    const overlay = document.createElement('div');
                    overlay.innerHTML = `
                        <div style="position:fixed;inset:0;background:rgba(0,0,0,0.9);z-index:99999;
                            display:flex;flex-direction:column;align-items:center;justify-content:center;
                            font-family:Cinzel,serif;color:#c9a227;text-align:center;padding:2rem;">
                            <div style="font-size:2rem;margin-bottom:1rem;">Another Tab Has Unsaved Changes</div>
                            <div style="font-size:1rem;color:#a0a0a0;max-width:400px;">
                                Please save or discard changes in the other tab first.<br>
                                You can safely close this tab.
                            </div>
                        </div>`;
                    document.body.appendChild(overlay);
                    for (let i = 1; i < 99999; i++) window.clearInterval(i);
                }
            }
        };

        // Announce ourselves and wait briefly for "dirty" responses
        waitingForResponses = true;
        channel.postMessage({ type: 'new-tab-opened', tabId: tabId });
        setTimeout(() => { waitingForResponses = false; }, 500);
    }

    // Initialize Lucide icons
    if (window.lucide) {
        lucide.createIcons();
    }

    // Check setup status first (config not loaded yet, so config links won't be accurate)
    await checkSetupStatus();

    await loadConfig();

    // Now that config is loaded, update the "configure settings first" links
    updateSetupConfigLinks();

    // Initialize VRAM monitoring if NeuTTS + GPU is selected
    updateVramMonitoring();

    // Initialize reasoning toggles for model inputs (after config loaded)
    if (window.ReasoningToggle) {
        await ReasoningToggle.init(config);

        // Set master toggle state based on current provider's reasoning_enabled
        const provider = config.llm?.provider || 'gemini';
        const masterEnabled = config.llm?.[provider]?.reasoning_enabled === true;
        ReasoningToggle.setMasterEnabled(masterEnabled);
    }

    await loadDialogueHistory();
    await loadMigrationStatus();
    checkServerStatus();

    // Initialize commitments
    loadCommitments();
    loadCommitmentLocations();

    // Initialize auto-expanding textareas
    AutoExpandTextarea.init();

    // Initialize character search filter
    CharacterSearch.init();

    // Poll server status every 5 seconds (includes game time)
    setInterval(checkServerStatus, 5000);
    // Poll dialogue history and commitments every 10 seconds
    setInterval(loadDialogueHistory, 10000);
    setInterval(loadCommitments, 10000);

    // Event delegation for character accordion toggles
    document.getElementById('bioList').addEventListener('click', (e) => {
        const header = e.target.closest('.character-accordion-header');
        if (!header) return;
        const card = header.closest('.character-card');
        if (!card || card.classList.contains('player-card')) return;
        card.classList.toggle('collapsed');

        // Resize textareas when accordion expands (they may have wrong height when hidden)
        if (!card.classList.contains('collapsed')) {
            setTimeout(() => {
                card.querySelectorAll('.character-guidance-input').forEach(textarea => {
                    AutoExpandTextarea.initTextarea(textarea);
                });
            }, 50); // Small delay for CSS transition
        }
    });

    // Navigation link click handler - expand sections before scrolling
    document.querySelectorAll('.nav-links a').forEach(link => {
        link.addEventListener('click', (e) => {
            const href = link.getAttribute('href');
            if (href && href.startsWith('#')) {
                e.preventDefault();
                const sectionId = href.substring(1);
                scrollToSection(sectionId);
                // Close nav menu on mobile/small screens
                closeNav();
            }
        });
    });
});

// Server communication
async function checkServerStatus() {
    if (statusCheckInFlight) return;
    statusCheckInFlight = true;
    try {
        const response = await fetchWithTimeout('/health');
        const dot = document.getElementById('serverStatus');
        const text = document.getElementById('serverStatusText');
        const restartBtn = document.getElementById('restartServerBtn');
        const restartHint = document.getElementById('restartServerHint');

        if (response.ok) {
            const data = await response.json();
            dot.classList.remove('disconnected');
            text.textContent = 'Connected to Server';
            // Display version
            const versionBadge = document.getElementById('versionBadge');
            if (versionBadge && data.version) {
                versionBadge.textContent = 'v' + data.version;
            }
            // Enable restart button when connected
            if (restartBtn) {
                // If restart completed (went offline then came back), clear the flags
                if (restartInProgress && restartWentOffline) {
                    restartInProgress = false;
                    restartWentOffline = false;
                }
                if (!restartInProgress) {
                    restartBtn.disabled = false;
                    restartHint.textContent = 'Restart the Python server';
                }
            }
            // Update VR badge
            const vrBadge = document.getElementById('vrBadge');
            if (vrBadge) {
                if (data.vr?.active) {
                    vrBadge.style.display = '';
                    vrBadge.title = 'VR: ' + (data.vr.backend || 'Active');
                    if (vrBadge.querySelector('[data-lucide]')) lucide.createIcons({ nodes: [vrBadge] });
                } else {
                    vrBadge.style.display = 'none';
                }
            }
            // Update game time display + cache for commitments
            cachedGameTime = data.game_time;
            updateGameTimeDisplay(data.game_time);
        } else {
            throw new Error('Not OK');
        }
    } catch (e) {
        document.getElementById('serverStatus').classList.add('disconnected');
        document.getElementById('serverStatusText').textContent = 'Server Disconnected';
        // Track that server went offline during restart
        if (restartInProgress) {
            restartWentOffline = true;
        }
        // Disable restart button when disconnected
        const restartBtn = document.getElementById('restartServerBtn');
        const restartHint = document.getElementById('restartServerHint');
        if (restartBtn) {
            restartBtn.disabled = true;
            if (!restartInProgress) {
                restartHint.textContent = 'Server is offline';
            }
            // Keep showing "Server restarting..." if restart in progress
        }
    } finally {
        statusCheckInFlight = false;
    }
}

function updateGameTimeDisplay(data) {
    const display = document.getElementById('gameTimeDisplay');
    const clockFace = display?.querySelector('.clock-face');
    const timeText = document.getElementById('gameTimeText');
    const dateText = document.getElementById('gameDateText');
    const hourHand = document.getElementById('hourHand');
    const minuteHand = document.getElementById('minuteHand');
    const celestialBody = document.getElementById('celestialBody');

    if (data && data.available && data.gameTime) {
        const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
        const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

        // Parse time (format: "HH:MM AM/PM" or "HH:MM")
        const timeMatch = data.gameTime.match(/(\d+):(\d+)/);
        let hours = 0;
        let minutes = 0;

        if (timeMatch) {
            hours = parseInt(timeMatch[1], 10);
            minutes = parseInt(timeMatch[2], 10);

            // Handle 12-hour format if PM/AM is present
            if (data.gameTime.includes('PM') && hours !== 12) {
                hours += 12;
            } else if (data.gameTime.includes('AM') && hours === 12) {
                hours = 0;
            }
        }

        // Update clock hands (360 degrees / 12 hours = 30 degrees per hour)
        const hourAngle = (hours % 12) * 30 + (minutes / 60) * 30;
        const minuteAngle = minutes * 6; // 360 / 60 = 6 degrees per minute

        if (hourHand) hourHand.style.transform = `rotate(${hourAngle}deg)`;
        if (minuteHand) minuteHand.style.transform = `rotate(${minuteAngle}deg)`;

        // Update celestial body (day: 6am-6pm, night: 6pm-6am)
        if (celestialBody) {
            if (hours >= 6 && hours < 18) {
                celestialBody.classList.remove('night');
            } else {
                celestialBody.classList.add('night');
            }
        }

        // Format display text
        const dayName = days[data.dayOfWeek] || '';
        const monthName = months[data.month - 1] || '';
        const dateStr = `${dayName}, ${monthName} ${data.day}, ${data.year}`;

        if (timeText) timeText.textContent = data.gameTime;
        if (dateText) dateText.textContent = dateStr;

        // Show clock with data
        if (clockFace) clockFace.classList.add('clock-has-data');
        display.style.display = 'block';
    } else {
        // Show clock in waiting state
        if (clockFace) clockFace.classList.remove('clock-has-data');
        if (timeText) timeText.textContent = 'Awaiting Game Data';
        if (dateText) dateText.textContent = '';
        display.style.display = 'block';
    }
}

async function restartServer() {
    const btn = document.getElementById('restartServerBtn');
    const hint = document.getElementById('restartServerHint');

    btn.disabled = true;
    hint.textContent = 'Restarting server...';
    restartInProgress = true;

    try {
        await fetch('/restart', { method: 'POST' });
        hint.textContent = 'Server restarting, please wait...';
        // Page will reconnect via existing health check loop
        // restartInProgress cleared when server goes offline then comes back
    } catch (e) {
        hint.textContent = 'Restart signal sent';
    }
}

async function deleteLogsWithConfirm() {
    if (!confirm('Are you sure you want to delete all log files? This cannot be undone.')) {
        return;
    }

    const btn = document.getElementById('deleteLogsBtn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Deleting...';

    try {
        const response = await fetch('/api/logs', { method: 'DELETE' });
        if (response.ok) {
            const data = await response.json();
            showToast(`Deleted ${data.deleted} log files`, 'success');
        } else {
            const data = await response.json();
            showToast('Failed to delete logs: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (e) {
        showToast('Failed to delete logs: ' + e.message, 'error');
    } finally {
        btn.textContent = originalText;
        btn.disabled = false;
    }
}

async function copyLogsToClipboard() {
    const btn = document.getElementById('copyLogsBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;

    try {
        // Fetch server logs from console buffer via API
        let serverLogs = '';
        try {
            const response = await fetch('/api/logs/server');
            if (response.ok) {
                const data = await response.json();
                serverLogs = data.content || '(Empty console buffer)';
            } else {
                const data = await response.json();
                serverLogs = `(Error: ${data.error || 'Failed to read console'})`;
            }
        } catch (e) {
            serverLogs = `(Error fetching server logs: ${e.message})`;
        }

        // Fetch client logs from UE4SS.log via API
        let clientLogs = '';
        try {
            const response = await fetch('/api/logs/client');
            if (response.ok) {
                const data = await response.json();
                clientLogs = data.content || '(Empty log file)';
            } else {
                const data = await response.json();
                clientLogs = `(Error: ${data.error || 'Failed to read log file'})`;
            }
        } catch (e) {
            clientLogs = `(Error fetching client logs: ${e.message})`;
        }

        // Combine logs with markdown headers
        const combinedLogs = `# SERVER LOGS\n\n${serverLogs}\n\n# CLIENT LOGS\n\n${clientLogs}`;

        // Copy to clipboard
        await navigator.clipboard.writeText(combinedLogs);

        btn.innerHTML = '<i data-lucide="check"></i> Copied!';
        lucide.createIcons();
        showToast('Logs copied to clipboard', 'success');

        setTimeout(() => {
            btn.innerHTML = originalText;
            btn.disabled = false;
            lucide.createIcons();
        }, 2000);
    } catch (e) {
        console.error('Failed to copy logs:', e);
        btn.innerHTML = '<i data-lucide="x"></i> Failed';
        lucide.createIcons();
        showToast('Failed to copy logs: ' + e.message, 'error');

        setTimeout(() => {
            btn.innerHTML = originalText;
            btn.disabled = false;
            lucide.createIcons();
        }, 2000);
    }
}

async function loadConfig() {
    try {
        // Load model presets early (cached for later use)
        await loadModelPresets();

        const response = await fetch('/api/config');
        if (response.ok) {
            config = await response.json();
            await populateForm(config);
        }
    } catch (e) {
        console.error('Failed to load config:', e);
        showToast('Failed to load configuration', 'error');
    } finally {
        isInitializing = false;
    }
}

async function populateForm(cfg) {
    // Server
    setCheckbox('modEnabled', cfg.server?.enabled !== false);
    setCheckbox('autoOpenConfig', cfg.server?.auto_open_config !== false);
    setCheckbox('devModeEnabled', cfg.dev?.enabled === true);

    // LLM Provider - Dynamic provider settings
    const currentLLMProvider = cfg.llm?.provider || 'gemini';
    const llmDropdown = document.getElementById('llmProvider');
    llmDropdown.value = currentLLMProvider;
    // LLM API key - use provider-specific key
    refreshLLMApiKeyField(currentLLMProvider);
    renderLLMProviderSettings(currentLLMProvider);
    updateLLMProviderHint(currentLLMProvider);
    updateConcurrencyHints(currentLLMProvider);

    // TTS - Dynamic provider settings
    const currentTTSProvider = cfg.tts?.provider || 'inworld';
    const ttsDropdown = document.getElementById('ttsProvider');
    ttsDropdown.innerHTML = generateProviderDropdown('tts', currentTTSProvider);
    renderProviderSettings('tts', currentTTSProvider);
    // Apply model-dependent field states after render
    if (currentTTSProvider === 'inworld') {
        onInworldModelChange(cfg.tts?.inworld?.model || 'inworld-tts-1.5-max');
    }
    updatePlayerVoiceSectionState(currentTTSProvider);

    // TTS common settings
    setCheckbox('spatialAudio', cfg.audio?.spatial !== false);
    setCheckbox('autoClone', cfg.tts?.auto_clone !== false);

    // STT - Dynamic provider settings
    const currentSTTProvider = cfg.stt?.provider || 'none';
    const sttDropdown = document.getElementById('sttProvider');
    sttDropdown.innerHTML = generateSTTProviderDropdown(currentSTTProvider);
    renderSTTProviderSettings(currentSTTProvider);
    updateRamMonitoring();
    setFieldValue('stt_hotkey', cfg.stt?.hotkey || 'middle_mouse');
    setFieldValue('input_stt_hotkey', cfg.stt?.hotkey || 'middle_mouse');
    setCheckbox('stt_voice_spells', cfg.stt?.voice_spells !== false);
    setFieldValue('stt_mic_gain_db', cfg.stt?.mic_gain_db ?? 0);
    updateRangeValue('stt_mic_gain_db', (cfg.stt?.mic_gain_db ?? 0) + ' dB');

    // Open Mic settings
    const openMicEnabled = cfg.open_mic?.enabled === true;
    setCheckbox('open_mic_enabled', openMicEnabled);
    setFieldValue('open_mic_vad_threshold', cfg.open_mic?.vad_threshold ?? 0.5);
    setFieldValue('open_mic_utterance_end_ms', cfg.open_mic?.utterance_end_ms ?? 500);
    setFieldValue('open_mic_turn_timeout', cfg.open_mic?.turn_timeout_secs ?? 3.0);
    updateRangeValue('open_mic_vad_threshold', (cfg.open_mic?.vad_threshold ?? 0.5).toFixed(2));
    updateRangeValue('open_mic_utterance_end_ms', (cfg.open_mic?.utterance_end_ms ?? 500) + 'ms');
    updateRangeValue('open_mic_turn_timeout', (cfg.open_mic?.turn_timeout_secs ?? 3.0).toFixed(1) + 's');
    toggleOpenMicSettings(openMicEnabled);

    // Player voice settings (in Voice Synthesis section, but stored in conversation settings)
    const playerVoiceEnabled = cfg.conversation?.player_voice_enabled !== false;
    setCheckbox('playerVoiceEnabled', playerVoiceEnabled);
    setCheckbox('playerVoiceSpatial', cfg.conversation?.player_voice_spatial !== false);
    setFieldValue('playerVoiceName', cfg.conversation?.player_voice_name || '');
    setFieldValue('playerVoiceModel', cfg.conversation?.player_voice_model || '');
    updatePlayerVoiceSubSettings(playerVoiceEnabled);

    // Agents - render all agent settings dynamically
    for (const agentId of Object.keys(AGENT_CONFIGS)) {
        renderAgentSettings(agentId);
    }
    // Update model placeholders based on current provider (without changing values)
    await updateModelPlaceholders(currentLLMProvider);

    // Audio
    setFieldValue('masterVolume', cfg.audio?.volume ?? 100);
    setFieldValue('narrationVolume', cfg.audio?.narration_volume ?? 80);
    setCheckbox('audioReverb', cfg.audio?.reverb !== false);
    setCheckbox('audioVrTracking', cfg.audio?.vr_tracking !== false);
    setFieldValue('audioCameraOffset', cfg.audio?.camera_offset ?? 0);
    setCheckbox('lipsyncFallback', cfg.lipsync?.fallback === true);

    // Pronunciation replacements
    const pronEl = document.getElementById('pronunciationReplacements');
    if (pronEl) {
        const pronData = cfg.audio?.pronunciation_replacements;
        const hasData = pronData && typeof pronData === 'object' && Object.keys(pronData).length > 0;
        pronEl.value = pronunciationReplacementsToText(hasData ? pronData : DEFAULT_PRONUNCIATION_REPLACEMENTS);
        if (!hasData) {
            updateSetting('audio.pronunciation_replacements', DEFAULT_PRONUNCIATION_REPLACEMENTS);
        }
    }

    // History
    setFieldValue('maxHistory', cfg.history?.max_entries || 100);
    setFieldValue('dedupWindow', cfg.history?.dedup_window || 5);
    setFieldValue('ambientDedupWindow', cfg.history?.ambient_dedup_window || 15);
    setCheckbox('trackAmbient', cfg.history?.track_ambient !== false);
    setCheckbox('trackCutscene', cfg.history?.track_cutscene !== false);
    setCheckbox('realisticMemory', cfg.history?.realistic_memory !== false);
    const maxLocEntries = cfg.history?.max_location_entries ?? 2;
    setFieldValue('maxLocationEntries', maxLocEntries);
    const maxSpellEntries = cfg.history?.max_spell_entries ?? 3;
    setFieldValue('maxSpellEntries', maxSpellEntries);

    // Commitments
    setCheckbox('commitmentsEnabled', cfg.commitments?.enabled === true);

    // Long-Term Memory
    setCheckbox('memoryEnabled', cfg.memory?.enabled === true);
    setFieldValue('chapterModel', cfg.memory?.chapter_model || 'gemini-2.0-flash');
    setFieldValue('proseModel', cfg.memory?.prose_model || 'gemini-2.0-flash');
    setFieldValue('graphitiModel', cfg.memory?.graphiti_model || 'gemini-2.0-flash');
    setFieldValue('graphitiSmallModel', cfg.memory?.graphiti_small_model || 'gemini-2.0-flash');
    setFieldValue('rerankerModel', cfg.memory?.reranker_model || 'meta-llama/llama-3.1-8b-instruct');
    setFieldValue('maxConcurrency', cfg.memory?.max_concurrency || 2);
    setFieldValue('chapterEntryThreshold', cfg.memory?.chapter_entry_threshold || 30);

    // Show provider-specific concurrency warnings
    updateConcurrencyHints(cfg.llm?.provider || 'gemini');

    // Conversation - Chat Models
    setFieldValue('conv_chat_model', cfg.conversation?.chat_model || GEMINI_3_FLASH);
    setFieldValue('conv_temperature', cfg.conversation?.temperature || 1.0);
    setFieldValue('conv_max_tokens', cfg.conversation?.max_tokens || 8192);
    updateGemini3TempHint();

    // Conversation - General settings
    setFieldValue('conv_max_turns', cfg.conversation?.max_turns || 6);
    setCheckbox('conv_target_use_crosshair', cfg.conversation?.target_selection_use_crosshair === true);
    setFieldValue('conv_target_model', cfg.conversation?.target_selection_model || 'gemini-2.5-flash-lite');
    setFieldValue('conv_speaker_max_tokens', cfg.conversation?.speaker_selection_max_tokens || 512);
    setFieldValue('conv_interjection_model', cfg.conversation?.interjection_model || 'gemini-2.5-flash-lite');
    updateRangeValue('conv_speaker_max_tokens', (cfg.conversation?.speaker_selection_max_tokens || 512) + ' tokens');
    // Input correction: use provider-aware default if not explicitly set
    const inputCorrectionExplicit = cfg.conversation?.input_correction_enabled;
    const inputCorrectionDefault = FEATURE_DEFAULTS[cfg.llm?.provider || 'gemini']?.['conversation.input_correction_enabled']?.default ?? false;
    setCheckbox('conv_input_correction_enabled', inputCorrectionExplicit !== undefined ? inputCorrectionExplicit : inputCorrectionDefault);
    setFieldValue('conv_input_correction_model', cfg.conversation?.input_correction_model || 'gemini-2.5-flash-lite');
    setCheckbox('conv_actions_enabled', cfg.conversation?.actions_enabled === true);
    // Floo Flame Companions is now compatible with NPC Actions (uses SetSystemicCompanionBP)
    setCheckbox('conv_gear_context', cfg.conversation?.gear_context !== false);
    setCheckbox('conv_mission_context', cfg.conversation?.mission_context !== false);
    setCheckbox('conv_sentence_subtitles', cfg.conversation?.sentence_subtitles !== false);
    // Companion callout blocking: default 1440 (1 game day)
    setFieldValue('conv_companion_callout_block', cfg.conversation?.companion_callout_block_minutes ?? 1440);
    setCheckbox('conv_companion_move_enabled', cfg.conversation?.companion_move_enabled !== false);
    setCheckbox('conv_narration_enabled', cfg.conversation?.narration_enabled === true);
    // Companion follow distance (meters)
    const followDist = cfg.conversation?.companion_follow_distance_m ?? 2.0;
    setFieldValue('companion_follow_distance', followDist);
    updateRangeValue('companion_follow_distance', followDist.toFixed(1) + ' m');

    // Input settings
    setFieldValue('input_chat_hotkey', cfg.input?.chat_hotkey || 'enter');
    setFieldValue('input_stop_hotkey', cfg.input?.stop_hotkey || 'delete');
    setFieldValue('input_mode_hotkey', cfg.input?.mode_hotkey || 'home');
    setCheckbox('input_preview_lock', cfg.input?.preview_lock !== false);

    // Time Dilation settings
    loadTimeDilationSettings(cfg.time_dilation || {});

    // Prompts
    if (cfg.prompts?.default) {
        document.getElementById('defaultPrompt').value = cfg.prompts.default;
    }
    validateDefaultPromptPlaceholders();
    setFieldValue('worldLore', cfg.prompts?.world_lore || '');
    if (cfg.prompts?.scene_continuation) {
        document.getElementById('sceneContinuationPrompt').value = cfg.prompts.scene_continuation;
    }
    if (cfg.prompts?.interjection_prompt_mode) {
        document.getElementById('interjectionPromptMode').value = cfg.prompts.interjection_prompt_mode;
    }

    // Character settings (editor guidance + viseme scales + temp mods)
    // Support both new 'editor_guidance' and legacy 'bios' key
    const editorGuidance = cfg.prompts?.editor_guidance || cfg.prompts?.bios || {};
    await populateCharacters(editorGuidance, cfg.lipsync?.npc_scales || {}, cfg.tts?.npc_temp_modifiers || {}, cfg.tts?.npc_model_overrides || {});

    // Update range display values
    updateRangeValue('conv_temperature', document.getElementById('conv_temperature').value);
    updateRangeValue('conv_max_tokens', document.getElementById('conv_max_tokens').value + ' tokens');
    updateRangeValue('conv_speaker_max_tokens', document.getElementById('conv_speaker_max_tokens').value + ' tokens');
    updateRangeValue('masterVolume', document.getElementById('masterVolume').value + '%');
    updateRangeValue('narrationVolume', document.getElementById('narrationVolume').value + '%');
    updateRangeValue('audioCameraOffset', document.getElementById('audioCameraOffset').value + 'm');
    updateRangeValue('maxHistory', document.getElementById('maxHistory').value);
    updateRangeValue('dedupWindow', document.getElementById('dedupWindow').value + ' min');
    updateRangeValue('ambientDedupWindow', document.getElementById('ambientDedupWindow').value + ' min');
    const locVal = document.getElementById('maxLocationEntries').value;
    updateRangeValue('maxLocationEntries', locVal == 0 ? 'None' : locVal);
    const spellVal = document.getElementById('maxSpellEntries').value;
    updateRangeValue('maxSpellEntries', spellVal == 0 ? 'None' : spellVal);

    // Refresh reasoning toggles after form population
    if (window.ReasoningToggle) {
        ReasoningToggle.refresh();
    }

    // Update local TTS/STT availability based on game language
    const setupLanguage = cfg.setup?.language || 'EN_US';
    updatePocketTTSAvailability(setupLanguage);
    updateParakeetSTTAvailability(setupLanguage);
    updateCanarySTTAvailability(setupLanguage);
    updateMoonshineSTTAvailability(setupLanguage);
}

function setFieldValue(id, value) {
    const el = document.getElementById(id);
    if (el && value !== undefined && value !== null) {
        el.value = value;
    }
}

function setCheckbox(id, value) {
    const el = document.getElementById(id);
    if (el) el.checked = value !== false;
}

function updateMemoryAvailability(provider) {
    const memoryToggle = document.getElementById('memoryEnabled');
    const memoryWarning = document.getElementById('memoryGeminiWarning');

    if (provider === 'gemini') {
        // Disable memory for Gemini
        const wasEnabled = memoryToggle?.checked;
        if (memoryToggle) {
            memoryToggle.checked = false;
            memoryToggle.disabled = true;
        }
        if (memoryWarning) {
            memoryWarning.style.display = 'block';
        }
        updateSetting('memory.enabled', false);
        // Notify user if memory was enabled and is now being disabled
        if (wasEnabled && !isInitializing) {
            showToast('Long-term memory disabled (not compatible with Gemini provider)', 'info');
        }
    } else {
        // Enable memory toggle for OpenRouter/OpenAI
        if (memoryToggle) {
            memoryToggle.disabled = false;
        }
        if (memoryWarning) {
            memoryWarning.style.display = 'none';
        }
    }
}

function updateGemini3TempHint() {
    const modelInput = document.getElementById('conv_chat_model');
    const hint = document.getElementById('geminiTempHint');
    if (!modelInput || !hint) return;
    const model = modelInput.value.toLowerCase();
    const isGemini3 = model.includes('gemini-3') || model.includes('gemini3');
    hint.style.display = isGemini3 ? 'block' : 'none';
    if (hint.style.display === 'block') lucide?.createIcons?.({ nameAttr: 'data-lucide', attrs: {} });
}

function updateConcurrencyHints(provider) {
    const geminiWarning = document.getElementById('concurrencyWarningGemini');
    const openrouterHint = document.getElementById('concurrencyHintOpenrouter');

    if (geminiWarning) geminiWarning.style.display = (provider === 'gemini') ? 'block' : 'none';
    if (openrouterHint) openrouterHint.style.display = (provider === 'openrouter') ? 'block' : 'none';

    // Update memory availability
    updateMemoryAvailability(provider);
}

function updatePocketTTSAvailability(language) {
    const ttsDropdown = document.getElementById('ttsProvider');
    if (!ttsDropdown) return;

    const currentProvider = ttsDropdown.value;
    const isEnglish = language === 'EN_US';

    // If switching to non-English and pocket/neutts is selected, change to "none"
    if (!isEnglish && (currentProvider === 'pocket' || currentProvider === 'neutts')) {
        switchProvider('tts', 'none');
        const providerName = currentProvider === 'pocket' ? 'Pocket TTS' : 'NeuTTS';
        showToast(`${providerName} only supports English. Switched to Disabled.`, 'warning');
    }

    // Regenerate dropdown to update disabled state
    ttsDropdown.innerHTML = generateProviderDropdown('tts', ttsDropdown.value);
}

async function populateCharacters(editorGuidance, npcScales = {}, ttsTempMods = {}, modelOverrides = {}) {
    const container = document.getElementById('bioList');
    container.innerHTML = '';

    // Check if memory is enabled for generated bio display
    const memoryEnabled = config?.memory?.enabled || false;

    // Always show Player guidance first (not collapsible)
    const playerGuidance = editorGuidance.Player || '';
    addCharacterCard('Player', playerGuidance, 1.0, 0, true, memoryEnabled);

    // Collect all unique NPC names from guidance, scales, temp mods, model overrides, and generated bios
    const allNpcs = new Set([
        ...Object.keys(editorGuidance).filter(n => n !== 'Player'),
        ...Object.keys(npcScales).filter(n => n !== 'Player'),
        ...Object.keys(ttsTempMods).filter(n => n !== 'Player'),
        ...Object.keys(modelOverrides).filter(n => n !== 'Player')
    ]);

    // If memory is enabled, also fetch NPCs with generated bios
    if (memoryEnabled) {
        try {
            const resp = await fetch('/api/memories/npcs-with-bios');
            const data = await resp.json();
            if (data.success && Array.isArray(data.npcs)) {
                for (const npcId of data.npcs) {
                    allNpcs.add(npcId);
                }
            }
        } catch (e) {
            console.error('Failed to fetch NPCs with bios:', e);
        }
    }

    // Then other characters (collapsible, default collapsed)
    for (const name of allNpcs) {
        const guidance = editorGuidance[name] || '';
        const scale = npcScales[name] || 1.0;
        const tempMod = ttsTempMods[name] || 0;
        const model = modelOverrides[name] || '';
        addCharacterCard(name, guidance, scale, tempMod, false, memoryEnabled, model);
    }

    // Load generated bios for all NPCs (if memory enabled)
    if (memoryEnabled) {
        const cards = document.querySelectorAll('.character-card:not(.player-card)');
        cards.forEach(card => {
            const npcId = card.dataset.npcId;
            if (npcId) {
                loadGeneratedBio(npcId, card);
            }
        });
    }

    // Refresh character search filter after population
    if (typeof CharacterSearch !== 'undefined') {
        CharacterSearch.refresh();
    }
}

// Legacy alias for backwards compatibility
function populateBios(bios, npcScales = {}, ttsTempMods = {}, modelOverrides = {}) {
    populateCharacters(bios, npcScales, ttsTempMods, modelOverrides);
}

/**
 * Refresh labels, hints, and placeholders for all character cards based on memory state.
 * Called when memory toggle changes to update UI without page refresh.
 */
function refreshCharacterLabels(memoryEnabled) {
    const cards = document.querySelectorAll('.character-card');
    cards.forEach(card => {
        const isPlayer = card.classList.contains('player-card');

        // Update guidance field label and hint
        const guidanceLabel = card.querySelector('.field-label');
        const guidanceHint = card.querySelector('.field-hint');
        const guidanceTextarea = card.querySelector('.character-guidance-input');

        if (guidanceLabel && (guidanceLabel.textContent.includes('Bio') || guidanceLabel.textContent.includes('Guidance'))) {
            // Player always shows "Bio", NPCs switch based on memory state
            guidanceLabel.textContent = isPlayer ? 'Bio' : (memoryEnabled ? "Editor's Guidance" : 'Bio');
        }

        if (guidanceHint && !guidanceHint.querySelector('.btn-link')) {
            guidanceHint.textContent = isPlayer
                ? 'Static facts about the player known to all NPCs (personality, background, etc.). Always injected alongside dynamic memories.'
                : (memoryEnabled
                    ? 'Character essence used when generating bio from memory. Used as fallback context if no bio has been generated yet.'
                    : 'Biographical context injected into prompts when this character speaks.');
        }

        if (guidanceTextarea) {
            guidanceTextarea.placeholder = isPlayer
                ? 'e.g. Ambitious, cunning, from a wealthy family...'
                : (memoryEnabled
                    ? 'e.g. Speaks with subtle arrogance. Protective of family...'
                    : 'Character biography/background...');
        }

        // Toggle generated bio section visibility
        if (!isPlayer) {
            const bioSection = card.querySelector('.generated-bio-section');
            if (memoryEnabled) {
                // Show generated bio section if it exists
                if (!bioSection) {
                    // Create section if it doesn't exist
                    const npcId = card.dataset.npcId;
                    const bioSectionHTML = `
                                <div class="field-group generated-bio-section">
                                    <label class="field-label">
                                        Generated Bio
                                        <span class="bio-timestamp" style="font-weight: normal; font-size: 0.75rem; opacity: 0.7;"></span>
                                    </label>
                                    <p class="field-hint">
                                        Auto-generated from long-term memory.
                                        <button type="button" class="btn-link" onclick="regenerateBioFromCard(this)" style="font-size: 0.8rem;">Regenerate</button>
                                    </p>
                                    <div class="generated-bio-display" style="background: var(--parchment-light); border: 1px solid var(--leather-border); border-radius: 4px; padding: 8px; min-height: 60px; font-size: 0.85rem; white-space: pre-wrap;">
                                        <span style="opacity: 0.5; font-style: italic;">No generated bio yet. Click Regenerate or wait for a conversation.</span>
                                    </div>
                                </div>
                            `;
                    const content = card.querySelector('.character-accordion-content');
                    // Insert after the guidance field
                    const guidanceFieldGroup = card.querySelector('.field-group');
                    if (guidanceFieldGroup && content) {
                        guidanceFieldGroup.insertAdjacentHTML('afterend', bioSectionHTML);
                        // Load existing bio if available
                        if (npcId) {
                            loadGeneratedBio(npcId, card);
                        }
                    }
                } else {
                    bioSection.style.display = '';
                }
            } else {
                // Hide generated bio section
                if (bioSection) {
                    bioSection.style.display = 'none';
                }
            }
        }
    });
}

function addCharacterCard(name = '', guidance = '', visemeScale = 1.0, ttsTempMod = 0, isPlayer = false, memoryEnabled = false, modelOverride = '') {
    const container = document.getElementById('bioList');
    const card = document.createElement('div');
    card.className = isPlayer ? 'character-card player-card' : 'character-card collapsed';
    card.dataset.npcId = name;  // Store NPC ID for bio fetching

    const displayName = name || 'New Character';
    const toggleIcon = isPlayer ? '' : '<span class="character-accordion-toggle">&#9660;</span>';

    const nameField = isPlayer
        ? `<span style="font-family: var(--font-display); font-weight: 600;">Player</span>`
        : `<input type="text" class="character-name-input" value="${escapeHtml(name)}" placeholder="Character ID (e.g. SebastianSallow)" onchange="updateCharacterTitle(this); this.closest('.character-card').dataset.npcId = this.value; markDirty()">`;

    const removeBtn = isPlayer
        ? ''
        : `<button class="btn btn-danger" onclick="event.stopPropagation(); removeCharacterCard(this);" style="padding: 4px 8px; font-size: 0.7rem;">Remove</button>`;

    // Format ttsTempMod with sign for display
    const ttsTempModDisplay = ttsTempMod >= 0 ? `+${ttsTempMod.toFixed(2)}` : ttsTempMod.toFixed(2);

    // Generated bio section (only for NPCs when memory enabled, hidden otherwise)
    const generatedBioSection = (isPlayer || !memoryEnabled) ? '' : `
                <div class="field-group generated-bio-section">
                    <label class="field-label">
                        Generated Bio
                        <span class="bio-timestamp" style="font-weight: normal; font-size: 0.75rem; opacity: 0.7;"></span>
                    </label>
                    <p class="field-hint">
                        Auto-generated from long-term memory.
                        <button type="button" class="btn-link" onclick="regenerateBioFromCard(this)" style="font-size: 0.8rem;">Regenerate</button>
                    </p>
                    <div class="generated-bio-display" style="background: var(--parchment-light); border: 1px solid var(--leather-border); border-radius: 4px; padding: 8px; min-height: 60px; font-size: 0.85rem; white-space: pre-wrap;">
                        <span style="opacity: 0.5; font-style: italic;">No generated bio yet. Click Regenerate or wait for a conversation.</span>
                    </div>
                </div>
            `;

    card.innerHTML = `
                <div class="character-accordion-header">
                    <div class="character-accordion-title">
                        <span class="character-title-text">${escapeHtml(displayName)}</span>
                        ${isPlayer ? '<span style="font-size: 0.75rem; opacity: 0.7;">(always included)</span>' : ''}
                    </div>
                    ${toggleIcon}
                </div>
                <div class="character-accordion-content">
                    ${isPlayer ? '' : `
                    <div class="field-group">
                        <label class="field-label">Character ID</label>
                        ${nameField}
                    </div>
                    `}
                    <div class="field-group">
                        <label class="field-label">${isPlayer ? 'Bio' : (memoryEnabled ? "Editor's Guidance" : 'Bio')}</label>
                        <p class="field-hint">${isPlayer
            ? 'Static facts about the player known to all NPCs (personality, background, etc.). Always injected alongside dynamic memories.'
            : (memoryEnabled
                ? 'Character essence used when generating bio from memory. Used as fallback context if no bio has been generated yet.'
                : 'Biographical context injected into prompts when this character speaks.')}</p>
                        <textarea class="character-guidance-input" placeholder="${isPlayer
            ? 'e.g. Ambitious, cunning, from a wealthy family...'
            : (memoryEnabled
                ? 'e.g. Speaks with subtle arrogance. Protective of family...'
                : 'Character biography/background...')}"
                                  onchange="markDirty()">${escapeHtml(guidance)}</textarea>
                    </div>
                    ${generatedBioSection}
                    <div class="field-group">
                        <label class="field-label">Viseme Scale</label>
                        <p class="field-hint">Lip sync intensity (0.5 = subtle, 1.0 = normal, 1.5 = exaggerated)</p>
                        <div class="range-wrapper">
                            <input type="range" class="character-viseme-scale" min="0.5" max="1.5" step="0.1" value="${visemeScale}"
                                   oninput="this.nextElementSibling.textContent = this.value; markDirty()">
                            <span class="range-value">${visemeScale}</span>
                        </div>
                    </div>
                    ${isPlayer ? '' : `
                    <div class="field-group">
                        <label class="field-label">TTS Temperature Modifier</label>
                        <p class="field-hint">Defaults are usually best. Only increase if voice sounds flat; too high causes instability.</p>
                        <div class="range-wrapper">
                            <input type="range" class="character-tts-temp-mod" min="-0.9" max="0.9" step="0.05" value="${ttsTempMod}"
                                   oninput="this.nextElementSibling.textContent = (parseFloat(this.value) >= 0 ? '+' : '') + parseFloat(this.value).toFixed(2); markDirty()">
                            <span class="range-value">${ttsTempModDisplay}</span>
                        </div>
                    </div>
                    <div class="field-group">
                        <label class="field-label">TTS Model Override</label>
                        <p class="field-hint">If you prefer a specific TTS model for this character. Leave empty to use the provider default.</p>
                        <input type="text" class="character-model-override" value="${escapeHtml(modelOverride)}" placeholder="Use provider default"
                               onchange="markDirty()">
                    </div>
                    `}
                    ${removeBtn ? `<div class="character-actions">${removeBtn}</div>` : ''}
                </div>
            `;
    container.appendChild(card);

    // Load generated bio if memory is enabled and this is an NPC
    if (memoryEnabled && !isPlayer && name) {
        loadGeneratedBio(name, card);
    }

    // Refresh character search filter after adding card
    if (typeof CharacterSearch !== 'undefined') {
        setTimeout(() => CharacterSearch.refresh(), 10);
    }
}

// Helper function to remove character card with search refresh
function removeCharacterCard(button) {
    const card = button.closest('.character-card');
    if (card) {
        card.remove();
        markDirty();

        // Refresh character search filter after removal
        if (typeof CharacterSearch !== 'undefined') {
            setTimeout(() => CharacterSearch.refresh(), 50);
        }
    }
}

// Legacy alias
function addBioCard(name = '', bio = '', visemeScale = 1.0, ttsTempMod = 0, isPlayer = false) {
    const memoryEnabled = config?.memory?.enabled || false;
    addCharacterCard(name, bio, visemeScale, ttsTempMod, isPlayer, memoryEnabled);
}

async function loadGeneratedBio(npcId, card) {
    try {
        const resp = await fetch(`/api/memories/bio/${encodeURIComponent(npcId)}`);
        const data = await resp.json();

        if (data.success && data.formatted) {
            const display = card.querySelector('.generated-bio-display');
            const timestamp = card.querySelector('.bio-timestamp');
            if (display) {
                display.innerHTML = escapeHtml(data.formatted);
            }
            if (timestamp && data.last_updated) {
                const date = new Date(data.last_updated * 1000);
                timestamp.textContent = `(Last: ${date.toLocaleDateString()})`;
            }
        }
    } catch (e) {
        console.error(`Failed to load bio for ${npcId}:`, e);
    }
}

function regenerateBioFromCard(buttonElement) {
    const card = buttonElement?.closest('.character-card');
    const npcId = card?.dataset.npcId;
    if (npcId) {
        regenerateBio(npcId, buttonElement);
    }
}

async function regenerateBio(npcId, buttonElement) {
    if (!npcId) return;

    const card = buttonElement?.closest('.character-card');
    const display = card?.querySelector('.generated-bio-display');
    const originalText = buttonElement?.textContent;

    try {
        if (buttonElement) {
            buttonElement.textContent = 'Regenerating...';
            buttonElement.disabled = true;
        }
        if (display) {
            display.innerHTML = '<span style="opacity: 0.5; font-style: italic;">Regenerating bio from memory...</span>';
        }

        const resp = await fetch(`/api/memories/bio/${encodeURIComponent(npcId)}/regenerate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await resp.json();

        if (data.success && data.formatted) {
            if (display) {
                display.innerHTML = escapeHtml(data.formatted);
            }
            const timestamp = card?.querySelector('.bio-timestamp');
            if (timestamp && data.last_updated) {
                const date = new Date(data.last_updated * 1000);
                timestamp.textContent = `(Last: ${date.toLocaleDateString()})`;
            }
        } else {
            if (display) {
                display.innerHTML = `<span style="color: var(--danger);">${escapeHtml(data.error || 'Failed to regenerate bio')}</span>`;
            }
        }
    } catch (e) {
        console.error(`Failed to regenerate bio for ${npcId}:`, e);
        if (display) {
            display.innerHTML = `<span style="color: var(--danger);">Error: ${escapeHtml(e.message)}</span>`;
        }
    } finally {
        if (buttonElement) {
            buttonElement.textContent = originalText || 'Regenerate';
            buttonElement.disabled = false;
        }
    }
}

function updateCharacterTitle(input) {
    const card = input.closest('.character-card');
    const titleText = card.querySelector('.character-title-text');
    titleText.textContent = input.value || 'New Character';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function addCharacterBio() {
    addBioCard('', '', 1.0, 0, false);
    // Expand the newly added card
    const cards = document.querySelectorAll('#bioList .character-card');
    const lastCard = cards[cards.length - 1];
    if (lastCard) {
        lastCard.classList.remove('collapsed');
    }
    markDirty();
}

// Pagination state
let allHistory = [];
let filteredHistory = null;  // null = no filter (show all)
let npcChapters = null;      // Chapter data for selected NPC (for dividers)
let currentPage = 1;
const ITEMS_PER_PAGE = 100;

// Edit mode state
let historyEditMode = false;
let selectedHistoryEntries = new Set();  // stores timestamps as strings for precision

// NPC name utilities (mirrors Python's dialogue.py)
const GENERIC_NPC_PREFIXES = [
    'AdultMale', 'AdultFemale', 'ElderlyMale', 'ElderlyFemale',
    'ChildMale', 'ChildFemale', 'TeenMale', 'TeenFemale'
];

function isNamedNPC(voiceName) {
    if (!voiceName) return false;
    const lower = voiceName.toLowerCase();
    if (lower === 'player' || lower === 'playermale' || lower === 'playerfemale') return false;
    return !GENERIC_NPC_PREFIXES.some(prefix => voiceName.startsWith(prefix));
}

function prettifyVoiceName(voiceName) {
    if (!voiceName) return 'Unknown';
    const lower = voiceName.toLowerCase();
    if (lower === 'player' || lower === 'playermale' || lower === 'playerfemale') return 'Player';
    // Add spaces before capitals: "SebastianSallow" -> "Sebastian Sallow"
    return voiceName.replace(/([a-z])([A-Z])/g, '$1 $2')
        .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2');
}

function shortenNameForButton(displayName) {
    // Keep full name if it's short or has a title
    if (!displayName || displayName.length <= 10) return displayName;
    const titlePrefixes = ['Professor', 'Headmaster', 'Sir', 'Madam', 'Lord', 'Lady', 'Mr', 'Mrs', 'Ms'];
    if (titlePrefixes.some(prefix => displayName.startsWith(prefix))) return displayName;
    // Use first name only
    const firstName = displayName.split(' ')[0];
    return firstName || displayName;
}

function getUniqueNPCsFromHistory(history) {
    const npcIds = new Set();
    for (const entry of history) {
        // Add speaker if named NPC
        const voiceName = entry.voiceName || '';
        if (voiceName && isNamedNPC(voiceName)) {
            npcIds.add(voiceName);
        }
        // Add all earshot witnesses
        if (Array.isArray(entry.earshot)) {
            for (const npcId of entry.earshot) {
                if (isNamedNPC(npcId)) {
                    npcIds.add(npcId);
                }
            }
        }
    }
    return Array.from(npcIds).sort((a, b) =>
        prettifyVoiceName(a).localeCompare(prettifyVoiceName(b))
    );
}

function populatePerspectiveDropdown(history) {
    const select = document.getElementById('historyPerspective');
    if (!select) return;
    const currentValue = select.value;

    // Clear existing options except "All"
    select.innerHTML = '<option value="all">All (default)</option>';

    // Get unique NPCs and add as options
    const npcIds = getUniqueNPCsFromHistory(history);
    for (const npcId of npcIds) {
        const option = document.createElement('option');
        option.value = npcId;
        option.textContent = prettifyVoiceName(npcId);
        select.appendChild(option);
    }

    // Restore selection if still valid
    if (currentValue && [...select.options].some(o => o.value === currentValue)) {
        select.value = currentValue;
    } else {
        select.value = 'all';
        filteredHistory = null;
    }

    // Update clear button visibility
    updateClearNpcButton();
}

async function filterHistoryByPerspective(resetPage = true) {
    const npcId = document.getElementById('historyPerspective').value;

    if (npcId === 'all') {
        filteredHistory = null;
        npcChapters = null;
    } else {
        filteredHistory = allHistory.filter(entry => {
            // NPC was the speaker
            if (entry.voiceName === npcId) return true;
            // NPC was in earshot
            if (Array.isArray(entry.earshot) && entry.earshot.includes(npcId)) return true;
            // Legacy entry (no earshot field) - include for backwards compat
            if (!('earshot' in entry)) return true;
            return false;
        });

        // Fetch chapter data for this NPC (for displaying dividers)
        try {
            const response = await fetch(`/api/memories/chapters/${encodeURIComponent(npcId)}`);
            if (response.ok) {
                const data = await response.json();
                npcChapters = data;
            } else {
                npcChapters = null;
            }
        } catch (e) {
            npcChapters = null;
        }
    }

    // Update clear button
    updateClearNpcButton();

    // Re-render tables with filtered data
    if (resetPage) currentPage = 1;
    const historyToRender = filteredHistory || allHistory;
    const collapsed = collapseSpells(historyToRender);
    populateHistoryTable(collapsed.slice(-10).reverse(), true);
    renderAllHistory();
}

function updateClearNpcButton() {
    const npcId = document.getElementById('historyPerspective').value;
    const clearBtn = document.getElementById('clearNpcMemoryBtn');
    const migrateBtn = document.getElementById('migrateNpcBtn');

    if (npcId === 'all') {
        if (clearBtn) clearBtn.style.display = 'none';
        if (migrateBtn) migrateBtn.style.display = 'none';
    } else {
        const displayName = prettifyVoiceName(npcId);
        const shortName = shortenNameForButton(displayName);
        if (clearBtn) {
            clearBtn.style.display = 'inline-block';
            clearBtn.textContent = `Clear ${shortName}'s Dialogue`;
        }
        if (migrateBtn) {
            // Only show migrate button if:
            // 1. Memory is enabled
            // 2. NPC has NOT been migrated yet (no chapters)
            const memoryEnabled = config.memory?.enabled === true;
            const hasChapters = npcChapters && (npcChapters.closed_chapters?.length > 0 || npcChapters.open_chapter);

            if (memoryEnabled && !hasChapters) {
                migrateBtn.style.display = 'inline-block';
                migrateBtn.textContent = `Migrate ${shortName}`;
            } else {
                migrateBtn.style.display = 'none';
            }
        }
    }
}

async function clearNpcMemory() {
    const npcId = document.getElementById('historyPerspective').value;
    if (npcId === 'all') return;

    const displayName = prettifyVoiceName(npcId);
    const confirmed = confirm(
        `This will remove ${displayName} from all conversation memories.\n\n` +
        `• ${displayName} will be removed from all earshot witness lists\n` +
        `• Entries only witnessed by ${displayName} will be deleted\n` +
        `• Entries where ${displayName} spoke will be deleted\n\n` +
        `This cannot be undone. Continue?`
    );
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/dialogue-history/clear-npc/${encodeURIComponent(npcId)}`, {
            method: 'DELETE'
        });
        if (!response.ok) throw new Error('Failed to clear dialogue');

        const result = await response.json();
        showToast(`Cleared ${displayName}'s dialogue (${result.entries_removed} entries removed)`, 'success');

        // Reset to "All" and refresh
        document.getElementById('historyPerspective').value = 'all';
        filteredHistory = null;
        await loadDialogueHistory();
    } catch (error) {
        console.error('Error clearing NPC dialogue:', error);
        showToast('Error clearing dialogue', 'error');
    }
}

async function migrateNpcMemory() {
    const npcId = document.getElementById('historyPerspective').value;
    if (npcId === 'all') return;

    const displayName = prettifyVoiceName(npcId);
    const migrateBtn = document.getElementById('migrateNpcBtn');
    const originalText = migrateBtn?.textContent || 'Migrate';

    const confirmed = confirm(
        `Migrate ${displayName}'s dialogue history to memory?\n\n` +
        `This will:\n` +
        `• Analyze their conversation history\n` +
        `• Create chapters from the dialogue\n` +
        `• Add episodes to the knowledge graph\n\n` +
        `This may take a while depending on history size.`
    );
    if (!confirmed) return;

    if (migrateBtn) {
        migrateBtn.disabled = true;
        migrateBtn.textContent = 'Starting...';
    }

    // Use SSE for real-time progress updates
    const eventSource = new EventSource(`/api/memories/migrate/${encodeURIComponent(npcId)}/stream`);

    eventSource.onmessage = async (event) => {
        try {
            const data = JSON.parse(event.data);

            switch (data.type) {
                case 'start':
                    if (migrateBtn) migrateBtn.textContent = 'Analyzing...';
                    break;

                case 'progress':
                    if (migrateBtn) {
                        // Show abbreviated progress message
                        const msg = data.message || '';
                        if (msg.includes('Detecting')) {
                            migrateBtn.textContent = 'Detecting...';
                        } else if (msg.includes('Generating')) {
                            migrateBtn.textContent = 'Generating...';
                        } else if (msg.includes('Indexing')) {
                            migrateBtn.textContent = 'Indexing...';
                        } else {
                            migrateBtn.textContent = 'Processing...';
                        }
                    }
                    break;

                case 'complete':
                    eventSource.close();
                    if (migrateBtn) {
                        migrateBtn.disabled = false;
                        migrateBtn.textContent = originalText;
                    }
                    if (data.skipped) {
                        showToast(`${displayName} already migrated - skipped`, 'info');
                    } else {
                        showToast(
                            `Migrated ${displayName}: ${data.chapters} chapters, ${data.episodes} episodes`,
                            'success'
                        );
                    }
                    // Refresh to update button visibility and memory UI
                    await filterHistoryByPerspective(false);
                    await refreshMemoryUI();
                    break;

                case 'error':
                    eventSource.close();
                    if (migrateBtn) {
                        migrateBtn.disabled = false;
                        migrateBtn.textContent = originalText;
                    }
                    showToast(`Migration failed: ${data.message}`, 'error');
                    break;
            }
        } catch (e) {
            console.error('Error parsing SSE event:', e);
        }
    };

    eventSource.onerror = (error) => {
        eventSource.close();
        if (migrateBtn) {
            migrateBtn.disabled = false;
            migrateBtn.textContent = originalText;
        }
        showToast('Migration failed - connection lost', 'error');
    };
}

async function loadDialogueHistory() {
    // Skip auto-refresh during edit mode to prevent disruption
    if (historyEditMode) return;
    if (historyLoadInFlight) return;
    historyLoadInFlight = true;
    try {
        const response = await fetchWithTimeout('/api/dialogue-history');
        if (response.ok) {
            allHistory = await response.json();

            // Populate NPC perspective dropdown
            populatePerspectiveDropdown(allHistory);

            // Apply current filter if any
            const npcId = document.getElementById('historyPerspective')?.value;
            if (npcId && npcId !== 'all') {
                filterHistoryByPerspective(false);  // Don't reset page on refresh
            } else {
                filteredHistory = null;
                // Collapse spells first (on chronological data), then reverse for display
                const collapsed = collapseSpells(allHistory);
                // Recent tab: last 10, newest first
                populateHistoryTable(collapsed.slice(-10).reverse(), true);
                // All History tab: paginated, newest first
                renderAllHistory();
            }
        }
    } catch (e) {
        // Silently ignore timeout/network errors during polling
    } finally {
        historyLoadInFlight = false;
    }
}

function formatEntryTime(entry) {
    const date = entry.gameDate || '';
    const time = entry.gameTime || '';
    if (date && time) return `${date} ${time}`;
    if (time) return time;
    if (date) return date;
    // Fallback to real-world time
    return new Date(entry.timestamp * 1000).toLocaleTimeString();
}

function collapseSpells(history) {
    // Collapse consecutive identical spell casts into single entries
    const collapsed = [];
    for (const entry of history) {
        if (entry.type !== 'spell') {
            collapsed.push(entry);
            continue;
        }

        const last = collapsed[collapsed.length - 1];
        if (last && last.type === 'spell' &&
            last.voiceName === entry.voiceName &&
            last.lineID === entry.lineID) {
            // Merge into existing entry
            last.count = (last.count || 1) + 1;
            if (!last.firstGameTime) {
                last.firstGameTime = last.gameTime;
                last.firstGameDate = last.gameDate;
            }
            last.lastGameTime = entry.gameTime;
            last.lastGameDate = entry.gameDate;
            last.gameTime = entry.gameTime;
            last.gameDate = entry.gameDate;
        } else {
            collapsed.push({ ...entry });
        }
    }
    return collapsed;
}

function formatSpellTime(entry) {
    // Format time for collapsed spell entries (shows range)
    if (entry.count > 1 && entry.firstGameTime) {
        const firstTime = entry.firstGameTime;
        const lastTime = entry.lastGameTime || entry.gameTime;
        const date = entry.firstGameDate || entry.gameDate || '';
        if (firstTime !== lastTime) {
            return date ? `${date} ${firstTime} - ${lastTime}` : `${firstTime} - ${lastTime}`;
        }
    }
    return formatEntryTime(entry);
}

function populateHistoryTable(history, alreadyCollapsed = false) {
    const table = document.querySelector('#historyRecent .history-table');
    const thead = table.querySelector('thead tr');
    const tbody = document.getElementById('historyTableBody');

    // Update header for edit mode
    if (historyEditMode) {
        if (!thead.querySelector('.history-checkbox-cell')) {
            thead.innerHTML = `
                        <th class="history-checkbox-cell"><input type="checkbox" class="history-checkbox history-select-all" onchange="this.checked ? selectAllHistoryEntries() : deselectAllHistoryEntries()" title="Select all"></th>
                        <th>Speaker</th>
                        <th>Text</th>
                        <th>Time</th>
                        <th class="history-delete-cell"></th>
                    `;
        }
    } else {
        thead.innerHTML = `
                    <th>Speaker</th>
                    <th>Text</th>
                    <th>Time</th>
                `;
    }

    tbody.innerHTML = '';

    // Collapse consecutive spell casts (skip if already done by caller)
    const collapsed = alreadyCollapsed ? history : collapseSpells(history);

    for (const entry of collapsed) {
        const row = document.createElement('tr');
        const timestamp = entry.timestamp;
        const timestampStr = String(timestamp);
        row.dataset.timestamp = timestampStr;

        let speaker = entry.speaker || entry.voiceName || 'Unknown';
        let text = entry.text || '...';
        let time;
        let rowClass = '';

        // Handle location transitions
        if (entry.type === 'location') {
            speaker = '→';
            text = `Entered ${entry.location || text.replace('Entered ', '')}`;
            time = formatEntryTime(entry);
            rowClass = 'history-row-location';
        }
        // Handle broom events
        else if (entry.type === 'broom') {
            speaker = '🧹';
            time = formatEntryTime(entry);
            rowClass = 'history-row-broom';
        }
        // Handle combat events
        else if (entry.type === 'combat') {
            speaker = '⚔️';
            time = formatSpellTime(entry);  // Uses first/last time range
            rowClass = 'history-row-combat';
        }
        // Handle collapsed spells
        else if (entry.type === 'spell' && entry.count > 1) {
            text = `${text} (${entry.count}x)`;
            time = formatSpellTime(entry);
        } else {
            time = formatEntryTime(entry);
        }

        const isSelected = selectedHistoryEntries.has(timestampStr);
        row.className = rowClass + (isSelected ? ' selected' : '');

        if (historyEditMode) {
            row.innerHTML = `
                        <td class="history-checkbox-cell"><input type="checkbox" class="history-checkbox" data-timestamp="${timestampStr}" ${isSelected ? 'checked' : ''} onchange="toggleHistoryEntrySelection('${timestampStr}', this)"></td>
                        <td class="history-speaker">${escapeHtml(speaker)}</td>
                        <td class="history-text">${escapeHtml(text)}</td>
                        <td class="history-time">${time}</td>
                        <td class="history-delete-cell"><button class="history-delete-btn" onclick="deleteSingleHistoryEntry('${timestampStr}')" title="Delete entry">&#10005;</button></td>
                    `;
        } else {
            row.innerHTML = `
                        <td class="history-speaker">${escapeHtml(speaker)}</td>
                        <td class="history-text">${escapeHtml(text)}</td>
                        <td class="history-time">${time}</td>
                    `;
        }
        tbody.appendChild(row);
    }

    if (collapsed.length === 0) {
        const colspan = historyEditMode ? 5 : 3;
        tbody.innerHTML = `<tr><td colspan="${colspan}" style="text-align:center;opacity:0.6;">No dialogue history yet</td></tr>`;
    }
}

function renderAllHistory() {
    // Use filtered history if set, otherwise all history
    const historyToRender = filteredHistory || allHistory;

    // Collapse consecutive spells first, then reverse for newest-first display
    const collapsed = collapseSpells(historyToRender);
    const reversed = [...collapsed].reverse(); // Newest first
    const totalPages = Math.ceil(reversed.length / ITEMS_PER_PAGE) || 1;

    // Clamp current page
    if (currentPage > totalPages) currentPage = totalPages;
    if (currentPage < 1) currentPage = 1;

    // Get page slice
    const start = (currentPage - 1) * ITEMS_PER_PAGE;
    const end = start + ITEMS_PER_PAGE;
    const pageData = reversed.slice(start, end);

    // Update count (show collapsed count, indicate if filtered)
    const countEl = document.getElementById('historyAllCount');
    if (filteredHistory) {
        const npcId = document.getElementById('historyPerspective')?.value;
        const npcName = prettifyVoiceName(npcId);
        countEl.textContent = `${collapsed.length} entries for ${npcName}`;
    } else {
        countEl.textContent = `${collapsed.length} entries total (${allHistory.length} raw)`;
    }

    // Update header for edit mode
    const table = document.querySelector('#historyAll .history-table');
    const thead = table.querySelector('thead tr');
    if (historyEditMode) {
        if (!thead.querySelector('.history-checkbox-cell')) {
            thead.innerHTML = `
                        <th class="history-checkbox-cell"><input type="checkbox" class="history-checkbox history-select-all" onchange="this.checked ? selectAllHistoryEntries() : deselectAllHistoryEntries()" title="Select all"></th>
                        <th>Speaker</th>
                        <th>Text</th>
                        <th>Time</th>
                        <th class="history-delete-cell"></th>
                    `;
        }
    } else {
        thead.innerHTML = `
                    <th>Speaker</th>
                    <th>Text</th>
                    <th>Time</th>
                `;
    }

    // Render table
    const tbody = document.getElementById('historyAllTableBody');
    tbody.innerHTML = '';

    // Helper to find which chapter an entry belongs to (by timestamp)
    function findChapterForEntry(entry) {
        if (!npcChapters || !filteredHistory) return null;
        const ts = entry.timestamp;
        if (!ts) return null;

        // Check closed chapters (by timestamp range)
        for (const ch of (npcChapters.closed_chapters || [])) {
            const startTs = ch.start_timestamp;
            const endTs = ch.end_timestamp;
            if (startTs && endTs && ts >= startTs && ts <= endTs) {
                return ch;
            }
        }
        // Check open chapter (any entry after start_timestamp)
        if (npcChapters.open_chapter) {
            const startTs = npcChapters.open_chapter.start_timestamp;
            if (startTs && ts >= startTs) {
                return { ...npcChapters.open_chapter, isOpen: true };
            }
        }
        return null;
    }

    // Helper to create chapter divider row
    function createChapterDivider(chapter) {
        const divider = document.createElement('tr');
        divider.className = 'history-row-chapter';
        const colspan = historyEditMode ? 5 : 3;
        const openBadge = chapter.isOpen ? ' <span style="opacity:0.6;font-size:10px;">(in progress)</span>' : '';
        divider.innerHTML = `
                    <td colspan="${colspan}">
                        <div style="display:flex;align-items:center;gap:8px;">
                            <span class="chapter-icon">📖</span>
                            <div>
                                <div class="chapter-title">${escapeHtml(chapter.title || 'Untitled Chapter')}${openBadge}</div>
                                ${chapter.summary ? `<div class="chapter-summary" title="${escapeHtml(chapter.summary)}">${escapeHtml(chapter.summary)}</div>` : ''}
                            </div>
                        </div>
                    </td>
                `;
        return divider;
    }

    let lastChapter = null;
    for (const entry of pageData) {
        // Check if we're entering a new chapter (insert divider before)
        const entryChapter = findChapterForEntry(entry);
        if (entryChapter && (!lastChapter || entryChapter.title !== lastChapter.title)) {
            tbody.appendChild(createChapterDivider(entryChapter));
            lastChapter = entryChapter;
        }

        const row = document.createElement('tr');
        const timestamp = entry.timestamp;
        const timestampStr = String(timestamp);
        row.dataset.timestamp = timestampStr;

        let speaker = entry.speaker || entry.voiceName || 'Unknown';
        let text = entry.text || '...';
        let time;
        let rowClass = '';

        // Handle location transitions
        if (entry.type === 'location') {
            speaker = '→';
            text = `Entered ${entry.location || text.replace('Entered ', '')}`;
            time = formatEntryTime(entry);
            rowClass = 'history-row-location';
        }
        // Handle broom events
        else if (entry.type === 'broom') {
            speaker = '🧹';
            time = formatEntryTime(entry);
            rowClass = 'history-row-broom';
        }
        // Handle combat events
        else if (entry.type === 'combat') {
            speaker = '⚔️';
            time = formatSpellTime(entry);  // Uses first/last time range
            rowClass = 'history-row-combat';
        }
        // Handle collapsed spells
        else if (entry.type === 'spell' && entry.count > 1) {
            text = `${text} (${entry.count}x)`;
            time = formatSpellTime(entry);
        } else {
            time = formatEntryTime(entry);
        }

        const isSelected = selectedHistoryEntries.has(timestampStr);
        row.className = rowClass + (isSelected ? ' selected' : '');

        if (historyEditMode) {
            row.innerHTML = `
                        <td class="history-checkbox-cell"><input type="checkbox" class="history-checkbox" data-timestamp="${timestampStr}" ${isSelected ? 'checked' : ''} onchange="toggleHistoryEntrySelection('${timestampStr}', this)"></td>
                        <td class="history-speaker">${escapeHtml(speaker)}</td>
                        <td class="history-text">${escapeHtml(text)}</td>
                        <td class="history-time">${time}</td>
                        <td class="history-delete-cell"><button class="history-delete-btn" onclick="deleteSingleHistoryEntry('${timestampStr}')" title="Delete entry">&#10005;</button></td>
                    `;
        } else {
            row.innerHTML = `
                        <td class="history-speaker">${escapeHtml(speaker)}</td>
                        <td class="history-text">${escapeHtml(text)}</td>
                        <td class="history-time">${time}</td>
                    `;
        }
        tbody.appendChild(row);
    }

    if (pageData.length === 0) {
        const colspan = historyEditMode ? 5 : 3;
        tbody.innerHTML = `<tr><td colspan="${colspan}" style="text-align:center;opacity:0.6;">No dialogue history yet</td></tr>`;
    }

    // Render pagination
    renderPagination(totalPages);
}

function renderPagination(totalPages) {
    const container = document.getElementById('historyPagination');
    container.innerHTML = '';

    if (totalPages <= 1) return;

    // Previous button
    const prevBtn = document.createElement('button');
    prevBtn.className = 'pagination-btn';
    prevBtn.innerHTML = '&laquo;';
    prevBtn.disabled = currentPage === 1;
    prevBtn.onclick = () => { currentPage--; renderAllHistory(); };
    container.appendChild(prevBtn);

    // Page numbers with ellipsis
    const pages = getPaginationRange(currentPage, totalPages);
    for (const page of pages) {
        if (page === '...') {
            const ellipsis = document.createElement('span');
            ellipsis.className = 'pagination-ellipsis';
            ellipsis.textContent = '...';
            container.appendChild(ellipsis);
        } else {
            const btn = document.createElement('button');
            btn.className = 'pagination-btn' + (page === currentPage ? ' active' : '');
            btn.textContent = page;
            btn.onclick = () => { currentPage = page; renderAllHistory(); };
            container.appendChild(btn);
        }
    }

    // Next button
    const nextBtn = document.createElement('button');
    nextBtn.className = 'pagination-btn';
    nextBtn.innerHTML = '&raquo;';
    nextBtn.disabled = currentPage === totalPages;
    nextBtn.onclick = () => { currentPage++; renderAllHistory(); };
    container.appendChild(nextBtn);
}

function getPaginationRange(current, total) {
    if (total <= 7) {
        return Array.from({ length: total }, (_, i) => i + 1);
    }

    if (current <= 3) {
        return [1, 2, 3, 4, '...', total];
    }

    if (current >= total - 2) {
        return [1, '...', total - 3, total - 2, total - 1, total];
    }

    return [1, '...', current - 1, current, current + 1, '...', total];
}

// UI Helpers
function _resizeChapterTextareas(chapter) {
    const textareas = chapter.querySelectorAll('textarea');
    if (textareas.length > 0) {
        requestAnimationFrame(() => {
            textareas.forEach(textarea => {
                textarea.style.height = 'auto';
                textarea.style.height = textarea.scrollHeight + 'px';
            });
        });
    }
}

function toggleChapter(id) {
    const chapter = document.getElementById(id);
    chapter.classList.toggle('collapsed');

    if (!chapter.classList.contains('collapsed')) {
        _resizeChapterTextareas(chapter);
    }
}

function expandChapter(id) {
    const chapter = document.getElementById(id);
    const wasCollapsed = chapter.classList.contains('collapsed');
    chapter.classList.remove('collapsed');

    if (wasCollapsed) {
        _resizeChapterTextareas(chapter);
    }
}

function toggleSubPanel(el) {
    const panel = el.closest('.sub-panel');
    panel.classList.toggle('collapsed');

    // Resize textareas after expanding (they have zero height when hidden)
    if (!panel.classList.contains('collapsed')) {
        // Find textareas in this panel and resize them directly
        const textareas = panel.querySelectorAll('textarea');
        if (textareas.length > 0) {
            // Use requestAnimationFrame to ensure DOM has updated
            requestAnimationFrame(() => {
                textareas.forEach(textarea => {
                    // Force recalculation by resetting height
                    textarea.style.height = 'auto';
                    textarea.style.height = textarea.scrollHeight + 'px';
                });
            });
        }
    }
}

function scrollToSection(id) {
    const section = document.getElementById(id);
    if (section) {
        // Expand the section first
        section.classList.remove('collapsed');
        // Scroll to section with smooth behavior
        section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

function switchTab(event, tabId) {
    // Update tab buttons
    const tabs = event.target.parentElement;
    tabs.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    event.target.classList.add('active');

    // Update tab content
    const parent = tabs.parentElement;
    parent.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');

    // Initialize commitment create form when its tab is opened
    if (tabId === 'commitmentsCreate') {
        populateCommitmentCreateNpcDropdown();
        initCommitmentDatePickers();
    }
}

function updateRangeValue(id, value) {
    const el = document.getElementById(id + 'Value');
    if (el) el.textContent = value;
}

function updateSetting(path, value) {
    // Update nested config object
    const parts = path.split('.');
    let obj = config;
    for (let i = 0; i < parts.length - 1; i++) {
        if (!obj[parts[i]]) obj[parts[i]] = {};
        obj = obj[parts[i]];
    }
    obj[parts[parts.length - 1]] = value;
    markDirty();
}

async function updateDevMode(enabled) {
    // Update config and save immediately (dev mode is a live setting)
    updateSetting('dev.enabled', enabled);
    await saveSettings();
    // Note: saveSettings() in Python auto-syncs to Lua when dev mode changes
}

function markDirty() {
    if (isInitializing) return;  // Skip during initial page load
    dirty = true;
    document.getElementById('saveText').textContent = 'Save Configuration *';
}

function validateModelNames() {
    const provider = config.llm?.provider || 'gemini';
    const errors = [];

    const modelFields = [
        { path: 'conversation.chat_model', label: 'Chat Model' },
        { path: 'conversation.target_selection_model', label: 'Target Selection Model' },
        { path: 'conversation.interjection_model', label: 'Interjection Model' },
        { path: 'conversation.input_correction_model', label: 'Input Correction Model' },
        { path: 'memory.chapter_model', label: 'Chapter Detection Model' },
        { path: 'memory.prose_model', label: 'Memory Prose Model' },
        { path: 'memory.graphiti_model', label: 'Graphiti Model (Main)' },
        { path: 'memory.graphiti_small_model', label: 'Graphiti Model (Small)' },
        { path: 'memory.reranker_model', label: 'Reranker Model' },
        { path: 'agents.vision.llm.model', label: 'Vision Agent Model' }
    ];

    for (const field of modelFields) {
        // Navigate nested path to get model value
        const parts = field.path.split('.');
        let value = config;
        for (const part of parts) {
            value = value?.[part];
            if (value === undefined) break;
        }

        // Skip if field is empty or undefined
        if (!value || typeof value !== 'string') continue;

        const modelName = value.trim();
        if (!modelName) continue;

        const hasSlash = modelName.includes('/');

        // OpenRouter: MUST have provider prefix (e.g., "openai/gpt-4")
        if (provider === 'openrouter' && !hasSlash) {
            errors.push(`${field.label}: "${modelName}" is invalid for OpenRouter. Model must include provider prefix (e.g., "openai/gpt-4", "anthropic/claude-3.5-sonnet")`);
        }

        // Gemini: MUST NOT have provider prefix (e.g., "gemini-2.0-flash", not "google/gemini-2.0-flash")
        if (provider === 'gemini' && hasSlash) {
            errors.push(`${field.label}: "${modelName}" is invalid for Gemini. Model must NOT include provider prefix (e.g., "gemini-2.0-flash", not "google/gemini-2.0-flash")`);
        }
    }

    return errors;
}

async function saveSettings() {
    const btn = document.querySelector('.btn-primary');
    btn.classList.add('loading');
    document.getElementById('saveText').innerHTML = '<span class="spinner"></span> Saving...';

    // Ensure latest API key text is captured even if the field never blurred.
    const llmKeyField = document.getElementById('llmApiKey');
    if (llmKeyField) {
        const rawKey = llmKeyField.value;
        const trimmedKey = typeof rawKey === 'string' ? rawKey.trim() : rawKey;
        if (trimmedKey && trimmedKey !== '********') {
            updateLLMApiKey(trimmedKey);
        }
    }

    // Validate provider fields (API keys, etc.)
    const providerErrors = validateActiveProviderFields();
    if (providerErrors.length > 0) {
        btn.classList.remove('loading');
        document.getElementById('saveText').textContent = 'Save Configuration';
        showToast(providerErrors[0], 'error');
        return;
    }

    // Validate model names against LLM provider
    const validationErrors = validateModelNames();
    if (validationErrors.length > 0) {
        btn.classList.remove('loading');
        document.getElementById('saveText').textContent = 'Save Configuration';

        const errorMessage = 'Invalid model configuration:\n\n' + validationErrors.join('\n\n');
        showToast('Configuration validation failed', 'error');
        alert(errorMessage);
        return;
    }

    // Validate default prompt contains {name} placeholder (required for NPC identity)
    const defaultPrompt = document.getElementById('defaultPrompt').value;
    if (defaultPrompt && !defaultPrompt.includes('{name}')) {
        btn.classList.remove('loading');
        document.getElementById('saveText').textContent = 'Save Configuration';
        showToast('Character prompt must contain {name} — this is a universal prompt shared by all NPCs and needs {name} so each character gets their own identity', 'error');
        return;
    }

    // Collect character settings (editor guidance + viseme scales + tts temp modifiers)
    config.prompts = config.prompts || {};
    config.prompts.editor_guidance = {};
    config.lipsync = config.lipsync || {};
    config.lipsync.npc_scales = {};
    config.tts = config.tts || {};
    config.tts.npc_temp_modifiers = {};
    config.tts.npc_model_overrides = {};

    document.querySelectorAll('#bioList .character-card').forEach(card => {
        const isPlayer = card.classList.contains('player-card');
        const name = isPlayer ? 'Player' : (card.querySelector('.character-name-input')?.value.trim() || '');
        const guidance = card.querySelector('.character-guidance-input')?.value.trim() || '';
        const visemeScale = parseFloat(card.querySelector('.character-viseme-scale')?.value || '1.0');
        const ttsTempMod = parseFloat(card.querySelector('.character-tts-temp-mod')?.value || '0');
        const modelOverride = card.querySelector('.character-model-override')?.value.trim() || '';

        if (name) {
            // Save guidance if not empty
            if (guidance) {
                config.prompts.editor_guidance[name] = guidance;
            }
            // Save viseme scale if not default (1.0) and not Player
            if (!isPlayer && visemeScale !== 1.0) {
                config.lipsync.npc_scales[name] = visemeScale;
            }
            // Save TTS temp modifier if not default (0) and not Player
            if (!isPlayer && ttsTempMod !== 0) {
                config.tts.npc_temp_modifiers[name] = ttsTempMod;
            }
            // Save model override if not empty and not Player
            if (!isPlayer && modelOverride) {
                config.tts.npc_model_overrides[name] = modelOverride;
            }
        }
    });
    config.prompts.default = document.getElementById('defaultPrompt').value;
    config.prompts.world_lore = document.getElementById('worldLore').value;
    config.prompts.scene_continuation = document.getElementById('sceneContinuationPrompt').value;
    config.prompts.interjection_prompt_mode = document.getElementById('interjectionPromptMode').value;

    // Sync pronunciation replacements from textarea (in case onchange hasn't fired)
    const pronEl = document.getElementById('pronunciationReplacements');
    if (pronEl) {
        parsePronunciationReplacements(pronEl.value);
    }

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });

        if (response.ok) {
            dirty = false;
            showToast('Configuration saved successfully', 'success');
            document.getElementById('saveText').textContent = 'Save Configuration';
        } else {
            throw new Error('Save failed');
        }
    } catch (e) {
        const isOffline = e instanceof TypeError || e.message === 'Failed to fetch';
        const msg = isOffline
            ? 'Failed to save — server is not running. Make sure the game is open.'
            : 'Failed to save configuration';
        showToast(msg, 'error');
        document.getElementById('saveText').textContent = 'Save Configuration *';
    }

    btn.classList.remove('loading');
}

const DEFAULT_PRONUNCIATION_REPLACEMENTS = {
    'Accio': 'Ackeeyoh|/ˈæk.i.oʊ/',
    'O.W.L.s': 'Owls',
    'Stupefy': '/ˈstuː.pɪ.faɪ/',
    'Legilimens': 'Lehjillihmenz|/lɛˈdʒɪl.ɪ.mɛnz/',
    'Crucio': 'Kroosheeoh|/ˈkruː.ʃi.oʊ/',
    'Levioso': 'Leveeohso|/ˌlɛv.iˈoʊ.soʊ/',
    'Alohomora': '/ˌæl.oʊ.hoʊˈmɔːr.ə/',
    'Petrificus Totalus': '/pɛˈtrɪf.ɪ.kəs toʊˈtæl.əs/',
    'Ominis': '/ˈɑː.mɪ.nɪs/',
    'Natsai': 'Notsigh|/ˈnɑːt.saɪ/',
    'Onai': 'Ohnigh|/oʊˈnaɪ/',
    'Ranrok': 'Ran-rock|/ˈræn.rɒk/',
    'Morganach': 'Morganakh|/ˈmɔːr.ɡən.ɑːx/',
    'Mandrake': 'Man-drayk|/ˈmæn.dreɪk/',
    'Mandrakes': 'Man-drayks|/ˈmæn.dreɪks/',
    'Protego': 'Protaygo|/proʊˈteɪ.ɡoʊ/',
    'Revelio': 'Rehvellio|/rɛˈvɛl.i.oʊ/',
    'Glacius': 'Glaessyus|/ˈɡleɪ.si.əs/',
    'Incendio': 'Insendio|/ɪnˈsɛn.di.oʊ/',
    'Lumos': 'Lewmoesse|/ˈluː.moʊs/',
    'you wound me': 'you woond me|you /wuːnd/ me',
    'lead the way': 'leed the way|/liːd/ the way',
};

function resetPronunciationReplacements() {
    const el = document.getElementById('pronunciationReplacements');
    if (el) {
        el.value = pronunciationReplacementsToText(DEFAULT_PRONUNCIATION_REPLACEMENTS);
        parsePronunciationReplacements(el.value);
        markDirty();
    }
}

function parsePronunciationReplacements(text) {
    const replacements = {};
    text.split('\n').forEach(line => {
        line = line.trim();
        if (!line || line.startsWith('#')) return;
        const colonIdx = line.indexOf(':');
        if (colonIdx <= 0) return;
        const word = line.substring(0, colonIdx).trim();
        const replacement = line.substring(colonIdx + 1).trim();
        if (word && replacement) {
            replacements[word] = replacement;
        }
    });
    updateSetting('audio.pronunciation_replacements', replacements);
}

function pronunciationReplacementsToText(replacements) {
    if (!replacements || typeof replacements !== 'object') return '';
    return Object.entries(replacements)
        .map(([word, replacement]) => `${word}:${replacement}`)
        .join('\n');
}

function resetToDefaults() {
    if (confirm('Reset all settings to defaults? This cannot be undone.')) {
        fetch('/api/config/reset', { method: 'POST' })
            .then(() => loadConfig())
            .then(() => showToast('Settings reset to defaults', 'success'))
            .catch(() => showToast('Reset failed', 'error'));
    }
}

function validateDefaultPromptPlaceholders() {
    const textarea = document.getElementById('defaultPrompt');
    const warning = document.getElementById('defaultPromptWarning');
    if (!textarea || !warning) return;
    warning.style.display = textarea.value.includes('{name}') ? 'none' : 'block';
}

async function resetDefaultPrompt() {
    try {
        const response = await fetch('/api/config/defaults/prompt');
        const data = await response.json();
        const textarea = document.getElementById('defaultPrompt');
        textarea.value = data.prompt;
        updateSetting('prompts.default', data.prompt);
        validateDefaultPromptPlaceholders();
        showToast('Character prompt reset', 'success');
    } catch (e) {
        showToast('Failed to reset prompt', 'error');
    }
}

async function resetSceneContinuationPrompt() {
    try {
        const response = await fetch('/api/config/defaults/scene-continuation-prompt');
        const data = await response.json();
        const textarea = document.getElementById('sceneContinuationPrompt');
        textarea.value = data.prompt;
        updateSetting('prompts.scene_continuation', data.prompt);
        showToast('Scene continuation prompt reset', 'success');
    } catch (e) {
        showToast('Failed to reset prompt', 'error');
    }
}

async function resetInterjectionPromptMode() {
    try {
        const response = await fetch('/api/config/defaults/interjection-prompt-mode');
        const data = await response.json();
        const textarea = document.getElementById('interjectionPromptMode');
        textarea.value = data.prompt;
        updateSetting('prompts.interjection_prompt_mode', data.prompt);
        showToast('Director mode prompt reset', 'success');
    } catch (e) {
        showToast('Failed to reset prompt', 'error');
    }
}

function exportHistory() {
    const npcId = document.getElementById('historyPerspective').value;

    if (npcId === 'all') {
        // Export all dialogue
        window.open('/api/dialogue-history/export', '_blank');
    } else {
        // Export specific NPC's dialogue
        window.open(`/api/dialogue-history/export/${encodeURIComponent(npcId)}`, '_blank');
    }
}

function clearHistory() {
    if (confirm('Clear all dialogue history? This cannot be undone.')) {
        fetch('/api/dialogue-history', { method: 'DELETE' })
            .then(() => {
                currentPage = 1;
                loadDialogueHistory();
                showToast('History cleared', 'success');
            })
            .catch(() => showToast('Clear failed', 'error'));
    }
}

function clearHistoryWithConfirm() {
    const message = 'Are you sure you want to clear all dialogue history?\n\n' +
        'This will erase the AI\'s memory of all past conversations with NPCs. ' +
        'Characters will no longer remember what you\'ve discussed.\n\n' +
        'Consider using Export first to create a backup.\n\n' +
        'This action cannot be undone.';
    if (confirm(message)) {
        fetch('/api/dialogue-history', { method: 'DELETE' })
            .then(() => {
                currentPage = 1;
                loadDialogueHistory();
                showToast('Dialogue history cleared', 'success');
            })
            .catch(() => showToast('Clear failed', 'error'));
    }
}

async function clearAllMemories() {
    const message = 'Are you sure you want to clear all NPC long-term memories?\n\n' +
        'This will delete the knowledge graph and all chapter data. ' +
        'NPCs will lose their persistent memories of past adventures.\n\n' +
        'This action cannot be undone.';
    if (confirm(message)) {
        try {
            const res = await fetch('/api/memories', { method: 'DELETE' });
            const data = await res.json();
            if (data.success) {
                showToast('All NPC memories cleared', 'success');
                // Refresh migration status
                await loadMigrationStatus();
            } else {
                showToast(data.error || 'Clear failed', 'error');
            }
        } catch (e) {
            showToast('Clear failed', 'error');
        }
    }
}

async function refreshMemoryUI() {
    /**
     * Refresh all memory-related UI after migration completes.
     * - Updates migration status count
     * - Refreshes knowledge graph explorer NPC list
     * - Reloads current graph if one is displayed
     * - Refreshes character bios with newly generated data
     */

    // Refresh migration status count
    await loadMigrationStatus();

    // Refresh knowledge graph explorer (if loaded)
    if (typeof refreshNpcList === 'function') {
        await refreshNpcList();
    }

    // Reload current graph if one is displayed
    const currentNpcSelect = document.getElementById('npcSelect');
    if (currentNpcSelect && currentNpcSelect.value && typeof loadNpcGraph === 'function') {
        await loadNpcGraph();
    }

    // Refresh character bios (reload generated bios for all cards)
    const memoryEnabled = config?.memory?.enabled || false;
    if (memoryEnabled) {
        const cards = document.querySelectorAll('.character-card:not(.player-card)');
        for (const card of cards) {
            const npcId = card.dataset.npcId;
            if (npcId) {
                await loadGeneratedBio(npcId, card);
            }
        }
    }
}

async function loadMigrationStatus() {
    const countEl = document.getElementById('migratePendingCount');
    if (!countEl) return;

    try {
        const response = await fetch('/api/memories/migration-status');
        const data = await response.json();

        if (data.success) {
            const { pending_count, migrated_count, total_npcs, min_entries_threshold } = data;
            if (pending_count > 0) {
                countEl.textContent = `${pending_count} NPC${pending_count !== 1 ? 's' : ''} pending migration (${migrated_count}/${total_npcs} already migrated, ${min_entries_threshold}+ entries required)`;
                countEl.style.color = 'var(--gold-dark)';
            } else if (total_npcs > 0) {
                countEl.textContent = `All ${total_npcs} NPCs already migrated`;
                countEl.style.color = 'var(--success)';
            } else {
                countEl.textContent = `No NPCs with sufficient dialogue history found (minimum ${min_entries_threshold} entries required)`;
                countEl.style.color = 'var(--text-secondary)';
            }
        } else {
            countEl.textContent = '';
        }
    } catch (e) {
        console.error('Error loading migration status:', e);
        countEl.textContent = '';
    }
}

async function migrateMemories() {
    const btn = document.getElementById('migrateBtn');
    const status = document.getElementById('migrateStatus');

    const message = 'This will process all existing dialogue history into chapters and add them to the knowledge graph.\n\n' +
        'This may take a while for large histories (multiple LLM calls per NPC).\n\n' +
        'Continue?';

    if (!confirm(message)) return;

    btn.disabled = true;
    btn.textContent = 'Migrating...';
    status.style.display = 'block';
    status.textContent = 'Starting migration...';

    // Use SSE for real-time progress updates
    const eventSource = new EventSource('/api/memories/migrate/stream');
    let processed = 0, skipped = 0, totalChapters = 0;

    eventSource.onmessage = async (event) => {
        try {
            const data = JSON.parse(event.data);

            switch (data.type) {
                case 'start':
                    status.textContent = `Found ${data.total_npcs} NPCs to process...`;
                    break;

                case 'npc_start':
                    status.textContent = `[${data.index}/${data.total}] Processing ${data.npc_name}...`;
                    break;

                case 'npc_done':
                    if (data.skipped) {
                        skipped++;
                        status.textContent = `[${data.index || ''}] ${data.npc_name}: skipped (already migrated)`;
                    } else if (data.error) {
                        status.textContent = `[${data.index || ''}] ${data.npc_name}: error - ${data.error}`;
                    } else {
                        processed++;
                        totalChapters += data.chapters || 0;
                        status.textContent = `[${data.index || ''}] ${data.npc_name}: ${data.chapters} chapters added`;
                    }
                    break;

                case 'complete':
                    eventSource.close();
                    const skippedMsg = data.skipped > 0 ? ` (${data.skipped} already migrated, skipped)` : '';
                    status.textContent = `Complete: ${data.processed} NPCs, ${data.chapters} chapters, ${data.episodes} episodes${skippedMsg}`;
                    showToast(`Migration complete: ${data.chapters} chapters created`, 'success');
                    btn.disabled = false;
                    btn.textContent = 'Migrate All NPCs';
                    await refreshMemoryUI();
                    break;

                case 'error':
                    eventSource.close();
                    status.textContent = `Error: ${data.message}`;
                    showToast(data.message || 'Migration failed', 'error');
                    btn.disabled = false;
                    btn.textContent = 'Migrate All NPCs';
                    break;
            }
        } catch (e) {
            console.error('Error parsing SSE event:', e);
        }
    };

    eventSource.onerror = (error) => {
        eventSource.close();
        status.textContent = 'Migration connection lost';
        showToast('Migration failed - connection lost', 'error');
        btn.disabled = false;
        btn.textContent = 'Migrate All NPCs';
    };
}

// Knowledge Graph Explorer Functions - see /js/graph.js

// ============================================
// History Edit Mode Functions
// ============================================
function toggleHistoryEditMode() {
    historyEditMode = !historyEditMode;
    const chapterContent = document.querySelector('#chapterHistory .chapter-content');
    const editBtn = document.getElementById('historyEditBtn');
    const editBar = document.getElementById('historyEditBar');

    if (historyEditMode) {
        chapterContent.classList.add('history-edit-mode');
        editBtn.textContent = 'Cancel';
        editBtn.classList.remove('btn-secondary');
        editBtn.classList.add('btn-warning');
        editBar.classList.add('active');
    } else {
        chapterContent.classList.remove('history-edit-mode');
        editBtn.textContent = 'Edit';
        editBtn.classList.remove('btn-warning');
        editBtn.classList.add('btn-secondary');
        editBar.classList.remove('active');
        selectedHistoryEntries.clear();
        // Refresh data when exiting edit mode
        loadDialogueHistory();
    }
    // Re-render tables to show/hide checkboxes (respect NPC filter)
    const historyToRender = filteredHistory || allHistory;
    const collapsed = collapseSpells(historyToRender);
    populateHistoryTable(collapsed.slice(-10).reverse(), true);
    renderAllHistory();
    updateHistorySelectionUI();
}

function toggleHistoryEntrySelection(timestampStr, checkbox) {
    if (checkbox.checked) {
        selectedHistoryEntries.add(timestampStr);
    } else {
        selectedHistoryEntries.delete(timestampStr);
    }
    updateHistorySelectionUI();
    updateRowSelectionState(timestampStr, checkbox.checked);
}

function updateRowSelectionState(timestampStr, isSelected) {
    // Update row visual state
    document.querySelectorAll(`tr[data-timestamp="${timestampStr}"]`).forEach(row => {
        if (isSelected) {
            row.classList.add('selected');
        } else {
            row.classList.remove('selected');
        }
    });
}

function selectAllHistoryEntries() {
    // Select all visible entries in current view
    const activeTab = document.querySelector('.tab-content.active');
    const tbody = activeTab.querySelector('tbody');
    const checkboxes = tbody.querySelectorAll('.history-checkbox');

    checkboxes.forEach(cb => {
        const timestampStr = cb.dataset.timestamp;
        if (timestampStr) {
            selectedHistoryEntries.add(timestampStr);
            cb.checked = true;
            cb.closest('tr').classList.add('selected');
        }
    });
    updateHistorySelectionUI();
}

function deselectAllHistoryEntries() {
    selectedHistoryEntries.clear();
    document.querySelectorAll('.history-checkbox').forEach(cb => {
        cb.checked = false;
        cb.closest('tr').classList.remove('selected');
    });
    updateHistorySelectionUI();
}

function updateHistorySelectionUI() {
    const count = selectedHistoryEntries.size;
    document.getElementById('historySelectedCount').textContent = count;
    document.getElementById('deleteSelectedBtn').disabled = count === 0;

    // Update select-all checkboxes
    document.querySelectorAll('.history-select-all').forEach(cb => {
        const tbody = cb.closest('table').querySelector('tbody');
        const rowCheckboxes = tbody.querySelectorAll('.history-checkbox');
        const allChecked = rowCheckboxes.length > 0 &&
            Array.from(rowCheckboxes).every(c => c.checked);
        cb.checked = allChecked;
    });
}

async function deleteSelectedHistoryEntries() {
    const count = selectedHistoryEntries.size;
    if (count === 0) return;

    const message = count === 1
        ? 'Delete this entry? This cannot be undone.'
        : `Delete ${count} entries? This cannot be undone.`;

    if (!confirm(message)) return;

    try {
        // Convert string timestamps to numbers for API
        const timestamps = Array.from(selectedHistoryEntries).map(s => parseFloat(s));
        const response = await fetch('/api/dialogue-history/entries', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ timestamps })
        });

        if (response.ok) {
            const result = await response.json();
            // Remove from local data (compare as strings for precision)
            allHistory = allHistory.filter(e => !selectedHistoryEntries.has(String(e.timestamp)));
            if (filteredHistory) {
                filteredHistory = filteredHistory.filter(e => !selectedHistoryEntries.has(String(e.timestamp)));
            }
            selectedHistoryEntries.clear();

            // Re-render (respect NPC filter)
            const historyToRender = filteredHistory || allHistory;
            const collapsed = collapseSpells(historyToRender);
            populateHistoryTable(collapsed.slice(-10).reverse(), true);
            renderAllHistory();
            updateHistorySelectionUI();

            showToast(`Deleted ${result.deleted} ${result.deleted === 1 ? 'entry' : 'entries'}`, 'success');
        } else {
            showToast('Delete failed', 'error');
        }
    } catch (e) {
        showToast('Delete failed: ' + e.message, 'error');
    }
}

async function deleteSingleHistoryEntry(timestampStr) {
    if (!confirm('Delete this entry? This cannot be undone.')) return;

    try {
        const timestamp = parseFloat(timestampStr);
        const response = await fetch('/api/dialogue-history/entries', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ timestamps: [timestamp] })
        });

        if (response.ok) {
            // Remove from local data (compare as strings for precision)
            allHistory = allHistory.filter(e => String(e.timestamp) !== timestampStr);
            if (filteredHistory) {
                filteredHistory = filteredHistory.filter(e => String(e.timestamp) !== timestampStr);
            }
            selectedHistoryEntries.delete(timestampStr);

            // Re-render (respect NPC filter)
            const historyToRender = filteredHistory || allHistory;
            const collapsed = collapseSpells(historyToRender);
            populateHistoryTable(collapsed.slice(-10).reverse(), true);
            renderAllHistory();
            updateHistorySelectionUI();

            showToast('Entry deleted', 'success');
        } else {
            showToast('Delete failed', 'error');
        }
    } catch (e) {
        showToast('Delete failed: ' + e.message, 'error');
    }
}

function importHistoryClick() {
    document.getElementById('historyImportInput').click();
}

function importHistory(input) {
    const file = input.files[0];
    if (!file) return;

    const npcId = document.getElementById('historyPerspective').value;

    const reader = new FileReader();
    reader.onload = (e) => {
        try {
            const data = JSON.parse(e.target.result);

            // Determine endpoint based on selection
            const endpoint = (npcId === 'all')
                ? '/api/dialogue-history/import'
                : `/api/dialogue-history/import/${encodeURIComponent(npcId)}`;

            fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            })
                .then(r => r.json())
                .then(result => {
                    if (result.error) {
                        showToast('Import failed: ' + result.error, 'error');
                    } else {
                        const msg = (npcId === 'all')
                            ? `Imported ${result.added} entries (${result.total} total)`
                            : `Imported ${result.added} entries for ${prettifyVoiceName(npcId)}`;
                        showToast(msg, 'success');
                        loadDialogueHistory();
                    }
                })
                .catch(() => showToast('Import failed', 'error'));
        } catch (err) {
            showToast('Invalid JSON file', 'error');
        }
    };
    reader.readAsText(file);
    input.value = '';
}

function exportCharacters() {
    window.open('/api/characters/export', '_blank');
}

function importCharactersClick() {
    document.getElementById('charactersImportInput').click();
}

function importCharacters(input) {
    const file = input.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (e) => {
        try {
            const data = JSON.parse(e.target.result);
            fetch('/api/characters/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            })
                .then(r => r.json())
                .then(result => {
                    if (result.error) {
                        showToast('Import failed: ' + result.error, 'error');
                    } else {
                        showToast(`Imported ${result.editor_guidance || 0} character guidance, ${result.viseme_scales || 0} viseme scales`, 'success');
                        loadConfig();
                    }
                })
                .catch(() => showToast('Import failed', 'error'));
        } catch (err) {
            showToast('Invalid JSON file', 'error');
        }
    };
    reader.readAsText(file);
    input.value = '';
}

// ============================================
// Commitments Section
// ============================================

let allCommitments = [];
let filteredCommitments = null;
let commitmentPage = 1;
const COMMITMENTS_PER_PAGE = 50;
let commitmentEditMode = false;
let selectedCommitments = new Set();
let commitmentLoadInFlight = false;
let cachedGameTime = null;
let cachedLocations = null;
let commitmentDatePicker = null;
let commitmentTimePicker = null;

async function loadCommitments() {
    if (commitmentEditMode) return;
    if (commitmentLoadInFlight) return;
    commitmentLoadInFlight = true;
    try {
        const response = await fetchWithTimeout('/api/commitments');
        if (response.ok) {
            allCommitments = await response.json();
            populateCommitmentNpcFilter();
            const npcId = document.getElementById('commitmentNpcFilter')?.value;
            if (npcId && npcId !== 'all') {
                filteredCommitments = allCommitments.filter(c => c.npc_id === npcId);
            } else {
                filteredCommitments = null;
            }
            renderCommitmentsRecent();
            renderCommitmentsAll();
        }
    } catch (e) {
        // Silently ignore during polling
    } finally {
        commitmentLoadInFlight = false;
    }
}

function populateCommitmentNpcFilter() {
    const select = document.getElementById('commitmentNpcFilter');
    if (!select) return;
    const currentVal = select.value;
    const npcIds = new Set();
    for (const c of allCommitments) {
        if (c.npc_id && isNamedNPC(c.npc_id)) npcIds.add(c.npc_id);
    }
    const sorted = Array.from(npcIds).sort((a, b) =>
        prettifyVoiceName(a).localeCompare(prettifyVoiceName(b))
    );

    // Preserve options from dialogue history NPCs for the create form
    select.innerHTML = '<option value="all">All NPCs</option>';
    for (const id of sorted) {
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = prettifyVoiceName(id);
        select.appendChild(opt);
    }
    select.value = currentVal || 'all';
}

function filterCommitmentsByNPC() {
    const npcId = document.getElementById('commitmentNpcFilter')?.value;
    if (npcId && npcId !== 'all') {
        filteredCommitments = allCommitments.filter(c => c.npc_id === npcId);
    } else {
        filteredCommitments = null;
    }
    commitmentPage = 1;
    renderCommitmentsRecent();
    renderCommitmentsAll();
}

function renderCommitmentsRecent() {
    const data = filteredCommitments || allCommitments;
    const tbody = document.getElementById('commitmentRecentBody');
    const emptyMsg = document.getElementById('commitmentRecentEmpty');
    if (!tbody) return;

    // Update header for edit mode
    const thead = tbody.closest('table')?.querySelector('thead tr');
    if (thead) {
        thead.innerHTML = commitmentEditMode
            ? '<th class="history-checkbox-cell"></th><th>NPC</th><th>Location</th><th>Time</th><th>Status</th><th class="history-delete-cell"></th>'
            : '<th>NPC</th><th>Location</th><th>Time</th><th>Status</th>';
    }

    const recent = data.slice(0, 10);
    tbody.innerHTML = '';

    if (recent.length === 0) {
        if (emptyMsg) emptyMsg.style.display = 'block';
        return;
    }
    if (emptyMsg) emptyMsg.style.display = 'none';

    for (const c of recent) {
        tbody.appendChild(createCommitmentRow(c));
    }
}

function renderCommitmentsAll() {
    const data = filteredCommitments || allCommitments;
    const tbody = document.getElementById('commitmentAllBody');
    const countEl = document.getElementById('commitmentAllCount');
    if (!tbody) return;

    const totalPages = Math.ceil(data.length / COMMITMENTS_PER_PAGE) || 1;
    if (commitmentPage > totalPages) commitmentPage = totalPages;
    if (commitmentPage < 1) commitmentPage = 1;

    const start = (commitmentPage - 1) * COMMITMENTS_PER_PAGE;
    const pageData = data.slice(start, start + COMMITMENTS_PER_PAGE);

    if (countEl) {
        const filterNpc = document.getElementById('commitmentNpcFilter')?.value;
        if (filterNpc && filterNpc !== 'all') {
            countEl.textContent = `${data.length} commitments for ${prettifyVoiceName(filterNpc)}`;
        } else {
            countEl.textContent = `${data.length} commitments total`;
        }
    }

    // Update header for edit mode
    const thead = tbody.closest('table')?.querySelector('thead tr');
    if (thead) {
        thead.innerHTML = commitmentEditMode
            ? '<th class="history-checkbox-cell"><input type="checkbox" class="history-checkbox" onchange="toggleAllCommitmentSelection(this)"></th><th>NPC</th><th>Location</th><th>Time</th><th>Status</th><th class="history-delete-cell"></th>'
            : '<th>NPC</th><th>Location</th><th>Time</th><th>Status</th>';
    }

    tbody.innerHTML = '';
    for (const c of pageData) {
        tbody.appendChild(createCommitmentRow(c));
    }

    renderCommitmentPagination(totalPages);
}

function createCommitmentRow(c) {
    const tr = document.createElement('tr');
    tr.className = `commitment-row-${c.status}`;
    tr.dataset.commitmentId = c.id;

    const isTerminal = ['completed', 'no_show', 'cancelled'].includes(c.status);
    const isEditable = !isTerminal && commitmentEditMode;

    if (commitmentEditMode) {
        // Checkbox cell
        const checkTd = document.createElement('td');
        checkTd.className = 'history-checkbox-cell';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'history-checkbox';
        cb.checked = selectedCommitments.has(c.id);
        cb.onchange = () => toggleCommitmentSelection(c.id, cb.checked);
        checkTd.appendChild(cb);
        tr.appendChild(checkTd);
    }

    // NPC
    const npcTd = document.createElement('td');
    npcTd.className = 'history-speaker';
    npcTd.textContent = prettifyVoiceName(c.npc_id);
    tr.appendChild(npcTd);

    // Location
    const locTd = document.createElement('td');
    locTd.className = 'commitment-location';
    locTd.textContent = c.location_display;
    tr.appendChild(locTd);

    // Time
    const timeTd = document.createElement('td');
    timeTd.className = 'commitment-time-cell';
    timeTd.textContent = formatCommitmentTime(c.game_time_start);
    tr.appendChild(timeTd);

    // Status
    const statusTd = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `commitment-status commitment-status-${c.status}`;
    badge.textContent = c.status === 'no_show' ? 'no show' : c.status;
    statusTd.appendChild(badge);
    tr.appendChild(statusTd);

    // Edit mode actions
    if (commitmentEditMode) {
        const actionTd = document.createElement('td');
        actionTd.className = 'history-delete-cell';
        if (isEditable) {
            const editBtn = document.createElement('button');
            editBtn.className = 'history-delete-btn';
            editBtn.title = 'Edit';
            editBtn.innerHTML = '&#9998;';
            editBtn.onclick = (e) => { e.stopPropagation(); startInlineEdit(c, tr); };
            actionTd.appendChild(editBtn);
        }
        const delBtn = document.createElement('button');
        delBtn.className = 'history-delete-btn';
        delBtn.title = 'Cancel/Delete';
        delBtn.innerHTML = '&#10005;';
        delBtn.onclick = (e) => { e.stopPropagation(); deleteSingleCommitment(c.id); };
        actionTd.appendChild(delBtn);
        tr.appendChild(actionTd);
    }

    return tr;
}

function formatCommitmentTime(timeStr) {
    // Convert 'YYYY/MM/DD HH:MM' to friendly display
    const m = timeStr.match(/(\d{4})\/(\d{2})\/(\d{2})\s+(\d{2}):(\d{2})/);
    if (!m) return timeStr;
    const [, year, month, day, hourStr, minStr] = m;
    const hour = parseInt(hourStr, 10);
    const minute = parseInt(minStr, 10);
    const period = hour < 12 ? 'AM' : 'PM';
    const h12 = hour % 12 || 12;
    return `${h12}:${minStr} ${period}, ${parseInt(month)}/${parseInt(day)}/${year}`;
}

function toggleCommitmentEditMode() {
    commitmentEditMode = !commitmentEditMode;
    const chapterContent = document.querySelector('#chapterCommitments .chapter-content');
    const editBtn = document.getElementById('commitmentEditBtn');
    const editBar = document.getElementById('commitmentEditBar');

    if (commitmentEditMode) {
        chapterContent.classList.add('history-edit-mode');
        editBtn.textContent = 'Cancel';
        editBtn.classList.remove('btn-secondary');
        editBtn.classList.add('btn-warning');
        editBar.classList.add('active');
    } else {
        chapterContent.classList.remove('history-edit-mode');
        editBtn.textContent = 'Edit';
        editBtn.classList.remove('btn-warning');
        editBtn.classList.add('btn-secondary');
        editBar.classList.remove('active');
        selectedCommitments.clear();
        loadCommitments();
    }
    renderCommitmentsRecent();
    renderCommitmentsAll();
    updateCommitmentSelectionUI();
}

function toggleCommitmentSelection(id, checked) {
    if (checked) {
        selectedCommitments.add(id);
    } else {
        selectedCommitments.delete(id);
    }
    updateCommitmentSelectionUI();
}

function toggleAllCommitmentSelection(masterCb) {
    const data = filteredCommitments || allCommitments;
    const start = (commitmentPage - 1) * COMMITMENTS_PER_PAGE;
    const pageData = data.slice(start, start + COMMITMENTS_PER_PAGE);

    if (masterCb.checked) {
        pageData.forEach(c => selectedCommitments.add(c.id));
    } else {
        pageData.forEach(c => selectedCommitments.delete(c.id));
    }
    renderCommitmentsAll();
    updateCommitmentSelectionUI();
}

function updateCommitmentSelectionUI() {
    const countEl = document.getElementById('commitmentSelectedCount');
    const deleteBtn = document.getElementById('deleteSelectedCommitmentsBtn');
    if (countEl) countEl.textContent = selectedCommitments.size;
    if (deleteBtn) deleteBtn.disabled = selectedCommitments.size === 0;
}

async function deleteSelectedCommitments() {
    if (selectedCommitments.size === 0) return;
    if (!confirm(`Cancel ${selectedCommitments.size} commitment(s)?`)) return;

    let cancelled = 0;
    for (const id of selectedCommitments) {
        try {
            const resp = await fetch(`/api/commitments/${id}`, { method: 'DELETE' });
            if (resp.ok) cancelled++;
        } catch (e) { /* skip */ }
    }
    selectedCommitments.clear();
    await loadCommitments();
    updateCommitmentSelectionUI();
    showToast(`Cancelled ${cancelled} commitment(s)`, 'success');
}

async function deleteSingleCommitment(id) {
    if (!confirm('Cancel this commitment?')) return;
    try {
        const resp = await fetch(`/api/commitments/${id}`, { method: 'DELETE' });
        if (resp.ok) {
            allCommitments = allCommitments.filter(c => c.id !== id);
            if (filteredCommitments) {
                filteredCommitments = filteredCommitments.filter(c => c.id !== id);
            }
            renderCommitmentsRecent();
            renderCommitmentsAll();
            showToast('Commitment cancelled', 'success');
        } else {
            showToast('Cancel failed', 'error');
        }
    } catch (e) {
        showToast('Cancel failed: ' + e.message, 'error');
    }
}

function renderCommitmentPagination(totalPages) {
    const container = document.getElementById('commitmentPagination');
    if (!container) return;
    container.innerHTML = '';
    if (totalPages <= 1) return;

    const prevBtn = document.createElement('button');
    prevBtn.className = 'pagination-btn';
    prevBtn.innerHTML = '&laquo;';
    prevBtn.disabled = commitmentPage === 1;
    prevBtn.onclick = () => { commitmentPage--; renderCommitmentsAll(); };
    container.appendChild(prevBtn);

    const pages = getPaginationRange(commitmentPage, totalPages);
    for (const page of pages) {
        if (page === '...') {
            const el = document.createElement('span');
            el.className = 'pagination-ellipsis';
            el.textContent = '...';
            container.appendChild(el);
        } else {
            const btn = document.createElement('button');
            btn.className = 'pagination-btn' + (page === commitmentPage ? ' active' : '');
            btn.textContent = page;
            btn.onclick = () => { commitmentPage = page; renderCommitmentsAll(); };
            container.appendChild(btn);
        }
    }

    const nextBtn = document.createElement('button');
    nextBtn.className = 'pagination-btn';
    nextBtn.innerHTML = '&raquo;';
    nextBtn.disabled = commitmentPage === totalPages;
    nextBtn.onclick = () => { commitmentPage++; renderCommitmentsAll(); };
    container.appendChild(nextBtn);
}

// --- Inline Edit ---

function startInlineEdit(commitment, tr) {
    if (!cachedLocations) return;

    const locTd = tr.querySelector('.commitment-location');
    const timeTd = tr.querySelector('.commitment-time-cell');
    if (!locTd || !timeTd) return;

    // Replace location cell with dropdown
    const locSelect = document.createElement('select');
    locSelect.style.fontSize = '0.85rem';
    locSelect.style.maxWidth = '180px';
    for (const [groupName, items] of Object.entries(cachedLocations)) {
        const optgroup = document.createElement('optgroup');
        optgroup.label = groupName;
        for (const item of items) {
            const opt = document.createElement('option');
            opt.value = item.id;
            opt.textContent = item.display;
            if (item.id === commitment.location_id) opt.selected = true;
            optgroup.appendChild(opt);
        }
        locSelect.appendChild(optgroup);
    }
    locTd.textContent = '';
    locTd.appendChild(locSelect);

    // Replace time cell with flatpickr input
    const timeInput = document.createElement('input');
    timeInput.type = 'text';
    timeInput.style.fontSize = '0.85rem';
    timeInput.style.width = '140px';
    timeInput.readOnly = true;

    // Parse existing time
    const m = commitment.game_time_start.match(/(\d{4})\/(\d{2})\/(\d{2})\s+(\d{2}):(\d{2})/);
    let defaultDate = null;
    if (m) {
        defaultDate = new Date(parseInt(m[1]), parseInt(m[2]) - 1, parseInt(m[3]), parseInt(m[4]), parseInt(m[5]));
    }

    timeTd.textContent = '';
    timeTd.appendChild(timeInput);

    const saveBtn = document.createElement('button');
    saveBtn.className = 'btn btn-primary btn-sm';
    saveBtn.textContent = 'Save';
    saveBtn.style.marginLeft = '4px';
    saveBtn.onclick = () => saveInlineEdit(commitment.id, locSelect.value, inlinePicker);

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-secondary btn-sm';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.style.marginLeft = '4px';
    cancelBtn.onclick = () => { if (inlinePicker) inlinePicker.destroy(); renderCommitmentsRecent(); renderCommitmentsAll(); };

    timeTd.appendChild(saveBtn);
    timeTd.appendChild(cancelBtn);

    const inlinePicker = flatpickr(timeInput, {
        enableTime: true,
        noCalendar: false,
        dateFormat: 'Y/m/d H:i',
        time_24hr: true,
        defaultDate: defaultDate,
    });
}

async function saveInlineEdit(commitmentId, locationId, picker) {
    const dateStr = picker?.selectedDates?.[0];
    if (!dateStr) {
        showToast('Please select a date and time', 'error');
        return;
    }
    const y = dateStr.getFullYear();
    const mo = String(dateStr.getMonth() + 1).padStart(2, '0');
    const d = String(dateStr.getDate()).padStart(2, '0');
    const h = String(dateStr.getHours()).padStart(2, '0');
    const mi = String(dateStr.getMinutes()).padStart(2, '0');
    const gameTimeStart = `${y}/${mo}/${d} ${h}:${mi}`;

    try {
        const resp = await fetch(`/api/commitments/${commitmentId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ location_id: locationId, game_time_start: gameTimeStart })
        });
        if (resp.ok) {
            if (picker) picker.destroy();
            // Fetch fresh data directly — loadCommitments() skips during edit mode
            await refreshCommitmentsData();
            showToast('Commitment updated', 'success');
        } else {
            const err = await resp.json();
            showToast(err.error || 'Update failed', 'error');
        }
    } catch (e) {
        showToast('Update failed: ' + e.message, 'error');
    }
}

async function refreshCommitmentsData() {
    /**Fetch and re-render commitments, even during edit mode.*/
    try {
        const response = await fetchWithTimeout('/api/commitments');
        if (response.ok) {
            allCommitments = await response.json();
            const npcId = document.getElementById('commitmentNpcFilter')?.value;
            if (npcId && npcId !== 'all') {
                filteredCommitments = allCommitments.filter(c => c.npc_id === npcId);
            } else {
                filteredCommitments = null;
            }
            renderCommitmentsRecent();
            renderCommitmentsAll();
        }
    } catch (e) { /* ignore */ }
}

// --- Create Form ---

async function loadCommitmentLocations() {
    if (cachedLocations) return;
    try {
        const resp = await fetchWithTimeout('/api/commitments/locations');
        if (resp.ok) {
            cachedLocations = await resp.json();
            populateLocationDropdown();
        }
    } catch (e) {
        // Will retry later
    }
}

function populateLocationDropdown() {
    const select = document.getElementById('commitmentCreateLocation');
    if (!select || !cachedLocations) return;

    select.innerHTML = '<option value="">Select location...</option>';
    for (const [groupName, items] of Object.entries(cachedLocations)) {
        const optgroup = document.createElement('optgroup');
        optgroup.label = groupName;
        for (const item of items) {
            const opt = document.createElement('option');
            opt.value = item.id;
            opt.textContent = item.display;
            optgroup.appendChild(opt);
        }
        select.appendChild(optgroup);
    }
}

function populateCommitmentCreateNpcDropdown() {
    const select = document.getElementById('commitmentCreateNpc');
    if (!select) return;
    const currentVal = select.value;

    // Get NPCs from dialogue history
    const npcIds = getUniqueNPCsFromHistory(allHistory);

    select.innerHTML = '<option value="">Select NPC...</option>';
    for (const id of npcIds) {
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = prettifyVoiceName(id);
        select.appendChild(opt);
    }
    if (currentVal) select.value = currentVal;
}

function initCommitmentDatePickers() {
    // Default to current game time + 1 hour
    let defaultDate = new Date();
    let defaultHour = 13;
    let defaultMinute = 0;

    if (cachedGameTime && cachedGameTime.available) {
        const gt = cachedGameTime;
        if (gt.year && gt.month && gt.day) {
            defaultDate = new Date(gt.year, gt.month - 1, gt.day);
        }
        if (gt.gameTime) {
            const tm = gt.gameTime.match(/(\d+):(\d+)/);
            if (tm) {
                let h = parseInt(tm[1], 10);
                const m = parseInt(tm[2], 10);
                if (gt.gameTime.includes('PM') && h !== 12) h += 12;
                else if (gt.gameTime.includes('AM') && h === 12) h = 0;
                defaultHour = (h + 1) % 24;
                defaultMinute = 0;
            }
        }
    }

    if (commitmentDatePicker) commitmentDatePicker.destroy();
    if (commitmentTimePicker) commitmentTimePicker.destroy();

    commitmentDatePicker = flatpickr('#commitmentCreateDate', {
        dateFormat: 'Y/m/d',
        defaultDate: defaultDate,
    });

    commitmentTimePicker = flatpickr('#commitmentCreateTime', {
        enableTime: true,
        noCalendar: true,
        dateFormat: 'H:i',
        time_24hr: true,
        defaultDate: new Date(2000, 0, 1, defaultHour, defaultMinute),
    });
}

async function createCommitment() {
    const npcId = document.getElementById('commitmentCreateNpc')?.value;
    const locationId = document.getElementById('commitmentCreateLocation')?.value;
    const dateVal = commitmentDatePicker?.selectedDates?.[0];
    const timeVal = commitmentTimePicker?.selectedDates?.[0];

    if (!npcId) { showToast('Please select an NPC', 'error'); return; }
    if (!locationId) { showToast('Please select a location', 'error'); return; }
    if (!dateVal) { showToast('Please select a date', 'error'); return; }
    if (!timeVal) { showToast('Please select a time', 'error'); return; }

    const y = dateVal.getFullYear();
    const mo = String(dateVal.getMonth() + 1).padStart(2, '0');
    const d = String(dateVal.getDate()).padStart(2, '0');
    const h = String(timeVal.getHours()).padStart(2, '0');
    const mi = String(timeVal.getMinutes()).padStart(2, '0');
    const gameTimeStart = `${y}/${mo}/${d} ${h}:${mi}`;

    try {
        const resp = await fetch('/api/commitments', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ npc_id: npcId, location_id: locationId, game_time_start: gameTimeStart })
        });
        if (resp.ok) {
            showToast('Commitment created', 'success');
            // Switch to Recent tab
            const recentTab = document.querySelector('#commitmentTabs .tab');
            if (recentTab) recentTab.click();
            await loadCommitments();
        } else {
            const err = await resp.json();
            showToast(err.error || 'Create failed', 'error');
        }
    } catch (e) {
        showToast('Create failed: ' + e.message, 'error');
    }
}

function showToast(message, type = 'success') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `
                <span>${type === 'success' ? '&#10003;' : '&#10007;'}</span>
                <span>${escapeHtml(message)}</span>
            `;
    container.appendChild(toast);

    setTimeout(() => toast.remove(), 3000);
}

// ============================================
// Reasoning Toggle Demo (prototype)
// ============================================
function showReasoningToast(enabled, modelName) {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = 'toast reasoning';

    const icon = enabled ? '&#10038;' : '&#10005;';
    const title = enabled ? 'Extended Thinking Enabled' : 'Extended Thinking Disabled';
    const desc = enabled
        ? `${modelName || 'Model'} will use deeper reasoning`
        : `${modelName || 'Model'} returns to standard mode`;

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

function toggleReasoningDemo(toggleEl) {
    const isActive = toggleEl.classList.toggle('active');
    const input = toggleEl.closest('.input-with-toggle').querySelector('input');
    const modelName = input?.value || 'Model';
    showReasoningToast(isActive, modelName);
}

// Warn before leaving with unsaved changes
window.addEventListener('beforeunload', (e) => {
    if (dirty) {
        e.preventDefault();
        e.returnValue = '';
    }
});

// ============================================
// Event Logging System - State-Tracked AJAX
// ============================================
let currentEventIds = new Set();

function formatRelativeTime(timestamp) {
    const now = Date.now() / 1000;
    const seconds = Math.floor(now - timestamp);

    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
}

function formatEventInfo(eventObj) {
    const type = eventObj.type;
    const data = eventObj.data || {};

    switch (type) {
        case 'llm':
            return `${data.model || 'LLM'} (${data.context || 'chat'})`;
        case 'tts':
            return `Voice: ${data.voice_id || 'unknown'}`;
        case 'voice_clone':
            return `Cloning: ${data.character_name || 'unknown'}`;
        case 'vision':
            return `Vision: ${data.location_name || 'unknown'}`;
        case 'stt':
            return `${data.provider || 'STT'}: ${data.model || 'transcription'}`;
        default:
            return 'Event';
    }
}

function formatEventDetail(eventObj) {
    const type = eventObj.type;
    const data = eventObj.data || {};

    switch (type) {
        case 'llm':
            return data.model || '';
        case 'tts':
            return data.text_excerpt ? data.text_excerpt.substring(0, 50) : '';
        case 'voice_clone':
            return data.reference_filename || '';
        case 'vision':
            return data.description_excerpt ? data.description_excerpt.substring(0, 50) : '';
        case 'stt':
            return data.transcript_excerpt ? data.transcript_excerpt.substring(0, 50) : '';
        default:
            return '';
    }
}

function formatEventMetric(eventObj) {
    const type = eventObj.type;
    const data = eventObj.data || {};
    const tokens = data.tokens || {};
    const latency = data.duration_ms ? `${Math.round(data.duration_ms)}ms` : null;

    switch (type) {
        case 'llm':
            const tokenStr = tokens.total ? `${tokens.total}T` : null;
            if (tokenStr && latency) return `${tokenStr} / ${latency}`;
            return tokenStr || latency || '—';
        case 'tts':
            const charCount = data.text_length || (data.text_excerpt ? data.text_excerpt.length : null);
            const charStr = charCount ? `${charCount} chars` : null;
            if (charStr && latency) return `${charStr} / ${latency}`;
            return charStr || latency || '—';
        case 'voice_clone':
            if (latency) return latency;
            return data.voice_id ? 'OK' : '—';
        case 'vision':
            const visionTokenStr = tokens.total ? `${tokens.total}T` : null;
            if (visionTokenStr && latency) return `${visionTokenStr} / ${latency}`;
            return visionTokenStr || latency || '—';
        case 'stt':
            const audioDuration = data.audio_duration_ms ? `${Math.round(data.audio_duration_ms / 1000)}s audio` : null;
            if (audioDuration && latency) return `${audioDuration} / ${latency}`;
            return audioDuration || latency || '—';
        default:
            return '—';
    }
}

function renderEvent(eventObj) {
    const typeClass = eventObj.type.replace('_', '_');
    const statusClass = eventObj.status || 'success';
    const isError = eventObj.status === 'error' && eventObj.error;
    const isSuccess = eventObj.status === 'success' || !eventObj.status;
    const timeStr = formatRelativeTime(eventObj.timestamp);
    const infoStr = formatEventInfo(eventObj);
    const detailStr = formatEventDetail(eventObj);
    const metricStr = formatEventMetric(eventObj);

    const row = document.createElement('div');
    let rowClass = 'event-row-new';
    if (isError) rowClass += ' event-row-error';
    else if (isSuccess) rowClass += ' event-row-success';
    row.className = rowClass;
    row.id = `event-${eventObj.id}`;
    row.dataset.timestamp = eventObj.timestamp;

    let errorLine = '';
    if (isError) {
        errorLine = `<div class="event-error-message">${escapeHtml(eventObj.error)}</div>`;
    }

    row.innerHTML = `
                <div class="event-time">${timeStr}</div>
                <div class="event-type ${typeClass}">${eventObj.type}</div>
                <div class="event-status ${statusClass}"></div>
                <div class="event-info">${escapeHtml(infoStr)}</div>
                <div class="event-metric">${escapeHtml(metricStr)}</div>
                ${errorLine}
            `;

    return row;
}

function updateAllEventTimes() {
    const listContainer = document.getElementById('eventsList');
    if (!listContainer) return;

    const rows = listContainer.querySelectorAll('[data-timestamp]');
    rows.forEach(row => {
        const timestamp = parseFloat(row.dataset.timestamp);
        if (!isNaN(timestamp)) {
            const timeEl = row.querySelector('.event-time');
            if (timeEl) {
                timeEl.textContent = formatRelativeTime(timestamp);
            }
        }
    });
}

function updateEventList(events) {
    const listContainer = document.getElementById('eventsList');
    if (!listContainer) return;

    // Find new and removed events
    const newEventIds = new Set(events.map(e => e.id));
    const addedEventIds = Array.from(newEventIds).filter(id => !currentEventIds.has(id));
    const removedEventIds = Array.from(currentEventIds).filter(id => !newEventIds.has(id));

    // Remove deleted events
    for (const id of removedEventIds) {
        const elem = document.getElementById(`event-${id}`);
        if (elem) elem.remove();
        currentEventIds.delete(id);
    }

    // Add new events at the top (most recent first)
    // Filter to only added events, then reverse so insertBefore yields correct order
    const addedEvents = events.filter(e => addedEventIds.includes(e.id));
    for (const event of addedEvents.reverse()) {
        const row = renderEvent(event);
        listContainer.insertBefore(row, listContainer.firstChild);
        currentEventIds.add(event.id);
    }

    // Remove empty message if events exist
    if (events.length > 0) {
        const emptyMsg = listContainer.querySelector('.events-empty');
        if (emptyMsg) emptyMsg.remove();
    } else if (currentEventIds.size === 0) {
        // Show empty message if no events
        listContainer.innerHTML = '<div class="events-empty">No events logged yet</div>';
    }
}

function loadSystemEvents() {
    if (eventsLoadInFlight) return;
    eventsLoadInFlight = true;
    fetchWithTimeout('/api/system-events?limit=100')
        .then(response => response.json())
        .then(events => {
            if (Array.isArray(events)) {
                updateEventList(events);
            }
        })
        .catch(() => {
            // Silently ignore timeout/network errors during polling
        })
        .finally(() => {
            eventsLoadInFlight = false;
        });
}

function clearEvents() {
    if (!confirm('Clear all events?')) return;

    fetch('/api/system-events', { method: 'DELETE' })
        .then(response => response.json())
        .then(() => {
            currentEventIds.clear();
            const listContainer = document.getElementById('eventsList');
            if (listContainer) {
                listContainer.innerHTML = '<div class="events-empty">No events logged yet</div>';
            }
            showToast('Events cleared', 'success');
        })
        .catch(err => {
            console.error('[Events] Failed to clear:', err);
            showToast('Failed to clear events', 'error');
        });
}

// ============================================
// Floating Navigation - Scroll Spy & Toggle
// ============================================
function toggleNav() {
    const nav = document.getElementById('grimoireNav');
    const backdrop = document.getElementById('navBackdrop');
    nav.classList.toggle('nav-open');
    backdrop.classList.toggle('active');
}

function closeNav() {
    const nav = document.getElementById('grimoireNav');
    const backdrop = document.getElementById('navBackdrop');
    nav.classList.remove('nav-open');
    backdrop.classList.remove('active');
}

function initScrollSpy() {
    const sections = document.querySelectorAll('.chapter');
    const navLinks = document.querySelectorAll('.nav-links a');

    // Create intersection observer
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const id = entry.target.id;
                // Update active state
                navLinks.forEach(link => {
                    link.classList.remove('active');
                    if (link.getAttribute('href') === '#' + id) {
                        link.classList.add('active');
                    }
                });
            }
        });
    }, {
        rootMargin: '-20% 0px -60% 0px',
        threshold: 0
    });

    // Observe all sections
    sections.forEach(section => observer.observe(section));

    // Close nav on link click (mobile)
    navLinks.forEach(link => {
        link.addEventListener('click', () => {
            if (window.innerWidth <= 900) {
                closeNav();
            }
        });
    });
}

// ============================================
// Time Dilation Controls - Tempus Arcanum
// ============================================
let timeDilationPanelOpen = false;

// Specific rate steps for day/night sliders: 0.5x, 1x, 2x, 3x, 4x, 5x, 10x, 20x, 30x, 50x, 100x
const TIME_RATE_STEPS = [0.5, 1, 2, 3, 4, 5, 10, 20, 30, 50, 100];

// Convert slider index (0-10) to actual rate value
function getTimeRateValue(index) {
    const idx = parseInt(index);
    return TIME_RATE_STEPS[Math.min(Math.max(idx, 0), TIME_RATE_STEPS.length - 1)];
}

// Convert rate value to slider index (finds closest)
function getTimeRateIndex(rate) {
    let closestIdx = 0;
    let closestDiff = Math.abs(TIME_RATE_STEPS[0] - rate);
    for (let i = 1; i < TIME_RATE_STEPS.length; i++) {
        const diff = Math.abs(TIME_RATE_STEPS[i] - rate);
        if (diff < closestDiff) {
            closestDiff = diff;
            closestIdx = i;
        }
    }
    return closestIdx;
}

function toggleTimeDilationPanel() {
    const panel = document.getElementById('timeDilationPanel');
    const toggle = document.getElementById('timeFlowToggle');
    timeDilationPanelOpen = !timeDilationPanelOpen;

    if (timeDilationPanelOpen) {
        panel.classList.add('open');
        toggle.classList.add('active');
    } else {
        panel.classList.remove('open');
        toggle.classList.remove('active');
    }
}

function loadTimeDilationSettings(settings) {
    const enabled = settings.enabled !== false;
    const dayRate = settings.day_rate ?? 3.0;
    const nightRate = settings.night_rate ?? 3.0;

    // Set checkbox
    setCheckbox('time_dilation_enabled', enabled);

    // Set sliders (day/night use index-based values)
    const daySlider = document.getElementById('time_day_rate');
    const nightSlider = document.getElementById('time_night_rate');

    if (daySlider) daySlider.value = getTimeRateIndex(dayRate);
    if (nightSlider) nightSlider.value = getTimeRateIndex(nightRate);

    // Update displays
    updateTimeDilationPreview('day', dayRate);
    updateTimeDilationPreview('night', nightRate);

    // Update toggle button state
    updateTimeFlowIndicator(enabled, dayRate);

    // Update content disabled state
    updateTimePanelContentState(enabled);

    // Update descriptive hint
    updateTimeDilationHint();
}

function updateTimeDilationPreview(type, value) {
    const valueEl = document.getElementById(`time_${type}_rate_value`);
    if (valueEl) {
        const numValue = parseFloat(value);
        valueEl.textContent = numValue === 1 ? '×1' : `×${numValue}`;
    }
    updateTimeDilationHint();
}

// Format a duration in hours into a readable string
function formatRealDuration(totalHours) {
    if (totalHours >= 48) {
        return `~${Math.round(totalHours / 24)} days`;
    }
    if (totalHours >= 24) {
        const days = totalHours / 24;
        return days === Math.floor(days) ? `${days} days` : `~${days.toFixed(1)} days`;
    }
    if (totalHours >= 1) {
        const hrs = Math.floor(totalHours);
        const mins = Math.round((totalHours - hrs) * 60);
        if (mins === 0) return `${hrs} hour${hrs !== 1 ? 's' : ''}`;
        return `${hrs} hour${hrs !== 1 ? 's' : ''} ${mins} minutes`;
    }
    const mins = Math.round(totalHours * 60);
    return `${mins} minutes`;
}

// Describe what 1 real hour equals in game time
function getGameTimePerRealHour(rate) {
    if (rate < 1) return `${Math.round(rate * 60)} game minutes`;
    if (rate === 1) return '1 game hour';
    return `${rate} game hours`;
}

// Update the hint below the sliders with a human-readable description
function updateTimeDilationHint() {
    const hintEl = document.getElementById('timeDilationHint');
    if (!hintEl) return;

    const daySlider = document.getElementById('time_day_rate');
    const nightSlider = document.getElementById('time_night_rate');
    if (!daySlider || !nightSlider) return;

    const dayRate = getTimeRateValue(daySlider.value);
    const nightRate = getTimeRateValue(nightSlider.value);

    if (dayRate === nightRate) {
        const realHours = 24 / dayRate;
        if (dayRate === 1) {
            hintEl.innerHTML = `Real time &mdash; full game day lasts 24 hours. Vanilla Hogwarts runs at 30&times;.`;
        } else {
            hintEl.innerHTML = `1 real hour = ${getGameTimePerRealHour(dayRate)} &middot; full game day in ~${formatRealDuration(realHours)}. Vanilla is 30&times;.`;
        }
    } else {
        // Different day/night rates. Assume ~12 game hrs each for day and night.
        const realDayHours = 12 / dayRate;
        const realNightHours = 12 / nightRate;
        const totalRealHours = realDayHours + realNightHours;

        hintEl.innerHTML =
            `Day: 1 real hour = ${getGameTimePerRealHour(dayRate)} &middot; ` +
            `Night: 1 real hour = ${getGameTimePerRealHour(nightRate)}<br>` +
            `Full game day in ~${formatRealDuration(totalRealHours)}. Vanilla is 30&times;.`;
    }
}

async function updateTimeDilation(setting, value) {
    // Build the full setting path
    const settingPath = `time_dilation.${setting}`;
    updateSetting(settingPath, value);

    // If toggling enabled, update UI state
    if (setting === 'enabled') {
        updateTimePanelContentState(value);
        // Get the actual rate value (not the slider index)
        const daySliderIndex = parseInt(document.getElementById('time_day_rate')?.value || 4);
        const dayRate = getTimeRateValue(daySliderIndex);
        updateTimeFlowIndicator(value, dayRate);
    }

    // If changing day rate, update flow indicator
    if (setting === 'day_rate') {
        const enabled = document.getElementById('time_dilation_enabled')?.checked;
        updateTimeFlowIndicator(enabled, value);
    }

    // Auto-save to immediately sync to Lua (time dilation is a live setting)
    await saveSettings();
}

function updateTimePanelContentState(enabled) {
    const content = document.getElementById('timePanelContent');
    const toggle = document.getElementById('timeFlowToggle');

    if (content) {
        if (enabled) {
            content.classList.remove('disabled');
        } else {
            content.classList.add('disabled');
        }
    }

    if (toggle) {
        if (enabled) {
            toggle.classList.remove('disabled');
        } else {
            toggle.classList.add('disabled');
        }
    }
}

function updateTimeFlowIndicator(enabled, dayRate) {
    const rateEl = document.getElementById('timeFlowRate');
    if (rateEl) {
        if (enabled) {
            const numRate = parseFloat(dayRate);
            rateEl.textContent = numRate === 1 ? '×1' : `×${numRate}`;
        } else {
            rateEl.textContent = 'OFF';
        }
    }
}

// ============================================
// VR Plugin Management
// ============================================

async function checkVrPluginStatus() {
    try {
        const response = await fetch('/api/vr/plugin-status');
        if (!response.ok) return;
        const data = await response.json();

        const badge = document.getElementById('vrPluginBadge');
        const installBtn = document.getElementById('vrPluginInstallBtn');
        const pathEl = document.getElementById('vrPluginPath');

        if (badge) {
            if (data.installed) {
                badge.innerHTML = '<span class="badge badge-success">Installed</span>';
                if (installBtn) installBtn.style.display = 'none';
            } else {
                badge.innerHTML = '<span class="badge badge-muted">Not Installed</span>';
                if (installBtn) installBtn.style.display = data.source_available ? '' : 'none';
            }
        }

        if (pathEl && data.target_dir) {
            pathEl.textContent = data.target_dir;
        }

        if (window.lucide) lucide.createIcons();
    } catch (error) {
        console.error('Failed to check VR plugin status:', error);
    }
}

async function installVrPlugin() {
    const btn = document.getElementById('vrPluginInstallBtn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Installing...';
    }

    try {
        const response = await fetch('/api/vr/install-plugin', { method: 'POST' });
        const data = await response.json();

        if (response.ok) {
            showToast('UEVR plugin installed successfully', 'success');
            checkVrPluginStatus();
        } else {
            showToast('Failed to install plugin: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        showToast('Failed to install plugin', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Install Plugin';
        }
    }
}

// Start polling for events on page load
window.addEventListener('load', () => {
    loadSystemEvents();
    setInterval(loadSystemEvents, 5000);  // Poll every 5 seconds
    setInterval(updateAllEventTimes, 1000);  // Update relative times every second
    initScrollSpy();  // Initialize navigation scroll spy
    checkVrPluginStatus();
    setInterval(checkVrPluginStatus, 30000);  // Check every 30s
});
