"""
ROM migration utility for the ROM Audit Tool.

Handles moving ROMs between RetroPie system folders when autofix
determines that a ROM works with a different MAME core version.

RetroPie organises MAME ROMs by core version via separate system
folders. Moving a ROM to the correct folder ensures it launches
with the right core automatically through EmulationStation.

Migration includes:
    1. Moving the ROM file to the target system folder
    2. Moving scraped assets (box art, video, etc.)
    3. Updating the source gamelist.xml (removing the entry)
    4. Updating the target gamelist.xml (adding the entry)
    5. Updating the audit CSV with the new system name

The user must restart EmulationStation after migration for changes
to take effect in the frontend.

RetroPie system folder → core mapping:
    arcade          lr-mame2003 (default)
    mame-libretro   lr-mame (current MAME)
    mame2000        lr-mame2000
    mame2003        lr-mame2003
    mame2003-plus   lr-mame2003-plus
    mame2010        lr-mame2010
    mame2014        lr-mame2014
    mame2016        lr-mame2016
"""

from __future__ import annotations  # Python 3.9 compatibility

import os
import shutil
import xml.etree.ElementTree as ET

from modules.common.logging import log


# ---------------------------------------------------------------------------
# Asset paths
# ---------------------------------------------------------------------------

ES_MEDIA_BASE   = "/home/pi/.emulationstation/downloaded_media"
ES_GAMELIST_BASE = "/home/pi/.emulationstation/gamelists"

ASSET_TYPES = [
    'box2dfront',
    'box2dback',
    'box2dside',
    'screenshot',
    'video',
    'fanart',
    'marquee',
    'titlescreen',
]

# Map from core display name to RetroPie system folder name
CORE_TO_SYSTEM = {
    'lr-mame2000':     'mame2000',
    'lr-mame2003':     'arcade',
    'lr-mame2003-plus':'mame2003-plus',
    'lr-mame2010':     'mame2010',
    'lr-mame2014':     'mame2014',
    'lr-mame2016':     'mame2016',
    'lr-mame':         'mame-libretro',
    'lr-fba':          'fba',
    'lr-fbalpha2012':  'fba',
    'lr-fbneo':        'fba',
}


# ---------------------------------------------------------------------------
# Gamelist utilities
# ---------------------------------------------------------------------------

def _gamelist_path(system: str) -> str:
    return os.path.join(ES_GAMELIST_BASE, system, 'gamelist.xml')


def _find_game_entry(tree_root, romname: str):
    """Find a game entry in a gamelist.xml by ROM filename."""
    for game in tree_root.findall('game'):
        path_elem = game.find('path')
        if path_elem is not None:
            if os.path.basename(path_elem.text or '') == romname:
                return game
    return None


def read_game_entry(system: str, romname: str) -> dict:
    """
    Read a game's metadata from the system's gamelist.xml.

    Args:
        system:  Source system folder name.
        romname: ROM filename e.g. 's1945a.zip'

    Returns:
        Dict of element tag → text for the game entry,
        or empty dict if not found.
    """
    gamelist = _gamelist_path(system)
    if not os.path.exists(gamelist):
        return {}
    try:
        tree = ET.parse(gamelist)
        game = _find_game_entry(tree.getroot(), romname)
        if game is None:
            return {}
        return {
            child.tag: (child.text or '')
            for child in game
        }
    except Exception as e:
        log(f"  Warning: could not read gamelist for [{system}]: {e}")
        return {}


def remove_game_from_gamelist(system: str, romname: str) -> bool:
    """
    Remove a game entry from a system's gamelist.xml.

    Args:
        system:  System folder name.
        romname: ROM filename.

    Returns:
        True if entry was found and removed.
    """
    gamelist = _gamelist_path(system)
    if not os.path.exists(gamelist):
        return False
    try:
        tree = ET.parse(gamelist)
        root = tree.getroot()
        game = _find_game_entry(root, romname)
        if game is None:
            return False
        root.remove(game)
        tree.write(gamelist, encoding='utf-8', xml_declaration=True)
        return True
    except Exception as e:
        log(f"  Warning: could not update gamelist for [{system}]: {e}")
        return False


def add_game_to_gamelist(
    system: str,
    romname: str,
    roms_path: str,
    metadata: dict
) -> bool:
    """
    Add a game entry to a system's gamelist.xml.

    Creates the gamelist if it does not exist. Updates the path
    element to point to the new system folder.

    Args:
        system:    Target system folder name.
        romname:   ROM filename.
        roms_path: Base ROMs path e.g. /home/pi/RetroPie/roms
        metadata:  Dict of element tag → text from the source gamelist.

    Returns:
        True if the entry was written successfully.
    """
    gamelist = _gamelist_path(system)
    os.makedirs(os.path.dirname(gamelist), exist_ok=True)

    try:
        if os.path.exists(gamelist):
            tree = ET.parse(gamelist)
            root = tree.getroot()
        else:
            root = ET.Element('gameList')
            tree = ET.ElementTree(root)

        game = ET.SubElement(root, 'game')
        new_path = os.path.join(roms_path, system, romname)

        # Write path element first
        path_elem = ET.SubElement(game, 'path')
        path_elem.text = new_path

        # Write remaining metadata, updating the path if present
        for tag, text in metadata.items():
            if tag == 'path':
                continue  # Already written
            elem = ET.SubElement(game, tag)
            elem.text = text

        tree.write(gamelist, encoding='utf-8', xml_declaration=True)
        return True
    except Exception as e:
        log(f"  Warning: could not update gamelist for [{system}]: {e}")
        return False


