# Sonorus - AI Conversations

### About Sonorus

Sonorus lets you have real conversations with any named character in Hogwarts Legacy. Using AI, NPCs respond dynamically to what you say, where you are, and what's happening around you - all with their original voices and synchronized lip movements.

Every character has their own personality. Professors remember they're your teachers. Students know which house you're in. Shopkeepers greet you differently at midnight than at noon. And when you're chatting with one character, others nearby might chime in with their own thoughts.

### Key Features

**Talk to Anyone**
Approach any named NPC in the game and start a conversation. Ask Professor Weasley about class, chat with Sebastian about the Undercroft, or get Deek's opinion on your latest adventure.

**Original Voices**
NPCs speak with voice-cloned versions of their original game voices. Combined with real-time lip sync, conversations feel natural and immersive.

**Context Awareness**
NPCs know:
- Your name and house
- The current time and date
- Their location (and yours)
- Who else is nearby
- What you're looking at (vision feature)

**Group Conversations**
When multiple NPCs are nearby, they can react to your conversations and speak to each other. Discussions can flow naturally between characters.

**Vision**
NPCs can "see" what you see through optional screenshot analysis. Comment on the weather, point out something interesting, or ask what that creature is - they'll understand.

### Completely Free to Use

Sonorus works entirely with free-tier AI services:

| Service | Purpose | Free Tier |
|---------|---------|-----------|
| **Gemini** | AI responses | Generous free credits |
| **InWorld** | Voice synthesis | Free tier available |
| **Deepgram** | Speech-to-text | $200 free credits |

No subscriptions required. No API costs for normal use.

### Installation

**Important: Disable any existing UE4SS installation first!** The version on Nexus Mods is outdated and crash-prone. Sonorus includes the latest experimental UE4SS build.

**Manual Installation:**
1. Extract the mod files
2. Copy contents to your game's `Phoenix` folder (overwrite when prompted)
3. Launch Hogwarts Legacy
4. Follow the setup wizard in your browser

The setup wizard will guide you through:
- Getting your free API keys
- Testing your configuration
- Customizing character voices

### Requirements

- Hogwarts Legacy (Steam version confirmed working)
- Windows PC
- Microphone (for voice chat) or keyboard (for text chat)
- Internet connection (for AI services)

### Compatibility

- **Steam**: Tested and working
- **Epic Games / Other**: Unknown - community testing appreciated!
- **Other UE4SS mods**: Should work, but disable other UE4SS installations first

### Known Issues

- NPCs with repeating quest callout lines (like Zenobia's Gobstones dialogue) may occasionally speak their ambient lines during AI conversations. A workaround mutes them during conversation, but it's not perfect. If you have solutions, please share!

### Experimental Features

Some features are marked experimental in the settings. Use at your own risk - especially anything that affects NPC behavior beyond conversation. Back up your saves!

### Future Plans

- Long-term NPC memory system (remembering past conversations)
- Additional features based on community feedback and support

### Credits

Special thanks to the original [ConvAI mod author](https://github.com/Conv-AI/Convai-Modding), whose work on AI integration for Hogwarts Legacy helped me understand the blueprint and Lua systems needed to make this possible.

Thanks to [Dekita](https://www.nexusmods.com/profile/Dekita) for valuable advice when I needed it.

### Open Source

The code is open for anyone to use, modify, or build upon. No attribution required, though always nice. Respect any licensing from third-party tools and scripts included in the mod.
