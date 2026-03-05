// Graph visualization and memory search for NPC knowledge graphs
// Requires: vis-network library, showToast() function from main page

let graphNetwork = null;
let graphEdgesData = [];

// Node type to color mapping
const NODE_COLORS = {
    'Character': '#4a6fa5',
    'Location': '#4a7c4e',
    'Quest': '#a67c00',
    'Item': '#8b6914',
    'Faction': '#6a4c93',
    'Creature': '#8b2500'
};

async function refreshNpcList() {
    const select = document.getElementById('graphNpcSelect');
    const currentValue = select.value;

    try {
        const response = await fetch('/api/memories/npcs');
        const data = await response.json();

        if (!data.success) {
            showToast(data.error || 'Failed to load NPCs', 'error');
            return;
        }

        // Clear and repopulate
        select.innerHTML = '<option value="">-- Select an NPC --</option>';
        for (const npc of data.npcs) {
            const option = document.createElement('option');
            option.value = npc.npc_id;
            option.textContent = npc.npc_name;
            select.appendChild(option);
        }

        // Restore selection if it still exists
        if (currentValue) {
            select.value = currentValue;
        }
    } catch (e) {
        console.error('Failed to refresh NPC list:', e);
        showToast('Failed to load NPC list', 'error');
    }
}

// Get short name for buttons (first name unless has title)
function getShortNpcName(displayName) {
    if (!displayName || displayName.length <= 10) return displayName;
    const titlePrefixes = ['Professor', 'Headmaster', 'Sir', 'Madam', 'Lord', 'Lady', 'Mr', 'Mrs', 'Ms'];
    if (titlePrefixes.some(prefix => displayName.startsWith(prefix))) return displayName;
    return displayName.split(' ')[0] || displayName;
}

async function loadNpcGraph() {
    const select = document.getElementById('graphNpcSelect');
    const npcId = select.value;
    const npcName = select.selectedOptions[0]?.textContent || npcId;

    // Update clear button text with NPC's first name
    const clearBtn = document.getElementById('clearNpcGraphBtn');
    if (clearBtn) {
        const shortName = getShortNpcName(npcName);
        clearBtn.textContent = npcId ? `Clear ${shortName}'s Memories` : 'Clear Memories';
    }

    // Hide all panels
    document.getElementById('graphStats').style.display = 'none';
    document.getElementById('graphContainer').style.display = 'none';
    document.getElementById('noGraphMessage').style.display = 'none';
    document.getElementById('edgeDetailsPanel').style.display = 'none';
    document.getElementById('memorySearchContainer').style.display = 'none';
    document.getElementById('memorySearchResults').style.display = 'none';
    document.getElementById('graphLoading').style.display = 'none';

    if (!npcId) {
        // Destroy existing network
        if (graphNetwork) {
            graphNetwork.destroy();
            graphNetwork = null;
        }
        return;
    }

    // Show loading
    document.getElementById('graphLoading').style.display = 'block';

    try {
        const response = await fetch(`/api/memories/graph/${encodeURIComponent(npcId)}`);
        const data = await response.json();

        document.getElementById('graphLoading').style.display = 'none';

        if (!data.success) {
            document.getElementById('noGraphMessage').style.display = 'block';
            document.getElementById('noGraphMessage').querySelector('p').textContent = data.error || 'Failed to load graph';
            return;
        }

        if (!data.edges || data.edges.length === 0) {
            document.getElementById('noGraphMessage').style.display = 'block';
            return;
        }

        // Store edges for detail panel
        graphEdgesData = data.edges;

        // Build nodes and edges for vis-network
        const nodesMap = new Map();
        const nodeConnections = new Map(); // Track unique connected nodes
        const edges = [];

        for (const edge of data.edges) {
            // Add source node
            if (!nodesMap.has(edge.source)) {
                nodesMap.set(edge.source, {
                    id: edge.source,
                    label: edge.source,
                    color: NODE_COLORS[edge.source_type] || NODE_COLORS['Character'] || '#666',
                    type: edge.source_type || 'Unknown'
                });
                nodeConnections.set(edge.source, new Set());
            }
            nodeConnections.get(edge.source).add(edge.target);

            // Add target node
            if (!nodesMap.has(edge.target)) {
                nodesMap.set(edge.target, {
                    id: edge.target,
                    label: edge.target,
                    color: NODE_COLORS[edge.target_type] || NODE_COLORS['Character'] || '#666',
                    type: edge.target_type || 'Unknown'
                });
                nodeConnections.set(edge.target, new Set());
            }
            nodeConnections.get(edge.target).add(edge.source);

            // Add edge
            edges.push({
                from: edge.source,
                to: edge.target,
                label: edge.name || '',
                title: edge.fact,
                edgeData: edge
            });
        }

        // Add degree to nodes for size scaling (unique connections, not edge count)
        const nodes = Array.from(nodesMap.values()).map(n => ({
            ...n,
            degree: nodeConnections.get(n.id)?.size || 1
        }));

        // Update stats
        document.getElementById('graphNodeCount').textContent = nodes.length;
        document.getElementById('graphEdgeCount').textContent = edges.length;
        document.getElementById('graphStats').style.display = 'block';
        document.getElementById('graphContainer').style.display = 'block';
        document.getElementById('memorySearchContainer').style.display = 'block';

        // Create vis-network
        renderGraph(nodes, edges);

    } catch (e) {
        console.error('Failed to load graph:', e);
        document.getElementById('graphLoading').style.display = 'none';
        document.getElementById('noGraphMessage').style.display = 'block';
        showToast('Failed to load graph', 'error');
    }
}

