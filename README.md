# **Sonorus – AI Conversations**

---

## **About Sonorus**

Sonorus lets you have real conversations with any named character in *Hogwarts Legacy*. Using AI, NPCs respond dynamically to what you say, where you are, and what's happening around you — all with their original voices and synchronized lip movements.

Every character has their own personality. Professors remember they're your teachers. Students know which house you're in. Shopkeepers greet you differently at midnight than at noon. And when you're chatting with one character, others nearby might chime in with their own thoughts.

---

## **Key Features**

### **Talk to Anyone**

Approach any named NPC in the game and start a conversation. Ask Professor Weasley about class, chat with Sebastian about the Undercroft, or get Deek's opinion on your latest adventure.

### **Original Voices**

NPCs speak with voice-cloned versions of their original game voices. Combined with real-time lip sync, conversations feel natural and immersive.

### **Context Awareness**

NPCs know:

* Your name and house
* The current time and date
* Their location (and yours)
* Who else is nearby
* Your gear and transmogs
* Whether you're invisible, in combat, on a broom, or swimming
* Your current mission
* What spells you've cast
* What you're looking at (vision feature)

### **Voice Spells**

Cast spells by speaking their names. Say **“Lumos”** to light your wand, **“Accio”** to summon objects, or any other unlocked spell. Can be toggled in settings.

### **Group Conversations**

When multiple NPCs are nearby, they can react to your conversations and speak to each other. Discussions can flow naturally between characters.

### **Realistic NPC Memory**

NPCs only remember conversations they actually witnessed. If Sebastian wasn’t nearby when you talked to Ominis, he won’t know about it — just like real life. This creates natural, believable interactions where characters have their own unique knowledge based on what they've experienced.

### **Vision**

NPCs can “see” what you see through optional screenshot analysis. Comment on the weather, point out something interesting, or ask what that creature is — they’ll understand.

---

## **Completely Free to Use**

Sonorus works entirely with free-tier AI services:

* **Gemini** — AI responses (Free tier)
* **InWorld** — Voice synthesis ($25 free credits)
* **Deepgram** — Speech-to-text ($200 free credits)

No subscriptions required. No API costs for normal use.

> **Note:** Gemini’s free tier has had rate limits reduced recently. If you hit daily limits, we recommend [OpenRouter](https://openrouter.ai/) — a $5 minimum deposit will last a long time.

---

## **Installation**

> ⚠️ **Important:** Remove any existing UE4SS installation first!
> At time of writing, the UE4SS version on Nexus Mods is out of date and will not work.

### **Step 1 – Install UE4SS**

1. Download the latest experimental UE4SS build:
   [UE4SS v3.0.1 (experimental)](https://github.com/UE4SS-RE/RE-UE4SS/releases/tag/experimental-latest)
2. Extract the zip contents (`ue4ss` folder and `dwmapi.dll`) into:

   ```
   Phoenix/Binaries/Win64
   ```

   Game directory (Steam):

   ```
   steamapps/common/Hogwarts Legacy
   ```

### **Step 2 – Install Sonorus**

1. Extract the mod’s `Phoenix` folder into your game directory
2. Replace files if prompted

### **Step 3 – Setup**

1. Launch *Hogwarts Legacy*
2. A setup window will open in the background — wait for it to complete
3. Your browser will open with the configuration wizard
4. Complete the wizard to configure API keys and settings
5. Enable **subtitles** in-game (required for chat input visibility)
6. Use the configured hotkeys to type or talk to NPCs

---

## **Requirements**

* Hogwarts Legacy (Steam version confirmed working)
* Windows PC
* Microphone (for voice chat) or keyboard (for text chat)
* Internet connection (for AI services)

---

## **Compatibility**

* **Steam:** Tested and working
* **Epic Games:** Tested and working
* **Ultra Plus:** Compatible

  * Run Ultra+ Manager *after* installing Sonorus
  * Select **“I maintain my own UE4SS”**
* **Other UE4SS mods:** Should work (remove other UE4SS installs first)
* **Emote With Any NPC:** Tested and working

  * Typing in chat may trigger some emotes

---

## **Known Issues**

* NPCs with repeating quest callout lines (e.g. Zenobia’s Gobstones dialogue) may occasionally speak ambient lines during AI conversations.

  * A workaround mutes them during conversation and stops native lip animations, but subtitles may still briefly appear and audio may occasionally be heard.
  * If you have solutions, please share!
* Subtitles must be enabled to see chat input.

---

## **Experimental Features**

Some features are marked experimental in settings. Use at your own risk — especially anything that affects NPC behavior beyond conversation.

⚠️ **Back up your saves!**

---

## **Future Plans**

* Extended memory system with periodic summarization for long playthroughs
* NPC emotes and improved conversation controls
* Radiant quests or NPC orchestration for more character agency (maybe!)
* Additional features based on community feedback

---

## **Changelog**

### **1.0.2**

* Improved performance
* Conversation history now editable on web configuration page
* Voice spell casting for any unlocked spell (toggleable)
* NPCs can see your gear and transmogs + descriptions (toggleable)
* NPCs know when you are invisible or swimming (companions included)
* Stealth reduces earshot range
* Companions know your current mission (toggleable)
* Conversations paused during cutscenes or when out of range
* Reduced interference from NPC callout lines
* Added server restart button
* More reliable conversation interruption
* Fixed vision issues related to fullscreen/windowed mode
* Fixed inconsistent chat input rendering
* Fixed lip-sync edge cases

### **1.0.1**

* Bug fixes
* Reduced bundled dependencies

### **1.0.0**

* Initial release

---

## **Credits**

* Original [ConvAI mod author](https://github.com/Conv-AI/Convai-Modding) for foundational AI integration work
* [Dekita](https://www.nexusmods.com/profile/Dekita) for valuable advice
* [Skytaks](https://www.nexusmods.com/profile/skytaks) for extensive testing and feedback

---

## **Open Source**

The code is open for anyone to use, modify, or build upon.

No attribution required (though always appreciated).
Please respect any third-party licenses included in the mod.