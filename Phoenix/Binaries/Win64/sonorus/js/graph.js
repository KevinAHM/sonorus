// Memory inspector for NPC long-term memory facts.
// Keeps the old graph.js entry points so the rest of config.js can refresh it.

let graphEdgesData = [];
let activeMemoryCategory = 'all';
let currentEdgeContext = null;

const MEMORY_CATEGORY_LABELS = {
    lore: 'Lore',
    character: 'Character',
    relationship: 'Relationship',
    quest: 'Quest',
    location: 'Location',
    item: 'Item',
    creature: 'Creature',
    faction: 'Faction',
    emotion: 'Emotion',
    combat: 'Combat',
    preference: 'Preference',
    milestone: 'Milestone',
    event: 'Event',
    memory: 'Memory',
    other: 'Other'
};

const MEMORY_CATEGORY_COLORS = {
    lore: '#4a6fa5',
    character: '#4a6fa5',
    relationship: '#7a4f8a',
    quest: '#a67c00',
    location: '#4a7c4e',
    item: '#8b6914',
    creature: '#8b2500',
    faction: '#6a4c93',
    emotion: '#9b5a3c',
    combat: '#8f2f2f',
    preference: '#3d6f73',
    milestone: '#6f5d2f',
    event: '#666',
    memory: '#666',
    other: '#666'
};

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function getFactCategory(fact) {
    return String(fact.target_type || fact.name || 'memory').toLowerCase();
}

function getCategoryLabel(category) {
    return MEMORY_CATEGORY_LABELS[category] || category.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function getCategoryColor(category) {
    return MEMORY_CATEGORY_COLORS[category] || MEMORY_CATEGORY_COLORS.other;
}

function getFactMeta(fact) {
    const meta = [];
    if (fact.chapters?.length) {
        meta.push(fact.chapters.join(', '));
    }
    if (fact.valid_at) {
        const validDate = new Date(fact.valid_at);
        if (!Number.isNaN(validDate.getTime()) && validDate.getFullYear() < 2000) {
            meta.push(validDate.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }));
        }
    }
    if (fact.created_at) {
        const created = new Date(fact.created_at);
        if (!Number.isNaN(created.getTime())) {
            meta.push(`indexed ${created.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`);
        }
    }
    return meta;
}

function getShortNpcName(displayName) {
    if (!displayName || displayName.length <= 10) return displayName;
    const titlePrefixes = ['Professor', 'Headmaster', 'Sir', 'Madam', 'Lord', 'Lady', 'Mr', 'Mrs', 'Ms'];
    if (titlePrefixes.some(prefix => displayName.startsWith(prefix))) return displayName;
    return displayName.split(' ')[0] || displayName;
}

async function refreshNpcList() {
    const select = document.getElementById('graphNpcSelect');
    if (!select) return;

    const currentValue = select.value;
    try {
        const response = await fetch('/api/memories/npcs');
        const data = await response.json();
        if (!data.success) {
            showToast(data.error || 'Failed to load NPCs', 'error');
            return;
        }

        select.innerHTML = '<option value="">-- Select an NPC --</option>';
        for (const npc of data.npcs) {
            const option = document.createElement('option');
            option.value = npc.npc_id;
            option.textContent = npc.npc_name;
            select.appendChild(option);
        }

        if (currentValue) {
            select.value = currentValue;
        }
    } catch (e) {
        console.error('Failed to refresh NPC list:', e);
        showToast('Failed to load NPC list', 'error');
    }
}

async function refreshMemoryInspector() {
    const select = document.getElementById('graphNpcSelect');
    const currentValue = select?.value || '';
    await refreshNpcList();

    if (!select || !currentValue) {
        return;
    }

    select.value = currentValue;
    await loadNpcGraph();
}

function setInspectorVisible(visible) {
    document.getElementById('graphStats').style.display = visible ? 'block' : 'none';
    document.getElementById('graphContainer').style.display = visible ? 'block' : 'none';
    document.getElementById('memorySearchContainer').style.display = visible ? 'block' : 'none';
}

function resetMemoryInspector() {
    graphEdgesData = [];
    activeMemoryCategory = 'all';
    currentEdgeContext = null;
    setInspectorVisible(false);
    document.getElementById('noGraphMessage').style.display = 'none';
    document.getElementById('edgeDetailsPanel').style.display = 'none';
    document.getElementById('memorySearchResults').style.display = 'none';
    document.getElementById('graphLoading').style.display = 'none';
    document.getElementById('graphCanvas').innerHTML = '';
    document.getElementById('graphLegend').innerHTML = '';
}