function renderGraph(nodes, edges) {
    const container = document.getElementById('graphCanvas');

    // Calculate min/max degree for scaling
    const degrees = nodes.map(n => n.degree);
    const minDegree = Math.min(...degrees);
    const maxDegree = Math.max(...degrees);
    const degreeRange = maxDegree - minDegree || 1;

    // Format nodes - size scaled by degree (connections)
    const visNodes = nodes.map(n => {
        // Scale: 8px min, 30px max based on degree
        const normalizedDegree = (n.degree - minDegree) / degreeRange;
        const size = 8 + (normalizedDegree * 22);
        // Font scales with node size
        const fontSize = 9 + (normalizedDegree * 4);

        return {
            id: n.id,
            label: n.label,
            color: {
                background: n.color,
                border: n.color,
                highlight: { background: n.color, border: '#d4a84b' },
                hover: { background: n.color, border: '#d4a84b' }
            },
            font: { color: '#fff', size: fontSize, face: 'Crimson Text', strokeWidth: 2, strokeColor: n.color },
            shape: 'dot',
            size: size,
            value: n.degree, // Used by physics for mass
            title: `${n.label} (${n.type}) - ${n.degree} ${n.degree === 1 ? 'connection' : 'connections'}`
        };
    });

    // Format edges - hide labels, show on hover
    const visEdges = edges.map((e, i) => ({
        id: i,
        from: e.from,
        to: e.to,
        title: `${e.label}: ${e.title}`,
        arrows: { to: { enabled: true, scaleFactor: 0.4 } },
        color: { color: 'rgba(139, 105, 20, 0.3)', highlight: '#d4a84b', hover: '#8b6914' },
        smooth: { type: 'continuous' },
        width: 0.5,
        edgeData: e.edgeData
    }));

    // Destroy existing network
    if (graphNetwork) {
        graphNetwork.destroy();
    }

    // Create network
    const data = {
        nodes: new vis.DataSet(visNodes),
        edges: new vis.DataSet(visEdges)
    };

    const options = {
        physics: {
            enabled: true,
            solver: 'barnesHut',
            barnesHut: {
                gravitationalConstant: -5000,
                centralGravity: 0.3,
                springLength: 200,
                springConstant: 0.05,
                damping: 0.9,
                avoidOverlap: 0.5
            },
            stabilization: {
                enabled: true,
                iterations: 500,
                updateInterval: 25,
                fit: true
            },
            maxVelocity: 50,
            minVelocity: 0.1
        },
        interaction: {
            hover: true,
            tooltipDelay: 100,
            zoomView: true,
            dragView: true
        },
        nodes: {
            borderWidth: 1,
            shadow: false
        },
        edges: {
            shadow: false,
            selectionWidth: 1.5
        }
    };

    graphNetwork = new vis.Network(container, data, options);

    // Stop physics after stabilization to prevent drift/rotation
    graphNetwork.on('stabilizationIterationsDone', function() {
        graphNetwork.setOptions({ physics: { enabled: false } });
    });

    // Unified click handler for edges, nodes, and empty space
    graphNetwork.on('click', function(params) {
        hideGraphContextMenu();
        if (params.edges.length > 0) {
            // Edge clicked directly - show its details
            const edgeId = params.edges[0];
            const edge = visEdges[edgeId];
            if (edge && edge.edgeData) {
                showEdgeDetails(edge.edgeData);
            }
        } else if (params.nodes.length > 0) {
            // Node clicked - show all facts for this node
            showNodeFacts(params.nodes[0]);
        } else {
            // Empty space
            document.getElementById('edgeDetailsPanel').style.display = 'none';
        }
    });

    // Right-click context menu
    graphNetwork.on('oncontext', function(params) {
        console.log('[Graph] Right-click detected', params);
        params.event.preventDefault();
        const pointer = params.pointer;
        const nodeId = graphNetwork.getNodeAt(pointer.DOM);
        const edgeId = graphNetwork.getEdgeAt(pointer.DOM);
        console.log('[Graph] Node:', nodeId, 'Edge:', edgeId);

        // Get the original browser event for positioning
        const domEvent = params.event.srcEvent || params.event;

        if (nodeId) {
            showGraphContextMenu(domEvent, 'node', nodeId, visNodes.find(n => n.id === nodeId));
        } else if (edgeId !== undefined) {
            showGraphContextMenu(domEvent, 'edge', edgeId, visEdges[edgeId]);
        } else {
            hideGraphContextMenu();
        }
    });

    // Also prevent default browser context menu on the canvas
    container.addEventListener('contextmenu', function(e) {
        e.preventDefault();
    });
}