# ---------------------------------------------------------------------------
# Asset migration
# ---------------------------------------------------------------------------

def move_assets(
    source_system: str,
    target_system: str,
    romname: str
) -> list[str]:
    """
    Move scraped media assets from source to target system folder.

    Looks for assets in all standard EmulationStation media directories.
    Moves any that exist, skips missing ones silently.

    Args:
        source_system: Source system folder name.
        target_system: Target system folder name.
        romname:       ROM filename (assets use the basename without extension).

    Returns:
        List of asset types successfully moved.
    """
    rom_base = os.path.splitext(romname)[0]
    moved    = []

    for asset_type in ASSET_TYPES:
        src_dir  = os.path.join(ES_MEDIA_BASE, source_system, asset_type)
        dst_dir  = os.path.join(ES_MEDIA_BASE, target_system, asset_type)

        # Assets can have various extensions
        if not os.path.isdir(src_dir):
            continue

        for filename in os.listdir(src_dir):
            if os.path.splitext(filename)[0] == rom_base:
                src_file = os.path.join(src_dir, filename)
                os.makedirs(dst_dir, exist_ok=True)
                dst_file = os.path.join(dst_dir, filename)
                try:
                    shutil.move(src_file, dst_file)
                    moved.append(asset_type)
                except Exception as e:
                    log(f"  Warning: could not move {asset_type} asset: {e}")

    return moved


# ---------------------------------------------------------------------------
# Main migration function
# ---------------------------------------------------------------------------

def migrate_rom(
    source_system: str,
    romname: str,
    target_core: str,
    roms_path: str,
    results_csv: str,
    already_tested: dict,
    dry_run: bool = False
) -> bool:
    """
    Migrate a ROM to the correct system folder for its working core.

    Performs the full migration:
        1. Determines target system folder from core name
        2. Moves the ROM file
        3. Moves scraped assets
        4. Updates source and target gamelist.xml files
        5. Updates the audit CSV

    Args:
        source_system:  Current system folder name (e.g. 'arcade')
        romname:        ROM filename (e.g. 's1945a.zip')
        target_core:    Core display name that works (e.g. 'lr-mame2010')
        roms_path:      Base ROMs path
        results_csv:    Path to audit CSV for updating
        already_tested: In-memory audit results dict
        dry_run:        If True, show what would happen without doing it

    Returns:
        True if migration succeeded (or dry run completed).
    """
    target_system = CORE_TO_SYSTEM.get(target_core)
    if not target_system:
        log(f"  ERROR: No system folder mapping for core '{target_core}'")
        log(f"  Add '{target_core}' to CORE_TO_SYSTEM in migrate.py")
        return False

    source_rom = os.path.join(roms_path, source_system, romname)
    target_dir = os.path.join(roms_path, target_system)
    target_rom = os.path.join(target_dir, romname)

    if not os.path.exists(source_rom):
        log(f"  ERROR: ROM not found: {source_rom}")
        return False

    log(f"\nMigration plan for [{source_system}] {romname}:")
    log(f"  Working core : {target_core}")
    log(f"  Target system: {target_system}")
    log(f"  ROM          : {source_rom}")
    log(f"           --> : {target_rom}")

    # Check for scraped assets
    rom_base  = os.path.splitext(romname)[0]
    has_assets = any(
        any(
            os.path.splitext(f)[0] == rom_base
            for f in os.listdir(
                os.path.join(ES_MEDIA_BASE, source_system, asset_type)
            )
        )
        for asset_type in ASSET_TYPES
        if os.path.isdir(
            os.path.join(ES_MEDIA_BASE, source_system, asset_type)
        )
    )

    # Check for gamelist entry
    metadata = read_game_entry(source_system, romname)

    if has_assets:
        log(f"  Assets       : will be moved to {target_system}")
    else:
        log(f"  Assets       : none found")

    if metadata:
        log(f"  Gamelist     : entry found, will be moved")
    else:
        log(f"  Gamelist     : no entry found")

    if dry_run:
        log(f"\nDry run — no changes made.")
        return True

    # -- Perform migration --

    # 1. Create target directory
    os.makedirs(target_dir, exist_ok=True)

    # 2. Move ROM
    try:
        shutil.move(source_rom, target_rom)
        log(f"  Moved ROM to {target_system}/")
    except Exception as e:
        log(f"  ERROR: Could not move ROM: {e}")
        return False

    # 3. Move assets
    if has_assets:
        moved = move_assets(source_system, target_system, romname)
        if moved:
            log(f"  Moved assets: {', '.join(moved)}")

    # 4. Update gamelists
    if metadata:
        if remove_game_from_gamelist(source_system, romname):
            log(f"  Removed from {source_system} gamelist")
        if add_game_to_gamelist(target_system, romname, roms_path, metadata):
            log(f"  Added to {target_system} gamelist")

    # 5. Update audit CSV
    old_key = f"{source_system}:{romname}"
    new_key = f"{target_system}:{romname}"
    if old_key in already_tested:
        entry = already_tested.pop(old_key)
        entry['system'] = target_system
        entry['status'] = 'OK'
        entry['notes']  = f"Migrated from {source_system} — uses {target_core}"
        already_tested[new_key] = entry
        log(f"  CSV updated — entry moved to [{target_system}]")

    log(f"\nMigration complete.")
    log(f"Restart EmulationStation to see changes in the frontend.")
    return True