async function loadNpcGraph() {
    const select = document.getElementById('graphNpcSelect');
    const npcId = select.value;
    const npcName = select.selectedOptions[0]?.textContent || npcId;
    const clearBtn = document.getElementById('clearNpcGraphBtn');
    if (clearBtn) {
        const shortName = getShortNpcName(npcName);
        clearBtn.textContent = npcId ? `Clear ${shortName}'s Memories` : 'Clear Memories';
    }

    resetMemoryInspector();
    if (!npcId) return;

    document.getElementById('graphLoading').style.display = 'block';
    try {
        const response = await fetch(`/api/memories/graph/${encodeURIComponent(npcId)}`);
        const data = await response.json();
        document.getElementById('graphLoading').style.display = 'none';

        if (!data.success) {
            showNoMemoryMessage(data.error || 'Failed to load memory facts');
            return;
        }

        graphEdgesData = Array.isArray(data.edges) ? data.edges : [];
        if (graphEdgesData.length === 0) {
            showNoMemoryMessage('No memory facts available for this NPC.');
            return;
        }

        updateMemoryStats();
        renderCategoryFilters();
        renderFactList();
        setInspectorVisible(true);
    } catch (e) {
        console.error('Failed to load memory facts:', e);
        document.getElementById('graphLoading').style.display = 'none';
        showNoMemoryMessage('Failed to load memory facts.');
        showToast('Failed to load memory facts', 'error');
    }
}

function showNoMemoryMessage(message) {
    const noGraph = document.getElementById('noGraphMessage');
    noGraph.style.display = 'block';
    noGraph.querySelector('p').textContent = message;
}

function updateMemoryStats() {
    const categories = new Set(graphEdgesData.map(getFactCategory));
    document.getElementById('graphNodeCount').textContent = graphEdgesData.length;
    document.getElementById('graphEdgeCount').textContent = categories.size;
}

function renderCategoryFilters() {
    const legend = document.getElementById('graphLegend');
    const counts = new Map();
    for (const fact of graphEdgesData) {
        const category = getFactCategory(fact);
        counts.set(category, (counts.get(category) || 0) + 1);
    }

    const categories = Array.from(counts.keys()).sort((a, b) => getCategoryLabel(a).localeCompare(getCategoryLabel(b)));
    const buttons = [
        `<button type="button" class="memory-category-chip ${activeMemoryCategory === 'all' ? 'active' : ''}" onclick="setMemoryCategory('all')">All <span>${graphEdgesData.length}</span></button>`,
        ...categories.map(category => {
            const color = getCategoryColor(category);
            return `<button type="button" class="memory-category-chip ${activeMemoryCategory === category ? 'active' : ''}" onclick="setMemoryCategory('${escapeHtml(category)}')">
                <span class="memory-category-dot" style="background:${color};"></span>${escapeHtml(getCategoryLabel(category))} <span>${counts.get(category)}</span>
            </button>`;
        })
    ];
    legend.innerHTML = buttons.join('');
}

function setMemoryCategory(category) {
    activeMemoryCategory = category;
    renderCategoryFilters();
    renderFactList();
}

function getVisibleFacts() {
    const facts = activeMemoryCategory === 'all'
        ? graphEdgesData
        : graphEdgesData.filter(fact => getFactCategory(fact) === activeMemoryCategory);
    return [...facts].sort((a, b) => {
        const ca = String(a.created_at || '');
        const cb = String(b.created_at || '');
        return cb.localeCompare(ca);
    });
}

function renderFactList(factsOverride = null) {
    const container = document.getElementById('graphCanvas');
    const facts = factsOverride || getVisibleFacts();
    if (!facts.length) {
        container.innerHTML = '<div class="memory-empty-state">No facts in this category.</div>';
        return;
    }

    container.innerHTML = facts.map((fact, index) => renderFactCard(fact, index)).join('');
    refreshMemoryInspectorIcons();
}