// Context menu state
let ctxMenuTarget = null;

function showGraphContextMenu(event, type, id, data) {
    const menu = document.getElementById('graphContextMenu');
    const deleteNode = document.getElementById('ctxDeleteNode');
    const deleteEdge = document.getElementById('ctxDeleteEdge');

    // Store target for action
    ctxMenuTarget = { type, id, data };

    // Show appropriate option
    deleteNode.style.display = type === 'node' ? 'flex' : 'none';
    deleteEdge.style.display = type === 'edge' ? 'flex' : 'none';

    // Update text with target name
    if (type === 'node' && data) {
        deleteNode.innerHTML = `<span class="graph-context-icon">&#10006;</span> Delete "${data.label}"`;
    } else if (type === 'edge' && data && data.edgeData) {
        deleteEdge.innerHTML = `<span class="graph-context-icon">&#10006;</span> Delete relationship`;
    }

    // Position menu at cursor - use clientX/Y for fixed positioning
    const x = event.clientX || event.center?.x || 100;
    const y = event.clientY || event.center?.y || 100;
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    menu.style.display = 'block';

    console.log('[Graph] Context menu shown for', type, id, 'at', x, y);
}

function hideGraphContextMenu() {
    document.getElementById('graphContextMenu').style.display = 'none';
    ctxMenuTarget = null;
}

// Close context menu when clicking outside
document.addEventListener('click', function(e) {
    if (!e.target.closest('.graph-context-menu')) {
        hideGraphContextMenu();
    }
});

// Context menu actions - set up after DOM ready
function setupContextMenuHandlers() {
    const deleteNodeBtn = document.getElementById('ctxDeleteNode');
    const deleteEdgeBtn = document.getElementById('ctxDeleteEdge');

    if (deleteNodeBtn) {
        deleteNodeBtn.addEventListener('click', async function() {
            if (!ctxMenuTarget || ctxMenuTarget.type !== 'node') return;
            const nodeData = ctxMenuTarget.data;
            const npcId = document.getElementById('graphNpcSelect').value;

            const confirmed = confirm(
                `Delete "${nodeData.label}" and ALL its relationships?\n\n` +
                `This will permanently remove this entity and all facts connected to it.\n\n` +
                `This cannot be undone.`
            );

            if (confirmed) {
                hideGraphContextMenu();
                await deleteGraphNode(npcId, nodeData.label);
            }
        });
    }

    if (deleteEdgeBtn) {
        deleteEdgeBtn.addEventListener('click', async function() {
            if (!ctxMenuTarget || ctxMenuTarget.type !== 'edge') return;
            const edgeData = ctxMenuTarget.data.edgeData;
            const npcId = document.getElementById('graphNpcSelect').value;

            const confirmed = confirm(
                `Delete this relationship?\n\n` +
                `"${edgeData.fact}"\n\n` +
                `This cannot be undone.`
            );

            if (confirmed) {
                hideGraphContextMenu();
                await deleteGraphEdge(npcId, edgeData);
            }
        });
    }
}

