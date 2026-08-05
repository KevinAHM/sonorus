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
const UNDUBBED_LANGUAGE_VALUES = new Set(["AR_AE", "PL_PL", "RU_RU", "KO_KR", "ZH_CN", "ZH_TW"]);

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
        label: "\ud83c\udfc6 Inworld AI (Recommended)",
        description: `New to Inworld? <a href="https://inworld.ai/signup?ref=7HQNN63N" target="_blank">Sign up with our link</a> to get $2 free credit (~40 minutes of audio) and $10 credit if you subscribe to a plan! Then <a href="https://platform.inworld.ai" target="_blank">get your API key</a> from the Inworld Platform.<br>
                    <b>\u26a0\ufe0f You must select "Write" access when creating your key (not "Read")</b>.<br>
                    \ud83d\udca1 Pay-as-you-go (free) supports up to 100 total voice clones. When the limit is reached, the oldest unused clone is automatically deleted to make room.`,
        fields: [
            { id: "api_url", type: "text", label: "API URL", placeholder: "https://api.inworld.ai", default: "https://api.inworld.ai", hint: "Base URL for the Inworld API (leave default unless using a proxy)", simple_hide: true },
            { id: "api_key", type: "password", label: "API Key", placeholder: "Base64 encoded key", hint: "Use Basic (Base64) key, not JWT — <strong>\u26a0\ufe0f select \"Write\" access when creating (not \"Read\")</strong>", validate: validateInworldApiKey },
            { id: "model", type: "text", label: "TTS Model", placeholder: "inworld-tts-2", default: "inworld-tts-2", hint: "<strong>inworld-tts-2</strong>&nbsp; (most expressive, highest quality, recommended), <strong>inworld-tts-1.5-mini</strong>&nbsp; (cheaper)", onChange: "onInworldModelChange" },
            { id: "temperature", type: "range", label: "TTS Expression", hint: "For Inworld TTS 2: 0.1–0.5 = Stable, 0.6–1.4 = Balanced, and 1.5–2.0 = Creative. Earlier models use this value as temperature. For per-NPC adjustments, use <a href=\"#chapterCharacters\" onclick=\"scrollToSection('chapterCharacters')\">Characters</a>.", min: 0.1, max: 2.0, step: 0.1, default: 1.1, simple_hide: true },
            { id: "speaking_rate", type: "range", label: "Speaking Rate", hint: "Adjust Inworld speech speed. Use 1.05× to closely match the speaking rate of Inworld TTS 1.5 Max when using TTS 2.", min: 0.9, max: 1.1, step: 0.005, default: 1.0, display_suffix: "×", simple_hide: true },
            {
                id: "sample_rate", type: "select", label: "Sample Rate", options: [
                    { value: 22050, label: "22050 Hz" },
                    { value: 24000, label: "24000 Hz" },
                    { value: 44100, label: "44100 Hz" },
                    { value: 48000, label: "48000 Hz" }
                ], default: 48000, simple_hide: true
            },
            { id: "localize_audio_tags", type: "toggle", label: "Localize Audio Tags", hint: "Translate [sigh], [laugh] etc. to language-specific equivalents for non-English languages. Disable if tags aren't being spoken correctly.", default: true, simple_hide: true },
            { id: "emote_passthrough", type: "toggle", label: "Emote Passthrough", hint: "Inworld TTS 2 only. Pass emotion tags such as [angry] and [amused] to TTS for expressive delivery. Turn off to use tags only for facial animation.", default: true, simple_hide: true },
            { id: "dynamic_delivery", type: "toggle", label: "Dynamic Delivery", hint: "Automatically use Creative delivery for high-energy emotion tags. With Inworld 1.5 models, expressive lines receive a +0.3 temperature boost instead.", default: true, simple_hide: true }
        ]
    },
    elevenlabs: {
        label: "\ud83e\udd48 ElevenLabs",
        description: "Pro plan recommended for optimal experience. Lower plans have reduced audio quality and fewer cloned voices (Free: 3, Starter: 10, Creator: 30, Pro: 100+). When voice limit is reached, least recently used clones are auto-deleted. Monthly voice clone operations: Free 55, Starter 65, Creator 95, Pro 290.",
        fields: [
            { id: "api_url", type: "text", label: "API URL", placeholder: "https://api.elevenlabs.io", default: "https://api.elevenlabs.io", hint: "Base URL for the ElevenLabs API (leave default unless using a proxy)", simple_hide: true },
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
            { id: "stability", type: "range", label: "Stability", hint: "Higher = more consistent, Lower = more expressive", min: 0, max: 1, step: 0.05, default: 0.5, simple_hide: true },
            { id: "similarity_boost", type: "range", label: "Clarity + Similarity", min: 0, max: 1, step: 0.05, default: 0.75, simple_hide: true },
            {
                id: "sample_rate", type: "select", label: "Sample Rate", hint: "Max rate depends on plan", options: [
                    { value: 16000, label: "16000 Hz" },
                    { value: 22050, label: "22050 Hz" },
                    { value: 24000, label: "24000 Hz" },
                    { value: 44100, label: "44100 Hz" }
                ], default: 24000, simple_hide: true
            }
        ]
    },
    pocket: {
        label: "\u26a1 Pocket TTS (Free, Local, English Only)",
        description: "Local text-to-speech using the Pocket TTS model. Lightweight CPU-based synthesis with voice cloning support. No API key required. English only at the moment.",
        fields: [
            { id: "streaming", type: "toggle", label: "Streaming Mode", hint: "Disable if you experience audio hitching or game lag during speech", default: true }
        ]
    },
    omnivoice_cpp: {
        label: "\ud83d\udcaa OmniVoice (Free, Local, Vulkan)",
        description: "Recommended. Compact Q8 build with faster startup and lower disk and memory usage. Runs through Vulkan on NVIDIA, AMD, and Intel GPUs.",
        fields: [
            { id: "first_sentence_steps", type: "range", label: "First Sentence Steps", hint: "Fewer steps on the first sentence for faster time-to-first-audio.", min: 8, max: 64, step: 4, default: 24 },
            { id: "num_steps", type: "range", label: "Default Steps", hint: "More steps = higher quality but slower. 32 is recommended.", min: 8, max: 64, step: 4, default: 32 },
            { id: "guidance_scale", type: "range", label: "Style Guidance (CFG)", hint: "Controls how closely the model follows its style conditioning. Higher = more expressive but less stable.", min: 0.0, max: 10.0, step: 0.1, default: 2.0 },
            { id: "apply_smoothing_eq", type: "toggle", label: "Apply Smoothing EQ", default: true }
        ]
    },
    omnivoice: {
        label: "\ud83d\udd37 OmniVoice FP16 (Free, Local, NVIDIA / CUDA)",
        description: "Full-precision PyTorch build for NVIDIA graphics cards only. Larger installation, higher resource usage, and slower generation.",
        fields: [
            { id: "first_sentence_steps", type: "range", label: "First Sentence Steps", hint: "Fewer steps on the first sentence for faster time-to-first-audio.", min: 8, max: 64, step: 4, default: 16 },
            { id: "num_steps", type: "range", label: "Default Steps", hint: "More steps = higher quality but slower. 32 is recommended.", min: 8, max: 64, step: 4, default: 32 },
            { id: "guidance_scale", type: "range", label: "Style Guidance (CFG)", hint: "Controls how closely the model follows its style conditioning. Higher = more expressive but less stable.", min: 0.0, max: 10.0, step: 0.1, default: 2.0 },
            { id: "prefix_kv_cache_enabled", type: "toggle", label: "Prefix KV Cache Optimization", hint: "Recommended. Reuses the conditioning prefix across generation steps for much faster speech. Disable to recompute full bidirectional attention each step; this may improve quality but is significantly slower.", default: true },
            { id: "prefix_kv_cache_first_sentence_only", type: "toggle", label: "First Sentence Only", hint: "Use the prefix KV cache for the first sentence, then use full bidirectional attention for later sentences.", default: false, parent: "prefix_kv_cache_enabled" },
            { id: "apply_smoothing_eq", type: "toggle", label: "Apply Smoothing EQ", default: true }
        ]
    },
    universal: {
        label: "\ud83c\udf10 Universal Speech Server (Free, Local, CUDA / Vulkan)",
        description: "Connect to a CrispASR-backed speech server. Model controls appear only after authenticated capability discovery.",
        fields: [
            { id: "silence_min_ms", type: "range", label: "Minimum Sentence Pause", hint: "Minimum server-inserted pause between sentences, in milliseconds.", min: 0, max: 2000, step: 50, default: 250, simple_hide: true },
            { id: "silence_max_ms", type: "range", label: "Maximum Sentence Pause", hint: "Maximum server-inserted pause between sentences, in milliseconds.", min: 0, max: 3000, step: 50, default: 1000, simple_hide: true }
        ]
    }
};

const LLM_PROVIDER_FEATURE_GATES = [
    { id: "disable_input_correction", feature: "input_correction", label: "Disable Input Correction", hint: "Blocks the input correction agent for this provider without changing the saved Input Correction setting." },
    { id: "disable_vision", feature: "vision", label: "Disable Vision", hint: "Blocks vision capture/scene description calls for this provider without changing the saved Vision Agent setting." },
    { id: "disable_owl_post", feature: "owl_post", label: "Disable Owl Post", hint: "Blocks Owl Post generation for this provider without changing the saved Owl Post setting." },
    { id: "disable_memory", feature: "memory", label: "Disable Long-Term Memory", hint: "Blocks long-term memory for this provider without changing the saved Long-Term Memory setting." }
];

function llmFeatureGateGroup(defaultDisabled, defaultOverrides = {}, featureFilter = null) {
    const gates = featureFilter
        ? LLM_PROVIDER_FEATURE_GATES.filter(gate => featureFilter.includes(gate.feature))
        : LLM_PROVIDER_FEATURE_GATES;
    return {
        id: "feature_gates",
        type: "toggle_group",
        label: "Provider Feature Disables",
        hint: "Use these when a provider should handle core chat, but not auxiliary LLM-heavy features. Saved feature settings stay unchanged.",
        fields: gates.map(gate => ({ ...gate, default: defaultOverrides[gate.id] ?? defaultDisabled }))
    };
}

const LLM_PROVIDERS = {
    gemini: {
        label: "Google Gemini (Limited Free)",
        fields: [
            { id: "reasoning_enabled", type: "toggle", label: "Enable Reasoning", hint: "Master switch for extended thinking. Enable per-model toggles below.", default: true },
            llmFeatureGateGroup(false, { disable_memory: true }, ['memory'])
        ]
    },
    openrouter: {
        label: "OpenRouter",
        fields: [
            { id: "reasoning_enabled", type: "toggle", label: "Enable Reasoning", hint: "Master switch for extended thinking. Enable per-model toggles below.", default: true },
            { id: "allow_provider_fallbacks", type: "toggle", label: "Allow Provider Fallbacks", hint: "When model provider routing is configured, allow OpenRouter to use providers outside your list if none match. Disable for more deterministic routing and fewer pricing surprises, but calls may error when no listed provider is available.", default: true, simple_hide: true },
            llmFeatureGateGroup(false, {}, ['memory'])
        ]
    },
    openai: {
        label: "OpenAI",
        fields: [
            { id: "api_url", type: "text", label: "API URL (Optional)", placeholder: "https://api.openai.com/v1", hint: "Leave empty to use default OpenAI endpoint", onChange: "onOpenAIUrlChange" },
            { id: "responses_api", type: "toggle", label: "Use Responses API", hint: "Enable for endpoints that support the Responses API. Required for reasoning.", default: true },
            { id: "reasoning_enabled", type: "toggle", label: "Enable Reasoning", hint: "Master switch for extended thinking. Enable per-model toggles below.", default: true },
            llmFeatureGateGroup(false)
        ]
    },
    ollama: {
        label: "Ollama Cloud (Free, Small Models)",
        fields: [
            { id: "api_url", type: "text", label: "API URL", placeholder: "https://ollama.com/api/chat", hint: "Ollama chat endpoint." },
            llmFeatureGateGroup(true)
        ]
    },
    llamacpp: {
        label: "llama.cpp",
        fields: [
            { id: "api_url", type: "text", label: "API URL", placeholder: "http://127.0.0.1:8080/v1", hint: "Local or remote llama.cpp OpenAI-compatible server URL. /v1 is added automatically if omitted." },
            { id: "kv_cache_enabled", type: "toggle", label: "Enable Slot KV Cache", hint: "Save and restore llama.cpp slot 0 for cacheable prompts. Requires llama-server to be started with <strong>--slot-save-path</strong>. This is most useful when the server has less than 32 GB of RAM available for KV cache; with sufficient RAM, <strong>--cache-ram 16384</strong> or higher can be used instead.", default: true },
            { id: "kv_cache_max_entries", type: "range", label: "Max KV Cache Entries", hint: "Number of reusable server-side slot snapshots to retain.", min: 1, max: 25, step: 1, default: 10 },
            llmFeatureGateGroup(true)
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

    responsesApiGroup.style.display = '';
    if (responsesApiField) {
        responsesApiField.disabled = false;
    }

    const responsesNotice = responsesApiGroup.querySelector('.responses-api-notice');
    if (responsesNotice) {
        responsesNotice.remove();
    }

    const responsesApiEnabled = config.llm?.openai?.responses_api === true;

    if (!responsesApiEnabled && reasoningField && reasoningGroup) {
        // Force reasoning OFF and disable when Responses API is disabled
        reasoningField.checked = false;
        reasoningField.disabled = true;
        reasoningGroup.style.opacity = '0.5';
        if (config.llm?.openai) config.llm.openai.reasoning_enabled = false;

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
        if (reasoningField) reasoningField.disabled = false;
        if (reasoningGroup) reasoningGroup.style.opacity = '';
        const notice = reasoningGroup?.querySelector('.responses-api-notice');
        if (notice) notice.remove();

        if (window.ReasoningToggle && reasoningField) {
            ReasoningToggle.setMasterEnabled(reasoningField.checked);
        }
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
        label: "Canary (Free, Local, Recommended)",
        description: `Local speech recognition using NVIDIA Canary 180M Flash. No API key needed &mdash; runs entirely on your machine. Supports English, German, Spanish, and French. Requires ~250 MB of available RAM. Model is downloaded on first use.`,
        fields: []
    },
    parakeet: {
        label: "Parakeet (Free, Local)",
        description: `Local speech recognition using NVIDIA Parakeet TDT 0.6B V3. No API key needed &mdash; runs entirely on your machine. Multilingual with automatic language detection. Requires ~1.5 GB of available RAM. Model is downloaded on first use.`,
        fields: []
    },
    moonshine: {
        label: "Moonshine (Free, Local, English Only)",
        description: `A lighter-weight alternative to Parakeet. Local speech recognition using Moonshine Base. No API key needed &mdash; runs entirely on your machine. English only. Requires ~250 MB of available RAM. Model is downloaded on first use.`,
        fields: []
    },
    universal: {
        label: "Universal Speech Server (Free, Local, CUDA / Vulkan)",
        description: "Use a speech-recognition model hosted by the shared Universal Speech Server.",
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
            { id: "cooldown_seconds", type: "range", label: "Cooldown (seconds)", hint: "Minimum time between captures when starting voice/chat input", min: 1, max: 30, step: 1, default: 5, display_suffix: 's', simple_hide: true },
            { id: "wait_for_capture", type: "toggle", label: "Wait for Capture", hint: "Wait for vision capture to complete before AI responds. Disable if using a fast model.", default: true },
            { id: "wait_timeout_seconds", type: "range", label: "Wait Timeout (seconds)", hint: "Maximum time to wait for a vision capture when Wait for Capture is enabled. Used by both normal conversations and event-driven commentary.", min: 1, max: 10, step: 0.5, default: 5, display_suffix: 's' }
        ],
        llm: {
            fields: [
                { id: "model", type: "text", label: "Vision Model", hint: "Use a fast model for quick scene descriptions.", placeholder: "gemini-3.1-flash-lite", default: "gemini-3.1-flash-lite" },
                { id: "temperature", type: "range", label: "Temperature", min: 0, max: 2, step: 0.1, default: 0.7, simple_hide: true },
                { id: "max_tokens", type: "range", label: "Max Tokens", hint: "High default accounts for reasoning budgets. Reduce if errors occur.", min: 128, max: 16384, step: 128, default: 8192, simple_hide: true }
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

    const simpleHideAttr = field.simple_hide ? ' data-simple-hide="true"' : '';
    const parentAttr = field.parent ? ` data-parent-field-id="${escapeHtml(field.parent)}"` : '';
    const nestedStyleAttr = field.parent
        ? ' style="border-left: 2px solid var(--gold-dark); margin-left: var(--space-sm); padding-left: var(--space-md);"'
        : '';
    let html = `<div class="field-group" data-config-path="${settingPath}" data-field-id="${field.id}"${simpleHideAttr}${parentAttr}${nestedStyleAttr}>`;
    if (field.type !== 'toggle_group') {
        html += `<label class="field-label">${escapeHtml(field.label)}</label>`;

        if (field.hint) {
            html += `<p class="field-hint">${field.hint}</p>`;
        }
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
            const rangeDisplay = field.display_suffix ? `${rangeValue}${field.display_suffix}` : rangeValue;
            const rangeDisplayExpr = field.display_suffix
                ? `this.value + '${field.display_suffix}'`
                : 'this.value';
            html += `<div class="range-wrapper">
                        <input type="range" id="${fieldId}"
                               min="${field.min}" max="${field.max}" step="${field.step}" value="${rangeValue}"
                               oninput="updateRangeValue('${fieldId}', ${rangeDisplayExpr}); updateProviderSetting('${category}', '${providerId}', '${field.id}', parseFloat(this.value))">
                        <span class="range-value" id="${fieldId}Value">${rangeDisplay}</span>
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
                                   onchange="updateProviderSetting('${category}', '${providerId}', '${field.id}', this.checked)${extraHandler}; updateProviderFieldDependencies('${category}', '${providerId}')">
                            <span class="toggle-track">
                                <span class="toggle-thumb"></span>
                            </span>
                        </label>
                    </div>`;
            if (field.description_html) {
                html += `<p class="field-hint" style="margin-top: var(--space-xs);">${field.description_html}</p>`;
            }
            break;

        case 'toggle_group':
            html += `<div class="sub-panel">
                        <div class="sub-panel-header" onclick="toggleSubPanel(this)">
                            <span class="sub-panel-title">
                                <span class="sub-panel-icon"><i data-lucide="sliders-horizontal"></i></span>
                                ${escapeHtml(field.label)}
                            </span>
                            <span class="sub-panel-toggle"><i data-lucide="chevron-down"></i></span>
                        </div>
                        <div class="sub-panel-content">`;
            if (field.hint) {
                html += `<p class="field-hint" style="margin-bottom: var(--space-md);">${field.hint}</p>`;
            }
            for (const child of field.fields || []) {
                const childId = `${category}_${providerId}_${child.id}`;
                const childValue = config[category]?.[providerId]?.[child.id] ?? child.default ?? false;
                html += `<div class="toggle-wrapper" style="padding-top: 0;">
                            <span class="toggle-label">${escapeHtml(child.label)}</span>
                            <label class="toggle">
                                <input type="checkbox" id="${childId}" ${childValue ? 'checked' : ''}
                                       onchange="updateProviderSetting('${category}', '${providerId}', '${child.id}', this.checked); updateLLMFeatureAvailability('${providerId}'); if ('${child.feature}' === 'memory') updateMemoryAvailability('${providerId}')">
                                <span class="toggle-track">
                                    <span class="toggle-thumb"></span>
                                </span>
                            </label>
                        </div>`;
                if (child.hint) {
                    html += `<p class="field-hint" style="margin-bottom: var(--space-md);">${escapeHtml(child.hint)}</p>`;
                }
            }
            html += `</div></div>`;
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

function updateProviderFieldDependencies(category, providerId) {
    const container = document.getElementById(`${category}ProviderSettings`);
    if (!container) return;

    container.querySelectorAll('[data-parent-field-id]').forEach(wrapper => {
        const parentFieldId = wrapper.dataset.parentFieldId;
        const parentToggle = document.getElementById(`${category}_${providerId}_${parentFieldId}`);
        const isDisabled = parentToggle?.checked !== true;

        wrapper.querySelectorAll('input, select, textarea, button').forEach(control => {
            control.disabled = isDisabled;
        });
        wrapper.style.opacity = isDisabled ? '0.5' : '1';
        wrapper.style.pointerEvents = isDisabled ? 'none' : 'auto';
    });
}

const UNIVERSAL_SPEECH_SERVER_RELEASES_URL = 'https://github.com/KevinAHM/universal-speech-server/releases';

const universalSpeechState = {
    status: 'idle',
    capabilities: null,
    resources: null,
    lastChecked: null,
    error: null,
    selectionWarning: '',
    asrSelectionWarning: '',
    requestId: 0,
    controller: null,
    debounceTimer: null,
    pollTimer: null,
    retryTimer: null,
    retryDelay: 3000,
    draftApiUrl: null,
    draftApiKey: null,
    voiceSetup: null,
    voiceSetupError: null,
    voiceSetupProgress: null,
    voiceSetupPollTimer: null,
    voiceSetupPollInFlight: false,
    voiceSetupRequestId: 0,
    loadPlan: null,
    loadPlanRequestId: 0,
    installPlans: {},
    installPlanErrors: {},
    installPlanRequests: new Set(),
    installGeneration: 0,
    installScope: null,
    installJob: null,
    installStartPending: null,
    installPollError: null,
    installPollTimer: null,
    installPollInFlight: false,
    resourcePollRequestId: 0,
    resourcePollInFlight: false,
    warmupRequestId: 0
};

function universalIsSelected() {
    return config.tts?.provider === 'universal';
}

function universalASRIsSelected() {
    return config.stt?.provider === 'universal';
}

function speechServerIsSelected() {
    return universalIsSelected() || universalASRIsSelected();
}

function universalStatusPresentation() {
    const state = universalSpeechState;
    if (state.status === 'connected') return ['Connected', 'The authenticated model registry is available.', 'success'];
    if (state.status === 'connecting') return ['Connecting…', 'Discovering models and server capabilities.', 'pending'];
    if (state.status === 'reconnecting') return ['Reconnecting…', 'Last-known settings remain visible but disabled.', 'pending'];
    if (state.status === 'incompatible') return ['Connected, no compatible models', state.error || 'No voice-cloning model supports the current game language.', 'warning'];
    if (state.status === 'stale') return ['Connection stale', state.error || 'The last-known model panel is disabled while Sonorus retries.', 'warning'];
    if (state.status === 'error') return ['Not connected', state.error || 'Check the URL and API key.', 'error'];
    return ['Not checked', 'Sonorus will connect automatically.', 'idle'];
}

function universalFriendlyConnectionDetail(detail) {
    const message = String(detail || '').trim();
    if (/networkerror when attempting to fetch resource|failed to fetch|load failed/i.test(message)) {
        return 'Unable to complete the speech-server connection check.';
    }
    return message;
}

function speechServerDownloadCTA() {
    return `<div class="universal-state-card universal-state-idle">
        <div class="universal-state-copy">
            <span class="universal-state-label">Get Universal Speech Server</span>
            <div class="universal-state-detail">Download the latest server release to host Voice and Speech recognition locally or on another machine.</div>
        </div>
        <a class="btn btn-primary btn-sm" href="${UNIVERSAL_SPEECH_SERVER_RELEASES_URL}" target="_blank" rel="noopener">Download server</a>
    </div>`;
}

function speechServerConnectionEditor() {
    const settings = config.speech_server || {};
    const apiUrl = settings.api_url || 'http://127.0.0.1:8100';
    const apiKey = settings.api_key ? '********' : '';
    const [title, rawDetail, tone] = universalStatusPresentation();
    const detail = universalFriendlyConnectionDetail(rawDetail);
    const stateMetadata = [];
    if (universalSpeechState.lastChecked) {
        stateMetadata.push(`Last checked ${new Date(universalSpeechState.lastChecked).toLocaleTimeString()}`);
    }
    if (['stale', 'reconnecting'].includes(universalSpeechState.status)) {
        stateMetadata.push('Retrying automatically');
    }
    return `<div class="field-group">
            <label class="field-label">API URL</label>
            <p class="field-hint">HTTP(S) base URL for the Universal Speech Server.</p>
            <input type="text" id="speech_server_api_url" value="${escapeHtml(apiUrl)}"
                   placeholder="http://127.0.0.1:8100"
                   oninput="onUniversalCredentialInput('api_url', this.value)">
        </div>
        <div class="field-group">
            <label class="field-label">API Key</label>
            <p class="field-hint">Optional Basic authentication key. Credentials are never returned by connection checks.</p>
            <input type="password" id="speech_server_api_key" value="${escapeHtml(apiKey)}"
                   placeholder="Optional Basic key" autocomplete="off" data-1p-ignore="true"
                   onfocus="if (this.value === '********') this.select()"
                   oninput="onUniversalCredentialInput('api_key', this.value)">
        </div>
        <div class="universal-state-card universal-state-${tone}" id="universalConnectionState" aria-live="polite">
            <div class="universal-state-copy">
                <span class="universal-state-label"><span class="universal-state-dot" aria-hidden="true"></span>${escapeHtml(title)}</span>
                <div class="universal-state-detail">${escapeHtml(detail)}</div>
                ${stateMetadata.length ? `<div class="universal-state-meta">${escapeHtml(stateMetadata.join(' · '))}</div>` : ''}
            </div>
            <button type="button" class="btn btn-sm" onclick="connectUniversalSpeechServer(true)">Refresh</button>
        </div>`;
}

function updateUniversalStateCard(card, title, detail, tone, stateMetadata = []) {
    if (!card) return;
    card.className = `universal-state-card universal-state-${tone}`;
    const label = card.querySelector('.universal-state-label');
    if (label) {
        const dot = label.querySelector('.universal-state-dot');
        label.replaceChildren();
        if (dot) label.appendChild(dot);
        label.appendChild(document.createTextNode(title));
    }
    const detailTarget = card.querySelector('.universal-state-detail');
    if (detailTarget) detailTarget.textContent = detail;

    const copy = card.querySelector('.universal-state-copy');
    let metadataTarget = card.querySelector('.universal-state-meta');
    if (stateMetadata.length) {
        if (!metadataTarget && copy) {
            metadataTarget = document.createElement('div');
            metadataTarget.className = 'universal-state-meta';
            copy.appendChild(metadataTarget);
        }
        if (metadataTarget) metadataTarget.textContent = stateMetadata.join(' · ');
    } else if (metadataTarget) {
        metadataTarget.remove();
    }
}

function updateUniversalConnectionState() {
    const [title, rawDetail, tone] = universalStatusPresentation();
    const stateMetadata = [];
    if (universalSpeechState.lastChecked) {
        stateMetadata.push(`Last checked ${new Date(universalSpeechState.lastChecked).toLocaleTimeString()}`);
    }
    if (['stale', 'reconnecting'].includes(universalSpeechState.status)) {
        stateMetadata.push('Retrying automatically');
    }
    updateUniversalStateCard(
        document.getElementById('universalConnectionState'),
        title,
        universalFriendlyConnectionDetail(rawDetail),
        tone,
        stateMetadata
    );
}

function universalPanelIsBeingEdited(panelId) {
    const panel = document.getElementById(panelId);
    const active = document.activeElement;
    return Boolean(
        panel && active && active !== document.body && panel.contains(active)
        && active.matches('input, textarea, select, [contenteditable="true"]')
    );
}

function refreshUniversalModelPanelIfIdle() {
    if (!universalPanelIsBeingEdited('universalModelPanel')) renderUniversalModelPanel();
}

function refreshUniversalASRModelPanelIfIdle() {
    if (!universalPanelIsBeingEdited('universalASRModelPanel')) renderUniversalASRModelPanel();
}

function refreshUniversalConnectionUI() {
    updateUniversalConnectionState();
    refreshUniversalModelPanelIfIdle();
    applySimpleMode();
}

function renderUniversalProviderSettings(container, providerConfig) {
    if (!universalIsSelected()) return;
    container.innerHTML = `
        <p class="field-hint" style="margin-bottom: var(--space-md);">${providerConfig.description}</p>
        ${speechServerDownloadCTA()}
        ${speechServerConnectionEditor()}
        <div id="universalModelPanel"></div>
        <div id="universalProviderWideSettings">
            ${providerConfig.fields.map(field => renderField(field, 'tts', 'universal')).join('')}
        </div>`;
    renderUniversalModelPanel();
    applySimpleMode();
}

function onUniversalCredentialInput(field, value) {
    if (field === 'api_url') universalSpeechState.draftApiUrl = value;
    if (field === 'api_key') universalSpeechState.draftApiKey = value;
    updateSetting(`speech_server.${field}`, value);
    clearTimeout(universalSpeechState.debounceTimer);
    universalSpeechState.debounceTimer = setTimeout(() => connectUniversalSpeechServer(false), 600);
}

function universalDraftPayload(extra = {}) {
    const settings = config.speech_server || {};
    return {
        api_url: universalSpeechState.draftApiUrl ?? document.getElementById('speech_server_api_url')?.value ?? settings.api_url ?? '',
        api_key: universalSpeechState.draftApiKey ?? document.getElementById('speech_server_api_key')?.value ?? (settings.api_key ? '********' : ''),
        game_language: config.setup?.language || 'EN_US',
        ...extra
    };
}

function universalInstallTargetKey(target) {
    return target ? `${target.component}:${target.model || ''}` : '';
}

function universalStackInstallTargets() {
    if ((universalSpeechState.capabilities?.capabilitiesVersion || 1) < 7) return [];
    const selection = universalStackSelection();
    const caps = universalSpeechState.capabilities || {};
    const targets = [];
    if (selection.tts_model) {
        const model = universalCompatibleModels().find(item => item.id === selection.tts_model);
        if (model && !model.installed) {
            targets.push({ component: 'model', model: model.id, label: `${model.name} TTS model`, installable: model.installable });
        }
    }
    if (selection.asr_model) {
        const model = universalCompatibleASRModels().find(item => item.id === selection.asr_model);
        if (model && !model.installed) {
            targets.push({ component: 'model', model: model.id, label: `${model.name} ASR model`, installable: model.installable });
        }
    }
    if (selection.upscale && caps.upscaler && !caps.upscaler.installed) {
        targets.push({ component: 'upscaler', model: null, label: 'VoxCPM2 AudioVAE', installable: caps.upscaler.installable });
    }
    if (selection.alignment && caps.alignment && !caps.alignment.installed) {
        targets.push({ component: 'aligner', model: null, label: 'CTC aligner', installable: caps.alignment.installable });
    }
    return targets;
}

function universalCurrentInstallTarget() {
    const job = universalSpeechState.installJob;
    if (job && !['completed', 'failed', 'cancelled'].includes(job.state)) {
        if (job.component.startsWith('model:')) {
            const modelId = job.component.slice('model:'.length);
            const tts = universalCompatibleModels().find(item => item.id === modelId);
            const asr = universalCompatibleASRModels().find(item => item.id === modelId);
            return {
                component: 'model', model: modelId,
                label: tts ? `${tts.name} TTS model` : asr ? `${asr.name} ASR model` : `${modelId} model`,
                installable: true,
            };
        }
        if (job.component === 'upscaler') {
            return { component: 'upscaler', model: null, label: 'VoxCPM2 AudioVAE', installable: true };
        }
        if (job.component === 'aligner') {
            return { component: 'aligner', model: null, label: 'CTC aligner', installable: true };
        }
    }
    return universalStackInstallTargets()[0] || null;
}

function universalRenderInstallSurfaces() {
    if (universalIsSelected()) refreshUniversalModelPanelIfIdle();
    if (universalASRIsSelected()) refreshUniversalASRModelPanelIfIdle();
}

async function refreshUniversalInstallPlan(target = universalCurrentInstallTarget()) {
    if (!target || universalSpeechState.status !== 'connected' || !target.installable) return;
    const key = universalInstallTargetKey(target);
    if (universalSpeechState.installPlans[key] || universalSpeechState.installPlanRequests.has(key)) return;
    universalSpeechState.installPlanRequests.add(key);
    const generation = universalSpeechState.installGeneration;
    delete universalSpeechState.installPlanErrors[key];
    universalRenderInstallSurfaces();
    try {
        const response = await fetch('/api/speech-server/install/plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload({
                component: target.component,
                ...(target.model ? { model: target.model } : {}),
            })),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.plan) {
            throw new Error(data.error?.message || `Install preview failed (HTTP ${response.status})`);
        }
        if (generation === universalSpeechState.installGeneration) {
            universalSpeechState.installPlans[key] = data.plan;
        }
    } catch (error) {
        if (generation === universalSpeechState.installGeneration) {
            universalSpeechState.installPlanErrors[key] = error.message || 'Unable to preview this download.';
        }
    } finally {
        if (generation === universalSpeechState.installGeneration) {
            universalSpeechState.installPlanRequests.delete(key);
            universalRenderInstallSurfaces();
        }
    }
}

function refreshUniversalStackInstallPlans() {
    return Promise.all(
        universalStackInstallTargets()
            .filter(target => target.installable)
            .map(target => refreshUniversalInstallPlan(target))
    );
}

function renderUniversalInstall(target) {
    if (!target) return '';
    const key = universalInstallTargetKey(target);
    const plan = universalSpeechState.installPlans[key];
    const error = universalSpeechState.installPlanErrors[key];
    const job = universalSpeechState.installJob?.component === (target.component === 'model'
        ? `model:${target.model}` : target.component) ? universalSpeechState.installJob : null;
    if (universalSpeechState.installStartPending === key) {
        return `<div class="universal-state-card universal-state-pending" aria-live="polite" aria-busy="true">
            <div class="universal-state-copy">
                <span class="universal-state-label">Starting ${escapeHtml(target.label)}</span>
                <div class="universal-state-detail">Creating the verified download job on the speech server&hellip;</div>
                <progress style="width:100%"></progress>
            </div>
        </div>`;
    }
    if (!target.installable) {
        return `<div class="universal-state-card universal-state-warning">
            <div class="universal-state-copy"><span class="universal-state-label">${escapeHtml(target.label)} is not installed</span>
            <div class="universal-state-detail">This server does not provide an automatic download for it.</div></div></div>`;
    }
    if (!plan && !error && !universalSpeechState.installPlanRequests.has(key)) {
        queueMicrotask(() => refreshUniversalInstallPlan(target));
    }
    if (job && !['completed', 'failed', 'cancelled'].includes(job.state)) {
        const downloaded = Number(job.downloadedBytes || 0);
        const total = Number(job.totalBytes || 0);
        const percent = total > 0 ? Math.min(100, downloaded / total * 100) : 0;
        const detail = total > 0
            ? `${formatUniversalBytes(downloaded)} of ${formatUniversalBytes(total)} (${percent.toFixed(0)}%)`
            : job.state === 'resolving' ? 'Resolving immutable download metadata…' : 'Preparing download…';
        const pollError = universalSpeechState.installPollError
            ? `<div class="universal-state-meta universal-warning">${escapeHtml(universalSpeechState.installPollError)} Reconnecting…</div>`
            : '';
        return `<div class="universal-state-card universal-state-pending" aria-live="polite">
            <div class="universal-state-copy"><span class="universal-state-label">Installing ${escapeHtml(target.label)}</span>
                <div class="universal-state-detail">${escapeHtml(detail)}</div>
                ${job.currentArtifact ? `<div class="universal-state-meta">${escapeHtml(job.currentArtifact)}</div>` : ''}
                ${pollError}
                <progress value="${downloaded}" ${total > 0 ? `max="${total}"` : ''} style="width:100%"></progress>
            </div>
            <button type="button" class="btn btn-sm" onclick="cancelUniversalInstall()">Cancel</button>
        </div>`;
    }
    if (error || job?.state === 'failed') {
        const message = job?.error?.message || error;
        return `<div class="universal-state-card universal-state-error"><div class="universal-state-copy">
            <span class="universal-state-label">Download unavailable</span><div class="universal-state-detail">${escapeHtml(message)}</div></div>
            <button type="button" class="btn btn-sm" onclick="retryUniversalInstallPlan()">Retry</button></div>`;
    }
    if (!plan) {
        return `<div class="universal-state-card universal-state-pending"><div class="universal-state-copy">
            <span class="universal-state-label">Checking ${escapeHtml(target.label)}</span>
            <div class="universal-state-detail">Locking the exact files and sizes before download.</div></div></div>`;
    }
    const approximateBytes = plan.artifacts.reduce((sum, item) => {
        const value = item.approximateSize;
        return typeof value === 'number' && Number.isFinite(value) ? sum + value : sum;
    }, 0);
    const approximateLabels = plan.artifacts
        .map(item => typeof item.approximateSize === 'string' ? item.approximateSize : null)
        .filter(Boolean);
    const size = plan.totalBytes ?? (approximateBytes || null);
    const sizeText = size
        ? `${plan.locked ? '' : 'About '}${formatUniversalBytes(size)}`
        : approximateLabels.length ? approximateLabels.join(' + ') : '';
    const fileCount = plan.artifacts.length;
    const acceptance = plan.requiresLicenseAcceptance ? `<label class="field-hint" style="display:block;margin-top:var(--space-sm)">
        <input type="checkbox" onchange="this.setCustomValidity('')"> I accept the ${escapeHtml(plan.license || 'model')} license for these files.
    </label>` : '';
    return `<div class="universal-state-card universal-state-warning"><div class="universal-state-copy">
        <span class="universal-state-label">Install ${escapeHtml(target.label)}</span>
        <div class="universal-state-detail">${sizeText ? `${escapeHtml(sizeText)} • ` : ''}${fileCount} file${fileCount === 1 ? '' : 's'} from the CrispASR registry • ${escapeHtml(plan.canonicalBackend)}${plan.license ? ` • ${escapeHtml(plan.license)}` : ''}.</div>
        <div class="universal-state-meta">Downloads are content-verified before activation.${plan.locked ? '' : ' Exact files are resolved only after license acceptance.'}</div>
        ${acceptance}</div>
        <button type="button" class="btn btn-primary btn-sm" onclick="startUniversalInstall(this)">Install</button></div>`;
}

function retryUniversalInstallPlan() {
    const target = universalCurrentInstallTarget();
    if (!target) return;
    const key = universalInstallTargetKey(target);
    delete universalSpeechState.installPlans[key];
    delete universalSpeechState.installPlanErrors[key];
    universalSpeechState.installJob = null;
    universalSpeechState.installStartPending = null;
    refreshUniversalInstallPlan(target);
}

async function startUniversalInstall(button) {
    const target = universalCurrentInstallTarget();
    const targetKey = universalInstallTargetKey(target);
    if (!target || universalSpeechState.installStartPending
        || universalSpeechState.installJob && !['completed', 'failed', 'cancelled'].includes(universalSpeechState.installJob.state)) return;
    const plan = universalSpeechState.installPlans[targetKey];
    const generation = universalSpeechState.installGeneration;
    if (!plan) return refreshUniversalInstallPlan(target);
    const acceptance = button?.closest('.universal-state-card')?.querySelector('input[type="checkbox"]');
    const acceptLicense = Boolean(acceptance?.checked);
    if (plan.requiresLicenseAcceptance && !acceptLicense) {
        acceptance?.setCustomValidity('Accept the model license before starting this download.');
        acceptance?.reportValidity();
        return;
    }
    universalSpeechState.installStartPending = targetKey;
    delete universalSpeechState.installPlanErrors[targetKey];
    universalRenderInstallSurfaces();
    try {
        const response = await fetch('/api/speech-server/install/start', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload({
                component: target.component, ...(target.model ? { model: target.model } : {}),
                accept_license: acceptLicense,
            })),
        });
        const data = await response.json().catch(() => ({}));
        if (generation !== universalSpeechState.installGeneration) return;
        if (!response.ok || !data.job) throw new Error(data.error?.message || `Install failed (HTTP ${response.status})`);
        universalSpeechState.installStartPending = null;
        universalSpeechState.installJob = data.job;
        universalSpeechState.installPollError = null;
        universalRenderInstallSurfaces();
        if (!['completed', 'failed', 'cancelled'].includes(data.job.state)) {
            scheduleUniversalInstallPoll();
        }
    } catch (error) {
        if (generation !== universalSpeechState.installGeneration) return;
        universalSpeechState.installStartPending = null;
        universalSpeechState.installPlanErrors[targetKey] = error.message || 'Unable to start the download.';
        universalRenderInstallSurfaces();
    }
}

function scheduleUniversalInstallPoll() {
    clearInterval(universalSpeechState.installPollTimer);
    universalSpeechState.installPollTimer = setInterval(pollUniversalInstall, 750);
    pollUniversalInstall();
}

async function pollUniversalInstall() {
    const jobId = universalSpeechState.installJob?.jobId;
    if (!jobId || universalSpeechState.installPollInFlight) return;
    universalSpeechState.installPollInFlight = true;
    const generation = universalSpeechState.installGeneration;
    try {
        const response = await fetch('/api/speech-server/install/status', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload({ job_id: jobId })),
        });
        const data = await response.json().catch(() => ({}));
        if (generation !== universalSpeechState.installGeneration) return;
        if (!response.ok || !data.job) throw new Error(data.error?.message || `Install status failed (HTTP ${response.status})`);
        universalSpeechState.installJob = data.job;
        universalSpeechState.installPollError = null;
        if (data.job.state === 'completed') {
            clearInterval(universalSpeechState.installPollTimer);
            universalSpeechState.installPollTimer = null;
            if (data.capabilities) universalSpeechState.capabilities = data.capabilities;
            if ('resources' in data) universalSpeechState.resources = data.resources;
            universalSpeechState.loadPlan = null;
            universalRenderInstallSurfaces();
            if (universalIsSelected()) refreshUniversalVoiceSetupStatus();
            refreshUniversalLoadPlan();
            refreshUniversalStackInstallPlans();
        } else if (['failed', 'cancelled'].includes(data.job.state)) {
            clearInterval(universalSpeechState.installPollTimer);
            universalSpeechState.installPollTimer = null;
        }
        universalRenderInstallSurfaces();
    } catch (error) {
        if (generation !== universalSpeechState.installGeneration) return;
        clearInterval(universalSpeechState.installPollTimer);
        universalSpeechState.installPollTimer = null;
        universalSpeechState.installPollError = error.message || 'Installation progress is temporarily unavailable.';
        universalRenderInstallSurfaces();
        scheduleUniversalReconnect();
    } finally {
        if (generation === universalSpeechState.installGeneration) {
            universalSpeechState.installPollInFlight = false;
        }
    }
}

async function cancelUniversalInstall() {
    const jobId = universalSpeechState.installJob?.jobId;
    if (!jobId) return;
    const generation = universalSpeechState.installGeneration;
    try {
        const response = await fetch('/api/speech-server/install/cancel', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload({ job_id: jobId })),
        });
        const data = await response.json().catch(() => ({}));
        if (generation !== universalSpeechState.installGeneration) return;
        if (!response.ok || !data.job) throw new Error(data.error?.message || 'Unable to cancel the download.');
        universalSpeechState.installJob = data.job;
        universalSpeechState.installPollError = null;
        universalRenderInstallSurfaces();
        if (!['completed', 'failed', 'cancelled'].includes(data.job.state)) {
            scheduleUniversalInstallPoll();
        }
    } catch (error) {
        if (generation !== universalSpeechState.installGeneration) return;
        universalSpeechState.installPollError = error.message || 'Unable to cancel the download.';
        universalRenderInstallSurfaces();
    }
}

function clearUniversalTimers({ keepPoll = false } = {}) {
    clearTimeout(universalSpeechState.retryTimer);
    universalSpeechState.retryTimer = null;
    if (!keepPoll && universalSpeechState.pollTimer) {
        clearInterval(universalSpeechState.pollTimer);
        universalSpeechState.pollTimer = null;
    }
    if (universalSpeechState.voiceSetupPollTimer) {
        clearInterval(universalSpeechState.voiceSetupPollTimer);
        universalSpeechState.voiceSetupPollTimer = null;
    }
    if (universalSpeechState.installPollTimer) {
        clearInterval(universalSpeechState.installPollTimer);
        universalSpeechState.installPollTimer = null;
    }
}

function suspendUniversalUiWork() {
    clearUniversalTimers();
    clearTimeout(universalSpeechState.debounceTimer);
    universalSpeechState.debounceTimer = null;
    if (universalSpeechState.controller) {
        universalSpeechState.controller.abort();
        universalSpeechState.controller = null;
    }
    // Invalidate requests without discarding last-known capabilities. Switching
    // back can reconnect from that stale-but-useful state.
    universalSpeechState.requestId += 1;
    universalSpeechState.voiceSetupRequestId += 1;
    universalSpeechState.loadPlanRequestId += 1;
    universalSpeechState.resourcePollRequestId += 1;
    universalSpeechState.resourcePollInFlight = false;
    universalSpeechState.warmupRequestId += 1;
    universalSpeechState.installPollInFlight = false;
}

function scheduleUniversalReconnect() {
    if (!speechServerIsSelected() || universalSpeechState.retryTimer) return;
    const delay = universalSpeechState.retryDelay;
    universalSpeechState.retryTimer = setTimeout(() => {
        universalSpeechState.retryTimer = null;
        connectUniversalSpeechServer(false);
    }, delay);
    universalSpeechState.retryDelay = Math.min(delay * 2, 30000);
}

async function connectUniversalSpeechServer(refresh = false) {
    if (!speechServerIsSelected()) return;
    const forceDiscovery = refresh || universalSpeechState.status === 'stale'
        || !universalSpeechState.capabilities;
    const draftPayload = universalDraftPayload({ refresh: forceDiscovery });
    const installScope = `${draftPayload.api_url}\n${draftPayload.api_key}`;
    const activeInstall = universalSpeechState.installJob
        && !['completed', 'failed', 'cancelled'].includes(universalSpeechState.installJob.state);
    if (universalSpeechState.installScope !== installScope || (forceDiscovery && !activeInstall)) {
        universalSpeechState.installPlans = {};
        universalSpeechState.installPlanErrors = {};
        universalSpeechState.installPlanRequests.clear();
        universalSpeechState.installGeneration += 1;
        universalSpeechState.installStartPending = null;
        if (universalSpeechState.installScope !== installScope) universalSpeechState.installJob = null;
        if (universalSpeechState.installScope !== installScope) universalSpeechState.installPollError = null;
        universalSpeechState.installScope = installScope;
    }
    clearUniversalTimers();
    universalSpeechState.voiceSetupRequestId += 1;
    universalSpeechState.loadPlanRequestId += 1;
    universalSpeechState.resourcePollRequestId += 1;
    universalSpeechState.resourcePollInFlight = false;
    universalSpeechState.warmupRequestId += 1;
    if (universalSpeechState.controller) universalSpeechState.controller.abort();
    const requestId = ++universalSpeechState.requestId;
    const controller = new AbortController();
    universalSpeechState.controller = controller;
    universalSpeechState.status = universalSpeechState.capabilities ? 'reconnecting' : 'connecting';
    universalSpeechState.error = null;
    const container = document.getElementById('ttsProviderSettings');
    if (container && universalIsSelected()) refreshUniversalConnectionUI();
    if (universalASRIsSelected()) refreshUniversalASRConnectionUI();

    try {
        const response = await fetch('/api/speech-server/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(draftPayload),
            signal: controller.signal
        });
        const responseBody = await response.text();
        let data = {};
        try {
            data = JSON.parse(responseBody);
        } catch {
            throw new Error('The Sonorus connection proxy returned a malformed response.');
        }
        if (requestId !== universalSpeechState.requestId || !speechServerIsSelected()) return;

        if (!response.ok) {
            const connectedDetails = data.error?.details;
            if (data.error?.code === 'no_compatible_models' && connectedDetails?.capabilities) {
                universalSpeechState.capabilities = connectedDetails.capabilities;
                universalSpeechState.resources = connectedDetails.resources || null;
                universalSpeechState.status = 'incompatible';
                universalSpeechState.error = data.error.message;
                universalSpeechState.lastChecked = Date.now();
                if (container && universalIsSelected()) refreshUniversalConnectionUI();
                if (universalASRIsSelected()) refreshUniversalASRConnectionUI();
                refreshUniversalOverrideAutocompletes();
                refreshSetupStateFromConfig();
                return;
            }
            throw new Error(data.error?.message || `Connection failed (HTTP ${response.status})`);
        }

        if (!data.capabilities || !Array.isArray(data.capabilities.compatibleModels)
            || !Array.isArray(data.capabilities.compatibleASRModels)) {
            throw new Error('The speech server returned an incomplete capability response.');
        }
        universalSpeechState.capabilities = data.capabilities;
        universalSpeechState.resources = data.resources || null;
        const advertisedJobs = Array.isArray(data.capabilities.activeInstallations)
            ? data.capabilities.activeInstallations : [];
        const localActive = universalSpeechState.installJob
            && !['completed', 'failed', 'cancelled'].includes(universalSpeechState.installJob.state);
        if (localActive) {
            universalSpeechState.installJob = advertisedJobs.find(
                job => job.jobId === universalSpeechState.installJob.jobId
            ) || null;
        } else {
            universalSpeechState.installJob = advertisedJobs[0] || universalSpeechState.installJob;
        }
        universalSpeechState.loadPlan = null;
        universalSpeechState.lastChecked = Date.now();
        universalSpeechState.status = 'connected';
        universalSpeechState.error = data.resourceError?.message || null;
        universalSpeechState.retryDelay = 3000;
        universalSpeechState.voiceSetup = null;
        universalSpeechState.voiceSetupError = null;
        universalSpeechState.installPollError = null;
        if (universalIsSelected()) ensureUniversalSelection();
        if (universalASRIsSelected()) ensureUniversalASRSelection();
        if (container && universalIsSelected()) refreshUniversalConnectionUI();
        if (universalASRIsSelected()) refreshUniversalASRConnectionUI();
        refreshUniversalOverrideAutocompletes();
        startUniversalResourcePolling();
        if (universalIsSelected()) refreshUniversalVoiceSetupStatus();
        refreshUniversalLoadPlan();
        refreshUniversalStackInstallPlans();
        if (universalSpeechState.installJob
            && !['completed', 'failed', 'cancelled'].includes(universalSpeechState.installJob.state)) {
            scheduleUniversalInstallPoll();
        }
        refreshSetupStateFromConfig();
    } catch (error) {
        if (error.name === 'AbortError' || requestId !== universalSpeechState.requestId
            || !speechServerIsSelected()) return;
        universalSpeechState.status = universalSpeechState.capabilities ? 'stale' : 'error';
        universalSpeechState.error = error.message || 'Connection failed.';
        universalSpeechState.lastChecked = Date.now();
        if (container && universalIsSelected()) refreshUniversalConnectionUI();
        if (universalASRIsSelected()) refreshUniversalASRConnectionUI();
        scheduleUniversalReconnect();
        refreshSetupStateFromConfig();
    }
}

function universalCompatibleModels() {
    return universalSpeechState.capabilities?.compatibleModels || [];
}

function ensureUniversalSelection() {
    const models = universalCompatibleModels();
    if (!models.length) return;
    const settings = config.tts.universal ||= {};
    const validIds = new Set(models.map(model => model.id));
    if (!validIds.has(settings.model)) {
        const previous = settings.model;
        settings.model = universalSpeechState.capabilities.recommendedModelId || models[0].id;
        universalSpeechState.selectionWarning = previous
            ? `Saved model “${previous}” is no longer compatible. ${settings.model} is selected as a draft; save to confirm.`
            : `${settings.model} was selected from the connected server; save to confirm.`;
        markDirty();
    } else {
        universalSpeechState.selectionWarning = '';
    }
    ensureUniversalModelProfile(settings.model);
}

function ensureUniversalModelProfile(modelId) {
    const settings = config.tts.universal ||= {};
    const profiles = settings.model_settings ||= {};
    const model = universalCompatibleModels().find(item => item.id === modelId);
    if (!model) return null;
    let changed = false;
    if (!profiles[modelId] || typeof profiles[modelId] !== 'object') {
        profiles[modelId] = { options: {} };
        changed = true;
    }
    const profile = profiles[modelId];
    profile.options ||= {};
    for (const control of model.controls || []) {
        if (!(control.id in profile.options)) {
            profile.options[control.id] = model.defaults?.options?.[control.id] ?? control.default;
            changed = true;
        }
    }
    if (model.upscaleEligible) {
        if (!('upscale' in profile)) {
            profile.upscale = true;
            changed = true;
        }
    } else if (profile.upscale === true) {
        profile.upscale = false;
        changed = true;
    }
    const batchingEligible = !!(model.segmentation && model.alignmentCompatible
        && universalSpeechState.capabilities?.capabilitiesVersion >= 3);
    if (batchingEligible && !('adaptive_batching' in profile)) {
        profile.adaptive_batching = model.defaults?.adaptive_batching !== false;
        changed = true;
    }
    if (changed) markDirty();
    return profile;
}

function formatUniversalBytes(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'Unavailable';
    const gib = Number(value) / (1024 ** 3);
    return gib >= 10 ? `${gib.toFixed(1)} GiB` : `${gib.toFixed(2)} GiB`;
}

const UNIVERSAL_ESTIMATE_QUANTUM_BYTES = 256 * 1024 * 1024;

function universalRequirementEstimate(model, kind) {
    const advertised = model?.resources?.[kind];
    const advertisedBytes = advertised?.estimatedBytes;
    if (advertisedBytes !== null && advertisedBytes !== undefined
        && Number.isFinite(Number(advertisedBytes)) && Number(advertisedBytes) >= 0) {
        return {
            bytes: Number(advertisedBytes),
            source: advertised.source || 'server',
            confidence: advertised.confidence || 'unknown',
        };
    }

    // Before installation there are no local component files for the server's
    // normal file-size heuristic.  A locked install plan has the exact complete
    // bundle size, so apply the same conservative 2x / 256-MiB rounding used by
    // ModelSpec.resource_requirements().  Keep it explicitly low confidence:
    // download bytes are not a measurement of runtime residency.
    const plan = model?.id
        ? universalSpeechState.installPlans[`model:${model.id}`]
        : null;
    const bundleBytes = plan?.locked ? Number(plan.totalBytes) : NaN;
    if (Number.isFinite(bundleBytes) && bundleBytes > 0) {
        return {
            bytes: Math.ceil((bundleBytes * 2) / UNIVERSAL_ESTIMATE_QUANTUM_BYTES)
                * UNIVERSAL_ESTIMATE_QUANTUM_BYTES,
            source: 'locked-download-size-heuristic',
            confidence: 'low',
        };
    }
    return null;
}

function universalEstimateText(model) {
    const ram = universalRequirementEstimate(model, 'ram');
    const vram = universalRequirementEstimate(model, 'vram');
    const estimate = value => value ? `~${formatUniversalBytes(value.bytes)}` : 'Unavailable';
    return `${estimate(ram)} RAM / ${estimate(vram)} VRAM`;
}

function universalEstimateSource(model) {
    const sources = ['ram', 'vram']
        .map(kind => universalRequirementEstimate(model, kind))
        .filter(Boolean);
    if (!sources.length) return 'estimate unavailable';
    if (sources.some(value => value.source === 'locked-download-size-heuristic')) {
        return 'low-confidence locked-download-size estimate';
    }
    return sources.every(value => value.source === 'registry')
        ? 'registry estimate'
        : 'low-confidence file-size estimate';
}

function universalFitTone(status) {
    return ['comfortable', 'tight', 'insufficient', 'busy'].includes(status) ? status : 'unknown';
}

function universalModelBadges(model) {
    return `${model?.recommended ? '<span class="universal-badge universal-badge-recommended">Recommended</span>' : ''}
        ${model?.loaded ? '<span class="universal-badge universal-badge-loaded">TTS loaded</span>' : ''}
        ${model?.installed === false ? '<span class="universal-badge">Download required</span>' : ''}
        ${model?.upscaleEligible ? '<span class="universal-badge universal-badge-upscaler">48 kHz upscale</span>' : ''}`;
}

const UNIVERSAL_ALIGNMENT_VRAM_OVERHEAD_BYTES = 500 * 1024 * 1024;
const UNIVERSAL_UPSCALER_VRAM_OVERHEAD_BYTES = 500 * 1024 * 1024;

function universalLoadPlanMatches(plan, model, profile) {
    const batching = !!(profile?.adaptive_batching && model?.segmentation
        && model?.alignmentCompatible);
    if (Array.isArray(plan?.desiredModels)) {
        const ids = new Set(plan.desiredModels.map(item => item.id));
        const expected = universalStackSelection();
        const expectedIds = [expected.tts_model, expected.asr_model].filter(Boolean);
        return ids.size === expectedIds.length
            && expectedIds.every(id => ids.has(id))
            && plan.upscale === expected.upscale
            && plan.alignment === expected.alignment;
    }
    return !!(plan && plan.modelId === model?.id
        && plan.upscale === !!profile?.upscale
        && plan.adaptiveBatching === batching);
}

function universalStackSelection() {
    const ttsModel = universalIsSelected() ? config.tts?.universal?.model || null : null;
    const asrModel = universalASRIsSelected() ? config.stt?.universal?.model || null : null;
    const model = ttsModel
        ? universalCompatibleModels().find(item => item.id === ttsModel)
        : null;
    const profile = model ? ensureUniversalModelProfile(model.id) : null;
    const alignment = !!(profile?.adaptive_batching && model?.segmentation
        && model?.alignmentCompatible);
    return {
        tts_model: ttsModel,
        asr_model: asrModel,
        upscale: !!(model && profile?.upscale),
        alignment,
        model,
        profile,
    };
}

function universalStackPayload(selection = universalStackSelection()) {
    return {
        tts_model: selection.tts_model,
        asr_model: selection.asr_model,
        upscale: selection.upscale,
        alignment: selection.alignment,
    };
}

function universalSelectedStackLoaded(model, profile) {
    const resources = universalSpeechState.resources;
    if (!resources) return false;
    const selection = universalStackSelection();
    if (!selection.tts_model && !selection.asr_model) return false;
    const batching = !!(profile?.adaptive_batching && model?.segmentation
        && model?.alignmentCompatible);
    return (!selection.tts_model || (resources.loadedModelIds || []).includes(selection.tts_model))
        && (!selection.asr_model || (resources.loadedModelIds || []).includes(selection.asr_model))
        && (!profile?.upscale || resources.upscalerLoaded)
        && (!batching || resources.alignerLoaded);
}

function universalMissingStackComponents(model, profile) {
    const resources = universalSpeechState.resources;
    const selection = universalStackSelection();
    if (!resources) {
        return [
            ...(selection.tts_model ? ['TTS model'] : []),
            ...(selection.asr_model ? ['ASR model'] : []),
        ];
    }
    const missing = [];
    if (selection.tts_model && !(resources.loadedModelIds || []).includes(selection.tts_model)) {
        missing.push('TTS model');
    }
    if (selection.asr_model && !(resources.loadedModelIds || []).includes(selection.asr_model)) {
        missing.push('ASR model');
    }
    if (profile?.upscale && !resources.upscalerLoaded) missing.push('AudioVAE');
    const batching = !!(profile?.adaptive_batching && model?.segmentation
        && model?.alignmentCompatible);
    if (batching && !resources.alignerLoaded) missing.push('CTC');
    return missing;
}

function universalLoadButtonLabel(missing) {
    const selection = universalStackSelection();
    if (selection.tts_model && selection.asr_model) return 'Load selected speech stack';
    if (missing.length === 1 && missing[0] === 'TTS model') return 'Load selected TTS model';
    if (missing.length === 1 && missing[0] === 'ASR model') return 'Load selected ASR model';
    if (missing.includes('TTS model') || missing.includes('ASR model')) {
        return 'Load selected speech stack';
    }
    return `Load ${missing.join(' + ')}`;
}

function universalResidentCapacityOk() {
    const selection = universalStackSelection();
    const required = [selection.tts_model, selection.asr_model].filter(Boolean).length;
    const advertised = Number(universalSpeechState.capabilities?.residentLimit || 1);
    if (required > advertised) return false;
    return universalSpeechState.loadPlan?.residentCapacitySatisfied !== false;
}

function universalResidentLimit() {
    return Number(
        universalSpeechState.loadPlan?.residentLimit
        ?? universalSpeechState.capabilities?.residentLimit
        ?? 1
    );
}

function universalMissingStackDetail(missing) {
    const options = missing.filter(component => !['TTS model', 'ASR model'].includes(component));
    if (missing.includes('TTS model') || missing.includes('ASR model') || !options.length) return '';
    return `TTS model loaded; ${options.join(' and ')} ${options.length === 1 ? 'is' : 'are'} still not loaded.`;
}

function universalFitNote(fit) {
    const displayNames = ids => ids.map(id =>
        universalSpeechState.capabilities?.models?.find(model => model.id === id)?.name
        || id
    ).join(', ');
    if (fit?.busyIds?.length) {
        return `Waiting for ${displayNames(fit.busyIds)} to finish speaking`;
    }
    if (fit?.evictIds?.length) {
        return `Projected after replacing ${displayNames(fit.evictIds)}`;
    }
    if (fit?.allResident) return 'Selected option stack is already loaded';
    const resources = universalSpeechState.resources;
    const ramFree = resources?.ram?.freeBytes;
    const gpuFree = (resources?.gpus || [])
        .map(gpu => gpu?.freeBytes)
        .filter(value => value !== null && value !== undefined
            && Number.isFinite(Number(value)))
        .map(Number);
    const available = [];
    if (ramFree !== null && ramFree !== undefined && Number.isFinite(Number(ramFree))) {
        available.push(`${formatUniversalBytes(ramFree)} RAM`);
    }
    if (gpuFree.length) available.push(`${formatUniversalBytes(Math.max(...gpuFree))} VRAM`);
    return available.length
        ? `Compared with ${available.join(' / ')} free`
        : 'Free RAM/VRAM telemetry unavailable';
}

function calculateUniversalFit(model, profile) {
    const resources = universalSpeechState.resources;
    if (!resources) return { status: 'unknown', ratio: null };
    const serverPlan = universalSpeechState.loadPlan;
    if (universalLoadPlanMatches(serverPlan, model, profile)) {
        return {
            status: serverPlan.fit?.status || 'unknown',
            ratio: serverPlan.fit?.ratio ?? null,
            evictIds: (serverPlan.evict || []).map(item => item.id),
            busyIds: (serverPlan.busy || []).map(item => item.id),
            allResident: universalSelectedStackLoaded(model, profile),
            serverPlan: true,
        };
    }
    // Only the capability-v6 stack planner can account for a simultaneously
    // selected ASR model and its steady-state evictions without double counting.
    // Do not briefly show a confident TTS-only result while that plan is pending.
    if (universalASRIsSelected()) return { status: 'unknown', ratio: null };
    const loaded = new Set(resources.loadedModelIds || []);
    const upscaler = universalSpeechState.capabilities?.upscaler;
    const residentLimit = Math.max(1, Number(
        universalSpeechState.capabilities?.residentLimit || 1
    ));
    const residentModels = (resources.components || []).filter(component =>
        component?.kind === 'model' && component.loaded);
    const evictionsNeeded = loaded.has(model.id)
        ? 0 : Math.max(0, residentModels.length + 1 - residentLimit);
    const victims = residentModels.filter(component => component.evictable)
        .slice(0, evictionsNeeded);
    const busyIds = evictionsNeeded > victims.length
        ? residentModels.filter(component => component.busy)
            .slice(0, evictionsNeeded - victims.length).map(component => component.id)
        : [];
    const ratios = [];
    const requirement = kind => {
        let bytes = loaded.has(model.id)
            ? 0
            : universalRequirementEstimate(model, kind)?.bytes;
        if (bytes === null || bytes === undefined || !Number.isFinite(Number(bytes))) return null;
        if (profile?.upscale && !resources.upscalerLoaded) {
            const upscaleBytes = upscaler?.resources?.[kind]?.estimatedBytes;
            if (kind === 'vram') {
                const measured = Number.isFinite(Number(upscaleBytes))
                    ? Number(upscaleBytes) : 0;
                bytes += Math.max(measured, UNIVERSAL_UPSCALER_VRAM_OVERHEAD_BYTES);
            } else {
                if (upscaleBytes === null || upscaleBytes === undefined
                    || !Number.isFinite(Number(upscaleBytes))) return null;
                bytes += Number(upscaleBytes);
            }
        }
        const batchingEffective = !!(profile?.adaptive_batching && model.segmentation
            && model.alignmentCompatible);
        if (batchingEffective && !resources.alignerLoaded) {
            const advertisedBytes = universalSpeechState.capabilities?.alignment
                ?.resources?.[kind]?.estimatedBytes;
            if (kind === 'vram') {
                const measured = Number.isFinite(Number(advertisedBytes))
                    ? Number(advertisedBytes) : 0;
                bytes += Math.max(measured, UNIVERSAL_ALIGNMENT_VRAM_OVERHEAD_BYTES);
            } else {
                if (advertisedBytes === null || advertisedBytes === undefined
                    || !Number.isFinite(Number(advertisedBytes))) return null;
                bytes += Number(advertisedBytes);
            }
        }
        return Number(bytes);
    };
    const ramRequired = requirement('ram');
    const reclaimable = kind => victims.reduce((total, component) => {
        const value = component?.resources?.[kind]?.estimatedBytes;
        return total + (Number.isFinite(Number(value)) ? Number(value) : 0);
    }, 0);
    if (ramRequired !== null && resources.ram?.freeBytes !== null
        && resources.ram?.freeBytes !== undefined && Number.isFinite(Number(resources.ram.freeBytes))) {
        ratios.push(ramRequired === 0 ? Infinity
            : (Number(resources.ram.freeBytes) + reclaimable('ram')) / ramRequired);
    }
    const gpuFreeValues = (resources.gpus || [])
        .map(gpu => gpu?.freeBytes)
        .filter(value => value !== null && value !== undefined && Number.isFinite(Number(value)))
        .map(Number);
    const bestGpuFree = gpuFreeValues.length ? Math.max(...gpuFreeValues) : null;
    const vramRequired = requirement('vram');
    if (vramRequired !== null && bestGpuFree !== null) {
        ratios.push(vramRequired === 0 ? Infinity
            : (bestGpuFree + reclaimable('vram')) / vramRequired);
    }
    if (busyIds.length) {
        return {
            status: 'busy', ratio: null,
            evictIds: victims.map(component => component.id), busyIds,
            allResident: false,
        };
    }
    if (!ratios.length) return { status: 'unknown', ratio: null };
    const ratio = Math.min(...ratios);
    return {
        status: ratio >= 1.5 ? 'comfortable' : ratio >= 1 ? 'tight' : 'insufficient',
        ratio,
        evictIds: victims.map(component => component.id),
        busyIds: [],
        allResident: universalSelectedStackLoaded(model, profile),
    };
}

function refreshUniversalFits() {
    for (const model of universalCompatibleModels()) {
        const saved = config.tts?.universal?.model_settings?.[model.id];
        const profile = saved || model.defaults || {};
        model.loaded = (universalSpeechState.resources?.loadedModelIds || []).includes(model.id);
        model.fit = calculateUniversalFit(model, profile);
    }
}

function renderUniversalModelPanel() {
    const panel = document.getElementById('universalModelPanel');
    if (!panel) return;
    const caps = universalSpeechState.capabilities;
    if (!caps) {
        panel.innerHTML = '';
        return;
    }
    const models = caps.compatibleModels || [];
    if (!models.length) {
        panel.innerHTML = '<div class="universal-empty-state">The server is connected, but no voice-cloning model supports the current game language.</div>';
        return;
    }
    const selectedId = config.tts?.universal?.model || caps.recommendedModelId || models[0].id;
    const model = models.find(item => item.id === selectedId) || models[0];
    const profile = ensureUniversalModelProfile(model.id);
    refreshUniversalFits();
    const disabled = universalSpeechState.status !== 'connected';
    const outputRate = profile?.upscale && caps.upscaler ? caps.upscaler.sampleRate : model.sampleRate;
    const controls = (model.controls || []).map(control => {
        const value = profile?.options?.[control.id] ?? control.default;
        return `<div class="field-group universal-dynamic-control">
            <label class="field-label">${escapeHtml(control.label || control.id)}</label>
            <div class="range-wrapper">
                <input type="range" min="${control.minimum}" max="${control.maximum}" step="${control.step}"
                       value="${value}" ${disabled ? 'disabled' : ''}
                       oninput="updateUniversalModelOption('${escapeHtml(control.id)}', this.value, '${control.type}'); this.nextElementSibling.textContent = this.value">
                <span class="range-value">${value}</span>
            </div>
        </div>`;
    }).join('');
    const upscale = model.upscaleEligible ? `
        <div class="toggle-wrapper">
            <div>
                <span class="toggle-label">Upscale to ${Number(caps.upscaler.sampleRate / 1000).toFixed(0)} kHz</span>
                <p class="field-hint">Uses VoxCPM2 AudioVAE V2 super-resolution to render lower-rate speech at 48 kHz and automatically applies smoothing EQ. Adds about 500 MB of VRAM overhead.</p>
            </div>
            <label class="toggle"><input type="checkbox" ${profile?.upscale ? 'checked' : ''} ${disabled ? 'disabled' : ''}
                onchange="updateUniversalProfileValue('upscale', this.checked, true)">
                <span class="toggle-track"><span class="toggle-thumb"></span></span></label>
        </div>` : '';
    const batchingEligible = !!(model.segmentation && model.alignmentCompatible
        && caps.capabilitiesVersion >= 3);
    const batching = model.segmentation ? `
        <div class="toggle-wrapper field-group" data-simple-hide="true">
            <div>
                <span class="toggle-label">Adaptive sentence batching</span>
                <p class="field-hint">Produces the best multi-sentence speech quality, but adds about 500 MB of VRAM overhead.${batchingEligible ? '' : ' Unavailable for the current language or server.'}</p>
            </div>
            <label class="toggle"><input type="checkbox" ${batchingEligible && profile?.adaptive_batching ? 'checked' : ''}
                ${disabled || !batchingEligible ? 'disabled' : ''}
                onchange="updateUniversalProfileValue('adaptive_batching', this.checked, true)">
                <span class="toggle-track"><span class="toggle-thumb"></span></span></label>
        </div>` : '';
    const voiceSetup = model.installed === false ? '' : renderUniversalVoiceSetup(model, disabled);
    const fit = calculateUniversalFit(model, profile);
    const fitTone = universalFitTone(fit.status);
    const stackLoaded = universalSelectedStackLoaded(model, profile);
    const missingStack = universalMissingStackComponents(model, profile);
    const missingStackDetail = universalMissingStackDetail(missingStack);
    const installTarget = universalCurrentInstallTarget();
    const installCard = renderUniversalInstall(installTarget);
    panel.innerHTML = `<fieldset class="universal-model-fieldset" ${disabled ? 'disabled' : ''}>
        <div class="field-group">
            <label class="field-label">Speech Model</label>
            <p class="field-hint">Search by model, backend, or language. Only compatible voice-cloning models are listed.</p>
            <div class="model-autocomplete-combobox universal-model-combobox">
                <input type="text" id="universalModelInput" value="${escapeHtml(model.name)}" autocomplete="off">
                <button type="button" class="model-autocomplete-dropdown-btn" aria-label="Browse speech models" ${disabled ? 'disabled' : ''}>&#9662;</button>
            </div>
            ${universalSpeechState.selectionWarning ? `<p class="field-hint universal-warning">${escapeHtml(universalSpeechState.selectionWarning)}</p>` : ''}
        </div>
        <div class="universal-model-summary universal-model-card">
            <div class="universal-model-card-header">
                <strong class="universal-model-card-name">${escapeHtml(model.name)}</strong>
                <span class="universal-badge-row">${universalModelBadges(model)}</span>
            </div>
            <div class="universal-model-description">${escapeHtml(model.description || 'Server-provided model.')}</div>
            <div class="universal-model-spec-grid">
                <div class="universal-model-spec universal-model-spec-rate">
                    <span class="universal-model-spec-label">Audio</span>
                    <strong>${Number(model.sampleRate / 1000).toFixed(1)} kHz <span aria-hidden="true">→</span> ${Number(outputRate / 1000).toFixed(1)} kHz</strong>
                    <span class="universal-model-spec-note">Native → output</span>
                </div>
                <div class="universal-model-spec universal-model-spec-memory">
                    <span class="universal-model-spec-label">Estimated requirement</span>
                    <strong>${escapeHtml(universalEstimateText(model))}</strong>
                    <span class="universal-model-spec-note">${escapeHtml(universalEstimateSource(model))}</span>
                </div>
                <div class="universal-model-spec universal-model-spec-fit">
                    <span class="universal-model-spec-label">Hardware fit</span>
                    <strong id="universalSelectedFit" class="universal-fit-badge universal-fit-${fitTone}">${escapeHtml(fit.status)}</strong>
                    <span class="universal-model-spec-note" id="universalSelectedFitNote">${escapeHtml(universalFitNote(fit))}</span>
                </div>
            </div>
        </div>
        ${voiceSetup}
        ${controls}${upscale}${batching}
        ${installCard}
        <div class="universal-model-actions">
            <button type="button" class="btn btn-sm" onclick="resetUniversalModelProfile()">${model.recommended ? 'Reset to recommended' : 'Reset to server defaults'}</button>
            <button type="button" class="btn btn-primary btn-sm" id="universalWarmupButton"
                onclick="warmupUniversalModel()" ${stackLoaded || installTarget ? 'hidden' : ''}>${universalLoadButtonLabel(missingStack)}</button>
            <span id="universalWarmupLoaded" class="universal-fit-badge universal-fit-comfortable"
                ${stackLoaded ? '' : 'hidden'}>Selected stack loaded</span>
        </div>
        <p class="field-hint universal-stack-load-detail" id="universalStackLoadDetail"
            ${missingStackDetail ? '' : 'hidden'}>${missingStackDetail}</p>
        <p class="field-hint" id="universalWarmupStatus"></p>
        <div id="universalRemoteResources" class="universal-resource-grid"></div>
    </fieldset>`;
    initializeUniversalModelAutocomplete();
    updateUniversalResourceDisplay();
    applySimpleMode();
}

function initializeUniversalModelAutocomplete() {
    const input = document.getElementById('universalModelInput');
    if (!input || !window.Awesomplete) return;
    const models = universalCompatibleModels();
    const lookup = new Map(models.map(model => [model.id, model]));
    const awesomplete = new Awesomplete(input, {
        list: models.map(model => ({ label: model.name, value: model.id })),
        minChars: 0, maxItems: 20, autoFirst: false, sort: false,
        filter(text, query) {
            const model = lookup.get(String(text.value));
            const haystack = [model?.name, model?.id, model?.backend, ...(model?.languages || [])].join(' ').toLowerCase();
            return input._universalShowFullList || haystack.includes(String(query || '').toLowerCase());
        },
        item(text, query, index) {
            const model = lookup.get(String(text.value));
            const fitStatus = model?.fit?.status || 'unknown';
            const li = document.createElement('li');
            li.setAttribute('role', 'option');
            li.id = `awesomplete_list_universal_item_${index}`;
            li.innerHTML = `<span class="universal-option-heading">
                    <span class="universal-option-title">${escapeHtml(model?.name || String(text))}</span>
                    <span class="universal-badge-row">${universalModelBadges(model)}</span>
                </span>
                <span class="universal-option-detail">
                    <span class="universal-option-backend">${escapeHtml(model?.backend || 'Unknown backend')}</span>
                    <span>${Number((model?.sampleRate || 0) / 1000).toFixed(1)} kHz native</span>
                    <span>${escapeHtml(universalEstimateText(model || {}))}</span>
                    <span class="universal-fit-badge universal-fit-${universalFitTone(fitStatus)}">${escapeHtml(fitStatus)} fit</span>
                </span>`;
            return li;
        },
        replace(text) {
            input.value = text.label;
        }
    });
    input._universalAwesomplete = awesomplete;
    const dropdown = input.closest('.universal-model-combobox')
        ?.querySelector('.model-autocomplete-dropdown-btn');
    if (dropdown) {
        dropdown.onclick = event => {
            event.preventDefault();
            input.focus();
            if (!awesomplete.ul.hasAttribute('hidden')) {
                awesomplete.close();
                return;
            }
            input._universalShowFullList = true;
            awesomplete.evaluate();
            awesomplete.open();
            input._universalShowFullList = false;
        };
    }
    const accept = event => {
        const selectedValue = event?.text?.value;
        const typedValue = input.value.trim().toLocaleLowerCase();
        const typedMatches = models.filter(model => model.name.toLocaleLowerCase() === typedValue
            || model.id.toLocaleLowerCase() === typedValue);
        const value = lookup.has(String(selectedValue))
            ? String(selectedValue)
            : typedMatches.length === 1 ? typedMatches[0].id : '';
        if (!lookup.has(value)) {
            const selectedModel = lookup.get(config.tts.universal.model);
            input.value = selectedModel?.name || '';
            input.classList.add('input-error');
            return;
        }
        input.classList.remove('input-error');
        if (value === config.tts.universal.model) {
            input.value = lookup.get(value).name;
            return;
        }
        selectUniversalModel(value);
    };
    input.addEventListener('awesomplete-selectcomplete', accept);
    input.addEventListener('change', accept);
}

function selectUniversalModel(modelId) {
    if (!universalCompatibleModels().some(model => model.id === modelId)) return;
    config.tts.universal.model = modelId;
    universalSpeechState.selectionWarning = '';
    universalSpeechState.voiceSetup = null;
    universalSpeechState.voiceSetupError = null;
    universalSpeechState.voiceSetupRequestId += 1;
    ensureUniversalModelProfile(modelId);
    markDirty();
    renderUniversalModelPanel();
    refreshUniversalLoadPlan();
    refreshUniversalStackInstallPlans();
    refreshUniversalVoiceSetupStatus();
    refreshUniversalOverrideAutocompletes();
}

function renderUniversalVoiceSetup(model, disabled) {
    const policy = model.voiceReference || {};
    const transcriptPolicy = policy.transcript || 'unused';
    const preparationMode = policy.preparation?.mode || 'lazy';
    const setup = universalSpeechState.voiceSetup;
    const setupError = universalSpeechState.voiceSetupError;
    const progress = universalSpeechState.voiceSetupProgress;
    const running = progress?.status === 'processing' && progress.model === model.id;
    const anotherModelRunning = progress?.status === 'processing' && progress.model !== model.id;
    const setupCurrent = setup?.model === model.id;
    const setupComplete = Boolean(setupCurrent && setup.complete);
    let detail = 'Checking local and remote voice references...';
    let warning = '';
    let buttonDisabled = disabled || !setup;
    if (running) {
        const total = Number(progress.total || 0);
        const completed = Number(progress.completed || 0);
        const transcriptProgress = [];
        if (progress.reused) transcriptProgress.push(`${progress.reused} local transcript${progress.reused === 1 ? '' : 's'} reused`);
        if (progress.transcribed) transcriptProgress.push(`${progress.transcribed} transcript${progress.transcribed === 1 ? '' : 's'} generated`);
        detail = `${progress.phase || 'Processing'}: ${completed}/${total}${progress.current ? ` - ${escapeHtml(progress.current)}` : ''}${transcriptProgress.length ? ` · ${transcriptProgress.join(', ')}` : ''}`;
        buttonDisabled = true;
    } else if (anotherModelRunning) {
        detail = `Voice setup is currently running for ${escapeHtml(progress.model)}.`;
        buttonDisabled = true;
    } else if (setupError) {
        detail = escapeHtml(setupError);
        buttonDisabled = true;
    } else if (setup && setup.model === model.id) {
        const parts = [];
        if (setup.transcriptsMissing) parts.push(`${setup.transcriptsMissing} transcript${setup.transcriptsMissing === 1 ? '' : 's'}`);
        if (setup.uploadsMissing) parts.push(`${setup.uploadsMissing} upload${setup.uploadsMissing === 1 ? '' : 's'}`);
        if (setup.preparationsMissing) parts.push(`${setup.preparationsMissing} encoding${setup.preparationsMissing === 1 ? '' : 's'}`);
        detail = setup.complete
            ? `${setup.total} active-language voice references are ready for this model.`
            : `${parts.join(', ') || 'Preparation pending'} for ${setup.total} active-language references.`;
        if (transcriptPolicy === 'required' && setup.transcriptsMissing && !setup.sttConfigured) {
            warning = 'This model requires reference transcripts. Configure a Speech-to-Text provider before preparing or lazily cloning new voices.';
            buttonDisabled = true;
        } else if (preparationMode === 'lazy') {
            detail += ' This backend encodes each voice on first use.';
        }
    }
    const failures = progress?.failures?.length
        ? `<p class="field-hint universal-warning">${progress.failures.length} voice reference${progress.failures.length === 1 ? '' : 's'} failed; retry resumes unfinished work.</p>`
        : '';
    let stateLabel = 'Checking';
    let stateTone = 'unknown';
    if (setupComplete) {
        stateLabel = 'Ready';
        stateTone = 'comfortable';
    } else if (running) {
        stateLabel = 'Processing';
        stateTone = 'busy';
    } else if (anotherModelRunning) {
        stateLabel = 'Waiting';
        stateTone = 'busy';
    } else if (setupError || progress?.failures?.length) {
        stateLabel = 'Attention';
        stateTone = 'insufficient';
    } else if (setupCurrent) {
        stateLabel = 'Setup needed';
        stateTone = 'tight';
    }
    return `<div class="universal-model-summary universal-voice-setup-card${setupComplete ? ' universal-voice-setup-ready' : ''}" id="universalVoiceSetupCard">
        <div class="universal-voice-setup-heading">
            <strong>Voice Reference Setup</strong>
            <span class="universal-fit-badge universal-fit-${stateTone}">${stateLabel}</span>
        </div>
        <div>${detail}</div>
        ${warning ? `<p class="field-hint universal-warning">${escapeHtml(warning)}</p>` : ''}
        ${failures}
        ${setupComplete ? '' : `<div class="universal-model-actions">
            <button type="button" class="btn btn-sm" onclick="startUniversalVoiceSetup()"
                ${buttonDisabled ? 'disabled' : ''}>Prepare Voice References</button>
            ${running ? '<button type="button" class="btn btn-sm" onclick="cancelUniversalVoiceSetup()">Cancel</button>' : ''}
        </div>`}
        <p class="field-hint universal-voice-setup-note">Full setup is optional. Without it, Sonorus performs the same required transcript, upload, and supported encoding work when a voice is first used.</p>
    </div>`;
}

function renderUniversalVoiceSetupCard() {
    const card = document.getElementById('universalVoiceSetupCard');
    const model = universalCompatibleModels().find(
        item => item.id === config.tts?.universal?.model
    );
    if (!card || !model) return;
    card.outerHTML = renderUniversalVoiceSetup(
        model, universalSpeechState.status !== 'connected'
    );
}

async function refreshUniversalVoiceSetupStatus() {
    if (!universalIsSelected() || universalSpeechState.status !== 'connected'
        || !config.tts?.universal?.model) return;
    const modelId = config.tts.universal.model;
    if (!universalCompatibleModels().find(model => model.id === modelId)?.installed) {
        universalSpeechState.voiceSetup = null;
        universalSpeechState.voiceSetupError = null;
        renderUniversalVoiceSetupCard();
        return;
    }
    const requestId = ++universalSpeechState.voiceSetupRequestId;
    universalSpeechState.voiceSetupError = null;
    renderUniversalVoiceSetupCard();
    try {
        const response = await fetch('/api/tts/universal/voice-setup/status', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload({ model: modelId }))
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error?.message || 'Voice setup check failed.');
        if (!universalIsSelected()
            || requestId !== universalSpeechState.voiceSetupRequestId
            || modelId !== config.tts?.universal?.model
            || universalSpeechState.status !== 'connected') return;
        universalSpeechState.voiceSetup = data.setup;
        universalSpeechState.voiceSetupError = null;
        universalSpeechState.voiceSetupProgress = data.progress;
        renderUniversalVoiceSetupCard();
        if (data.progress?.status === 'processing') startUniversalVoiceSetupPolling();
    } catch (error) {
        if (!universalIsSelected()
            || requestId !== universalSpeechState.voiceSetupRequestId
            || modelId !== config.tts?.universal?.model) return;
        universalSpeechState.voiceSetupError = error.message || 'Voice setup check failed.';
        renderUniversalVoiceSetupCard();
        console.warn('[Universal] Voice setup status failed:', error);
    }
}

async function startUniversalVoiceSetup() {
    const modelId = config.tts.universal.model;
    try {
        const response = await fetch('/api/tts/universal/voice-setup/start', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload({ model: modelId }))
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error?.message || 'Voice setup failed to start.');
        universalSpeechState.voiceSetupProgress = {
            status: 'processing', model: modelId,
            total: 0, completed: 0, phase: 'starting', current: '', failures: []
        };
        universalSpeechState.voiceSetupError = null;
        renderUniversalVoiceSetupCard();
        startUniversalVoiceSetupPolling();
    } catch (error) {
        showToast(error.message || 'Voice setup failed to start.', 'error');
    }
}

function startUniversalVoiceSetupPolling() {
    if (universalSpeechState.voiceSetupPollTimer) return;
    universalSpeechState.voiceSetupPollTimer = setInterval(pollUniversalVoiceSetup, 1000);
    pollUniversalVoiceSetup();
}

async function pollUniversalVoiceSetup() {
    if (!universalIsSelected()) {
        clearInterval(universalSpeechState.voiceSetupPollTimer);
        universalSpeechState.voiceSetupPollTimer = null;
        return;
    }
    if (universalSpeechState.voiceSetupPollInFlight) return;
    universalSpeechState.voiceSetupPollInFlight = true;
    try {
        const response = await fetch('/api/tts/universal/voice-setup/progress');
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error('Voice setup progress failed.');
        if (!universalIsSelected()) return;
        universalSpeechState.voiceSetupProgress = data.progress;
        renderUniversalVoiceSetupCard();
        if (data.progress?.status !== 'processing') {
            clearInterval(universalSpeechState.voiceSetupPollTimer);
            universalSpeechState.voiceSetupPollTimer = null;
            await refreshUniversalVoiceSetupStatus();
            pollUniversalResources();
        }
    } catch (error) {
        clearInterval(universalSpeechState.voiceSetupPollTimer);
        universalSpeechState.voiceSetupPollTimer = null;
    } finally {
        universalSpeechState.voiceSetupPollInFlight = false;
    }
}

async function cancelUniversalVoiceSetup() {
    await fetch('/api/tts/universal/voice-setup/cancel', { method: 'POST' });
}

function updateUniversalModelOption(controlId, rawValue, type) {
    const profile = ensureUniversalModelProfile(config.tts.universal.model);
    if (!profile) return;
    profile.options[controlId] = type === 'integer' ? parseInt(rawValue, 10) : parseFloat(rawValue);
    markDirty();
}

function updateUniversalProfileValue(key, value, rerender = false) {
    const profile = ensureUniversalModelProfile(config.tts.universal.model);
    if (!profile) return;
    profile[key] = value;
    markDirty();
    if (rerender) {
        universalSpeechState.loadPlan = null;
        renderUniversalModelPanel();
        refreshUniversalLoadPlan();
        refreshUniversalStackInstallPlans();
    }
}

function resetUniversalModelProfile() {
    const model = universalCompatibleModels().find(item => item.id === config.tts.universal.model);
    if (!model) return;
    const profile = ensureUniversalModelProfile(model.id);
    const advertised = new Set((model.controls || []).map(control => control.id));
    const preserved = Object.fromEntries(
        Object.entries(profile.options || {}).filter(([id]) => !advertised.has(id))
    );
    profile.options = { ...preserved, ...(model.defaults?.options || {}) };
    if (model.upscaleEligible) profile.upscale = model.defaults?.upscale !== false;
    if (model.segmentation && model.alignmentCompatible) {
        profile.adaptive_batching = model.defaults?.adaptive_batching !== false;
    }
    markDirty();
    universalSpeechState.loadPlan = null;
    renderUniversalModelPanel();
    refreshUniversalLoadPlan();
    refreshUniversalStackInstallPlans();
}

async function warmupUniversalModel() {
    if (universalCurrentInstallTarget()) {
        refreshUniversalStackInstallPlans();
        return;
    }
    const button = document.getElementById('universalWarmupButton');
    const asrButton = document.getElementById('universalASRWarmupButton');
    const status = document.getElementById('universalWarmupStatus');
    const asrStatus = document.getElementById('universalASRWarmupStatus');
    const selection = universalStackSelection();
    const connectionRequestId = universalSpeechState.requestId;
    const warmupRequestId = ++universalSpeechState.warmupRequestId;
    const components = [];
    if (selection.tts_model) components.push('TTS');
    if (selection.asr_model) components.push('ASR');
    if (selection.upscale) components.push('AudioVAE');
    if (selection.alignment) components.push('CTC');
    if (button) button.disabled = true;
    if (asrButton) asrButton.disabled = true;
    const setStatus = value => {
        if (status) status.textContent = value;
        if (asrStatus) asrStatus.textContent = value;
    };
    setStatus(`Loading ${components.join(', ')} on the speech server…`);
    try {
        const combined = (universalSpeechState.capabilities?.capabilitiesVersion || 1) >= 6;
        const endpoint = combined ? '/api/speech-server/warmup' : '/api/tts/universal/warmup';
        const payload = combined ? universalStackPayload(selection) : {
            model: selection.tts_model,
            upscale: selection.upscale,
            adaptive_batching: selection.alignment,
        };
        const response = await fetch(endpoint, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload(payload))
        });
        const data = await response.json().catch(() => ({}));
        if (
            connectionRequestId !== universalSpeechState.requestId
            || warmupRequestId !== universalSpeechState.warmupRequestId
            || !speechServerIsSelected()
        ) return;
        if (!response.ok || data.ok === false) {
            const failures = data.warmup?.results?.filter(result => !result.loaded)
                .map(result => `${result.id}: ${result.error || 'not loaded'}`).join('; ');
            throw new Error(data.error?.message || failures || 'Warmup failed.');
        }
        universalSpeechState.resources = data.resources || universalSpeechState.resources;
        setStatus(`${components.join(', ')} loaded.`);
        // Warmup changes residency, not the model registry. Keep the successful
        // capability discovery and update the existing panel in place; forcing a
        // reconnect here can replace it with an empty shell if that follow-up
        // request is interrupted or returns an incomplete response.
        universalSpeechState.loadPlan = null;
        refreshUniversalFits();
        if (universalIsSelected()) refreshUniversalModelPanelIfIdle();
        if (universalASRIsSelected()) refreshUniversalASRConnectionUI();
        await refreshUniversalLoadPlan();
    } catch (error) {
        if (
            connectionRequestId !== universalSpeechState.requestId
            || warmupRequestId !== universalSpeechState.warmupRequestId
            || !speechServerIsSelected()
        ) return;
        setStatus(error.message || 'Warmup failed.');
    } finally {
        if (button) button.disabled = false;
        if (asrButton) asrButton.disabled = false;
    }
}

function updateUniversalWarmupAction(model, profile) {
    const button = document.getElementById('universalWarmupButton');
    const loaded = document.getElementById('universalWarmupLoaded');
    const detail = document.getElementById('universalStackLoadDetail');
    const stackLoaded = universalSelectedStackLoaded(model, profile);
    const installTarget = universalCurrentInstallTarget();
    const missing = universalMissingStackComponents(model, profile);
    const missingDetail = universalMissingStackDetail(missing);
    const capacityOk = universalResidentCapacityOk();
    if (button) {
        button.hidden = stackLoaded || Boolean(installTarget);
        button.textContent = universalLoadButtonLabel(missing);
        button.disabled = !capacityOk || Boolean(installTarget);
    }
    if (loaded) loaded.hidden = !stackLoaded;
    if (detail) {
        const residentLimit = universalResidentLimit();
        const capacityDetail = capacityOk ? ''
            : `The server allows ${residentLimit} resident model ${residentLimit === 1 ? 'slot' : 'slots'}; two are required for remote TTS and ASR.`;
        detail.hidden = !(capacityDetail || missingDetail);
        detail.textContent = capacityDetail || missingDetail;
    }
}

function updateUniversalSelectedFitDisplay() {
    const selectedModel = universalCompatibleModels().find(
        model => model.id === config.tts?.universal?.model
    );
    if (!selectedModel) return;
    const profile = config.tts?.universal?.model_settings?.[selectedModel.id];
    const result = calculateUniversalFit(selectedModel, profile);
    const fit = document.getElementById('universalSelectedFit');
    const note = document.getElementById('universalSelectedFitNote');
    if (fit) {
        fit.textContent = result.status;
        fit.className = `universal-fit-badge universal-fit-${universalFitTone(result.status)}`;
    }
    if (note) note.textContent = universalFitNote(result);
    updateUniversalWarmupAction(selectedModel, profile);
}

async function refreshUniversalLoadPlan() {
    const caps = universalSpeechState.capabilities;
    const selection = universalStackSelection();
    if (!speechServerIsSelected() || universalSpeechState.status !== 'connected'
        || !caps?.loadPlanning || universalCurrentInstallTarget()
        || (!selection.tts_model && !selection.asr_model)) {
        universalSpeechState.loadPlan = null;
        updateUniversalSelectedFitDisplay();
        return;
    }
    const requestId = ++universalSpeechState.loadPlanRequestId;
    try {
        const combined = (caps.capabilitiesVersion || 1) >= 6;
        if (!combined && !selection.tts_model) {
            throw new Error('Combined load planning requires an updated speech server.');
        }
        const endpoint = combined ? '/api/speech-server/plan' : '/api/tts/universal/plan';
        const payload = combined ? universalStackPayload(selection) : {
            model: selection.tts_model,
            upscale: selection.upscale,
            adaptive_batching: selection.alignment,
        };
        const response = await fetch(endpoint, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload(payload))
        });
        const data = await response.json().catch(() => ({}));
        if (!speechServerIsSelected()
            || requestId !== universalSpeechState.loadPlanRequestId) return;
        if (!response.ok) throw new Error(
            data.error?.message || 'Component load planning failed.'
        );
        universalSpeechState.loadPlan = data.plan;
        refreshUniversalFits();
        updateUniversalSelectedFitDisplay();
        if (universalASRIsSelected()) refreshUniversalASRModelPanelIfIdle();
    } catch (error) {
        if (!speechServerIsSelected()
            || requestId !== universalSpeechState.loadPlanRequestId) return;
        universalSpeechState.loadPlan = null;
        refreshUniversalFits();
        updateUniversalSelectedFitDisplay();
        if (universalASRIsSelected()) refreshUniversalASRModelPanelIfIdle();
        console.warn('[Universal] Load planning unavailable:', error);
    }
}

function startUniversalResourcePolling() {
    if (!speechServerIsSelected() || universalSpeechState.status !== 'connected'
        || universalSpeechState.capabilities?.resourcesAvailable === false) return;
    if (!universalSpeechState.pollTimer) {
        universalSpeechState.pollTimer = setInterval(pollUniversalResources, 3000);
    }
}

async function pollUniversalResources() {
    if (!speechServerIsSelected() || universalSpeechState.status !== 'connected'
        || document.hidden || universalSpeechState.resourcePollInFlight) return;
    const connectionRequestId = universalSpeechState.requestId;
    const pollRequestId = ++universalSpeechState.resourcePollRequestId;
    universalSpeechState.resourcePollInFlight = true;
    try {
        const response = await fetch('/api/speech-server/resources', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(universalDraftPayload())
        });
        const data = await response.json().catch(() => ({}));
        if (!speechServerIsSelected()
            || connectionRequestId !== universalSpeechState.requestId
            || pollRequestId !== universalSpeechState.resourcePollRequestId) return;
        if (!response.ok) throw new Error(data.error?.message || 'Resource polling failed.');
        universalSpeechState.resources = data.resources;
        universalSpeechState.lastChecked = Date.now();
        refreshUniversalFits();
        updateUniversalResourceDisplay();
        updateUniversalSelectedFitDisplay();
        if (universalASRIsSelected()) refreshUniversalASRModelPanelIfIdle();
        refreshUniversalLoadPlan();
    } catch (error) {
        if (!speechServerIsSelected()
            || connectionRequestId !== universalSpeechState.requestId
            || pollRequestId !== universalSpeechState.resourcePollRequestId) return;
        universalSpeechState.status = 'stale';
        universalSpeechState.error = error.message;
        clearUniversalTimers();
        const container = document.getElementById('ttsProviderSettings');
        if (container && universalIsSelected()) refreshUniversalConnectionUI();
        if (universalASRIsSelected()) refreshUniversalASRConnectionUI();
        scheduleUniversalReconnect();
        refreshSetupStateFromConfig();
    } finally {
        if (pollRequestId === universalSpeechState.resourcePollRequestId) {
            universalSpeechState.resourcePollInFlight = false;
        }
    }
}

function updateUniversalResourceDisplay() {
    const target = document.getElementById('universalRemoteResources');
    if (!target) return;
    const resources = universalSpeechState.resources;
    if (!resources) {
        target.innerHTML = '<p class="field-hint">Remote RAM/VRAM telemetry unavailable.</p>';
        return;
    }
    const ram = resources.ram;
    const gpu = [...(resources.gpus || [])].sort((a, b) => (b.totalBytes || 0) - (a.totalBytes || 0))[0];
    const meter = (label, item, detail = '') => {
        if (!item?.totalBytes) return `<div class="universal-resource-meter"><strong>${escapeHtml(label)}</strong><span>Unavailable</span></div>`;
        const pct = Math.max(0, Math.min(100, item.usedBytes / item.totalBytes * 100));
        const tone = pct >= 90 ? 'red' : pct >= 75 ? 'yellow' : 'green';
        return `<div class="universal-resource-meter"><strong>${escapeHtml(label)}</strong>
            <span>${formatUniversalBytes(item.usedBytes)} / ${formatUniversalBytes(item.totalBytes)}</span>
            <div class="vram-bar" role="meter" aria-label="${escapeHtml(label)} usage"
                aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct.toFixed(1)}">
                <div class="vram-fill ${tone}" style="width:${pct.toFixed(1)}%"></div>
            </div>
            <span class="field-hint">${formatUniversalBytes(item.freeBytes)} free · remote measurement${detail}</span></div>`;
    };
    const processDetail = resources.processRamBytes !== null
        && resources.processRamBytes !== undefined
        && Number.isFinite(Number(resources.processRamBytes))
        ? ` · speech process ${formatUniversalBytes(resources.processRamBytes)}` : '';
    target.innerHTML = meter('Server RAM', ram, processDetail) + meter(gpu?.name || 'Server GPU VRAM', gpu);
}

function refreshUniversalOverrideAutocompletes() {
    const enabled = universalIsSelected() && !!universalSpeechState.capabilities;
    const models = enabled ? universalCompatibleModels() : [];
    const ids = models.map(model => model.id);
    document.querySelectorAll('#playerVoiceModel, .character-model-override').forEach(input => {
        if (window.Awesomplete) {
            if (!input._universalOverrideAwesomplete) {
                input._universalOverrideAwesomplete = new Awesomplete(input, { list: ids, minChars: 0, maxItems: 20, sort: false });
                input.addEventListener('awesomplete-selectcomplete', () => input.dispatchEvent(new Event('change', { bubbles: true })));
            } else {
                input._universalOverrideAwesomplete.list = ids;
            }
        }
        const invalid = enabled && input.value.trim() && !ids.includes(input.value.trim());
        input.classList.toggle('universal-ignored-override', invalid);
        input.title = invalid ? 'Ignored by Universal Speech Server: this model is not currently compatible.' : '';
        let warning = input.parentElement?.querySelector('.universal-override-warning');
        if (invalid && !warning) {
            warning = document.createElement('p');
            warning.className = 'field-hint universal-warning universal-override-warning';
            warning.textContent = 'Saved value is retained but ignored; Universal uses the global model.';
            input.insertAdjacentElement('afterend', warning);
        } else if (!invalid && warning) {
            warning.remove();
        }
    });
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        if (universalSpeechState.status === 'connected') pollUniversalResources();
        else if (speechServerIsSelected()) connectUniversalSpeechServer(false);
    }
});

function renderProviderSettings(category, providerId) {
    const providers = category === 'tts' ? TTS_PROVIDERS : {};
    const providerConfig = providers[providerId];
    const container = document.getElementById(`${category}ProviderSettings`);

    if (!providerConfig || !container) {
        console.warn(`No config for ${category}/${providerId}`);
        return;
    }

    if (category === 'tts' && providerId === 'universal') {
        renderUniversalProviderSettings(container, providerConfig);
        return;
    }

    let html = '';
    if (providerConfig.description) {
        html += `<p class="field-hint" style="margin-bottom: var(--space-md);">${providerConfig.description}</p>`;
    }
    html += providerConfig.fields.map(f => renderField(f, category, providerId)).join('');
    container.innerHTML = html;
    updateProviderFieldDependencies(category, providerId);
    applySimpleMode();
}

function switchProvider(category, providerId) {
    updateSetting(`${category}.provider`, providerId);
    if (category === 'tts' && providerId !== 'universal' && !universalASRIsSelected()) {
        suspendUniversalUiWork();
    }
    renderProviderSettings(category, providerId);

    // Handle TTS-specific UI updates
    if (category === 'tts') {
        updatePlayerVoiceSectionState(providerId);
        updateVramMonitoring();
        updateRamMonitoring();
        updateOmniVoicePanel();
        updateOmniVoiceCppPanel();
        if (providerId === 'universal') {
            connectUniversalSpeechServer(false);
        } else if (universalASRIsSelected()) {
            renderSTTProviderSettings('universal');
            refreshUniversalLoadPlan();
        }
        refreshUniversalOverrideAutocompletes();
    }

    refreshSetupStateFromConfig();

    if (category === 'tts') {
        restoreProviderSectionScroll('chapterTTS');
    }
}

function isSimpleModeEnabled() {
    return config.ui?.simple_mode !== false;
}

function setSimpleHidden(target, hidden) {
    if (!target) return;
    target.hidden = hidden;
    target.classList.toggle('simple-mode-hidden', hidden);
}

function setSimpleHiddenById(id, hidden) {
    setSimpleHidden(document.getElementById(id), hidden);
}

function applySimpleMode() {
    const simpleEnabled = isSimpleModeEnabled();

    document.querySelectorAll('.field-group[data-simple-hide="true"]').forEach(group => {
        setSimpleHidden(group, simpleEnabled);
    });

    [
        'playerVoiceSubSettings',
        'open_mic_settings',
        'open_mic_endpointing_settings',
        'open_mic_timeout_settings',
        'convSpeakerMaxTokensGroup',
        'backgroundCommentaryMaxTokensGroup',
        'convMaxTokensGroup',
        'audioCameraOffsetGroup',
        'audioReverbGroup'
    ].forEach(id => setSimpleHiddenById(id, simpleEnabled));
}

function updateSimpleMode(enabled) {
    updateSetting('ui.simple_mode', enabled);
    applySimpleMode();
}

// Disable/enable player voice and pronunciation sections based on TTS provider
function updatePlayerVoiceSubSettings(enabled) {
    const container = document.getElementById('playerVoiceSubSettings');
    if (container) {
        container.style.opacity = enabled ? '1' : '0.5';
        container.style.pointerEvents = enabled ? 'auto' : 'none';
    }
    applySimpleMode();
}

function updateConversationFpvSubSettings(enabled) {
    const container = document.getElementById('conversationFpvSubSettings');
    if (container) {
        container.style.opacity = enabled ? '1' : '0.5';
        container.style.pointerEvents = enabled ? 'auto' : 'none';
        container.querySelectorAll('input, select, textarea, button').forEach(el => {
            el.disabled = !enabled;
        });
    }
}

function updatePlayerVoiceSectionState(providerId) {
    const isDisabled = providerId === 'none';
    const section = document.getElementById('playerVoiceSection');
    const toggle = document.getElementById('playerVoiceEnabled');
    const input = document.getElementById('playerVoiceName');
    const pronunciationSection = document.getElementById('pronunciationSection');
    const pronunciationTextarea = document.getElementById('pronunciationReplacements');
    const settingsTestButton = document.getElementById('ttsSettingsTestBtn');

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
    if (settingsTestButton) {
        settingsTestButton.disabled = isDisabled;
        settingsTestButton.title = isDisabled
            ? 'Voice testing is unavailable while TTS is set to Disabled (Subtitles Only).'
            : 'Open the TTS voice test in Setup.';
    }

    // Update sub-settings (spatial, voice override) based on player voice toggle
    if (!isDisabled && toggle) {
        updatePlayerVoiceSubSettings(toggle.checked);
    } else {
        applySimpleMode();
    }
}

// ============================================
// VRAM Monitoring for NeuTTS GPU mode
// ============================================
let vramMonitorInterval = null;

function updateVramMonitoring() {
    const provider = config.tts?.provider;

    // OmniVoice has its own VRAM monitoring in updateOmniVoicePanel
    if (provider === 'omnivoice') {
        stopVramMonitoring();
        return;
    }

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

// ============================================
// OmniVoice Setup & Monitoring
// ============================================
let omnivoiceStatusInterval = null;
let _nvidiaDetected = null;
let _omnivoiceSelectedDeviceForMeters = null;

function updateOmniVoicePanel() {
    const provider = config.tts?.provider;
    const panel = document.getElementById('omnivoiceSetup');
    if (provider !== 'omnivoice') {
        if (panel) panel.style.display = 'none';
        if (omnivoiceStatusInterval) {
            clearInterval(omnivoiceStatusInterval);
            omnivoiceStatusInterval = null;
        }
        return;
    }
    if (panel) panel.style.display = 'block';
    fetchOmniVoiceStatus();
    if (!omnivoiceStatusInterval) {
        omnivoiceStatusInterval = setInterval(fetchOmniVoiceStatus, 3000);
    }
}

async function fetchOmniVoiceStatus() {
    try {
        const resp = await fetch('/api/tts/omnivoice/status');
        const data = await resp.json();
        _nvidiaDetected = data.gpu.nvidia_detected;
        renderOmniVoicePanel(data);
    } catch (e) {
        console.error('[OmniVoice] Status check failed:', e);
    }
}

function renderOmniVoicePanel(data) {
    const gpu = data.gpu;
    const gpus = Array.isArray(gpu.gpus) ? gpu.gpus : [];
    const selectedDevice = getOmniVoiceSelectedDevice(data);
    const modelLoadedOnSelectedDevice = data.model_loaded && selectedDevice === data.selected_device;
    _omnivoiceSelectedDeviceForMeters = selectedDevice;

    // GPU name
    const gpuName = document.getElementById('omnivoiceGpuName');
    if (gpuName) {
        if (gpu.nvidia_detected) {
            const selectedGpu = gpus.find(g => g.device === selectedDevice) || gpus[0];
            if (selectedGpu) {
                gpuName.textContent = 'Using ' + selectedGpu.name + ' (' + selectedGpu.vram_total_gb + ' GB)';
            } else {
                gpuName.textContent = gpu.gpu_name + ' (' + gpu.vram_total_gb + ' GB)';
            }
            gpuName.style.color = '';
        } else {
            gpuName.textContent = 'No NVIDIA GPU detected. OmniVoice requires a CUDA GPU.';
            gpuName.style.color = 'var(--error)';
        }
    }

    renderOmniVoiceGpuPicker(data, selectedDevice);

    const needsInstall = !data.deps_installed && gpu.nvidia_detected;
    const needsVoiceSetup = data.deps_installed && data.voices_needing_setup > 0;
    const setupComplete = data.deps_installed && data.voices_needing_setup === 0;

    // Install section (step 1)
    const installSection = document.getElementById('omnivoiceInstallSection');
    if (installSection) {
        installSection.style.display = needsInstall ? 'block' : 'none';
    }

    // Voice setup section (step 2 — same prominent spot as install)
    const voiceSetup = document.getElementById('omnivoiceVoiceSetup');
    const progress = data.setup_progress || {};
    const isProcessing = progress.status === 'transcribing'
        || progress.status === 'processing' || progress.status === 'loading';

    if (voiceSetup) {
        voiceSetup.style.display = (needsVoiceSetup || isProcessing) ? 'block' : 'none';

        const voiceCount = document.getElementById('omnivoiceVoiceCount');
        const sttWarning = document.getElementById('omnivoiceSttWarning');
        const pretokenizeBtn = document.getElementById('omnivoicePretokenizeBtn');
        const progressBar = document.getElementById('omnivoiceSetupProgress');
        const progressFill = document.getElementById('omnivoiceProgressFill');
        const progressCount = document.getElementById('omnivoiceProgressCount');
        const progressStatus = document.getElementById('omnivoiceProgressStatus');

        if (isProcessing) {
            // Show progress bar, hide button
            if (pretokenizeBtn) pretokenizeBtn.style.display = 'none';
            if (sttWarning) sttWarning.style.display = 'none';
            if (progressBar) progressBar.style.display = 'block';

            const pct = progress.total > 0 ? Math.round(progress.completed / progress.total * 100) : 0;
            if (progressFill) progressFill.style.width = pct + '%';
            if (progressCount) progressCount.textContent = progress.completed + '/' + progress.total;

            const current = progress.current || '';
            if (voiceCount) {
                voiceCount.textContent = progress.status === 'loading'
                    ? 'Loading OmniVoice model (first time may download ~3GB)...'
                    : progress.status === 'transcribing'
                        ? 'Transcribing voice references...'
                        : 'Processing voice references...';
                voiceCount.style.color = '';
            }
            if (progressStatus) {
                if (progress.status === 'loading') {
                    progressStatus.textContent = current || 'Starting worker...';
                } else {
                    progressStatus.textContent = current ? 'Processing: ' + current : '';
                }
            }
        } else if (needsVoiceSetup) {
            // Show button, hide progress bar
            if (progressBar) progressBar.style.display = 'none';
            if (voiceCount) {
                voiceCount.textContent = data.voices_needing_setup + ' voice reference(s) need processing before OmniVoice can be used.';
                voiceCount.style.color = '';
            }
            const needsTranscription = Number(data.transcripts_needing_setup || 0) > 0;
            const canPrepare = data.stt_configured || !needsTranscription;
            if (sttWarning) sttWarning.style.display = canPrepare ? 'none' : 'block';
            if (pretokenizeBtn) pretokenizeBtn.style.display = canPrepare ? '' : 'none';
        }
    }

    // Settings fields: only show when fully set up
    const settingsContainer = document.getElementById('ttsProviderSettings');
    if (settingsContainer) {
        const fieldGroups = settingsContainer.querySelectorAll('.field-group');
        fieldGroups.forEach(fg => {
            fg.style.display = setupComplete ? '' : 'none';
        });
    }

    // VRAM/RAM meters: show when deps installed (even during voice setup)
    const vramEl = document.getElementById('omnivoiceVramIndicator');
    const ramEl = document.getElementById('omnivoiceRamIndicator');
    if (data.deps_installed) {
        if (vramEl) vramEl.style.display = 'block';
        if (ramEl) ramEl.style.display = 'block';
        fetchOmniVoiceResources(modelLoadedOnSelectedDevice);
    } else {
        if (vramEl) vramEl.style.display = 'none';
        if (ramEl) ramEl.style.display = 'none';
    }
}

function getOmniVoiceSelectedDevice(data) {
    const configured = config.tts?.omnivoice?.device;
    if (configured && configured !== 'auto') {
        const gpus = Array.isArray(data.gpu?.gpus) ? data.gpu.gpus : [];
        if (gpus.some(g => g.device === configured) || configured === 'cuda') {
            return configured;
        }
    }
    return data.selected_device || data.gpu?.recommended_device || 'cuda';
}

function renderOmniVoiceGpuPicker(data, selectedDevice) {
    const picker = document.getElementById('omnivoiceGpuPicker');
    const select = document.getElementById('omnivoiceGpuSelect');
    const gpus = Array.isArray(data.gpu?.gpus) ? data.gpu.gpus : [];
    if (!picker || !select) return;

    if (gpus.length <= 1) {
        picker.style.display = 'none';
        return;
    }

    picker.style.display = 'block';
    const previous = select.value;
    select.innerHTML = '';

    for (const gpu of gpus) {
        const opt = document.createElement('option');
        opt.value = gpu.device;
        const free = Number(gpu.vram_free_gb || 0).toFixed(1);
        const total = Number(gpu.vram_total_gb || 0).toFixed(1);
        opt.textContent = `GPU ${gpu.index}: ${gpu.name} (${free} / ${total} GB free)`;
        select.appendChild(opt);
    }

    const validDevice = gpus.some(g => g.device === selectedDevice)
        ? selectedDevice
        : (data.gpu?.recommended_device || gpus[0].device);
    select.value = validDevice;
    _omnivoiceSelectedDeviceForMeters = validDevice;

    if (previous && previous !== validDevice) {
        fetchOmniVoiceResources(data.model_loaded && validDevice === data.selected_device);
    }
}

function updateOmniVoiceGpu(device) {
    if (!device) return;
    updateProviderSetting('tts', 'omnivoice', 'device', device);
    _omnivoiceSelectedDeviceForMeters = device;
    fetchOmniVoiceResources(false);
}

async function fetchOmniVoiceResources(modelLoaded) {
    try {
        const device = _omnivoiceSelectedDeviceForMeters || config.tts?.omnivoice?.device || 'auto';
        const [vramResp, ramResp] = await Promise.all([
            fetch('/api/tts/vram-status?device=' + encodeURIComponent(device)),
            fetch('/api/system/ram-status')
        ]);
        const vramData = await vramResp.json();
        const ramData = await ramResp.json();
        renderOmniVoiceResourceMeters('omnivoice', vramData, ramData, modelLoaded);
    } catch (e) {
        console.error('[OmniVoice] Resource fetch failed:', e);
    }
}

// ============================================
// OmniVoice (Vulkan) Setup & Monitoring
// ============================================
let omnivoiceCppStatusInterval = null;
let omnivoiceCppPollMs = 0;
let _omnivoiceCppSavedDevice = null;
let _omnivoiceCppStatusPromise = null;
let _omnivoiceCppInstallStarting = false;
let _omnivoiceCppBackendReady = null;
let _omnivoiceCppInstallRunning = false;

function updateOmniVoiceCppPanel() {
    const provider = config.tts?.provider;
    const panel = document.getElementById('omnivoiceCppSetup');
    if (provider !== 'omnivoice_cpp') {
        if (panel) panel.style.display = 'none';
        if (omnivoiceCppStatusInterval) {
            clearInterval(omnivoiceCppStatusInterval);
            omnivoiceCppStatusInterval = null;
            omnivoiceCppPollMs = 0;
        }
        return;
    }
    if (panel) panel.style.display = 'block';
    fetchOmniVoiceCppStatus();
    _setOmniVoiceCppPoll(3000);
}

function _setOmniVoiceCppPoll(ms) {
    if (omnivoiceCppPollMs === ms && omnivoiceCppStatusInterval) return;
    omnivoiceCppPollMs = ms;
    if (omnivoiceCppStatusInterval) clearInterval(omnivoiceCppStatusInterval);
    omnivoiceCppStatusInterval = setInterval(fetchOmniVoiceCppStatus, ms);
}

function fetchOmniVoiceCppStatus() {
    if (_omnivoiceCppStatusPromise) return _omnivoiceCppStatusPromise;
    _omnivoiceCppStatusPromise = (async () => {
        try {
            const device = config.tts?.omnivoice_cpp?.device || 'auto';
            const [gpuResp, setupResp, ramResp] = await Promise.all([
                fetch('/api/tts/vram-status?provider=omnivoice_cpp&device=' + encodeURIComponent(device)),
                fetch('/api/tts/omnivoice-cpp/status'),
                fetch('/api/system/ram-status')
            ]);
            if (!gpuResp.ok || !setupResp.ok || !ramResp.ok) throw new Error('Status request failed');
            const [gpuData, setupData, ramData] = await Promise.all([
                gpuResp.json(),
                setupResp.json(),
                ramResp.json()
            ]);
            if (config.tts?.provider !== 'omnivoice_cpp') return true;
            renderOmniVoiceCppPanel({ ...gpuData, ...setupData, ram_status: ramData });
            return true;
        } catch (e) {
            console.error('[OmniVoiceCpp] Status check failed:', e);
            return false;
        } finally {
            _omnivoiceCppStatusPromise = null;
        }
    })();
    return _omnivoiceCppStatusPromise;
}

function renderOmniVoiceCppPanel(data) {
    const serverProgress = data.install_progress || {};
    if (_omnivoiceCppInstallStarting && serverProgress.status === 'idle') {
        data = {
            ...data,
            install_progress: {
                ...serverProgress,
                status: 'installing',
                current: 'Waiting for the installer to start...',
                message: 'Saving configuration and starting verified downloads...',
            },
        };
    } else if (serverProgress.status === 'installing' ||
               serverProgress.status === 'complete' ||
               serverProgress.status === 'error') {
        _omnivoiceCppInstallStarting = false;
    }

    const gpus = Array.isArray(data.gpus) ? data.gpus : [];
    // selected_device reflects the saved settings on the server — use it as
    // the baseline for the "restart needed" notice.
    _omnivoiceCppSavedDevice = data.selected_device || 'auto';

    const runtimeReady = data.runtime_present === true;
    const modelsReady = data.models_present === true;
    const backendReady = runtimeReady && modelsReady;

    const progress = data.install_progress || {};
    const installing = progress.status === 'installing';
    _omnivoiceCppBackendReady = backendReady;
    _omnivoiceCppInstallRunning = installing;

    // Runtime and Vulkan state
    const hintGroup = document.getElementById('omnivoiceCppRuntimeHint');
    const hintText = document.getElementById('omnivoiceCppRuntimeHintText');
    if (hintGroup && hintText) {
        if (!runtimeReady && !installing) {
            hintText.textContent = 'The native runtime is not shipped with the mod. Sonorus downloads and verifies runtime ' + (data.runtime_version || '') + ' from its GitHub release when this provider is activated.';
            hintText.style.color = 'var(--warning)';
            hintGroup.style.display = 'block';
        } else if (gpus.length === 0) {
            hintText.textContent = 'No Vulkan GPU was detected. Auto may fall back to CPU, which is much slower.';
            hintText.style.color = 'var(--warning)';
            hintGroup.style.display = 'block';
        } else {
            hintGroup.style.display = 'none';
        }
    }

    renderOmniVoiceCppInstall(data, runtimeReady, modelsReady);
    renderOmniVoiceCppVoiceSetup(data, backendReady);

    const gpuPicker = document.getElementById('omnivoiceCppGpuPicker');
    const restartSection = document.getElementById('omnivoiceCppRestartSection');
    if (gpuPicker) gpuPicker.style.display = backendReady ? '' : 'none';
    if (restartSection) restartSection.style.display = backendReady ? '' : 'none';

    renderOmniVoiceCppGpuPicker(gpus);
    updateOmniVoiceCppRestartNotice();

    if (runtimeReady) {
        const modelLoadedOnSelectedDevice = data.model_loaded === true
            && data.server_device === data.selected_device;
        renderOmniVoiceResourceMeters(
            'omnivoice_cpp',
            data,
            data.ram_status || {},
            modelLoadedOnSelectedDevice
        );
    } else {
        setOmniVoiceResourceMetersVisible('omnivoice_cpp', false);
    }

    // Poll fast only while something is actually in flight; idle panels
    // (everything installed, or waiting on user action) tick slowly.
    const installBusy = (data.install_progress || {}).status === 'installing';
    const voiceBusy = (data.voice_progress || {}).status === 'processing';
    _setOmniVoiceCppPoll(installBusy || voiceBusy ? 3000 : 15000);
}

function renderOmniVoiceCppInstall(data, runtimeReady, modelsReady) {
    const section = document.getElementById('omnivoiceCppInstallSection');
    if (!section) return;

    const progress = data.install_progress || {};
    const installing = progress.status === 'installing';
    const backendReady = runtimeReady && modelsReady;
    section.style.display = !backendReady || installing ? 'block' : 'none';

    const btn = document.getElementById('omnivoiceCppInstallBtn');
    const hint = document.getElementById('omnivoiceCppInstallHint');
    const progressBox = document.getElementById('omnivoiceCppInstallProgress');
    const progressFill = document.getElementById('omnivoiceCppInstallProgressFill');
    const progressCount = document.getElementById('omnivoiceCppInstallProgressCount');
    const progressStatus = document.getElementById('omnivoiceCppInstallProgressStatus');
    const completed = Number(progress.completed || 0);
    const total = Number(progress.total || 4);

    if (btn) {
        btn.disabled = installing;
        btn.textContent = installing
            ? 'Downloading OmniVoice...'
            : (progress.status === 'error' ? 'Retry OmniVoice Download' : 'Download OmniVoice Now');
    }
    if (hint) {
        if (progress.status === 'error') {
            hint.textContent = progress.message;
        } else if (installing) {
            hint.textContent = progress.message || 'Downloading and verifying the native runtime and three GGUF models...';
        } else {
            hint.textContent = 'Saving this provider starts the verified native runtime and three GGUF model downloads automatically (approximately 1.5 GB total). Use this button only to download before saving or to retry; existing partial downloads are resumed.';
        }
        hint.style.color = progress.status === 'error' ? 'var(--error)' : '';
    }
    if (progressBox) progressBox.style.display = installing ? 'block' : 'none';
    if (progressFill) progressFill.style.width = (total > 0 ? Math.round(completed / total * 100) : 0) + '%';
    if (progressCount) progressCount.textContent = completed + '/' + total;
    if (progressStatus) {
        progressStatus.textContent = progress.current || progress.message || 'Starting installer...';
    }
}

function showOmniVoiceCppInstallStarting() {
    if (config.tts?.provider !== 'omnivoice_cpp') return;
    if (_omnivoiceCppBackendReady === true) return;
    if (_omnivoiceCppInstallRunning) {
        _setOmniVoiceCppPoll(3000);
        return;
    }
    _omnivoiceCppInstallStarting = true;
    renderOmniVoiceCppInstall({
        install_progress: {
            status: 'installing',
            total: 4,
            completed: 0,
            current: 'Saving configuration and starting downloads...',
            message: 'Saving configuration and starting verified downloads...',
        },
    }, false, false);
    _setOmniVoiceCppPoll(3000);
}

function cancelOmniVoiceCppInstallStarting() {
    _omnivoiceCppInstallStarting = false;
    fetchOmniVoiceCppStatus();
}

function renderOmniVoiceCppVoiceSetup(data, backendReady) {
    const section = document.getElementById('omnivoiceCppVoiceSetup');
    if (!section) return;

    const progress = data.voice_progress || {};
    const processing = progress.status === 'processing';
    const missing = Number(data.voices_needing_setup || 0);
    const missingTranscripts = Number(data.transcripts_needing_setup || 0);
    const missingTokens = Number(data.tokens_needing_setup || 0);
    const canPrepare = data.stt_configured || missingTranscripts === 0;
    section.style.display = backendReady && (missing > 0 || processing) ? 'block' : 'none';

    const count = document.getElementById('omnivoiceCppVoiceCount');
    const warning = document.getElementById('omnivoiceCppSttWarning');
    const btn = document.getElementById('omnivoiceCppPrepareVoicesBtn');
    const progressBox = document.getElementById('omnivoiceCppVoiceProgress');
    const progressFill = document.getElementById('omnivoiceCppVoiceProgressFill');
    const progressCount = document.getElementById('omnivoiceCppVoiceProgressCount');
    const progressStatus = document.getElementById('omnivoiceCppVoiceProgressStatus');
    const completed = Number(progress.completed || 0);
    const total = Number(progress.total || missing);

    if (count) {
        if (processing) {
            const phase = progress.phase || 'processing';
            count.textContent = phase === 'transcribing'
                ? 'Creating transcript sidecars for voice references...'
                : 'Encoding voice references for future reuse...';
            count.style.color = '';
        } else if (progress.error) {
            count.textContent = progress.error;
            count.style.color = 'var(--error)';
        } else {
            const work = [];
            if (missingTokens > 0) work.push(`${missingTokens} need audio encoding`);
            if (missingTranscripts > 0) work.push(`${missingTranscripts} need transcription`);
            const detail = work.length > 0 ? ' (' + work.join('; ') + ')' : '';
            count.textContent = missing + ' voice reference(s) need preparation' + detail + '. Preparing them avoids first-conversation setup delays.';
            count.style.color = '';
        }
    }
    if (warning) warning.style.display = !processing && !canPrepare ? 'block' : 'none';
    if (btn) {
        btn.style.display = processing ? 'none' : '';
        btn.disabled = processing || !canPrepare;
        btn.textContent = canPrepare
            ? 'Prepare Voice References'
            : 'Configure STT to Prepare Voices';
        btn.title = canPrepare
            ? ''
            : 'Select and configure a Speech-to-Text provider first.';
    }
    if (progressBox) progressBox.style.display = processing ? 'block' : 'none';
    if (progressFill) progressFill.style.width = (total > 0 ? Math.round(completed / total * 100) : 0) + '%';
    if (progressCount) progressCount.textContent = completed + '/' + total;
    if (progressStatus) {
        const phaseLabels = {
            transcribing: 'Transcribing',
            loading: 'Loading OmniVoice for encoding',
            encoding: 'Encoding',
        };
        const phaseLabel = phaseLabels[progress.phase] || 'Processing';
        progressStatus.textContent = progress.current ? phaseLabel + ': ' + progress.current : (progress.error || 'Starting...');
    }
}

function renderOmniVoiceCppGpuPicker(gpus) {
    const select = document.getElementById('omnivoiceCppGpuSelect');
    if (!select) return;

    const configured = config.tts?.omnivoice_cpp?.device || 'auto';
    const desiredValue = gpus.some(g => g.device === configured) ? configured : 'auto';
    const signature = JSON.stringify(gpus.map(g => [g.device, g.name]));

    // Rebuilding the options collapses the dropdown if the user has it open —
    // skip while neither the device list nor the selection has changed.
    if (select.dataset.gpuSignature === signature && select.value === desiredValue) {
        return;
    }

    select.dataset.gpuSignature = signature;
    select.innerHTML = '';

    const autoOpt = document.createElement('option');
    autoOpt.value = 'auto';
    autoOpt.textContent = 'Auto (best device)';
    select.appendChild(autoOpt);

    for (const gpu of gpus) {
        const opt = document.createElement('option');
        opt.value = gpu.device;
        const memory = Number.isFinite(gpu.vram_total_gb) ? ` (${gpu.vram_total_gb.toFixed(1)} GB)` : '';
        opt.textContent = `GPU ${gpu.index}: ${gpu.name}${memory}`;
        select.appendChild(opt);
    }

    select.value = desiredValue;
}

function updateOmniVoiceCppGpu(device) {
    if (!device) return;
    updateProviderSetting('tts', 'omnivoice_cpp', 'device', device);
    updateOmniVoiceCppRestartNotice();
    fetchOmniVoiceCppStatus();
}

function updateOmniVoiceCppRestartNotice() {
    const notice = document.getElementById('omnivoiceCppRestartNotice');
    if (!notice) return;
    const current = config.tts?.omnivoice_cpp?.device || 'auto';
    const changed = _omnivoiceCppSavedDevice !== null && current !== _omnivoiceCppSavedDevice;
    notice.style.display = changed ? 'block' : 'none';
}

async function installOmniVoiceCpp() {
    const btn = document.getElementById('omnivoiceCppInstallBtn');
    showOmniVoiceCppInstallStarting();
    try {
        const resp = await fetch('/api/tts/omnivoice-cpp/install', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.status === 'already_installed') {
            showToast('OmniVoice (Vulkan) is already installed', 'info');
        } else if (resp.ok && data.status === 'installing') {
            showToast('OmniVoice runtime and model install started', 'success');
        } else {
            _omnivoiceCppInstallStarting = false;
            showToast(data.error || 'Could not start OmniVoice installation', 'error');
        }
    } catch (e) {
        _omnivoiceCppInstallStarting = false;
        showToast('Could not start OmniVoice installation: ' + e.message, 'error');
    } finally {
        const refreshed = await fetchOmniVoiceCppStatus();
        if (!refreshed && btn) {
            btn.disabled = false;
            btn.textContent = 'Retry OmniVoice Download';
        }
    }
}

async function prepareOmniVoiceCppVoices() {
    const btn = document.getElementById('omnivoiceCppPrepareVoicesBtn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Starting...';
    }
    try {
        const resp = await fetch('/api/tts/omnivoice-cpp/prepare-voices', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.status === 'already_prepared') {
            showToast('Voice references are already prepared', 'info');
        } else if (resp.ok && data.status === 'processing') {
            showToast('Preparing voice references...', 'success');
        } else {
            showToast(data.error || 'Could not prepare voice references', 'error');
        }
    } catch (e) {
        showToast('Could not prepare voice references: ' + e.message, 'error');
    } finally {
        const refreshed = await fetchOmniVoiceCppStatus();
        if (!refreshed && btn) {
            btn.disabled = false;
            btn.textContent = 'Prepare Voice References';
        }
    }
}

async function restartOmniVoiceCppWorker() {
    const btn = document.getElementById('omnivoiceCppRestartBtn');
    const status = document.getElementById('omnivoiceCppRestartStatus');
    if (btn) btn.disabled = true;
    if (status) status.textContent = 'Restarting TTS worker...';
    try {
        const resp = await fetch('/api/tts/omnivoice-cpp/restart-worker', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.status === 'ok') {
            if (status) {
                status.textContent = data.warming_up
                    ? 'Worker restarting in the background.'
                    : 'Worker stopped. It will start on the next voice request.';
            }
            showToast('TTS worker restarted');
        } else {
            if (status) status.textContent = '';
            showToast(data.error || 'Restart failed', 'error');
        }
    } catch (e) {
        if (status) status.textContent = '';
        showToast('Restart failed: ' + e.message, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

const OMNIVOICE_RESOURCE_METERS = {
    omnivoice: {
        displayName: 'OmniVoice',
        vramElementId: 'omnivoiceVramIndicator',
        ramElementId: 'omnivoiceRamIndicator',
        vramNeededGb: 2.5,
        ramNeededGb: 2.5,
    },
    omnivoice_cpp: {
        displayName: 'OmniVoice (Vulkan)',
        vramElementId: 'omnivoiceCppVramIndicator',
        ramElementId: 'omnivoiceCppRamIndicator',
        vramNeededGb: 2.0,
        ramNeededGb: 0.5,
    },
};

function setOmniVoiceResourceMetersVisible(provider, visible) {
    const meterConfig = OMNIVOICE_RESOURCE_METERS[provider];
    if (!meterConfig) return;
    for (const id of [meterConfig.vramElementId, meterConfig.ramElementId]) {
        const el = document.getElementById(id);
        if (el) el.style.display = visible ? 'block' : 'none';
    }
}

function renderOmniVoiceResourceMeters(provider, vramData, ramData, modelLoaded) {
    const meterConfig = OMNIVOICE_RESOURCE_METERS[provider];
    if (!meterConfig) return;

    const vramEl = document.getElementById(meterConfig.vramElementId);
    const hasVram = Number.isFinite(vramData.vram_total_gb)
        && Number.isFinite(vramData.vram_free_gb)
        && Number.isFinite(vramData.vram_used_gb);
    if (vramEl) {
        vramEl.style.display = hasVram ? 'block' : 'none';
        if (hasVram) {
            const gpuLabel = vramData.gpu_name ? ' - ' + vramData.gpu_name : '';
            const label = modelLoaded
                ? `GPU VRAM (${meterConfig.displayName} loaded)`
                : `GPU VRAM (needs ~${meterConfig.vramNeededGb} GB${gpuLabel})`;
            _updateMeter(
                vramEl,
                vramData.vram_used_gb,
                vramData.vram_total_gb,
                vramData.vram_free_gb,
                meterConfig.vramNeededGb,
                label,
                modelLoaded
            );
        }
    }

    const ramEl = document.getElementById(meterConfig.ramElementId);
    const hasRam = Number.isFinite(ramData.ram_total_gb) && ramData.ram_total_gb > 0;
    if (ramEl) {
        ramEl.style.display = hasRam ? 'block' : 'none';
        if (hasRam) {
            // Estimate the pre-load baseline once the model is resident so its
            // own process memory does not cause a false insufficient warning.
            const processRam = Number(ramData.process_ram_gb || 0);
            const adjustedFree = modelLoaded ? ramData.ram_free_gb + processRam : ramData.ram_free_gb;
            const adjustedUsed = modelLoaded ? Math.max(0, ramData.ram_used_gb - processRam) : ramData.ram_used_gb;
            const label = modelLoaded
                ? `System RAM (${meterConfig.displayName} loaded)`
                : `System RAM (needs ~${meterConfig.ramNeededGb} GB)`;
            _updateMeter(
                ramEl,
                adjustedUsed,
                ramData.ram_total_gb,
                adjustedFree,
                meterConfig.ramNeededGb,
                label,
                modelLoaded
            );
        }
    }
}

function _updateMeter(el, used, total, free, needed, label, isLoaded) {
    const fill = el.querySelector('.vram-fill');
    const value = el.querySelector('.vram-value');
    const status = el.querySelector('.field-hint');
    const labelEl = el.querySelector('.field-label');
    if (!fill || !value || !status || !labelEl) return;

    labelEl.textContent = label;
    const usedPercent = total > 0 ? (used / total) * 100 : 0;
    fill.style.width = usedPercent + '%';
    value.textContent = isLoaded
        ? used.toFixed(1) + ' / ' + total.toFixed(1) + ' GB used'
        : free.toFixed(1) + ' / ' + total.toFixed(1) + ' GB free';

    fill.className = 'vram-fill';
    if (isLoaded) {
        // Model is already on GPU — its VRAM is already in "used".
        // Don't compare free against needed; just show it's loaded fine.
        fill.classList.add('green');
        status.textContent = 'Model loaded.';
        status.style.color = 'var(--success)';
    } else if (free >= needed * 2) {
        fill.classList.add('green');
        status.textContent = 'Sufficient space available.';
        status.style.color = 'var(--success)';
    } else if (free >= needed) {
        fill.classList.add('yellow');
        status.textContent = 'Tight fit — should work.';
        status.style.color = 'var(--warning)';
    } else {
        fill.classList.add('red');
        status.textContent = 'Insufficient space (' + needed + ' GB needed).';
        status.style.color = 'var(--error)';
    }
}

async function installOmniVoiceDeps() {
    const btn = document.getElementById('omnivoiceInstallBtn');
    const hint = document.getElementById('omnivoiceInstallHint');
    if (btn) { btn.disabled = true; btn.textContent = 'Installing...'; }
    if (hint) hint.textContent = 'Downloading PyTorch and dependencies. This may take several minutes...';

    // Save current settings so they survive the server restart
    await saveSettings();

    try {
        const resp = await fetch('/api/tts/omnivoice/install-deps', { method: 'POST' });
        const data = await resp.json();

        if (data.status === 'already_installed') {
            showToast('Dependencies already installed', 'info');
            fetchOmniVoiceStatus();
            return;
        }
        if (data.status === 'error') {
            showToast(data.error || 'Install failed', 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Install OmniVoice Dependencies'; }
            if (hint) hint.textContent = data.error || 'Install failed.';
            return;
        }
        if (data.status === 'installing') {
            if (hint) hint.textContent = 'Installing... this may take several minutes.';
            const pollInterval = setInterval(async () => {
                try {
                    const sr = await fetch('/api/tts/omnivoice/install-status');
                    const sd = await sr.json();
                    if (sd.install_complete) {
                        clearInterval(pollInterval);
                        showToast('Dependencies installed! Restarting server...', 'success');
                        if (hint) hint.textContent = 'Server restarting... page will reload shortly.';
                        await fetch('/api/server/restart', { method: 'POST' });
                        setTimeout(() => _pollServerHealth(), 3000);
                    }
                } catch (e) { /* server may be restarting */ }
            }, 5000);
        }
    } catch (e) {
        showToast('Install request failed', 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Install OmniVoice Dependencies'; }
    }
}

function _pollServerHealth() {
    const poll = setInterval(async () => {
        try {
            const resp = await fetch('/health');
            if (resp.ok) { clearInterval(poll); window.location.reload(); }
        } catch (e) { /* still restarting */ }
    }, 2000);
}

async function pretokenizeOmniVoice() {
    const btn = document.getElementById('omnivoicePretokenizeBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Processing...'; }
    try {
        const resp = await fetch('/api/tts/omnivoice/pretokenize', { method: 'POST' });
        const data = await resp.json();
        if (data.status === 'processing') {
            showToast('Processing voice references...', 'success');
        } else if (data.status === 'error') {
            showToast(data.error || 'Processing failed', 'error');
        }
    } catch (e) {
        showToast('Request failed', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Process Voice References'; }
    }
}

const LLM_PROVIDER_HINTS = {
    gemini: 'Google\'s Gemini API with powerful models on a limited free tier. <a href="https://aistudio.google.com/app/apikey" target="_blank">Get your free API key</a>. If you experience errors, you may be at a minute-by-minute or daily limit, in which case we recommend you switch to Ollama Cloud (free) or OpenRouter (best quality).',
        openrouter: '<strong>(Recommended)</strong> Access 100+ AI models through one API. <a href="https://openrouter.ai/" target="_blank">Sign up at openrouter.ai</a> and add credits ($5 minimum purchase - generally lasts a long time). Review <a href="https://openrouter.ai/settings/privacy" target="_blank">OpenRouter privacy settings</a> to control data/privacy preferences.',
    openai: 'OpenAI-compatible third-party endpoints and direct access to OpenAI models (GPT-5, etc). For OpenAI directly: <a href="https://auth.openai.com/create-account" target="_blank">create an account</a>, then get an API key from <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com</a>. OpenAI direct access requires prepaid credits ($5 minimum top-up). New OpenAI accounts often have very low rate limits, which can lead to a poor experience; these OpenAI-specific restrictions may not apply to third-party endpoints. If you use OpenAI directly, we recommend disabling Owl Post and Long-Term Memory until your account reaches Tier 2 or higher.',
    ollama: '<strong>Free tier:</strong> <a href="https://signin.ollama.com/sign-up" target="_blank">Sign up for Ollama Cloud</a>. Ollama Cloud has a generous free tier, but only for small models that do not compare with Gemini or OpenRouter. They are still sufficient for roleplaying. <strong>Monitor your free usage here: <a href="https://ollama.com/settings" target="_blank">ollama.com/settings</a>.</strong> Keep provider features disabled on the free plan because its maximum concurrent request limit is 1, which conflicts with Owl Post, Vision, Input Correction, and memory workflows.',
    llamacpp: 'Use a local or remote llama.cpp server through its OpenAI-compatible API. API key is optional unless your server requires one. <strong>Hardware warning:</strong> llama.cpp is not recommended unless you have an extra GPU with 12-24GB VRAM, ideally 24GB, while using a 24B+ model such as <code>unsloth/gemma-4-26B-A4B-it-qat-GGUF UD-Q4_K_XL</code> (great, slower, smaller) or <code>Qwen/Qwen3.5-35B-A3B Q4</code> (good, faster, bigger). <strong>Never run llama.cpp on the same GPU Hogwarts Legacy is using.</strong> <a href="https://discord.com/channels/1460397759675895820/1476935126901461154" target="_blank">Read our guide on Discord</a>.'
};

const LLM_API_KEY_VALIDATORS = {
    gemini: { prefixes: ['AIza', 'AQ'], label: 'Gemini' },
    openrouter: { prefix: 'sk-or-v1-', label: 'OpenRouter' }
};

function validateLLMApiKey(value, providerId) {
    if (!value || value === '********') return null;
    const rule = LLM_API_KEY_VALIDATORS[providerId];
    if (!rule) return null;
    const prefixes = rule.prefixes || [rule.prefix];
    // Still typing a valid prefix - don't flag yet
    if (prefixes.some(prefix => value.length < prefix.length && prefix.startsWith(value))) return null;
    if (!prefixes.some(prefix => value.startsWith(prefix))) {
        const expected = prefixes.map(prefix => `"${prefix}"`).join(' or ');
        return `\u26a0\ufe0f ${rule.label} keys start with ${expected} — this doesn't look right. Make sure you're not pasting your Voice/TTS key here.`;
    }
    return null;
}

// Gemini defaults follow Google's expected preview deprecation dates.
const GEMINI_3_5_SWITCH_DATE = new Date(2027, 5, 1);
const GEMINI_3_5_ENABLED = new Date() >= GEMINI_3_5_SWITCH_DATE;
const GEMINI_CHAT_DEFAULT = GEMINI_3_5_ENABLED ? 'gemini-3.5-flash' : 'gemini-3-flash-preview';
const GEMINI_CHAT_DEFAULT_OR = GEMINI_3_5_ENABLED ? 'google/gemini-3.5-flash' : 'google/gemini-3-flash-preview';

const GEMINI_2_5_FLASH_LITE_SWITCH_DATE = new Date(2026, 7, 3);
const GEMINI_3_1_FLASH_LITE_SWITCH_DATE = new Date(2027, 4, 7);

function applyDatedGeminiPresetDefaults(presets) {
    const replacements = new Map();
    if (new Date() >= GEMINI_2_5_FLASH_LITE_SWITCH_DATE) {
        replacements.set('gemini-2.5-flash-lite', 'gemini-3.1-flash-lite');
        replacements.set('google/gemini-2.5-flash-lite', 'google/gemini-3.1-flash-lite');
    }
    if (new Date() >= GEMINI_3_1_FLASH_LITE_SWITCH_DATE) {
        replacements.set('gemini-3.1-flash-lite', 'gemini-3.5-flash-lite');
        replacements.set('google/gemini-3.1-flash-lite', 'google/gemini-3.5-flash-lite');
    }

    for (const providerPresets of Object.values(presets)) {
        for (const [key, model] of Object.entries(providerPresets)) {
            if (typeof model !== 'string') continue;
            const separatorIndex = model.indexOf(':');
            const baseModel = separatorIndex === -1 ? model : model.slice(0, separatorIndex);
            const suffix = separatorIndex === -1 ? '' : model.slice(separatorIndex);
            let currentModel = baseModel;
            let replacement = replacements.get(currentModel);
            while (replacement) {
                currentModel = replacement;
                replacement = replacements.get(currentModel);
            }
            if (currentModel !== baseModel) {
                providerPresets[key] = `${currentModel}${suffix}`;
            }
        }
    }
}

// Model presets per provider - loaded from shared JSON file (with fallback)
let MODEL_PRESETS = null;
let MODEL_FIELDS_PATHS = null;
let MODEL_PROVIDER_ROUTE_PRESETS = {};

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
        MODEL_PROVIDER_ROUTE_PRESETS = data._provider_routes || {};

        // Apply Gemini chat default date logic (always, to ensure consistency)
        if (MODEL_PRESETS.gemini) {
            MODEL_PRESETS.gemini.chat = GEMINI_CHAT_DEFAULT;
        }
        if (MODEL_PRESETS.openrouter) {
            MODEL_PRESETS.openrouter.chat = GEMINI_CHAT_DEFAULT_OR;
        }
        applyDatedGeminiPresetDefaults(MODEL_PRESETS);

        console.log('[ModelPresets] Loaded presets for providers:', Object.keys(MODEL_PRESETS));
        return MODEL_PRESETS;
    } catch (e) {
        console.error('[ModelPresets] Failed to load presets, using fallback:', e);
        // Fallback to hardcoded defaults if JSON load fails
        MODEL_PRESETS = getHardcodedPresets();
        MODEL_PROVIDER_ROUTE_PRESETS = getHardcodedProviderRoutePresets();
        return MODEL_PRESETS;
    }
}

function getHardcodedProviderRoutePresets() {
    return {
        openrouter: {
            chat: ['google-ai-studio', 'google-vertex'],
            vision: ['google-ai-studio', 'google-vertex'],
            target: ['mistral', 'deepinfra'],
            interjection: ['google-ai-studio', 'google-vertex'],
            commentary: ['mistral', 'deepinfra'],
            inputCorrection: ['wandb', 'groq', 'deepinfra', 'novita'],
            chapter: ['google-ai-studio', 'google-vertex'],
            prose: ['google-ai-studio', 'google-vertex'],
            graphiti: ['google-ai-studio', 'google-vertex'],
            graphitiSmall: ['google-ai-studio', 'google-vertex'],
            reranker: ['wandb', 'groq', 'deepinfra', 'novita'],
            owlSummarize: ['google-ai-studio', 'google-vertex'],
            locationResolver: ['google-ai-studio', 'google-vertex']
        }
    };
}

function getHardcodedPresets() {
    // Fallback presets if JSON fails to load (must match model_presets.json)
    const presets = {
        gemini: {
            chat: GEMINI_CHAT_DEFAULT,
            vision: 'gemini-3.1-flash-lite',
            target: 'gemini-3.1-flash-lite',
            interjection: 'gemini-3.1-flash-lite',
            commentary: 'gemini-3.1-flash-lite',
            inputCorrection: 'gemini-3.1-flash-lite',
            embedding: 'gemini-embedding-2',
            chapter: 'gemini-3.1-flash-lite',
            prose: 'gemini-3.1-flash-lite',
            graphiti: 'gemini-3.1-flash-lite',
            graphitiSmall: 'gemini-3.1-flash-lite',
            reranker: 'gemini-3.1-flash-lite',
            owlSummarize: 'gemini-3.1-flash-lite',
            locationResolver: 'gemini-3.1-flash-lite'
        },
        openrouter: {
            chat: GEMINI_CHAT_DEFAULT_OR,
            vision: 'google/gemini-3.1-flash-lite',
            target: 'mistralai/mistral-small-3.2-24b-instruct:nitro',
            interjection: 'google/gemini-3.1-flash-lite',
            commentary: 'mistralai/mistral-small-3.2-24b-instruct:nitro',
            inputCorrection: 'meta-llama/llama-3.1-8b-instruct:nitro',
            embedding: 'openai/text-embedding-3-small',
            chapter: 'google/gemini-3.1-flash-lite',
            prose: 'google/gemini-3.1-flash-lite',
            graphiti: 'google/gemini-3.1-flash-lite',
            graphitiSmall: 'google/gemini-3.1-flash-lite',
            reranker: 'meta-llama/llama-3.1-8b-instruct:nitro',
            owlSummarize: 'google/gemini-3.1-flash-lite',
            locationResolver: 'google/gemini-3.1-flash-lite'
        },
        openai: {
            chat: 'gpt-5-mini',
            vision: 'gpt-4.1-nano',
            target: 'gpt-4.1-nano',
            interjection: 'gpt-4.1-nano',
            commentary: 'gpt-4.1-nano',
            inputCorrection: 'gpt-4.1-nano',
            embedding: 'text-embedding-3-small',
            chapter: 'gpt-4.1-nano',
            prose: 'gpt-4.1-nano',
            graphiti: 'gpt-4.1-nano',
            graphitiSmall: 'gpt-4.1-nano',
            reranker: 'gpt-4.1-nano',
            locationResolver: 'gpt-5-nano'
        },
        ollama: {
            chat: 'gemma4:31b-cloud',
            vision: 'gemma4:31b-cloud',
            target: 'gemma4:31b-cloud',
            interjection: 'gemma4:31b-cloud',
            commentary: 'gemma4:31b-cloud',
            inputCorrection: 'gemma4:31b-cloud',
            chapter: 'gemma4:31b-cloud',
            prose: 'gemma4:31b-cloud',
            graphiti: 'gemma4:31b-cloud',
            graphitiSmall: 'gemma4:31b-cloud',
            reranker: 'gemma4:31b-cloud',
            owlSummarize: 'gemma4:31b-cloud',
            locationResolver: 'gemma4:31b-cloud'
        },
        llamacpp: {
            chat: 'local-model',
            vision: 'local-model',
            target: 'local-model',
            interjection: 'local-model',
            commentary: 'local-model',
            inputCorrection: 'local-model',
            chapter: 'local-model',
            prose: 'local-model',
            graphiti: 'local-model',
            graphitiSmall: 'local-model',
            reranker: 'local-model',
            locationResolver: 'local-model'
        }
    };
    applyDatedGeminiPresetDefaults(presets);
    return presets;
}

// Model field mappings: { key: { settingPath, elementId, isAgent } }
// Easy to extend with new model fields
const MODEL_FIELDS = {
    chat: { path: 'conversation.chat_model', elementId: 'conv_chat_model' },
    vision: { path: 'agents.vision.llm.model', elementId: 'agent_vision_llm_model', isAgent: true, agentId: 'vision', prefix: 'llm', fieldId: 'model' },
    target: { path: 'conversation.target_selection_model', elementId: 'conv_target_model' },
    interjection: { path: 'conversation.interjection_model', elementId: 'conv_interjection_model' },
    commentary: { path: 'conversation.commentary_model', elementId: 'background_commentary_model' },
    inputCorrection: { path: 'conversation.input_correction_model', elementId: 'conv_input_correction_model' },
    embedding: { path: 'memory.embedding_model', elementId: 'embeddingModel' },
    chapter: { path: 'memory.chapter_model', elementId: 'chapterModel' },
    prose: { path: 'memory.prose_model', elementId: 'proseModel' },
    graphiti: { path: 'memory.graphiti_model', elementId: 'graphitiModel' },
    graphitiSmall: { path: 'memory.graphiti_small_model', elementId: 'graphitiSmallModel' },
    reranker: { path: 'memory.reranker_model', elementId: 'rerankerModel' },
    owlSummarize: { path: 'owl_post.summarize_model', elementId: 'owlPostSummarizeModel' },
    locationResolver: { path: 'commitment.location_resolver_model', elementId: 'commitment_location_resolver_model' }
};

const OPENROUTER_MODEL_AUTOCOMPLETE_EXTRA_INPUTS = [
    'owlPostOrchestratorModel',
    'owlPostMailModel',
    'owlPostBoardModel'
];

const OPENROUTER_MODEL_AUTOCOMPLETE_MAX_ITEMS = 500;

function getModelProviderPath(field) {
    if (field.providerPath) return field.providerPath;
    if (field.path.endsWith('.model')) return field.path.replace(/\.model$/, '.providers');
    if (field.path.endsWith('_model')) return `${field.path}_providers`;
    return null;
}

function applyProviderRoutePresets(providerId) {
    const routePresets = MODEL_PROVIDER_ROUTE_PRESETS?.[providerId];
    if (!routePresets) return;

    for (const [key, providers] of Object.entries(routePresets)) {
        const field = MODEL_FIELDS[key];
        const providerPath = field ? getModelProviderPath(field) : null;
        if (!providerPath || !Array.isArray(providers)) continue;
        updateSetting(providerPath, providers.slice());
        console.log(`[ModelPresets] ${key} providers: ${providers.join(', ') || 'none'}`);
    }
}

// Default reasoning toggle states per provider and model field
// Only specified fields will have reasoning enabled by default
const REASONING_DEFAULTS = {
    gemini: {
        graphiti: true,          // gemini-3.1-flash-lite
        graphitiSmall: true      // gemini-3.1-flash-lite
    },
    openrouter: {
        graphiti: false,         // google/gemini-3.1-flash-lite
        graphitiSmall: true      // google/gemini-3.1-flash-lite
    },
    openai: {
        graphiti: true,          // gpt-4.1-nano
        graphitiSmall: true      // gpt-4.1-nano
    },
    ollama: {
    },
    llamacpp: {
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
    },
    ollama: {
        'conversation.input_correction_enabled': { default: false, elementId: 'conv_input_correction_enabled' }
    },
    llamacpp: {
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

    applyProviderRoutePresets(newProviderId);

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
    if (window.ProviderRouting) {
        ProviderRouting.refresh();
    }

    updateGemini3TempHint();
}

// API key placeholders per LLM provider
const LLM_API_KEY_PLACEHOLDERS = {
    gemini: 'AIza... or AQ...',
    openrouter: 'sk-or-v1-...',
    openai: 'API key',
    ollama: 'OLLAMA_API_KEY',
    llamacpp: 'Optional API key'
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
        config.llm?.gemini?.api_key || config.llm?.openrouter?.api_key || config.llm?.openai?.api_key || config.llm?.ollama?.api_key || config.llm?.llamacpp?.api_key
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
        // OpenAI reasoning requires Responses API
        if (providerId === 'openai' && config.llm?.openai?.responses_api !== true) {
            masterEnabled = false;
        }
        ReasoningToggle.setMasterEnabled(masterEnabled);
    }
    if (window.ProviderRouting) {
        ProviderRouting.refresh();
    }

    // Disable long-term memory for Gemini due to embedding incompatibility
    updateMemoryAvailability(providerId);
    await refreshOpenRouterModelAutocompletes();
    refreshSetupStateFromConfig();
    restoreProviderSectionScroll('chapterLLMProvider');
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
    lucide?.createIcons?.({ nameAttr: 'data-lucide', attrs: {} });
    updateLLMFeatureAvailability(providerId);

    // OpenAI-specific: conditional visibility of responses_api toggle
    if (providerId === 'openai') {
        applyOpenAIResponsesApiVisibility();
    }
}

function switchSTTProvider(providerId) {
    updateSetting('stt.provider', providerId);
    renderSTTProviderSettings(providerId);
    if (providerId === 'universal') {
        connectUniversalSpeechServer(false);
    } else if (!speechServerIsSelected()) {
        suspendUniversalUiWork();
    } else {
        renderProviderSettings('tts', config.tts?.provider);
        refreshUniversalLoadPlan();
    }
    updateRamMonitoring();
    restoreProviderSectionScroll('chapterSTT');
}

function universalCompatibleASRModels() {
    return universalSpeechState.capabilities?.compatibleASRModels || [];
}

function ensureUniversalASRSelection() {
    const models = universalCompatibleASRModels();
    if (!models.length) return;
    const settings = config.stt.universal ||= {};
    if (!models.some(model => model.id === settings.model)) {
        const previous = settings.model;
        settings.model = universalSpeechState.capabilities.recommendedASRModelId || models[0].id;
        universalSpeechState.asrSelectionWarning = previous
            ? `Saved ASR model “${previous}” is unavailable. ${settings.model} is selected as a draft; save to confirm.`
            : `${settings.model} was selected from the connected server; save to confirm.`;
        markDirty();
    } else {
        universalSpeechState.asrSelectionWarning = '';
    }
}

function universalSharedConnectionCard() {
    const [title, rawDetail, tone] = universalStatusPresentation();
    return `<div class="universal-state-card universal-state-${tone}" id="universalASRConnectionState">
        <div class="universal-state-copy">
            <span class="universal-state-label"><span class="universal-state-dot"></span>${escapeHtml(title)}</span>
            <div class="universal-state-detail">Using the shared Speech Server connection configured under Voice.</div>
            ${rawDetail ? `<div class="universal-state-meta">${escapeHtml(universalFriendlyConnectionDetail(rawDetail))}</div>` : ''}
        </div>
        <button type="button" class="btn btn-sm" onclick="document.getElementById('speech_server_api_url')?.scrollIntoView({behavior:'smooth', block:'center'}); document.getElementById('speech_server_api_url')?.focus();">Configure</button>
        </div>`;
}

function updateUniversalASRConnectionState() {
    if (!universalIsSelected()) {
        updateUniversalConnectionState();
        return;
    }
    const [title, rawDetail, tone] = universalStatusPresentation();
    updateUniversalStateCard(
        document.getElementById('universalASRConnectionState'),
        title,
        'Using the shared Speech Server connection configured under Voice.',
        tone,
        rawDetail ? [universalFriendlyConnectionDetail(rawDetail)] : []
    );
}

function refreshUniversalASRConnectionUI() {
    updateUniversalASRConnectionState();
    refreshUniversalASRModelPanelIfIdle();
    applySimpleMode();
}

function renderUniversalASRModelPanel() {
    const panel = document.getElementById('universalASRModelPanel');
    if (!panel) return;
    const models = universalCompatibleASRModels();
    if (universalSpeechState.status !== 'connected' && !universalSpeechState.capabilities) {
        panel.innerHTML = '';
        return;
    }
    if (!models.length) {
        const message = (universalSpeechState.capabilities?.capabilitiesVersion || 1) < 6
            ? 'This server must be updated to capabilities version 6 before it can provide speech recognition.'
            : 'The server is connected, but no installed ASR model supports the current game language.';
        panel.innerHTML = `<div class="universal-empty-state">${escapeHtml(message)}</div>`;
        return;
    }
    ensureUniversalASRSelection();
    const selected = models.find(model => model.id === config.stt?.universal?.model) || models[0];
    const disabled = universalSpeechState.status !== 'connected';
    const loaded = (universalSpeechState.resources?.loadedModelIds || []).includes(selected.id);
    const stackSelection = universalStackSelection();
    const stackLoaded = universalSelectedStackLoaded(stackSelection.model, stackSelection.profile);
    const missing = universalMissingStackComponents(stackSelection.model, stackSelection.profile);
    const planMatches = universalLoadPlanMatches(
        universalSpeechState.loadPlan, stackSelection.model, stackSelection.profile
    );
    const fit = planMatches ? universalSpeechState.loadPlan.fit?.status || 'unknown' : 'unknown';
    const capacityOk = universalResidentCapacityOk();
    const residentLimit = universalResidentLimit();
    const capacityWarning = capacityOk ? ''
        : `The server allows ${residentLimit} resident model ${residentLimit === 1 ? 'slot' : 'slots'}; remote TTS and ASR need two.`;
    const installTarget = universalCurrentInstallTarget();
    const installCard = renderUniversalInstall(installTarget);
    panel.innerHTML = `<fieldset class="universal-model-fieldset" ${disabled ? 'disabled' : ''}>
        <legend>Speech recognition model</legend>
        <div class="field-group">
            <label class="field-label" for="universalASRModelInput">Model</label>
            <div class="model-autocomplete-combobox universal-model-combobox">
                <input type="text" id="universalASRModelInput" value="${escapeHtml(selected.name)}" autocomplete="off">
                <button type="button" class="autocomplete-dropdown-btn" aria-label="Show all ASR models">▼</button>
            </div>
            ${universalSpeechState.asrSelectionWarning ? `<p class="field-hint universal-warning">${escapeHtml(universalSpeechState.asrSelectionWarning)}</p>` : ''}
        </div>
        <div class="universal-model-summary universal-model-card">
            <div class="universal-model-card-header">
                <strong class="universal-model-card-name">${escapeHtml(selected.name)}</strong>
                <span class="universal-badge-row">
                    ${selected.recommended ? '<span class="universal-badge universal-badge-recommended">Recommended</span>' : ''}
                    ${loaded ? '<span class="universal-badge universal-badge-loaded">ASR loaded</span>' : ''}
                    ${selected.installed === false ? '<span class="universal-badge">Download required</span>' : ''}
                </span>
            </div>
            <div class="universal-model-description">${escapeHtml(selected.description)}</div>
            <div class="universal-model-spec-grid">
                <div class="universal-model-spec"><span class="universal-model-spec-label">Languages</span><strong>${selected.languages.length} supported</strong><span class="universal-model-spec-note">Automatic detection${selected.transcription?.automaticLanguageDetection ? ' available' : ' unavailable'}</span></div>
                <div class="universal-model-spec universal-model-spec-memory"><span class="universal-model-spec-label">Estimated requirement</span><strong>${escapeHtml(universalEstimateText(selected))}</strong><span class="universal-model-spec-note">${escapeHtml(universalEstimateSource(selected))}</span></div>
                <div class="universal-model-spec universal-model-spec-fit"><span class="universal-model-spec-label">Full-stack fit</span><strong class="universal-fit-badge universal-fit-${universalFitTone(fit)}">${escapeHtml(fit)}</strong><span class="universal-model-spec-note">Includes selected remote speech components</span></div>
            </div>
        </div>
        ${installCard}
        <div class="universal-model-actions">
            <button type="button" class="btn btn-primary btn-sm" id="universalASRWarmupButton" onclick="warmupUniversalModel()" ${stackLoaded || installTarget ? 'hidden' : ''} ${capacityOk ? '' : 'disabled'}>${escapeHtml(universalLoadButtonLabel(missing))}</button>
            <span class="universal-fit-badge universal-fit-comfortable" ${stackLoaded ? '' : 'hidden'}>Loaded</span>
        </div>
        <p class="field-hint" id="universalASRWarmupStatus"></p>
        ${capacityWarning ? `<p class="field-hint universal-warning">${escapeHtml(capacityWarning)}</p>` : ''}
        ${universalIsSelected() ? '' : '<div id="universalRemoteResources" class="universal-resource-grid"></div>'}
    </fieldset>`;
    initializeUniversalASRPicker();
    updateUniversalResourceDisplay();
}

function initializeUniversalASRPicker() {
    const input = document.getElementById('universalASRModelInput');
    if (!input || !window.Awesomplete) return;
    const models = universalCompatibleASRModels();
    const selected = models.find(model => model.id === config.stt?.universal?.model) || models[0];
    const byName = new Map(models.map(model => [model.name, model]));
    const awesomplete = new Awesomplete(input, {
        list: models.map(model => model.name), minChars: 0, maxItems: 20, sort: false,
        filter(text, query) {
            const model = byName.get(String(text));
            const haystack = `${model?.name || ''} ${model?.id || ''} ${model?.backend || ''} ${(model?.languages || []).join(' ')}`.toLowerCase();
            return input._universalShowFullList || haystack.includes(String(query || '').toLowerCase());
        },
        item(text) {
            const model = byName.get(String(text));
            const li = document.createElement('li');
            li.innerHTML = `<span class="universal-option-heading"><span class="universal-option-title">${escapeHtml(model?.name || String(text))}</span>${model?.recommended ? '<span class="universal-badge universal-badge-recommended">Recommended</span>' : ''}${model?.installed === false ? '<span class="universal-badge">Download required</span>' : ''}</span><span class="universal-option-detail"><span>${escapeHtml(model?.backend || '')}</span><span>${escapeHtml(universalEstimateText(model || {}))}</span></span>`;
            return li;
        },
    });
    input.closest('.universal-model-combobox')?.querySelector('.autocomplete-dropdown-btn')?.addEventListener('click', () => {
        input._universalShowFullList = true;
        awesomplete.evaluate();
        awesomplete.open();
        input._universalShowFullList = false;
    });
    input.addEventListener('awesomplete-selectcomplete', event => {
        const model = byName.get(event.text?.value || input.value);
        if (!model) return;
        config.stt.universal.model = model.id;
        universalSpeechState.asrSelectionWarning = '';
        universalSpeechState.loadPlan = null;
        markDirty();
        renderUniversalASRModelPanel();
        refreshUniversalLoadPlan();
        refreshUniversalStackInstallPlans();
    });
    input.addEventListener('change', () => {
        if (!byName.has(input.value)) input.value = selected?.name || '';
    });
}

function renderSTTProviderSettings(providerId) {
    const providerConfig = STT_PROVIDERS[providerId];
    const container = document.getElementById('sttProviderSettings');

    if (!providerConfig || !container) {
        console.warn(`No config for stt/${providerId}`);
        return;
    }

    if (providerId === 'universal') {
        container.innerHTML = `<p class="field-hint" style="margin-bottom: var(--space-md);">${providerConfig.description}</p>
            ${speechServerDownloadCTA()}
            ${universalIsSelected() ? universalSharedConnectionCard() : speechServerConnectionEditor()}
            <div id="universalASRModelPanel"></div>`;
        renderUniversalASRModelPanel();
        applySimpleMode();
    } else {
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
    }

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
    const openMicToggleGroup = document.getElementById('open_mic_toggle_group');
    const openMicSettings = document.getElementById('open_mic_settings');
    const openMicEndpointingSettings = document.getElementById('open_mic_endpointing_settings');
    const openMicTimeoutSettings = document.getElementById('open_mic_timeout_settings');
    if (openMicToggleGroup) {
        openMicToggleGroup.style.marginBottom = enabled ? '' : '0';
    }
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
    applySimpleMode();
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

function stripLegacyConfigFields(cfg) {
    if (cfg?.tts?.inworld) {
        delete cfg.tts.inworld.workspace_id;
    }
}

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
    applySimpleMode();
}

function onInworldModelChange(modelValue) {
    applySimpleMode();
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
        if (category === 'tts' && id === 'omnivoice' && _nvidiaDetected === false) {
            disabled = 'disabled';
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

    const simpleHideAttr = field.simple_hide ? ' data-simple-hide="true"' : '';
    let html = `<div class="field-group" data-config-path="${settingPath}" data-field-id="${field.id}"${simpleHideAttr}>`;
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
            const rangeDisplay = field.display_suffix ? `${rangeValue}${field.display_suffix}` : rangeValue;
            const rangeDisplayExpr = field.display_suffix
                ? `this.value + '${field.display_suffix}'`
                : 'this.value';
            html += `<div class="range-wrapper">
                        <input type="range" id="${fieldId}"
                               min="${field.min}" max="${field.max}" step="${field.step}" value="${rangeValue}"
                               oninput="updateRangeValue('${fieldId}', ${rangeDisplayExpr}); updateAgentSetting('${agentId}', '${prefix}', '${field.id}', parseFloat(this.value))">
                        <span class="range-value" id="${fieldId}Value">${rangeDisplay}</span>
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
    applySimpleMode();
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
let voiceManifestIds = [];
let voiceManifestIdsPromise = null;
let characterDisplayNames = {};
let openRouterModelIds = [];
let openRouterModelIdsPromise = null;
let openRouterEmbeddingModelIds = ['openai/text-embedding-3-small'];
let openRouterEmbeddingModelIdsPromise = null;
let openRouterVisionModelIds = [];
let openRouterVisionModelIdsPromise = null;

// In-flight request guards (prevent stacking when server is offline)
let statusCheckInFlight = false;
let historyLoadInFlight = false;
let eventsLoadInFlight = false;
let eventCostsLoadInFlight = false;
let restartInProgress = false;  // Prevents status polling from resetting restart button
let restartWentOffline = false; // Tracks if server went offline during restart
let wasGameAvailable = false;   // Tracks game connection for display name refresh
let currentPlayerContext = { ready: false, player_name: '', normalized_name: '' };
let loadedPlayerContext = { ready: false, player_name: '', normalized_name: '' };
let playerContextCheckInFlight = false;
let lastPlayerDirtyWarningKey = '';

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
        const ttsConfigured = isCurrentTtsConfigured();
        ttsLink.style.display = ttsConfigured ? 'none' : 'flex';
    }

    // Hide "Configure LLM settings first" if LLM is configured
    const llmLink = document.getElementById('setupLlmConfigLink');
    if (llmLink) {
        const llmConfigured = isCurrentLlmConfigured();
        llmLink.style.display = llmConfigured ? 'none' : 'flex';
    }
}

function isCurrentTtsConfigured() {
    const ttsProvider = config.tts?.provider || 'inworld';
    if (ttsProvider === 'universal') {
        return universalSpeechState.status === 'connected'
            && universalCompatibleModels().length > 0;
    }
    return (
        ttsProvider === 'none' ||
        ttsProvider === 'pocket' ||
        ttsProvider === 'neutts' ||
        ttsProvider === 'omnivoice' ||
        ttsProvider === 'omnivoice_cpp' ||
        Boolean(config.tts?.[ttsProvider]?.api_key)
    );
}

function isCurrentLlmConfigured() {
    const llmProvider = config.llm?.provider || 'gemini';
    const legacyKey = config.llm?.api_key;
    const hasProviderSpecificKeys = Boolean(
        config.llm?.gemini?.api_key || config.llm?.openrouter?.api_key || config.llm?.openai?.api_key || config.llm?.ollama?.api_key || config.llm?.llamacpp?.api_key
    );
    const hasApiKey = Boolean(config.llm?.[llmProvider]?.api_key || (legacyKey && !hasProviderSpecificKeys));
    const isLocalOpenAI = llmProvider === 'openai' && config.llm?.openai?.api_url;
    const isLlamaCpp = llmProvider === 'llamacpp' && config.llm?.llamacpp?.api_url;
    return hasApiKey || isLocalOpenAI || isLlamaCpp;
}

function getCurrentSetupLlmModelsFromConfig() {
    const convSettings = config.conversation || {};
    const visionConfig = config.agents?.vision || {};
    const visionSettings = visionConfig.llm || {};
    const memorySettings = config.memory || {};
    const models = {
        chat: convSettings.chat_model || GEMINI_CHAT_DEFAULT_OR,
        target: convSettings.target_selection_model || 'meta-llama/llama-4-scout:nitro',
        interject: convSettings.interjection_model || 'google/gemini-3.1-flash-lite'
    };

    if (visionConfig.enabled !== false && !isLLMFeatureDisabledByProvider('vision')) {
        models.vision = visionSettings.model || 'google/gemini-3.1-flash-lite';
    }
    if (convSettings.input_correction_enabled && convSettings.input_correction_model && !isLLMFeatureDisabledByProvider('input_correction')) {
        models.input_correction = convSettings.input_correction_model;
    }
    if (memorySettings.enabled) {
        for (const [key, settingKey] of [
            ['embedding', 'embedding_model'],
            ['chapter', 'chapter_model'],
            ['prose', 'prose_model'],
            ['graphiti', 'graphiti_model'],
            ['graphiti_small', 'graphiti_small_model'],
            ['reranker', 'reranker_model']
        ]) {
            if (memorySettings[settingKey]) {
                models[key] = memorySettings[settingKey];
            }
        }
    }

    return models;
}

function getEffectiveSetupStatus(status) {
    if (!status) return status;

    const effective = JSON.parse(JSON.stringify(status));
    const currentTtsProvider = config.tts?.provider || effective.steps?.tts?.current_provider || 'inworld';
    const currentLlmProvider = config.llm?.provider || effective.steps?.llm?.current_provider || 'gemini';

    const ttsRunning = effective.running_command === 'test_tts';
    const llmRunning = effective.running_command === 'test_llm';
    const ttsValidForProvider = effective.steps?.tts?.tested && effective.steps?.tts?.tested_provider === currentTtsProvider;
    const llmValidForProvider = effective.steps?.llm?.tested && effective.steps?.llm?.tested_provider === currentLlmProvider;

    if (effective.steps?.tts) {
        effective.steps.tts.current_provider = currentTtsProvider;
        effective.steps.tts.tested = ttsValidForProvider;
        if (!ttsRunning && (!ttsValidForProvider || !isCurrentTtsConfigured())) {
            effective.steps.tts.status = 'not_started';
        }
    }

    if (effective.steps?.llm) {
        effective.steps.llm.current_provider = currentLlmProvider;
        effective.steps.llm.tested = llmValidForProvider;
        effective.steps.llm.models = getCurrentSetupLlmModelsFromConfig();
        if (!llmRunning && (!llmValidForProvider || !isCurrentLlmConfigured())) {
            effective.steps.llm.status = 'not_started';
        }
    }

    effective.complete = Boolean(
        effective.steps?.localization?.status === 'complete' &&
        effective.steps?.voices?.status === 'complete' &&
        effective.steps?.tts?.status === 'complete' &&
        effective.steps?.llm?.status === 'complete'
    );

    return effective;
}

function refreshSetupStateFromConfig() {
    updateSetupConfigLinks();
    if (setupStatus) {
        updateSetupUI(setupStatus, setupStatus);
    }
}

function formatHotkeyLabel(hotkey) {
    const labels = {
        enter: 'Enter',
        middle_mouse: 'Middle Mouse',
        delete: 'Delete',
        home: 'Home',
        end: 'End',
        insert: 'Insert',
        escape: 'Escape',
        backquote: 'Tilde (~)'
    };

    if (!hotkey) return 'Unbound';
    return labels[hotkey] || hotkey.toUpperCase();
}

function getSetupReadyTips() {
    const boldHotkey = (hotkey) => `<strong>${hotkey}</strong>`;
    const chatHotkey = formatHotkeyLabel(config.input?.chat_hotkey || 'enter');
    const stopHotkey = formatHotkeyLabel(config.input?.stop_hotkey || 'delete');
    const modeHotkey = formatHotkeyLabel(config.input?.mode_hotkey || 'home');
    const fpvHotkey = formatHotkeyLabel(config.input?.fpv_hotkey || 'insert');
    const owlPostHotkey = formatHotkeyLabel(config.input?.owlpost_hotkey || 'backquote');
    const sttProvider = config.stt?.provider || 'none';
    const openMicEnabled = config.open_mic?.enabled === true;
    const micEnabled = sttProvider !== 'none';
    const sttHotkey = formatHotkeyLabel(config.stt?.hotkey || 'middle_mouse');
    const micModeText = openMicEnabled
        ? `Press ${boldHotkey(sttHotkey)} to toggle open mic on or off.`
        : `Hold ${boldHotkey(sttHotkey)} for push-to-talk while speaking.`;

    return [
        `To get started, press ${boldHotkey(chatHotkey)} to open chat with a nearby NPC.`,
        micEnabled
            ? micModeText
            : 'Voice input is disabled.',
        `Press ${boldHotkey(stopHotkey)} to stop the current conversation at any time.`,
        `Press ${boldHotkey(modeHotkey)} to cycle conversation modes like Default, 1-to-1, and Continuous.`,
        `Press ${boldHotkey(fpvHotkey)} to toggle first-person view.`,
        `Press ${boldHotkey(owlPostHotkey)} to open Owl Post.`
    ];
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

    const readyTips = getSetupReadyTips()
        .map(tip => `<li style="margin: 0 0 0.5rem;">${tip}</li>`)
        .join('');

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
        <div style="
            text-align: left;
            margin: 0 0 1.5rem;
            padding: 1rem 1.1rem;
            border-radius: 8px;
            background: rgba(244, 228, 193, 0.06);
            border: 1px solid rgba(212, 168, 75, 0.18);
        ">
            <div style="
                font-family: var(--font-display, 'Cinzel', serif);
                color: var(--gold-bright, #d4a84b);
                margin-bottom: 0.7rem;
                font-size: 0.95rem;
            ">Current Bindings</div>
            <ul style="margin: 0; padding-left: 1.2rem; line-height: 1.6; opacity: 0.95;">
                ${readyTips}
            </ul>
        </div>
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

function showConfigConfirmModal({
    title = 'Are you sure?',
    message = '',
    details = [],
    confirmText = 'Continue',
    cancelText = 'Cancel',
    icon = 'alert-triangle'
} = {}) {
    return new Promise(resolve => {
        const existing = document.getElementById('configConfirmOverlay');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.id = 'configConfirmOverlay';
        overlay.className = 'config-modal-overlay';

        const detailItems = details
            .filter(Boolean)
            .map(detail => `<li>${escapeHtml(detail)}</li>`)
            .join('');

        overlay.innerHTML = `
            <div class="config-modal-card" role="dialog" aria-modal="true" aria-labelledby="configConfirmTitle">
                <div class="config-modal-icon"><i data-lucide="${icon}"></i></div>
                <h2 id="configConfirmTitle">${escapeHtml(title)}</h2>
                ${message ? `<p>${escapeHtml(message)}</p>` : ''}
                ${detailItems ? `<ul class="config-modal-details">${detailItems}</ul>` : ''}
                <div class="config-modal-actions">
                    <button type="button" class="btn btn-secondary" id="configConfirmCancel">${escapeHtml(cancelText)}</button>
                    <button type="button" class="btn btn-warning" id="configConfirmOk">${escapeHtml(confirmText)}</button>
                </div>
            </div>
        `;

        const close = confirmed => {
            overlay.remove();
            document.removeEventListener('keydown', onKeyDown);
            resolve(confirmed);
        };

        const onKeyDown = event => {
            if (event.key === 'Escape') close(false);
        };

        overlay.addEventListener('click', event => {
            if (event.target === overlay) close(false);
        });

        document.addEventListener('keydown', onKeyDown);
        document.body.appendChild(overlay);
        document.getElementById('configConfirmCancel')?.addEventListener('click', () => close(false));
        document.getElementById('configConfirmOk')?.addEventListener('click', () => close(true));
        document.getElementById('configConfirmCancel')?.focus();

        if (window.lucide) {
            lucide.createIcons({ nodes: [overlay] });
        }
    });
}

function updateSetupUI(status, previousStatus = null) {
    const effectiveStatus = getEffectiveSetupStatus(status);
    const setupSection = document.getElementById('chapterSetup');
    const navSetup = document.getElementById('navSetup');
    const setupTitle = document.getElementById('setupTitle');
    const setupIntro = document.getElementById('setupIntro');

    // Always show setup section and nav
    setupSection.style.display = 'block';
    navSetup.style.display = 'list-item';

    // Update language dropdown - use local config value if available (user may have just changed it)
    // Otherwise fall back to server status or default
    const currentLanguage = config.setup?.language || effectiveStatus.language || 'EN_US';
    document.getElementById('setupLanguage').value = currentLanguage;

    // Update TTS test text to match current language (only if not already set)
    const ttsTextInput = document.getElementById('setupTtsText');
    if (ttsTextInput && !ttsTextInput.value) {
        ttsTextInput.value = TTS_TEST_TEXTS[currentLanguage] || TTS_TEST_TEXTS['EN_US'];
    }

    // Show/hide warnings banner
    const warningsDiv = document.getElementById('setupWarnings');
    if (warningsDiv) {
        if (effectiveStatus.warnings && effectiveStatus.warnings.length > 0) {
            warningsDiv.textContent = effectiveStatus.warnings.join('\n');
            warningsDiv.style.display = 'block';
        } else {
            warningsDiv.style.display = 'none';
        }
    }

    if (effectiveStatus.complete) {
        // Setup complete: rename, collapse, hide intro
        setupTitle.textContent = 'Setup';
        setupIntro.style.display = 'none';

        // Collapse the section (use class only, not inline style)
        setupSection.classList.add('collapsed');

        stopSetupPolling();
        hideSetupError();

        // Detect real transition from incomplete -> complete
        const previousEffectiveStatus = getEffectiveSetupStatus(previousStatus);
        const justCompleted = Boolean(previousEffectiveStatus && !previousEffectiveStatus.complete && effectiveStatus.complete);

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
        updateSetupStep(3, effectiveStatus.steps.tts);
        updateSetupStep(4, effectiveStatus.steps.llm);

        // Populate LLM models list for retesting
        if (effectiveStatus.steps.llm && effectiveStatus.steps.llm.models) {
            populateLlmModels(effectiveStatus.steps.llm.models);
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
        updateSetupStep(1, effectiveStatus.steps.localization);
        updateSetupStep(2, effectiveStatus.steps.voices);
        updateSetupStep(3, effectiveStatus.steps.tts);
        updateSetupStep(4, effectiveStatus.steps.llm);

        // Step 2 requires step 1 to be complete
        if (effectiveStatus.steps.localization?.status !== 'complete') {
            const step2Btn = document.getElementById('setupStep2Btn');
            const step2SkipBtn = document.getElementById('setupStep2SkipBtn');
            if (step2Btn) step2Btn.disabled = true;
            if (step2SkipBtn) step2SkipBtn.disabled = true;
        }

        // Populate LLM models list if not tested yet
        if (effectiveStatus.steps.llm && effectiveStatus.steps.llm.models && effectiveStatus.steps.llm.status !== 'complete') {
            populateLlmModels(effectiveStatus.steps.llm.models);
        }

        // Update "configure settings first" link visibility
        updateSetupConfigLinks();

        // Show/hide error
        if (effectiveStatus.last_error) {
            showSetupError(effectiveStatus.last_error, inferSetupErrorStep(effectiveStatus));
        } else {
            hideSetupError();
        }

        // Start polling if any step is running
        if (effectiveStatus.running_command) {
            startSetupPolling();
        }
    }

    // Update sticky setup banner
    updateSetupBanner(effectiveStatus.complete);
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
    if (stepNum === 3 && config.tts?.provider === 'none') {
        btn.disabled = true;
        btn.title = 'Voice testing is unavailable while TTS is set to Disabled (Subtitles Only).';
    } else if (stepNum === 3 && !isCurrentTtsConfigured()) {
        btn.disabled = true;
        btn.title = config.tts?.provider === 'universal'
            ? 'Connect to a compatible Universal Speech Server before testing.'
            : 'Configure the selected TTS provider before testing.';
    } else if (stepNum === 3) {
        btn.title = '';
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

function inferSetupErrorStep(status = setupStatus) {
    if (!status?.steps) return null;

    if (status.steps.localization?.status === 'error') return 1;
    if (status.steps.voices?.status === 'error') return 2;
    if (status.steps.tts?.status === 'error') return 3;
    if (status.steps.llm?.status === 'error') return 4;

    switch (status.running_command) {
        case 'extract_localization': return 1;
        case 'extract_voices': return 2;
        case 'test_tts': return 3;
        case 'test_llm': return 4;
        default: return null;
    }
}

let _lastSetupError = null;

function showSetupError(message, stepNum = null) {
    const targetStep = stepNum || inferSetupErrorStep();
    if (!targetStep) return;

    const errorKey = `${targetStep}:${message}`;
    const isNewError = errorKey !== _lastSetupError;

    hideSetupError(false);

    const errorDiv = document.getElementById(`setupStep${targetStep}Error`);
    const errorMsg = document.getElementById(`setupStep${targetStep}ErrorMessage`);
    if (!errorDiv || !errorMsg) return;

    // Convert URLs to clickable links while escaping other HTML
    const urlRegex = /(https?:\/\/[^\s]+)/g;
    const escapedMessage = message.replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const messageWithLinks = escapedMessage.replace(urlRegex, '<a href="$1" target="_blank" style="color: var(--gold); text-decoration: underline;">$1</a>');

    errorMsg.innerHTML = messageWithLinks;
    errorDiv.style.display = 'block';

    // Only scroll on a genuinely new error, not repeated polling of the same one.
    if (isNewError) {
        _lastSetupError = errorKey;
        setTimeout(() => {
            const rect = errorDiv.getBoundingClientRect();
            const topPadding = 120;
            const targetY = window.scrollY + rect.top - topPadding;
            if (rect.bottom > window.innerHeight || rect.top < 0) {
                window.scrollTo({ top: targetY, behavior: 'smooth' });
            }
        }, 50);
    }
}

function hideSetupError(resetLastError = true) {
    if (resetLastError) {
        _lastSetupError = null;
    }
    document.querySelectorAll('.setup-step-error').forEach(errorEl => {
        errorEl.style.display = 'none';
    });
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
            showSetupError(data.error || 'Failed to start extraction', 1);
            btn.disabled = false;
            btnText.textContent = 'Retry';
        }
    } catch (e) {
        showSetupError('Network error: Could not connect to server', 1);
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
            showSetupError(data.error || 'Failed to start voice extraction', 2);
            btn.disabled = false;
            btnText.textContent = 'Retry';
        }
    } catch (e) {
        showSetupError('Network error: Could not connect to server', 2);
        btn.disabled = false;
        btnText.textContent = 'Retry';
    }
}

async function startTtsTest() {
    if (config.tts?.provider === 'none') {
        showToast('Voice testing is unavailable while TTS is disabled.', 'warning');
        return;
    }
    // Require saved configuration before testing
    if (dirty) {
        showToast('Please save your configuration before testing', 'error');
        return;
    }
    if (!isCurrentTtsConfigured()) {
        showToast('Connect to a compatible TTS provider before testing', 'error');
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
            showSetupError(`TTS Error: ${data.error}`, 3);
            btn.disabled = false;
            btnText.textContent = 'Retry';
        }
    } catch (e) {
        const isOffline = e instanceof TypeError || e.message === 'Failed to fetch';
        const msg = isOffline
            ? 'Server is not running. Make sure the game is open before testing.'
            : `Network error: ${e.message}`;
        showSetupError(msg, 3);
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
            showSetupError(`LLM Error: ${data.error}`, 4);
            btn.disabled = false;
            btnText.textContent = 'Retry';
        }
    } catch (e) {
        const isOffline = e instanceof TypeError || e.message === 'Failed to fetch';
        const msg = isOffline
            ? 'Server is not running. Make sure the game is open before testing.'
            : `Network error: ${e.message}`;
        showSetupError(msg, 4);
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
                    <span class="setup-model-name">${escapeHtml(modelId)}</span>
                    <span class="setup-model-uses">(${escapeHtml(info.used_for.join(', '))})</span>
                    ${info.warning ? `<div class="setup-model-warning">${escapeHtml(info.warning)}</div>` : ''}
                    ${info.error ? `<div class="setup-model-error">${escapeHtml(info.error)}</div>` : ''}
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
            const staticBioInput = card.querySelector('.character-static-bio-input');
            const guidanceInput = card.querySelector('.character-editor-guidance-input');
            const titleText = card.querySelector('.character-title-text');

            const name = [
                nameInput?.value || '',
                titleText?.textContent || ''
            ].join(' ').toLowerCase();
            const searchText = [
                staticBioInput?.value || '',
                guidanceInput?.value || ''
            ].join(' ').toLowerCase();

            // Check if search term matches name or profile text
            const matchesName = name.includes(normalizedSearch);
            const matchesGuidance = searchText.includes(normalizedSearch);
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
//  Page Search
// ============================================

const ConfigPageSearch = (() => {
    let searchInput = null;
    let clearButton = null;
    let prevButton = null;
    let nextButton = null;
    let statusDisplay = null;
    let controlsWrap = null;
    let matches = [];
    let activeIndex = -1;

    function updateStatus() {
        if (!statusDisplay) return;

        if (matches.length === 0) {
            statusDisplay.textContent = searchInput && searchInput.value.trim() ? '0 matches' : '';
            return;
        }

        statusDisplay.textContent = `${activeIndex + 1} / ${matches.length}`;
    }

    function updateButtonState() {
        const hasQuery = Boolean(searchInput && searchInput.value.trim());
        if (clearButton) {
            clearButton.style.display = hasQuery ? 'inline-flex' : 'none';
        }
        if (controlsWrap) {
            controlsWrap.style.display = hasQuery ? 'flex' : 'none';
        }

        const disabled = matches.length === 0;
        if (prevButton) prevButton.disabled = disabled;
        if (nextButton) nextButton.disabled = disabled;
    }

    function clearHighlights(root = document.querySelector('.grimoire')) {
        if (!root) return;

        root.querySelectorAll('mark.config-search-match').forEach(mark => {
            const parent = mark.parentNode;
            if (!parent) return;

            parent.replaceChild(document.createTextNode(mark.textContent), mark);
            parent.normalize();
        });

        root.querySelectorAll('.config-search-input-match, .config-search-input-match-active').forEach(el => {
            el.classList.remove('config-search-input-match', 'config-search-input-match-active');
        });

        matches = [];
        activeIndex = -1;
        updateStatus();
        updateButtonState();
    }

    function shouldSkipTextNode(node) {
        if (!node || !node.parentElement) return true;

        const parent = node.parentElement;
        if (!node.nodeValue || !node.nodeValue.trim()) return true;
        if (parent.closest('#configSearchPanel')) return true;
        if (parent.closest('#chapterEvents')) return true;
        if (parent.closest('#historyTableBody, #historyAllTableBody, #historyAllCount, #commitmentRecentBody, #commitmentAllBody, #commitmentAllCount, #commitmentRecentEmpty')) return true;
        if (parent.closest('script, style, option, select, textarea')) return true;
        if (parent.closest('.toast, .toast-container')) return true;

        return false;
    }

    function shouldSkipValueElement(el) {
        if (!el) return true;
        if (el.closest('#configSearchPanel')) return true;
        if (el.closest('#chapterEvents')) return true;
        if (el.closest('#historyTableBody, #historyAllTableBody, #historyAllCount, #commitmentRecentBody, #commitmentAllBody, #commitmentAllCount, #commitmentRecentEmpty')) return true;
        if (el.closest('.toast, .toast-container')) return true;
        if (el.matches('input[type="hidden"], input[type="checkbox"], input[type="radio"], input[type="range"], input[type="file"], input[type="button"], input[type="submit"], input[type="reset"]')) return true;

        return false;
    }

    function collectTextNodes(root) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                return shouldSkipTextNode(node)
                    ? NodeFilter.FILTER_REJECT
                    : NodeFilter.FILTER_ACCEPT;
            }
        });

        const nodes = [];
        let current = walker.nextNode();
        while (current) {
            nodes.push(current);
            current = walker.nextNode();
        }
        return nodes;
    }

    function collectValueMatches(root, queryLower) {
        const valueMatches = [];
        const controls = root.querySelectorAll('input, textarea, select');

        controls.forEach(control => {
            if (shouldSkipValueElement(control)) return;

            const value = control.tagName === 'SELECT'
                ? control.options[control.selectedIndex]?.text || control.value || ''
                : control.value || '';
            const valueLower = value.toLowerCase();
            let matchIndex = valueLower.indexOf(queryLower);

            while (matchIndex !== -1) {
                control.classList.add('config-search-input-match');
                valueMatches.push({
                    type: 'value',
                    element: control,
                    start: matchIndex,
                    end: matchIndex + queryLower.length
                });
                matchIndex = valueLower.indexOf(queryLower, matchIndex + queryLower.length);
            }
        });

        return valueMatches;
    }

    function highlightNode(node, queryLower) {
        const text = node.nodeValue;
        const textLower = text.toLowerCase();
        let startIndex = 0;
        let matchIndex = textLower.indexOf(queryLower, startIndex);

        if (matchIndex === -1) return [];

        const fragment = document.createDocumentFragment();
        const nodeMatches = [];

        while (matchIndex !== -1) {
            if (matchIndex > startIndex) {
                fragment.appendChild(document.createTextNode(text.slice(startIndex, matchIndex)));
            }

            const mark = document.createElement('mark');
            mark.className = 'config-search-match';
            mark.textContent = text.slice(matchIndex, matchIndex + queryLower.length);
            fragment.appendChild(mark);
            nodeMatches.push(mark);

            startIndex = matchIndex + queryLower.length;
            matchIndex = textLower.indexOf(queryLower, startIndex);
        }

        if (startIndex < text.length) {
            fragment.appendChild(document.createTextNode(text.slice(startIndex)));
        }

        node.parentNode.replaceChild(fragment, node);
        return nodeMatches.map(mark => ({ type: 'text', element: mark }));
    }

    function resizePanelTextareas(panel) {
        if (!panel) return;

        const textareas = panel.querySelectorAll('textarea');
        if (textareas.length === 0) return;

        requestAnimationFrame(() => {
            textareas.forEach(textarea => {
                textarea.style.height = 'auto';
                textarea.style.height = textarea.scrollHeight + 'px';
            });
        });
    }

    function expandParents(el) {
        const chapter = el.closest('.chapter');
        if (chapter) {
            chapter.classList.remove('collapsed');
            _resizeChapterTextareas(chapter);
        }

        let panel = el.closest('.sub-panel');
        while (panel) {
            panel.classList.remove('collapsed');
            resizePanelTextareas(panel);
            panel = panel.parentElement?.closest('.sub-panel');
        }
    }

    function focusMatch(index) {
        if (matches.length === 0) return;

        if (activeIndex >= 0 && matches[activeIndex]) {
            const previous = matches[activeIndex];
            const previousElement = previous.element;
            if (previous.type === 'value') {
                previousElement.classList.remove('config-search-input-match-active');
            } else {
                previousElement.classList.remove('config-search-match-active');
            }
        }

        activeIndex = (index + matches.length) % matches.length;
        const match = matches[activeIndex];
        const matchElement = match.element;
        expandParents(matchElement);

        if (match.type === 'value') {
            matchElement.classList.add('config-search-input-match-active');
            matchElement.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
            matchElement.focus({ preventScroll: true });
            if (typeof matchElement.setSelectionRange === 'function') {
                try {
                    matchElement.setSelectionRange(match.start, match.end);
                } catch (e) {
                    // Some form controls expose a value but do not support text selection.
                }
            }
        } else {
            matchElement.classList.add('config-search-match-active');
            matchElement.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
        }

        updateStatus();
        updateButtonState();
    }

    function performSearch(rawQuery) {
        const root = document.querySelector('.grimoire');
        if (!root) return;

        clearHighlights(root);
        const query = rawQuery.trim();
        if (!query) return;

        const queryLower = query.toLowerCase();
        const textNodes = collectTextNodes(root);

        textNodes.forEach(node => {
            matches.push(...highlightNode(node, queryLower));
        });
        matches.push(...collectValueMatches(root, queryLower));

        if (matches.length > 0) {
            focusMatch(0);
        } else {
            updateStatus();
            updateButtonState();
        }
    }

    function clearSearch() {
        if (searchInput) {
            searchInput.value = '';
            clearHighlights();
            searchInput.focus();
        }
    }

    function init() {
        searchInput = document.getElementById('configSearchInput');
        clearButton = document.getElementById('configSearchClear');
        prevButton = document.getElementById('configSearchPrev');
        nextButton = document.getElementById('configSearchNext');
        statusDisplay = document.getElementById('configSearchStatus');
        controlsWrap = document.querySelector('#configSearchPanel .nav-search-controls');

        if (!searchInput) return;

        updateStatus();
        updateButtonState();

        searchInput.addEventListener('input', e => performSearch(e.target.value));
        searchInput.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
                focusMatch(activeIndex + (e.shiftKey ? -1 : 1));
                e.preventDefault();
            } else if (e.key === 'Escape') {
                clearSearch();
                e.preventDefault();
            }
        });

        clearButton?.addEventListener('click', clearSearch);
        prevButton?.addEventListener('click', () => focusMatch(activeIndex - 1));
        nextButton?.addEventListener('click', () => focusMatch(activeIndex + 1));
    }

    return {
        init,
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
        if (initializedTextareas.has(textarea)) {
            resizeTextarea(textarea);
            return;
        }
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
        resizeTextarea,
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
    await Promise.all([loadVoiceManifestIds(), loadCharacterDisplayNames()]);

    await loadConfig();

    // Re-apply setup UI now that local provider/config state is available.
    refreshSetupStateFromConfig();
    // Initialize VRAM monitoring if NeuTTS + GPU is selected
    updateVramMonitoring();

    // Initialize OmniVoice panel if selected
    updateOmniVoicePanel();

    // Initialize OmniVoice (Vulkan) panel if selected
    updateOmniVoiceCppPanel();

    // Initialize reasoning toggles for model inputs (after config loaded)
    if (window.ReasoningToggle) {
        await ReasoningToggle.init(config);

        // Set master toggle state based on current provider's reasoning_enabled
        const provider = config.llm?.provider || 'gemini';
        const masterEnabled = config.llm?.[provider]?.reasoning_enabled === true;
        ReasoningToggle.setMasterEnabled(masterEnabled);
    }
    if (window.ProviderRouting) {
        ProviderRouting.init(config);
    }

    await refreshOpenRouterModelAutocompletes();

    await loadDialogueHistory();
    await loadMigrationStatus();
    await loadVectorMigrationStatus();
    await loadGraphBackups();
    checkServerStatus();

    // Initialize commitments
    loadCommitments();
    loadCommitmentLocations();

    // Initialize auto-expanding textareas
    AutoExpandTextarea.init();

    // Initialize character search filter
    CharacterSearch.init();
    ConfigPageSearch.init();

    // Poll server status every 5 seconds (includes game time)
    setInterval(checkServerStatus, 5000);
    // Poll dialogue history every 10 seconds, commitments every 30 seconds
    setInterval(loadDialogueHistory, 10000);
    setInterval(loadCommitments, 30000);

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

    const owlCustomCharacterList = document.getElementById('owlCustomCharacterList');
    owlCustomCharacterList?.addEventListener('click', (e) => {
        const header = e.target.closest('.character-accordion-header');
        if (!header) return;
        const card = header.closest('.character-card');
        if (!card) return;
        card.classList.toggle('collapsed');

        if (!card.classList.contains('collapsed')) {
            setTimeout(() => {
                card.querySelectorAll('.character-guidance-input').forEach(textarea => {
                    AutoExpandTextarea.initTextarea(textarea);
                });
            }, 50);
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

async function loadVoiceManifestIds() {
    if (voiceManifestIdsPromise) {
        return voiceManifestIdsPromise;
    }

    voiceManifestIdsPromise = fetch('data/voice_manifest.json', { cache: 'no-store' })
        .then(resp => {
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }
            return resp.json();
        })
        .then(data => {
            const voices = data?.voices && typeof data.voices === 'object' ? data.voices : {};
            voiceManifestIds = Object.keys(voices).sort((a, b) => a.localeCompare(b));
            refreshCharacterIdAutocompletes();
            return voiceManifestIds;
        })
        .catch(err => {
            console.error('Failed to load character IDs from voice_manifest.json:', err);
            voiceManifestIds = [];
            return voiceManifestIds;
        });

    return voiceManifestIdsPromise;
}

async function loadCharacterDisplayNames() {
    try {
        const resp = await fetch('/api/character-display-names', { cache: 'no-store' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const mapping = await resp.json();
        setCharacterDisplayNames(mapping);
        // Re-apply display names to already-rendered character cards
        document.querySelectorAll('#bioList .character-card').forEach(card => {
            if (card.classList.contains('player-card')) return;
            const nameInput = card.querySelector('.character-name-input');
            const titleText = card.querySelector('.character-title-text');
            if (nameInput && titleText) {
                titleText.textContent = getCharacterDisplayName(nameInput.value, 'New Character');
            }
        });
    } catch (err) {
        console.error('Failed to load character display names:', err);
    }
}

function setCharacterDisplayNames(mapping = {}) {
    characterDisplayNames = {};
    if (!mapping || typeof mapping !== 'object') {
        return;
    }

    for (const [npcId, displayName] of Object.entries(mapping)) {
        const normalizedId = String(npcId || '').trim();
        const normalizedDisplay = String(displayName || '').trim();
        if (!normalizedId || !normalizedDisplay) continue;
        characterDisplayNames[normalizedId] = normalizedDisplay;
        characterDisplayNames[normalizedId.toLowerCase()] = normalizedDisplay;
    }
}

function getCharacterDisplayName(npcId, fallback = 'New Character') {
    const normalizedId = String(npcId || '').trim();
    if (!normalizedId) return fallback;

    const lower = normalizedId.toLowerCase();
    if (lower === 'player' || lower === 'playermale' || lower === 'playerfemale') {
        return 'Player';
    }

    return characterDisplayNames[normalizedId] || characterDisplayNames[lower] || prettifyVoiceName(normalizedId);
}

async function loadOpenRouterModelIds() {
    if (openRouterModelIdsPromise) {
        return openRouterModelIdsPromise;
    }

    openRouterModelIdsPromise = fetch('/api/openrouter-models', { cache: 'no-store' })
        .then(resp => {
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }
            return resp.json();
        })
        .then(data => {
            openRouterModelIds = Array.isArray(data)
                ? data.filter(id => typeof id === 'string' && id.trim())
                    .sort((a, b) => a.localeCompare(b))
                : [];
            return openRouterModelIds;
        })
        .catch(err => {
            console.error('Failed to load OpenRouter model IDs:', err);
            openRouterModelIds = [];
            return openRouterModelIds;
        });

    return openRouterModelIdsPromise;
}

async function loadOpenRouterEmbeddingModelIds() {
    if (openRouterEmbeddingModelIdsPromise) {
        return openRouterEmbeddingModelIdsPromise;
    }

    openRouterEmbeddingModelIdsPromise = fetch('/api/openrouter-embedding-models', { cache: 'no-store' })
        .then(resp => {
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }
            return resp.json();
        })
        .then(data => {
            openRouterEmbeddingModelIds = Array.isArray(data)
                ? data.filter(id => typeof id === 'string' && id.trim())
                    .sort((a, b) => a.localeCompare(b))
                : ['openai/text-embedding-3-small'];
            if (!openRouterEmbeddingModelIds.includes('openai/text-embedding-3-small')) {
                openRouterEmbeddingModelIds.unshift('openai/text-embedding-3-small');
            }
            return openRouterEmbeddingModelIds;
        })
        .catch(err => {
            console.error('Failed to load OpenRouter embedding model IDs:', err);
            openRouterEmbeddingModelIds = ['openai/text-embedding-3-small'];
            return openRouterEmbeddingModelIds;
        });

    return openRouterEmbeddingModelIdsPromise;
}

async function loadOpenRouterVisionModelIds() {
    if (openRouterVisionModelIdsPromise) {
        return openRouterVisionModelIdsPromise;
    }

    openRouterVisionModelIdsPromise = fetch('/api/openrouter-vision-models', { cache: 'no-store' })
        .then(resp => {
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }
            return resp.json();
        })
        .then(data => {
            openRouterVisionModelIds = Array.isArray(data)
                ? data.filter(id => typeof id === 'string' && id.trim())
                    .sort((a, b) => a.localeCompare(b))
                : [];
            return openRouterVisionModelIds;
        })
        .catch(err => {
            console.error('Failed to load OpenRouter vision model IDs:', err);
            openRouterVisionModelIds = [];
            return openRouterVisionModelIds;
        });

    return openRouterVisionModelIdsPromise;
}

function getOpenRouterModelAutocompleteInputIds() {
    return [
        ...new Set([
            ...Object.values(MODEL_FIELDS).map(field => field.elementId),
            ...OPENROUTER_MODEL_AUTOCOMPLETE_EXTRA_INPUTS
        ])
    ];
}

function isOpenRouterAutocompleteEnabled() {
    return config.llm?.provider === 'openrouter';
}

function getOpenRouterAutocompleteListForInput(input) {
    if (input?.id === 'embeddingModel') return openRouterEmbeddingModelIds;
    if (input?.id === MODEL_FIELDS.vision.elementId) return openRouterVisionModelIds;
    return openRouterModelIds;
}

function ensureModelAutocompleteCombobox(input) {
    if (!input) return null;

    const existing = input.closest('.model-autocomplete-combobox');
    if (existing) return existing;

    const host = input.closest('.input-with-toggle') || input;
    if (!host?.parentNode) return null;

    const combobox = document.createElement('div');
    combobox.className = 'model-autocomplete-combobox';

    host.parentNode.insertBefore(combobox, host);
    combobox.appendChild(host);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'model-autocomplete-dropdown-btn';
    const isEmbeddingInput = input.id === 'embeddingModel';
    button.setAttribute('aria-label', isEmbeddingInput ? 'Browse OpenRouter embedding models' : 'Browse OpenRouter models');
    button.title = isEmbeddingInput ? 'Browse OpenRouter embedding models' : 'Browse OpenRouter models';
    button.innerHTML = '&#9662;';
    combobox.appendChild(button);

    return combobox;
}

function initializeOpenRouterModelAutocomplete(input, enabled) {
    if (!window.Awesomplete || !input) return;
    if (!enabled && !input._openrouterAwesomplete && !input.closest('.model-autocomplete-combobox')) return;

    const combobox = ensureModelAutocompleteCombobox(input);
    const dropdownBtn = combobox?.querySelector('.model-autocomplete-dropdown-btn');
    if (!combobox || !dropdownBtn) return;

    combobox.classList.toggle('autocomplete-disabled', !enabled);
    dropdownBtn.style.display = enabled ? '' : 'none';

    const filterFn = Awesomplete.FILTER_CONTAINS || ((text, userInput) =>
        text.toLowerCase().includes(userInput.toLowerCase()));
    const openRouterFilterFn = (text, userInput) =>
        input._openrouterShowFullList || filterFn(text, userInput);

    if (!input._openrouterAwesomplete) {
        const awesomplete = new Awesomplete(input, {
            list: enabled ? getOpenRouterAutocompleteListForInput(input) : [],
            minChars: 1,
            maxItems: OPENROUTER_MODEL_AUTOCOMPLETE_MAX_ITEMS,
            autoFirst: false,
            sort: false,
            filter: openRouterFilterFn
        });

        input._openrouterAwesomplete = awesomplete;

        input.addEventListener('awesomplete-selectcomplete', () => {
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
        });
    } else {
        input._openrouterAwesomplete.list = enabled ? getOpenRouterAutocompleteListForInput(input) : [];
    }

    dropdownBtn.onclick = (e) => {
        e.preventDefault();
        if (!enabled) return;

        const awesomplete = input._openrouterAwesomplete;
        if (!awesomplete) return;

        input.focus();

        if (!awesomplete.ul.hasAttribute('hidden')) {
            awesomplete.close();
            return;
        }

        const previousMinChars = awesomplete.minChars;
        input._openrouterShowFullList = true;
        awesomplete.minChars = 0;
        awesomplete.evaluate();
        awesomplete.open();
        awesomplete.minChars = previousMinChars;
        input._openrouterShowFullList = false;
    };

    if (!enabled) {
        input._openrouterAwesomplete.close();
    }
}

async function refreshOpenRouterModelAutocompletes() {
    const enabled = isOpenRouterAutocompleteEnabled();
    if (enabled) {
        await Promise.all([
            loadOpenRouterModelIds(),
            loadOpenRouterEmbeddingModelIds(),
            loadOpenRouterVisionModelIds()
        ]);
    }

    for (const inputId of getOpenRouterModelAutocompleteInputIds()) {
        const input = document.getElementById(inputId);
        if (!input) continue;
        initializeOpenRouterModelAutocomplete(input, enabled);
    }

    // Also refresh character LLM model override inputs (dynamic, no static IDs)
    document.querySelectorAll('#bioList .character-llm-model-override').forEach(input => {
        initializeOpenRouterModelAutocomplete(input, enabled);
    });
}

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
            // Update VR badge + VR section visibility
            const vrBadge = document.getElementById('vrBadge');
            const vrSection = document.getElementById('chapterVR');
            const vrNav = document.getElementById('navVR');
            const vrActive = !!data.vr?.active;
            if (vrBadge) {
                if (vrActive) {
                    vrBadge.style.display = '';
                    vrBadge.title = 'VR: ' + (data.vr.backend || 'Active');
                    if (vrBadge.querySelector('[data-lucide]')) lucide.createIcons({ nodes: [vrBadge] });
                } else {
                    vrBadge.style.display = 'none';
                }
            }
            if (vrSection) vrSection.style.display = vrActive ? '' : 'none';
            if (vrNav) vrNav.style.display = vrActive ? '' : 'none';
            // Refresh display names when game first connects (player ready)
            const gameNowAvailable = !!data.game_time?.available;
            if (gameNowAvailable && !wasGameAvailable) {
                loadCharacterDisplayNames();
            }
            wasGameAvailable = gameNowAvailable;
            refreshPlayerContextFromServer();
            // Update game time display + cache for commitments
            cachedGameTime = data.game_time;
            updateGameTimeDisplay(data.game_time);
            // Update NPC schedule display with current game time + player house
            if (typeof updateNpcScheduleDisplay === 'function') {
                updateNpcScheduleDisplay(data.game_time, data.player_house);
            }
            // Update companion/follower panels
            updateCompanionPanel(data.companion);
            updateFollowersPanel(data.followers);
            // Update header background based on player house
            const house = (data.player_house || '').toLowerCase();
            const houseBackgrounds = {
                gryffindor: '/images/gryffindor_commonroom.webp',
                hufflepuff: '/images/hufflepuff_commonroom.webp',
                ravenclaw: '/images/ravenclaw_commonroom.webp',
                slytherin: '/images/slytherin_commonroom.webp'
            };
            const header = document.querySelector('.grimoire-header');
            const houseBackground = houseBackgrounds[house] || '';
            if (header && header.dataset.house !== house) {
                header.dataset.house = house;
                if (houseBackground) {
                    header.style.setProperty('--house-bg', `url('${houseBackground}')`);
                } else {
                    header.style.removeProperty('--house-bg');
                }
            }
        } else {
            throw new Error('Not OK');
        }
    } catch (e) {
        document.getElementById('serverStatus').classList.add('disconnected');
        document.getElementById('serverStatusText').textContent = 'Server Disconnected';
        // Hide companion/follower panels on disconnect
        updateCompanionPanel(null);
        updateFollowersPanel(null);
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

function updateAttentionMeterToggleState() {
    const meterEnabled = document.getElementById('conv_attention_meter_enabled')?.checked;
    const coldToggle = document.getElementById('conv_attention_cold_approach_enabled');
    const coldWrapper = document.getElementById('conv_attention_cold_approach_wrapper');
    if (!coldToggle) return;

    const isDisabled = !meterEnabled;
    coldToggle.disabled = isDisabled;
    if (coldWrapper) {
        coldWrapper.style.opacity = isDisabled ? '0.5' : '1';
        coldWrapper.style.pointerEvents = isDisabled ? 'none' : 'auto';
    }

    // If meter turned off, uncheck cold approach too
    if (isDisabled && coldToggle.checked) {
        coldToggle.checked = false;
        updateSetting('conversation.attention_cold_approach_enabled', false);
    }
}

function updateNarrationToggleState() {
    const narrationEnabled = document.getElementById('conv_narration_enabled')?.checked;
    const childrenWrapper = document.getElementById('conv_narration_children');
    if (!childrenWrapper) return;

    const isDisabled = !narrationEnabled;
    childrenWrapper.querySelectorAll('input, select, textarea, button').forEach(control => {
        control.disabled = isDisabled;
    });
    childrenWrapper.style.opacity = isDisabled ? '0.5' : '1';
    childrenWrapper.style.pointerEvents = isDisabled ? 'none' : 'auto';
}

function updateFreeformEmoteToggleState() {
    const toggle = document.getElementById('conv_freeform_emote_tags');
    const wrapper = document.getElementById('conv_freeform_emote_tags_wrapper');
    if (!toggle) return;

    const emotesEnabled = document.getElementById('conv_emotes_enabled')?.checked === true;
    toggle.disabled = !emotesEnabled;
    if (wrapper) {
        wrapper.style.opacity = emotesEnabled ? '1' : '0.5';
        wrapper.style.pointerEvents = emotesEnabled ? 'auto' : 'none';
    }

    if (!emotesEnabled && toggle.checked) {
        toggle.checked = false;
        updateSetting('conversation.freeform_emote_tags', false);
    }
}

function updateFollowersToggleState() {
    const actionsEnabled = document.getElementById('conv_actions_enabled')?.checked;
    const followersToggle = document.getElementById('conv_followers_enabled');
    const followersWrapper = document.getElementById('conv_followers_wrapper');
    if (!followersToggle) return;

    const isDisabled = !actionsEnabled;
    followersToggle.disabled = isDisabled;
    if (followersWrapper) {
        followersWrapper.style.opacity = isDisabled ? '0.5' : '1';
        followersWrapper.style.pointerEvents = isDisabled ? 'none' : 'auto';
    }
    // If NPC Actions turned off, uncheck followers too
    if (isDisabled && followersToggle.checked) {
        followersToggle.checked = false;
        updateSetting('conversation.followers_enabled', false);
    }
}

function updateCommentaryControlsState() {
    const commentaryEnabled = document.getElementById('conv_commentary_enabled')?.checked;
    const wrapper = document.getElementById('conv_commentary_children');
    if (!wrapper) return;

    const isDisabled = !commentaryEnabled;
    const controls = wrapper.querySelectorAll('input, select, textarea, button');
    controls.forEach(control => {
        control.disabled = isDisabled;
    });
    wrapper.style.opacity = isDisabled ? '0.5' : '1';
    wrapper.style.pointerEvents = isDisabled ? 'none' : 'auto';
}

function updateCompanionPanel(companion) {
    const panel = document.getElementById('companionPanel');
    const body = document.getElementById('companionBody');
    if (!panel || !body) return;

    if (companion && companion.id) {
        body.innerHTML = '';
        const entry = document.createElement('div');
        entry.className = 'tempus-panel-entry';
        const name = document.createElement('span');
        name.className = 'tempus-panel-name';
        name.textContent = companion.name || prettifyVoiceName(companion.id);
        name.title = companion.id;
        entry.appendChild(name);
        const btn = document.createElement('button');
        btn.className = 'tempus-dismiss-btn';
        btn.title = 'Dismiss companion';
        btn.innerHTML = '<i data-lucide="x"></i>';
        btn.onclick = () => dismissCompanion(companion.name || companion.id);
        entry.appendChild(btn);
        body.appendChild(entry);
        panel.classList.remove('tempus-hidden');
        lucide.createIcons({ nodes: [panel] });
    } else {
        panel.classList.add('tempus-hidden');
    }
}

function updateFollowersPanel(followers) {
    const panel = document.getElementById('followersPanel');
    const body = document.getElementById('followersBody');
    if (!panel || !body) return;

    if (followers && followers.length > 0) {
        body.innerHTML = '';
        for (const f of followers) {
            const entry = document.createElement('div');
            entry.className = 'tempus-panel-entry';
            const name = document.createElement('span');
            name.className = 'tempus-panel-name';
            name.textContent = f.name || prettifyVoiceName(f.id);
            name.title = f.id;
            entry.appendChild(name);
            const btn = document.createElement('button');
            btn.className = 'tempus-dismiss-btn';
            btn.title = 'Remove follower';
            btn.innerHTML = '<i data-lucide="x"></i>';
            btn.onclick = () => dismissFollower(f.id, f.name || f.id);
            entry.appendChild(btn);
            body.appendChild(entry);
        }
        panel.classList.remove('tempus-hidden');
        lucide.createIcons({ nodes: [panel] });
    } else {
        panel.classList.add('tempus-hidden');
    }
}

async function dismissCompanion(displayName) {
    if (!confirm(`Dismiss ${displayName} as companion?\n\nThis can break active quests that require a companion.`)) return;
    try {
        await fetch('/api/dismiss-companion', { method: 'POST' });
        showToast(`${displayName} dismissed`, 'success');
    } catch (e) {
        showToast('Failed to dismiss companion', 'error');
    }
}

async function dismissFollower(voiceName, displayName) {
    if (!confirm(`Remove ${displayName} as follower?`)) return;
    try {
        await fetch('/api/dismiss-follower', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voice_name: voiceName })
        });
        showToast(`${displayName} removed`, 'success');
    } catch (e) {
        showToast('Failed to remove follower', 'error');
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
            currentPlayerContext = config.player_context || { ready: false, player_name: '', normalized_name: '' };
            loadedPlayerContext = { ...currentPlayerContext };
            stripLegacyConfigFields(config);
            await populateForm(config);
            updateMemoryDataManagementPlayerState();
            updateOwlPostPlayerState();
        }
    } catch (e) {
        console.error('Failed to load config:', e);
        showToast('Failed to load configuration', 'error');
    } finally {
        isInitializing = false;
    }
}

function getPlayerCardTitle() {
    const playerName = (loadedPlayerContext?.player_name || '').trim();
    return playerName ? `Player: ${playerName}` : 'Player';
}

function getPlayerCardNameLabel() {
    return (loadedPlayerContext?.player_name || '').trim() || 'Player';
}

function updatePlayerCardLabels() {
    document.querySelectorAll('#bioList .character-card.player-card').forEach(card => {
        const title = card.querySelector('.character-title-text');
        if (title) title.textContent = getPlayerCardTitle();
        const nameLabel = card.querySelector('.player-name-label');
        if (nameLabel) nameLabel.textContent = getPlayerCardNameLabel();
    });
}

function isPlayerContextReady() {
    return currentPlayerContext?.ready === true;
}

function setMemoryDataStatus(elementId, text, color = 'var(--text-secondary)') {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.textContent = text || '';
    el.style.color = color;
}

function setMemoryDataButton(elementId, enabled, title = '') {
    const btn = document.getElementById(elementId);
    if (!btn) return;
    btn.style.display = 'inline-block';
    btn.disabled = !enabled;
    btn.title = title;
}

function setMemoryDataManagementNoPlayerState() {
    setMemoryDataStatus('migratePendingCount', '');
    setMemoryDataStatus('vectorMigratePendingCount', '');
    setMemoryDataStatus('graphBackupsStatus', '');

    setMemoryDataButton('migrateBtn', false, '');
    setMemoryDataButton('vectorMigrateBtn', false, '');
    setMemoryDataButton('clearAllMemoriesBtn', false, '');
    setMemoryDataButton('resetMemorySystemBtn', false, '');
    setMemoryDataButton('refreshGraphBackupsBtn', false, '');

    const backupsList = document.getElementById('graphBackupsList');
    if (backupsList) backupsList.innerHTML = '';
}

function updateMemoryDataManagementPlayerState() {
    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return false;
    }

    setMemoryDataButton('clearAllMemoriesBtn', true, 'Clear memory data for the loaded player');
    setMemoryDataButton('resetMemorySystemBtn', true, 'Reset memory data for the loaded player');
    setMemoryDataButton('refreshGraphBackupsBtn', true, 'Refresh memory snapshots for the loaded player');
    return true;
}

function setOwlPostStatus(elementId, text, color = 'var(--text-secondary)') {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.textContent = text || '';
    el.style.color = color;
}

function setOwlPostButton(elementId, enabled, title = '') {
    const btn = document.getElementById(elementId);
    if (!btn) return;
    btn.disabled = !enabled;
    btn.title = title;
}

function setOwlPostNoPlayerState() {
    setOwlPostStatus('owlMailDangerStatus', '');
    setOwlPostStatus('owlBoardsDangerStatus', '');
    setOwlPostStatus('owlLogStatus', '');
    setOwlPostButton('resetOwlMailBtn', false, '');
    setOwlPostButton('resetOwlBoardsBtn', false, '');
    setOwlPostButton('clearOwlLogBtn', false, '');
}

function updateOwlPostPlayerState() {
    if (!isPlayerContextReady()) {
        setOwlPostNoPlayerState();
        return false;
    }

    setOwlPostStatus('owlMailDangerStatus', '');
    setOwlPostStatus('owlBoardsDangerStatus', '');
    setOwlPostStatus('owlLogStatus', '');
    setOwlPostButton('resetOwlMailBtn', true, 'Reset owl mail for the loaded player');
    setOwlPostButton('resetOwlBoardsBtn', true, 'Reset notice boards for the loaded player');
    setOwlPostButton('clearOwlLogBtn', true, 'Clear the Owl Post activity log for the loaded player');
    return true;
}

async function refreshPlayerContextFromServer() {
    if (playerContextCheckInFlight) return;
    playerContextCheckInFlight = true;
    try {
        const response = await fetch('/api/player-context', { cache: 'no-store' });
        if (!response.ok) return;
        const data = await response.json();
        const oldKey = currentPlayerContext?.normalized_name || '';
        const newKey = data?.normalized_name || '';
        const changed = !!newKey && oldKey !== newKey;

        currentPlayerContext = data || { ready: false, player_name: '', normalized_name: '' };
        updateMemoryDataManagementPlayerState();
        updateOwlPostPlayerState();

        if (!changed) return;

        if (dirty) {
            if (lastPlayerDirtyWarningKey !== newKey) {
                lastPlayerDirtyWarningKey = newKey;
                showToast('Player changed. This page is still editing the previous character bio until you save or reload.', 'warning');
            }
            return;
        }

        lastPlayerDirtyWarningKey = '';
        isInitializing = true;
        await loadConfig();
        showToast(`Loaded settings for ${currentPlayerContext.player_name || 'current player'}`, 'info');
    } catch (e) {
        console.error('Failed to refresh player context:', e);
    } finally {
        playerContextCheckInFlight = false;
    }
}

async function populateForm(cfg) {
    // Server
    setCheckbox('modEnabled', cfg.server?.enabled !== false);
    setCheckbox('autoOpenConfig', cfg.server?.auto_open_config !== false);
    setCheckbox('devModeEnabled', cfg.dev?.enabled === true);
    setCheckbox('simpleModeToggle', cfg.ui?.simple_mode !== false);

    // VR
    setCheckbox('vrPresetEnabled', cfg.vr?.preset_enabled !== false);
    setCheckbox('audioVrTracking', cfg.audio?.vr_tracking !== false);

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
        onInworldModelChange(cfg.tts?.inworld?.model || 'inworld-tts-2');
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
    if (currentTTSProvider === 'universal' || currentSTTProvider === 'universal') {
        setTimeout(() => connectUniversalSpeechServer(false), 0);
    } else {
        suspendUniversalUiWork();
    }
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
    updateRangeValue('agent_vision_cooldown_seconds', (cfg.agents?.vision?.cooldown_seconds ?? 5) + 's');
    updateRangeValue('agent_vision_wait_timeout_seconds', (cfg.agents?.vision?.wait_timeout_seconds ?? 5) + 's');
    // Update model placeholders based on current provider (without changing values)
    await updateModelPlaceholders(currentLLMProvider);

    // Audio
    setFieldValue('masterVolume', cfg.audio?.volume ?? 100);
    setFieldValue('narrationVolume', cfg.audio?.narration_volume ?? 80);
    setCheckbox('audioReverb', cfg.audio?.reverb !== false);
    setFieldValue('audioCameraOffset', cfg.audio?.camera_offset ?? 0);

    // Pronunciation replacements
    const pronEl = document.getElementById('pronunciationReplacements');
    if (pronEl) {
        const pronData = cfg.audio?.pronunciation_replacements;
        const hasData = pronData && typeof pronData === 'object' && Object.keys(pronData).length > 0;
        pronEl.value = pronunciationReplacementsToText(hasData ? pronData : DEFAULT_PRONUNCIATION_REPLACEMENTS);
        resizeTextareaAfterProgrammaticUpdate(pronEl);
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
    setCheckbox('archiveTtsWavs', cfg.tts?.archive_enabled !== false);
    setCheckbox('realisticMemory', cfg.history?.realistic_memory !== false);
    const maxLocEntries = cfg.history?.max_location_entries ?? 2;
    setFieldValue('maxLocationEntries', maxLocEntries);
    const maxSpellEntries = cfg.history?.max_spell_entries ?? 3;
    setFieldValue('maxSpellEntries', maxSpellEntries);

    // Commitments
    setCheckbox('commitmentsEnabled', cfg.commitments?.enabled === true);
    setFieldValue('commitment_location_resolver_model', cfg.commitment?.location_resolver_model || '');

    // Owl Post
    setCheckbox('owlPostEnabled', cfg.owl_post?.enabled !== false);
    setCheckbox('owlPostBoardsEnabled', cfg.owl_post?.boards_enabled !== false);
    setFieldValue('owlPostOrchestratorModel', cfg.owl_post?.orchestrator_model || '');
    setFieldValue('owlPostMailModel', cfg.owl_post?.mail_model || '');
    setFieldValue('owlPostBoardModel', cfg.owl_post?.board_model || '');
    setFieldValue('owlPostSummarizeModel', cfg.owl_post?.summarize_model || '');
    const mailInterval = cfg.owl_post?.mail_interval ?? 180;
    const boardInterval = cfg.owl_post?.board_interval ?? 300;
    const convCooldown = cfg.owl_post?.conversation_cooldown ?? 300;
    setFieldValue('owlPostMailInterval', mailInterval);
    updateRangeValue('owlPostMailInterval', mailInterval + 's');
    setFieldValue('owlPostBoardInterval', boardInterval);
    updateRangeValue('owlPostBoardInterval', boardInterval + 's');
    setFieldValue('owlPostConvCooldown', convCooldown);
    updateRangeValue('owlPostConvCooldown', convCooldown + 's');
    const maxBoardPostsPerDay = cfg.owl_post?.max_board_posts_per_day ?? 0;
    setFieldValue('owlPostMaxBoardPostsPerDay', maxBoardPostsPerDay);
    updateRangeValue('owlPostMaxBoardPostsPerDay', maxBoardPostsPerDay == 0 ? 'Unlimited' : maxBoardPostsPerDay);
    const deliveryMin = cfg.owl_post?.delivery_minutes ?? 20;
    setFieldValue('owlPostDeliveryMinutes', deliveryMin);
    updateRangeValue('owlPostDeliveryMinutes', deliveryMin + ' min');
    populateOwlCustomCharacters(cfg.owl_post?.custom_characters || []);
    refreshVoiceReferenceHelpText();

    // Long-Term Memory
    setCheckbox('memoryEnabled', cfg.memory?.enabled === true);
    setFieldValue('embeddingModel', cfg.memory?.embedding_model || '');
    setFieldValue('chapterModel', cfg.memory?.chapter_model || '');
    setFieldValue('proseModel', cfg.memory?.prose_model || '');
    setFieldValue('graphitiModel', cfg.memory?.graphiti_model || '');
    setFieldValue('graphitiSmallModel', cfg.memory?.graphiti_small_model || '');
    setFieldValue('rerankerModel', cfg.memory?.reranker_model || '');
    setFieldValue('maxConcurrency', cfg.memory?.max_concurrency || 2);
    setFieldValue('chapterEntryThreshold', cfg.memory?.chapter_entry_threshold || 30);
    setCheckbox('memoryIncludeCutscene', cfg.memory?.include_cutscene !== false);
    setCheckbox('memoryWhitelistedNpcsOnly', cfg.memory?.whitelisted_npcs_only === true);

    // Show provider-specific concurrency warnings
    updateConcurrencyHints(cfg.llm?.provider || 'gemini');

    // Conversation - Chat Models
    setFieldValue('conv_chat_model', cfg.conversation?.chat_model || '');
    setFieldValue('conv_temperature', cfg.conversation?.temperature || 1.0);
    setFieldValue('conv_max_tokens', cfg.conversation?.max_tokens || 8192);
    updateGemini3TempHint();

    // Conversation - General settings
    setFieldValue('conv_max_turns', cfg.conversation?.max_turns || 6);
    setCheckbox('conv_target_use_crosshair', cfg.conversation?.target_selection_use_crosshair !== false);
    setFieldValue('conv_target_model', cfg.conversation?.target_selection_model || '');
    setFieldValue('conv_speaker_max_tokens', cfg.conversation?.speaker_selection_max_tokens || 512);
    setFieldValue('conv_interjection_model', cfg.conversation?.interjection_model || '');
    setFieldValue('background_commentary_model', cfg.conversation?.commentary_model || '');
    setFieldValue('background_commentary_max_tokens', cfg.conversation?.commentary_max_tokens || 8192);
    updateRangeValue('conv_speaker_max_tokens', (cfg.conversation?.speaker_selection_max_tokens || 512) + ' tokens');
    updateRangeValue('background_commentary_max_tokens', (cfg.conversation?.commentary_max_tokens || 8192) + ' tokens');
    // Input correction: use provider-aware default if not explicitly set
    const inputCorrectionExplicit = cfg.conversation?.input_correction_enabled;
    const inputCorrectionDefault = FEATURE_DEFAULTS[cfg.llm?.provider || 'gemini']?.['conversation.input_correction_enabled']?.default ?? false;
    setCheckbox('conv_input_correction_enabled', inputCorrectionExplicit !== undefined ? inputCorrectionExplicit : inputCorrectionDefault);
    setFieldValue('conv_input_correction_model', cfg.conversation?.input_correction_model || '');
    setCheckbox('conv_actions_enabled', cfg.conversation?.actions_enabled === true);
    setCheckbox('conv_followers_enabled', cfg.conversation?.followers_enabled !== false);
    setCheckbox('conv_conversation_fpv', cfg.conversation?.conversation_fpv === true);
    setFieldValue('conv_conversation_fpv_transition', cfg.conversation?.conversation_fpv_transition || 'normal');
    updateConversationFpvSubSettings(cfg.conversation?.conversation_fpv === true);
    setCheckbox('conv_conversation_look_at_speaker', cfg.conversation?.conversation_look_at_speaker === true);
    setCheckbox('conv_attention_meter_enabled', cfg.conversation?.attention_meter_enabled !== false);
    setCheckbox('conv_attention_cold_approach_enabled', cfg.conversation?.attention_cold_approach_enabled !== false);
    updateAttentionMeterToggleState();
    updateFollowersToggleState();
    // Floo Flame Companions is now compatible with NPC Actions (uses SetSystemicCompanionBP)
    setCheckbox('conv_gear_context', cfg.conversation?.gear_context !== false);
    setCheckbox('conv_mission_context', cfg.conversation?.mission_context !== false);
    setCheckbox('conv_followup_nudge', cfg.conversation?.followup_nudge !== false);
    setCheckbox('conv_farewell_line', cfg.conversation?.farewell_line !== false);
    setCheckbox('conv_sentence_subtitles', cfg.conversation?.sentence_subtitles !== false);
    setCheckbox('conv_auto_mute_ambient', cfg.conversation?.auto_mute_ambient === true);
    setCheckbox('conv_gaze_enabled', cfg.conversation?.gaze_enabled !== false);
    setCheckbox('conv_commentary_enabled', cfg.commentary?.enabled !== false);
    const commentaryCooldown = cfg.commentary?.global_cooldown_seconds ?? 60;
    setFieldValue('conv_commentary_cooldown', commentaryCooldown);
    updateRangeValue('conv_commentary_cooldown', commentaryCooldown + ' s');
    const commentaryWindow = cfg.commentary?.aggregation_window_seconds ?? 4;
    setFieldValue('conv_commentary_window', commentaryWindow);
    updateRangeValue('conv_commentary_window', commentaryWindow + ' s');
    updateCommentaryControlsState();
    setCheckbox('conv_companion_move_enabled', cfg.conversation?.companion_move_enabled !== false);
    setCheckbox('conv_emotes_enabled', cfg.conversation?.emotes_enabled !== false);
    setCheckbox('conv_freeform_emote_tags', cfg.conversation?.freeform_emote_tags !== false);
    updateFreeformEmoteToggleState();
    setCheckbox('conv_narration_enabled', cfg.conversation?.narration_enabled === true);
    setCheckbox('conv_player_narration_enabled', cfg.conversation?.player_narration_enabled !== false);
    setCheckbox('conv_spatial_grounding_enabled', cfg.conversation?.spatial_grounding_enabled !== false);
    updateNarrationToggleState();
    // Companion follow distance (meters)
    const followDist = cfg.conversation?.companion_follow_distance_m ?? 2.0;
    setFieldValue('companion_follow_distance', followDist);
    updateRangeValue('companion_follow_distance', followDist.toFixed(1) + ' m');

    // Input settings
    setFieldValue('input_chat_hotkey', cfg.input?.chat_hotkey || 'enter');
    setFieldValue('input_stop_hotkey', cfg.input?.stop_hotkey || 'delete');
    setFieldValue('input_mode_hotkey', cfg.input?.mode_hotkey || 'home');
    setFieldValue('input_fpv_hotkey', cfg.input?.fpv_hotkey || 'insert');
    setFieldValue('input_owlpost_hotkey', cfg.input?.owlpost_hotkey || 'backquote');
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

    // Owl Post prompts
    if (cfg.prompts?.owl_board_rules) {
        document.getElementById('owlBoardRulesPrompt').value = cfg.prompts.owl_board_rules;
    }
    if (cfg.prompts?.owl_mail_classifier) {
        document.getElementById('owlMailClassifierPrompt').value = cfg.prompts.owl_mail_classifier;
    }
    if (cfg.prompts?.owl_mail_letter) {
        document.getElementById('owlMailLetterPrompt').value = cfg.prompts.owl_mail_letter;
    }
    if (cfg.prompts?.owl_board_thread) {
        document.getElementById('owlBoardThreadPrompt').value = cfg.prompts.owl_board_thread;
    }
    if (cfg.prompts?.owl_board_reply) {
        document.getElementById('owlBoardReplyPrompt').value = cfg.prompts.owl_board_reply;
    }

    // Character settings (static bios + editor guidance + viseme scales + temp mods)
    const staticBios = cfg.prompts?.static_bios || {};
    const editorGuidance = cfg.prompts?.editor_guidance || cfg.prompts?.bios || {};
    await populateCharacters(
        staticBios,
        editorGuidance,
        cfg.lipsync?.npc_scales || {},
        cfg.tts?.npc_temp_modifiers || {},
        cfg.tts?.npc_model_overrides || {},
        cfg.memory?.npc_long_term_memory || {},
        cfg.memory?.whitelisted_npcs_only === true,
        cfg.conversation?.npc_llm_model_overrides || {}
    );
    refreshCharacterLongTermMemoryVisibility(cfg.memory?.whitelisted_npcs_only === true);

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

    applySimpleMode();

    // Update local TTS/STT availability based on game language
    const setupLanguage = cfg.setup?.language || 'EN_US';
    updatePocketTTSAvailability(setupLanguage);
    updateParakeetSTTAvailability(setupLanguage);
    updateCanarySTTAvailability(setupLanguage);
    updateMoonshineSTTAvailability(setupLanguage);

    // Backfill empty model fields from current provider's presets
    const currentProvider = cfg.llm?.provider || 'gemini';
    const presets = MODEL_PRESETS?.[currentProvider];
    if (presets) {
        for (const [key, field] of Object.entries(MODEL_FIELDS)) {
            const element = document.getElementById(field.elementId);
            if (!element) continue;
            const presetModel = presets[key];
            if (presetModel) {
                element.placeholder = presetModel;
                if (!element.value) {
                    element.value = presetModel;
                    if (field.isAgent) {
                        updateAgentSetting(field.agentId, field.prefix, field.fieldId, presetModel);
                    } else {
                        updateSetting(field.path, presetModel);
                    }
                    console.log(`[ModelPresets] Backfilled ${key}: ${presetModel}`);
                }
            }
        }
    }
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

function getActiveVoiceReferenceFolderInfo(language = null) {
    const selectedLanguage = language || config?.setup?.language || 'EN_US';
    const voiceLanguage = UNDUBBED_LANGUAGE_VALUES.has(selectedLanguage) ? 'EN_US' : selectedLanguage;

    if (voiceLanguage === 'EN_US') {
        return {
            selectedLanguage,
            voiceLanguage,
            folderHtml: '<code>voice_references/</code>',
        };
    }

    const folderName = voiceLanguage.toLowerCase();
    return {
        selectedLanguage,
        voiceLanguage,
        folderHtml: `<code>voice_references/${folderName}/</code>`,
    };
}

function getVoiceReferenceHelpHtml(voiceIdExample, options = {}) {
    const info = getActiveVoiceReferenceFolderInfo(options.language);
    const safeVoiceId = escapeHtml(voiceIdExample || 'YourVoiceId');

    return `To use your own voice, place a 5-15 second WAV clip named ` +
        `<code>${safeVoiceId}_reference.wav</code> in ${info.folderHtml}. ` +
        `<a href="#" onclick="openVoiceReferencesFolder(); return false;">Open folder</a>`;
}

function refreshOwlCustomCharacterVoiceHints() {
    document.querySelectorAll('#owlCustomCharacterList .character-card').forEach(card => {
        const hint = card.querySelector('.owl-custom-character-voice-hint');
        const voiceId = card.dataset.npcId || 'YourVoiceId';
        if (!hint) return;
        hint.innerHTML = getVoiceReferenceHelpHtml(voiceId);
    });
}

function refreshVoiceReferenceHelpText() {
    const playerHint = document.getElementById('playerVoiceReferenceHint');
    if (playerHint) {
        playerHint.innerHTML =
            `Optional override for player voice. Leave empty to use the normal player voice. ` +
            `${getVoiceReferenceHelpHtml('YourVoiceId')}`;
    }

    const narratorHint = document.getElementById('narratorVoiceReferenceHint');
    if (narratorHint) {
        narratorHint.innerHTML = getVoiceReferenceHelpHtml('Narrator');
    }

    refreshOwlCustomCharacterVoiceHints();
}

async function openVoiceReferencesFolder() {
    try {
        const response = await fetch('/api/setup/open-voice-references', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language: config?.setup?.language || 'EN_US' }),
        });

        const data = await response.json();
        if (!response.ok) {
            showToast(data.error || 'Failed to open voice references folder', 'error');
            return false;
        }

        return false;
    } catch (e) {
        showToast('Failed to open voice references folder', 'error');
        return false;
    }
}

function getLLMFeatureGateField(feature) {
    return LLM_PROVIDER_FEATURE_GATES.find(gate => gate.feature === feature);
}

function getLLMProviderFeatureDefault(provider, fieldId) {
    const providerConfig = LLM_PROVIDERS[provider];
    for (const field of providerConfig?.fields || []) {
        if (field.type !== 'toggle_group') continue;
        const child = (field.fields || []).find(item => item.id === fieldId);
        if (child) return child.default === true;
    }
    return false;
}

function isLLMFeatureDisabledByProvider(feature, provider = config.llm?.provider || 'gemini') {
    if (feature === 'owl_post' && provider === 'gemini') {
        return true;
    }
    const gate = getLLMFeatureGateField(feature);
    if (!gate) return false;
    const value = config.llm?.[provider]?.[gate.id];
    return value !== undefined ? value === true : getLLMProviderFeatureDefault(provider, gate.id);
}

function setControlsDisabled(ids, disabled) {
    for (const id of ids) {
        const el = document.getElementById(id);
        if (el) el.disabled = disabled;
    }
}

function setDescendantControlsDisabled(containerId, disabled) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.querySelectorAll('input, select, textarea, button').forEach(el => {
        el.disabled = disabled;
    });
}

function updateProviderFeatureWarning(id, disabled, html) {
    const warning = document.getElementById(id);
    if (!warning) return;
    warning.innerHTML = disabled ? html : '';
    warning.style.display = disabled ? 'block' : 'none';
}

function updateLLMFeatureAvailability(provider = config.llm?.provider || 'gemini') {
    const providerLabel = LLM_PROVIDERS[provider]?.label || provider;

    const inputCorrectionDisabled = isLLMFeatureDisabledByProvider('input_correction', provider);
    setControlsDisabled(['conv_input_correction_enabled', 'conv_input_correction_model'], inputCorrectionDisabled);
    updateProviderFeatureWarning(
        'inputCorrectionProviderWarning',
        inputCorrectionDisabled,
        `<i data-lucide="alert-triangle"></i> Input Correction is disabled for ${escapeHtml(providerLabel)} by LLM Provider settings.`
    );

    const visionDisabled = isLLMFeatureDisabledByProvider('vision', provider);
    setDescendantControlsDisabled('agent_vision_settings', visionDisabled);
    const visionPanel = document.getElementById('agentVision');
    if (visionPanel) visionPanel.style.opacity = visionDisabled ? '0.65' : '';
    updateProviderFeatureWarning(
        'visionProviderWarning',
        visionDisabled,
        `<i data-lucide="alert-triangle"></i> Vision is disabled for ${escapeHtml(providerLabel)} by LLM Provider settings.`
    );

    const owlDisabled = isLLMFeatureDisabledByProvider('owl_post', provider);
    const owlReason = provider === 'gemini'
        ? 'Owl Post is disabled for Gemini provider. Switch to OpenRouter, OpenAI, or another compatible provider to enable NPC letters and notice boards.'
        : `Owl Post is disabled for ${escapeHtml(providerLabel)} by LLM Provider settings.`;
    const owlContent = document.querySelector('#chapterOwlPost .chapter-content');
    if (owlContent) owlContent.classList.toggle('disabled', owlDisabled);
    setDescendantControlsDisabled('chapterOwlPost', owlDisabled);
    updateProviderFeatureWarning(
        'owlPostGeminiWarning',
        owlDisabled,
        `<i data-lucide="alert-triangle"></i> ${owlReason}`
    );

    lucide?.createIcons?.({ nameAttr: 'data-lucide', attrs: {} });
}

function updateMemoryAvailability(provider) {
    const memoryToggle = document.getElementById('memoryEnabled');
    const memoryWarning = document.getElementById('memoryGeminiWarning');
    const memoryDisabled = isLLMFeatureDisabledByProvider('memory', provider);
    setControlsDisabled(['embeddingModel'], memoryDisabled);

    if (memoryDisabled) {
        // Disable memory for providers that should not run memory-heavy workflows.
        const wasEnabled = memoryToggle?.checked;
        if (memoryToggle) {
            memoryToggle.checked = false;
            memoryToggle.disabled = true;
        }
        if (memoryWarning) {
            const providerLabel = LLM_PROVIDERS[provider]?.label || provider;
            memoryWarning.innerHTML = `<i data-lucide="alert-triangle"></i> Long-term memory is disabled for ${escapeHtml(providerLabel)} by LLM Provider settings.`;
            memoryWarning.style.display = 'block';
        }
        updateSetting('memory.enabled', false);
        refreshCharacterLabels(false);
        // Notify user if memory was enabled and is now being disabled
        if (wasEnabled && !isInitializing) {
            showToast('Long-term memory disabled for this LLM provider', 'info');
        }
    } else {
        // Enable memory toggle when the provider gate allows it.
        if (memoryToggle) {
            memoryToggle.disabled = false;
        }
        if (memoryWarning) {
            memoryWarning.style.display = 'none';
        }
        refreshCharacterLabels(config?.memory?.enabled === true);
    }

    // Owl Post — same treatment
    updateLLMFeatureAvailability(provider);
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

async function populateCharacters(staticBios = {}, editorGuidance = {}, npcScales = {}, ttsTempMods = {}, modelOverrides = {}, npcLongTermMemory = {}, memoryWhitelistOnly = false, llmModelOverrides = {}) {
    const container = document.getElementById('bioList');
    container.innerHTML = '';

    // Check if memory is enabled for generated bio display
    const memoryEnabled = config?.memory?.enabled || false;

    // Always show Player bio first (not collapsible)
    const playerBio = staticBios.Player || '';
    addCharacterCard('Player', playerBio, '', 1.0, 0, true, memoryEnabled, '', false, memoryWhitelistOnly);

    // Collect all unique NPC names from bios, guidance, scales, temp mods, model overrides, and generated bios
    const allNpcs = new Set([
        ...Object.keys(staticBios).filter(n => n !== 'Player'),
        ...Object.keys(editorGuidance).filter(n => n !== 'Player'),
        ...Object.keys(npcScales).filter(n => n !== 'Player'),
        ...Object.keys(ttsTempMods).filter(n => n !== 'Player'),
        ...Object.keys(modelOverrides).filter(n => n !== 'Player'),
        ...Object.keys(llmModelOverrides).filter(n => n !== 'Player'),
        ...Object.keys(npcLongTermMemory).filter(n => n !== 'Player')
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
        const staticBio = staticBios[name] || '';
        const guidance = editorGuidance[name] || '';
        const scale = npcScales[name] || 1.0;
        const tempMod = ttsTempMods[name] || 0;
        const model = modelOverrides[name] || '';
        const llmModel = llmModelOverrides[name] || '';
        addCharacterCard(
            name,
            staticBio,
            guidance,
            scale,
            tempMod,
            false,
            memoryEnabled,
            model,
            npcLongTermMemory[name] === true,
            memoryWhitelistOnly,
            llmModel
        );
    }

    // Load generated bios for all NPCs (if memory enabled)
    if (memoryEnabled) {
        const cards = document.querySelectorAll('#bioList .character-card:not(.player-card)');
        cards.forEach(card => {
            const npcId = card.dataset.npcId;
            if (npcId && isCharacterMemoryEffectivelyEnabled(card, memoryEnabled)) {
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
    populateCharacters(bios, {}, npcScales, ttsTempMods, modelOverrides);
}

function isMemoryWhitelistOnlyEnabled() {
    return config?.memory?.whitelisted_npcs_only === true;
}

function isNpcLongTermMemoryEnabled(npcId) {
    if (!npcId) return false;
    return config?.memory?.npc_long_term_memory?.[npcId] === true;
}

function setNpcLongTermMemoryEnabled(npcId, enabled) {
    if (!npcId) return;
    config.memory = config.memory || {};
    config.memory.npc_long_term_memory = config.memory.npc_long_term_memory || {};
    if (enabled) {
        config.memory.npc_long_term_memory[npcId] = true;
    } else {
        delete config.memory.npc_long_term_memory[npcId];
    }
}

function isCharacterMemoryEffectivelyEnabled(card, memoryEnabledOverride = null) {
    if (!card || card.classList.contains('player-card')) return false;

    const memoryEnabled = memoryEnabledOverride !== null
        ? memoryEnabledOverride
        : (config?.memory?.enabled === true);
    if (!memoryEnabled) return false;

    if (!isMemoryWhitelistOnlyEnabled()) {
        return true;
    }

    return card.querySelector('.character-long-term-memory-toggle')?.checked === true;
}

function getGeneratedBioSectionHtml() {
    return `
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
}

function refreshCharacterCardMemoryState(card, memoryEnabledOverride = null) {
    if (!card) return;

    const isPlayer = card.classList.contains('player-card');
    const effectiveMemoryEnabled = isCharacterMemoryEffectivelyEnabled(card, memoryEnabledOverride);

    if (isPlayer) {
        return;
    }

    const guidanceFieldGroup = card.querySelector('.character-guidance-field');
    if (guidanceFieldGroup) {
        guidanceFieldGroup.style.display = effectiveMemoryEnabled ? '' : 'none';
    }

    const bioSection = card.querySelector('.generated-bio-section');
    if (effectiveMemoryEnabled) {
        if (!bioSection) {
            const content = card.querySelector('.character-accordion-content');
            if (guidanceFieldGroup && content) {
                guidanceFieldGroup.insertAdjacentHTML('afterend', getGeneratedBioSectionHtml());
            }
        } else {
            bioSection.style.display = '';
        }

        const npcId = card.dataset.npcId;
        if (npcId) {
            loadGeneratedBio(npcId, card);
        }
    } else if (bioSection) {
        bioSection.style.display = 'none';
    }
}

function refreshCharacterLongTermMemoryVisibility(showToggle) {
    document.querySelectorAll('#bioList .character-card:not(.player-card)').forEach(card => {
        const toggleSection = card.querySelector('.character-memory-toggle-section');
        if (toggleSection) {
            toggleSection.style.display = showToggle ? '' : 'none';
        }
        refreshCharacterCardMemoryState(card);
    });
    updateClearNpcButton();
}

/**
 * Refresh generated bio visibility for all character cards based on memory state.
 */
function refreshCharacterLabels(memoryEnabled) {
    document.querySelectorAll('#bioList .character-card').forEach(card => {
        refreshCharacterCardMemoryState(card, memoryEnabled);
    });
    updateClearNpcButton();
}

function onCharacterLongTermMemoryToggle(checkbox) {
    const card = checkbox?.closest('.character-card');
    const npcId = card?.dataset?.npcId?.trim();
    setNpcLongTermMemoryEnabled(npcId, checkbox?.checked === true);
    markDirty();
    refreshCharacterCardMemoryState(card);
    updateClearNpcButton();
}

function handleCharacterIdChange(input) {
    const card = input.closest('.character-card');
    const value = input.value.trim();
    if (card) {
        card.dataset.npcId = value;
    }
    updateCharacterTitle(input);
    markDirty();
}

function initializeCharacterIdAutocomplete(card) {
    if (!window.Awesomplete || !card || card.classList.contains('player-card')) return;

    const input = card.querySelector('.character-name-input');
    if (!input) return;

    if (input._awesomplete) {
        input._awesomplete.list = voiceManifestIds;
        return;
    }

    const combobox = input.closest('.character-id-combobox');
    const dropdownBtn = combobox?.querySelector('.character-id-dropdown-btn');

    const filterFn = Awesomplete.FILTER_CONTAINS || ((text, userInput) =>
        text.toLowerCase().includes(userInput.toLowerCase()));

    const awesomplete = new Awesomplete(input, {
        list: voiceManifestIds,
        minChars: 1,
        maxItems: 12,
        autoFirst: false,
        sort: false,
        filter: filterFn
    });

    input._awesomplete = awesomplete;

    input.addEventListener('awesomplete-selectcomplete', () => {
        handleCharacterIdChange(input);
    });

    if (dropdownBtn) {
        dropdownBtn.addEventListener('click', (e) => {
            e.preventDefault();
            input.focus();

            const previousMinChars = awesomplete.minChars;
            awesomplete.minChars = 0;

            if (awesomplete.ul.childNodes.length === 0) {
                awesomplete.evaluate();
            } else if (awesomplete.ul.hasAttribute('hidden')) {
                awesomplete.open();
            } else {
                awesomplete.close();
            }

            awesomplete.minChars = previousMinChars;
        });
    }
}

function refreshCharacterIdAutocompletes() {
    document.querySelectorAll('#bioList .character-card:not(.player-card)').forEach(card => {
        initializeCharacterIdAutocomplete(card);
    });
}

function addCharacterCard(
    name = '',
    staticBio = '',
    guidance = '',
    visemeScale = 1.0,
    ttsTempMod = 0,
    isPlayer = false,
    memoryEnabled = false,
    modelOverride = '',
    longTermMemoryEnabled = false,
    showLongTermMemoryToggle = false,
    llmModelOverride = ''
) {
    const container = document.getElementById('bioList');
    const card = document.createElement('div');
    card.className = isPlayer ? 'character-card player-card' : 'character-card collapsed';
    card.dataset.npcId = name;  // Store NPC ID for bio fetching
    const effectiveMemoryEnabled = memoryEnabled && (!showLongTermMemoryToggle || longTermMemoryEnabled);

    const displayName = isPlayer ? getPlayerCardTitle() : getCharacterDisplayName(name, 'New Character');
    const toggleIcon = isPlayer ? '' : '<span class="character-accordion-toggle">&#9660;</span>';

    const nameField = isPlayer
        ? `<span class="player-name-label" style="font-family: var(--font-display); font-weight: 600;">${escapeHtml(getPlayerCardNameLabel())}</span>`
        : `
            <div class="character-id-combobox">
                <input type="text" class="character-name-input" value="${escapeHtml(name)}" placeholder="Character ID (e.g. SebastianSallow)" autocomplete="off" spellcheck="false" onchange="handleCharacterIdChange(this)">
                <button type="button" class="character-id-dropdown-btn" aria-label="Browse character IDs" title="Browse character IDs">&#9662;</button>
            </div>
        `;

    const removeBtn = isPlayer
        ? ''
        : `<button class="btn btn-danger" onclick="event.stopPropagation(); removeCharacterCard(this);" style="padding: 4px 8px; font-size: 0.7rem;">Remove</button>`;

    // Format ttsTempMod with sign for display
    const ttsTempModDisplay = ttsTempMod >= 0 ? `+${ttsTempMod.toFixed(2)}` : ttsTempMod.toFixed(2);

    // Generated bio section (only for NPCs when memory enabled, hidden otherwise)
    const generatedBioSection = (isPlayer || !effectiveMemoryEnabled) ? '' : getGeneratedBioSectionHtml();

    const longTermMemorySection = isPlayer ? '' : `
                <div class="field-group character-memory-toggle-section" style="display: ${showLongTermMemoryToggle ? '' : 'none'};">
                    <div class="toggle-wrapper">
                        <span class="toggle-label">Enable Long Term Memory</span>
                        <label class="toggle">
                            <input type="checkbox" class="character-long-term-memory-toggle" ${longTermMemoryEnabled ? 'checked' : ''}
                                   onchange="onCharacterLongTermMemoryToggle(this)">
                            <span class="toggle-track">
                                <span class="toggle-thumb"></span>
                            </span>
                        </label>
                    </div>
                    <p class="field-hint">Allow this NPC to use long-term memory when whitelisted-only mode is enabled.</p>
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
                        <p class="field-hint character-id-hint">Case-sensitive. Use the exact voice ID, usually TitleCase like SebastianSallow. Custom IDs still work.</p>
                    </div>
                    `}
                    <div class="field-group">
                        <label class="field-label">Bio</label>
                        <p class="field-hint">${isPlayer
            ? 'Static facts about the player known to all NPCs. Always included alongside dynamic memories when available.'
            : 'Immutable lore and background. Injected directly when long-term memory is off, and added alongside generated memory when it is on.'}</p>
                        <textarea class="character-guidance-input character-static-bio-input" placeholder="${isPlayer
            ? 'e.g. Ambitious, cunning, from a wealthy family...'
            : 'Character biography, lore, and background...'}"
                                  onchange="markDirty()">${escapeHtml(staticBio)}</textarea>
                    </div>
                    ${isPlayer ? '' : `
                    <div class="field-group character-guidance-field" style="${effectiveMemoryEnabled ? '' : 'display: none;'}">
                        <label class="field-label">Editor's Guidance</label>
                        <p class="field-hint">Optional. Short notes for how the Generated Bio should interpret and preserve this character. Use this for character essence, tone, or interpretation hints. Do not put long lore here; put that in Bio.</p>
                        <textarea class="character-guidance-input character-editor-guidance-input" placeholder="e.g. Keep her dry wit and guarded warmth. Avoid flattening him into comic relief. Preserve that she is deeply loyal to family."
                                  onchange="markDirty()">${escapeHtml(guidance)}</textarea>
                    </div>
                    `}
                    ${generatedBioSection}
                    ${longTermMemorySection}
                    <div class="field-group" data-simple-hide="true">
                        <label class="field-label">Lipsync Intensity</label>
                        <p class="field-hint">0.5 = subtle, 1.0 = normal, 1.5 = exaggerated</p>
                        <div class="range-wrapper">
                            <input type="range" class="character-viseme-scale" min="0.5" max="1.5" step="0.1" value="${visemeScale}"
                                   oninput="this.nextElementSibling.textContent = this.value; markDirty()">
                            <span class="range-value">${visemeScale}</span>
                        </div>
                    </div>
                    ${isPlayer ? '' : `
                    <div class="field-group" data-simple-hide="true">
                        <label class="field-label">TTS Temperature Modifier</label>
                        <p class="field-hint">Defaults are usually best. Only increase if voice sounds flat; too high causes instability.</p>
                        <div class="range-wrapper">
                            <input type="range" class="character-tts-temp-mod" min="-0.9" max="0.9" step="0.05" value="${ttsTempMod}"
                                   oninput="this.nextElementSibling.textContent = (parseFloat(this.value) >= 0 ? '+' : '') + parseFloat(this.value).toFixed(2); markDirty()">
                            <span class="range-value">${ttsTempModDisplay}</span>
                        </div>
                    </div>
                    <div class="field-group" data-simple-hide="true">
                        <label class="field-label">LLM Model Override</label>
                        <p class="field-hint">Use a different LLM for this character's dialogue. Leave empty to use the global chat model.</p>
                        <input type="text" class="character-llm-model-override" value="${escapeHtml(llmModelOverride)}" placeholder="Use global chat model"
                               onchange="markDirty()">
                    </div>
                    <div class="field-group" data-simple-hide="true">
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

    if (!isPlayer) {
        initializeCharacterIdAutocomplete(card);
        // Initialize OpenRouter autocomplete on LLM model override input
        const llmModelInput = card.querySelector('.character-llm-model-override');
        if (llmModelInput) {
            initializeOpenRouterModelAutocomplete(llmModelInput, isOpenRouterAutocompleteEnabled());
        }
        refreshUniversalOverrideAutocompletes();
    }

    // Load generated bio if memory is effectively enabled for this NPC
    if (effectiveMemoryEnabled && !isPlayer && name) {
        loadGeneratedBio(name, card);
    }

    applySimpleMode();

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
    const memoryWhitelistOnly = config?.memory?.whitelisted_npcs_only === true;
    addCharacterCard(name, bio, '', visemeScale, ttsTempMod, isPlayer, memoryEnabled, '', false, memoryWhitelistOnly);
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
    titleText.textContent = getCharacterDisplayName(input.value, 'New Character');
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

function deriveOwlCustomCharacterId(name) {
    return String(name || '').replace(/[^A-Za-z0-9]+/g, '');
}

function updateOwlCustomCharacterEmptyState() {
    const container = document.getElementById('owlCustomCharacterList');
    if (!container) return;

    const existingEmpty = container.querySelector('.owl-custom-character-empty');
    const hasCards = container.querySelector('.character-card') !== null;

    if (!hasCards) {
        if (!existingEmpty) {
            const empty = document.createElement('div');
            empty.className = 'field-hint owl-custom-character-empty';
            empty.textContent = 'No mail-only custom characters yet.';
            container.appendChild(empty);
        }
        return;
    }

    if (existingEmpty) {
        existingEmpty.remove();
    }
}

function updateOwlCustomCharacterName(input) {
    const card = input.closest('.character-card');
    if (!card) return;

    const name = input.value.trim();
    const derivedId = deriveOwlCustomCharacterId(name);
    const title = card.querySelector('.character-title-text');

    if (title) {
        title.textContent = name || 'New Custom Character';
    }
    const voiceHint = card.querySelector('.owl-custom-character-voice-hint');
    if (voiceHint) {
        voiceHint.innerHTML = getVoiceReferenceHelpHtml(derivedId || 'YourVoiceId');
    }

    card.dataset.npcId = derivedId;
    markDirty();
}

function addOwlCustomCharacterCard(name = '', bio = '') {
    const container = document.getElementById('owlCustomCharacterList');
    if (!container) return null;

    const card = document.createElement('div');
    card.className = 'character-card owl-custom-character-card collapsed';
    const titleText = name || 'New Custom Character';
    const derivedId = deriveOwlCustomCharacterId(name);

    card.innerHTML = `
                <div class="character-accordion-header">
                    <div class="character-accordion-title">
                        <span class="character-title-text">${escapeHtml(titleText)}</span>
                    </div>
                    <span class="character-accordion-toggle"><i data-lucide="chevron-down"></i></span>
                </div>
                <div class="character-accordion-content">
                    <div class="field-group">
                        <label class="field-label">Name</label>
                        <p class="field-hint">Display name used in Owl Post.</p>
                        <input type="text" class="owl-custom-character-name" value="${escapeHtml(name)}"
                               placeholder="e.g. John Smith"
                               oninput="updateOwlCustomCharacterName(this)"
                               onchange="markDirty()">
                    </div>
                    <div class="field-group">
                        <label class="field-label">Voice</label>
                        <p class="field-hint owl-custom-character-voice-hint">${getVoiceReferenceHelpHtml(derivedId || 'YourVoiceId')}</p>
                    </div>
                    <div class="field-group">
                        <label class="field-label">Bio</label>
                        <p class="field-hint">Background and personality used when this character replies by mail.</p>
                        <textarea class="character-guidance-input owl-custom-character-bio"
                                  placeholder="e.g. The player's father. Formal, thoughtful, and quietly protective."
                                  oninput="markDirty()"
                                  onchange="markDirty()">${escapeHtml(bio)}</textarea>
                    </div>
                    <div class="character-actions">
                        <button class="btn btn-secondary" onclick="removeOwlCustomCharacterCard(this)">Remove</button>
                    </div>
                </div>
            `;

    container.appendChild(card);
    card.dataset.npcId = derivedId;
    updateOwlCustomCharacterEmptyState();

    const textarea = card.querySelector('.character-guidance-input');
    if (textarea) {
        AutoExpandTextarea.initTextarea(textarea);
    }

    if (window.lucide) {
        lucide.createIcons({ nodes: [card] });
    }

    return card;
}

function populateOwlCustomCharacters(characters = []) {
    const container = document.getElementById('owlCustomCharacterList');
    if (!container) return;

    container.innerHTML = '';
    characters.forEach(entry => {
        if (!entry || typeof entry !== 'object') return;
        addOwlCustomCharacterCard(entry.name || entry.id || '', entry.bio || '');
    });
    updateOwlCustomCharacterEmptyState();
}

function addOwlCustomCharacter() {
    const card = addOwlCustomCharacterCard('', '');
    if (card) {
        card.classList.remove('collapsed');
    }
    markDirty();
}

function removeOwlCustomCharacterCard(button) {
    const card = button.closest('.character-card');
    if (!card) return;
    card.remove();
    updateOwlCustomCharacterEmptyState();
    markDirty();
}

function collectOwlCustomCharactersForSave() {
    const cards = document.querySelectorAll('#owlCustomCharacterList .character-card');
    const characters = [];
    const errors = [];
    const seenIds = new Map();
    const builtInIds = new Set((voiceManifestIds || []).map(id => String(id).toLowerCase()));
    const reservedIds = new Set(['player']);

    cards.forEach((card, index) => {
        const nameInput = card.querySelector('.owl-custom-character-name');
        const bioField = card.querySelector('.owl-custom-character-bio');

        const name = nameInput?.value.trim() || '';
        const id = deriveOwlCustomCharacterId(name);
        const bio = bioField?.value || '';

        card.dataset.npcId = id;

        if (!id) {
            errors.push(`Custom character #${index + 1} needs a name with at least one letter or number.`);
            return;
        }

        const lowerId = id.toLowerCase();
        if (reservedIds.has(lowerId)) {
            errors.push(`"${name}" conflicts with a reserved name. Choose a different name.`);
            return;
        }
        if (builtInIds.has(lowerId)) {
            errors.push(`"${name}" conflicts with an existing in-game voice name. Choose a different name.`);
            return;
        }
        if (seenIds.has(lowerId)) {
            errors.push(`Two custom characters would use the same voice file name. Choose different names.`);
            return;
        }

        seenIds.set(lowerId, index + 1);
        characters.push({ name, id, bio });
    });

    return { characters, errors };
}

// Pagination state
let historyRecentEntries = [];
let historyPageEntries = [];
let historyVisibleCount = 0;
let historyRawCount = null;
let historyTotalPages = 1;
let historySelectedNpcId = 'all';
let historyNpcOptions = [];
let npcChapters = null;      // Chapter data for selected NPC (for dividers)
let historyChaptersNpcId = null;
let historyCurrentPage = 1;
let historyLoadError = '';
let queuedHistoryLoadOptions = null;
const ITEMS_PER_PAGE = 100;

// Edit mode state
let historyEditMode = false;
let selectedHistoryEntries = new Map();  // row key -> raw dialogue row IDs

function normalizeHistorySourceIds(entry) {
    if (!entry || !Array.isArray(entry.sourceEntryIds)) return [];
    return Array.from(new Set(
        entry.sourceEntryIds
            .map(id => Number(id))
            .filter(id => Number.isInteger(id) && id > 0)
    )).sort((a, b) => a - b);
}

function getHistoryEntryKey(entry) {
    const sourceIds = normalizeHistorySourceIds(entry);
    if (sourceIds.length > 0) {
        return `id-${sourceIds.join('-')}`;
    }
    const timestamp = String(entry?.timestamp ?? '0').replace(/\./g, '_');
    return `ts-${timestamp}`;
}

function getHistoryArchiveAudio(entry) {
    const urls = [];
    const paths = [];
    const seenUrls = new Set();
    const seenPaths = new Set();

    function addUnique(values, target, seen) {
        for (const value of values || []) {
            if (typeof value !== 'string' || !value) continue;
            if (seen.has(value)) continue;
            seen.add(value);
            target.push(value);
        }
    }

    addUnique(entry?.ttsArchiveUrl ? [entry.ttsArchiveUrl] : [], urls, seenUrls);
    addUnique(Array.isArray(entry?.ttsArchiveUrls) ? entry.ttsArchiveUrls : [], urls, seenUrls);
    addUnique(entry?.ttsArchivePath ? [entry.ttsArchivePath] : [], paths, seenPaths);
    addUnique(Array.isArray(entry?.ttsArchivePaths) ? entry.ttsArchivePaths : [], paths, seenPaths);

    return {
        url: urls[0] || '',
        path: paths[0] || ''
    };
}

function renderHistoryTextContent(entry, text, entryKey) {
    const safeText = escapeHtml(text || '...');
    const archive = getHistoryArchiveAudio(entry);
    if (!archive.url) {
        return safeText;
    }

    return `
                <div class="history-text-content">
                    <span class="history-text-label">${safeText}</span>
                    <button type="button" class="history-audio-toggle" data-entry-key="${escapeHtml(entryKey)}" data-audio-url="${escapeHtml(archive.url)}" data-audio-path="${escapeHtml(archive.path)}" data-state="stopped" aria-label="Play archived audio" title="Play archived audio">
                        <i data-lucide="play"></i>
                    </button>
                </div>
            `;
}

function refreshHistoryAudioControls(root, syncActive = false) {
    if (syncActive && window.HistoryAudioPlayer && typeof window.HistoryAudioPlayer.syncState === 'function') {
        window.HistoryAudioPlayer.syncState(root);
        return;
    }
    if (window.lucide && root) {
        lucide.createIcons({ nodes: [root] });
    }
}

function stopHistoryAudio() {
    if (window.HistoryAudioPlayer && typeof window.HistoryAudioPlayer.stopAll === 'function') {
        window.HistoryAudioPlayer.stopAll();
    }
}

function getSelectedHistoryEntryIds() {
    const ids = new Set();
    selectedHistoryEntries.forEach(sourceIds => {
        for (const id of sourceIds || []) {
            ids.add(id);
        }
    });
    return Array.from(ids).sort((a, b) => a - b);
}

function pruneHistoryAfterDelete(history, deletedIds) {
    if (!Array.isArray(history) || deletedIds.length === 0) return history;
    const deletedIdSet = new Set(deletedIds);
    return history.filter(entry => {
        const sourceIds = normalizeHistorySourceIds(entry);
        return !sourceIds.some(id => deletedIdSet.has(id));
    });
}

function getHistoryPerspectiveValue() {
    const select = document.getElementById('historyPerspective');
    const value = select?.value || historySelectedNpcId || 'all';
    return value || 'all';
}

function getHistoryViewRequestParams() {
    const params = new URLSearchParams({
        page: String(historyCurrentPage || 1),
        page_size: String(ITEMS_PER_PAGE),
        recent_limit: '10'
    });
    const npcId = getHistoryPerspectiveValue();
    if (npcId && npcId !== 'all') {
        params.set('npc_id', npcId);
    }
    return params;
}

function renderHistoryMessage(message) {
    const recentBody = document.getElementById('historyTableBody');
    const allBody = document.getElementById('historyAllTableBody');
    const countEl = document.getElementById('historyAllCount');
    const colspan = historyEditMode ? 5 : 3;
    const safeMessage = escapeHtml(message || 'No dialogue history yet');
    const markup = `<tr><td colspan="${colspan}" style="text-align:center;opacity:0.6;">${safeMessage}</td></tr>`;

    if (recentBody) {
        recentBody.innerHTML = markup;
        refreshHistoryAudioControls(recentBody, document.getElementById('historyRecent')?.classList.contains('active'));
    }
    if (allBody) {
        allBody.innerHTML = markup;
        refreshHistoryAudioControls(allBody, document.getElementById('historyAll')?.classList.contains('active'));
    }
    if (countEl) {
        countEl.textContent = message || 'No dialogue history yet';
    }
    renderPagination(1);
}

function applyHistoryViewPayload(payload) {
    historyRecentEntries = Array.isArray(payload?.recent_entries) ? payload.recent_entries : [];
    historyPageEntries = Array.isArray(payload?.page_entries) ? payload.page_entries : [];
    historyVisibleCount = Number.isInteger(payload?.visible_count) ? payload.visible_count : 0;
    historyRawCount = Number.isInteger(payload?.raw_count) ? payload.raw_count : null;
    historyTotalPages = Math.max(1, Number(payload?.total_pages) || 1);
    historyCurrentPage = Math.max(1, Number(payload?.page) || 1);
    historySelectedNpcId = payload?.selected_npc_id || 'all';
    historyNpcOptions = Array.isArray(payload?.npc_options) ? payload.npc_options : [];
    historyLoadError = '';
}

async function loadHistoryChaptersForSelection(forceRefresh = false) {
    const npcId = historySelectedNpcId;
    if (!npcId || npcId === 'all') {
        npcChapters = null;
        historyChaptersNpcId = null;
        return;
    }

    if (!forceRefresh && npcChapters && historyChaptersNpcId === npcId) {
        return;
    }

    try {
        const response = await fetch(`/api/memories/chapters/${encodeURIComponent(npcId)}`);
        if (response.ok) {
            npcChapters = await response.json();
            historyChaptersNpcId = npcId;
        } else {
            npcChapters = null;
            historyChaptersNpcId = null;
        }
    } catch (e) {
        npcChapters = null;
        historyChaptersNpcId = null;
    }
}

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

function populatePerspectiveDropdown(options, selectedNpcId = 'all') {
    const select = document.getElementById('historyPerspective');
    if (!select) return;

    // Clear existing options except "All"
    select.innerHTML = '<option value="all">All (default)</option>';

    for (const optionData of (options || [])) {
        const option = document.createElement('option');
        option.value = optionData.id;
        option.textContent = optionData.name || prettifyVoiceName(optionData.id);
        select.appendChild(option);
    }

    const normalizedSelection = [...select.options].some(o => o.value === selectedNpcId)
        ? selectedNpcId
        : 'all';
    select.value = normalizedSelection;
    historySelectedNpcId = normalizedSelection;

    // Update clear button visibility
    updateClearNpcButton();
}

async function filterHistoryByPerspective(resetPage = true, stopAudioPlayback = true) {
    if (stopAudioPlayback) {
        stopHistoryAudio();
    }
    historySelectedNpcId = getHistoryPerspectiveValue();
    if (resetPage) historyCurrentPage = 1;
    if (historySelectedNpcId === 'all' || historyChaptersNpcId !== historySelectedNpcId) {
        npcChapters = null;
        historyChaptersNpcId = null;
    }
    await loadDialogueHistory({ allowDuringEdit: true });
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
            // 1.5 NPC is effectively allowed to use memory when whitelist mode is on
            // 2. NPC has NOT been migrated into the active memory backend yet
            const memoryEnabled = config.memory?.enabled === true;
            const whitelistOnly = isMemoryWhitelistOnlyEnabled();
            const npcMemoryEnabled = !whitelistOnly || isNpcLongTermMemoryEnabled(npcId);
            const hasMemoryFacts = npcChapters?.has_memory_facts === true;

            if (memoryEnabled && npcMemoryEnabled && !hasMemoryFacts) {
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
        `• Player lines addressed only to ${displayName}, with no remaining witnesses, will be deleted\n` +
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
        historySelectedNpcId = 'all';
        historyCurrentPage = 1;
        npcChapters = null;
        historyChaptersNpcId = null;
        await loadDialogueHistory({ allowDuringEdit: true });
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
        `• Extract searchable long-term facts\n\n` +
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
                    historyChaptersNpcId = null;
                    await loadDialogueHistory({ allowDuringEdit: true });
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

async function loadDialogueHistory(options = {}) {
    const allowDuringEdit = options.allowDuringEdit === true;

    // Skip auto-refresh during edit mode to prevent disruption
    if (historyEditMode && !allowDuringEdit) return;
    if (historyLoadInFlight) {
        queuedHistoryLoadOptions = {
            allowDuringEdit: Boolean(queuedHistoryLoadOptions?.allowDuringEdit || allowDuringEdit)
        };
        return;
    }
    historyLoadInFlight = true;
    try {
        const response = await fetchWithTimeout(`/api/dialogue-history/view?${getHistoryViewRequestParams().toString()}`, {}, 8000);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const payload = await response.json();
        applyHistoryViewPayload(payload);
        await loadHistoryChaptersForSelection();

        populatePerspectiveDropdown(historyNpcOptions, historySelectedNpcId);
        populateHistoryTable(historyRecentEntries, true);
        renderAllHistory();
        populateCommitmentCreateNpcDropdown();
    } catch (e) {
        console.error('Failed to load dialogue history:', e);
        historyLoadError = e?.name === 'AbortError'
            ? 'Dialogue history load timed out.'
            : 'Dialogue history failed to load.';
        renderHistoryMessage(historyLoadError);
    } finally {
        historyLoadInFlight = false;
        if (queuedHistoryLoadOptions) {
            const nextOptions = queuedHistoryLoadOptions;
            queuedHistoryLoadOptions = null;
            await loadDialogueHistory(nextOptions);
        }
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
            last.sourceEntryIds = Array.from(new Set([
                ...normalizeHistorySourceIds(last),
                ...normalizeHistorySourceIds(entry)
            ])).sort((a, b) => a - b);
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
                        <th class="history-checkbox-cell"><input type="checkbox" class="history-checkbox history-select-all" onchange="toggleAllVisibleHistoryEntries(this)" title="Select all"></th>
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
        const entryKey = getHistoryEntryKey(entry);
        const sourceIds = normalizeHistorySourceIds(entry);
        const sourceIdsStr = sourceIds.join(',');
        row.dataset.entryKey = entryKey;

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
        // Handle mount events
        else if (entry.type === 'broom' || entry.type === 'mount') {
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

        const isSelected = selectedHistoryEntries.has(entryKey);
        row.className = rowClass + (isSelected ? ' selected' : '');

        if (historyEditMode) {
            row.innerHTML = `
                        <td class="history-checkbox-cell"><input type="checkbox" class="history-checkbox" data-entry-key="${entryKey}" data-source-ids="${sourceIdsStr}" ${isSelected ? 'checked' : ''} onchange="toggleHistoryEntrySelection(this.dataset.entryKey, this.dataset.sourceIds, this)"></td>
                        <td class="history-speaker">${escapeHtml(speaker)}</td>
                        <td class="history-text">${renderHistoryTextContent(entry, text, entryKey)}</td>
                        <td class="history-time">${time}</td>
                        <td class="history-delete-cell"><button class="history-delete-btn" data-entry-key="${entryKey}" data-source-ids="${sourceIdsStr}" onclick="deleteSingleHistoryEntry(this.dataset.entryKey, this.dataset.sourceIds)" title="Delete entry">&#10005;</button></td>
                    `;
        } else {
            row.innerHTML = `
                        <td class="history-speaker">${escapeHtml(speaker)}</td>
                        <td class="history-text">${renderHistoryTextContent(entry, text, entryKey)}</td>
                        <td class="history-time">${time}</td>
                    `;
        }
        tbody.appendChild(row);
    }

    if (collapsed.length === 0) {
        const colspan = historyEditMode ? 5 : 3;
        tbody.innerHTML = `<tr><td colspan="${colspan}" style="text-align:center;opacity:0.6;">No dialogue history yet</td></tr>`;
    }

    refreshHistoryAudioControls(tbody, document.getElementById('historyRecent')?.classList.contains('active'));
}

function renderAllHistory() {
    const pageData = Array.isArray(historyPageEntries) ? historyPageEntries : [];
    const selectedNpcId = historySelectedNpcId || 'all';

    const countEl = document.getElementById('historyAllCount');
    if (selectedNpcId !== 'all') {
        const npcName = prettifyVoiceName(selectedNpcId);
        countEl.textContent = `${historyVisibleCount} entries for ${npcName}`;
    } else {
        const rawCountText = Number.isInteger(historyRawCount) ? ` (${historyRawCount} raw)` : '';
        countEl.textContent = `${historyVisibleCount} entries total${rawCountText}`;
    }

    // Update header for edit mode
    const table = document.querySelector('#historyAll .history-table');
    const thead = table.querySelector('thead tr');
    if (historyEditMode) {
        if (!thead.querySelector('.history-checkbox-cell')) {
            thead.innerHTML = `
                        <th class="history-checkbox-cell"><input type="checkbox" class="history-checkbox history-select-all" onchange="toggleAllVisibleHistoryEntries(this)" title="Select all"></th>
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
        if (!npcChapters || selectedNpcId === 'all') return null;
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
        const entryKey = getHistoryEntryKey(entry);
        const sourceIds = normalizeHistorySourceIds(entry);
        const sourceIdsStr = sourceIds.join(',');
        row.dataset.entryKey = entryKey;

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
        // Handle mount events
        else if (entry.type === 'broom' || entry.type === 'mount') {
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

        const isSelected = selectedHistoryEntries.has(entryKey);
        row.className = rowClass + (isSelected ? ' selected' : '');

        if (historyEditMode) {
            row.innerHTML = `
                        <td class="history-checkbox-cell"><input type="checkbox" class="history-checkbox" data-entry-key="${entryKey}" data-source-ids="${sourceIdsStr}" ${isSelected ? 'checked' : ''} onchange="toggleHistoryEntrySelection(this.dataset.entryKey, this.dataset.sourceIds, this)"></td>
                        <td class="history-speaker">${escapeHtml(speaker)}</td>
                        <td class="history-text">${renderHistoryTextContent(entry, text, entryKey)}</td>
                        <td class="history-time">${time}</td>
                        <td class="history-delete-cell"><button class="history-delete-btn" data-entry-key="${entryKey}" data-source-ids="${sourceIdsStr}" onclick="deleteSingleHistoryEntry(this.dataset.entryKey, this.dataset.sourceIds)" title="Delete entry">&#10005;</button></td>
                    `;
        } else {
            row.innerHTML = `
                        <td class="history-speaker">${escapeHtml(speaker)}</td>
                        <td class="history-text">${renderHistoryTextContent(entry, text, entryKey)}</td>
                        <td class="history-time">${time}</td>
                    `;
        }
        tbody.appendChild(row);
    }

    if (pageData.length === 0) {
        const colspan = historyEditMode ? 5 : 3;
        tbody.innerHTML = `<tr><td colspan="${colspan}" style="text-align:center;opacity:0.6;">No dialogue history yet</td></tr>`;
    }

    refreshHistoryAudioControls(tbody, document.getElementById('historyAll')?.classList.contains('active'));

    // Render pagination
    renderPagination(historyTotalPages);
}

function renderPagination(totalPages) {
    const container = document.getElementById('historyPagination');
    container.innerHTML = '';

    if (totalPages <= 1) return;

    // Previous button
    const prevBtn = document.createElement('button');
    prevBtn.className = 'pagination-btn';
    prevBtn.innerHTML = '&laquo;';
    prevBtn.disabled = historyCurrentPage === 1;
    prevBtn.onclick = async () => {
        stopHistoryAudio();
        historyCurrentPage = Math.max(1, historyCurrentPage - 1);
        await loadDialogueHistory({ allowDuringEdit: true });
    };
    container.appendChild(prevBtn);

    // Page numbers with ellipsis
    const pages = getPaginationRange(historyCurrentPage, totalPages);
    for (const page of pages) {
        if (page === '...') {
            const ellipsis = document.createElement('span');
            ellipsis.className = 'pagination-ellipsis';
            ellipsis.textContent = '...';
            container.appendChild(ellipsis);
        } else {
            const btn = document.createElement('button');
            btn.className = 'pagination-btn' + (page === historyCurrentPage ? ' active' : '');
            btn.textContent = page;
            btn.onclick = async () => {
                stopHistoryAudio();
                historyCurrentPage = page;
                await loadDialogueHistory({ allowDuringEdit: true });
            };
            container.appendChild(btn);
        }
    }

    // Next button
    const nextBtn = document.createElement('button');
    nextBtn.className = 'pagination-btn';
    nextBtn.innerHTML = '&raquo;';
    nextBtn.disabled = historyCurrentPage === totalPages;
    nextBtn.onclick = async () => {
        stopHistoryAudio();
        historyCurrentPage = Math.min(totalPages, historyCurrentPage + 1);
        await loadDialogueHistory({ allowDuringEdit: true });
    };
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

let providerSectionScrollTimeout = null;

function scrollToSection(id, block = 'start') {
    const section = document.getElementById(id);
    if (!section) return;

    // A target may sit inside an auto-collapsed chapter or nested sub-panel.
    // Open the entire ancestor chain before measuring its scroll position.
    let current = section;
    while (current) {
        if (current.classList?.contains('chapter')
            || current.classList?.contains('sub-panel')) {
            current.classList.remove('collapsed');
        }
        current = current.parentElement;
    }

    requestAnimationFrame(() => {
        section.scrollIntoView({ behavior: 'smooth', block });
    });
}

function scrollToTtsSettings() {
    scrollToSection('chapterTTS');
}

function scrollToTtsTest() {
    if (config.tts?.provider === 'none') return;
    scrollToSection('setupStep3', 'center');
}

function restoreProviderSectionScroll(sectionId) {
    if (providerSectionScrollTimeout) {
        clearTimeout(providerSectionScrollTimeout);
    }

    providerSectionScrollTimeout = setTimeout(() => {
        scrollToSection(sectionId);
        providerSectionScrollTimeout = null;
    }, 100);
}

function switchTab(event, tabId) {
    stopHistoryAudio();

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
        loadCommitmentLocations();
        populateCommitmentCreateNpcDropdown();
        initCommitmentDatePickers();
    }

    if (tabId === 'eventsCostTab') {
        loadSystemEventCosts();
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
    if (path === 'setup.language') {
        refreshVoiceReferenceHelpText();
    }
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
        { path: 'conversation.commentary_model', label: 'Companion Commentary Model' },
        { path: 'conversation.input_correction_model', label: 'Input Correction Model', feature: 'input_correction' },
        { path: 'memory.embedding_model', label: 'Embedding Model', feature: 'memory' },
        { path: 'memory.chapter_model', label: 'Chapter Detection Model' },
        { path: 'memory.prose_model', label: 'Memory Prose Model' },
        { path: 'memory.graphiti_model', label: 'Fact Extraction Model' },
        { path: 'memory.graphiti_small_model', label: 'Fact Deduplication Model' },
        { path: 'memory.reranker_model', label: 'Reranker Model' },
        { path: 'agents.vision.llm.model', label: 'Vision Agent Model', feature: 'vision' }
    ];

    for (const field of modelFields) {
        if (field.feature && isLLMFeatureDisabledByProvider(field.feature, provider)) continue;

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
    const btn = document.getElementById('saveConfigBtn');
    btn.classList.add('loading');
    document.getElementById('saveText').innerHTML = '<span class="spinner"></span> Saving...';

    if (window.ProviderRouting) {
        ProviderRouting.sync();
    }

    // Ensure latest API key text is captured even if the field never blurred.
    const llmKeyField = document.getElementById('llmApiKey');
    if (llmKeyField) {
        const rawKey = llmKeyField.value;
        const trimmedKey = typeof rawKey === 'string' ? rawKey.trim() : rawKey;
        if (trimmedKey && trimmedKey !== '********') {
            updateLLMApiKey(trimmedKey);
        }
    }

    // Provider API-key checks are heuristic warnings, not hard save blockers.
    const providerWarnings = validateActiveProviderFields();
    if (providerWarnings.length > 0) {
        btn.classList.remove('loading');
        document.getElementById('saveText').textContent = 'Save Configuration';
        const confirmed = await showConfigConfirmModal({
            title: 'Save anyway?',
            message: 'This looks incorrect, but provider key formats can change. Save this configuration anyway?',
            details: providerWarnings,
            confirmText: 'Save Anyway',
            cancelText: 'Review Settings'
        });
        if (!confirmed) {
            showToast('Save cancelled', 'info');
            return;
        }
        btn.classList.add('loading');
        document.getElementById('saveText').innerHTML = '<span class="spinner"></span> Saving...';
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

    await loadVoiceManifestIds();

    const owlCustomCharacters = collectOwlCustomCharactersForSave();
    if (owlCustomCharacters.errors.length > 0) {
        btn.classList.remove('loading');
        document.getElementById('saveText').textContent = 'Save Configuration';
        showToast('Owl Post custom characters validation failed', 'error');
        alert('Invalid Owl Post custom characters:\n\n' + owlCustomCharacters.errors.join('\n\n'));
        return;
    }

    // Collect character settings (static bios + editor guidance + viseme scales + tts temp modifiers)
    config.prompts = config.prompts || {};
    config.prompts.static_bios = {};
    config.prompts.editor_guidance = {};
    config.lipsync = config.lipsync || {};
    config.lipsync.npc_scales = {};
    config.tts = config.tts || {};
    config.tts.npc_temp_modifiers = {};
    config.tts.npc_model_overrides = {};
    config.conversation = config.conversation || {};
    config.conversation.npc_llm_model_overrides = {};
    config.memory = config.memory || {};
    config.memory.npc_long_term_memory = {};
    config.owl_post = config.owl_post || {};
    config.owl_post.custom_characters = owlCustomCharacters.characters;

    document.querySelectorAll('#bioList .character-card').forEach(card => {
        const isPlayer = card.classList.contains('player-card');
        const name = isPlayer ? 'Player' : (card.querySelector('.character-name-input')?.value.trim() || '');
        const staticBio = card.querySelector('.character-static-bio-input')?.value.trim() || '';
        const guidance = card.querySelector('.character-editor-guidance-input')?.value.trim() || '';
        const visemeScale = parseFloat(card.querySelector('.character-viseme-scale')?.value || '1.0');
        const ttsTempMod = parseFloat(card.querySelector('.character-tts-temp-mod')?.value || '0');
        const modelOverride = card.querySelector('.character-model-override')?.value.trim() || '';
        const llmModelOverride = card.querySelector('.character-llm-model-override')?.value.trim() || '';

        if (name) {
            if (isPlayer || staticBio) {
                config.prompts.static_bios[name] = staticBio;
            }
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
            // Save LLM model override if not empty and not Player
            if (!isPlayer && llmModelOverride) {
                config.conversation.npc_llm_model_overrides[name] = llmModelOverride;
            }
            if (!isPlayer && card.querySelector('.character-long-term-memory-toggle')?.checked) {
                config.memory.npc_long_term_memory[name] = true;
            }
        }
    });
    config.prompts.default = document.getElementById('defaultPrompt').value;
    config.prompts.world_lore = document.getElementById('worldLore').value;
    config.prompts.scene_continuation = document.getElementById('sceneContinuationPrompt').value;
    config.prompts.interjection_prompt_mode = document.getElementById('interjectionPromptMode').value;
    config.prompts.owl_board_rules = document.getElementById('owlBoardRulesPrompt').value;
    config.prompts.owl_mail_classifier = document.getElementById('owlMailClassifierPrompt').value;
    config.prompts.owl_mail_letter = document.getElementById('owlMailLetterPrompt').value;
    config.prompts.owl_board_thread = document.getElementById('owlBoardThreadPrompt').value;
    config.prompts.owl_board_reply = document.getElementById('owlBoardReplyPrompt').value;

    // Sync pronunciation replacements from textarea (in case onchange hasn't fired)
    const pronEl = document.getElementById('pronunciationReplacements');
    if (pronEl) {
        parsePronunciationReplacements(pronEl.value);
    }
    stripLegacyConfigFields(config);

    const savingOmniVoiceCpp = config.tts?.provider === 'omnivoice_cpp';
    if (savingOmniVoiceCpp) {
        // The save endpoint starts the dependency installer. Reflect that
        // immediately instead of leaving a live install button visible until
        // the next background status poll happens to observe the new job.
        showOmniVoiceCppInstallStarting();
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
            if (savingOmniVoiceCpp) {
                fetchOmniVoiceCppStatus();
            }
            await checkSetupStatus();
        } else {
            throw new Error('Save failed');
        }
    } catch (e) {
        if (savingOmniVoiceCpp) {
            cancelOmniVoiceCppInstallStarting();
        }
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
    'O.W.L.': 'Owl',
    'Stupefy': '/ˈstuː.pɪ.faɪ/',
    'Legilimens': 'Lehjillihmenz|/lɛˈdʒɪl.ɪ.mɛnz/',
    'Crucio': 'Kroosheeoh|/ˈkruː.ʃi.oʊ/',
    'Levioso': 'Leveeohso|/ˌlɛv.iˈoʊ.soʊ/',
    'Alohomora': '/ˌæl.oʊ.hoʊˈmɔːr.ə/',
    'Petrificus Totalus': '/pɛˈtrɪf.ɪ.kəs toʊˈtæl.əs/',
    'Ominis': 'ominous|/ˈɑː.mə.nəs/',
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
        resizeTextareaAfterProgrammaticUpdate(el);
        parsePronunciationReplacements(el.value);
        markDirty();
    }
}

function resizeTextareaAfterProgrammaticUpdate(textarea) {
    if (!textarea) return;
    requestAnimationFrame(() => {
        if (typeof AutoExpandTextarea !== 'undefined' && AutoExpandTextarea.resizeTextarea) {
            AutoExpandTextarea.resizeTextarea(textarea);
        } else {
            textarea.style.height = 'auto';
            textarea.style.height = textarea.scrollHeight + 'px';
        }
    });
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

async function resetOwlPrompt(settingsKey, textareaId) {
    try {
        const response = await fetch(`/api/config/defaults/owl-prompt/${settingsKey}`);
        const data = await response.json();
        const textarea = document.getElementById(textareaId);
        textarea.value = data.prompt;
        updateSetting(`prompts.${settingsKey}`, data.prompt);
        showToast('Prompt reset to default', 'success');
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
                historyCurrentPage = 1;
                historySelectedNpcId = 'all';
                npcChapters = null;
                historyChaptersNpcId = null;
                loadDialogueHistory({ allowDuringEdit: true });
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
                historyCurrentPage = 1;
                historySelectedNpcId = 'all';
                npcChapters = null;
                historyChaptersNpcId = null;
                loadDialogueHistory({ allowDuringEdit: true });
                showToast('Dialogue history cleared', 'success');
            })
            .catch(() => showToast('Clear failed', 'error'));
    }
}

async function clearAllMemories() {
    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return;
    }

    const message = 'Are you sure you want to clear all NPC long-term memories?\n\n' +
        'This will delete all memory facts and chapter data. ' +
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
                await loadVectorMigrationStatus();
                await loadGraphBackups();
            } else {
                showToast(data.error || 'Clear failed', 'error');
            }
        } catch (e) {
            showToast('Clear failed', 'error');
        }
    }
}

async function resetMemorySystem() {
    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return;
    }

    const message = 'RESET MEMORY SYSTEM\n\n' +
        'This will force-delete all memory database files without creating a backup.\n\n' +
        'Use this only when the database is corrupted and Clear/Restore are not working.\n\n' +
        'Type "RESET" to confirm.';
    const answer = prompt(message);
    if (answer !== 'RESET') return;

    try {
        const res = await fetch('/api/memories/reset', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            const deleted = (data.deleted || []).join(', ') || 'nothing to delete';
            const errCount = (data.errors || []).length;
            const msg = errCount > 0
                ? `Memory system reset (${errCount} file(s) could not be deleted)`
                : 'Memory system fully reset';
            showToast(msg, errCount > 0 ? 'warning' : 'success');
            await loadMigrationStatus();
            await loadVectorMigrationStatus();
            await loadGraphBackups();
        } else {
            showToast(data.error || 'Reset failed', 'error');
        }
    } catch (e) {
        showToast('Reset failed: ' + e.message, 'error');
    }
}

function formatGraphBackupLabel(backup) {
    const session = (backup.session || '').replace(/^session_/, 'Session ');
    const time = backup.time || '';
    const reason = backup.reason_label || backup.reason || 'backup';
    const created = backup.created_at ? new Date(backup.created_at * 1000).toLocaleString() : '';
    const kind = backup.kind_label || 'Backup';
    return {
        title: `${reason}`,
        meta: [kind, backup.date, session, time].filter(Boolean).join(' • '),
        created
    };
}

function renderGraphBackups(backups) {
    const listEl = document.getElementById('graphBackupsList');
    const statusEl = document.getElementById('graphBackupsStatus');
    if (!listEl || !statusEl) return;

    listEl.innerHTML = '';

    if (!Array.isArray(backups) || backups.length === 0) {
        statusEl.textContent = 'No Cognis memory snapshots found.';
        statusEl.style.color = 'var(--text-secondary)';
        listEl.innerHTML = `
            <div style="padding: 12px; border: 1px dashed var(--leather-border); border-radius: 4px; color: var(--text-secondary); background: var(--parchment-mid);">
                No restorable Cognis memory snapshots available yet.
            </div>
        `;
        return;
    }

    statusEl.textContent = `${backups.length} Cognis memory snapshot${backups.length !== 1 ? 's' : ''} available`;
    statusEl.style.color = 'var(--text-secondary)';

    for (const backup of backups) {
        const { title, meta, created } = formatGraphBackupLabel(backup);
        const card = document.createElement('div');
        card.style.cssText = 'display:flex; justify-content:space-between; align-items:flex-start; gap:12px; padding:12px; border:1px solid var(--leather-border); border-radius:4px; background:var(--parchment-mid);';
        card.innerHTML = `
            <div style="min-width:0;">
                <div style="font-weight:600; color:var(--ink-brown); margin-bottom:4px;">${escapeHtml(title)}</div>
                <div style="font-size:0.85em; color:var(--text-secondary); margin-bottom:2px;">${escapeHtml(meta)}</div>
                <div style="font-size:0.8em; color:var(--text-secondary);">${escapeHtml(created)}</div>
            </div>
            <div style="display:flex; align-items:center; gap:8px; flex-shrink:0;">
                <button class="btn btn-secondary" data-backup-id="${escapeHtml(backup.backup_id)}">Restore</button>
            </div>
        `;
        const btn = card.querySelector('button');
        btn.addEventListener('click', () => restoreGraphBackup(backup.backup_id, title));
        listEl.appendChild(card);
    }
}

async function loadGraphBackups() {
    const listEl = document.getElementById('graphBackupsList');
    const statusEl = document.getElementById('graphBackupsStatus');
    const refreshBtn = document.getElementById('refreshGraphBackupsBtn');
    if (!listEl || !statusEl) return;

    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return;
    }

    if (refreshBtn) refreshBtn.disabled = true;
    statusEl.textContent = 'Loading Cognis memory snapshots...';
    statusEl.style.color = 'var(--text-secondary)';

    try {
        const response = await fetch('/api/memories/backups');
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || 'Failed to load Cognis memory snapshots');
        }
        renderGraphBackups(data.backups || []);
    } catch (e) {
        console.error('Error loading memory snapshots:', e);
        statusEl.textContent = e.message || 'Failed to load Cognis memory snapshots';
        statusEl.style.color = 'var(--danger)';
        listEl.innerHTML = '';
    } finally {
        if (refreshBtn) refreshBtn.disabled = !isPlayerContextReady();
    }
}

async function restoreGraphBackup(backupId, backupTitle) {
    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return;
    }

    const label = backupTitle || 'this memory snapshot';
    const message = `Restore ${label}?\n\n` +
        'Cognis memory snapshots replace memory facts, chapter files, staged chapter content, bios, and memory queue state together.\n\n' +
        'Continue?';

    if (!confirm(message)) return;

    const refreshBtn = document.getElementById('refreshGraphBackupsBtn');
    if (refreshBtn) refreshBtn.disabled = true;

    try {
        const response = await fetch('/api/memories/backups/restore', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ backup_id: backupId })
        });
        const data = await response.json();
        if (!response.ok || !data.success) {
            throw new Error(data.error || 'Restore failed');
        }

        showToast('Memory snapshot restored', 'success');
        await loadGraphBackups();
        await refreshMemoryUI();
        if (typeof loadNpcGraph === 'function') {
            await loadNpcGraph();
        }
    } catch (e) {
        console.error('Error restoring memory snapshot:', e);
        showToast(`Restore failed: ${e.message}`, 'error');
    } finally {
        if (refreshBtn) refreshBtn.disabled = !isPlayerContextReady();
    }
}

async function refreshMemoryUI() {
    /**
     * Refresh all memory-related UI after migration completes.
     * - Updates migration status count
     * - Refreshes memory inspector NPC list
     * - Reloads current NPC facts if one is displayed
     * - Refreshes character bios with newly generated data
     */

    // Refresh migration status count
    await loadMigrationStatus();
    await loadVectorMigrationStatus();
    await loadGraphBackups();

    // Refresh memory inspector (if loaded)
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
        const cards = document.querySelectorAll('#bioList .character-card:not(.player-card)');
        for (const card of cards) {
            const npcId = card.dataset.npcId;
            if (npcId && isCharacterMemoryEffectivelyEnabled(card, memoryEnabled)) {
                await loadGeneratedBio(npcId, card);
            }
        }
    }
}

async function loadMigrationStatus() {
    const countEl = document.getElementById('migratePendingCount');
    if (!countEl) return;

    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return;
    }

    try {
        const response = await fetch('/api/memories/migration-status');
        const data = await response.json();

        if (data.success) {
            const { pending_count, migrated_count, total_npcs, min_entries_threshold } = data;
            if (pending_count > 0) {
                countEl.textContent = `${pending_count} NPC${pending_count !== 1 ? 's' : ''} pending migration (${migrated_count}/${total_npcs} already migrated, ${min_entries_threshold}+ entries required)`;
                countEl.style.color = 'var(--gold-dark)';
                setMemoryDataButton('migrateBtn', true, 'Migrate eligible dialogue history for the loaded player');
            } else if (total_npcs > 0) {
                countEl.textContent = `All ${total_npcs} NPCs already migrated`;
                countEl.style.color = 'var(--success)';
                setMemoryDataButton('migrateBtn', false, 'All eligible NPCs are already migrated');
            } else {
                countEl.textContent = `No NPCs with sufficient dialogue history found (minimum ${min_entries_threshold} entries required)`;
                countEl.style.color = 'var(--text-secondary)';
                setMemoryDataButton('migrateBtn', false, 'No eligible dialogue history to migrate');
            }
        } else {
            countEl.textContent = data.error || 'Unable to check migration status';
            countEl.style.color = 'var(--text-secondary)';
            setMemoryDataButton('migrateBtn', false, 'Migration status is unavailable');
        }
    } catch (e) {
        console.error('Error loading migration status:', e);
        countEl.textContent = 'Unable to check migration status';
        countEl.style.color = 'var(--text-secondary)';
        setMemoryDataButton('migrateBtn', false, 'Migration status is unavailable');
    }
}

async function loadVectorMigrationStatus() {
    const countEl = document.getElementById('vectorMigratePendingCount');
    const btn = document.getElementById('vectorMigrateBtn');
    if (!countEl) return;

    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return;
    }

    try {
        const response = await fetch('/api/memories/vector-migration-status');
        const data = await response.json();

        if (data.success) {
            const count = data.mismatched_count || 0;
            const model = data.current_model || 'current model';
            if (count > 0) {
                countEl.textContent = `${count} memor${count === 1 ? 'y has' : 'ies have'} vectors from another embedding model. Rebuild to use ${model}.`;
                countEl.style.color = 'var(--gold-dark)';
                if (btn) {
                    btn.style.display = 'inline-block';
                    btn.disabled = false;
                    btn.title = 'Rebuild memory vectors for the current embedding model';
                }
            } else {
                countEl.textContent = `All memory vectors match ${model}`;
                countEl.style.color = 'var(--success)';
                if (btn) {
                    btn.style.display = 'inline-block';
                    btn.disabled = true;
                    btn.title = 'No memory vectors need rebuilding';
                }
            }
        } else {
            countEl.textContent = data.error || 'Unable to check memory vector status';
            countEl.style.color = 'var(--text-secondary)';
            if (btn) {
                btn.style.display = 'inline-block';
                btn.disabled = true;
                btn.title = 'Memory vector status is unavailable';
            }
        }
    } catch (e) {
        console.error('Error loading vector migration status:', e);
        countEl.textContent = 'Unable to check memory vector status';
        countEl.style.color = 'var(--text-secondary)';
        if (btn) {
            btn.style.display = 'inline-block';
            btn.disabled = true;
            btn.title = 'Memory vector status is unavailable';
        }
    }
}

async function migrateMemoryVectors() {
    const btn = document.getElementById('vectorMigrateBtn');
    const status = document.getElementById('vectorMigrateStatus');
    const countEl = document.getElementById('vectorMigratePendingCount');

    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return;
    }

    const message = 'Rebuild memory vectors for the current embedding model?\n\n' +
        'This will regenerate embeddings from stored memory text and update the vector search index.\n\n' +
        'It may use embedding API quota and can be resumed safely if interrupted.\n\n' +
        'Continue?';

    if (!confirm(message)) return;

    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Rebuilding...';
    }
    if (status) {
        status.style.display = 'block';
        status.textContent = 'Starting vector rebuild...';
        status.style.color = 'var(--text-secondary)';
    }

    const eventSource = new EventSource('/api/memories/vector-migrate/stream');

    eventSource.onmessage = async (event) => {
        try {
            const data = JSON.parse(event.data);

            switch (data.type) {
                case 'start':
                    if (data.total > 0) {
                        status.textContent = `Found ${data.total} memor${data.total === 1 ? 'y' : 'ies'} to rebuild for ${data.current_model || 'the current model'}...`;
                    } else {
                        status.textContent = 'No memory vectors need rebuilding.';
                    }
                    break;

                case 'progress': {
                    const total = data.total || 0;
                    const current = data.current || 0;
                    const pct = total > 0 ? Math.round((current / total) * 100) : 0;
                    status.textContent = `[${current}/${total}] ${data.message || 'Rebuilding vectors...'} (${pct}%)`;
                    break;
                }

                case 'complete':
                    eventSource.close();
                    status.textContent = `Complete: rebuilt ${data.rebuilt || 0} memory vector${(data.rebuilt || 0) === 1 ? '' : 's'}.`;
                    status.style.color = 'var(--success)';
                    showToast(`Rebuilt ${data.rebuilt || 0} memory vector${(data.rebuilt || 0) === 1 ? '' : 's'}`, 'success');
                    if (btn) {
                        btn.disabled = false;
                        btn.textContent = 'Rebuild Vectors';
                    }
                    await loadVectorMigrationStatus();
                    if (typeof refreshMemoryInspector === 'function') {
                        await refreshMemoryInspector();
                    }
                    break;

                case 'error':
                    eventSource.close();
                    status.textContent = `Error: ${data.message || 'Vector rebuild failed'}`;
                    status.style.color = 'var(--danger)';
                    showToast(data.message || 'Vector rebuild failed', 'error');
                    if (btn) {
                        btn.disabled = false;
                        btn.textContent = 'Rebuild Vectors';
                    }
                    await loadVectorMigrationStatus();
                    break;
            }
        } catch (e) {
            console.error('Error parsing vector migration SSE event:', e);
        }
    };

    eventSource.onerror = () => {
        eventSource.close();
        if (status) {
            status.textContent = 'Vector rebuild connection lost';
            status.style.color = 'var(--danger)';
        }
        showToast('Vector rebuild failed - connection lost', 'error');
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Rebuild Vectors';
        }
        if (countEl) loadVectorMigrationStatus();
    };
}

async function migrateMemories() {
    const btn = document.getElementById('migrateBtn');
    const status = document.getElementById('migrateStatus');

    if (!isPlayerContextReady()) {
        setMemoryDataManagementNoPlayerState();
        return;
    }

    const message = 'This will process all existing dialogue history into chapters and extract searchable memory facts.\n\n' +
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

// Memory Inspector Functions - see /js/graph.js

// ============================================
// History Edit Mode Functions
// ============================================
function toggleHistoryEditMode() {
    stopHistoryAudio();
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
    // Re-render tables to show/hide checkboxes using current server view state
    populateHistoryTable(historyRecentEntries, true);
    renderAllHistory();
    updateHistorySelectionUI();
}

function parseHistorySourceIds(sourceIdsStr) {
    if (!sourceIdsStr) return [];
    return sourceIdsStr
        .split(',')
        .map(id => Number(id))
        .filter(id => Number.isInteger(id) && id > 0);
}

function toggleHistoryEntrySelection(entryKey, sourceIdsStr, checkbox) {
    const sourceIds = parseHistorySourceIds(sourceIdsStr);
    if (checkbox.checked) {
        selectedHistoryEntries.set(entryKey, sourceIds);
    } else {
        selectedHistoryEntries.delete(entryKey);
    }
    updateHistorySelectionUI();
    updateRowSelectionState(entryKey, checkbox.checked);
}

function updateRowSelectionState(entryKey, isSelected) {
    // Update row visual state
    document.querySelectorAll(`tr[data-entry-key="${entryKey}"]`).forEach(row => {
        if (isSelected) {
            row.classList.add('selected');
        } else {
            row.classList.remove('selected');
        }
    });
}

function getHistorySelectionTbody(triggerCheckbox) {
    if (triggerCheckbox) {
        return triggerCheckbox.closest('table')?.querySelector('tbody') || null;
    }

    const activeHistoryTab = document.querySelector('#chapterHistory .tab-content.active');
    return activeHistoryTab?.querySelector('tbody') || null;
}

function toggleAllVisibleHistoryEntries(triggerCheckbox) {
    if (triggerCheckbox.checked) {
        selectAllHistoryEntries(triggerCheckbox);
    } else {
        deselectAllHistoryEntries(triggerCheckbox);
    }
}

function selectAllHistoryEntries(triggerCheckbox = null) {
    // Select all visible entries in the table that owns the select-all checkbox.
    const tbody = getHistorySelectionTbody(triggerCheckbox);
    if (!tbody) return;
    const checkboxes = tbody.querySelectorAll('.history-checkbox:not(.history-select-all)');

    checkboxes.forEach(cb => {
        const entryKey = cb.dataset.entryKey;
        if (entryKey) {
            selectedHistoryEntries.set(entryKey, parseHistorySourceIds(cb.dataset.sourceIds));
            cb.checked = true;
            cb.closest('tr').classList.add('selected');
        }
    });
    updateHistorySelectionUI();
}

function deselectAllHistoryEntries(triggerCheckbox = null) {
    const tbody = getHistorySelectionTbody(triggerCheckbox);
    const checkboxes = tbody
        ? tbody.querySelectorAll('.history-checkbox:not(.history-select-all)')
        : document.querySelectorAll('.history-checkbox:not(.history-select-all)');

    if (!triggerCheckbox) {
        selectedHistoryEntries.clear();
    }

    checkboxes.forEach(cb => {
        const entryKey = cb.dataset.entryKey;
        if (triggerCheckbox && entryKey) {
            selectedHistoryEntries.delete(entryKey);
        }
        cb.checked = false;
        cb.closest('tr').classList.remove('selected');
    });
    updateHistorySelectionUI();
}

function updateHistorySelectionUI() {
    const count = selectedHistoryEntries.size;
    document.getElementById('historySelectedCount').textContent = count;
    document.getElementById('deleteSelectedBtn').disabled = count === 0;

    document.querySelectorAll('.history-checkbox:not(.history-select-all)').forEach(cb => {
        const isSelected = selectedHistoryEntries.has(cb.dataset.entryKey);
        cb.checked = isSelected;
        cb.closest('tr')?.classList.toggle('selected', isSelected);
    });

    // Update select-all checkboxes
    document.querySelectorAll('.history-select-all').forEach(cb => {
        const tbody = cb.closest('table').querySelector('tbody');
        const rowCheckboxes = tbody.querySelectorAll('.history-checkbox:not(.history-select-all)');
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
        const entryIds = getSelectedHistoryEntryIds();
        const response = await fetch('/api/dialogue-history/entries', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entry_ids: entryIds })
        });

        if (response.ok) {
            const result = await response.json();
            selectedHistoryEntries.clear();
            historyChaptersNpcId = null;
            await loadDialogueHistory({ allowDuringEdit: true });
            updateHistorySelectionUI();

            showToast(`Deleted ${result.deleted} ${result.deleted === 1 ? 'entry' : 'entries'}`, 'success');
        } else {
            showToast('Delete failed', 'error');
        }
    } catch (e) {
        showToast('Delete failed: ' + e.message, 'error');
    }
}

async function deleteSingleHistoryEntry(entryKey, sourceIdsStr) {
    if (!confirm('Delete this entry? This cannot be undone.')) return;

    try {
        const entryIds = parseHistorySourceIds(sourceIdsStr);
        const response = await fetch('/api/dialogue-history/entries', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entry_ids: entryIds })
        });

        if (response.ok) {
            selectedHistoryEntries.delete(entryKey);
            historyChaptersNpcId = null;
            await loadDialogueHistory({ allowDuringEdit: true });
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
                        historyChaptersNpcId = null;
                        loadDialogueHistory({ allowDuringEdit: true });
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
                        showToast(`Imported ${result.static_bios || 0} bios, ${result.editor_guidance || 0} character guidance, ${result.viseme_scales || 0} viseme scales`, 'success');
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
        const response = await fetchWithTimeout('/api/commitments', {}, 5000);
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

async function saveInlineEdit(commitmentId, rawLocationId, picker) {
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

    // Parse "LocationKey::label" format from dropdown
    let locationId = rawLocationId;
    let spotLabel = null;
    if (rawLocationId.includes('::')) {
        const parts = rawLocationId.split('::');
        locationId = parts[0];
        spotLabel = parts[1];
    }

    const body = { location_id: locationId, game_time_start: gameTimeStart };
    if (spotLabel) body.spot_label = spotLabel;

    try {
        const resp = await fetch(`/api/commitments/${commitmentId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
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

    select.innerHTML = '<option value="">Select NPC...</option>';
    for (const optionData of historyNpcOptions) {
        const opt = document.createElement('option');
        opt.value = optionData.id;
        opt.textContent = optionData.name || prettifyVoiceName(optionData.id);
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
    const rawLocationId = document.getElementById('commitmentCreateLocation')?.value;
    const dateVal = commitmentDatePicker?.selectedDates?.[0];
    const timeVal = commitmentTimePicker?.selectedDates?.[0];

    if (!npcId) { showToast('Please select an NPC', 'error'); return; }
    if (!rawLocationId) { showToast('Please select a location', 'error'); return; }
    if (!dateVal) { showToast('Please select a date', 'error'); return; }
    if (!timeVal) { showToast('Please select a time', 'error'); return; }

    // Parse "location_id::label" format for labeled spots
    let locationId = rawLocationId;
    let spotLabel = null;
    if (rawLocationId.includes('::')) {
        const parts = rawLocationId.split('::');
        locationId = parts[0];
        spotLabel = parts[1];
    }

    const y = dateVal.getFullYear();
    const mo = String(dateVal.getMonth() + 1).padStart(2, '0');
    const d = String(dateVal.getDate()).padStart(2, '0');
    const h = String(timeVal.getHours()).padStart(2, '0');
    const mi = String(timeVal.getMinutes()).padStart(2, '0');
    const gameTimeStart = `${y}/${mo}/${d} ${h}:${mi}`;

    const body = { npc_id: npcId, location_id: locationId, game_time_start: gameTimeStart };
    if (spotLabel) body.spot_label = spotLabel;

    try {
        const resp = await fetch('/api/commitments', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
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
                <span>${type === 'error' ? '&#10007;' : '&#10003;'}</span>
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
            return `${data.model || 'LLM'} (${data.context || 'chat'})${data.provider_used ? ` via ${data.provider_used}` : ''}`;
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

function formatEventCost(costValue) {
    const numericCost = Number(costValue);
    if (!Number.isFinite(numericCost)) return null;
    return `$${numericCost.toFixed(4)}`;
}

function formatEventMetric(eventObj) {
    const type = eventObj.type;
    const data = eventObj.data || {};
    const tokens = data.tokens || {};
    const latency = data.duration_ms ? `${Math.round(data.duration_ms)}ms` : null;

    switch (type) {
        case 'llm':
            const cost = data.cost || {};
            const costStr = formatEventCost(cost.total);
            const tokenStr = tokens.total ? `${tokens.total}T${tokens.reasoning ? ` (${tokens.reasoning}R)` : ''}` : null;
            if (costStr && tokenStr && latency) return `${costStr} / ${tokenStr} / ${latency}`;
            if (costStr && tokenStr) return `${costStr} / ${tokenStr}`;
            if (costStr && latency) return `${costStr} / ${latency}`;
            if (costStr) return costStr;
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
    const isWarning = eventObj.status === 'warning';
    const isSuccess = eventObj.status === 'success' || !eventObj.status;
    const timeStr = formatRelativeTime(eventObj.timestamp);
    const infoStr = formatEventInfo(eventObj);
    const detailStr = formatEventDetail(eventObj);
    const metricStr = formatEventMetric(eventObj);

    const row = document.createElement('div');
    let rowClass = 'event-row-new';
    if (isError) rowClass += ' event-row-error';
    else if (isWarning) rowClass += ' event-row-warning';
    else if (isSuccess) rowClass += ' event-row-success';
    row.className = rowClass;
    row.id = `event-${eventObj.id}`;
    row.dataset.timestamp = eventObj.timestamp;

    let errorLine = '';
    if (isError) {
        errorLine = `<div class="event-error-message">${escapeHtml(eventObj.error)}</div>`;
    }
    const warning = eventObj.data?.warning || (isWarning ? eventObj.error : '');
    let warningLine = '';
    if (isWarning && warning) {
        warningLine = `<div class="event-warning-message">${escapeHtml(warning)}</div>`;
    }

    row.innerHTML = `
                <div class="event-time">${timeStr}</div>
                <div class="event-type ${typeClass}">${eventObj.type}</div>
                <div class="event-status ${statusClass}"></div>
                <div class="event-info">${escapeHtml(infoStr)}</div>
                <div class="event-metric">${escapeHtml(metricStr)}</div>
                ${warningLine}
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

function getSystemEventCostTimeframe() {
    return document.getElementById('eventCostTimeframe')?.value || 'today';
}

function updateSystemEventCostSummary(payload) {
    const summaryEl = document.getElementById('eventCostSummary');
    if (!summaryEl) return;

    const timeframeLabel = payload?.timeframe_label || 'Today';
    const totalCost = formatEventCost(payload?.total_cost || 0) || '$0.0000';
    const callCount = Number(payload?.call_count || 0);
    const featureCount = Number(payload?.feature_count || 0);
    const callLabel = callCount === 1 ? 'billable call' : 'billable calls';
    const featureLabel = featureCount === 1 ? 'feature' : 'features';
    summaryEl.textContent = `${timeframeLabel}: ${totalCost} across ${callCount} ${callLabel} in ${featureCount} ${featureLabel}`;
}

function renderSystemEventCosts(payload) {
    const tbody = document.getElementById('eventCostTableBody');
    if (!tbody) return;

    updateSystemEventCostSummary(payload || {});

    const features = Array.isArray(payload?.features) ? payload.features : [];
    if (features.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="event-cost-empty">No billable LLM cost for this timeframe yet</td></tr>';
        return;
    }

    const rows = [];
    for (const feature of features) {
        const featureCost = formatEventCost(feature.total_cost) || '$0.0000';
        rows.push(`
            <tr class="event-cost-feature-row">
                <td>${escapeHtml(feature.feature_label || 'Other')}</td>
                <td>Total</td>
                <td>${Number(feature.call_count || 0)}</td>
                <td>${escapeHtml(featureCost)}</td>
            </tr>
        `);

        const modules = Array.isArray(feature.modules) ? feature.modules : [];
        for (const module of modules) {
            const moduleCost = formatEventCost(module.total_cost) || '$0.0000';
            const contextText = module.context ? ` <span class="event-cost-module-context">(${escapeHtml(module.context)})</span>` : '';
            rows.push(`
                <tr class="event-cost-module-row">
                    <td>&nbsp;</td>
                    <td class="event-cost-module-cell">
                        <span class="event-cost-module-name">${escapeHtml(module.label || module.context || 'Unknown')}${contextText}</span>
                    </td>
                    <td>${Number(module.call_count || 0)}</td>
                    <td>${escapeHtml(moduleCost)}</td>
                </tr>
            `);
        }
    }

    tbody.innerHTML = rows.join('');
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

function loadSystemEventCosts() {
    if (eventCostsLoadInFlight) return;
    eventCostsLoadInFlight = true;

    const timeframe = encodeURIComponent(getSystemEventCostTimeframe());
    fetchWithTimeout(`/api/system-events/costs?timeframe=${timeframe}`)
        .then(response => response.json())
        .then(payload => {
            renderSystemEventCosts(payload);
        })
        .catch(() => {
            const summaryEl = document.getElementById('eventCostSummary');
            if (summaryEl) {
                summaryEl.textContent = 'Cost totals unavailable right now';
            }
        })
        .finally(() => {
            eventCostsLoadInFlight = false;
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
            renderSystemEventCosts({
                timeframe: getSystemEventCostTimeframe(),
                timeframe_label: document.getElementById('eventCostTimeframe')?.selectedOptions?.[0]?.textContent || 'Today',
                total_cost: 0,
                call_count: 0,
                feature_count: 0,
                features: [],
            });
            showToast('Events cleared', 'success');
        })
        .catch(err => {
            console.error('[Events] Failed to clear:', err);
            showToast('Failed to clear events', 'error');
        });
}

// ============================================
// Owl Post Activity Log
// ============================================
let _owlLogIds = new Set();

function _formatOwlBadge(event) {
    const labels = {
        mail_sent: 'sent',
        mail_reply: 'reply',
        mail_skip: 'skip',
        eval_no: 'eval',
        board_thread: 'thread',
        board_reply: 'reply',
        board_skip: 'skip',
        error: 'error',
    };
    return labels[event] || event;
}

function renderOwlLogEntry(entry) {
    const row = document.createElement('div');
    row.id = `owl-log-${entry.id}`;

    const timeStr = formatRelativeTime(entry.timestamp);
    const badge = _formatOwlBadge(entry.event);
    const npcSpan = entry.npc_name
        ? `<span class="owl-log-npc">${escapeHtml(entry.npc_name)}</span> — `
        : '';
    const detail = entry.detail || '';

    row.innerHTML = `
        <div class="owl-log-time">${timeStr}</div>
        <div class="owl-log-badge ${entry.event}">${badge}</div>
        <div class="owl-log-detail">${npcSpan}${escapeHtml(detail)}</div>
    `;
    row.dataset.timestamp = entry.timestamp;
    return row;
}

function updateOwlPostLog(entries) {
    const list = document.getElementById('owlPostLog');
    if (!list) return;

    const newIds = new Set(entries.map(e => e.id));
    // Remove stale
    for (const id of _owlLogIds) {
        if (!newIds.has(id)) {
            const el = document.getElementById(`owl-log-${id}`);
            if (el) el.remove();
            _owlLogIds.delete(id);
        }
    }
    // Add new at top
    const added = entries.filter(e => !_owlLogIds.has(e.id));
    for (const entry of added.reverse()) {
        list.insertBefore(renderOwlLogEntry(entry), list.firstChild);
        _owlLogIds.add(entry.id);
    }
    // Empty state
    const empty = list.querySelector('.owl-log-empty');
    if (_owlLogIds.size > 0 && empty) empty.remove();
    else if (_owlLogIds.size === 0 && !empty) {
        list.innerHTML = '<div class="owl-log-empty">No activity yet</div>';
    }
}

let _owlLogInFlight = false;

function loadOwlPostLog() {
    if (_owlLogInFlight) return;
    if (!isPlayerContextReady()) {
        setOwlPostNoPlayerState();
        return;
    }
    _owlLogInFlight = true;
    fetchWithTimeout('/owlpost/api/log?limit=200')
        .then(r => r.json())
        .then(entries => {
            if (Array.isArray(entries)) updateOwlPostLog(entries);
        })
        .catch(() => { })
        .finally(() => { _owlLogInFlight = false; });
}

async function clearOwlPostLog() {
    if (!isPlayerContextReady()) {
        setOwlPostNoPlayerState();
        return;
    }
    if (!confirm('Clear owl post activity log?')) return;
    try {
        await fetch('/owlpost/api/log', { method: 'DELETE' });
        _owlLogIds.clear();
        const list = document.getElementById('owlPostLog');
        if (list) list.innerHTML = '<div class="owl-log-empty">No activity yet</div>';
        updateOwlPostPlayerState();
        showToast('Log cleared', 'success');
    } catch (e) {
        showToast('Failed to clear log', 'error');
    }
}

async function resetOwlBoardsWithConfirm() {
    if (!isPlayerContextReady()) {
        setOwlPostNoPlayerState();
        return;
    }
    const message = 'Are you sure you want to reset all notice boards?\n\n' +
        'This will delete every board post and thread. ' +
        'Board definitions and unlocks are preserved.\n\n' +
        'This action cannot be undone.';
    if (!confirm(message)) return;
    try {
        await fetch('/owlpost/api/boards/reset', { method: 'DELETE' });
        updateOwlPostPlayerState();
        showToast('All board posts cleared', 'success');
    } catch (e) {
        showToast('Failed to reset boards', 'error');
    }
}

async function resetOwlMailWithConfirm() {
    if (!isPlayerContextReady()) {
        setOwlPostNoPlayerState();
        return;
    }
    const message = 'Are you sure you want to reset all owl mail?\n\n' +
        'This will delete every letter, thread, meeting proposal, cached read-aloud audio file, ' +
        'and mail generation state. Notice boards, settings, and custom characters are preserved.\n\n' +
        'This action cannot be undone.';
    if (!confirm(message)) return;
    try {
        const response = await fetch('/owlpost/api/mail/reset', { method: 'DELETE' });
        if (!response.ok) throw new Error('Request failed');
        updateOwlPostPlayerState();
        showToast('All owl mail cleared', 'success');
    } catch (e) {
        showToast('Failed to reset owl mail', 'error');
    }
}

function updateOwlLogTimes() {
    const list = document.getElementById('owlPostLog');
    if (!list) return;
    list.querySelectorAll('[data-timestamp]').forEach(row => {
        const ts = parseFloat(row.dataset.timestamp);
        if (!isNaN(ts)) {
            const el = row.querySelector('.owl-log-time');
            if (el) el.textContent = formatRelativeTime(ts);
        }
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
            if (data.installed && data.outdated) {
                badge.innerHTML = '<span class="badge badge-warning">Update Available</span>';
                if (installBtn) {
                    installBtn.style.display = '';
                    installBtn.textContent = 'Update Plugin';
                }
            } else if (data.installed) {
                badge.innerHTML = '<span class="badge badge-success">Installed</span>';
                if (installBtn) installBtn.style.display = 'none';
            } else {
                badge.innerHTML = '<span class="badge badge-muted">Not Installed</span>';
                if (installBtn) {
                    installBtn.style.display = '';
                    installBtn.textContent = 'Install Plugin';
                }
            }
        }

        // Update header VR badge color based on plugin install status
        const vrBadge = document.getElementById('vrBadge');
        if (vrBadge) {
            vrBadge.style.color = data.installed ? '' : '#c0392b';
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
        btn.textContent = 'Downloading...';
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
    loadOwlPostLog();
    setInterval(loadOwlPostLog, 5000);
    setInterval(updateOwlLogTimes, 30000);
    initScrollSpy();  // Initialize navigation scroll spy
    checkVrPluginStatus();
    setInterval(checkVrPluginStatus, 30000);  // Check every 30s
});