function renderFactCard(fact, index) {
    const category = getFactCategory(fact);
    const meta = getFactMeta(fact);
    const factText = escapeHtml(fact.fact || 'No fact text');
    const metaText = meta.length ? escapeHtml(meta.join(' · ')) : 'No source metadata';
    return `
        <div class="memory-fact-card" data-fact-index="${index}" onclick="showFactDetailsByVisibleIndex(${index})">
            <div class="memory-fact-header">
                <span class="memory-category-badge" style="border-color:${getCategoryColor(category)}; color:${getCategoryColor(category)};">
                    ${escapeHtml(getCategoryLabel(category))}
                </span>
                <button type="button" class="memory-delete-btn" onclick="event.stopPropagation(); deleteFactByVisibleIndex(${index})" title="Delete fact">
                    <i data-lucide="x"></i>
                </button>
            </div>
            <div class="memory-fact-text">${factText}</div>
            <div class="memory-fact-meta">${metaText}</div>
        </div>`;
}

function showFactDetailsByVisibleIndex(index) {
    const fact = getVisibleFacts()[index];
    if (fact) showFactDetails(fact);
}

function showFactDetails(fact) {
    currentEdgeContext = { edges: [fact] };
    const category = getFactCategory(fact);
    const meta = getFactMeta(fact);
    document.getElementById('edgeDetailTitle').textContent = `${getCategoryLabel(category)} Fact`;
    document.getElementById('edgeDetailFact').innerHTML = `
        <div style="display:flex; gap:8px; align-items:flex-start;">
            <button onclick="deleteSpecificFact(0)" title="Delete this fact" style="background:none; border:none; color:var(--ember-red); cursor:pointer; padding:0; font-size:1.1em; line-height:1;">&times;</button>
            <span>${escapeHtml(fact.fact || 'No details available')}</span>
        </div>`;
    document.getElementById('edgeDetailChapters').textContent = fact.chapters?.length ? `Chapter: ${fact.chapters.join(', ')}` : '';
    document.getElementById('edgeDetailTime').textContent = meta.length ? meta.join(' · ') : '';
    document.getElementById('edgeDetailsPanel').style.display = 'block';
}

async function searchNpcMemory() {
    const npcId = document.getElementById('graphNpcSelect').value;
    const query = document.getElementById('memorySearchInput').value.trim();
    if (!npcId || !query) {
        showToast('Select an NPC and enter a search query', 'error');
        return;
    }

    const resultsContainer = document.getElementById('memorySearchResults');
    const resultsList = document.getElementById('memorySearchResultsList');
    resultsList.innerHTML = '<em>Searching...</em>';
    resultsContainer.style.display = 'block';

    try {
        const response = await fetch(`/api/memories/search/${encodeURIComponent(npcId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query })
        });
        const data = await response.json();

        if (data.success && data.results && data.results.length > 0) {
            const facts = data.results.map(result => {
                if (typeof result === 'string') return { fact: result, target_type: 'memory' };
                const exact = graphEdgesData.find(edge => edge.fact === result.fact);
                return exact || { fact: result.fact, source: result.source, target: result.target, target_type: 'memory' };
            });
            resultsList.innerHTML = facts.map((fact, index) => {
                const category = getFactCategory(fact);
                return `<div class="memory-search-result" onclick="showSearchResultFact(${index})">
                    <span class="memory-category-badge" style="border-color:${getCategoryColor(category)}; color:${getCategoryColor(category)};">${escapeHtml(getCategoryLabel(category))}</span>
                    <span>${escapeHtml(fact.fact || '')}</span>
                </div>`;
            }).join('');
            resultsList._memorySearchFacts = facts;
            refreshMemoryInspectorIcons();
            showToast(`Found ${data.results.length} results`, 'success');
        } else if (data.success) {
            resultsList.innerHTML = '<em>No results found for this query.</em>';
        } else {
            resultsList.innerHTML = `<em style="color: var(--red-leather);">Error: ${escapeHtml(data.error || 'Search failed')}</em>`;
        }
    } catch (e) {
        console.error('Search failed:', e);
        resultsList.innerHTML = `<em style="color: var(--red-leather);">Search failed: ${escapeHtml(e.message)}</em>`;
    }
}

function showSearchResultFact(index) {
    const facts = document.getElementById('memorySearchResultsList')._memorySearchFacts || [];
    if (facts[index]) {
        showFactDetails(facts[index]);
    }
}

async function clearNpcGraph() {
    const npcId = document.getElementById('graphNpcSelect').value;
    if (!npcId) return;

    const npcName = document.getElementById('graphNpcSelect').selectedOptions[0]?.textContent || npcId;
    const shortName = getShortNpcName(npcName);
    const confirmed = confirm(
        `Are you sure you want to clear ${shortName}'s memories?\n\n` +
        `This will delete their facts, chapters, and generated bio.\n` +
        `Dialogue history is preserved. You can re-migrate from the\n` +
        `Dialogue History section to regenerate memories.`
    );
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/memories/graph/${encodeURIComponent(npcId)}`, { method: 'DELETE' });
        const data = await response.json();
        if (data.success) {
            showToast(`Cleared ${shortName}'s memories`, 'success');
        } else if (data.chapters_cleared) {
            showToast(`Cleared ${shortName}'s chapters (memory clear failed: ${data.error || 'unknown'})`, 'warning');
        } else {
            showToast(data.error || 'Clear failed', 'error');
        }

        if (data.success || data.chapters_cleared) {
            await loadNpcGraph();
            if (typeof loadMigrationStatus === 'function') await loadMigrationStatus();
            const historyPerspective = document.getElementById('historyPerspective');
            if (historyPerspective && historyPerspective.value === npcId && typeof filterHistoryByPerspective === 'function') {
                await filterHistoryByPerspective(false);
            }
        }
    } catch (e) {
        console.error('Clear memories failed:', e);
        showToast('Clear failed', 'error');
    }
}

