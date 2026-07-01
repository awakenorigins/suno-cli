#!/usr/bin/env python3
"""Guard Suno structured PromptStyle before/after generation.

Pre-submit: ensure compressed style tags still preserve the source structure DNA.
Post-gen: inspect generated clip metadata and require the same markers/DNA before delivery.
"""
from __future__ import annotations
import argparse, json, re, sys, subprocess
from pathlib import Path

REQUIRED = [
    ("Leading Genre DNA", ["leading genre dna"]),
    ("Singer Identity", ["singer identity"]),
    ("Vocal Register", ["vocal register"]),
    ("Rhythm Section", ["rhythm section"]),
    ("Signature Motif", ["signature motif"]),
    ("Hook Design", ["hook design"]),
    ("Arrangement Map", ["arrangement map"]),
    ("Instrument Palette", ["instrument palette"]),
    ("Mood Logic", ["mood logic"]),
    ("Mix Target", ["mix target"]),
    ("Strictly Avoid", ["strictly avoid"]),
]
NEGATIVE_HINTS = ["generic", "karaoke", "muddy", "weak hook", "recycled", "pop-washing"]

def norm(s: str) -> str:
    return s.lower().replace("đ", "d")

def present(text: str, kws: list[str]) -> bool:
    t = norm(text)
    return any(norm(k) in t for k in kws)

def score(text: str) -> tuple[bool, list[str]]:
    missing = [name for name,kws in REQUIRED if not present(text,kws)]
    return (not missing, missing)

def build_compact() -> str:
    return (
        "Leading Genre DNA: epic cinematic Vietnamese hybrid trailer ballad, 88-94 BPM, massive 6/8, orchestral folk-fusion; "
        "Singer Identity: emotional Vietnamese female lead, heroic childlike fragility; "
        "Vocal Register: intimate low verse, lifted pre-chorus, wide belt chorus, choir final hook; "
        "Rhythm Section: taiko heartbeat, floor toms, sub pulses; "
        "Signature Motif: child raises paper sun, hope from ash, broken horizon lights; "
        "Hook Design: title-drop 'Gọi mặt trời về', choir answer, brass lift; "
        "Arrangement Map: dusty đàn bầu intro→sparse verse→rising pre→full orchestral hook→prayer bridge→final choir lift→sunlit outro; "
        "Instrument Palette: đàn bầu, tremolo strings, low brass, soft piano, children choir, risers; "
        "Mood Logic: grief→courage→first light→collective hope→sacred release; "
        "Mix Target: clear Vietnamese vocal front, separated low-end, wide cinematic master; "
        "Strictly Avoid: generic stock loop, karaoke backing, muddy choir, weak hook, recycled melody, pop-washing"
    )

LYRIC_REQUIRED_SECTIONS = ["intro", "verse", "pre", "hook", "chorus", "bridge", "outro"]
AI_FILLER = ["lalala", "placeholder", "insert lyrics", "tbd", "chatgpt"]

def lyric_score(text: str, title: str | None = None) -> tuple[bool, list[str]]:
    t = norm(text)
    missing = []
    for sec in LYRIC_REQUIRED_SECTIONS:
        if f"[{sec}" not in t and (sec != "hook" or "[chorus" not in t):
            missing.append(f"missing_section:{sec}")
    if title:
        title_words = [w for w in re.split(r"\W+", norm(title)) if len(w) >= 3]
        if title_words and not any(w in t for w in title_words):
            missing.append("missing_title_hook")
    if any(x in t for x in AI_FILLER):
        missing.append("placeholder_or_ai_filler")
    if len(text.strip()) < 700:
        missing.append("too_short_for_full_song")
    # Vietnamese integrity: require common diacritics to survive, not ascii-only.
    if not any(ch in text for ch in "ăâêôơưđáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ"):
        missing.append("vietnamese_diacritics_missing")
    return (not missing, missing)

def production_density(text: str) -> tuple[bool, list[str]]:
    t = norm(text)
    checks = {
        "instrument_texture": ["reverb", "texture", "tremolo", "sub", "close", "pad", "brass", "strings", "đàn", "dan"],
        "dynamic_arc": ["opens", "verse", "pre", "chorus", "bridge", "detonates", "build", "lift", "outro"],
        "mix_specifics": ["db", "reverb", "close-mic", "low-end", "wide", "master", "front", "isolated"],
        "emotional_adjectives": ["human", "fragility", "grief", "courage", "hope", "sacred", "aching", "heroic"],
    }
    missing=[]
    for name,kws in checks.items():
        if sum(1 for k in kws if norm(k) in t) < 2:
            missing.append(name)
    return (not missing, missing)

