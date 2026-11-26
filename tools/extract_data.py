#!/usr/bin/env python3
"""
Rebuild Alchemy Table's ingredient and effect data from your own Skyrim SE/AE install.

Usage:
    python extract_data.py "<Skyrim>/Data" [--extra "<MO2>/mods/<mod>" ...] --inject ../index.html

Reads INGR and MGEF records from the official plugins (base game, DLC, and every Creation
Club / Anniversary Edition plugin that contains ingredients) and the English and Russian
names from the .strings files packed in the BSAs. Plugins and BSAs are looked up in the Data
folder and in any --extra folder (for example a Mod Organizer 2 mod that holds the
Anniversary Edition content). Writes data.json and fidmap.json next to this script; with
--inject it also replaces the DATA and FIDMAP lines inside index.html.
Python 3.8+, standard library only.
"""
import argparse, json, os, re, struct, zlib

# Base game and the four free Creation Club plugins, in load order.
PLUGINS = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
           "ccBGSSSE001-Fish.esm", "ccBGSSSE025-AdvDSGS.esm", "ccBGSSSE037-Curios.esl"]
# Any other cc*.esm/esl found in the search folders that contains INGR records is added after
# these (Anniversary Edition content) and tagged "AE".
SRC = {"Skyrim.esm": "S", "Update.esm": "S", "Dawnguard.esm": "DG", "HearthFires.esm": "HF", "Dragonborn.esm": "DB",
       "ccBGSSSE001-Fish.esm": "FISH", "ccBGSSSE025-AdvDSGS.esm": "SS", "ccBGSSSE037-Curios.esl": "CUR"}
LANGS = ["english", "russian"]
SKIP = {"MS03BlackBriarSecretIngredient"}           # quest item, never usable
MERGE = {"ccBGSSSE025_FlameStalk": "ccBGSSSE037_FlameStalk"}  # same ingredient shipped twice
# Other copies (same English name and identical effects as an earlier ingredient) are merged
# automatically into the earlier one, so the bag shows a single entry.


def lz4_block(src, out):
    i, n = 0, len(src)
    while i < n:
        tok = src[i]; i += 1; lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255: break
        out += src[i:i + lit]; i += lit
        if i >= n: break
        off = src[i] | (src[i + 1] << 8); i += 2; ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255: break
        ml += 4; st = len(out) - off
        if off >= ml: out += out[st:st + ml]
        else:
            for k in range(ml): out.append(out[st + k])
    return out


def lz4_frame(data):
    flg = data[4]; p = 6
    if flg & 8: p += 8
    if flg & 1: p += 4
    p += 1; out = bytearray()
    while True:
        bs = struct.unpack_from("<I", data, p)[0]; p += 4
        if bs == 0: break
        unc = bs & 0x80000000; bs &= 0x7FFFFFFF; blk = data[p:p + bs]; p += bs
        if unc: out += blk
        else: lz4_block(blk, out)
        if flg & 0x10: p += 4
    return bytes(out)


def bsa_files(path, want):
    with open(path, "rb") as f:
        _, ver, _, aflags, nfold, _, _, tfln, _ = struct.unpack_from("<4sIIIIIIIH", f.read(36))
        counts = []
        for _ in range(nfold):
            if ver == 105: _, c, _, _ = struct.unpack("<QIIQ", f.read(24))
            else: _, c, _ = struct.unpack("<QII", f.read(16))
            counts.append(c)
        recs = []
        for c in counts:
            ln = f.read(1)[0]; folder = f.read(ln).rstrip(b"\0").decode("latin1")
            for _ in range(c):
                _, sz, fo = struct.unpack("<QII", f.read(16)); recs.append((folder, sz, fo))
        names = f.read(tfln).split(b"\0"); res = {}
        for (folder, sz, fo), n in zip(recs, names):
            full = (folder + "\\" + n.decode("latin1")).lower()
            if not want(full): continue
            f.seek(fo); size = sz & 0x3FFFFFFF; comp = bool(aflags & 4) != bool(sz & 0x40000000)
            if aflags & 0x100:
                l = f.read(1)[0]; f.read(l); size -= l + 1
            data = f.read(size)
            if comp: data = lz4_frame(data[4:]) if ver == 105 else zlib.decompress(data[4:])
            res[full] = data
        return res