// Initialize context menu handlers when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupContextMenuHandlers);
} else {
    setupContextMenuHandlers();
}

function highlightGraphNodes(nodeNames) {
    if (!graphNetwork) return;
    // Select the nodes in the graph
    graphNetwork.selectNodes(nodeNames, false);
}

function selectEdgeByNodes(source, target, fact) {
    if (!graphNetwork) return;

    // Find the edge index that matches source/target (and optionally fact)
    let edgeIndex = -1;
    for (let i = 0; i < graphEdgesData.length; i++) {
        const e = graphEdgesData[i];
        const matches = (e.source === source && e.target === target) ||
                       (e.source === target && e.target === source);
        if (matches) {
            // If fact provided, prefer exact match
            if (fact && e.fact === fact) {
                edgeIndex = i;
                break;
            } else if (!fact || edgeIndex === -1) {
                edgeIndex = i;
            }
        }
    }

    if (edgeIndex >= 0) {
        // Use setSelection to select both nodes and the edge - this triggers full highlighting
        graphNetwork.setSelection({
            nodes: [source, target],
            edges: [edgeIndex]
        }, { highlightEdges: true });
    }
}

function clearGraphHighlight() {
    if (!graphNetwork) return;
    graphNetwork.unselectAll();
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
            // Results now have {fact, source, target}
            resultsList.innerHTML = data.results.map((r, i) => {
                const fact = typeof r === 'string' ? r : r.fact;
                const source = typeof r === 'string' ? null : r.source;
                const target = typeof r === 'string' ? null : r.target;
                const escapedFact = fact ? fact.replace(/"/g, '&quot;') : '';
                const nodeData = source && target ? `data-source="${source}" data-target="${target}" data-fact="${escapedFact}"` : '';
                return `<div class="search-result-item" ${nodeData} style="padding: 4px 0; border-bottom: 1px solid var(--leather-border); cursor: pointer; ${i === data.results.length - 1 ? 'border-bottom: none;' : ''}">${fact}</div>`;
            }).join('');

            // Add hover handlers
            resultsList.querySelectorAll('.search-result-item').forEach(item => {
                item.addEventListener('mouseenter', () => {
                    const source = item.dataset.source;
                    const target = item.dataset.target;
                    if (source || target) {
                        highlightGraphNodes([source, target].filter(Boolean));
                    }
                });
                item.addEventListener('mouseleave', clearGraphHighlight);

                // Click to select edge and show details panel (same as clicking in graph)
                item.addEventListener('click', () => {
                    const source = item.dataset.source;
                    const target = item.dataset.target;
                    const fact = item.dataset.fact;
                    if (source && target) {
                        // Find matching edge in graph data - prefer exact fact match
                        let edge = graphEdgesData.find(e =>
                            e.fact === fact &&
                            ((e.source === source && e.target === target) ||
                             (e.source === target && e.target === source))
                        );
                        // Fallback to any edge between these nodes
                        if (!edge) {
                            edge = graphEdgesData.find(e =>
                                (e.source === source && e.target === target) ||
                                (e.source === target && e.target === source)
                            );
                        }
                        if (edge) {
                            showEdgeDetails(edge);
                            // Select edge + nodes with full highlighting (like clicking in graph)
                            selectEdgeByNodes(source, target, fact);
                        }
                    }
                });
            });

            showToast(`Found ${data.results.length} results`, 'success');
        } else if (data.success) {
            resultsList.innerHTML = '<em>No results found for this query.</em>';
        } else {
            resultsList.innerHTML = `<em style="color: var(--red-leather);">Error: ${data.error || 'Search failed'}</em>`;
        }
    } catch (e) {
        console.error('Search failed:', e);
        resultsList.innerHTML = `<em style="color: var(--red-leather);">Search failed: ${e.message}</em>`;
    }
}

