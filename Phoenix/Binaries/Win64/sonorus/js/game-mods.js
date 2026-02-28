// ============================================
// Game Mods Functions
// ============================================

let gameModsData = {};

async function loadGameModsStatus() {
    try {
        const response = await fetch('/api/game-mods/status');
        if (!response.ok) return;
        const data = await response.json();
        gameModsData = data.mods || {};
        updateGameModsUI();
    } catch (error) {
        console.error('Failed to load game mods status:', error);
    }
}

function updateGameModsUI() {
    // Floo Flame Companions mod - compatible via SetSystemicCompanionBP
    // No longer needs to disable NPC Actions

    // House Points mod
    const hpData = gameModsData.house_points;
    if (!hpData) return;

    const statusEl = document.getElementById('modStatusHousePoints');
    const notInstalledEl = document.getElementById('housePointsNotInstalled');
    const settingsEl = document.getElementById('housePointsSettings');
    const liveDataEl = document.getElementById('housePointsLiveData');
    const panel = document.getElementById('modPanelHousePoints');

    if (hpData.installed) {
        // Mod is installed
        statusEl.innerHTML = '<span class="badge badge-success">Detected</span>';
        notInstalledEl.style.display = 'none';
        settingsEl.style.display = 'block';

        // Update settings checkboxes
        const contextEnabled = document.getElementById('housePointsContextEnabled');
        const teacherActions = document.getElementById('housePointsTeacherActions');
        if (contextEnabled) contextEnabled.checked = hpData.settings?.context_enabled ?? true;
        if (teacherActions) teacherActions.checked = hpData.settings?.teacher_actions ?? true;

        // Update live standings if we have data
        const points = hpData.live_data?.points;
        if (points && Object.keys(points).length > 0) {
            liveDataEl.style.display = 'block';
            updateHousePointsTable(points);
        } else {
            liveDataEl.style.display = 'block';  // Show with "awaiting data" message
        }

        // Expand the sub-panel if installed (and currently collapsed)
        if (panel && panel.classList.contains('collapsed')) {
            const header = panel.querySelector('.sub-panel-header');
            if (header) toggleSubPanel(header);
        }
    } else {
        // Mod not installed
        statusEl.innerHTML = '<span class="badge badge-muted">Not Detected</span>';
        notInstalledEl.style.display = 'block';
        settingsEl.style.display = 'none';
        liveDataEl.style.display = 'none';
    }

    // Refresh Lucide icons for any new elements
    if (window.lucide) lucide.createIcons();
}

function updateHousePointsTable(points) {
    const tbody = document.getElementById('housePointsTableBody');
    if (!tbody) return;

    const houses = ['Gryffindor', 'Slytherin', 'Hufflepuff', 'Ravenclaw'];
    const houseColors = {
        'Gryffindor': { bg: '#740001', text: '#740001' },
        'Slytherin': { bg: '#1a472a', text: '#1a472a' },
        'Hufflepuff': { bg: '#ecb939', text: '#7a5c00' },  // Darker text for readability
        'Ravenclaw': { bg: '#0e1a40', text: '#0e1a40' }
    };

    let html = '';
    let hasData = false;

    for (const house of houses) {
        const p = points[house];
        if (p) {
            hasData = true;
            const colors = houseColors[house];
            html += `<tr style="background: ${colors.bg}22;">
                <td style="font-weight: 600; color: ${colors.text};">${house}</td>
                <td>${p.season ?? '-'}</td>
                <td>${p.month ?? '-'}</td>
                <td>${p.week ?? '-'}</td>
                <td>${p.day ?? '-'}</td>
            </tr>`;
        }
    }

    if (!hasData) {
        html = '<tr><td colspan="5" style="text-align: center; opacity: 0.6;">Awaiting game data...</td></tr>';
    }

    tbody.innerHTML = html;
}

async function updateModSetting(modId, settingKey, value) {
    try {
        const payload = {};
        payload[modId] = {};
        payload[modId][settingKey] = value;

        const response = await fetch('/api/game-mods/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            showToast(`${settingKey.replace(/_/g, ' ')} ${value ? 'enabled' : 'disabled'}`, 'success');
            // Update local cache
            if (gameModsData[modId]) {
                if (!gameModsData[modId].settings) gameModsData[modId].settings = {};
                gameModsData[modId].settings[settingKey] = value;
            }
        } else {
            showToast('Failed to save mod setting', 'error');
        }
    } catch (error) {
        console.error('Failed to update mod setting:', error);
        showToast('Failed to save mod setting', 'error');
    }
}

// Initialize game mods on page load
window.addEventListener('load', () => {
    loadGameModsStatus();
    setInterval(loadGameModsStatus, 5000);  // Poll every 5 seconds
});
