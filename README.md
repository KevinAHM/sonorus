# **Sonorus - AI Conversations**

[Join us on Discord!](https://discord.gg/YXhJy3pA7b)

---

## **About Sonorus**

Sonorus lets you have real conversations with any named character in Hogwarts Legacy. Using AI, NPCs respond dynamically to what you say, where you are, and what's happening around you - all with their original voices and synchronized lip movements.

Every character has their own personality. Professors remember they're your teachers. Students know which house you're in. Shopkeepers greet you differently at midnight than at noon. And when you're chatting with one character, others nearby might chime in with their own thoughts.

**Disclaimer:** Sonorus uses AI language models to generate NPC dialogue. AI responses are not curated and may be inaccurate, unexpected, or occasionally offensive. Do not take anything an NPC says as fact or personal advice. These are fictional characters powered by probabilistic text generation, not real people. Please be mindful of this, especially during extended play sessions. For more on the psychological effects of conversational AI, see: [Chatbot Psychosis (Wikipedia)](https://en.wikipedia.org/wiki/Chatbot_psychosis)

## **Key Features**

**Talk to Anyone**  
Approach any named NPC in the game and start a conversation. Ask Professor Weasley about class, chat with Sebastian about the Undercroft, or get Deek's opinion on your latest adventure.

**Original Voices**  
NPCs speak with voice-cloned versions of their original game voices. Combined with real-time lip sync, conversations feel natural and immersive.  
NPC voices are spatialized in 3D and react to their surroundings, so characters sound like they’re speaking from their actual position and environment.

**Context Awareness**  
NPCs know:

- Your name and house
- Your gear and transmogs
- Any custom bio set for your character
- The current time and date
- Their location (and yours)
- Who else is nearby
- Whether you're invisible, in combat, on a broom, or swimming
- Your current mission
- What spells you've recently cast
- Who you've recently fought
- What you're looking at (vision feature)
- How long it's been since you last spoke to them
- World lore (configurable static info all NPCs share)

**Group Conversations**  
When multiple NPCs are nearby, they can react to your conversations and speak to each other. Discussions can flow naturally between characters.

**NPC Commitments**  
NPCs can agree to meet at specific locations and times, then travel there and wait for you (up to 45 in-game minutes). If you don't show up, they'll remember the no-show.

<details>
<summary><strong>Commitment Meeting Locations</strong></summary>

**These are the locations NPCs can agree to meet at.**

**Hogsmeade**
- Three Broomsticks
- Hog's Head
- Hogsmeade Graveyard
- The Old Fool
- Twisted Alley
- Hogsmeade Village Gates
- Hogsmeade Water Mill

**Hogwarts - Great Hall & Courtyards**
- The Great Hall
- Quad Courtyard
- Transfiguration Courtyard
- Viaduct Entrance

**Hogwarts - Common Rooms**
- Gryffindor Common Room
- Hufflepuff Common Room
- Ravenclaw Common Room
- Slytherin Common Room

**Hogwarts - Other**
- Astronomy Tower
- The Boathouse
- Hospital Wing
- The Owlery
- Faculty Tower
- Stone Bridge
- Suspension Bridge
- Wooden Bridge
- The Greenhouses
- The Pond Dock

</details>

**Director Mode**  
Hold the chat hotkey to prompt conversations between NPCs, or say "direct ..." followed by your prompt. Direct scenes and let characters talk to each other on your cue.

**Conversation Modes**  
Toggle between Default, Continuous (no turn limit), and 1-to-1 (no interjections) using the Mode hotkey (default: Home).

**Vision**  
NPCs can "see" what you see through optional screenshot analysis. Comment on the weather, point out something interesting, or ask what that creature is - they'll understand.

**VR Support (UEVR)**  
Sonorus supports VR through UEVR with headtracking 3D audio, gaze offset, and gesture controls. Tap grip near your mouth to toggle Open Mic, or hold grip to stop a conversation.

**Day / Night Time Controls**  
Sonorus includes optional day and night time scaling. By default, both day and night run at 3× real time speed (game default is 30x), allowing time to pass naturally without affecting conversation flow.

---

## **Speech Interaction**

You can speak naturally to NPCs using your microphone, with real-time speech-to-text and optional hands-free input. Multiple local speech recognition options are available — Parakeet, Moonshine (English only), and Canary 180M Flash (English, German, French, Spanish) — with no cloud API or internet connection needed. Cloud providers (Deepgram, OpenAI Whisper) are available as alternatives.

Speech input supports:

- Conversational dialogue with NPCs
- Optional Open Mic mode for hands-free play
- Configurable voice activity detection (VAD)

Spoken input is treated the same as typed text and fully respects NPC context, memory, and awareness.

**Voice Spells**  
As an optional feature, you can cast spells by speaking their names while holding right-click/LT (or holding your wand out in VR). Custom trained spell detection models provide fast, accurate recognition. Saying "Lumos", "Accio", or any other unlocked spell will trigger it automatically.

Voice spells can be enabled or disabled independently in the settings.

---

## **Memory System**

Sonorus uses a two-layer memory system to keep conversations coherent in the moment, while also building long-term, evolving relationships over many play sessions.

**Full Dialogue History (Stored Long-Term)**  
Sonorus keeps a persistent dialogue history for each NPC. This provides a complete record of your past conversations and can be viewed and managed from the web configuration page.

During live conversations, only a configurable amount of the most recent dialogue (per NPC) is included in the AI’s context. This keeps responses fast, relevant, and focused while still preserving the full history for reference and management.

**Long-Term Knowledge Graph Memory**  
In addition to recent dialogue context, NPCs build long-term memory using a structured knowledge graph.

Rather than relying on raw chat logs alone, Sonorus extracts meaningful information — such as events, relationships, opinions, discoveries, and shared experiences — and connects them into an evolving network unique to each NPC. This gives characters more natural recall and continuity over time.

How it works:

- **Narrative Chapters** – Conversations are organized into chapters (for example, "A Late Night at the Three Broomsticks"). When a chapter closes, key details are distilled into long-term memory.
- **Per-NPC Perspective** – Each NPC has their own memory graph. Characters only remember what they personally experienced or were told, leading to different knowledge, opinions, and relationships.
- **Evolving NPC Bios** – NPC bios are automatically created and updated over time based on memories. Personality traits, ongoing concerns, attitudes toward you, and notable history evolve naturally.
- **Contextual Recall** – During conversations, NPCs recall relevant memories based on who is present, where you are, what you’re discussing, and your shared history.
- **Player Guidance** – You can optionally add custom guidance per NPC through the web configuration page to shape personality or behavior without overwriting their lived experiences.

Together, recent dialogue context and knowledge-graph memory allow NPCs to stay grounded in what was just said while still remembering the bigger story across many sessions.

**Note:** Long-term memory requires OpenRouter or OpenAI as your LLM provider (Gemini is not supported for this feature).

---

## **Completely Free to Use**

Sonorus works entirely with free tier or free credit AI services:

- **Gemini** - AI responses (Free tier, handful of messages per day)
- **Voice Synthesis:**
  - **Pocket TTS** - Local voice cloning (runs on your PC, English only)
  - **Inworld** - Cloud TTS ($2 free credit, about 3 hours of audio)
- **Speech-to-Text:**
  - **Canary Flash** - Local speech recognition (Runs on your PC, English, German, French, Spanish)
  - **Parakeet** - Local speech recognition (Runs on your PC, most game supported languages)
  - **Moonshine** - Local speech recognition (Runs on your PC, English only)
  - **Deepgram** - Cloud STT alternative ($200 free credits)

No subscriptions required. No API costs for light use.

**Note:** Gemini has reduced their free tier rate limits recently. If you hit daily limits, we recommend [OpenRouter](https://openrouter.ai/) - a $5 minimum deposit will usually last a long time.

---

## **Other Services**

You can use other services or proxies to locally hosted models:

- **OpenRouter** - AI responses (Recommended! Minimum $5 deposit)
- **OpenAI** - AI responses (Official API or a proxy)
- **Voice Synthesis:**
  - **ElevenLabs** - Expensive but high quality (Not recommended)
- **Speech-to-Text:**
  - **OpenAI Whisper** - Official API or a proxy

---

## **Installation**

1. Extract the downloaded zip to any folder on your computer (not your game folder)
2. Make sure Hogwarts Legacy is closed
3. Double-click `install_sonorus.bat` and follow the prompts
4. Launch Hogwarts Legacy
5. A setup window will open in the background - wait for it to complete
6. Your web browser will open with the configuration wizard
7. Complete the wizard to configure your API keys and settings
8. Enable subtitles in your Hogwarts Legacy settings (for chat input to be visible)
9. Use the configured hotkeys or your microphone to talk to NPCs

Once installed, you can safely delete the extracted files. Sonorus now lives inside your game directory.

---

## **Updating**

Run `install_sonorus.bat` again from a newer version of the zip. Your data (memories, dialogue, settings) will not be affected.

**Potential Issue:** I am having trouble with voice clones on Inworld after upgrading.  
**a)** Create a new workspace ID on Inworld and set it in the Sonorus TTS settings.

---

## **Uninstalling**

Run `uninstall_sonorus.bat` from the same zip you used to install. It will offer to back up your data (memories, dialogue, settings) before removing anything.

---

## **Requirements**

- Hogwarts Legacy (latest Steam version confirmed working)
- Windows PC
- Microphone (for voice chat) or keyboard (for text chat)
- Internet connection (for AI services)

---

## **Compatibility**

- **Languages:** Voice synthesis and cloning are currently supported for English, German, Italian, Brazilian Portuguese, Latin American Spanish, and French. NPC speech automatically matches the language your game is set to. Additional voices and languages can be added manually using the /voice-manager/ in the web interface.
- **Steam:** Tested and working
- **Epic Games:** Tested and working
- **Xbox App:** Likely working
- **Ultra Plus:** Tested and working, but run Ultra+ Manager *after* installing Sonorus and select its "I maintain my own UE4SS" option
- **Reshade:** Tested and working
- **UEVR:** Tested and working - headtracking 3D audio, gaze offset, and gesture controls
- **Other UE4SS mods:** Should work, but remove other UE4SS installations first, then manually install them into ue4ss/Mods
- **Floo Companion Mod:** Tested and working - converse on brooms! (recommended)
- **Stand Up Straight:** Tested and working (recommended)
- **House Points Mod:** Tested and working (recommended)
- (MOD SUPPORT) NPCs will know house point totals and teachers can award/demerit
- **Emote With Any NPC:** Tested and working, but typing into chat may trigger emotes
- **Time Dilator Mod:** Incompatible, Sonorus has its own time controls
- **Immersive NPC ModPack:** Incompatible, breaks Tempus in Sonorus. If you want to use it you *have* to disable Tempus in the web UI and then use Time Dilator as a workaround (if wanting time controls).
- **Immersive NPC Schedules:** Tested and working (CurseForge version)

---

## **Known Issues**

- NPCs with repeating quest callout lines (like Zenobia's Gobstones dialogue) may occasionally speak their ambient lines during AI conversations. A workaround mutes them during conversation and stops native lip animations, but it's not perfect, as the subtitles still show for a brief moment and the audio can occasionally be heard in the background ambience. If you have solutions, please share!
- Subtitles need to be turned on to see chat input and for the game to record non-AI dialog to history.
- Game recordings do not include AI speech unless you have "capture desktop audio" on.
- Some users experience a crash on game startup due to mod compatibility. If this happens, go to `Phoenix\Binaries\Win64\ue4ss\Mods` and delete the `BPModLoaderMod` folder, then try again.

---

## **Experimental Features**

Some features are marked experimental in the settings. Use at your own risk - especially anything that affects NPC behavior beyond conversation. Back up your saves!

---

## **Changelog**

**1.0.6**
- All-in-one installer (no need to download UE4SS or copy folders)
- Add UEVR support (headtracking 3D audio, gaze offset, tap grip near mouth for Open Mic toggle, hold grip to stop conversation)
- Add local Speech-to-Text options: Moonshine (English only) and Canary 180M Flash (English, German, French, Spanish)
- Add custom trained verbal spell detection models for faster casting
- Verbal spell casts now require right-click/LT held + mic on (or wand out in VR)
- Add mic gain slider to Speech settings
- Director mode accessible through speech - say "direct ..." or localized equivalent
- Add NPC commitments - NPCs can agree to meet at specific locations, show up and wait up to 45 minutes, and remember no-shows
- Improve NPC attention lock - more likely to smoothly exit activity before talking
- NPCs linger after conversation ends instead of immediately walking away
- Companions face their conversation target and orient towards you during other conversations
- Companions move where you are looking when asked (toggleable)
- Companions no longer lag behind + configurable follow distance in experimental section
- Overhauled interjection system - less ping-ponging, more natural turn taking
- Improve Open Mic interruptions - if no speech transcribed, NPCs resume speaking
- NPC join/leave companion actions now compatible with Floo Companion mod
- Improve 3D audio spatialization (realistic distance roll-offs)
- Add IPA pronunciation support for Inworld (press "Reset to Default" for updated defaults)
- Add per-character TTS model override option
- Add Italian voice manifest
- Improve NPC, Director, and Target LLM prompts (press "Reset to Default" in Agent Prompts)
- Improve character prompt (press "Reset to Default" in Character Settings)
- Add world lore option for static info all NPCs should know
- Add time since last conversation to NPC prompt
- Add crosshair targeting option to bypass LLM-determined speech target when looking at an NPC (faster responses)
- Fix memory graphs not updating (requires "Clear All Memories" then "Migrate All NPCs")
- Fix "Clear Memories" crash
- Memory LLMs respect Responses API setting for OpenAI proxies
- Add setup confirmation modal and bottom bar for setup incomplete warning
- Web configuration validates API key formats with better warnings and hints
- Fix API key change issue and day of week display
- Fix chat hotkey binding
- Add 4x Tempus option and better hints
- Faster LLM responses (https keepalive, streaming sentences directly into websocket)
- Performance improvements
- Reduce server crashes and fix "worker process not available" error
- Fix game date & time storage inconsistency
- Improve Parakeet speech detection for short utterances
- Preserve "Ominis" and "Deek" in text correction and Deep gram STT
- Fix vision agent when using Open Mic
- NPCs use instant rotation lock outside of Hogwarts to avoid nav-mesh issues
- Fix local LM with OpenAI provider requiring API key
- Skip interjection LLM call when only last speaker is nearby
- Subtitles for AI chat show each sentence at a time as spoken
- Pocket TTS streams sentences from LLM into generator for lower latency
- NPCs begin speaking before LLM finishes (big speed up for slow AI or long responses!)
- Add Narrator that speaks in third-person between NPC dialogue (default OFF in Conversation -> Experimental)
- Fixed vision to work on multi-monitor setups

**1.0.5**
- Fix Blueprint cache issue (caused Spell Cast and Lipsync bugs)
- Fix OpenAI provider "reasoning" errors
- Fix "Max Output Tokens" setting not being respected
- Fix player TTS aborting
- Fix Director Mode using OpenRouter-only model
- Increase hardcoded max_tokens values to support reasoning models
- Change OpenAI default model to "gpt-4.1-nano" for text correction and reranker (faster)
- Fix Inworld TTS workspace bug
- Fix HTML rendering bug in configuration page
- Change "OpenRouter" to recommended provider
- Add OpenAI provider "Responses API" toggle for proxy compatibility
- Fix "Setup Incomplete" warning when using non-English language
- Update "Test LLM" to support input correction and memory models
- Update "Reset Defaults" to support new fields
- Update Sebastian default TTS temperature to +0.2
- Add warning in Setup section when Subtitles are off
- Add TTS pronunciation settings with "Accio" to "Akeeyo"
- Change "Voice Player Messages" to work for speech input
- Fix rare bug where NPCs remain muted after conversation
- Fix NPCs stopping when in follow mission
- Remove wav2vec2 for Pocket TTS in favor of amplitude-based lipsync (faster)
- Remove Pocket TTS temperature setting (sounds best at default 0.7)
- Change player TTS to use 3D spatialization (toggleable)
- Speed up player TTS time to audio playback
- Add local Speech-to-Text option (Parakeet)
- Fix voice casting wrong spell
- Fix Open Mic turn detection

**1.0.4**
- Add "Director" input - allows prompting conversations between NPCs by holding the chat hotkey
- Add Mode hotkey (default Home) for toggling between Default, Continuous (no turn limit), and 1-to-1 (no NPCs can interject)
- Add prompt editor for Target and Interjection agents
- Consolidated agent settings into one section
- Fix Interjection prompt to better pass turn to player when they are asked questions or given requests
- Extended range for companions to chat while on brooms (Floo Companions Mod)
- Reduced max TTS temperature and added clamping to prevent errors
- Fix "Reset Defaults" to include all setting fields
- Fix voice deduplication for Inworld TTS
- NPC actions "Follow" and "StopFollowing" renamed to "JoinAsCompanion" and "LeaveCompanion"
- NPCs less likely to walk away when AI has decided to include them in a conversation
- Spells are recorded in dialogue history only when out of combat (for RP purposes)
- Fix companions stop talking when entering new location
- Fix "Imelda Reyes" NPC issues
- Fix parenthesis text in player messages being read aloud by TTS
- Roll back keyboard/mouse lock during typing due to map navigation issues
- Disable "NPC Actions" JoinAsCompanion/LeaveCompanion when Floo Companions installed

**1.0.3**
- More immersive 3D sound (HRTF) with dynamic environmental reverb
- Add long-term NPC memory using a dynamic knowledge graph (toggleable)
- Add day/night time controls (default: 3× day speed, 3× night speed)
- NPCs will now speak the same language that your game is set to
- Add voice clone manifest for German, Portuguese, Spanish, French + a tool to build your own
- Interrupting NPCs now trims their dialogue to what was actually spoken
- Cutscenes now reliably interrupt ongoing conversations
- Open Mic mode for hands-free conversation
- Pocket TTS - Local voice cloning option (runs on CPU, English only for now)
- NPCs you're looking at will pay attention when you start talking or typing
- Add option to disable TTS (subtitles with lip sync)
- Unnamed/generic NPCs are now excluded from the mod's processes
- Add combat stats to dialogue history - defeated NPCs, damage dealt
- NPCs behind walls are excluded from earshot and interjections
- Seated NPCs should no longer get up to converse
- Add support for "Hogwarts House Points" mod - NPCs know stats + Teachers can award/demerit
- Fixed many lip sync bugs and improved performance
- Allows speech transcription in combat for voice spell casting
- Companions no longer need to stop moving to talk
- Companion's repeated voice lines are muted outside of cutscenes (toggleable)
- Per-NPC TTS temperature/expressiveness settings in web configuration
- Improved AI prompts including better adherence to game language
- Reduced latency before player begins to speak (when player voice is enabled)
- Migration from JSON to SQLite dialogue database for scalability and atomicity
- Fixed foreground window detection (removed dependencies)
- Import/Export dialogue now works per-NPC
- Server exits quicker after game closes
- Enable/Disable toggle for mod on the configuration page

**1.0.2**
- Improved performance
- Conversation history now editable on web configuration page
- Cast spells with voice commands by saying the name of any unlocked spell (toggleable)
- NPCs can now see a list of your gear and transmogs + descriptions (toggleable)
- NPCs know when you are invisible (disillusionment charm) or swimming, and companions know when they are as well
- Stealth reduces earshot range - fewer NPCs will overhear or interject
- Companions now know your current mission (toggleable)
- AI conversations are now stopped/prevented during cutscenes or when the player has moved too far away
- NPC callout lines have reduced interference with lip sync in AI conversations
- Add server restart button on web configuration page
- More reliable conversation interruption
- Fixed vision not working based on fullscreen/windowed mode
- Fixed inconsistent rendering of chat input
- Fixed issue where lips sometimes do not fully close after speaking

**1.0.1**
- Fixed bugs
- Slimmed down included dependencies

**1.0.0**
- Initial release

---

## **FAQ/Troubleshooting**

Read the FAQ below and check the Known Issues section above. If you still need help, reach out on the [Sonorus Discord](https://discord.gg/YXhJy3pA7b) or post in Posts/Bugs on the Nexus Mods page.

**1.** Open Mic is not picking up my voice.  
**a)** Try decreasing the threshold for VAD in settings, or increasing your mic volume.

**2.** My language is not supported for voice extraction.  
**a)** You can use the Voice Manager (/voice-manager/) to extract your own voice references, or request a language in the Discord.

**3.** NPC is glitched/not moving/mouth stuck open.  
**a)** Press the "Stop Conversation" button that you mapped in input - this often fixes NPCs.

**4.** How do I back up my Sonorus related data?  
**a)** The `Phoenix\Binaries\Win64\sonorus\data` folder contains all dialogue history, graph memory, and settings. Copy this folder somewhere secure to back it up. Alternatively, `uninstall_sonorus.bat` will offer to back up your data before removing the mod.

