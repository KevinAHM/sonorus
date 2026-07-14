/**
 * Voice Manifest Builder - Frontend Logic
 *
 * Manages state for language selection, character browsing, sample analysis,
 * selection, and manifest export.
 */

const VoiceBuilder = {
    // Languages not supported by Parakeet (no CJK or Arabic)
    PARAKEET_UNSUPPORTED_PREFIXES: ['JA', 'KO', 'ZH', 'AR'],

    // Application state
    state: {
        language: null,
        languages: [],
        characters: [],
        selectedCharacter: null,
        samples: {},
        selections: {},
        filter: '',
        activeTasks: {},
        currentAudio: null,
        waveforms: {}, // Store WaveSurfer instances by wemId
        currentTaskId: null, // Track active auto-build task for cancellation
        currentUnloadHandler: null, // Track beforeunload handler for cleanup
        analyzer: 'deepgram', // Current analyzer: 'deepgram' or 'parakeet'
        deepgramConfigured: false,
        parakeetSupported: true
    },

    // DOM element cache
    elements: {},

    /**
     * Initialize the application
     */
    async init() {
        this.cacheElements();
        this.bindEvents();
        await this.checkConfigStatus();
        await this.loadLanguages();
    },

    /**
     * Check if required API keys/models are configured
     */
    async checkConfigStatus() {
        try {
            const lang = this.state.language || 'EN_US';
            const response = await fetch(`/voice-manager/api/config-status?lang=${lang}`);
            const status = await response.json();

            this.state.deepgramConfigured = status.deepgramConfigured;
            this.state.parakeetSupported = status.parakeetSupported;

            this.updateAnalyzerWarning();
            this.updateAnalyzerDropdown();
        } catch (error) {
            console.error('Failed to check config status:', error);
        }
    },

    /**
     * Update warning banner based on selected analyzer
     */
    updateAnalyzerWarning() {
        const warningBanner = document.getElementById('configWarning');
        const warningText = document.getElementById('configWarningText');
        const warningLink = document.getElementById('configWarningLink');
        const analyzer = this.state.analyzer;

        if (analyzer === 'deepgram' && !this.state.deepgramConfigured) {
            warningText.textContent = 'Deepgram API key not configured - required for audio analysis. Configure it in the Settings page.';
            warningLink.style.display = '';
            warningBanner.style.display = 'flex';
            lucide.createIcons();
        } else if (analyzer === 'parakeet' && !this.state.parakeetSupported) {
            warningText.textContent = 'Parakeet does not support this language. Switch to Deepgram or choose a supported language.';
            warningLink.style.display = 'none';
            warningBanner.style.display = 'flex';
            lucide.createIcons();
        } else {
            warningBanner.style.display = 'none';
        }
    },

    /**
     * Update analyzer dropdown - disable Parakeet for unsupported languages
     */
    updateAnalyzerDropdown() {
        const select = document.getElementById('analyzerSelect');
        if (!select) return;

        const lang = this.state.language || 'EN_US';
        const langPrefix = lang.split('_')[0];
        const parakeetUnsupported = this.PARAKEET_UNSUPPORTED_PREFIXES.includes(langPrefix);

        // Update Parakeet option disabled state
        for (const option of select.options) {
            if (option.value === 'parakeet') {
                option.disabled = parakeetUnsupported;
            }
        }

        // If Parakeet is selected but unsupported, switch to Deepgram
        if (parakeetUnsupported && this.state.analyzer === 'parakeet') {
            this.state.analyzer = 'deepgram';
            select.value = 'deepgram';
            this.updateAnalyzerWarning();
        }
    },

    /**
     * Cache frequently used DOM elements
     */
    cacheElements() {
        this.elements = {
            analyzerSelect: document.getElementById('analyzerSelect'),
            languageSelect: document.getElementById('languageSelect'),
            characterSearch: document.getElementById('characterSearch'),
            characterList: document.getElementById('characterList'),
            selectedCharacterName: document.getElementById('selectedCharacterName'),
            characterStats: document.getElementById('characterStats'),
            sampleActions: document.getElementById('sampleActions'),
            selectionDuration: document.getElementById('selectionDuration'),
            selectionCount: document.getElementById('selectionCount'),
            progressModal: document.getElementById('progressModal'),
            progressTitle: document.getElementById('progressTitle'),
            progressFill: document.getElementById('progressFill'),
            progressText: document.getElementById('progressText'),
            exportBtn: document.getElementById('exportBtn'),
            exportStatus: document.getElementById('exportStatus'),
            audioPlayer: document.getElementById('audioPlayer'),
            progressSummary: document.getElementById('progressSummary'),
            completeCount: document.getElementById('completeCount'),
            totalCount: document.getElementById('totalCount'),
            readinessSection: document.getElementById('readinessSection'),
            selectedSection: document.getElementById('selectedSection'),
            availableSection: document.getElementById('availableSection'),
            emptyState: document.getElementById('emptyState'),
            selectedList: document.getElementById('selectedList'),
            availableList: document.getElementById('availableList')
        };
    },

    /**
     * Bind event handlers
     */
    bindEvents() {
        // Analyzer selection
        this.elements.analyzerSelect.addEventListener('change', (e) => {
            this.selectAnalyzer(e.target.value);
        });

        // Language selection
        this.elements.languageSelect.addEventListener('change', (e) => {
            this.selectLanguage(e.target.value);
        });

        // Character search
        this.elements.characterSearch.addEventListener('input', (e) => {
            this.state.filter = e.target.value.toLowerCase();
            this.renderCharacters();
        });

        // Batch actions
        document.getElementById('extractAllBtn').addEventListener('click', () => {
            this.extractAll();
        });
        document.getElementById('autoBuildBtn').addEventListener('click', () => {
            this.autoBuildAll();
        });
        document.getElementById('analyzeAllBtn').addEventListener('click', () => {
            this.analyzeAll();
        });
        document.getElementById('autoSelectAllBtn').addEventListener('click', () => {
            this.autoSelectAll();
        });

        // Sample actions
        document.getElementById('extractBtn').addEventListener('click', () => {
            if (this.state.selectedCharacter) {
                this.extractCharacter(this.state.selectedCharacter);
            }
        });
        document.getElementById('analyzeBtn').addEventListener('click', () => {
            if (this.state.selectedCharacter) {
                this.analyzeCharacter(this.state.selectedCharacter);
            }
        });
        document.getElementById('autoSelectBtn').addEventListener('click', () => {
            if (this.state.selectedCharacter) {
                this.autoSelectCharacter(this.state.selectedCharacter);
            }
        });
        document.getElementById('clearSelectionBtn').addEventListener('click', () => {
            if (this.state.selectedCharacter) {
                this.clearSelection(this.state.selectedCharacter);
            }
        });

        // Preview
        document.getElementById('playPreviewBtn').addEventListener('click', () => {
            if (this.state.selectedCharacter) {
                this.playPreview(this.state.selectedCharacter);
            }
        });

        document.getElementById('stopPreviewBtn').addEventListener('click', () => {
            this.stopPreview();
        });

        // Export
        this.elements.exportBtn.addEventListener('click', () => {
            this.exportManifest();
        });

        // Cancel task
        document.getElementById('cancelTaskBtn').addEventListener('click', () => {
            this.cancelTask();
        });

        // Audio player
        this.elements.audioPlayer.addEventListener('ended', () => {
            this.onAudioEnded();
        });
    },

    /**
     * Load available languages
     */
    async loadLanguages() {
        try {
            const response = await fetch('/voice-manager/api/languages');
            const languages = await response.json();
            this.state.languages = languages;

            // Populate dropdown
            this.elements.languageSelect.innerHTML = languages.map(lang =>
                `<option value="${lang.code}">${lang.name}${lang.hasManifest ? ' *' : ''}</option>`
            ).join('');

            // Select first language
            if (languages.length > 0) {
                this.selectLanguage(languages[0].code);
            }
        } catch (error) {
            console.error('Failed to load languages:', error);
            this.elements.languageSelect.innerHTML = '<option value="">Error loading languages</option>';
        }
    },

    /**
     * Select an analyzer
     */
    selectAnalyzer(value) {
        this.state.analyzer = value;
        this.updateAnalyzerWarning();

        // Warm up Parakeet worker when selected
        if (value === 'parakeet') {
            fetch('/voice-manager/api/analyzer/warmup', { method: 'POST' })
                .catch(err => console.error('Failed to warm up Parakeet:', err));
        }
    },

    /**
     * Select a language and load its characters
     */
    async selectLanguage(code) {
        this.state.language = code;
        this.state.selectedCharacter = null;
        this.state.samples = {};

        // Re-check config status for new language (Parakeet support may change)
        await this.checkConfigStatus();

        // Load session for this language
        await this.loadSession();

        // Load characters
        await this.loadCharacters();

        // Clear sample panel
        this.elements.selectedCharacterName.textContent = 'Select a character';
        this.elements.characterStats.innerHTML = '';
        this.elements.sampleActions.style.display = 'none';
        this.elements.readinessSection.style.display = 'none';
        this.elements.selectedSection.style.display = 'none';
        this.elements.availableSection.style.display = 'none';
        this.elements.emptyState.style.display = 'flex';

        // Update export button
        this.updateExportButton();
    },

    /**
     * Load session data for current language
     */
    async loadSession() {
        try {
            const response = await fetch(`/voice-manager/api/session/load?lang=${this.state.language}`);
            const session = await response.json();
            this.state.selections = session.selections || {};
        } catch (error) {
            console.error('Failed to load session:', error);
            this.state.selections = {};
        }
    },

    /**
     * Load characters for current language
     */
    async loadCharacters() {
        this.elements.characterList.innerHTML = '<div class="loading-state">Loading characters...</div>';

        try {
            const response = await fetch(`/voice-manager/api/characters?lang=${this.state.language}`);
            const characters = await response.json();
            this.state.characters = characters;
            this.renderCharacters();
            this.updateProgressSummary();
        } catch (error) {
            console.error('Failed to load characters:', error);
            this.elements.characterList.innerHTML = '<div class="loading-state">Error loading characters</div>';
        }
    },

    /**
     * Render character list with current filter
     */
    renderCharacters() {
        const filtered = this.state.characters.filter(char =>
            !this.state.filter ||
            char.displayName.toLowerCase().includes(this.state.filter) ||
            char.voiceName.toLowerCase().includes(this.state.filter)
        );

        if (filtered.length === 0) {
            this.elements.characterList.innerHTML = '<div class="loading-state">No characters match filter</div>';
            return;
        }

        this.elements.characterList.innerHTML = filtered.map(char => {
            const isSelected = this.state.selectedCharacter === char.voiceName;
            const selectionCount = (this.state.selections[char.voiceName] || []).length;

            return `
                <div class="character-card ${isSelected ? 'selected' : ''}"
                     data-voice="${char.voiceName}"
                     onclick="VoiceBuilder.selectCharacter('${char.voiceName}')">
                    <div class="status-indicator ${char.status}"></div>
                    <span class="name">${char.displayName}</span>
                    <span class="sample-count">${selectionCount > 0 ? selectionCount + ' sel' : char.sampleCount + ' smp'}</span>
                </div>
            `;
        }).join('');
    },

    /**
     * Select a character and load their samples
     */
    async selectCharacter(voiceName) {
        // Clean up existing waveforms before switching characters
        this.destroyAllWaveforms();

        this.state.selectedCharacter = voiceName;
        this.renderCharacters();

        const char = this.state.characters.find(c => c.voiceName === voiceName);
        if (!char) return;

        this.elements.selectedCharacterName.textContent = char.displayName;
        this.elements.sampleActions.style.display = 'flex';

        // Show stats
        this.elements.characterStats.innerHTML = `
            <div class="stat"><span class="metric-label">Samples:</span> ${char.sampleCount}</div>
            <div class="stat"><span class="metric-label">Analyzed:</span> ${char.analyzedCount}</div>
            <div class="stat"><span class="metric-label">Selected:</span> ${char.selectedCount}</div>
        `;

        // Load samples
        await this.loadSamples(voiceName);
    },

    /**
     * Load samples for a character
     */
    async loadSamples(voiceName) {
        this.elements.emptyState.style.display = 'flex';
        this.elements.emptyState.innerHTML = `
            <div class="empty-icon">⏳</div>
            <p class="empty-message">Loading samples...</p>
        `;

        try {
            const response = await fetch(`/voice-manager/api/samples?lang=${this.state.language}&voice=${voiceName}`);
            const data = await response.json();
            this.state.samples[voiceName] = data.samples || [];
            this.renderSamples(voiceName);
        } catch (error) {
            console.error('Failed to load samples:', error);
            this.elements.emptyState.innerHTML = `
                <i data-lucide="alert-triangle" class="empty-icon"></i>
                <p class="empty-message">Error loading samples</p>
            `;
            lucide.createIcons();
        }
    },

    /**
     * Render samples for a character (two-section layout)
     */
    renderSamples(voiceName) {
        const samples = this.state.samples[voiceName] || [];
        const selections = this.state.selections[voiceName] || [];

        // Show action buttons when character is selected
        this.elements.sampleActions.style.display = 'flex';

        // Show/hide sections based on whether samples exist
        if (samples.length === 0) {
            // No samples yet - show empty state, enable extract button
            this.elements.emptyState.style.display = 'flex';
            this.elements.emptyState.innerHTML = `
                <div class="empty-icon">📁</div>
                <p class="empty-message">No valid samples found. Extract samples to begin.</p>
            `;
            this.elements.readinessSection.style.display = 'none';
            this.elements.selectedSection.style.display = 'none';
            this.elements.availableSection.style.display = 'none';

            // Enable extract button, disable others
            const extractBtn = document.getElementById('extractBtn');
            const analyzeBtn = document.getElementById('analyzeBtn');
            const autoSelectBtn = document.getElementById('autoSelectBtn');

            if (extractBtn) {
                extractBtn.disabled = false;
                extractBtn.style.opacity = '1';
                extractBtn.title = 'Extract voice samples from game files';
            }
            if (analyzeBtn) {
                analyzeBtn.disabled = true;
                analyzeBtn.style.opacity = '0.5';
            }
            if (autoSelectBtn) {
                autoSelectBtn.disabled = true;
                autoSelectBtn.style.opacity = '0.5';
            }
            return;
        }

        // Samples exist - hide empty, show sections, disable extract
        this.elements.emptyState.style.display = 'none';
        this.elements.readinessSection.style.display = 'block';
        this.elements.selectedSection.style.display = 'block';
        this.elements.availableSection.style.display = 'block';

        // Render both sections
        this.renderSelectedSamples(voiceName, samples, selections);
        this.renderAvailableSamples(voiceName, samples, selections);

        // Update button states - extract disabled, others enabled
        const extractBtn = document.getElementById('extractBtn');
        const analyzeBtn = document.getElementById('analyzeBtn');
        const autoSelectBtn = document.getElementById('autoSelectBtn');

        if (extractBtn) {
            extractBtn.disabled = true;
            extractBtn.style.opacity = '0.5';
            extractBtn.title = 'Samples already extracted';
        }
        if (analyzeBtn) {
            analyzeBtn.disabled = false;
            analyzeBtn.style.opacity = '1';
        }
        if (autoSelectBtn) {
            autoSelectBtn.disabled = false;
            autoSelectBtn.style.opacity = '1';
        }
    },

    /**
     * Render selected samples (top section - compact cards)
     */
    renderSelectedSamples(voiceName, samples, selections) {
        const selectedSamples = samples.filter(s => selections.includes(s.wemId));
        const totalDuration = selectedSamples.reduce((sum, s) => sum + s.duration, 0);

        document.getElementById('selectionDuration').textContent = `${totalDuration.toFixed(1)}s`;
        document.getElementById('selectionCount').textContent = `${selectedSamples.length} sample${selectedSamples.length !== 1 ? 's' : ''}`;

        // Update readiness indicators
        this.updateReadinessIndicators(selectedSamples);

        const selectedList = document.getElementById('selectedList');
        if (selectedSamples.length === 0) {
            selectedList.innerHTML = '<div class="empty-state" style="padding: var(--space-md); font-style: italic; opacity: 0.6;">No samples selected yet. Use "Add +" buttons below to add samples.</div>';
            return;
        }

        selectedList.innerHTML = selectedSamples.map(sample => `
            <div class="selected-card" data-wem="${sample.wemId}">
                <button class="play-btn-small" onclick="VoiceBuilder.playSample('${voiceName}', '${sample.wemId}')" title="Play"></button>
                <div class="info">
                    <div class="transcript">${sample.transcript || 'Not analyzed'}</div>
                    <div class="duration">${sample.duration.toFixed(1)}s</div>
                </div>
                <button class="remove-btn" onclick="VoiceBuilder.removeFromSelection('${voiceName}', '${sample.wemId}')" title="Remove from selection">
                    &#x2715;
                </button>
            </div>
        `).join('');
    },

    /**
     * Update readiness indicators for the three duration targets
     */
    updateReadinessIndicators(selectedSamples) {
        // Target 1: 10s (prefer single sample 8-12s, fall back to combinations)
        // First try single sample
        const single10 = selectedSamples.find(s => s.duration >= 8 && s.duration <= 12);
        const status10 = document.querySelector('#readiness10s .readiness-status');
        const detail10 = document.querySelector('#readiness10s .readiness-detail');

        if (single10) {
            // Found single sample - ideal
            status10.textContent = '✓ Ready';
            status10.className = 'readiness-status ready';
            detail10.textContent = `${single10.duration.toFixed(1)}s sample`;
        } else {
            // Try combinations as fallback
            const combo10 = this.findSubsetInRange(selectedSamples, 8, 12);
            if (combo10) {
                status10.textContent = '✓ Ready (combo)';
                status10.className = 'readiness-status ready';
                detail10.textContent = `${combo10.total.toFixed(1)}s (${combo10.count} samples)`;
            } else {
                status10.textContent = '✗ Not met';
                status10.className = 'readiness-status not-ready';
                detail10.textContent = 'Need single sample 8-12s';
            }
        }

        // Target 2: 15s (find any combination totaling 13-17s)
        const combo15 = this.findSubsetInRange(selectedSamples, 13, 17);
        const status15 = document.querySelector('#readiness15s .readiness-status');
        const detail15 = document.querySelector('#readiness15s .readiness-detail');

        if (combo15) {
            status15.textContent = '✓ Ready';
            status15.className = 'readiness-status ready';
            detail15.textContent = `${combo15.total.toFixed(1)}s (${combo15.count} samples)`;
        } else {
            const totalDuration = selectedSamples.reduce((sum, s) => sum + s.duration, 0);
            if (totalDuration < 13) {
                status15.textContent = '✗ Too short';
                status15.className = 'readiness-status not-ready';
                detail15.textContent = `${totalDuration.toFixed(1)}s total / need 13-17s`;
            } else {
                status15.textContent = '✗ No valid combo';
                status15.className = 'readiness-status not-ready';
                detail15.textContent = 'Need combination 13-17s';
            }
        }

        // Target 3: 60s (optional - find any combination totaling 55-65s)
        const combo60 = this.findSubsetInRange(selectedSamples, 55, 65);
        const status60 = document.querySelector('#readiness60s .readiness-status');
        const detail60 = document.querySelector('#readiness60s .readiness-detail');

        if (combo60) {
            status60.textContent = '✓ Ready';
            status60.className = 'readiness-status ready';
            detail60.textContent = `${combo60.total.toFixed(1)}s (${combo60.count} samples)`;
        } else {
            const totalDuration = selectedSamples.reduce((sum, s) => sum + s.duration, 0);
            status60.textContent = 'Optional';
            status60.className = 'readiness-status optional';
            detail60.textContent = `${totalDuration.toFixed(1)}s total / 55-65s optional`;
        }
    },

    /**
     * Find a subset of samples whose total duration falls within the target range
     * Uses greedy algorithm: tries to get as close to middle of range as possible
     */
    findSubsetInRange(samples, minDuration, maxDuration) {
        if (samples.length === 0) return null;

        const target = (minDuration + maxDuration) / 2;

        // Sort samples by duration (descending) for greedy approach
        const sorted = [...samples].sort((a, b) => b.duration - a.duration);

        // Try to build a combination that fits in range
        let best = null;
        let bestDiff = Infinity;

        // Try different combinations starting with different samples
        for (let startIdx = 0; startIdx < sorted.length; startIdx++) {
            let total = 0;
            let count = 0;

            for (let i = startIdx; i < sorted.length; i++) {
                const newTotal = total + sorted[i].duration;

                if (newTotal >= minDuration && newTotal <= maxDuration) {
                    // Found valid combination
                    const diff = Math.abs(newTotal - target);
                    if (diff < bestDiff) {
                        bestDiff = diff;
                        best = { total: newTotal, count: count + 1 };
                    }
                }

                if (newTotal > maxDuration) {
                    break; // This path exceeds max, try next start
                }

                total = newTotal;
                count++;
            }
        }

        return best;
    },

    /**
     * Render available samples (bottom section - full cards with checkboxes + Add buttons)
     */
    renderAvailableSamples(voiceName, samples, selections) {
        const availableList = document.getElementById('availableList');

        availableList.innerHTML = samples.map(sample => {
            const isSelected = selections.includes(sample.wemId);
            const qualityClass = this.getQualityClass(sample.qualityScore);
            const sentimentClass = sample.sentiment ? `sentiment-${sample.sentiment}` : '';

            return `
                <div class="sample-card" data-wem="${sample.wemId}">
                    <div class="checkbox-container">
                        <input type="checkbox" class="analyze-checkbox" data-wem="${sample.wemId}">
                    </div>
                    <button class="play-btn" onclick="VoiceBuilder.playSample('${voiceName}', '${sample.wemId}')" title="Play"></button>
                    <div class="waveform" id="waveform-${sample.wemId}"></div>
                    <div class="info">
                        <div class="transcript ${sample.transcript ? '' : 'empty'}">
                            ${sample.transcript || (sample.analyzed ? 'No speech detected' : 'Not analyzed')}
                        </div>
                        <div class="metrics">
                            <div class="metric">
                                <span class="metric-label">Duration:</span>
                                <span class="metric-value">${sample.duration.toFixed(1)}s</span>
                            </div>
                            ${sample.speechDensity !== null ? `
                                <div class="metric">
                                    <span class="metric-label">Density:</span>
                                    <span class="metric-value ${qualityClass}">${Math.round(sample.speechDensity * 100)}%</span>
                                </div>
                            ` : ''}
                            ${sample.sentiment ? `
                                <div class="metric">
                                    <span class="metric-label">Sentiment:</span>
                                    <span class="metric-value ${sentimentClass}">${sample.sentiment}</span>
                                </div>
                            ` : ''}
                            ${sample.qualityScore !== null ? `
                                <div class="metric">
                                    <span class="metric-label">Quality:</span>
                                    <span class="metric-value ${qualityClass}">${Math.round(sample.qualityScore * 100)}</span>
                                </div>
                            ` : ''}
                        </div>
                    </div>
                    <button class="add-btn ${isSelected ? 'added' : ''}"
                            onclick="VoiceBuilder.toggleAddToSelection('${voiceName}', '${sample.wemId}')">
                        ${isSelected ? '✓ Added' : '+ Add'}
                    </button>
                </div>
            `;
        }).join('');

        // Initialize waveforms after rendering
        this.initializeWaveforms(voiceName, samples);
    },

    /**
     * Initialize WaveSurfer instances for all waveforms
     */
    initializeWaveforms(voiceName, samples) {
        const langInfo = this.state.languages.find(l => l.code === this.state.language);
        const langPath = langInfo ? langInfo.path : 'en-us';

        samples.forEach(sample => {
            const waveformId = `waveform-${sample.wemId}`;
            const container = document.getElementById(waveformId);

            if (!container) return;

            // Destroy existing waveform if it exists
            if (this.state.waveforms[sample.wemId]) {
                this.state.waveforms[sample.wemId].destroy();
                delete this.state.waveforms[sample.wemId];
            }

            try {
                // Create WaveSurfer instance
                const wavesurfer = WaveSurfer.create({
                    container: `#${waveformId}`,
                    waveColor: '#8B7355',
                    progressColor: '#D4A84B',
                    cursorColor: 'transparent',
                    barWidth: 2,
                    barRadius: 2,
                    barGap: 1,
                    height: 40,
                    normalize: true,
                    interact: false,
                    hideScrollbar: true
                });

                // Load audio file
                const audioUrl = `/voice-manager/audio/${langPath}/${voiceName}/${sample.wemId}.wav`;
                wavesurfer.load(audioUrl);

                // Store instance
                this.state.waveforms[sample.wemId] = wavesurfer;
            } catch (error) {
                console.error(`Failed to initialize waveform for ${sample.wemId}:`, error);
            }
        });
    },

    /**
     * Destroy all WaveSurfer instances (cleanup)
     */
    destroyAllWaveforms() {
        Object.values(this.state.waveforms).forEach(wavesurfer => {
            try {
                wavesurfer.destroy();
            } catch (error) {
                console.error('Error destroying waveform:', error);
            }
        });
        this.state.waveforms = {};
    },

    /**
     * Get CSS class for quality score
     */
    getQualityClass(score) {
        if (score === null || score === undefined) return '';
        if (score >= 0.8) return 'quality-excellent';
        if (score >= 0.6) return 'quality-good';
        if (score >= 0.4) return 'quality-fair';
        return 'quality-poor';
    },

    /**
     * Toggle add/remove sample from selection
     */
    async toggleAddToSelection(voiceName, wemId) {
        if (!this.state.selections[voiceName]) {
            this.state.selections[voiceName] = [];
        }

        const index = this.state.selections[voiceName].indexOf(wemId);
        if (index >= 0) {
            this.state.selections[voiceName].splice(index, 1);
        } else {
            this.state.selections[voiceName].push(wemId);
        }

        // Save selection
        await this.saveSelection(voiceName);

        // Update UI
        this.renderSamples(voiceName);
        this.updateProgressSummary();
        this.updateExportButton();
    },

    /**
     * Remove sample from selection (from selected list)
     */
    async removeFromSelection(voiceName, wemId) {
        if (!this.state.selections[voiceName]) return;

        const index = this.state.selections[voiceName].indexOf(wemId);
        if (index >= 0) {
            this.state.selections[voiceName].splice(index, 1);
        }

        // Save selection
        await this.saveSelection(voiceName);

        // Update UI
        this.renderSamples(voiceName);
        this.updateProgressSummary();
        this.updateExportButton();
    },

    /**
     * Save selection for a character
     */
    async saveSelection(voiceName) {
        try {
            await fetch('/voice-manager/api/select', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language,
                    voiceName: voiceName,
                    selectedWemIds: this.state.selections[voiceName] || []
                })
            });
        } catch (error) {
            console.error('Failed to save selection:', error);
        }
    },


    /**
     * Play a sample (toggles between play and stop)
     */
    playSample(voiceName, wemId) {
        // If this sample is already playing, stop it
        if (this.state.currentAudio === wemId) {
            this.stopSample();
            return;
        }

        const langInfo = this.state.languages.find(l => l.code === this.state.language);
        const langPath = langInfo ? langInfo.path : 'en-us';
        const url = `/voice-manager/audio/${langPath}/${voiceName}/${wemId}.wav`;

        // Stop any currently playing sample
        if (this.state.currentAudio) {
            this.stopSample();
        }

        // Play new sample
        this.elements.audioPlayer.src = url;
        this.elements.audioPlayer.play();
        this.state.currentAudio = wemId;

        // Update all buttons for this sample (both play-btn and play-btn-small)
        const buttons = document.querySelectorAll(`[data-wem="${wemId}"] .play-btn, [data-wem="${wemId}"] .play-btn-small`);
        buttons.forEach(btn => {
            btn.classList.add('playing');
            btn.setAttribute('title', 'Stop');
        });
    },

    /**
     * Stop currently playing sample
     */
    stopSample() {
        if (!this.state.currentAudio) return;

        const wemId = this.state.currentAudio;
        this.elements.audioPlayer.pause();
        this.elements.audioPlayer.currentTime = 0;

        // Reset all buttons for this sample back to play icon
        const buttons = document.querySelectorAll(`[data-wem="${wemId}"] .play-btn, [data-wem="${wemId}"] .play-btn-small`);
        buttons.forEach(btn => {
            btn.classList.remove('playing');
            btn.setAttribute('title', 'Play');
        });

        this.state.currentAudio = null;
    },

    /**
     * Handle audio ended
     */
    onAudioEnded() {
        if (this.state.currentAudio) {
            this.stopSample();
        }
    },

    /**
     * Play preview of selected samples sequentially
     */
    playPreview(voiceName) {
        const selections = this.state.selections[voiceName] || [];
        if (selections.length === 0) {
            alert('No samples selected');
            return;
        }

        // Show stop button, hide play button
        document.getElementById('playPreviewBtn').style.display = 'none';
        document.getElementById('stopPreviewBtn').style.display = 'inline-flex';

        // Play samples sequentially
        let currentIndex = 0;
        const langInfo = this.state.languages.find(l => l.code === this.state.language);
        const langPath = langInfo ? langInfo.path : 'en-us';

        const playNext = () => {
            if (currentIndex >= selections.length) {
                // All done - reset buttons
                this.stopPreview();
                return;
            }
            const wemId = selections[currentIndex];
            const url = `/voice-manager/audio/${langPath}/${voiceName}/${wemId}.wav`;

            this.elements.audioPlayer.src = url;
            this.elements.audioPlayer.onended = () => {
                currentIndex++;
                playNext();
            };
            this.elements.audioPlayer.play();
        };

        playNext();
    },

    /**
     * Stop preview playback
     */
    stopPreview() {
        this.elements.audioPlayer.pause();
        this.elements.audioPlayer.currentTime = 0;
        this.elements.audioPlayer.onended = null;

        // Reset buttons
        document.getElementById('playPreviewBtn').style.display = 'inline-flex';
        document.getElementById('stopPreviewBtn').style.display = 'none';
    },

    /**
     * Extract samples for a character
     */
    async extractCharacter(voiceName) {
        this.showProgress('Extracting Samples', `Extracting audio for ${voiceName}...`);

        try {
            const response = await fetch('/voice-manager/api/extract', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language,
                    voiceNames: [voiceName]
                })
            });
            const data = await response.json();

            if (data.taskId) {
                await this.pollTask(data.taskId, 'extract');
            }

            await this.loadCharacters();
            await this.loadSamples(voiceName);
        } catch (error) {
            console.error('Extraction failed:', error);
        } finally {
            this.hideProgress();
        }
    },

    /**
     * Analyze samples for a character (only checked ones)
     */
    async analyzeCharacter(voiceName) {
        // Get checked WEM IDs from checkboxes
        const checkedWemIds = [];
        document.querySelectorAll('.analyze-checkbox:checked').forEach(checkbox => {
            const wemId = checkbox.getAttribute('data-wem');
            if (wemId) checkedWemIds.push(wemId);
        });

        if (checkedWemIds.length === 0) {
            alert('Please check at least one sample to analyze');
            return;
        }

        const analyzerLabel = this.state.analyzer === 'parakeet' ? 'Parakeet' : 'Deepgram';
        this.showProgress('Analyzing Samples', `Analyzing ${checkedWemIds.length} sample(s) with ${analyzerLabel}...`);

        try {
            const response = await fetch('/voice-manager/api/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language,
                    voiceNames: [voiceName],
                    selectedWemIds: { [voiceName]: checkedWemIds },
                    analyzer: this.state.analyzer
                })
            });
            const data = await response.json();

            if (data.taskId) {
                await this.pollTask(data.taskId, 'analyze');
            }

            await this.loadCharacters();
            await this.loadSamples(voiceName);
        } catch (error) {
            console.error('Analysis failed:', error);
        } finally {
            this.hideProgress();
        }
    },

    /**
     * Auto-select samples for a character
     */
    async autoSelectCharacter(voiceName) {
        try {
            const response = await fetch('/voice-manager/api/auto-select', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language,
                    voiceNames: [voiceName],
                    targetDuration: 15
                })
            });
            const data = await response.json();

            if (data.results && data.results[voiceName]) {
                this.state.selections[voiceName] = data.results[voiceName].selectedIds;
            }

            await this.loadCharacters();
            this.renderSamples(voiceName);
            this.updateExportButton();
        } catch (error) {
            console.error('Auto-select failed:', error);
        }
    },

    /**
     * Clear selection for a character
     */
    async clearSelection(voiceName) {
        this.state.selections[voiceName] = [];
        await this.saveSelection(voiceName);
        this.renderSamples(voiceName);
        this.updateProgressSummary();
        this.updateExportButton();
    },

    /**
     * Extract all characters
     */
    async extractAll() {
        const voiceNames = this.state.characters
            .filter(c => c.status === 'pending')
            .map(c => c.voiceName);

        if (voiceNames.length === 0) {
            alert('All characters have been extracted');
            return;
        }

        this.showProgress('Extracting All', `Extracting ${voiceNames.length} characters...`);

        try {
            const response = await fetch('/voice-manager/api/extract', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language,
                    voiceNames: voiceNames
                })
            });
            const data = await response.json();

            if (data.taskId) {
                await this.pollTask(data.taskId, 'extract');
            }

            await this.loadCharacters();
        } catch (error) {
            console.error('Batch extraction failed:', error);
        } finally {
            this.hideProgress();
        }
    },

    /**
     * Auto-build selections for all characters (fully automatic mode)
     *
     * Fully automatic workflow that:
     * - Extracts samples if not already extracted
     * - Intelligently analyzes samples (5-15s range only)
     * - Selects high-quality samples (>0.90 score)
     * - Stops when first two targets (10s, 15s) are met
     * Processes NPCs one by one until all have targets met.
     */
    async autoBuildAll() {
        const analyzerLabel = this.state.analyzer === 'parakeet' ? 'Parakeet' : 'Deepgram';
        this.showProgress('Auto-Building Selections', `Starting automatic selection builder (${analyzerLabel})...`);

        try {
            const response = await fetch('/voice-manager/api/auto-build-selections', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language,
                    analyzer: this.state.analyzer
                })
            });
            const data = await response.json();

            if (data.taskId) {
                // Store taskId for cancellation
                this.state.currentTaskId = data.taskId;

                // Remove any existing unload handler before adding new one
                if (this.state.currentUnloadHandler) {
                    window.removeEventListener('beforeunload', this.state.currentUnloadHandler);
                }

                // Set up page unload handler to cancel task
                const taskId = data.taskId; // Capture in closure
                const unloadHandler = () => {
                    // Use sendBeacon for reliable cancellation on page close
                    const cancelData = JSON.stringify({ taskId: taskId });
                    navigator.sendBeacon('/voice-manager/api/auto-build-selections/cancel',
                        new Blob([cancelData], { type: 'application/json' }));
                };
                this.state.currentUnloadHandler = unloadHandler;
                window.addEventListener('beforeunload', unloadHandler);

                // Poll task
                await this.pollTask(data.taskId, 'auto_build');

                // Clear taskId when done
                this.state.currentTaskId = null;
            }
        } catch (error) {
            console.error('Auto-build failed:', error);
            alert('Auto-build failed: ' + error.message);
        } finally {
            // Clean up task state and handler
            this.state.currentTaskId = null;
            if (this.state.currentUnloadHandler) {
                window.removeEventListener('beforeunload', this.state.currentUnloadHandler);
                this.state.currentUnloadHandler = null;
            }
            this.hideProgress();

            // Refresh ALL data after auto-build (session has new selections)
            await this.loadSession();  // CRITICAL: Load new selections from server
            await this.loadCharacters();
            if (this.state.selectedCharacter) {
                await this.loadSamples(this.state.selectedCharacter);
            }
            this.updateProgressSummary();
            this.updateExportButton();
        }
    },

    /**
     * Analyze all characters
     */
    async analyzeAll() {
        const voiceNames = this.state.characters
            .filter(c => c.status === 'extracted' || c.sampleCount > 0)
            .map(c => c.voiceName);

        if (voiceNames.length === 0) {
            alert('No characters to analyze');
            return;
        }

        const analyzerLabel = this.state.analyzer === 'parakeet' ? 'Parakeet' : 'Deepgram';
        this.showProgress('Analyzing All', `Analyzing ${voiceNames.length} characters with ${analyzerLabel}...`);

        try {
            const response = await fetch('/voice-manager/api/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language,
                    voiceNames: voiceNames,
                    analyzer: this.state.analyzer
                })
            });
            const data = await response.json();

            if (data.taskId) {
                await this.pollTask(data.taskId, 'analyze');
            }

            await this.loadCharacters();
        } catch (error) {
            console.error('Batch analysis failed:', error);
        } finally {
            this.hideProgress();
        }
    },

    /**
     * Auto-select all characters
     */
    async autoSelectAll() {
        const voiceNames = this.state.characters
            .filter(c => c.analyzedCount > 0)
            .map(c => c.voiceName);

        if (voiceNames.length === 0) {
            alert('No analyzed characters to auto-select');
            return;
        }

        this.showProgress('Auto-Selecting All', `Processing ${voiceNames.length} characters...`);

        try {
            const response = await fetch('/voice-manager/api/auto-select', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language,
                    voiceNames: voiceNames,
                    targetDuration: 15
                })
            });
            const data = await response.json();

            if (data.results) {
                for (const [voiceName, result] of Object.entries(data.results)) {
                    this.state.selections[voiceName] = result.selectedIds;
                }
            }

            await this.loadCharacters();
            this.updateExportButton();
        } catch (error) {
            console.error('Batch auto-select failed:', error);
        } finally {
            this.hideProgress();
        }
    },

    /**
     * Poll a background task for completion
     */
    async pollTask(taskId, type) {
        let endpoint;
        if (type === 'extract') {
            endpoint = 'extract';
        } else if (type === 'auto_build') {
            endpoint = 'auto-build-selections';
        } else {
            endpoint = 'analyze';
        }

        while (true) {
            try {
                const response = await fetch(`/voice-manager/api/${endpoint}/status?taskId=${taskId}`);
                const status = await response.json();

                let message = status.current || 'Processing...';
                if (type === 'auto_build') {
                    // Build detailed message for auto-build
                    const charName = status.current || 'Processing';
                    const progress = status.completed !== undefined && status.total !== undefined
                        ? `(${status.completed}/${status.total})`
                        : '';
                    const stage = status.stage ? ` - ${status.stage}` : '';
                    message = `${charName} ${progress}${stage}`;
                }

                this.updateProgress(status.progress, message);

                if (status.status === 'complete' || status.status === 'cancelled') {
                    break;
                }

                await new Promise(resolve => setTimeout(resolve, 500));
            } catch (error) {
                console.error('Task polling failed:', error);
                break;
            }
        }
    },

    /**
     * Show progress modal
     */
    showProgress(title, text) {
        this.elements.progressTitle.textContent = title;
        this.elements.progressText.textContent = text;
        this.elements.progressFill.style.width = '0%';
        this.elements.progressModal.style.display = 'flex';
    },

    /**
     * Update progress modal
     */
    updateProgress(percent, text) {
        this.elements.progressFill.style.width = `${percent}%`;
        this.elements.progressText.textContent = text;
    },

    /**
     * Hide progress modal
     */
    hideProgress() {
        this.elements.progressModal.style.display = 'none';
    },

    /**
     * Cancel active task
     */
    async cancelTask() {
        if (this.state.currentTaskId) {
            try {
                await fetch('/voice-manager/api/auto-build-selections/cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ taskId: this.state.currentTaskId })
                });
                console.log('Task cancelled:', this.state.currentTaskId);
            } catch (error) {
                console.error('Failed to cancel task:', error);
            }
            this.state.currentTaskId = null;
        }
        // Remove unload handler since we're manually cancelling
        if (this.state.currentUnloadHandler) {
            window.removeEventListener('beforeunload', this.state.currentUnloadHandler);
            this.state.currentUnloadHandler = null;
        }
        this.hideProgress();
    },

    /**
     * Update progress summary in left panel
     */
    updateProgressSummary() {
        const complete = this.state.characters.filter(c =>
            (this.state.selections[c.voiceName] || []).length > 0
        ).length;
        const total = this.state.characters.length;

        this.elements.completeCount.textContent = complete;
        this.elements.totalCount.textContent = total;
    },

    /**
     * Update export button state
     */
    updateExportButton() {
        const hasSelections = Object.values(this.state.selections).some(s => s.length > 0);
        this.elements.exportBtn.disabled = !hasSelections;
    },

    /**
     * Export manifest
     */
    async exportManifest() {
        try {
            const response = await fetch('/voice-manager/api/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    language: this.state.language
                })
            });
            const data = await response.json();

            if (data.success) {
                this.elements.exportStatus.textContent = `Exported ${data.voiceCount} voices to ${data.filename}`;
                setTimeout(() => {
                    this.elements.exportStatus.textContent = '';
                }, 5000);
            } else {
                this.elements.exportStatus.textContent = `Error: ${data.error}`;
            }
        } catch (error) {
            console.error('Export failed:', error);
            this.elements.exportStatus.textContent = 'Export failed';
        }
    }
};

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    VoiceBuilder.init();
});
