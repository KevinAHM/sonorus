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
    if (hpData) {
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
    }

    // NPC Schedule Enhanced mod
    const nsData = gameModsData.npc_schedule;
    if (nsData) {
        const nsStatusEl = document.getElementById('modStatusNpcSchedule');
        const nsNotInstalledEl = document.getElementById('npcScheduleNotInstalled');
        const nsSettingsEl = document.getElementById('npcScheduleSettings');
        const nsPanel = document.getElementById('modPanelNpcSchedule');

        if (nsData.installed) {
            nsStatusEl.innerHTML = '<span class="badge badge-success">Detected</span>';
            nsNotInstalledEl.style.display = 'none';
            nsSettingsEl.style.display = 'block';

            const contextEnabled = document.getElementById('npcScheduleContextEnabled');
            if (contextEnabled) contextEnabled.checked = nsData.settings?.context_enabled ?? true;

            const notifEnabled = document.getElementById('npcScheduleNotificationsEnabled');
            if (notifEnabled) notifEnabled.checked = nsData.settings?.notifications_enabled ?? true;

            if (nsPanel && nsPanel.classList.contains('collapsed')) {
                const header = nsPanel.querySelector('.sub-panel-header');
                if (header) toggleSubPanel(header);
            }
        } else {
            nsStatusEl.innerHTML = '<span class="badge badge-muted">Not Detected</span>';
            nsNotInstalledEl.style.display = 'block';
            nsSettingsEl.style.display = 'none';
        }
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

// ── NPC Schedule Live Display ──────────────────────────────────────

let _teacherSchedules = null;
let _cachedPlayerHouse = null;

async function _loadTeacherSchedules() {
    if (_teacherSchedules) return _teacherSchedules;
    try {
        const resp = await fetch('/data/teacher_schedules.json');
        if (resp.ok) _teacherSchedules = await resp.json();
    } catch (e) { /* ignore */ }
    return _teacherSchedules;
}

const _DISPLAY_SUBJECTS = { 'Beasts': 'Care of Magical Creatures' };
const _DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

function _fmtMil(mil) {
    const h = Math.floor(mil / 100), m = mil % 100;
    const ampm = h < 12 ? 'AM' : 'PM';
    const h12 = h % 12 || 12;
    return `${h12}:${String(m).padStart(2, '0')} ${ampm}`;
}

function _getPlayerTimeline(sched, dayName, playerHouse) {
    if (!sched || !playerHouse) return [];
    const teachers = sched.teachers || {};
    const isWeekend = dayName === 'Saturday' || dayName === 'Sunday';

    if (isWeekend) {
        return [
            { label: 'Weekend free time', start: 600, end: 2159, type: 'free' },
            { label: 'After hours', start: 2200, end: 559, type: 'after_hours' },
        ];
    }

    const periods = [
        { label: 'Free time', start: 600, end: 759, type: 'free' },
        { label: 'Breakfast', start: 800, end: 959, type: 'meal' },
        { label: 'Free time', start: 1000, end: 1159, type: 'free' },
        { label: 'Class block', start: 1200, end: 1359, type: 'class_block' },
        { label: 'Free time', start: 1400, end: 1559, type: 'free' },
        { label: 'Class block', start: 1600, end: 1759, type: 'class_block' },
        { label: 'Free time', start: 1800, end: 1859, type: 'free' },
        { label: 'Dinner', start: 1900, end: 2029, type: 'meal' },
        { label: 'Free time', start: 2030, end: 2159, type: 'free' },
        { label: 'After hours', start: 2200, end: 559, type: 'after_hours' },
    ];

    // Resolve class blocks for the player's house
    for (const [tid, tinfo] of Object.entries(teachers)) {
        const subj = _DISPLAY_SUBJECTS[tinfo.subject] || tinfo.subject;
        const lastName = tid.replace(/([a-z])([A-Z])/g, '$1 $2').split(' ').pop();
        for (const cls of (tinfo.classes || [])) {
            if (cls.day !== dayName || !cls.houses.includes(playerHouse)) continue;
            const cs = parseInt(cls.start.replace(':', ''), 10);
            if (cs === 2100) {
                // Astronomy: split free_night slot
                for (let i = 0; i < periods.length; i++) {
                    if (periods[i].type === 'free' && periods[i].start === 2030) {
                        periods[i] = { label: 'Free time', start: 2030, end: 2059, type: 'free' };
                        periods.splice(i + 1, 0, {
                            label: `Astronomy with Professor ${lastName}`,
                            start: 2100, end: 2159, type: 'class',
                            houses: cls.houses, subject: 'Astronomy'
                        });
                        break;
                    }
                }
            } else {
                for (const p of periods) {
                    if (p.type === 'class_block' && p.start === cs) {
                        p.label = `${subj} with Professor ${lastName}`;
                        p.type = 'class';
                        p.houses = cls.houses;
                        p.subject = subj;
                    }
                }
            }
        }
    }

    // Remaining unresolved class_blocks become free time
    for (const p of periods) {
        if (p.type === 'class_block') {
            p.label = 'Free time';
            p.type = 'free';
        }
    }

    return periods;
}

function _inPeriod(now, start, end) {
    if (start <= end) return now >= start && now <= end;
    return now >= start || now <= end;
}

function updateNpcScheduleDisplay(gameTime, playerHouse) {
    const container = document.getElementById('npcScheduleContent');
    const wrapper = document.getElementById('npcScheduleLiveData');
    if (!container || !wrapper) return;
    if (!_teacherSchedules || !gameTime || !gameTime.available || !playerHouse) {
        wrapper.style.display = 'none';
        return;
    }

    wrapper.style.display = 'block';

    const dayName = _DAY_NAMES[gameTime.dayOfWeek] || 'Monday';
    // Parse game time to military
    const match = gameTime.gameTime?.match(/(\d+):(\d+)/);
    if (!match) { container.innerHTML = '<span style="opacity:0.6;">Awaiting game data...</span>'; return; }
    let hours = parseInt(match[1], 10);
    const minutes = parseInt(match[2], 10);
    if (gameTime.gameTime.includes('PM') && hours !== 12) hours += 12;
    else if (gameTime.gameTime.includes('AM') && hours === 12) hours = 0;
    const nowMil = hours * 100 + minutes;

    const timeline = _getPlayerTimeline(_teacherSchedules, dayName, playerHouse);

    let current = null, next = null;
    for (let i = 0; i < timeline.length; i++) {
        if (_inPeriod(nowMil, timeline[i].start, timeline[i].end)) {
            current = timeline[i];
            if (i + 1 < timeline.length) next = timeline[i + 1];
            break;
        }
    }

    if (!current) { container.innerHTML = '<span style="opacity:0.6;">Awaiting game data...</span>'; return; }

    const houseColors = {
        'Gryffindor': '#740001', 'Slytherin': '#1a472a',
        'Hufflepuff': '#7a5c00', 'Ravenclaw': '#0e1a40'
    };
    const houseColor = houseColors[playerHouse] || 'var(--ink-brown)';

    let html = '';

    // Current period
    const isWeekend = dayName === 'Saturday' || dayName === 'Sunday';
    if (current.type === 'class') {
        html += `<div style="margin-bottom: var(--space-sm);">
            <span style="font-weight: 600; color: ${houseColor};">Now:</span>
            ${current.label}
            <span style="opacity: 0.6;">(${_fmtMil(current.start)} – ${_fmtMil(current.end)})</span>
        </div>`;
    } else if (current.type === 'meal') {
        html += `<div style="margin-bottom: var(--space-sm);">
            <span style="font-weight: 600; color: ${houseColor};">Now:</span>
            ${current.label}
        </div>`;
    } else if (isWeekend) {
        html += `<div style="margin-bottom: var(--space-sm);">
            <span style="font-weight: 600; color: ${houseColor};">Now:</span>
            Weekend free time
        </div>`;
    } else if (current.type === 'after_hours') {
        html += `<div style="margin-bottom: var(--space-sm);">
            <span style="font-weight: 600; color: ${houseColor};">Now:</span>
            After hours
        </div>`;
    } else {
        html += `<div style="margin-bottom: var(--space-sm);">
            <span style="font-weight: 600; color: ${houseColor};">Now:</span>
            Free time
        </div>`;
    }

    // Next period
    if (next) {
        const nextTime = _fmtMil(next.start);
        if (next.type === 'class') {
            html += `<div><span style="opacity: 0.6;">Next:</span> ${next.label} at ${nextTime}</div>`;
        } else if (next.type === 'meal') {
            html += `<div><span style="opacity: 0.6;">Next:</span> ${next.label} at ${nextTime}</div>`;
        } else if (next.type === 'after_hours') {
            html += `<div><span style="opacity: 0.6;">Next:</span> After hours at ${nextTime}</div>`;
        } else {
            html += `<div><span style="opacity: 0.6;">Next:</span> Free time at ${nextTime}</div>`;
        }
    }

    container.innerHTML = html;
    container.style.opacity = '1';
    container.style.textAlign = 'left';
}

// Initialize game mods on page load
window.addEventListener('load', () => {
    loadGameModsStatus();
    _loadTeacherSchedules();
    setInterval(loadGameModsStatus, 5000);  // Poll every 5 seconds
});
