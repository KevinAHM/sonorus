"""Shared contextual terms for speech-recognition providers."""

CHARACTER_KEYTERMS = [
    "Garlick",
    "Professor Garlick",
    "Mirabel Garlick",
    "Ominis",
    "Deek",
    "Garreth",
]

SPELL_KEYTERMS = [
    "Avada Kedavra", "Wingardium Leviosa", "Arresto Momentum",
    "Petrificus Totalus", "Expecto Patronum", "Animagus Form", "Bat Bogey",
    "Fiend Fyre", "Levioso", "Accio", "Depulso", "Descendo", "Flipendo",
    "Glacius", "Incendio", "Confringo", "Diffindo", "Expelliarmus",
    "Expulso", "Crucio", "Imperio", "Stupefy", "Lumos", "Nox", "Reparo",
    "Revelio", "Protego", "Confundo", "Oppugno", "Obliviate", "Episkey",
    "Evanesco", "Conjuration", "Alohomora", "Reducio", "Reducto",
    "Apparition", "Bombarda",
]

LOCATION_KEYTERMS = ["Hogsmeade"]


def all_keyterms() -> list[str]:
    return list(dict.fromkeys(CHARACTER_KEYTERMS + SPELL_KEYTERMS + LOCATION_KEYTERMS))


ALL_KEYTERMS = all_keyterms()