PLAYLIST_RULES = [
    ("Epic-Cinematic-Trailer-Score", ["epic cinematic", "trailer", "hybrid orchestral", "film score", "orchestral"]),
    ("Vietnamese-Soul-Cinematic", ["vietnamese soul", "đàn", "dan tranh", "dan bau", "erhu"]),
    ("Pop-Ballad", ["pop", "ballad", "v-pop"]),
    ("EDM-Dance", ["edm", "dance", "vinahouse", "house", "rave"]),
]

def resolve_playlist(text: str) -> str | None:
    t = norm(text)
    best = None
    best_score = 0
    for name, kws in PLAYLIST_RULES:
        sc = sum(1 for k in kws if norm(k) in t)
        if sc > best_score:
            best_score = sc
            best = name
    return best if best_score else None

def write_manifest(args, playlist: str, text: str) -> str:
    out = Path(args.manifest or 'suno_generation_manifest.json')
    payload = {
        "ok": True,
        "title": args.title,
        "playlist": playlist,
        "album": playlist,
        "style_length": len(text),
        "status": "pre_generation_routed",
        "rule": "PromptStyle must resolve playlist/album before Suno submit; postcheck must add/download under this target."
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(out)

def cmd_route(args):
    text = args.text or Path(args.file).read_text(encoding='utf-8')
    playlist = args.playlist or resolve_playlist(text)
    missing = []
    if not playlist:
        missing.append('playlist_album_unresolved')
    ok = not missing
    manifest = write_manifest(args, playlist, text) if ok and args.manifest else None
    print(json.dumps({"ok": ok, "playlist": playlist, "album": playlist, "missing": missing, "manifest": manifest, "audit": "playlist_album_routing_before_generation"}, ensure_ascii=False, indent=2))
    return 0 if ok else 1

def cmd_pre(args):
    text = args.text or Path(args.file).read_text(encoding='utf-8')
    ok, missing = score(text)
    dense_ok, dense_missing = production_density(text)
    if not dense_ok:
        missing = missing + [f"low_production_detail:{m}" for m in dense_missing]
        ok = False
    if len(text) > args.max_chars:
        print(json.dumps({"ok": False, "error": "too_long", "length": len(text), "max": args.max_chars}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": ok, "length": len(text), "missing": missing, "audit": "prompt_music_11_blocks_plus_production_density"}, ensure_ascii=False, indent=2))
    return 0 if ok else 1

def cmd_build(args):
    text = build_compact()
    ok, missing = score(text)
    print(json.dumps({"ok": ok, "length": len(text), "missing": missing, "style": text}, ensure_ascii=False, indent=2))
    return 0 if ok and len(text) <= args.max_chars else 1

def cmd_post(args):
    infos=[]; failures=[]
    for cid in args.clip_ids:
        raw = subprocess.check_output([args.suno, "info", cid, "--json"], text=True)
        data=json.loads(raw)["data"]
        tags=(data.get("metadata") or {}).get("tags") or ""
        ok, missing = score(tags)
        if missing:
            failures.append({"id": cid, "missing": missing, "tags": tags})
        infos.append({"id": cid, "status": data.get("status"), "duration": (data.get("metadata") or {}).get("duration"), "tags": tags})
    out={"ok": not failures, "clips": infos, "failures": failures}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not failures else 1

def cmd_lyrics(args):
    text = args.text or Path(args.file).read_text(encoding='utf-8')
    ok, missing = lyric_score(text, args.title)
    print(json.dumps({"ok": ok, "length": len(text), "missing": missing, "audit": "lyrics_structure_hook_vietnamese_integrity"}, ensure_ascii=False, indent=2))
    return 0 if ok else 1

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='cmd', required=True)
    a=sub.add_parser('build-compact'); a.add_argument('--max-chars', type=int, default=1000)
    a=sub.add_parser('precheck'); a.add_argument('--text'); a.add_argument('--file'); a.add_argument('--max-chars', type=int, default=1000)
    a=sub.add_parser('postcheck'); a.add_argument('clip_ids', nargs='+'); a.add_argument('--suno', default='/root/.openclaw/workspace/bin/suno')
    a=sub.add_parser('lyrics-check'); a.add_argument('--text'); a.add_argument('--file'); a.add_argument('--title')
    a=sub.add_parser('playlist-route'); a.add_argument('--text'); a.add_argument('--file'); a.add_argument('--title', required=True); a.add_argument('--playlist'); a.add_argument('--manifest')
    args=p.parse_args()
    return {'build-compact':cmd_build,'precheck':cmd_pre,'postcheck':cmd_post,'lyrics-check':cmd_lyrics,'playlist-route':cmd_route}[args.cmd](args)
if __name__ == '__main__': sys.exit(main())
