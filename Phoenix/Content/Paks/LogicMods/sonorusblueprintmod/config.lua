-- Override the default asset path for sonorusblueprintmod
-- BPModLoader defaults to /Game/Mods/<modname>/ModActor
-- But this pak was built with assets at /sonorusblueprintmod/ModActor
if Mods then
    Mods["sonorusblueprintmod"] = {
        AssetPath = "/sonorusblueprintmod/ModActor",
        AssetName = "ModActor_C"
    }
end