async function clearNpcGraph() {
    const npcId = document.getElementById('graphNpcSelect').value;
    if (!npcId) return;

    const npcName = document.getElementById('graphNpcSelect').selectedOptions[0]?.textContent || npcId;
    const shortName = getShortNpcName(npcName);

    const confirmed = confirm(
        `Are you sure you want to clear ${shortName}'s memories?\n\n` +
        `This will delete their knowledge graph, chapters, and bio.\n` +
        `Dialogue history is preserved. You can re-migrate from the\n` +
        `Dialogue History section to regenerate memories.`
    );

    if (!confirmed) return;

    try {
        const response = await fetch(`/api/memories/graph/${encodeURIComponent(npcId)}`, {
            method: 'DELETE'
        });
        const data = await response.json();

        if (data.success) {
            showToast(`Cleared ${shortName}'s memories: ${data.nodes_deleted} entities, ${data.edges_deleted} relationships`, 'success');
        } else if (data.chapters_cleared) {
            showToast(`Cleared ${shortName}'s chapters (graph clear failed: ${data.error || 'unknown'})`, 'warning');
        } else {
            showToast(data.error || 'Clear failed', 'error');
        }

        // Always refresh UI if chapters were cleared (regardless of graph success)
        if (data.success || data.chapters_cleared) {
            loadNpcGraph();

            if (typeof loadMigrationStatus === 'function') {
                await loadMigrationStatus();
            }

            const historyPerspective = document.getElementById('historyPerspective');
            if (historyPerspective && historyPerspective.value === npcId) {
                if (typeof filterHistoryByPerspective === 'function') {
                    await filterHistoryByPerspective(false);
                }
            }
        }
    } catch (e) {
        console.error('Clear graph failed:', e);
        showToast('Clear failed', 'error');
    }
}

async function deleteGraphNode(npcId, nodeName) {
    try {
        const response = await fetch(`/api/memories/graph/${encodeURIComponent(npcId)}/node/${encodeURIComponent(nodeName)}`, {
            method: 'DELETE'
        });
        const data = await response.json();

        if (data.success) {
            showToast(`Deleted "${nodeName}" and ${data.edges_deleted} relationships`, 'success');
            loadNpcGraph(); // Reload graph
        } else {
            showToast(data.error || 'Delete failed', 'error');
        }
    } catch (e) {
        console.error('Delete node failed:', e);
        showToast('Delete failed', 'error');
    }
}

async function deleteGraphEdge(npcId, edgeData) {
    try {
        const response = await fetch(`/api/memories/graph/${encodeURIComponent(npcId)}/edge`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source: edgeData.source,
                target: edgeData.target,
                fact: edgeData.fact
            })
        });
        const data = await response.json();

        if (data.success) {
            showToast('Relationship deleted', 'success');
            loadNpcGraph(); // Reload graph
        } else {
            showToast(data.error || 'Delete failed', 'error');
        }
    } catch (e) {
        console.error('Delete edge failed:', e);
        showToast('Delete failed', 'error');
    }
}

// Store current edge context for deletion
let currentEdgeContext = null;

function showEdgeDetails(edge) {
    const panel = document.getElementById('edgeDetailsPanel');
    document.getElementById('edgeDetailTitle').textContent = `${edge.source} \u2192 ${edge.target}`;

    // Find ALL edges between these two nodes (may have multiple facts)
    const relatedEdges = graphEdgesData.filter(e =>
        (e.source === edge.source && e.target === edge.target) ||
        (e.source === edge.target && e.target === edge.source)
    );

    currentEdgeContext = { source: edge.source, target: edge.target, edges: relatedEdges };

    if (relatedEdges.length > 1) {
        // Multiple facts - show as list with delete buttons
        const factsHtml = relatedEdges.map((e, i) => {
            let meta = [];
            if (e.chapters?.length > 0) meta.push(e.chapters.join(', '));
            // Only show game date if it's actually from the 1800s
            if (e.valid_at) {
                const year = new Date(e.valid_at).getFullYear();
                if (year < 2000) {
                    meta.push(new Date(e.valid_at).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }));
                }
            }
            const metaStr = meta.length > 0 ? ` <span style="opacity:0.6; font-size:0.85em">(${meta.join(' · ')})</span>` : '';
            return `<div style="display:flex; gap:8px; align-items:flex-start; margin-bottom:8px;">
                <button onclick="deleteSpecificFact(${i})" title="Delete this fact" style="background:none; border:none; color:var(--ember-red); cursor:pointer; padding:0; font-size:1.1em; line-height:1;">&times;</button>
                <span>${e.fact}${metaStr}</span>
            </div>`;
        }).join('');
        document.getElementById('edgeDetailFact').innerHTML = factsHtml;
        document.getElementById('edgeDetailChapters').textContent = '';
        document.getElementById('edgeDetailTime').textContent = `${relatedEdges.length} facts between these entities`;
    } else {
        // Single fact - show with delete option
        const deleteBtn = `<button onclick="deleteSpecificFact(0)" title="Delete this fact" style="background:none; border:none; color:var(--ember-red); cursor:pointer; padding:0; font-size:1.1em; margin-right:6px;">&times;</button>`;
        document.getElementById('edgeDetailFact').innerHTML = deleteBtn + (edge.fact || 'No details available');

        const chapters = edge.chapters || [];
        document.getElementById('edgeDetailChapters').textContent = chapters.length > 0
            ? `Chapters: ${chapters.join(', ')}`
            : '';

        // valid_at = game time when fact became true (if captured)
        const validDate = edge.valid_at ? new Date(edge.valid_at) : null;
        let timeText = '';
        if (validDate) {
            const year = validDate.getFullYear();
            // Only show if it's actually a game date (1800s), not a fallback to current year
            if (year < 2000) {
                const gameDate = validDate.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
                timeText = `Game date: ${gameDate}`;
            }
        }
        document.getElementById('edgeDetailTime').textContent = timeText;
    }

    panel.style.display = 'block';
}

