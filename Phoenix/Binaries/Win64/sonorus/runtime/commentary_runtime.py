"""Commentary-specific generation and playback orchestration."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .streaming_playback import build_live_sentence_stream, start_streaming_playback_session

from utils import (
    call_llm,
    call_llm_stream,
    build_character_guidance_sections,
    filter_dialogue_history,
    format_dialogue_history,
    format_game_context,
    get_character,
    get_display_name,
    get_speech_rules,
    get_world_lore_block,
    load_dialogue_history,
    load_settings,
    remove_unpaired_double_quotes,
    strip_action_tag,
)
from utils.dialogue import format_dialogue_as_messages
from utils.game_context import format_static_context, format_dynamic_context
from utils.llm_utils import call_llm_messages, strip_response_metadata


def _format_history_output_reminder(*, has_vision_context: bool = False) -> str:
    conversation = load_settings().get("conversation", {})
    if conversation.get("narration_enabled", False):
        reminder = (
            'Remember: the timestamped history lines are context, not part of your response. '
            'Do not prefix your response with timestamps, your name, or "(to ...)".'
        )
        if conversation.get("spatial_grounding_enabled", True) and has_vision_context:
            reminder += (
                ' When visual context gives a character\'s current location, keep any narration spatially consistent with that '
                'location. You may invent fitting actions, but do not relocate the character to a different part of the scene.'
            )
        return reminder
    return (
        'Remember: the timestamped history lines are context, not an output format. '
        'Respond with dialogue only. Do not prefix your response with timestamps, your name, or "(to ...)".'
    )


def build_commentary_request(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    *,
    topic: Optional[str] = None,
    recent_events: Optional[list] = None,
) -> Dict[str, Any]:
    """Build three-layer cached message structure for commentary generation."""
    settings = load_settings()
    player_name = game_context.get("playerName", "Unknown")
    target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name

    speaker_name, base_prompt = get_character(speaker_id, game_context, speaking_to=target_name)
    participants = [player_name, target_name] if player_name and player_name != "Unknown" else [target_name]

    # Layer 1: Static system message (cacheable across turns)
    static_ctx = format_static_context(game_context, current_speaker=speaker_id)
    system_parts = [base_prompt]
    if static_ctx:
        system_parts.append(static_ctx)

    # Layer 2: User/assistant message pairs from dialogue history
    dialogue_history = load_dialogue_history(game_context)
    y, m, d = game_context.get("year"), game_context.get("month"), game_context.get("day")
    current_game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get("dateFormatted", "")
    current_game_time = game_context.get("timeFormatted", "") or game_context.get("time", "")
    history_messages, event_entries = format_dialogue_as_messages(
        dialogue_history,
        for_npc_id=speaker_id,
        current_game_date=current_game_date,
    )

    # Memory: contextual → system message (stable), search → user message (dynamic)
    memory_search_results = None
    if settings.get("memory", {}).get("enabled", True):
        try:
            from utils.memory import get_contextual_memory, search_relevant_facts

            current_location = game_context.get("locationName") or game_context.get("zoneLocation") or game_context.get("location", "")
            memory_block = get_contextual_memory(
                npc_id=speaker_id,
                npc_name=speaker_name,
                player_name=player_name,
                current_location=current_location,
                nearby_npcs=[target_name] if target_id.lower() != "player" else [],
                mentioned_entities=[],
            )
            if memory_block:
                system_parts.append(memory_block)
                print(f"[Commentary] Injected contextual memory (~{len(memory_block)//4} tokens)")

            last_entry_text = dialogue_history[-1].get("text", "") if dialogue_history else ""
            if last_entry_text and last_entry_text.strip():
                print(f"[Commentary] Searching memories based on: '{last_entry_text[:60]}...'")
                search_start = time.time()
                relevant_facts = search_relevant_facts(
                    npc_id=speaker_id,
                    query=last_entry_text,
                    npc_name=speaker_name,
                    player_name=player_name,
                    current_game_date=current_game_date,
                    current_game_time=current_game_time,
                )
                search_elapsed = (time.time() - search_start) * 1000
                if relevant_facts:
                    memory_search_results = "### Relevant Memories\n" + "\n".join(f"- {fact}" for fact in relevant_facts)
                    print(f"[Commentary] Added {len(relevant_facts)} relevant facts (~{len(memory_search_results)//4} tokens) in {search_elapsed:.0f}ms")
                else:
                    print(f"[Commentary] Memory search returned no results ({search_elapsed:.0f}ms)")
        except Exception as e:
            print(f"[Commentary] Memory error: {e}")
    else:
        print("[Commentary] Long-term memory disabled; skipping contextual memory/search")

    # Commitment actions → system message (stable)
    if settings.get("commitments", {}).get("enabled", False):
        try:
            from utils.commitments import build_commitment_action_instructions

            commitment_parts = build_commitment_action_instructions(player_name, npc_id=speaker_id)
            if commitment_parts:
                action_header = "**Actions:** You may optionally include an action at the END of your response using `[Action: X]` format. Most responses need no action."
                system_parts.append(f"{action_header}\n" + "\n".join(commitment_parts))
        except Exception as e:
            print(f"[Commentary] Error adding commitment actions: {e}")

    system_message = "\n\n".join(system_parts)

    # Layer 3: Final user message with dynamic context
    dynamic_ctx = format_dynamic_context(
        game_context, current_speaker=speaker_id,
        participants=participants,
        event_entries=event_entries,
    )

    commentary_lines = []
    for event_summary in recent_events or []:
        commentary_lines.append(f"- {event_summary}")
    if topic:
        commentary_lines.append(f"Topic hook: {topic}")
    commentary_lines.append(
        f"This is an unprompted comment to {player_name} after a recent event. No conversation is currently active. "
        f"Any prior dialogue is context for what just happened — do NOT address or continue speaking to anyone else mentioned. "
        f"You are turning to {player_name} to share your reaction."
    )

    user_instruction = (
        f"(You are making a brief unprompted remark as {speaker_name}. "
        f"You are turning to speak directly to {target_name} — not to anyone else present or mentioned. "
        f"React to the recent moment naturally in 1-2 sentences.)"
    )

    final_user_parts = [
        "### Recent Event Context\n" + "\n".join(commentary_lines),
        user_instruction,
    ]
    if dynamic_ctx:
        final_user_parts.append("---")
        final_user_parts.append(dynamic_ctx)
    if memory_search_results:
        final_user_parts.append(memory_search_results)
    final_user_parts.append(_format_history_output_reminder(
        has_vision_context=bool(dynamic_ctx and "**What you can see:**" in dynamic_ctx)
    ))
    final_user_message = "\n\n".join(final_user_parts)

    # Assemble three-layer messages array
    messages = [{"role": "system", "content": system_message}]
    messages.extend(history_messages)
    messages.append({"role": "user", "content": final_user_message})

    return {
        "messages": messages,
        "speaker_name": speaker_name,
        "target_name": target_name,
        "player_name": player_name,
    }


def generate_commentary_response(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    *,
    topic: Optional[str] = None,
    recent_events: Optional[list] = None,
    lua_socket: Any = None,
) -> Optional[str]:
    """Blocking commentary generator used for fallback compatibility."""
    request = build_commentary_request(
        speaker_id,
        target_id,
        game_context,
        topic=topic,
        recent_events=recent_events,
    )

    raw_response = call_llm_messages(request["messages"], speaker_id=speaker_id)
    if raw_response is None:
        return None

    response = strip_response_metadata(strip_action_tag(raw_response))
    print(f"[Commentary] {speaker_id} response: {response}")
    return response


def build_follow_up_request(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    *,
    last_question_text: str,
) -> Dict[str, str]:
    """Build prompt/user input for a delayed post-question follow-up."""
    player_name = game_context.get("playerName", "Unknown")
    target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name

    speaker_name, base_prompt = get_character(speaker_id, game_context, speaking_to=target_name)
    prompt = base_prompt
    context_str = format_game_context(
        game_context,
        current_speaker=speaker_id,
        participants=[player_name, target_name],
        observer_mode=False,
    )
    if context_str:
        prompt = f"{base_prompt}\n\n{context_str}"

    dialogue_history = load_dialogue_history(game_context)
    y, m, d = game_context.get("year"), game_context.get("month"), game_context.get("day")
    current_game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get("dateFormatted", "")
    dialogue_str = format_dialogue_history(
        dialogue_history,
        for_npc_id=speaker_id,
        current_game_date=current_game_date,
    )
    if dialogue_str:
        prompt = f"{prompt}\n\n{dialogue_str}"

    prompt = (
        f"{prompt}\n\n### Follow-Up Context\n"
        f"You asked {player_name} a question a moment ago and they have gone quiet.\n"
        f"Your last question was: \"{last_question_text.strip()}\"\n"
        f"Give one short in-character follow-up line to check whether {player_name} is still there.\n"
        f"Do not start a longer conversation. Do not involve other NPCs."
    )

    user_input = (
        f"(You are making a brief follow-up as {speaker_name}. "
        f"You are speaking to {target_name}. Check whether they are still there in one short line.)"
    )

    return {
        "prompt": prompt,
        "user_input": user_input,
        "speaker_name": speaker_name,
        "target_name": target_name,
        "player_name": player_name,
    }


def generate_follow_up_response(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    *,
    last_question_text: str,
    lua_socket: Any = None,
) -> Optional[str]:
    """Blocking follow-up generator used for fallback compatibility."""
    request = build_follow_up_request(
        speaker_id,
        target_id,
        game_context,
        last_question_text=last_question_text,
    )

    raw_response = call_llm(request["prompt"], request["user_input"], speaker_id=speaker_id)
    if raw_response is None:
        return None

    response = strip_response_metadata(strip_action_tag(raw_response))
    print(f"[FollowUp] {speaker_id} response: {response}")
    return response


def _entry_mentions_selected_speakers(entry: dict, selected_speaker_ids: List[str]) -> bool:
    selected_lower = {sid.lower() for sid in selected_speaker_ids}
    selected_names_lower = {get_display_name(sid).lower() for sid in selected_speaker_ids}

    voice_name = str(entry.get("voiceName") or "").lower()
    target_id = str(entry.get("targetId") or "").lower()
    speaker_name = str(entry.get("speaker") or "").lower()
    target_name = str(entry.get("target") or "").lower()
    if voice_name in selected_lower or target_id in selected_lower:
        return True
    if speaker_name in selected_names_lower or target_name in selected_names_lower:
        return True

    for witness in entry.get("earshot") or []:
        if str(witness).lower() in selected_lower:
            return True

    if entry.get("type") == "location":
        companions = entry.get("companions") or []
        companion_names = {str(name).lower() for name in companions}
        if speaker_name in selected_names_lower or companion_names.intersection(selected_names_lower):
            return True

    return False


def _build_shared_recent_history(
    participant_ids: List[str],
    game_context: dict,
    *,
    limit: int = 50,
) -> str:
    dialogue_history = load_dialogue_history(game_context)
    if not dialogue_history:
        return ""

    filtered = filter_dialogue_history(dialogue_history)
    relevant = [entry for entry in filtered if _entry_mentions_selected_speakers(entry, participant_ids)]
    deduped = []
    seen = set()
    for entry in relevant:
        key = (
            int(entry.get("timestamp") or 0),
            str(entry.get("voiceName") or ""),
            str(entry.get("targetId") or ""),
            str(entry.get("speaker") or ""),
            str(entry.get("target") or ""),
            str(entry.get("type") or ""),
            str(entry.get("text") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    if not deduped:
        return ""

    y, m, d = game_context.get("year"), game_context.get("month"), game_context.get("day")
    current_game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get("dateFormatted", "")
    return format_dialogue_history(
        deduped,
        limit=limit,
        current_game_date=current_game_date,
    )


def build_linger_goodbye_request(
    selected_speaker_ids: List[str],
    game_context: dict,
    *,
    conversation_speaker_order: Optional[List[str]] = None,
    departing_ids: Optional[List[str]] = None,
    allowed_target_ids: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Build prompt/user input for streamed terminal linger goodbye lines."""
    player_name = game_context.get("playerName", "Unknown")

    departing_ids = departing_ids or list(selected_speaker_ids)
    departing_names = [get_display_name(sid) for sid in departing_ids]
    selected_names = [get_display_name(sid) for sid in selected_speaker_ids]

    companion_id = game_context.get("companionId", "")
    follower_ids = [fid for fid in (game_context.get("followers", []) or []) if isinstance(fid, str)]
    staying_ids = []
    if companion_id and companion_id not in departing_ids:
        staying_ids.append(companion_id)
    for fid in follower_ids:
        if fid not in departing_ids and fid not in staying_ids:
            staying_ids.append(fid)
    staying_names = [get_display_name(sid) for sid in staying_ids]

    participant_names = [player_name] if player_name and player_name != "Unknown" else []
    participant_names.extend(name for name in selected_names if name and name not in participant_names)
    participant_names.extend(name for name in staying_names if name and name not in participant_names)

    context_str = format_game_context(
        game_context,
        current_speaker=selected_speaker_ids[0] if selected_speaker_ids else None,
        participants=participant_names,
        observer_mode=False,
    )
    history_participant_ids = list(departing_ids or selected_speaker_ids)
    shared_history = _build_shared_recent_history(history_participant_ids, game_context, limit=50)

    persona_blocks = []
    for speaker_id in selected_speaker_ids:
        speaker_name = get_display_name(speaker_id)
        guidance_sections = build_character_guidance_sections(
            speaker_id,
            speaker_name,
            player_name=player_name,
            prompt_mode=False,
            include_player_bio=False,
        )
        if guidance_sections:
            persona_blocks.append(f"### {speaker_name}\n" + "\n\n".join(guidance_sections))

    conversation_order_names = [get_display_name(sid) for sid in (conversation_speaker_order or [])]
    targetable_names = []
    for target_id in allowed_target_ids or []:
        if str(target_id).lower() == "player":
            name = player_name
        else:
            name = get_display_name(target_id)
        if name and name not in targetable_names:
            targetable_names.append(name)
    response_lines = "\n".join(
        f"{get_display_name(sid)} (to {player_name}): <short goodbye response>"
        for sid in selected_speaker_ids
    )

    speech_rules = get_speech_rules(narration_enabled_override=False)
    placeholder_context = {
        "name": ", ".join(selected_names),
        "player": player_name,
        "player_name": player_name,
        "player_house": game_context.get("playerHouse", ""),
        "location": game_context.get("zoneLocation", "") or game_context.get("location", ""),
        "time": game_context.get("timeFormatted", ""),
        "speaking_to": player_name,
    }
    world_lore_block = get_world_lore_block(
        game_context,
        placeholder_context=placeholder_context,
        include_heading=True,
    )

    prompt_parts = [
        f"You are roleplaying as {', '.join(selected_names)}.",
        "",
        speech_rules,
        "",
    ]
    if world_lore_block:
        prompt_parts.extend([world_lore_block, ""])
    prompt_parts.extend([
        "## Current Situation",
        "The conversation has just ended. These are brief terminal walk-away lines right before the departing characters leave.",
        f"Departing now: {', '.join(departing_names) if departing_names else 'None'}",
        f"Staying with the player: {', '.join(staying_names) if staying_names else 'None'}",
        f"Recent conversation order: {', '.join(conversation_order_names) if conversation_order_names else 'Unknown'}",
        f"Valid farewell targets for these lines: {', '.join(targetable_names) if targetable_names else player_name}",
        "Companions and followers who are still accompanying the player must not sound like they are leaving the player.",
        "Each line should feel like a natural final remark after the conversation ended.",
        "Keep each line brief, natural, and in character.",
        "Do not ask a fresh question or start a new conversation.",
        "",
        "### Recent Event Context",
        "- Conversation ended",
        "",
        "## Response Format",
        "Reply with exactly one line for each selected character.",
        "Start each line with the exact display name, then `(to Target Name):`, then the spoken line.",
        "Use one of the valid farewell targets above as the target name.",
        "Do not skip any selected character.",
        "Do not add any extra lines before or after the labeled responses.",
        "",
        response_lines,
    ])
    if context_str:
        prompt_parts.extend(["", context_str])
    if shared_history:
        prompt_parts.extend(["", shared_history])
    if persona_blocks:
        prompt_parts.extend(["", "## Character Grounding", "\n\n".join(persona_blocks)])

    prompt = "\n".join(part for part in prompt_parts if part is not None)
    user_input = (
        f"(The conversation has ended. Give one short in-character goodbye line for each selected character addressed naturally to {player_name} or the immediate group around them. Spoken dialogue only; no narration.)"
    )
    return {
        "prompt": prompt,
        "user_input": user_input,
        "speaker_names": {speaker_id: get_display_name(speaker_id) for speaker_id in selected_speaker_ids},
    }


