"""
Reverb Presets for OpenAL EFX

Maps game Wwise AuxBus names to OpenAL EFX reverb parameters.
Uses AL_EFFECT_EAXREVERB for advanced control.

Parameters based on EAX 2.0/3.0 presets tuned for each environment type.
"""

# ============================================
# EFX Reverb Parameter Presets
# ============================================
# Each preset is a dict of AL_EAXREVERB_* parameters
# Values based on standard EAX presets, tweaked for environment

REVERB_PRESETS = {
    # ----------------------------------------
    # OUTDOOR ENVIRONMENTS
    # ----------------------------------------

    "OutdoorOverland": {
        # Open field/plains - minimal reverb, just air
        "density": 1.0,
        "diffusion": 0.3,
        "gain": 0.316,           # -10dB
        "gain_hf": 0.631,        # -4dB
        "gain_lf": 1.0,
        "decay_time": 1.5,
        "decay_hf_ratio": 0.6,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.050,
        "reflections_delay": 0.069,
        "late_reverb_gain": 0.050,
        "late_reverb_delay": 0.019,
        "echo_time": 0.25,
        "echo_depth": 0.0,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorConvolution": {
        # Town/village streets - moderate reflections from buildings
        "density": 1.0,
        "diffusion": 0.79,
        "gain": 0.316,
        "gain_hf": 0.794,
        "gain_lf": 1.0,
        "decay_time": 1.79,
        "decay_hf_ratio": 0.56,
        "decay_lf_ratio": 0.87,
        "reflections_gain": 0.251,
        "reflections_delay": 0.046,
        "late_reverb_gain": 0.126,
        "late_reverb_delay": 0.028,
        "echo_time": 0.125,
        "echo_depth": 0.35,       # Slight echo from buildings
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorForestMedium": {
        # Forest - diffuse, absorbed by trees
        "density": 1.0,
        "diffusion": 0.69,
        "gain": 0.316,
        "gain_hf": 0.447,        # Trees absorb highs
        "gain_lf": 1.0,
        "decay_time": 1.49,
        "decay_hf_ratio": 0.54,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.028,
        "reflections_delay": 0.162,
        "late_reverb_gain": 0.089,
        "late_reverb_delay": 0.088,
        "echo_time": 0.125,
        "echo_depth": 0.0,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 4705.0,
        "lf_reference": 99.6,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorRuralAir": {
        # Similar to forest but more open
        "density": 0.5,
        "diffusion": 0.5,
        "gain": 0.316,
        "gain_hf": 0.562,
        "gain_lf": 1.0,
        "decay_time": 1.3,
        "decay_hf_ratio": 0.65,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.035,
        "reflections_delay": 0.15,
        "late_reverb_gain": 0.056,
        "late_reverb_delay": 0.08,
        "echo_time": 0.25,
        "echo_depth": 0.0,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorMountains": {
        # Mountains - long decay, distant reflections
        "density": 0.27,
        "diffusion": 1.0,
        "gain": 0.316,
        "gain_hf": 0.562,
        "gain_lf": 1.0,
        "decay_time": 4.0,
        "decay_hf_ratio": 0.21,
        "decay_lf_ratio": 0.83,
        "reflections_gain": 0.040,
        "reflections_delay": 0.30,   # Distant reflections
        "late_reverb_gain": 0.114,
        "late_reverb_delay": 0.10,
        "echo_time": 0.25,
        "echo_depth": 1.0,           # Strong mountain echo
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.97,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorHills": {
        # Hills - moderate distance, some echo
        "density": 0.5,
        "diffusion": 0.75,
        "gain": 0.316,
        "gain_hf": 0.631,
        "gain_lf": 1.0,
        "decay_time": 2.5,
        "decay_hf_ratio": 0.45,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.063,
        "reflections_delay": 0.20,
        "late_reverb_gain": 0.100,
        "late_reverb_delay": 0.075,
        "echo_time": 0.18,
        "echo_depth": 0.5,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.98,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorCanyonMedium": {
        # Canyon - strong walls, echo
        "density": 0.5,
        "diffusion": 0.8,
        "gain": 0.398,           # Slightly louder
        "gain_hf": 0.794,
        "gain_lf": 1.0,
        "decay_time": 3.0,
        "decay_hf_ratio": 0.55,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.251,   # Strong early reflections
        "reflections_delay": 0.10,
        "late_reverb_gain": 0.178,
        "late_reverb_delay": 0.05,
        "echo_time": 0.20,
        "echo_depth": 0.85,          # Strong canyon echo
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorCavern": {
        # Open cave/cavern - large, resonant
        "density": 1.0,
        "diffusion": 0.9,
        "gain": 0.446,
        "gain_hf": 0.631,
        "gain_lf": 1.0,
        "decay_time": 3.9,
        "decay_hf_ratio": 0.79,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.316,
        "reflections_delay": 0.025,
        "late_reverb_gain": 0.251,
        "late_reverb_delay": 0.034,
        "echo_time": 0.125,
        "echo_depth": 0.7,
        "modulation_time": 0.25,
        "modulation_depth": 0.08,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorCanyonWalls": {
        # Close canyon walls - tighter than medium
        "density": 0.7,
        "diffusion": 0.85,
        "gain": 0.398,
        "gain_hf": 0.891,
        "gain_lf": 1.0,
        "decay_time": 2.5,
        "decay_hf_ratio": 0.65,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.316,
        "reflections_delay": 0.07,
        "late_reverb_gain": 0.200,
        "late_reverb_delay": 0.04,
        "echo_time": 0.15,
        "echo_depth": 0.75,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorCliffside": {
        # Cliff edge - one-sided reflections
        "density": 0.4,
        "diffusion": 0.6,
        "gain": 0.316,
        "gain_hf": 0.708,
        "gain_lf": 1.0,
        "decay_time": 2.0,
        "decay_hf_ratio": 0.5,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.112,
        "reflections_delay": 0.15,
        "late_reverb_gain": 0.126,
        "late_reverb_delay": 0.06,
        "echo_time": 0.22,
        "echo_depth": 0.6,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorBridge": {
        # Stone bridge - enclosed overhead, open sides
        "density": 0.6,
        "diffusion": 0.7,
        "gain": 0.355,
        "gain_hf": 0.794,
        "gain_lf": 1.0,
        "decay_time": 1.8,
        "decay_hf_ratio": 0.6,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.200,
        "reflections_delay": 0.04,
        "late_reverb_gain": 0.158,
        "late_reverb_delay": 0.03,
        "echo_time": 0.1,
        "echo_depth": 0.4,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorCourtyardWide": {
        # Large courtyard - enclosed but open sky
        "density": 0.8,
        "diffusion": 0.82,
        "gain": 0.316,
        "gain_hf": 0.708,
        "gain_lf": 1.0,
        "decay_time": 2.2,
        "decay_hf_ratio": 0.55,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.178,
        "reflections_delay": 0.065,
        "late_reverb_gain": 0.141,
        "late_reverb_delay": 0.04,
        "echo_time": 0.125,
        "echo_depth": 0.45,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorCourtyardSlapback": {
        # Courtyard with strong early reflections (slapback)
        "density": 0.8,
        "diffusion": 0.6,
        "gain": 0.355,
        "gain_hf": 0.794,
        "gain_lf": 1.0,
        "decay_time": 1.4,
        "decay_hf_ratio": 0.7,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.398,   # Strong slap
        "reflections_delay": 0.035,  # Quick return
        "late_reverb_gain": 0.089,
        "late_reverb_delay": 0.025,
        "echo_time": 0.08,
        "echo_depth": 0.3,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "OutdoorCastleStone": {
        # Castle exterior - large stone surfaces
        "density": 0.9,
        "diffusion": 0.78,
        "gain": 0.355,
        "gain_hf": 0.891,
        "gain_lf": 1.0,
        "decay_time": 2.8,
        "decay_hf_ratio": 0.64,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.282,
        "reflections_delay": 0.055,
        "late_reverb_gain": 0.178,
        "late_reverb_delay": 0.038,
        "echo_time": 0.15,
        "echo_depth": 0.5,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.99,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    # ----------------------------------------
    # INDOOR ENVIRONMENTS
    # ----------------------------------------

    "IndoorWoodSmall": {
        # Small wooden room (Honeydukes shops)
        "density": 1.0,
        "diffusion": 0.94,
        "gain": 0.398,
        "gain_hf": 0.631,        # Wood absorbs some highs
        "gain_lf": 1.0,
        "decay_time": 1.1,
        "decay_hf_ratio": 0.83,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.398,
        "reflections_delay": 0.012,  # Quick - small room
        "late_reverb_gain": 0.355,
        "late_reverb_delay": 0.012,
        "echo_time": 0.125,
        "echo_depth": 0.0,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5168.6,
        "lf_reference": 139.5,
        "room_rolloff_factor": 0.0,
    },

    "IndoorWoodMedium": {
        # Medium wooden room
        "density": 1.0,
        "diffusion": 0.88,
        "gain": 0.398,
        "gain_hf": 0.562,
        "gain_lf": 1.0,
        "decay_time": 1.47,
        "decay_hf_ratio": 0.79,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.316,
        "reflections_delay": 0.020,
        "late_reverb_gain": 0.282,
        "late_reverb_delay": 0.024,
        "echo_time": 0.125,
        "echo_depth": 0.0,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5168.6,
        "lf_reference": 139.5,
        "room_rolloff_factor": 0.0,
    },

    "IndoorCave": {
        # Cave interior - larger than cavern rooms
        "density": 1.0,
        "diffusion": 0.76,
        "gain": 0.501,
        "gain_hf": 0.794,
        "gain_lf": 1.0,
        "decay_time": 2.91,
        "decay_hf_ratio": 0.8,
        "decay_lf_ratio": 0.95,
        "reflections_gain": 0.446,
        "reflections_delay": 0.015,
        "late_reverb_gain": 0.398,
        "late_reverb_delay": 0.022,
        "echo_time": 0.125,
        "echo_depth": 0.5,
        "modulation_time": 0.25,
        "modulation_depth": 0.05,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorDungeonStone": {
        # Stone dungeon - hard surfaces, long decay
        "density": 1.0,
        "diffusion": 0.79,
        "gain": 0.501,
        "gain_hf": 0.891,        # Stone reflects highs
        "gain_lf": 1.0,
        "decay_time": 2.81,
        "decay_hf_ratio": 0.9,
        "decay_lf_ratio": 0.95,
        "reflections_gain": 0.446,
        "reflections_delay": 0.014,
        "late_reverb_gain": 0.355,
        "late_reverb_delay": 0.021,
        "echo_time": 0.125,
        "echo_depth": 0.25,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorMediumRoom": {
        # Generic medium indoor room
        "density": 1.0,
        "diffusion": 0.83,
        "gain": 0.398,
        "gain_hf": 0.708,
        "gain_lf": 1.0,
        "decay_time": 1.3,
        "decay_hf_ratio": 0.83,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.355,
        "reflections_delay": 0.015,
        "late_reverb_gain": 0.282,
        "late_reverb_delay": 0.018,
        "echo_time": 0.125,
        "echo_depth": 0.0,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorHallwaySmall": {
        # Small hallway/corridor
        "density": 1.0,
        "diffusion": 0.75,
        "gain": 0.446,
        "gain_hf": 0.794,
        "gain_lf": 1.0,
        "decay_time": 1.49,
        "decay_hf_ratio": 0.86,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.501,
        "reflections_delay": 0.007,  # Very quick - narrow
        "late_reverb_gain": 0.316,
        "late_reverb_delay": 0.011,
        "echo_time": 0.125,
        "echo_depth": 0.1,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorHallwayLarge": {
        # Large hallway/corridor
        "density": 1.0,
        "diffusion": 0.82,
        "gain": 0.446,
        "gain_hf": 0.794,
        "gain_lf": 1.0,
        "decay_time": 2.1,
        "decay_hf_ratio": 0.83,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.446,
        "reflections_delay": 0.012,
        "late_reverb_gain": 0.282,
        "late_reverb_delay": 0.016,
        "echo_time": 0.125,
        "echo_depth": 0.2,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorTunnelLong": {
        # Long tunnel - extended decay, strong echoes
        "density": 1.0,
        "diffusion": 0.65,
        "gain": 0.501,
        "gain_hf": 0.891,
        "gain_lf": 1.0,
        "decay_time": 3.5,
        "decay_hf_ratio": 0.85,
        "decay_lf_ratio": 0.95,
        "reflections_gain": 0.562,
        "reflections_delay": 0.018,
        "late_reverb_gain": 0.398,
        "late_reverb_delay": 0.025,
        "echo_time": 0.2,
        "echo_depth": 0.7,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorCoastRuinSmall": {
        # Small coastal ruin - damp, eroded stone
        "density": 0.9,
        "diffusion": 0.72,
        "gain": 0.398,
        "gain_hf": 0.562,
        "gain_lf": 1.0,
        "decay_time": 1.8,
        "decay_hf_ratio": 0.65,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.316,
        "reflections_delay": 0.018,
        "late_reverb_gain": 0.251,
        "late_reverb_delay": 0.024,
        "echo_time": 0.125,
        "echo_depth": 0.15,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.98,
        "hf_reference": 4500.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorCoastRuinMedium": {
        # Medium coastal ruin
        "density": 0.85,
        "diffusion": 0.75,
        "gain": 0.398,
        "gain_hf": 0.562,
        "gain_lf": 1.0,
        "decay_time": 2.3,
        "decay_hf_ratio": 0.62,
        "decay_lf_ratio": 0.9,
        "reflections_gain": 0.282,
        "reflections_delay": 0.025,
        "late_reverb_gain": 0.224,
        "late_reverb_delay": 0.032,
        "echo_time": 0.15,
        "echo_depth": 0.25,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.98,
        "hf_reference": 4500.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorCoastRuinLarge": {
        # Large coastal ruin
        "density": 0.8,
        "diffusion": 0.78,
        "gain": 0.398,
        "gain_hf": 0.501,
        "gain_lf": 1.0,
        "decay_time": 2.9,
        "decay_hf_ratio": 0.58,
        "decay_lf_ratio": 0.88,
        "reflections_gain": 0.251,
        "reflections_delay": 0.035,
        "late_reverb_gain": 0.200,
        "late_reverb_delay": 0.045,
        "echo_time": 0.18,
        "echo_depth": 0.35,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.98,
        "hf_reference": 4500.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorHogwartsSmall": {
        # Hogwarts small room - magic stone/wood mix
        "density": 1.0,
        "diffusion": 0.87,
        "gain": 0.398,
        "gain_hf": 0.708,
        "gain_lf": 1.0,
        "decay_time": 1.2,
        "decay_hf_ratio": 0.82,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.355,
        "reflections_delay": 0.013,
        "late_reverb_gain": 0.316,
        "late_reverb_delay": 0.015,
        "echo_time": 0.125,
        "echo_depth": 0.0,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorHogwartsMedium": {
        # Hogwarts medium room (classrooms)
        "density": 1.0,
        "diffusion": 0.85,
        "gain": 0.398,
        "gain_hf": 0.708,
        "gain_lf": 1.0,
        "decay_time": 1.6,
        "decay_hf_ratio": 0.8,
        "decay_lf_ratio": 1.0,
        "reflections_gain": 0.316,
        "reflections_delay": 0.018,
        "late_reverb_gain": 0.282,
        "late_reverb_delay": 0.022,
        "echo_time": 0.125,
        "echo_depth": 0.1,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },

    "IndoorHogwartsLarge": {
        # Hogwarts large room (Great Hall)
        "density": 1.0,
        "diffusion": 0.82,
        "gain": 0.446,
        "gain_hf": 0.708,
        "gain_lf": 1.0,
        "decay_time": 2.4,
        "decay_hf_ratio": 0.78,
        "decay_lf_ratio": 0.95,
        "reflections_gain": 0.282,
        "reflections_delay": 0.030,
        "late_reverb_gain": 0.251,
        "late_reverb_delay": 0.038,
        "echo_time": 0.15,
        "echo_depth": 0.25,
        "modulation_time": 0.25,
        "modulation_depth": 0.0,
        "air_absorption_gain_hf": 0.994,
        "hf_reference": 5000.0,
        "lf_reference": 250.0,
        "room_rolloff_factor": 0.0,
    },
}


# ============================================
# AuxBus Name Mapping
# ============================================
# Maps game's AuxBus names to our preset names
# Some game names don't match exactly, so we map them

AUXBUS_TO_PRESET = {
    # Direct matches
    "OutdoorOverland": "OutdoorOverland",
    "OutdoorConvolution": "OutdoorConvolution",
    "OutdoorForestMedium": "OutdoorForestMedium",
    "OutdoorRuralAir": "OutdoorRuralAir",
    "OutdoorMountains": "OutdoorMountains",
    "OutdoorHills": "OutdoorHills",
    "OutdoorCanyonMedium": "OutdoorCanyonMedium",
    "OutdoorCanyonWalls": "OutdoorCanyonWalls",
    "OutdoorCavern": "OutdoorCavern",
    "OutdoorCliffside": "OutdoorCliffside",
    "OutdoorBridge": "OutdoorBridge",
    "OutdoorCourtyardWide": "OutdoorCourtyardWide",
    "OutdoorCourtyardSlapback": "OutdoorCourtyardSlapback",
    "OutdoorCastleStone": "OutdoorCastleStone",

    "IndoorWoodSmall": "IndoorWoodSmall",
    "IndoorWoodMedium": "IndoorWoodMedium",
    "IndoorCave": "IndoorCave",
    "IndoorDungeonStone": "IndoorDungeonStone",
    "IndoorMediumRoom": "IndoorMediumRoom",
    "IndoorHallwaySmall": "IndoorHallwaySmall",
    "IndoorHallwayLarge": "IndoorHallwayLarge",
    "IndoorTunnelLong": "IndoorTunnelLong",
    "IndoorCoastRuinSmall": "IndoorCoastRuinSmall",
    "IndoorCoastRuinMedium": "IndoorCoastRuinMedium",
    "IndoorCoastRuinLarge": "IndoorCoastRuinLarge",
    "IndoorHogwartsSmall": "IndoorHogwartsSmall",
    "IndoorHogwartsMedium": "IndoorHogwartsMedium",
    "IndoorHogwartsLarge": "IndoorHogwartsLarge",

    # Aliases/variants that map to existing presets
    "IndoorHallwaySmall_3D_Portal": "IndoorHallwaySmall",
    "IndoorHallwayLarge_3D_Portal": "IndoorHallwayLarge",
    "IndoorCoastRuin_Small": "IndoorCoastRuinSmall",
    "IndoorCoastRuin_Medium": "IndoorCoastRuinMedium",
    "IndoorCoastRuin_Large": "IndoorCoastRuinLarge",

    # Canyon variants -> CanyonMedium (could be refined)
    "OutdoorCanyonWide": "OutdoorCanyonMedium",
}

# Fallback preset for unknown AuxBus
FALLBACK_PRESET = "OutdoorOverland"


def get_preset_for_auxbus(auxbus_name: str) -> dict:
    """
    Get reverb preset parameters for a given game AuxBus name.

    Args:
        auxbus_name: The AuxBus name from GetCurrentReverb() (e.g., "OutdoorConvolution")

    Returns:
        Dict of EFX reverb parameters
    """
    if not auxbus_name:
        return REVERB_PRESETS[FALLBACK_PRESET]

    # Check direct mapping
    preset_name = AUXBUS_TO_PRESET.get(auxbus_name)
    if preset_name and preset_name in REVERB_PRESETS:
        return REVERB_PRESETS[preset_name]

    # Try exact name match
    if auxbus_name in REVERB_PRESETS:
        return REVERB_PRESETS[auxbus_name]

    # Fallback
    print(f"[Reverb] Unknown AuxBus '{auxbus_name}', using fallback")
    return REVERB_PRESETS[FALLBACK_PRESET]


def get_preset_name_for_auxbus(auxbus_name: str) -> str:
    """Get preset name for logging/debugging."""
    if not auxbus_name:
        return FALLBACK_PRESET

    preset_name = AUXBUS_TO_PRESET.get(auxbus_name)
    if preset_name:
        return preset_name

    if auxbus_name in REVERB_PRESETS:
        return auxbus_name

    return FALLBACK_PRESET


def list_presets() -> list:
    """List all available preset names."""
    return list(REVERB_PRESETS.keys())
