"""Build data/items.json (item search list for Gideon's Ledger) from YOUR copy of the Hexinton Elden Ring cheat table.

Usage:  python build_items.py "C:/path/to/Hexinton ... .zip"      (or the .CT file inside it)
The table is not redistributed here; this reads the Weapon/Armor/Talisman/Goods/Magic/Ash of War ID lists from your own copy.
Each entry is [name, category, item id with the category prefix the game's give-item function expects].
"""
import html, json, os, re, sys, zipfile

CATS = {"Weapon": 0x0, "Armor": 0x10000000, "Talisman": 0x20000000, "Goods": 0x40000000, "Magic": 0x40000000, "Ash of War": 0x80000000}


def read_ct(path):
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".ct"))
            return z.read(name).decode("utf-8", "ignore")
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def parse(text):
    items, seen = [], set()
    for m in re.finditer(r"<DropDownList\b[^>]*>(.*?)</DropDownList>", text, re.S):
        descs = re.findall(r'<Description>"?([^<]*?)"?</Description>', text[max(0, m.start() - 2500):m.start()])
        if len(descs) < 2 or descs[-1] != "ID" or descs[-2] not in CATS:  # the param tables' ID lists, e.g. [..., "Weapon", "ID"]
            continue
        cat = descs[-2]
        for line in m.group(1).splitlines():
            ident, _, name = line.strip().partition(":")
            name = html.unescape(name).strip()
            if not ident.isdigit() or not name or name == "None" or name.lower().startswith("test"):
                continue
            iid = CATS[cat] | int(ident)
            if iid not in seen:
                seen.add(iid)
                items.append([name, "Goods" if cat == "Magic" else cat, iid])
    return sorted(items, key=lambda it: it[0].lower())


def selftest():
    ct = ('<Description>"Weapon"</Description><Description>"ID"</Description><DropDownList ReadOnly="1">\n0:None\n110000:Unarmed\n'
          '1000100:Heavy Dagger\n</DropDownList><Description>"Goods"</Description><Description>"ID"</Description>'
          '<DropDownList>\n100:Tarnished&apos;s Furled Finger\n</DropDownList>'
          '<Description>"Other"</Description><Description>"ID"</Description><DropDownList>\n5:Skip Me\n</DropDownList>')
    got = parse(ct)
    assert got == [["Heavy Dagger", "Weapon", 1000100], ["Tarnished's Furled Finger", "Goods", 0x40000064], ["Unarmed", "Weapon", 110000]], got
    print("build_items selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif len(sys.argv) != 2:
        print(__doc__)
    else:
        items = parse(read_ct(sys.argv[1]))
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "items.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(items, f)
        print(f"Wrote {len(items)} items to {out}")
