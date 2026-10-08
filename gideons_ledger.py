"""Gideon's Ledger: Elden Ring trainer. Launch offline, read/add runes, smart drops. Offline only."""
import queue, re, struct, sys, threading, time, tkinter as tk

# GameDataMan signature + layout from The-Grand-Archives/Elden-Ring-CT-TGA (patch-resistant AOB, no fixed address).
# [[GameDataMan]+8] = PlayerGameData; +0x68 level (in TGA table), +0x6C runes (neighbour field; confirm in-game).
AOB = rb"\x48\x8B\x05[\x00-\xFF]{4}\x48\x85\xC0\x74\x05\x48\x8B\x40\x58\xC3\xC3"  # [\x00-\xFF] since "." skips 0x0A
PLAYER_DATA, LEVEL, RUNES = 0x8, 0x68, 0x6C
MAX_RUNES = 999_999_999


def runes_ok(v):
    return 0 <= v <= MAX_RUNES


def rip_target(match, rel32):
    """Address a `mov rax,[rip+rel32]` (7 bytes) at `match` points to."""
    return match + 7 + rel32


_scans = {}
_pid = None  # game process the cached scans / edit lists belong to
_pm = None  # last Pymem handle, closed on the next attach so handles do not pile up


def scan(read, base, size, chunk=1 << 22, aob=AOB):
    """First AOB match address in [base, base+size). `read(addr, n)` -> bytes. pymem's own scanner misses it."""
    if (aob, base, size) in _scans:  # crewcut: an unchanged exe at the same base always matches at the same address
        return _scans[aob, base, size]
    pat = re.compile(aob, re.S)
    for off in range(0, size, chunk):
        m = pat.search(read(base + off, min(chunk + 32, size - off)))  # +32 overlap so no match straddles a chunk
        if m:
            _scans[aob, base, size] = base + off + m.start()
            return _scans[aob, base, size]


def commas(s):
    """Keep digits only, group by thousands. '' stays ''."""
    d = "".join(c for c in s if c.isdigit())
    return f"{int(d):,}" if d else ""


def new_runes(cur, delta):
    return min(MAX_RUNES, max(0, cur + delta))


def attach():
    """-> (pymem, PlayerGameData address, level, runes). Raises RuntimeError with a user-facing reason."""
    import pymem  # imported here so --selftest runs without it
    import pymem.process
    global _pid, _last_inv, _pm
    if _pm:  # one handle at a time: callers run one at a time (busy lock), so the previous one is idle
        try:
            _pm.close_process()
        except Exception:
            pass
        _pm = None
    pm = _pm = pymem.Pymem("eldenring.exe")
    if pm.process_id != _pid:  # a new game process has fresh tables at new addresses: drop everything remembered
        _scans.clear(); _lots.clear(); _orig.clear(); _last_inv = None
        _pid = pm.process_id
    if pymem.process.process_from_name("start_protected_game.exe") or \
            pymem.process.process_from_name("EasyAntiCheat_EOS.exe"):
        raise RuntimeError("Easy Anti-Cheat running. Launch the game offline first.")
    mod = pymem.process.module_from_name(pm.process_handle, "eldenring.exe")
    match = scan(pm.read_bytes, mod.lpBaseOfDll, mod.SizeOfImage)
    if not match:
        raise RuntimeError("GameDataMan pattern not found. Game patched? Need new signature.")
    gdm = pm.read_longlong(rip_target(match, pm.read_int(match + 3)))
    if not gdm:
        raise RuntimeError("GameDataMan empty. Load into your character first.")
    player = pm.read_longlong(gdm + PLAYER_DATA)
    level, runes = pm.read_int(player + LEVEL), pm.read_int(player + RUNES)
    if not runes_ok(runes) or not 1 <= level <= 713:  # read-before-write guard
        raise RuntimeError(f"Read level {level}, runes {runes}: out of range. Offsets wrong. Not writing.")
    return pm, player, level, runes


def read_runes():
    _, _, level, runes = attach()
    return f"Level {level}, runes {runes:,}."


def add_runes(delta):
    pm, player, _, runes = attach()
    new = new_runes(runes, delta)
    pm.write_int(player + RUNES, new)
    return f"Runes {runes:,} -> {pm.read_int(player + RUNES):,}."


# Param tables (layout from TGA param_containers.h). CSRegulationManager+0x18/+0x20 = begin/end of ParamResCap* list.
# ParamResCap: +0x18 name (wstring), +0x80 -> ParamHeader; header+0x80 -> ParamTable; table: u16 rows @0x0A, rows @0x40 (0x18 each).
# Item discovery = CalcCorrectGraph row 140 (Arcane -> discovery curve); TGA multiplies stageMaxGrowVal0..4 (floats @0x14..0x24).
REGMAN_AOB = rb"\x48\x8B\x0D[\x00-\xFF]{4}\x48\x85\xC9\x74\x0B\x4C\x8B\xC0\x48\x8B\xD7"
DISC_PARAM, DISC_ROW, DISC_OFFS = "CalcCorrectGraph", 140, [0x14 + 4 * i for i in range(5)]
_orig = {}  # addr -> original float, kept so Discovery OFF restores exactly


def _q(read, a):
    return struct.unpack("<Q", read(a, 8))[0]


def param_table(read, mgr, name):
    """Address of param `name`'s table, or None."""
    begin, end = _q(read, mgr + 0x18), _q(read, mgr + 0x20)
    for p in range(begin, end, 8):
        cap = _q(read, p)
        if not cap:
            continue
        s, n, capacity = cap + 0x18, _q(read, cap + 0x28), _q(read, cap + 0x30)
        if n > 64:
            continue
        if read(_q(read, s) if capacity > 7 else s, n * 2).decode("utf-16le", "ignore") != name:
            continue
        return _q(read, _q(read, cap + 0x80) + 0x80)


