"""Resolve natural prose subjects for character-specific narration prompts."""

NARRATION_SUBJECT_OVERRIDES = {
    "EN_US": {
        # Professors: the localized display title is the natural prose subject.
        "AbrahamRonen": "Professor Ronen",
        "AesopSharp": "Professor Sharp",
        "BaiHowin": "Professor Howin",
        "CuthbertBinns": "Professor Binns",
        "DinahHecat": "Professor Hecat",
        "EleazarFig": "Professor Fig",
        "MatildaWeasley": "Professor Weasley",
        "MirabelGarlick": "Professor Garlick",
        "MudiwaOnai": "Professor Onai",
        "PhineasBlack": "Professor Black",
        "SatyavatiShah": "Professor Shah",

        # Other titled named characters.
        "SirCadogan": "Sir Cadogan",
        "SirNestorAmset": "Sir Nestor Amset",
        "SirPatrickDelaneyPodmore": "Sir Patrick Delaney-Podmore",
        "SirWensleyDollamott": "Sir Wensley Dollamott",

        # Named identities whose first display-name token is not the right subject.
        "BloodyBaron": "The Bloody Baron",
        "FatFriar": "The Fat Friar",
        "FatLady": "The Fat Lady",
        "GreyLady": "The Grey Lady",
        "HerbertFleming": "Herbert",
        "NearlyHeadlessNick": "Nick",
        "SortingHat": "The Sorting Hat",

        # Unnamed characters and speaking entities.
        "Bully1": "The Slytherin student",
        "Centaur1": "The centaur",
        "Centaur2": "The centaur",
        "ExtraGhostCharacterM2": "The ghost",
        "FGMKitchenF": "The kitchen house-elf",
        "FGMKitchenM": "The kitchen house-elf",
        "GreyCat": "The cat",
        "MagicalWell": "The Magical Well",
        "PrivateGoblinBanker": "The goblin banker",
        "RavenclawEntranceKnocker": "The Ravenclaw Entrance Knocker",
        "StaffroomGargoyleB": "The gargoyle",
        "TalkingMirror": "The Talking Mirror",
        "TownCrier": "The Town Crier",

        # Game-ID aliases whose English localization is absent.
        "HOG_Sanctum_Guardian5y11": "Isidora",
        "HOG_Sanctum_Guardian5y17": "Isidora",

        # Shop labels are possessive business names, not character first names.
        "VENDORCauldronShop": "The shopkeeper",
        "VENDORJokeShop": "The shopkeeper",
        "VENDORMusicShop": "The shopkeeper",
        "VENDORTeaShop": "The shopkeeper",
    },
}

_CASEFOLDED_OVERRIDES = {
    language: {npc_id.casefold(): subject for npc_id, subject in overrides.items()}
    for language, overrides in NARRATION_SUBJECT_OVERRIDES.items()
}


def get_narration_subject(npc_id, display_name, language="EN_US"):
    """Return an EN_US prose subject, or ``None`` for unsupported languages.

    Explicit mappings are keyed only by stable NPC ID. Unmapped English names
    use their localized first token deterministically.
    """
    language_key = str(language or "").strip().upper()
    overrides = _CASEFOLDED_OVERRIDES.get(language_key)
    if overrides is None:
        return None

    npc_key = str(npc_id or "").strip().casefold()
    if npc_key and npc_key in overrides:
        return overrides[npc_key]

    localized_name = str(display_name or "").strip()
    if not localized_name:
        return None
    return localized_name.split(maxsplit=1)[0]
