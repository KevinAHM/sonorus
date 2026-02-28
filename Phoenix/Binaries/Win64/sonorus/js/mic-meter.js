// Mic Level Meter for VAD calibration
(function() {
    let audioContext = null;
    let analyser = null;
    let mediaStream = null;
    let animationId = null;
    let isRunning = false;

    window.toggleMicMeter = function() {
        if (isRunning) {
            stopMicMeter();
        } else {
            startMicMeter();
        }
    };

    window.updateMicMeterThreshold = function(value) {
        const marker = document.getElementById('mic_meter_threshold');
        if (marker) {
            marker.style.left = (parseFloat(value) * 100) + '%';
        }
    };

    async function startMicMeter() {
        const btn = document.getElementById('mic_meter_toggle');
        const wrapper = document.getElementById('mic_meter_wrapper');

        try {
            mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
            audioContext = new (window.AudioContext || window.webkitAudioContext)();
            analyser = audioContext.createAnalyser();
            analyser.fftSize = 512;
            analyser.smoothingTimeConstant = 0.5;

            const source = audioContext.createMediaStreamSource(mediaStream);
            source.connect(analyser);

            isRunning = true;
            btn.textContent = 'Stop Test';
            btn.classList.add('btn-danger');
            wrapper.style.display = 'block';

            // Show VAD threshold marker only when open mic is enabled
            const openMicEnabled = config?.open_mic?.enabled === true;
            const thresholdMarker = document.getElementById('mic_meter_threshold');
            if (thresholdMarker) {
                thresholdMarker.style.display = openMicEnabled ? 'block' : 'none';
            }
            if (openMicEnabled) {
                const thresholdSlider = document.getElementById('open_mic_vad_threshold');
                if (thresholdSlider) {
                    updateMicMeterThreshold(thresholdSlider.value);
                }
            }

            updateMeter();
        } catch (err) {
            console.error('Mic access error:', err);
            alert('Could not access microphone: ' + err.message);
        }
    }

    function stopMicMeter() {
        const btn = document.getElementById('mic_meter_toggle');
        const wrapper = document.getElementById('mic_meter_wrapper');
        const level = document.getElementById('mic_meter_level');

        isRunning = false;

        if (animationId) {
            cancelAnimationFrame(animationId);
            animationId = null;
        }

        if (mediaStream) {
            mediaStream.getTracks().forEach(track => track.stop());
            mediaStream = null;
        }

        if (audioContext) {
            audioContext.close();
            audioContext = null;
        }

        btn.textContent = 'Test Microphone';
        btn.classList.remove('btn-danger');
        wrapper.style.display = 'none';
        if (level) level.style.width = '0%';
    }

    function updateMeter() {
        if (!isRunning || !analyser) return;

        const dataArray = new Float32Array(analyser.fftSize);
        analyser.getFloatTimeDomainData(dataArray);

        // Calculate RMS (root mean square) for volume level
        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) {
            sum += dataArray[i] * dataArray[i];
        }
        const rms = Math.sqrt(sum / dataArray.length);

        // Apply mic gain boost from settings (dB to linear)
        const gainDb = config?.stt?.mic_gain_db || 0;
        const gainLinear = gainDb > 0 ? Math.pow(10, gainDb / 20) : 1;

        // Convert to 0-1 scale (RMS is typically 0-1 for normalized audio)
        // Apply some scaling to make it more visible
        const level = Math.min(1, rms * 4 * gainLinear);

        const levelBar = document.getElementById('mic_meter_level');
        if (levelBar) {
            levelBar.style.width = (level * 100) + '%';
        }

        animationId = requestAnimationFrame(updateMeter);
    }

    // Cleanup on page unload
    window.addEventListener('beforeunload', stopMicMeter);
})();