async function recheckNpcChapters() {
    const npcId = document.getElementById('graphNpcSelect').value;
    if (!npcId) return;

    const btn = document.getElementById('recheckNpcGraphBtn');
    const npcName = document.getElementById('graphNpcSelect').selectedOptions[0]?.textContent || npcId;
    const shortName = getShortNpcName(npcName);
    const originalText = btn?.textContent || 'Recheck Chapters';
    const confirmed = confirm(
        `Force a chapter check for ${shortName} now?\n\n` +
        `This bypasses the minimum new-message threshold.\n` +
        `It does not clear or rebuild memories.`
    );
    if (!confirmed) return;

    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Checking...';
    }

    try {
        const response = await fetch(`/api/memories/recheck/${encodeURIComponent(npcId)}`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Chapter recheck failed');

        if (data.status === 'no_new_entries') {
            showToast(`No pending dialogue to check for ${shortName}`, 'info');
        } else if (data.status === 'skipped') {
            showToast(`Checked ${shortName}; no chapter change`, 'info');
        } else {
            const bypassed = data.threshold_bypassed ? ' (threshold bypassed)' : '';
            showToast(`Checked ${shortName}: ${data.current_chapter_action || 'continue'}${bypassed}`, 'success');
        }

        await loadNpcGraph();
        if (typeof loadMigrationStatus === 'function') await loadMigrationStatus();
    } catch (e) {
        console.error('Chapter recheck failed:', e);
        showToast(e.message || 'Chapter recheck failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    }
}

async function deleteFactByVisibleIndex(index) {
    const fact = getVisibleFacts()[index];
    if (fact) {
        currentEdgeContext = { edges: [fact] };
        await deleteSpecificFact(0);
    }
}

async function deleteGraphNode(npcId, nodeName) {
    showToast('Entity deletion is not available in fact memory. Delete individual facts instead.', 'info');
}

async function deleteGraphEdge(npcId, edgeData) {
    currentEdgeContext = { edges: [edgeData] };
    await deleteSpecificFact(0);
}

async function deleteSpecificFact(index) {
    if (!currentEdgeContext || !currentEdgeContext.edges[index]) return;
    const fact = currentEdgeContext.edges[index];
    const npcId = document.getElementById('graphNpcSelect').value;
    const confirmed = confirm(`Delete this fact?\n\n"${fact.fact}"\n\nThis cannot be undone.`);
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/memories/graph/${encodeURIComponent(npcId)}/edge`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source: fact.source || '',
                target: fact.target || '',
                fact: fact.fact
            })
        });
        const data = await response.json();
        if (data.success) {
            showToast('Fact deleted', 'success');
            document.getElementById('edgeDetailsPanel').style.display = 'none';
            await loadNpcGraph();
        } else {
            showToast(data.error || 'Delete failed', 'error');
        }
    } catch (e) {
        console.error('Delete fact failed:', e);
        showToast('Delete failed', 'error');
    }
}

function highlightGraphNodes() {}
function clearGraphHighlight() {}
function hideGraphContextMenu() {}

function refreshMemoryInspectorIcons() {
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
        window.lucide.createIcons();
    }
}

document.addEventListener('DOMContentLoaded', function() {
    setTimeout(refreshNpcList, 500);
});