function showNodeFacts(nodeId) {
    const connectedEdges = graphEdgesData.filter(e =>
        e.source === nodeId || e.target === nodeId
    );

    if (connectedEdges.length === 0) {
        document.getElementById('edgeDetailsPanel').style.display = 'none';
        return;
    }

    // If all edges are between the same pair, use showEdgeDetails
    const counterparts = new Set(connectedEdges.map(e =>
        e.source === nodeId ? e.target : e.source
    ));

    if (counterparts.size === 1) {
        showEdgeDetails(connectedEdges[0]);
        return;
    }

    // Multiple counterparts - show node-centric view
    currentEdgeContext = { source: nodeId, target: null, edges: connectedEdges };

    const panel = document.getElementById('edgeDetailsPanel');
    document.getElementById('edgeDetailTitle').textContent = nodeId;

    const factsHtml = connectedEdges.map((e, i) => {
        const other = e.source === nodeId ? e.target : e.source;
        let meta = [];
        if (e.chapters?.length > 0) meta.push(e.chapters.join(', '));
        if (e.valid_at) {
            const year = new Date(e.valid_at).getFullYear();
            if (year < 2000) {
                meta.push(new Date(e.valid_at).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }));
            }
        }
        const metaStr = meta.length > 0 ? ` <span style="opacity:0.6; font-size:0.85em">(${meta.join(' · ')})</span>` : '';
        return `<div style="display:flex; gap:8px; align-items:flex-start; margin-bottom:8px;">
            <button onclick="deleteSpecificFact(${i})" title="Delete this fact" style="background:none; border:none; color:var(--ember-red); cursor:pointer; padding:0; font-size:1.1em; line-height:1;">&times;</button>
            <span><strong style="opacity:0.7">${other}:</strong> ${e.fact}${metaStr}</span>
        </div>`;
    }).join('');

    document.getElementById('edgeDetailFact').innerHTML = factsHtml;
    document.getElementById('edgeDetailChapters').textContent = '';
    document.getElementById('edgeDetailTime').textContent = `${connectedEdges.length} facts about ${nodeId}`;

    panel.style.display = 'block';
}

async function deleteSpecificFact(index) {
    if (!currentEdgeContext || !currentEdgeContext.edges[index]) return;

    const edge = currentEdgeContext.edges[index];
    const npcId = document.getElementById('graphNpcSelect').value;

    const confirmed = confirm(`Delete this fact?\n\n"${edge.fact}"\n\nThis cannot be undone.`);
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/memories/graph/${encodeURIComponent(npcId)}/edge`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source: edge.source,
                target: edge.target,
                fact: edge.fact
            })
        });
        const data = await response.json();

        if (data.success) {
            showToast('Fact deleted', 'success');
            loadNpcGraph(); // Reload graph
        } else {
            showToast(data.error || 'Delete failed', 'error');
        }
    } catch (e) {
        console.error('Delete fact failed:', e);
        showToast('Delete failed', 'error');
    }
}

// Load NPC list on page load (after config loads)
document.addEventListener('DOMContentLoaded', function() {
    // Delay to let config load first
    setTimeout(refreshNpcList, 500);
});
