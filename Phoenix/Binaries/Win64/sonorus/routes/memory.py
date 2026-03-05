"""
Memory API endpoints for Sonorus NPC memory system.

Handles knowledge graph operations, chapter management, and memory migration.
"""

import os
import shutil

import json
import queue
import threading

from flask import Blueprint, request, jsonify, Response

from utils.settings import DATA_DIR
from utils.localization import get_display_name

memory_bp = Blueprint('memory', __name__)


# ============================================
# Memory API Endpoints
# ============================================

@memory_bp.route('/api/memories', methods=['DELETE'])
def clear_all_memories():
    """Clear all NPC long-term memories (Kuzu graph and chapter data)."""
    try:
        from utils.memory import reset_memory_connection
        from utils.memory_queue import reset_all_state

        # Reset the Graphiti connection first (releases file handles)
        reset_memory_connection()

        # Delete Kuzu database files
        for kuzu_file in ('memory.kuzu', 'memory.kuzu.wal'):
            kuzu_path = os.path.join(DATA_DIR, kuzu_file)
            if os.path.exists(kuzu_path):
                os.remove(kuzu_path)
                print(f"[Memory] Deleted {kuzu_file}")

        # Delete chapters directory (local state)
        chapters_dir = os.path.join(DATA_DIR, 'chapters')
        if os.path.exists(chapters_dir):
            shutil.rmtree(chapters_dir)
            print(f"[Memory] Deleted chapters directory: {chapters_dir}")

        # Reset queue checkpoints so entries are reprocessed from the beginning
        reset_all_state()

        return jsonify({"success": True, "message": "All memories cleared"})
    except Exception as e:
        print(f"[Memory] Error clearing memories: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/migrate', methods=['POST'])
def migrate_memories():
    """Migrate existing dialogue history into knowledge graph."""
    try:
        from utils.memory import migrate_all_npcs, is_memory_available
        from utils.dialogue import load_dialogue_history

        if not is_memory_available():
            return jsonify({"success": False, "error": "Memory system not available. Install graphiti-core[kuzu] package."}), 400

        # Load full dialogue history
        dialogue_history = load_dialogue_history(lambda: {})

        if not dialogue_history:
            return jsonify({"success": False, "error": "No dialogue history to migrate"}), 400

        print(f"[Migration] Starting migration of {len(dialogue_history)} total entries")

        # Run migration for all NPCs
        results = migrate_all_npcs(dialogue_history)

        # Summarize results
        total_chapters = sum(r.get('chapters_created', 0) for r in results.values() if isinstance(r, dict))
        total_episodes = sum(r.get('episodes_added', 0) for r in results.values() if isinstance(r, dict))
        total_errors = sum(len(r.get('errors', [])) for r in results.values() if isinstance(r, dict))
        npcs_processed = len([r for r in results.values() if isinstance(r, dict) and not r.get('error') and not r.get('skipped')])
        npcs_skipped = len([r for r in results.values() if isinstance(r, dict) and r.get('skipped')])

        return jsonify({
            "success": True,
            "npcs_processed": npcs_processed,
            "npcs_skipped": npcs_skipped,
            "chapters_created": total_chapters,
            "episodes_added": total_episodes,
            "errors": total_errors,
            "details": results
        })

    except Exception as e:
        print(f"[Migration] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/migrate/<npc_id>', methods=['POST'])
def migrate_npc_memories(npc_id):
    """Migrate dialogue history for a specific NPC."""
    try:
        from utils.memory import migrate_npc_history, is_memory_available
        from utils.dialogue import load_dialogue_history

        if not is_memory_available():
            return jsonify({"success": False, "error": "Memory system not available"}), 400

        dialogue_history = load_dialogue_history(lambda: {})
        npc_name = get_display_name(npc_id)

        print(f"[Migration] Migrating {npc_name} ({npc_id})")
        result = migrate_npc_history(npc_id, npc_name, dialogue_history)

        if result.get('error'):
            return jsonify({"success": False, "error": result['error']}), 400

        if result.get('skipped'):
            return jsonify({
                "success": True,
                "skipped": True,
                "npc_id": npc_id,
                "npc_name": npc_name,
                "reason": result.get('reason', 'Already migrated')
            })

        return jsonify({
            "success": True,
            "npc_id": npc_id,
            "npc_name": npc_name,
            **result
        })

    except Exception as e:
        print(f"[Migration] Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/migrate/stream', methods=['GET'])
def migrate_memories_stream():
    """
    Stream migration progress via Server-Sent Events.

    Event types:
    - start: Migration started, includes total NPC count
    - npc_start: Started processing an NPC
    - npc_done: Finished processing an NPC (with chapters/episodes count)
    - complete: Migration finished
    - error: An error occurred

    Note: Detailed progress phases (detecting/generating/indexing) are only shown
    for individual NPC migration, not bulk "Migrate All" to keep it performant.
    """
    def generate():
        try:
            from utils.memory import migrate_npc_history, is_memory_available
            from utils.dialogue import load_dialogue_history

            if not is_memory_available():
                yield f"data: {json.dumps({'type': 'error', 'message': 'Memory system not available'})}\n\n"
                return

            # Load dialogue history
            dialogue_history = load_dialogue_history(lambda: {})
            if not dialogue_history:
                yield f"data: {json.dumps({'type': 'error', 'message': 'No dialogue history to migrate'})}\n\n"
                return

            # Find all unique significant NPCs in history and count entries
            from utils.settings import load_settings
            from utils.text_utils import is_significant_npc
            settings = load_settings()
            min_entries = settings.get('memory', {}).get('chapter_entry_threshold', 30)

            npc_entry_counts = {}
            for entry in dialogue_history:
                voice_name = entry.get('voiceName')
                if voice_name and voice_name.lower() != 'player' and is_significant_npc(voice_name):
                    npc_entry_counts[voice_name] = npc_entry_counts.get(voice_name, 0) + 1
                for char_id in entry.get('earshot', []):
                    if char_id.lower() != 'player' and is_significant_npc(char_id):
                        npc_entry_counts[char_id] = npc_entry_counts.get(char_id, 0) + 1

            # Filter to NPCs meeting minimum entry threshold
            npc_ids = {npc_id for npc_id, count in npc_entry_counts.items() if count >= min_entries}

            total_npcs = len(npc_ids)
            yield f"data: {json.dumps({'type': 'start', 'total_npcs': total_npcs})}\n\n"

            results = {}
            processed = 0
            skipped = 0

            for i, npc_id in enumerate(npc_ids):
                npc_name = get_display_name(npc_id)

                yield f"data: {json.dumps({'type': 'npc_start', 'npc_id': npc_id, 'npc_name': npc_name, 'index': i + 1, 'total': total_npcs})}\n\n"

                # Run migration for this NPC (no detailed progress callback for bulk migration)
                result = migrate_npc_history(npc_id, npc_name, dialogue_history)
                results[npc_id] = result

                if result.get('skipped'):
                    skipped += 1
                    yield f"data: {json.dumps({'type': 'npc_done', 'npc_id': npc_id, 'npc_name': npc_name, 'skipped': True, 'reason': result.get('reason', 'Already migrated')})}\n\n"
                elif result.get('error'):
                    yield f"data: {json.dumps({'type': 'npc_done', 'npc_id': npc_id, 'npc_name': npc_name, 'error': result.get('error')})}\n\n"
                else:
                    processed += 1
                    yield f"data: {json.dumps({'type': 'npc_done', 'npc_id': npc_id, 'npc_name': npc_name, 'chapters': result.get('chapters_created', 0), 'episodes': result.get('episodes_added', 0)})}\n\n"

            # Summary
            total_chapters = sum(r.get('chapters_created', 0) for r in results.values() if isinstance(r, dict))
            total_episodes = sum(r.get('episodes_added', 0) for r in results.values() if isinstance(r, dict))

            yield f"data: {json.dumps({'type': 'complete', 'processed': processed, 'skipped': skipped, 'chapters': total_chapters, 'episodes': total_episodes})}\n\n"

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'  # Disable nginx buffering
        }
    )


@memory_bp.route('/api/memories/migrate/<npc_id>/stream', methods=['GET'])
def migrate_npc_stream(npc_id):
    """Stream migration progress for a single NPC via Server-Sent Events."""
    def generate():
        try:
            from utils.memory import migrate_npc_history, is_memory_available
            from utils.dialogue import load_dialogue_history

            if not is_memory_available():
                yield f"data: {json.dumps({'type': 'error', 'message': 'Memory system not available'})}\n\n"
                return

            npc_name = get_display_name(npc_id)
            yield f"data: {json.dumps({'type': 'start', 'npc_id': npc_id, 'npc_name': npc_name})}\n\n"

            dialogue_history = load_dialogue_history(lambda: {})

            # Progress queue for inter-thread communication
            progress_queue = queue.Queue()

            def progress_callback(current, total, message):
                progress_queue.put({'current': current, 'total': total, 'message': message})

            # Run migration in background thread
            result_holder = [None]
            def run_migration():
                result_holder[0] = migrate_npc_history(
                    npc_id, npc_name, dialogue_history,
                    progress_callback=progress_callback
                )
                progress_queue.put(None)  # Signal completion

            thread = threading.Thread(target=run_migration)
            thread.start()

            # Yield progress updates as they come
            while True:
                try:
                    progress = progress_queue.get(timeout=0.5)
                    if progress is None:
                        break
                    yield f"data: {json.dumps({'type': 'progress', **progress})}\n\n"
                except queue.Empty:
                    # Send keepalive
                    yield f": keepalive\n\n"

            thread.join()
            result = result_holder[0]

            if result.get('skipped'):
                yield f"data: {json.dumps({'type': 'complete', 'skipped': True, 'reason': result.get('reason')})}\n\n"
            elif result.get('error'):
                yield f"data: {json.dumps({'type': 'error', 'message': result.get('error')})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'complete', 'chapters': result.get('chapters_created', 0), 'episodes': result.get('episodes_added', 0)})}\n\n"

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@memory_bp.route('/api/memories/chapters/<npc_id>', methods=['GET'])
def get_npc_chapters(npc_id):
    """Get chapter data for an NPC (for displaying chapter dividers in dialog history)."""
    try:
        from utils.memory import ChapterManager

        chapter_mgr = ChapterManager()
        chapters = chapter_mgr.get_all_chapters(npc_id)

        return jsonify({
            "success": True,
            "npc_id": npc_id,
            "open_chapter": chapters.get('open_chapter'),
            "closed_chapters": chapters.get('closed_chapters', [])
        })

    except Exception as e:
        print(f"[Memory] Error getting chapters for {npc_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/npcs', methods=['GET'])
def list_memory_npcs():
    """List all NPCs that have memory data (chapters directory)."""
    try:
        from utils.memory import get_chapters_dir

        npcs = []

        # Check chapters directory for NPCs with chapter files
        # Graph data is built from chapters, so if they have chapters they may have graph data
        chapters_dir = get_chapters_dir()
        if os.path.exists(chapters_dir):
            for filename in os.listdir(chapters_dir):
                if filename.endswith('.json') and not filename.endswith('_memory.txt'):
                    npc_id = filename[:-5]  # Remove .json
                    npcs.append({
                        "npc_id": npc_id,
                        "npc_name": get_display_name(npc_id),
                        "has_chapters": True
                    })

        # Sort by display name
        npcs.sort(key=lambda x: x["npc_name"])

        return jsonify({
            "success": True,
            "npcs": npcs
        })

    except Exception as e:
        print(f"[Memory] Error listing NPCs: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/graph/<npc_id>', methods=['GET'])
def get_npc_graph(npc_id):
    """Get raw graph data for an NPC (deterministic - no LLM call)."""
    try:
        from utils.memory import GraphitiManager, ChapterManager

        graphiti_mgr = GraphitiManager()
        if not graphiti_mgr.init_graphiti():
            return jsonify({"success": False, "error": "Memory system not connected"}), 503

        graph_data = graphiti_mgr.get_graph_data(npc_id)

        # Also get chapter info and cached memory
        chapter_mgr = ChapterManager()
        cached_memory = chapter_mgr.get_cached_memory(npc_id)

        return jsonify({
            "success": True,
            "npc_id": npc_id,
            "npc_name": get_display_name(npc_id),
            "entity_count": graph_data.get('entity_count', 0),
            "edges": graph_data.get('edges', []),
            "cached_memory": cached_memory
        })

    except Exception as e:
        print(f"[Memory] Error getting graph for {npc_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/prose/<npc_id>', methods=['POST'])
def generate_npc_prose(npc_id):
    """Generate (or regenerate) memory prose for an NPC (uses LLM)."""
    try:
        from utils.memory import ChapterManager, get_npc_memory

        # Check if we should force regeneration
        force = request.json.get('force', False) if request.is_json else False

        chapter_mgr = ChapterManager()
        if force:
            chapter_mgr.invalidate_memory_cache(npc_id)

        npc_name = get_display_name(npc_id)
        prose = get_npc_memory(npc_id, npc_name)

        return jsonify({
            "success": True,
            "npc_id": npc_id,
            "npc_name": npc_name,
            "prose": prose,
            "regenerated": force
        })

    except Exception as e:
        print(f"[Memory] Error generating prose for {npc_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/graph/<npc_id>', methods=['DELETE'])
def clear_npc_graph(npc_id):
    """Delete all nodes and edges for an NPC's graph, plus chapter data."""
    try:
        from utils.memory import GraphitiManager, ChapterManager
        from utils.memory_queue import reset_npc_state

        # Always clear chapter data and queue state so the NPC can be re-migrated,
        # even if graph clear fails (e.g. corrupted Kuzu DB)
        chapter_mgr = ChapterManager()
        chapter_mgr.clear_npc_data(npc_id)
        reset_npc_state(npc_id)

        # Attempt to clear the graph (may fail if Kuzu DB is corrupted/missing)
        graph_result = {"success": False, "error": "Graph not initialized"}
        graphiti_mgr = GraphitiManager()
        if graphiti_mgr.init_graphiti():
            graph_result = graphiti_mgr.clear_graph(npc_id)
        else:
            print(f"[Memory] Could not init Graphiti for {npc_id} graph clear - chapters/state already cleared")

        if graph_result.get("success"):
            print(f"[Memory] Cleared graph + chapters for {npc_id}")
            return jsonify(graph_result)
        else:
            # Graph clear failed but chapters/state were still cleared
            print(f"[Memory] Graph clear failed for {npc_id} but chapters/state cleared: {graph_result.get('error')}")
            graph_result["chapters_cleared"] = True
            return jsonify(graph_result)

    except Exception as e:
        print(f"[Memory] Error clearing graph for {npc_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/graph/<npc_id>/node/<node_name>', methods=['DELETE'])
def delete_graph_node(npc_id, node_name):
    """Delete a node and all its edges from the NPC's graph."""
    try:
        from utils.memory import GraphitiManager

        graphiti_mgr = GraphitiManager()
        if not graphiti_mgr.init_graphiti():
            return jsonify({"success": False, "error": "Memory system not connected"}), 503

        result = graphiti_mgr.delete_node(npc_id, node_name)

        if result.get("success"):
            print(f"[Memory] Deleted node '{node_name}' and {result.get('edges_deleted', 0)} edges for {npc_id}")
            return jsonify(result)
        else:
            return jsonify(result), 400

    except Exception as e:
        print(f"[Memory] Error deleting node {node_name} for {npc_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/graph/<npc_id>/edge', methods=['DELETE'])
def delete_graph_edge(npc_id):
    """Delete a specific edge from the NPC's graph."""
    try:
        from utils.memory import GraphitiManager

        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Missing request body"}), 400

        source = data.get('source')
        target = data.get('target')
        fact = data.get('fact')

        if not source or not target:
            return jsonify({"success": False, "error": "Missing source or target"}), 400

        graphiti_mgr = GraphitiManager()
        if not graphiti_mgr.init_graphiti():
            return jsonify({"success": False, "error": "Memory system not connected"}), 503

        result = graphiti_mgr.delete_edge(npc_id, source, target, fact)

        if result.get("success"):
            print(f"[Memory] Deleted edge {source} -> {target} for {npc_id}")
            return jsonify(result)
        else:
            return jsonify(result), 400

    except Exception as e:
        print(f"[Memory] Error deleting edge for {npc_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/search/<npc_id>', methods=['POST'])
def search_npc_memory(npc_id):
    """Search an NPC's memory graph with a query (raw, no LLM processing)."""
    try:
        from utils.memory import GraphitiManager

        data = request.get_json() or {}
        query = data.get('query', '').strip()

        if not query:
            return jsonify({"success": False, "error": "Query is required"}), 400

        graphiti_mgr = GraphitiManager()
        if not graphiti_mgr.init_graphiti():
            return jsonify({"success": False, "error": "Memory system not connected"}), 503

        # Direct search - no LLM intent extraction
        facts = graphiti_mgr.search_facts(
            npc_id=npc_id,
            query=query,
            max_results=50
        )

        return jsonify({
            "success": True,
            "query": query,
            "npc_id": npc_id,
            "results": facts or []
        })

    except Exception as e:
        print(f"[Memory] Search error for {npc_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/migration-status', methods=['GET'])
def get_migration_status():
    """Get migration status for all NPCs with dialogue history."""
    try:
        from utils.memory import ChapterManager, get_chapters_dir
        from utils.dialogue import load_dialogue_history
        from utils.settings import load_settings

        # Load settings to get minimum entry threshold
        settings = load_settings()
        min_entries = settings.get('memory', {}).get('chapter_entry_threshold', 30)

        # Load dialogue history to find NPCs
        dialogue_history = load_dialogue_history(lambda: {})

        # Count entries per significant NPC (speaker or in earshot)
        from utils.text_utils import is_significant_npc
        npc_entry_counts = {}
        for entry in dialogue_history:
            voice_name = entry.get('voiceName')
            if voice_name and voice_name.lower() != 'player' and is_significant_npc(voice_name):
                npc_entry_counts[voice_name] = npc_entry_counts.get(voice_name, 0) + 1
            for char_id in entry.get('earshot', []):
                if char_id.lower() != 'player' and is_significant_npc(char_id):
                    npc_entry_counts[char_id] = npc_entry_counts.get(char_id, 0) + 1

        # Filter NPCs that meet minimum entry threshold
        npc_ids = {npc_id for npc_id, count in npc_entry_counts.items() if count >= min_entries}

        # Check which NPCs have chapters (already migrated)
        chapters_dir = get_chapters_dir()
        migrated_npcs = set()
        pending_npcs = []

        for npc_id in npc_ids:
            chapter_file = os.path.join(chapters_dir, f"{npc_id}.json")
            if os.path.exists(chapter_file):
                # Check if file has actual content (not just empty/default)
                try:
                    import json
                    with open(chapter_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        # Consider migrated if has closed chapters or open chapter
                        if data.get('closed_chapters') or data.get('open_chapter'):
                            migrated_npcs.add(npc_id)
                            continue
                except:
                    pass

            # Not migrated - add to pending list
            pending_npcs.append({
                "npc_id": npc_id,
                "npc_name": get_display_name(npc_id),
                "entry_count": npc_entry_counts.get(npc_id, 0)
            })

        # Sort pending by display name
        pending_npcs.sort(key=lambda x: x["npc_name"])

        return jsonify({
            "success": True,
            "total_npcs": len(npc_ids),
            "migrated_count": len(migrated_npcs),
            "pending_count": len(pending_npcs),
            "pending_npcs": pending_npcs,
            "min_entries_threshold": min_entries
        })

    except Exception as e:
        print(f"[Memory] Error getting migration status: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/npcs-with-bios', methods=['GET'])
def list_npcs_with_bios():
    """List all NPCs that have generated bios."""
    try:
        bios_dir = os.path.join(DATA_DIR, 'npc_bios')
        if not os.path.exists(bios_dir):
            return jsonify({"success": True, "npcs": []})

        npc_ids = []
        for filename in os.listdir(bios_dir):
            if filename.endswith('.json'):
                npc_id = filename[:-5]  # Remove .json extension
                npc_ids.append(npc_id)

        return jsonify({
            "success": True,
            "npcs": sorted(npc_ids)
        })

    except Exception as e:
        print(f"[Memory] Error listing NPCs with bios: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/bio/<npc_id>', methods=['GET'])
def get_npc_bio(npc_id):
    """Get the stored bio for an NPC."""
    try:
        from utils.memory import load_bio, format_bio_for_context

        bio = load_bio(npc_id)
        if bio:
            formatted = format_bio_for_context(bio)
            return jsonify({
                "success": True,
                "npc_id": npc_id,
                "npc_name": get_display_name(npc_id),
                "bio": bio,
                "formatted": formatted,
                "last_updated": bio.get('last_updated')
            })
        return jsonify({
            "success": True,
            "npc_id": npc_id,
            "npc_name": get_display_name(npc_id),
            "bio": None,
            "formatted": None,
            "last_updated": None
        })

    except Exception as e:
        print(f"[Memory] Error getting bio for {npc_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@memory_bp.route('/api/memories/bio/<npc_id>/regenerate', methods=['POST'])
def regenerate_npc_bio(npc_id):
    """Regenerate bio from graph data, incorporating editor's guidance."""
    try:
        from utils.memory import generate_full_bio
        from utils.settings import load_settings

        # Get editor's guidance from settings
        settings = load_settings()
        guidance = settings.get('prompts', {}).get('editor_guidance', {}).get(npc_id, '')

        npc_name = get_display_name(npc_id)

        print(f"[Bio] Regenerating bio for {npc_name} (guidance: {'yes' if guidance else 'no'})")

        bio = generate_full_bio(npc_id, npc_name, editor_guidance=guidance)

        if bio:
            from utils.memory import format_bio_for_context
            formatted = format_bio_for_context(bio)
            return jsonify({
                "success": True,
                "npc_id": npc_id,
                "npc_name": npc_name,
                "bio": bio,
                "formatted": formatted,
                "last_updated": bio.get('last_updated')
            })
        return jsonify({
            "success": False,
            "error": "Failed to generate bio - no graph data or LLM error"
        }), 400

    except Exception as e:
        print(f"[Memory] Error regenerating bio for {npc_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