def _app_dir():  # folder beside the .exe when frozen (onefile __file__ is a temp dir), else beside the script
    import os
    return os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def _load_json(path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def table_rows(read, table):
    """[(row_id, row_addr)] for a param table."""
    if not table:
        raise RuntimeError("Drop table not found (game update?). Not writing.")
    n = struct.unpack("<H", read(table + 0x0A, 2))[0]
    raw = read(table + 0x40, 0x18 * n)
    return [(struct.unpack_from("<Q", raw, 0x18 * i)[0], table + struct.unpack_from("<Q", raw, 0x18 * i + 8)[0])
            for i in range(n)]


def param_row(read, mgr, name, row_id):
    """Address of `row_id`'s data in param `name`, or None."""
    table = param_table(read, mgr, name)
    if table:
        return next((a for rid, a in table_rows(read, table) if rid == row_id), None)


def regulation_manager(pm):
    import pymem.process
    mod = pymem.process.module_from_name(pm.process_handle, "eldenring.exe")
    m = scan(pm.read_bytes, mod.lpBaseOfDll, mod.SizeOfImage, aob=REGMAN_AOB)
    if not m:
        raise RuntimeError("CSRegulationManager pattern not found. Game patched?")
    return pm.read_longlong(rip_target(m, pm.read_int(m + 3)))


def discovery_floats(pm):
    mgr = regulation_manager(pm)
    row = param_row(pm.read_bytes, mgr, DISC_PARAM, DISC_ROW) if mgr else None
    if not row:
        raise RuntimeError("Discovery param row not found. Load into your character first.")
    addrs = [row + o for o in DISC_OFFS]
    vals = [pm.read_float(a) for a in addrs]
    if not all(0 <= v < 1000 for v in vals):  # read-before-write guard
        raise RuntimeError(f"Discovery values look wrong: {vals}. Not writing.")
    return addrs, vals


def show_discovery():
    addrs, vals = discovery_floats(attach()[0])
    return "Discovery curve: " + ", ".join(f"{v:g}" for v in vals) + (" (boosted)" if _orig else "")


def set_discovery(on, mult=10.0):
    pm = attach()[0]
    addrs, vals = discovery_floats(pm)
    if on:
        if _orig:
            return "Discovery already ON."
        if not 1 <= mult <= 100:
            return "Multiplier must be 1 to 100."
        for a, v in zip(addrs, vals):
            _orig[a] = v
            pm.write_float(a, v * mult)
        return f"Discovery x{mult:g} ON. Kill enemies to test. Click OFF before quitting."
    if not _orig:
        return "Discovery is not ON (or app restarted: restart the game to reset)."
    for a, v in _orig.items():
        pm.write_float(a, v)
    _orig.clear()
    return "Discovery OFF (restored)."


# Give item: call the game's own ItemGive (same method as TGA's ItemGive_code.cea), so the game registers the item.
# Needs code run inside the game: a 36-byte stub in a remote thread. Crash risk if the signature is stale.
ITEMGIVE_AOB = rb"\x8B\x02\x83\xF8\x0A"  # must be unique; function starts 0x52 bytes before
MAPITEMMAN_AOB = rb"\x48\x8B\x0D[\x00-\xFF]{4}\xC7\x44\x24\x50\xFF\xFF\xFF\xFF"  # TGA's (now commented-out) signature
LORDS_RUNE, LORDS_RUNE_MAX = 0x40000B67, 99  # goods 2919; max 99 assumed (inventory cap), game may clamp lower


def call_stub(func, a, b, c):
    """x64: func(rcx=a, rdx=b, r8=c, r9=0) inside a thread start routine."""
    imm = lambda v: struct.pack("<Q", v)
    return (b"\x48\x83\xEC\x28" + b"\x48\xB9" + imm(a) + b"\x48\xBA" + imm(b) + b"\x49\xB8" + imm(c)
            + b"\x45\x31\xC9" + b"\x48\xB8" + imm(func) + b"\xFF\xD0" + b"\x48\x83\xC4\x28\xC3")


def item_block(item_id, qty):
    """128-byte ItemGive_data; the one-item table (count, id, qty, -1, gem -1) sits at +32."""
    buf = bytearray(128)
    struct.pack_into("<IIIII", buf, 32, 1, item_id, qty, 0xFFFFFFFF, 0xFFFFFFFF)
    return bytes(buf)


def item_give_addrs(pm):
    import pymem.process
    mod = pymem.process.module_from_name(pm.process_handle, "eldenring.exe")
    base, size = mod.lpBaseOfDll, mod.SizeOfImage
    hit = scan(pm.read_bytes, base, size, aob=ITEMGIVE_AOB)
    if not hit or scan(pm.read_bytes, hit + 1, base + size - hit - 1, aob=ITEMGIVE_AOB):
        raise RuntimeError("ItemGive signature missing or not unique. Game patched? Not calling.")
    m = scan(pm.read_bytes, base, size, aob=MAPITEMMAN_AOB)
    if not m:
        raise RuntimeError("MapItemMan signature not found. Game patched? Not calling.")
    man = pm.read_longlong(rip_target(m, pm.read_int(m + 3)))
    if not man:
        raise RuntimeError("MapItemMan empty. Load into your character first.")
    return hit - 0x52, man


def give_item(item_id, qty):
    import ctypes
    pm = attach()[0]  # also enforces: offline, character loaded, offsets sane
    func, man = item_give_addrs(pm)
    data, code = pm.allocate(128), pm.allocate(64)
    try:
        pm.write_bytes(data, item_block(item_id, qty), 128)
        stub = call_stub(func, man, data + 32, data)
        pm.write_bytes(code, stub, len(stub))
        th = pm.start_thread(code)
        done = ctypes.windll.kernel32.WaitForSingleObject(th, 5000) == 0
        ctypes.windll.kernel32.CloseHandle(th)
        if not done:
            return "Game did not answer in 5s. Check the game window."
    finally:
        pm.free(data), pm.free(code)
    return f"Gave {qty}x item {item_id:#x}. Check your inventory."


def add_lords_rune(qty):
    if not 1 <= qty <= LORDS_RUNE_MAX:
        return f"Quantity must be 1 to {LORDS_RUNE_MAX}."
    return give_item(LORDS_RUNE, qty)


# Upgrade materials: goods ids from the Hexinton table (goods category 0x40000000 | id). Smithing Stone [9] is not a game item.
UPGRADE_MAX = 999
UPGRADES = {f"Smithing Stone [{i + 1}]": 10100 + i for i in range(8)}
UPGRADES["Ancient Dragon Smithing Stone"] = 10140
UPGRADES.update({f"Somber Smithing Stone [{i + 1}]": 10160 + i for i in range(8)})
UPGRADES["Somber Smithing Stone [9]"] = 10200
UPGRADES["Somber Ancient Dragon Smithing Stone"] = 10168
UPGRADES.update({f"Grave Glovewort [{i + 1}]": 10900 + i for i in range(9)})  # spirit ash upgrades
UPGRADES.update({f"Ghost Glovewort [{i + 1}]": 10910 + i for i in range(9)})
UPGRADES["Great Grave Glovewort"] = 10909
UPGRADES["Great Ghost Glovewort"] = 10919


def add_upgrade(name, qty):
    if name not in UPGRADES:
        return "Pick an item from the list."
    if not 1 <= qty <= UPGRADE_MAX:
        return f"Quantity must be 1 to {UPGRADE_MAX}."
    return give_item(0x40000000 | UPGRADES[name], qty)


def add_upgrades(names, qty):
    if not names:
        return "Tick at least one item."
    if not 1 <= qty <= UPGRADE_MAX:
        return f"Quantity must be 1 to {UPGRADE_MAX}."
    for n in names:
        res = add_upgrade(n, qty)
        if not res.startswith("Gave"):
            return f"Stopped at {n}: {res}"
    return f"Gave {qty} each of {len(names)} item(s). Check your inventory."


def check_item_give():
    func, man = item_give_addrs(attach()[0])
    return f"Item give ready: function {func:#x}, MapItemMan {man:#x}. Nothing called."


# Smart drops: in every enemy drop lot (ItemLotParam_enemy, 0x98-byte rows) that offers weapons/armor you do not own,
# give those slots all the weight (so the lot always drops them) and zero the rest. Lots with nothing new stay as-is.
# Row layout (TGA paramdefs.h): ids +0x00 (8*s32), categories +0x20 (8*s32), weights +0x40 (8*u16).
# Lot categories: 2 = weapon, 3 = armor. Inventory: PlayerGameData+0x5D0 (bag) / +0x8D0 (chest) -> list ptr +0x10, 0x18-byte
# entries (handle, item id, qty), capacities 2688 / 1920 (TGA Get functions.cea). Item id top nibble: 0 weapon, 1 armor.
LOT_ROW, INV_SLOTS = 0x98, ((0x5D0, 2688), (0x8D0, 1920))
CAT_GOODS, CAT_WEAPON, CAT_ARMOR, CAT_ACCESSORY, CAT_GEM, NEW_WEIGHT = 1, 2, 3, 4, 5, 1000
_lots = {}  # row addr -> original 16 weight bytes, for rows we changed
_smart_on = False
_last_inv = None  # (weapons, armor) of the last full pass; None forces the next refresh to do one
_consumables = True  # also make consumables/materials always drop (shares each lot 50/50 with new gear)


def set_consumables(on):
    global _consumables, _last_inv
    _consumables = bool(on)
    _last_inv = None


def owned_gear(read, player):
    """(weapon base ids, armor ids) in bag + chest. Weapon id // 10000 drops upgrade level and affinity."""
    weapons, armor = set(), set()
    for off, slots in INV_SLOTS:
        eid = _q(read, player + off)
        raw = read(_q(read, eid + 0x10), 0x18 * slots)
        for i in range(slots):
            handle, item = struct.unpack_from("<II", raw, 0x18 * i)
            if not handle or item == 0xFFFFFFFF:
                continue
            if item >> 28 == 0:
                weapons.add(item // 10000)
            elif item >> 28 == 1:
                armor.add(item & 0x0FFFFFFF)
    return weapons, armor


def plan_lot(row, weapons, armor, consumables=True):
    """New 8 weights for a 0x98-byte lot row, or None to leave it alone.
    Unowned gear and (optionally) consumables/materials share the roll 50/50; the 'nothing' slot and owned gear
    drop to 0. A lot with only one of the two kinds always drops it. Talismans/Ashes of War keep their weights."""
    ids = struct.unpack_from("<8i", row, 0)
    cats = struct.unpack_from("<8i", row, 0x20)
    pts = struct.unpack_from("<8H", row, 0x40)
    gear = [i for i in range(8) if pts[i] and ((cats[i] == CAT_WEAPON and ids[i] // 10000 not in weapons) or
                                               (cats[i] == CAT_ARMOR and ids[i] not in armor))]
    goods = [i for i in range(8) if consumables and pts[i] and cats[i] == CAT_GOODS and ids[i]]
    owned = [i for i in range(8) if pts[i] and cats[i] in (CAT_WEAPON, CAT_ARMOR) and i not in gear]
    if not gear and not goods:
        if not owned:
            return None
        new = list(pts)  # only gear you already own: stop it dropping again, leave the rest (incl. 'nothing') alone
        for i in owned:
            new[i] = 0
        return new
    new = [p if cats[i] in (CAT_ACCESSORY, CAT_GEM) else 0 for i, p in enumerate(pts)]
    for i in gear:
        new[i] = max(1, NEW_WEIGHT // len(gear))
    total = sum(pts[i] for i in goods)
    for i in goods:  # keep the consumables' relative odds; scale to half the roll only when gear shares the lot
        new[i] = min(65535, max(1, round(pts[i] * NEW_WEIGHT / total))) if gear else pts[i]
    return new if new != list(pts) else None


def smart_refresh():
    """Recompute from current inventory; apply to changed lots, restore lots that no longer qualify."""
    pm, player = attach()[:2]
    weapons, armor = owned_gear(pm.read_bytes, player)
    if not weapons or not armor:  # a real character owns both; empty means the inventory read is wrong
        raise RuntimeError(f"Inventory read looks wrong ({len(weapons)} weapons, {len(armor)} armor). Not writing.")
    global _last_inv
    if _last_inv == (weapons, armor):  # nothing picked up since the last full pass: skip the param walk + 700 KB read
        return len(_lots), 0, 0, len(weapons), len(armor)
    _last_inv = (weapons, armor)
    rows = table_rows(pm.read_bytes, param_table(pm.read_bytes, regulation_manager(pm), "ItemLotParam_enemy"))
    if len(rows) < 1000:
        raise RuntimeError(f"Only {len(rows)} drop lots found. Not writing.")
    lo = min(a for _, a in rows)
    hi = max(a for _, a in rows) + LOT_ROW
    block = pm.read_bytes(lo, hi - lo)
    edits, restored = {}, 0  # addr -> new 16 weight bytes
    for _, addr in rows:
        row = bytearray(block[addr - lo:addr - lo + LOT_ROW])
        if addr in _lots:  # judge from the original weights, not our edited ones
            row[0x40:0x50] = _lots[addr]
        cats = struct.unpack_from("<8i", row, 0x20)
        if any(c not in range(0, 6) for c in cats):
            raise RuntimeError("Drop lot layout looks wrong (bad category). Not writing.")
        new = plan_lot(row, weapons, armor, _consumables)
        if new:
            data = struct.pack("<8H", *new)
            if addr not in _lots:
                _lots[addr] = bytes(row[0x40:0x50])
            if block[addr - lo + 0x40:addr - lo + 0x50] != data:
                edits[addr] = data
        elif addr in _lots:
            edits[addr] = _lots.pop(addr)
            restored += 1
    t = time.perf_counter()
    _patch_weights(pm, edits)
    with open("refresh.log", "a") as f:  # evidence for the in-game hitch hunt: when a full pass wrote, and how long the write took
        f.write(f"{time.strftime('%H:%M:%S')} full pass, {len(edits)} edits, write {1000 * (time.perf_counter() - t):.0f} ms\n")
    return len(rows), len(edits) - restored, restored, len(weapons), len(armor)


def _patch_weights(pm, edits):
    """Write {lot addr: 16 weight bytes} in ONE call: each separate memory write costs ~7 ms (4600 lots = 30+ s)."""
    if not edits:
        return
    lo, hi = min(edits) + 0x40, max(edits) + 0x50
    buf = bytearray(pm.read_bytes(lo, hi - lo))  # fresh read right before the write, so other lots are rewritten as-is
    for addr, data in edits.items():
        buf[addr + 0x40 - lo:addr + 0x50 - lo] = data
    pm.write_bytes(lo, bytes(buf), len(buf))


def smart_drops(on):
    global _smart_on, _last_inv
    _last_inv = None  # ON/OFF always starts from a full pass
    if on:
        lots, changed, restored, w, a = smart_refresh()
        _smart_on = True
        return (f"Smart drops ON. {len(_lots)} of {lots} drop lots changed "
                f"(you own {w} weapons, {a} armor). Refreshes every 10s.")
    pm = attach()[0]
    n = len(_lots)
    _patch_weights(pm, dict(_lots))
    _lots.clear()
    _smart_on = False
    return f"Smart drops OFF. Restored {n} lots."


def smart_dry_run():
    """Read-only: how many lots would change, and what do they offer."""
    pm, player = attach()[:2]
    weapons, armor = owned_gear(pm.read_bytes, player)
    rows = table_rows(pm.read_bytes, param_table(pm.read_bytes, regulation_manager(pm), "ItemLotParam_enemy"))
    n = sum(1 for _, a in rows if plan_lot(pm.read_bytes(a, LOT_ROW), weapons, armor, _consumables))
    return f"You own {len(weapons)} weapons, {len(armor)} armor. {n} of {len(rows)} drop lots would change. Nothing written."


# Progress: event flags. CSEventFlagMan's VirtualMemoryFlag (VMF) keeps flag bits in blocks of `divisor` (1000) flags,
# found through a red-black tree keyed by flag_id // divisor. Signature + layout from SoulSplitter's ReadEventFlag
# (Ghidra-annotated; reimplemented here, not copied). Flag names come from a local data/event_flags.json.
VMF_AOB = rb"\x44\x89\x7c\x24\x28\x4c\x8b\x25[\x00-\xFF]{4}\x4d\x85\xe4"  # mov r12,[rip+rel32]: rel32 at +8, ends at +12


def event_flag(read, vmf, flag):
    """True if event flag `flag` is set. `vmf` = address of the VirtualMemoryFlag object."""
    u32 = lambda a: struct.unpack("<I", read(a, 4))[0]
    div = u32(vmf + 0x1C)
    if not div:
        return False  # not initialised yet
    cat, low = divmod(flag, div)
    cur = _q(read, _q(read, vmf + 0x38) + 8)  # +0x38 = tree header (nil sentinel); root hangs off header+8
    best = None
    while read(cur + 0x19, 1) == b"\0":  # not a nil node
        if u32(cur + 0x20) < cat:
            cur = _q(read, cur + 0x10)
        else:
            best, cur = cur, _q(read, cur)
    if best is None or cat < u32(best + 0x20):
        return False  # no block for this category
    mode = u32(best + 0x28)
    if mode == 1:
        base = u32(vmf + 0x20) * u32(best + 0x30) + _q(read, vmf + 0x28)
    elif mode == 2:
        return False
    else:
        base = _q(read, best + 0x30)
    return bool(base and read(base + (low >> 3), 1)[0] & (1 << (7 - (low & 7))))


def vmf_address(pm):
    import pymem.process
    mod = pymem.process.module_from_name(pm.process_handle, "eldenring.exe")
    m = scan(pm.read_bytes, mod.lpBaseOfDll, mod.SizeOfImage, aob=VMF_AOB)
    if not m:
        raise RuntimeError("VirtualMemoryFlag pattern not found. Game patched?")
    vmf = pm.read_longlong(m + 12 + pm.read_int(m + 8))
    if not vmf or pm.read_int(vmf + 0x1C) != 1000:  # read-before-trust guard
        raise RuntimeError("Event flags not ready. Load into your character first.")
    return vmf


def progress_report(flags=None):
    """Text report of story, bosses and graces from data/event_flags.json. Read-only."""
    import json, os
    path = os.path.join(_app_dir(), "data", "event_flags.json")
    try:
        flags = flags or _load_json(path)["flags"]
    except OSError:
        return f"Flag list missing: {path}\nSee README (Progress)."
    pm, player = attach()[:2]
    vmf = vmf_address(pm)
    on = {f[0]: event_flag(pm.read_bytes, vmf, f[0]) for f in flags}
    lines = []
    for cat, sub in (("System", "Story Progress"), ("System", "NG+ & Endings"), ("System", "World Events"), ("System", "Great Runes")):
        got = [f for f in flags if f[2] == cat and f[3] == sub]
        lines += [f"== {sub}: {sum(on[f[0]] for f in got)}/{len(got)} =="] + [f"  [x] {f[1]}" for f in got if on[f[0]]]
        lines += [f"  [ ] {f[1]}" for f in got if not on[f[0]]]
    for cat, label in (("Bosses", "Bosses defeated"), ("Grace", "Graces found")):
        seen = set()  # a flag can be listed twice (a boss under Misc and under its region): count it once
        rows = [f for f in sorted(flags, key=lambda f: f[3] == "Misc") if f[2] == cat and f[3] not in ("TEST", "None") and not (f[0] in seen or seen.add(f[0]))]
        lines += ["", f"== {label}: {sum(on[f[0]] for f in rows)}/{len(rows)} =="]
        for reg in sorted({str(f[3]) for f in rows}):
            r = [f for f in rows if str(f[3]) == reg]
            lines.append(f"  {reg}: {sum(on[f[0]] for f in r)}/{len(r)}" + "".join(f"\n      missing: {f[1]}" for f in r if not on[f[0]]))
    return "\n".join(lines)


def quest_report(get, steps):
    """Per NPC, the dialogue lines whose flag conditions all hold now: where each NPC's story stands.
    `get(flag) -> bool`; `steps` = data/npc_steps.json['npcs']. Conditions are approximate (the 'and' list is capped)."""
    seen, lines = {}, []
    ok = lambda f, gate: seen.setdefault(f, get(f)) == (gate == "set")
    for npc, entries in sorted(steps.items()):
        now = []
        for e in entries:
            if ok(e["flag"], e["gate"]) and all(ok(c["flag"], c["gate"]) for c in e.get("and", [])):  # entries with a single condition have no 'and'
                now += [l for l in e["lines"] if l not in now]
        if now:
            lines += [f"{npc}:"] + [f'    "{l}"' for l in now[:4]] + ([f"    (+{len(now) - 4} more)"] if len(now) > 4 else [])
    return "\n".join(lines) or "No NPC dialogue state matched. Load into your character first."


def show_quests():
    import json, os
    here = _app_dir()
    try:
        steps = _load_json(os.path.join(here, "data", "npc_steps.json"))["npcs"]
    except OSError:
        return "data/npc_steps.json missing. See README (Quests)."
    pm = attach()[0]
    vmf = vmf_address(pm)
    text = quest_report(lambda f: event_flag(pm.read_bytes, vmf, f), steps)
    out = os.path.join(here, "quests.txt")
    _save_text(out, text)
    os.startfile(out)
    return f"Quest state saved to quests.txt ({text.count(chr(10) + '    ') + 1} dialogue lines)."


def show_progress():
    import os
    out = os.path.join(_app_dir(), "progress.txt")
    text = progress_report()
    _save_text(out, text)
    os.startfile(out)
    return "Progress saved to progress.txt: " + text.splitlines()[0]


class Tip(tk.Label):
    """Small (i) icon; hover for a tooltip."""
    def __init__(self, parent, text):
        super().__init__(parent, text="i", font=("Segoe UI", 8, "bold"), fg="white", bg="#3b78c4", width=2, cursor="question_arrow")
        self.text, self.win = text, None
        self.bind("<Enter>", self.show)
        self.bind("<Leave>", self.hide)

    def show(self, _=None):
        self.win = tk.Toplevel(self)
        self.win.wm_overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"+{self.winfo_rootx() + 22}+{self.winfo_rooty() + 18}")
        tk.Label(self.win, text=self.text, wraplength=260, justify="left", bg="#ffffe0", relief="solid", borderwidth=1, padx=6, pady=3).pack()

    def hide(self, _=None):
        if self.win:
            self.win.destroy()
            self.win = None


def main():
    root = tk.Tk()
    root.title("Gideon's Ledger v1.1")
    root.attributes("-topmost", True)
    out = tk.StringVar(value="Back up your save first. Offline mode only.")

    amount = tk.StringVar(value="1,000,000")
    amount.trace_add("write", lambda *_: commas(amount.get()) != amount.get() and amount.set(commas(amount.get())))

    msgs, busy = queue.Queue(), threading.Lock()

    def run(fn):  # memory work goes to a thread so the window never freezes; the worker only touches the queue
        if not busy.acquire(blocking=False):
            out.set("Still working on the last click...")
            return
        out.set("Working...")

        def work():
            try:
                msgs.put(fn())
            except Exception as e:  # process missing, bad pointer, etc.
                msgs.put(f"Failed: {e}")
            finally:
                busy.release()
        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            while True:
                out.set(msgs.get_nowait())
        except queue.Empty:
            pass
        root.after(100, poll)

    def add():
        run(lambda: add_runes(int(amount.get().replace(",", ""))))

    def tick():  # smart drops follow your inventory: each pickup removes that item from the guaranteed list
        if _smart_on and busy.acquire(blocking=False):
            def work():
                try:
                    t = time.perf_counter()
                    smart_refresh()
                    if (dt := time.perf_counter() - t) > 1:  # evidence for the in-game hitch hunt
                        msgs.put(f"Smart drops refresh took {dt:.1f}s")
                except Exception as e:
                    msgs.put("Smart drops waiting for the game to start..." if "Could not find process" in str(e)
                             else f"Smart drops refresh failed: {e}")
                finally:
                    busy.release()
            threading.Thread(target=work, daemon=True).start()
        root.after(10_000, tick)

    def close():  # put the game's tables back before the app disappears
        busy.acquire(timeout=10)  # let a refresh in progress finish first
        for fn in (lambda: smart_drops(False), lambda: set_discovery(False)):
            try:
                fn()
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(10_000, tick)
    root.after(100, poll)

    def launch():
        import os, subprocess
        game_dir = r"C:\Program Files (x86)\Steam\steamapps\common\ELDEN RING\Game"  # crewcut: default Steam path only
        env = {**os.environ, "SteamAppId": "1245620", "SteamGameId": "1245620"}
        try:
            subprocess.Popen([game_dir + r"\eldenring.exe"], cwd=game_dir, env=env)
            out.set("Launched offline (no Easy Anti-Cheat). Stay offline.")
        except Exception as e:
            out.set(f"Launch failed: {e}")

    def launch_walls():  # Mod Engine 3 + walls.me3 (auto-reveal illusory walls); both live next to this script
        import os, subprocess
        here = _app_dir()
        try:
            subprocess.Popen([os.path.join(here, "me3", "bin", "me3.exe"), "launch", "-p", os.path.join(here, "walls.me3")], cwd=here)
            out.set("Launching with auto-reveal walls (Steam must be running). Stay offline.")
        except Exception as e:
            out.set(f"Launch failed: {e}")

    def section(title):
        f = tk.LabelFrame(root, text=title, padx=6, pady=4)
        f.pack(fill="x", padx=10, pady=(6, 0))
        return f

    def line(parent, tip=None):  # one row: widgets packed left, optional (i) tooltip pinned right
        r = tk.Frame(parent)
        r.pack(fill="x", pady=2)
        if tip:
            Tip(r, tip).pack(side="right", padx=(4, 0))
        return r

    def btn(parent, text, cmd, tip=None):
        r = line(parent, tip)
        b = tk.Button(r, text=text, command=cmd)
        b.pack(side="left", fill="x", expand=True)
        return b

    def entry(r, var, width=12):
        tk.Entry(r, textvariable=var, justify="right", width=width).pack(side="left", padx=(0, 4))

    f = section("Launch")
    btn(f, "Launch offline", launch, "Starts eldenring.exe directly so Easy Anti-Cheat never loads. Offline play only; going online risks a ban.")
    btn(f, "Launch with auto-reveal walls", launch_walls, "Starts the game through Mod Engine 3 with your walls.me3 profile (optional; see README).")

    f = section("Runes")
    btn(f, "Read runes", lambda: run(read_runes), "Shows your current rune count.")
    r = line(f, "Adds this many runes to your current total.")
    entry(r, amount)
    tk.Button(r, text="Add runes", command=add).pack(side="left", fill="x", expand=True)
    btn(f, "Max runes", lambda: run(lambda: add_runes(MAX_RUNES)), "Sets runes to the maximum, 999,999,999.")

    f = section("Items")
    qty = tk.StringVar(value="1")
    r = line(f, "Gives Lord's Runes through the game's own item function, so they register properly.")
    entry(r, qty, 5)
    tk.Button(r, text="Add Lord's Rune", command=lambda: run(lambda: add_lords_rune(int(qty.get())))).pack(side="left", fill="x", expand=True)
    def upgrade_window():  # one checkbox per item, one quantity for all ticked items
        win = tk.Toplevel(root)
        win.title("Upgrade items")
        win.attributes("-topmost", True)
        checks = {name: tk.BooleanVar() for name in UPGRADES}
        groups = ("Smithing Stones (weapons)", "Somber Stones (special weapons)",
                  "Grave Glovewort (spirit ashes)", "Ghost Glovewort (spirit ashes)")
        frames = [tk.LabelFrame(win, text=t) for t in groups]
        for col, f in enumerate(frames):
            f.grid(row=0, column=col, padx=6, pady=6, sticky="n")
        members = [[] for _ in frames]
        for name in UPGRADES:
            i = 1 if "Somber" in name else 2 if "Grave" in name else 3 if "Ghost" in name else 0
            members[i].append(name)
            tk.Checkbutton(frames[i], text=name, variable=checks[name], anchor="w").pack(fill="x")

        def setall(names, on):
            for n in names:
                checks[n].set(on)

        for f, names in zip(frames, members):  # per-box: tick all, or clear
            row = tk.Frame(f)
            row.pack(fill="x", pady=(4, 0))
            tk.Button(row, text="All", command=lambda ns=names: setall(ns, True)).pack(side="left", expand=True, fill="x")
            tk.Button(row, text="None", command=lambda ns=names: setall(ns, False)).pack(side="left", expand=True, fill="x")
        qty_var = tk.StringVar(value="10")
        bar = tk.Frame(win)
        bar.grid(row=1, column=0, columnspan=len(groups), pady=(0, 8))
        tk.Button(bar, text="Select all", command=lambda: setall(list(UPGRADES), True)).pack(side="left", padx=(0, 4))
        tk.Button(bar, text="Clear all", command=lambda: setall(list(UPGRADES), False)).pack(side="left", padx=(0, 12))
        tk.Label(bar, text="Quantity each:").pack(side="left")
        tk.Entry(bar, textvariable=qty_var, width=6, justify="right").pack(side="left", padx=6)
        tk.Button(bar, text="Add ticked items",
                  command=lambda: run(lambda: add_upgrades([n for n, v in checks.items() if v.get()], int(qty_var.get())))).pack(side="left")

    btn(f, "Weapon / spirit ash upgrade items...", upgrade_window, "Opens a list of smithing stones, somber stones and gloveworts. Tick what you want and add them in bulk.")
    btn(f, "Check item give", lambda: run(check_item_give), "Read-only self-check: confirms the item-give function was found. Nothing is called or written.")

    f = section("Smart drops")
    cons = tk.BooleanVar(value=True)
    r = line(f, "Also guarantee consumables and materials you don't own, not just weapons and armor. Set before turning drops ON.")
    tk.Checkbutton(r, text="Include consumables + materials", variable=cons, command=lambda: set_consumables(cons.get())).pack(side="left")
    btn(f, "Preview", lambda: run(smart_dry_run), "Read-only: counts how many drop lots would change. Nothing is written.")
    btn(f, "ON (new gear)", lambda: run(lambda: smart_drops(True)), "Enemy drop lots that offer gear you don't own always drop it. Refreshes as you pick things up.")
    btn(f, "OFF", lambda: run(lambda: smart_drops(False)), "Restores the original drop table byte-for-byte. Closing the app does this too.")

    f = section("Discovery")
    mult = tk.StringVar(value="10")
    r = line(f, "Multiplies the item-discovery curve (Arcane), 1 to 100. OFF restores the exact original values.")
    tk.Label(r, text="Multiplier x").pack(side="left")
    entry(r, mult, 5)
    tk.Button(r, text="ON", command=lambda: run(lambda: set_discovery(True, float(mult.get())))).pack(side="left", fill="x", expand=True)
    tk.Button(r, text="OFF", command=lambda: run(lambda: set_discovery(False))).pack(side="left", fill="x", expand=True, padx=(4, 0))
    btn(f, "Read discovery", lambda: run(show_discovery), "Shows the current discovery curve values.")

    f = section("Read-only")
    btn(f, "Read progress", lambda: run(show_progress), "Story flags, endings, Great Runes, bosses and graces per region. Needs data/event_flags.json (see README).")
    btn(f, "Read NPC quest state", lambda: run(show_quests), "What each NPC would say right now. Needs data/npc_steps.json built from your own game files (see README).")

    tk.Label(root, textvariable=out, wraplength=300, justify="left", relief="sunken", anchor="w", padx=6, pady=4).pack(fill="x", padx=10, pady=10)
    root.mainloop()


def selftest():
    assert runes_ok(0) and runes_ok(MAX_RUNES) and not runes_ok(-1) and not runes_ok(MAX_RUNES + 1)
    assert rip_target(0x1000, 0x20) == 0x1027
    assert rip_target(0x1000, struct.unpack("<i", struct.pack("<i", -8))[0]) == 0xFFF
    sig = b"\x48\x8B\x05\x0A\x00\x00\x00\x48\x85\xC0\x74\x05\x48\x8B\x40\x58\xC3\xC3"  # rel32 contains 0x0A
    mem = bytearray(b"\x90" * 100)
    mem[40:40 + len(sig)] = sig
    fake = lambda addr, n: bytes(mem[addr:addr + n])
    assert scan(fake, 0, 100, chunk=45) == 40  # match straddles chunk boundary
    assert scan(fake, 0, 30, chunk=45) is None
    assert new_runes(10, 5) == 15 and new_runes(10, -50) == 0 and new_runes(MAX_RUNES - 1, 99) == MAX_RUNES
    mem = bytearray(0x2000)
    put = lambda a, fmt, *v: mem.__setitem__(slice(a, a + struct.calcsize(fmt)), struct.pack(fmt, *v))
    name = "CalcCorrectGraph".encode("utf-16le")
    put(0x18, "<QQ", 0x100, 0x108)                        # manager: list [0x100, 0x108) = one ResCap
    put(0x100, "<Q", 0x200)                               # list[0] -> ResCap @0x200
    put(0x200 + 0x18, "<Q", 0x400); mem[0x400:0x400 + len(name)] = name  # heap name (capacity > 7)
    put(0x200 + 0x28, "<QQ", 16, 31)                      # length, capacity
    put(0x200 + 0x80, "<Q", 0x600); put(0x600 + 0x80, "<Q", 0x800)  # header -> table
    put(0x800 + 0x0A, "<H", 2)
    put(0x800 + 0x40, "<QQQ", 139, 0x300, 0); put(0x800 + 0x58, "<QQQ", 140, 0x500, 0)
    rd = lambda a, n: bytes(mem[a:a + n])
    assert param_row(rd, 0, "CalcCorrectGraph", 140) == 0x800 + 0x500
    assert param_row(rd, 0, "CalcCorrectGraph", 141) is None and param_row(rd, 0, "Other", 140) is None
    assert commas("1234567") == "1,234,567" and commas("1,2a3") == "123" and commas("") == "" and commas("-5") == "5"
    s = call_stub(0x1122334455667788, 0xAA, 0xBB, 0xCC)
    assert len(s) == 4 + 10 + 10 + 10 + 3 + 10 + 2 + 5 and s.endswith(b"\xFF\xD0\x48\x83\xC4\x28\xC3")
    assert s[6:14] == struct.pack("<Q", 0xAA) and s[26:34] == struct.pack("<Q", 0xCC)
    b = item_block(LORDS_RUNE, 3)
    assert len(b) == 128 and struct.unpack_from("<IIIII", b, 32) == (1, 0x40000B67, 3, 0xFFFFFFFF, 0xFFFFFFFF)
    assert add_lords_rune(0).startswith("Quantity") and add_lords_rune(100).startswith("Quantity")
    lot = bytearray(LOT_ROW)  # slot1: owned weapon, slot2: new weapon, slot3: new armor, slot4: new goods, slot5: weight 0
    struct.pack_into("<8i", lot, 0, 1000000, 2000000, 5000, 77, 3000000, 0, 0, 0)
    struct.pack_into("<8i", lot, 0x20, 2, 2, 3, 1, 2, 0, 0, 0)
    struct.pack_into("<8H", lot, 0x40, 500, 300, 100, 90, 0, 0, 0, 0)
    assert plan_lot(bytes(lot), {100}, {5000}, False) == [0, NEW_WEIGHT, 0, 0, 0, 0, 0, 0]  # armor 5000 owned, weapon 200 new
    assert plan_lot(bytes(lot), {100, 200}, {5000}, False) == [0, 0, 0, 90, 0, 0, 0, 0]  # all gear owned: no repeats, rest kept
    struct.pack_into("<8H", lot, 0x40, 0, 0, 0, 90, 0, 0, 0, 0)
    assert plan_lot(bytes(lot), {100, 200}, {5000}, False) is None  # nothing gear-related in the lot -> untouched
    struct.pack_into("<8H", lot, 0x40, 500, 300, 100, 90, 0, 0, 0, 0)
    assert plan_lot(bytes(lot), {100, 200}, set(), False) == [0, 0, NEW_WEIGHT, 0, 0, 0, 0, 0]
    assert plan_lot(bytes(lot), {100}, {5000}) == [0, NEW_WEIGHT, 0, NEW_WEIGHT, 0, 0, 0, 0]  # gear and goods share 50/50
    assert plan_lot(bytes(lot), {100, 200}, {5000}) == [0, 0, 0, 90, 0, 0, 0, 0]  # only goods: always drops, odds kept
    struct.pack_into("<8H", lot, 0x40, 500, 300, 100, 90, 0, 40, 0, 0)  # add a talisman (cat 4) slot with weight 40
    struct.pack_into("<i", lot, 0x14, 4)
    struct.pack_into("<i", lot, 0x34, CAT_ACCESSORY)
    assert plan_lot(bytes(lot), {100, 200}, {5000})[5] == 40  # talismans keep their weight
    class FakePm:  # three lots at 0x1000, 0x1098, 0x1130 in a 0x2000 block; patch lots 1 and 3 only, lot 2 must survive
        def __init__(self):
            self.mem = bytearray(range(256)) * 32
            self.writes = 0

        def read_bytes(self, a, n):
            return bytes(self.mem[a:a + n])

        def write_bytes(self, a, d, n):
            self.mem[a:a + n] = d
            self.writes += 1
    pmf, before = FakePm(), None
    before = bytes(pmf.mem)
    _patch_weights(pmf, {0x1000: b"\xAA" * 16, 0x1130: b"\xBB" * 16})
    assert pmf.writes == 1 and pmf.mem[0x1040:0x1050] == b"\xAA" * 16 and pmf.mem[0x1170:0x1180] == b"\xBB" * 16
    assert pmf.mem[0x1098 + 0x40:0x1098 + 0x50] == before[0x1098 + 0x40:0x1098 + 0x50]  # middle lot unchanged
    assert bytes(pmf.mem[:0x1040]) == before[:0x1040] and bytes(pmf.mem[0x1180:]) == before[0x1180:]
    assert len(UPGRADES) == 8 + 1 + 9 + 1 + 20 and UPGRADES["Somber Smithing Stone [9]"] == 10200 and UPGRADES["Ghost Glovewort [9]"] == 10918
    assert add_upgrades([], 5).startswith("Tick") and add_upgrades(["Smithing Stone [1]"], 1000).startswith("Quantity")
    assert add_upgrade("nope", 1).startswith("Pick") and add_upgrade("Smithing Stone [1]", 0).startswith("Quantity")
    st = {"Ranni": [{"flag": 1, "gate": "set", "and": [{"flag": 2, "gate": "clear"}], "lines": ["Hello."]},
                    {"flag": 3, "gate": "clear", "and": [], "lines": ["Later."]}],
          "Fia": [{"flag": 9, "gate": "set", "lines": ["Never."]}]}  # no 'and' key, as in the real data
    assert quest_report(lambda f: f in (1, 3), st) == 'Ranni:\n    "Hello."'  # 2 clear, 3 set -> only the first matches
    assert quest_report(lambda f: f == 2, st) == 'Ranni:\n    "Later."' and quest_report(lambda f: f == 9, st).startswith("Fia:")
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