**5.** How do I use the hotkeys with my controller?  
**a)** In Steam input settings you can set controller buttons to simulate pressing your keyboard or mouse.

**6.** How do I talk to unnamed NPCs like my Hippogriff? Or add support for custom NPCs?  
**a)** Simply add a "`[NPCId]_reference_15s.wav`" to voice_references/, and for languages other than English use voice_references/[lang_code]/ such as "/de_de/". Example: voice_references/de_de/Hippogriff_reference_15s.wav

**7.** How do I disable the mod?  
**a)** There is a toggle in Server section of the mod configuration page.

**8.** I hit Gemini free tier quota. What do I do?  
**a)** OpenRouter has a minimum $5 deposit and it often goes a long way. Otherwise, you can wait until the next day when your rate limit resets. Gemini Pro and other subscriptions that do not include an API are not supported.

**9.** The Sonorus server or configuration page does not open when I start the game.  
**a)** Try re-running `install_sonorus.bat` to ensure everything was installed correctly. If the issue persists, delete the `ue4ss` folder and `dwmapi.dll` from your Phoenix/Binaries/Win64 directory and run the installer again.

**10.** Can I use a local LLM or a subscription that includes an API like NanoGPT?  
**a)** Yes, as long as you have an OpenAI compatible API for it. Set the provider to OpenAI and add your own endpoint URL. If you're using a local model, most smaller LLMs (<70B params) do not have enough world knowledge to portray the characters accurately. Gemma 3 27B (or upcoming Gemma 4) seem promising, but have not been tested with the mod.

