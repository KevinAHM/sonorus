(function () {
    let activeAudio = null;
    let activeButton = null;
    let activeEntryKey = '';
    let activeUrl = '';

    function renderButtonIcon(button, iconName) {
        if (!button) return;
        button.innerHTML = `<i data-lucide="${iconName}"></i>`;
        if (window.lucide && typeof window.lucide.createIcons === 'function') {
            lucide.createIcons({ nodes: [button] });
        }
    }

    function setButtonState(button, isPlaying) {
        if (!button) return;
        button.dataset.state = isPlaying ? 'playing' : 'stopped';
        button.setAttribute('aria-label', isPlaying ? 'Stop archived audio' : 'Play archived audio');
        button.title = isPlaying ? 'Stop audio' : 'Play archived audio';
        renderButtonIcon(button, isPlaying ? 'square' : 'play');
    }

    function clearActiveButton() {
        if (activeButton) {
            setButtonState(activeButton, false);
        }
        activeButton = null;
    }

    function stopAll() {
        if (activeAudio) {
            activeAudio.pause();
            activeAudio.currentTime = 0;
            activeAudio.src = '';
            activeAudio = null;
        }

        clearActiveButton();
        activeEntryKey = '';
        activeUrl = '';
    }

    function handlePlaybackFinished() {
        stopAll();
    }

    function play(button) {
        const url = button?.dataset?.audioUrl;
        if (!url) return;

        if (activeAudio && activeUrl === url && activeButton === button) {
            stopAll();
            return;
        }

        stopAll();

        const audio = new Audio(url);
        activeAudio = audio;
        activeButton = button;
        activeEntryKey = button.dataset.entryKey || '';
        activeUrl = url;

        audio.addEventListener('ended', handlePlaybackFinished);
        audio.addEventListener('error', handlePlaybackFinished);

        setButtonState(button, true);

        const playPromise = audio.play();
        if (playPromise && typeof playPromise.catch === 'function') {
            playPromise.catch(() => {
                stopAll();
            });
        }
    }

    function findMatchingButton(root) {
        if (!root || !activeUrl) return null;
        const buttons = root.querySelectorAll('.history-audio-toggle');
        for (const button of buttons) {
            if (button.dataset.audioUrl !== activeUrl) continue;
            if (activeEntryKey && button.dataset.entryKey !== activeEntryKey) continue;
            return button;
        }
        return null;
    }

    function syncState(root) {
        if (window.lucide && typeof window.lucide.createIcons === 'function' && root) {
            lucide.createIcons({ nodes: [root] });
        }

        if (!activeAudio) return;

        const nextButton = findMatchingButton(root || document);
        if (nextButton) {
            if (activeButton && activeButton !== nextButton) {
                setButtonState(activeButton, false);
            }
            activeButton = nextButton;
            setButtonState(activeButton, true);
        }
    }

    document.addEventListener('click', (event) => {
        const button = event.target.closest('.history-audio-toggle');
        if (!button) return;
        event.preventDefault();
        play(button);
    });

    window.HistoryAudioPlayer = {
        stopAll,
        syncState,
    };
})();