class _LingerGoodbyeLineParser:
    def __init__(self, selected_speaker_ids: List[str], allowed_target_ids: Optional[List[str]] = None, player_name: str = "player"):
        self._buffer = ""
        self._emitted = set()
        self._speaker_labels = {
            get_display_name(speaker_id).strip().lower(): speaker_id
            for speaker_id in selected_speaker_ids
        }
        self._target_labels = {}
        for target_id in allowed_target_ids or []:
            if str(target_id).lower() == "player":
                display_name = player_name or "player"
                self._target_labels[display_name.strip().lower()] = "player"
                self._target_labels["player"] = "player"
            else:
                display_name = get_display_name(target_id)
                if display_name:
                    self._target_labels[display_name.strip().lower()] = target_id

    def feed(self, chunk: str) -> List[Dict[str, str]]:
        self._buffer += chunk
        completed = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            parsed = self._parse_line(line)
            if parsed:
                completed.append(parsed)
        return completed

    def finish(self) -> List[Dict[str, str]]:
        completed = []
        parsed = self._parse_line(self._buffer)
        if parsed:
            completed.append(parsed)
        self._buffer = ""
        return completed

    def _parse_line(self, raw_line: str) -> Optional[Dict[str, str]]:
        line = (raw_line or "").strip()
        if not line:
            return None
        line = re.sub(r"^```(?:\w+)?\s*", "", line).strip()
        line = re.sub(r"\s*```$", "", line).strip()
        if not line or line.lower().startswith("=== response ==="):
            return None

        match = re.match(r"^(?:(?:[-*]|\d+[.)])\s*)?([^:(]+?)(?:\s*\(to\s+([^)]+)\))?:\s*(.+)$", line, re.IGNORECASE)
        if not match:
            return None

        label = match.group(1).strip().lower()
        raw_target = (match.group(2) or "").strip()
        text = strip_action_tag(match.group(3).strip())
        speaker_id = self._speaker_labels.get(label)
        if not speaker_id or not text or speaker_id.lower() in self._emitted:
            return None

        target_id = "player"
        if raw_target:
            target_id = self._target_labels.get(raw_target.lower(), "player")

        self._emitted.add(speaker_id.lower())
        return {
            "speaker_id": speaker_id,
            "target_id": target_id,
            "text": text,
        }