**11.** How do I roleplay scenarios or direct NPC conversations?  
**a)** Use Director mode by holding the chat hotkey, or say "direct ..." followed by your prompt, to prompt conversations between NPCs - for example, "Sebastian and Ominis discuss the Undercroft". You can also type parenthesized prompts in normal chat for lighter direction. Both require the NPCs to be nearby - does not move or spawn NPCs.

**12.** My chat message turns to "I can't produce text like this..."  
**a)** The default text correction model is fairly censorious even for mild swearing - disable it or change it to another - it's okay for most players though.

**13.** How do I speed up the responses from NPCs?  
**a)** The first time you talk to an NPC it will perform voice cloning and that can take up to 10-20 seconds. Keep the default settings for the LLM providers as they're already optimized for high intelligence with low latency. Ensure that you have not turned on "Thinking" for any of the conversation related LLMs including vision, chat model, text correction, target, interjection, etc. Enable "Use Crosshair Targeting" to bypass the the LLM target selection when looking at an NPC - this can noticeably speed up responses. Push to talk is faster than open mic as it doesn't need to wait to confirm that you have finished speaking. If local speech recognition (Canary/Parakeet/Moonshine) feels slow, try switching to Deepgram which offloads transcription to the cloud. The memory system also performs a query for relevant memories, and disabling this can offer a mild speed increase (<1s gain on average).