def parse_strings(b):
    cnt, _ = struct.unpack_from("<II", b); base = 8 + cnt * 8; res = {}
    for i in range(cnt):
        sid, off = struct.unpack_from("<II", b, 8 + i * 8)
        s = b[base + off:b.index(b"\0", base + off)]
        try: res[sid] = s.decode("utf-8")
        except UnicodeDecodeError: res[sid] = s.decode("cp1252", "replace")
    return res


def subrecords(data):
    p, big = 0, None
    while p < len(data):
        t = data[p:p + 4]; sz = struct.unpack_from("<H", data, p + 4)[0]; p += 6
        if t == b"XXXX": big = struct.unpack_from("<I", data, p)[0]; p += sz; continue
        if big is not None: sz, big = big, None
        yield t, data[p:p + sz]; p += sz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", help="Skyrim Special Edition\\Data folder")
    ap.add_argument("--extra", action="append", default=[], help="extra folder with plugins/BSAs (repeatable)")
    ap.add_argument("--inject", help="index.html to update in place")
    a = ap.parse_args()
    dirs = [a.data_dir] + a.extra
    files = {}
    for d in dirs:
        for f in os.listdir(d):
            files.setdefault(f.lower(), os.path.join(d, f))
    find = lambda name: files.get(name.lower())

    # plugin list: base, then every other cc plugin that contains ingredients
    order = [p for p in PLUGINS if find(p)]
    for p in PLUGINS:
        if not find(p): print("missing, skipped:", p)
    base_l = {p.lower() for p in PLUGINS}
    extra = sorted(f for f in files if f.startswith("cc") and f.endswith((".esm", ".esl")) and f not in base_l)
    for f in extra:
        if b"INGR" in open(files[f], "rb").read():
            order.append(os.path.basename(files[f]))

    strings = {}
    bsas = ["Skyrim - Interface.bsa"] + [os.path.splitext(p)[0] + ".bsa" for p in order if p.lower().startswith("cc")]
    for bsa in bsas:
        path = find(bsa)
        if not path: continue
        got = bsa_files(path, lambda n: n.startswith("strings") and any(n.endswith("_" + l + ".strings") for l in LANGS))
        for k, v in got.items():
            plug, lang = os.path.basename(k)[:-8].rsplit("_", 1); strings[(plug, lang)] = parse_strings(v)

    ingr, mgef = {}, {}
    for fn in order:
        bb = open(find(fn), "rb").read()
        hsz = struct.unpack_from("<I", bb, 4)[0]; localized = bool(struct.unpack_from("<I", bb, 8)[0] & 0x80)
        masters = [v.rstrip(b"\0").decode() for t, v in subrecords(bb[24:24 + hsz]) if t == b"MAST"] + [fn]
        plug = os.path.splitext(fn)[0].lower()

        def name(sv):
            if not localized: return {l: sv.rstrip(b"\0").decode("cp1252", "replace") for l in LANGS}
            sid = struct.unpack_from("<I", sv)[0]
            return {l: strings.get((plug, l), {}).get(sid) for l in LANGS}

        def key(fid):
            mi = fid >> 24; return (masters[mi] if mi < len(masters) else fn) + ":" + format(fid & 0xFFFFFF, "06X")

        def walk(p, end):
            while p < end:
                t = bb[p:p + 4]
                if t == b"GRUP":
                    gs = struct.unpack_from("<I", bb, p + 4)[0]
                    if struct.unpack_from("<i", bb, p + 12)[0] == 0 and bb[p + 8:p + 12] not in (b"INGR", b"MGEF"):
                        p += gs; continue
                    walk(p + 24, p + gs); p += gs; continue
                ds, fl, fid = struct.unpack_from("<III", bb, p + 4)
                if t in (b"INGR", b"MGEF"):
                    data = bb[p + 24:p + 24 + ds]
                    if fl & 0x40000: data = zlib.decompress(data[4:])
                    k = key(fid); r = {"file": fn}; effs = []; cur = None
                    for st, sv in subrecords(data):
                        if st == b"EDID": r["edid"] = sv.rstrip(b"\0").decode("latin1")
                        elif st == b"FULL": r["name"] = name(sv)
                        elif t == b"INGR" and st == b"DATA": r["value"], r["weight"] = struct.unpack_from("<If", sv)
                        elif t == b"INGR" and st == b"EFID": cur = {"m": key(struct.unpack_from("<I", sv)[0])}; effs.append(cur)
                        elif t == b"INGR" and st == b"EFIT" and cur is not None: cur["mag"], _, cur["dur"] = struct.unpack_from("<fII", sv)
                        elif t == b"MGEF" and st == b"DATA": r["flags"], r["cost"] = struct.unpack_from("<If", sv)
                    tbl = ingr if t == b"INGR" else mgef
                    if t == b"INGR": r["effects"] = effs
                    if k in tbl and "name" not in r: r["name"] = tbl[k].get("name")
                    tbl[k] = r
                p += 24 + ds
        walk(24 + hsz, len(bb))

    used, I, F = {}, [], {}
    seen = {}  # (english name, effects) -> edid, for automatic merging of copies
    for k, v in ingr.items():
        ed = v.get("edid")
        if ed in SKIP or not v.get("effects") or not v.get("name"): continue
        plug, loc = k.split(":")
        en = v["name"].get("english") or ed
        ru = v["name"].get("russian") or en
        sig = (en, tuple((e["m"], round(e.get("mag", 0), 2), e.get("dur", 0)) for e in v["effects"]))
        target = MERGE.get(ed) or seen.get(sig)
        if target and target != ed:
            F[ed] = [plug, int(loc, 16), target]; continue
        seen.setdefault(sig, ed)
        effs = []
        for e in v["effects"]:
            used.setdefault(e["m"], len(used))
            effs.append([used[e["m"]], round(e.get("mag", 0), 2), e.get("dur", 0)])
        src = SRC.get(v["file"], "AE") + ("+SS" if ed in MERGE.values() else "")
        I.append([ed, en, ru, src, v.get("value"), round(v.get("weight", 0), 3), effs])
        F[ed] = [plug, int(loc, 16)]
    E = [None] * len(used)
    for m, idx in used.items():
        g = mgef.get(m, {}); nm = g.get("name") or {}
        E[idx] = [g.get("edid"), nm.get("english"), nm.get("russian") or nm.get("english"), g.get("flags", 0), round(g.get("cost", 0), 4)]

    data = json.dumps({"E": E, "I": I}, ensure_ascii=False, separators=(",", ":"))
    fidmap = json.dumps(F, separators=(",", ":"))
    here = os.path.dirname(os.path.abspath(__file__))
    open(os.path.join(here, "data.json"), "w", encoding="utf-8").write(data)
    open(os.path.join(here, "fidmap.json"), "w", encoding="utf-8").write(fidmap)
    print(f"{len(order)} plugins, {len(I)} ingredients, {len(E)} effects, {len(F) - len(I)} merged copies")
    if a.inject:
        html = open(a.inject, encoding="utf-8").read()
        html = re.sub(r"^const DATA=.*;$", lambda _: "const DATA=" + data + ";", html, count=1, flags=re.M)
        html = re.sub(r"^const FIDMAP=.*;$", lambda _: "const FIDMAP=" + fidmap + ";", html, count=1, flags=re.M)
        open(a.inject, "w", encoding="utf-8").write(html)
        print("updated", a.inject)


if __name__ == "__main__":
    main()