def stream_linger_goodbye_lines(
    selected_speaker_ids: List[str],
    game_context: dict,
    *,
    conversation_speaker_order: Optional[List[str]] = None,
    departing_ids: Optional[List[str]] = None,
    allowed_target_ids: Optional[List[str]] = None,
):
    """Stream terminal goodbye lines as they are labeled by speaker."""
    request = build_linger_goodbye_request(
        selected_speaker_ids,
        game_context,
        conversation_speaker_order=conversation_speaker_order,
        departing_ids=departing_ids,
        allowed_target_ids=allowed_target_ids,
    )
    parser = _LingerGoodbyeLineParser(
        selected_speaker_ids,
        allowed_target_ids=allowed_target_ids,
        player_name=game_context.get("playerName", "player"),
    )
    for chunk, _accumulated in call_llm_stream(request["prompt"], request["user_input"]):
        for parsed in parser.feed(chunk):
            yield parsed
    for parsed in parser.finish():
        yield parsed


def build_attention_request(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    *,
    mode: str = "continuation",
) -> Dict[str, Any]:
    """Build three-layer cached message structure for attention-triggered NPC speech."""
    player_name = game_context.get("playerName", "Unknown")
    target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name

    speaker_name, base_prompt = get_character(speaker_id, game_context, speaking_to=target_name)

    # Layer 1: Static system message (cacheable across turns)
    static_ctx = format_static_context(game_context, current_speaker=speaker_id)
    system_parts = [base_prompt]
    if static_ctx:
        system_parts.append(static_ctx)
    system_message = "\n\n".join(system_parts)

    # Layer 2: User/assistant message pairs from dialogue history
    dialogue_history = load_dialogue_history(game_context)
    y, m, d = game_context.get("year"), game_context.get("month"), game_context.get("day")
    current_game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get("dateFormatted", "")
    history_messages, event_entries = format_dialogue_as_messages(
        dialogue_history,
        for_npc_id=speaker_id,
        current_game_date=current_game_date,
    )

    # Layer 3: Final user message with dynamic context
    dynamic_ctx = format_dynamic_context(
        game_context, current_speaker=speaker_id,
        event_entries=event_entries,
    )

    if mode == "continuation":
        context_instruction = (
            f"### Continuation Context\n"
            f"You were just speaking with {player_name} and the conversation trailed off.\n"
            f"They are still here, looking at you.\n"
            f"Continue the conversation naturally — share a thought, bring up something "
            f"related to what you were discussing, or ask them something.\n"
            f"One or two lines, stay in character.\n"
            f"Do not reference that they are staring or silent."
        )
        user_instruction = (
            f"(You are continuing a conversation as {speaker_name}. "
            f"You are speaking to {target_name}. Share a thought or ask something naturally.)"
        )
    else:
        context_instruction = (
            f"### Approach Context\n"
            f"{player_name} is standing near you and looking at you.\n"
            f"You have not been speaking with them.\n"
            f"React naturally — acknowledge them, greet them, or comment on what's "
            f"happening around you.\n"
            f"One short line, stay in character."
        )
        user_instruction = (
            f"(You are reacting to {target_name} approaching you as {speaker_name}. "
            f"Greet them or comment on something nearby in one short line.)"
        )

    final_user_parts = [context_instruction, user_instruction]
    if dynamic_ctx:
        final_user_parts.append("---")
        final_user_parts.append(dynamic_ctx)
    final_user_parts.append(_format_history_output_reminder(
        has_vision_context=bool(dynamic_ctx and "**What you can see:**" in dynamic_ctx)
    ))
    final_user_message = "\n\n".join(final_user_parts)

    # Assemble three-layer messages array
    messages = [{"role": "system", "content": system_message}]
    messages.extend(history_messages)
    messages.append({"role": "user", "content": final_user_message})

    return {
        "messages": messages,
        "speaker_name": speaker_name,
        "target_name": target_name,
        "player_name": player_name,
    }