**14.** Pocket TTS hitches/stutters.  
**a)** This often means your CPU is struggling to generate real time audio. This feature requires a mid-range or better CPU to stream in real time. You can turn off "Streaming" in TTS settings to fix this at the cost of waiting longer before NPCs respond.

**15.** NPCs do not lipsync to the audio.  
**a)** If this happens you can find a "Lipsync Fallback" option in Audio section that should fix it. This rare bug seems to occur due to a mod incompatibility and certain conditions.

**16.** Voice spells keep getting transcribed wrong when I talk.  
**a)** Try switching to Deepgram for speech recognition - it has better spell keyword detection compared to the local options.

**17.** I am having trouble with voice clones on Inworld after upgrading.  
**a)** Create a new workspace ID on Inworld and set it in the Sonorus TTS settings.

---

## **Credits**

Special thanks to the original [ConvAI mod author](https://github.com/Conv-AI/Convai-Modding), whose work on AI integration for Hogwarts Legacy helped me understand the blueprint and Lua systems needed to make this possible.

Thanks to [Dekita](https://www.nexusmods.com/profile/Dekita) for valuable advice when I needed it.

Thank you to [Skytaks](https://www.nexusmods.com/profile/skytaks) who spent hours with me testing and providing valuable feedback.

**Speech recognition:**

* [NVIDIA Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
* [ONNX Version](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx)
* [NVIDIA Canary 180M Flash](https://huggingface.co/nvidia/canary-180m-flash)
* [ONNX Version](https://huggingface.co/istupakov/canary-180m-flash-onnx)
* [Moonshine](https://huggingface.co/UsefulSensors/moonshine)
* [ONNX Version](https://huggingface.co/onnx-community/moonshine-base-ONNX)

**Voice synthesis:**

* [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts)
* [ONNX](https://huggingface.co/KevinAHM/pocket-tts-onnx)

---

## **Open Source**

The code is open for anyone to use, modify, or build upon. No attribution required, though always nice. Respect any licensing from third-party tools and scripts included in the mod.