def generate_attention_response(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    *,
    mode: str = "continuation",
    lua_socket: Any = None,
) -> Optional[str]:
    """Blocking attention response generator used for fallback compatibility."""
    request = build_attention_request(
        speaker_id,
        target_id,
        game_context,
        mode=mode,
    )

    raw_response = call_llm_messages(request["messages"], speaker_id=speaker_id)
    if raw_response is None:
        return None

    response = strip_response_metadata(strip_action_tag(raw_response))
    print(f"[Attention] {speaker_id} response ({mode}): {response}")
    return response


def _create_pending_history_entry(
    *,
    speaker_name: str,
    speaker_id: str,
    target_name: str,
    target_id: str,
    game_context: dict,
    text: str,
    topic: Optional[str],
    trigger_event_type: str,
    extra_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    game_time = game_context.get("timeFormatted", "") or game_context.get("time", "")
    y, m, d = game_context.get("year"), game_context.get("month"), game_context.get("day")
    game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get("dateFormatted", "")
    entry = {
        "timestamp": int(time.time()),
        "gameTime": game_time,
        "gameDate": game_date,
        "speaker": speaker_name,
        "voiceName": speaker_id,
        "target": target_name,
        "targetId": "Player" if target_id and target_id.lower() == "player" else target_id,
        "text": text,
        "isAIResponse": True,
        "isPlayer": False,
        "type": "dialogue",
        "earshot": [],
        "_playback_completed": False,
        "eventTrigger": trigger_event_type,
        "eventTopic": topic or "",
    }
    if extra_fields:
        entry.update(extra_fields)
    return entry


def _retain_committable_history_entries(conv_state: Any, log_prefix: str) -> None:
    """Drop entries that explicitly require full playback but never completed."""
    if not conv_state.pending_history_entries:
        return

    retained = []
    discarded = 0
    for entry in conv_state.pending_history_entries:
        if entry.get("_require_full_playback") and not entry.get("_playback_completed"):
            discarded += 1
            continue
        retained.append(entry)

    if discarded:
        print(f"{log_prefix} Discarded {discarded} pending full-playback entries")
    conv_state.pending_history_entries = retained


def play_unsolicited_response(
    speaker_id: str,
    target_id: str,
    response: str,
    game_context: dict,
    *,
    log_prefix: str,
    topic: Optional[str],
    trigger_event_type: str,
    extra_history_fields: Optional[Dict[str, Any]] = None,
    lua_socket: Any,
    conv_state: Any,
    start_new_conversation: Callable[[], int],
    clear_cancel: Callable[[], None],
    is_conversation_valid: Callable[[Optional[int]], bool],
    play_completed_response_streaming: Callable[..., Dict[str, Any]],
    run_tts_async: Callable,
    tts_available: bool,
    cancel_pending_follow_up: Optional[Callable[[str], None]] = None,
    before_play_guard: Optional[Callable[[], bool]] = None,
) -> bool:
    """Play a single unsolicited line with streaming/fallback parity."""
    if not response or not speaker_id:
        return False

    mode_name = log_prefix.strip("[]").lower() or "unsolicited"
    if cancel_pending_follow_up:
        cancel_pending_follow_up(f"{mode_name}_start")

    if conv_state.state != "idle" or lua_socket.pipeline_active:
        print(f"{log_prefix} Abort before play - Sonorus no longer idle")
        return False

    if before_play_guard and not before_play_guard():
        print(f"{log_prefix} Abort before play - guard callback failed")
        return False

    epoch = start_new_conversation()
    clear_cancel()
    conv_state.reset()
    conv_state.state = "playing"
    conv_state.turn_count = 1

    player_name = game_context.get("playerName", "Unknown")
    speaker_name = get_display_name(speaker_id)
    target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name
    commentary_location = game_context.get("zoneLocation") or game_context.get("location", "Unknown")
    conv_state.track_speaker(speaker_id, location=commentary_location, context=game_context)

    lua_socket.send_conversation_state("playing")
    lua_socket.send_lock_npc(speaker_id, target_id)
    conv_state.add_to_queue(speaker_name, target_name, response, speaker_id=speaker_id)

    pending_history_entry = _create_pending_history_entry(
        speaker_name=speaker_name,
        speaker_id=speaker_id,
        target_name=target_name,
        target_id=target_id,
        game_context=game_context,
        text=response,
        topic=topic,
        trigger_event_type=trigger_event_type,
        extra_fields=extra_history_fields,
    )
    turn_started = False

    playback_result = play_completed_response_streaming(
        response,
        speaker_id,
        speaker_name,
        target_id,
        conv_state.turn_count,
        epoch,
        raw_response=response,
        pending_history_entry=pending_history_entry,
    )

    if not playback_result.get("success"):
        if playback_result.get("fallback_required") and speaker_id:
            try:
                subtitle_seed_text = remove_unpaired_double_quotes(response)
                turn_result = lua_socket.send_play_turn(
                    speaker_id=speaker_id,
                    display_name=speaker_name,
                    text=subtitle_seed_text,
                    turn_index=conv_state.turn_count,
                    target_id=target_id,
                    streaming_subtitles=False,
                )
                if not turn_result.get("success"):
                    print(f"{log_prefix} play_turn failed for {speaker_id}")
                    conv_state.pending_history_entries = []
                    conv_state.reset()
                    lua_socket.send_reset()
                    lua_socket.send_conversation_state("idle")
                    return False
                turn_started = True
                conv_state.add_pending_history(pending_history_entry)
                if tts_available and response.strip():
                    lua_socket.pipeline_active = True
                    lua_socket.pipeline_event.clear()
                    tts_thread = threading.Thread(
                        target=run_tts_async,
                        args=(response, speaker_id, turn_result.get("positions"), turn_result.get("turn_id"), epoch, pending_history_entry),
                        daemon=True,
                    )
                    tts_thread.start()
            except Exception as e:
                print(f"{log_prefix} TTS error: {e}")
                lua_socket.send_notification(f"TTS failed: {e}")
                conv_state.reset()
                lua_socket.send_reset()
                lua_socket.send_conversation_state("idle")
                return False
        else:
            print(f"{log_prefix} Playback setup failed for {speaker_id}: {playback_result.get('error')}")
            conv_state.pending_history_entries = []
            conv_state.reset()
            lua_socket.send_reset()
            lua_socket.send_conversation_state("idle")
            return False
    else:
        turn_started = True

    if lua_socket.pipeline_active:
        lua_socket.wait_for_pipeline_stop(timeout=60.0)

    playback_completed = bool(pending_history_entry.get("_playback_completed"))
    requires_full_playback = bool(pending_history_entry.get("_require_full_playback"))
    _retain_committable_history_entries(conv_state, log_prefix)
    if is_conversation_valid(epoch) and conv_state.pending_history_entries:
        count, _ = conv_state.commit_pending_history()
        print(f"{log_prefix} Committed {count} history entries")
    elif conv_state.pending_history_entries:
        conv_state.pending_history_entries = []

    conv_state.state = "idle"
    conv_state.interrupted = False
    lua_socket.send_conversation_state("idle")
    clear_cancel()
    if requires_full_playback:
        return playback_completed
    if tts_available and turn_started:
        return playback_completed
    return turn_started


def run_unsolicited_turn(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    *,
    log_prefix: str,
    topic: Optional[str],
    trigger_event_type: str,
    request_builder: Callable[..., Dict[str, str]],
    blocking_response_builder: Callable[..., Optional[str]],
    request_kwargs: Optional[Dict[str, Any]] = None,
    extra_history_fields: Optional[Dict[str, Any]] = None,
    lua_socket: Any,
    conv_state: Any,
    start_new_conversation: Callable[[], int],
    clear_cancel: Callable[[], None],
    is_conversation_valid: Callable[[Optional[int]], bool],
    can_use_streaming_tts: Callable[[], bool],
    run_streaming_tts_async: Callable,
    tts_service: Any,
    load_settings_func: Callable[[], Dict[str, Any]],
    stream_sentences_func: Callable,
    play_completed_response_streaming_func: Callable[..., Dict[str, Any]],
    run_tts_async: Callable,
    tts_available: bool,
    cancel_pending_follow_up: Optional[Callable[[str], None]] = None,
    before_play_guard: Optional[Callable[[], bool]] = None,
) -> bool:
    """Canonical single-turn unsolicited runtime entrypoint."""
    request_kwargs = request_kwargs or {}
    mode_name = log_prefix.strip("[]").lower() or "unsolicited"

    def fallback_to_blocking_unsolicited() -> bool:
        response = blocking_response_builder(
            speaker_id,
            target_id,
            game_context,
            **request_kwargs,
            lua_socket=lua_socket,
        )
        if not response:
            return False
        return play_unsolicited_response(
            speaker_id,
            target_id,
            response,
            game_context,
            log_prefix=log_prefix,
            topic=topic,
            trigger_event_type=trigger_event_type,
            extra_history_fields=extra_history_fields,
            lua_socket=lua_socket,
            conv_state=conv_state,
            start_new_conversation=start_new_conversation,
            clear_cancel=clear_cancel,
            is_conversation_valid=is_conversation_valid,
            play_completed_response_streaming=play_completed_response_streaming_func,
            run_tts_async=run_tts_async,
            tts_available=tts_available,
            cancel_pending_follow_up=cancel_pending_follow_up,
            before_play_guard=before_play_guard,
        )

    if not can_use_streaming_tts():
        return fallback_to_blocking_unsolicited()

    if cancel_pending_follow_up:
        cancel_pending_follow_up(f"{mode_name}_start")

    request = request_builder(
        speaker_id,
        target_id,
        game_context,
        **request_kwargs,
    )

    epoch = start_new_conversation()
    clear_cancel()

    full_text_holder = {"text": "", "raw": ""}
    llm_done = threading.Event()
    _stream_kwargs = dict(
        full_text_holder=full_text_holder,
        llm_done=llm_done,
        stream_sentences_func=stream_sentences_func,
        strip_action_tag_func=strip_action_tag,
        narration_enabled=load_settings_func().get("conversation", {}).get("narration_enabled", False),
        log_prefix=f"{log_prefix}Stream",
        speaker_id=speaker_id,
    )
    if "messages" in request:
        sentence_iterable = build_live_sentence_stream(messages=request["messages"], **_stream_kwargs)
    else:
        sentence_iterable = build_live_sentence_stream(request["prompt"], request["user_input"], **_stream_kwargs)

    pending_history_entry = _create_pending_history_entry(
        speaker_name=request["speaker_name"],
        speaker_id=speaker_id,
        target_name=request["target_name"],
        target_id=target_id,
        game_context=game_context,
        text="",
        topic=topic,
        trigger_event_type=trigger_event_type,
        extra_fields=extra_history_fields,
    )

    session_result = start_streaming_playback_session(
        sentence_iterable=sentence_iterable,
        speaker_id=speaker_id,
        full_text_holder=full_text_holder,
        epoch=epoch,
        run_streaming_tts_async=run_streaming_tts_async,
        tts_service=tts_service,
        lua_socket=lua_socket,
        load_settings_func=load_settings_func,
        can_use_streaming_tts_func=can_use_streaming_tts,
        is_conversation_valid_func=is_conversation_valid,
        pending_history_entry=pending_history_entry,
        require_voice_prefetch=True,
    )
    if not session_result.get("success"):
        print(f"{log_prefix} Streaming setup failed for {speaker_id}; falling back to blocking path")
        return fallback_to_blocking_unsolicited()

    session = session_result["session"]
    if not session.wait_for_buffer(timeout=120.0):
        print(f"{log_prefix} TTS buffer ready timeout for {speaker_id}")
        session.abort()
        if is_conversation_valid(epoch):
            lua_socket.send_conversation_state("idle")
        clear_cancel()
        return False

    if not full_text_holder.get("text", "").strip():
        print(f"{log_prefix} Streaming produced no dialogue text")
        session.abort()
        if is_conversation_valid(epoch):
            lua_socket.send_conversation_state("idle")
        clear_cancel()
        return False

    if not is_conversation_valid(epoch) or conv_state.state != "idle":
        print(f"{log_prefix} Abort before play - Sonorus no longer idle/current")
        session.abort()
        clear_cancel()
        return False

    if before_play_guard and not before_play_guard():
        print(f"{log_prefix} Abort before play - guard callback failed")
        session.abort()
        clear_cancel()
        return False

    conv_state.reset()
    conv_state.state = "playing"
    conv_state.turn_count = 1

    commentary_location = game_context.get("zoneLocation") or game_context.get("location", "Unknown")
    conv_state.track_speaker(speaker_id, location=commentary_location, context=game_context)
    conv_state.add_to_queue(
        request["speaker_name"],
        request["target_name"],
        full_text_holder.get("text", ""),
        speaker_id=speaker_id,
    )

    lua_socket.send_conversation_state("playing")
    lua_socket.send_lock_npc(speaker_id, target_id)

    playback_result = session.start_playback(
        display_name=request["speaker_name"],
        text=full_text_holder.get("text", ""),
        turn_index=conv_state.turn_count,
        target_id=target_id,
        remove_unpaired_double_quotes=remove_unpaired_double_quotes,
        pending_history_entry=pending_history_entry,
        add_pending_history=conv_state.add_pending_history,
    )
    if not playback_result.get("success"):
        print(f"{log_prefix} Playback setup failed for {speaker_id}: {playback_result.get('error')}")
        conv_state.pending_history_entries = []
        conv_state.reset()
        lua_socket.send_reset()
        lua_socket.send_conversation_state("idle")
        clear_cancel()
        return False

    playback_completed = False
    if not llm_done.wait(timeout=120.0):
        print(f"{log_prefix} LLM streaming timeout (audio may still be playing)")

    final_text = full_text_holder.get("text", "")
    if final_text:
        # Preserve narration/emphasis markup in canonical history.
        pending_history_entry["text"] = final_text
        if not session.sentence_subtitles and playback_result["turn_result"].get("turn_id"):
            subtitle_text = re.sub(r"\*([^*]+)\*", r"\1", final_text)
            subtitle_text = remove_unpaired_double_quotes(subtitle_text)
            lua_socket.send(
                {
                    "type": "subtitle_update",
                    "turn_id": playback_result["turn_result"]["turn_id"],
                    "text": subtitle_text,
                    "sentence_idx": 0,
                    "total_sentences": 1,
                    "is_narration": False,
                }
            )

    if lua_socket.pipeline_active:
        lua_socket.wait_for_pipeline_stop(timeout=60.0)

    playback_completed = bool(pending_history_entry.get("_playback_completed"))
    requires_full_playback = bool(pending_history_entry.get("_require_full_playback"))
    _retain_committable_history_entries(conv_state, log_prefix)
    if is_conversation_valid(epoch) and conv_state.pending_history_entries:
        count, _ = conv_state.commit_pending_history()
        print(f"{log_prefix} Committed {count} history entries")
    elif conv_state.pending_history_entries:
        conv_state.pending_history_entries = []

    conv_state.state = "idle"
    conv_state.interrupted = False
    lua_socket.send_conversation_state("idle")
    clear_cancel()
    if requires_full_playback:
        return playback_completed
    return playback_completed


def play_commentary_response(
    speaker_id: str,
    target_id: str,
    response: str,
    game_context: dict,
    topic: Optional[str],
    trigger_event_type: str,
    *,
    lua_socket: Any,
    conv_state: Any,
    start_new_conversation: Callable[[], int],
    clear_cancel: Callable[[], None],
    is_conversation_valid: Callable[[Optional[int]], bool],
    play_completed_response_streaming: Callable[..., Dict[str, Any]],
    run_tts_async: Callable,
    tts_available: bool,
    cancel_pending_follow_up: Optional[Callable[[str], None]] = None,
    before_play_guard: Optional[Callable[[], bool]] = None,
) -> bool:
    """Play a single completed commentary line with streaming/fallback parity."""
    return play_unsolicited_response(
        speaker_id,
        target_id,
        response,
        game_context,
        log_prefix="[Commentary]",
        topic=topic,
        trigger_event_type=trigger_event_type,
        lua_socket=lua_socket,
        conv_state=conv_state,
        start_new_conversation=start_new_conversation,
        clear_cancel=clear_cancel,
        is_conversation_valid=is_conversation_valid,
        play_completed_response_streaming=play_completed_response_streaming,
        run_tts_async=run_tts_async,
        tts_available=tts_available,
        cancel_pending_follow_up=cancel_pending_follow_up,
        before_play_guard=before_play_guard,
    )


def play_follow_up_response(
    speaker_id: str,
    target_id: str,
    response: str,
    game_context: dict,
    last_question_text: str,
    *,
    lua_socket: Any,
    conv_state: Any,
    start_new_conversation: Callable[[], int],
    clear_cancel: Callable[[], None],
    is_conversation_valid: Callable[[Optional[int]], bool],
    play_completed_response_streaming: Callable[..., Dict[str, Any]],
    run_tts_async: Callable,
    tts_available: bool,
    before_play_guard: Optional[Callable[[], bool]] = None,
) -> bool:
    """Play a single completed follow-up line with streaming/fallback parity."""
    return play_unsolicited_response(
        speaker_id,
        target_id,
        response,
        game_context,
        log_prefix="[FollowUp]",
        topic=None,
        trigger_event_type="question_follow_up",
        extra_history_fields={
            "followUp": True,
            "followUpReason": "question_timeout",
            "lastQuestionText": last_question_text,
            "_require_full_playback": True,
        },
        lua_socket=lua_socket,
        conv_state=conv_state,
        start_new_conversation=start_new_conversation,
        clear_cancel=clear_cancel,
        is_conversation_valid=is_conversation_valid,
        play_completed_response_streaming=play_completed_response_streaming,
        run_tts_async=run_tts_async,
        tts_available=tts_available,
        before_play_guard=before_play_guard,
    )


def run_commentary_turn(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    topic: Optional[str],
    trigger_event_type: str,
    *,
    recent_events: Optional[list] = None,
    lua_socket: Any,
    conv_state: Any,
    start_new_conversation: Callable[[], int],
    clear_cancel: Callable[[], None],
    is_conversation_valid: Callable[[Optional[int]], bool],
    can_use_streaming_tts: Callable[[], bool],
    run_streaming_tts_async: Callable,
    tts_service: Any,
    load_settings_func: Callable[[], Dict[str, Any]],
    stream_sentences_func: Callable,
    play_completed_response_streaming_func: Callable[..., Dict[str, Any]],
    run_tts_async: Callable,
    tts_available: bool,
    cancel_pending_follow_up: Optional[Callable[[str], None]] = None,
    before_play_guard: Optional[Callable[[], bool]] = None,
) -> bool:
    """Canonical commentary runtime entrypoint."""
    return run_unsolicited_turn(
        speaker_id,
        target_id,
        game_context,
        log_prefix="[Commentary]",
        topic=topic,
        trigger_event_type=trigger_event_type,
        request_builder=build_commentary_request,
        blocking_response_builder=generate_commentary_response,
        request_kwargs={"topic": topic, "recent_events": recent_events},
        lua_socket=lua_socket,
        conv_state=conv_state,
        start_new_conversation=start_new_conversation,
        clear_cancel=clear_cancel,
        is_conversation_valid=is_conversation_valid,
        can_use_streaming_tts=can_use_streaming_tts,
        run_streaming_tts_async=run_streaming_tts_async,
        tts_service=tts_service,
        load_settings_func=load_settings_func,
        stream_sentences_func=stream_sentences_func,
        play_completed_response_streaming_func=play_completed_response_streaming_func,
        run_tts_async=run_tts_async,
        tts_available=tts_available,
        cancel_pending_follow_up=cancel_pending_follow_up,
        before_play_guard=before_play_guard,
    )


def run_follow_up_turn(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    last_question_text: str,
    *,
    lua_socket: Any,
    conv_state: Any,
    start_new_conversation: Callable[[], int],
    clear_cancel: Callable[[], None],
    is_conversation_valid: Callable[[Optional[int]], bool],
    can_use_streaming_tts: Callable[[], bool],
    run_streaming_tts_async: Callable,
    tts_service: Any,
    load_settings_func: Callable[[], Dict[str, Any]],
    stream_sentences_func: Callable,
    play_completed_response_streaming_func: Callable[..., Dict[str, Any]],
    run_tts_async: Callable,
    tts_available: bool,
    before_play_guard: Optional[Callable[[], bool]] = None,
) -> bool:
    """Canonical follow-up runtime entrypoint."""
    return run_unsolicited_turn(
        speaker_id,
        target_id,
        game_context,
        log_prefix="[FollowUp]",
        topic=None,
        trigger_event_type="question_follow_up",
        request_builder=build_follow_up_request,
        blocking_response_builder=generate_follow_up_response,
        request_kwargs={"last_question_text": last_question_text},
        extra_history_fields={
            "followUp": True,
            "followUpReason": "question_timeout",
            "lastQuestionText": last_question_text,
            "_require_full_playback": True,
        },
        lua_socket=lua_socket,
        conv_state=conv_state,
        start_new_conversation=start_new_conversation,
        clear_cancel=clear_cancel,
        is_conversation_valid=is_conversation_valid,
        can_use_streaming_tts=can_use_streaming_tts,
        run_streaming_tts_async=run_streaming_tts_async,
        tts_service=tts_service,
        load_settings_func=load_settings_func,
        stream_sentences_func=stream_sentences_func,
        play_completed_response_streaming_func=play_completed_response_streaming_func,
        run_tts_async=run_tts_async,
        tts_available=tts_available,
        before_play_guard=before_play_guard,
    )


def run_attention_turn(
    speaker_id: str,
    target_id: str,
    game_context: dict,
    mode: str,
    *,
    lua_socket: Any,
    conv_state: Any,
    start_new_conversation: Callable[[], int],
    clear_cancel: Callable[[], None],
    is_conversation_valid: Callable[[Optional[int]], bool],
    can_use_streaming_tts: Callable[[], bool],
    run_streaming_tts_async: Callable,
    tts_service: Any,
    load_settings_func: Callable[[], Dict[str, Any]],
    stream_sentences_func: Callable,
    play_completed_response_streaming_func: Callable[..., Dict[str, Any]],
    run_tts_async: Callable,
    tts_available: bool,
    cancel_pending_follow_up: Optional[Callable[[str], None]] = None,
    before_play_guard: Optional[Callable[[], bool]] = None,
) -> bool:
    """Canonical attention-triggered runtime entrypoint."""
    trigger_type = "attention_continuation" if mode == "continuation" else "attention_cold_approach"
    return run_unsolicited_turn(
        speaker_id,
        target_id,
        game_context,
        log_prefix="[Attention]",
        topic=None,
        trigger_event_type=trigger_type,
        request_builder=build_attention_request,
        blocking_response_builder=generate_attention_response,
        request_kwargs={"mode": mode},
        lua_socket=lua_socket,
        conv_state=conv_state,
        start_new_conversation=start_new_conversation,
        clear_cancel=clear_cancel,
        is_conversation_valid=is_conversation_valid,
        can_use_streaming_tts=can_use_streaming_tts,
        run_streaming_tts_async=run_streaming_tts_async,
        tts_service=tts_service,
        load_settings_func=load_settings_func,
        stream_sentences_func=stream_sentences_func,
        play_completed_response_streaming_func=play_completed_response_streaming_func,
        run_tts_async=run_tts_async,
        tts_available=tts_available,
        cancel_pending_follow_up=cancel_pending_follow_up,
        before_play_guard=before_play_guard,
    )
