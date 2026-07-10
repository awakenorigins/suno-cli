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

# --- v0.7.0 Lyric Artistry Guard ---------------------------------------------
# Viral/hook-oriented artistry rules for Vietnamese vocal songs:
#  A-01 ai_cliche_phrases      : generic AI V-pop phrases kill authenticity
#  A-02 english_leakage        : 0% English inside Vietnamese lyrics
#  A-03 markdown_or_emdash     : prose formatting must never leak into lyrics
#  A-04 hook_repetition        : hook line must recur (>=2 chorus blocks share it)
#  A-05 hook_too_long          : hook line must stay punchy (<=9 words)
#  A-06 unsingable_lines       : >12-word lines break phrasing; max 2 tolerated
#  A-07 da_typo                : Vietnamese 'dạ' must never degrade to 'dą'
#  A-08 stanza_monotony        : all stanzas same line-count = robotic structure
AI_CLICHE_VI = [
    "hành trình", "khát vọng", "vươn xa", "tỏa sáng", "cháy hết mình",
    "chạm tới ước mơ", "vượt qua giông bão", "thắp sáng ước mơ",
    "con tim rực cháy", "không gục ngã", "vươn tới trời cao",
    "đam mê rực cháy", "hòa chung nhịp đập", "trái tim rực lửa",
    "bạn và tôi cùng nhau", "nắm chặt tay nhau tiến bước", "mạnh mẽ vượt qua",
]
ENGLISH_COMMON = {
    "the", "you", "love", "baby", "night", "light", "heart", "fire", "time",
    "life", "dream", "forever", "tonight", "girl", "boy", "yeah", "oh", "my",
    "me", "we", "and", "is", "are", "your", "shine", "sky", "fly", "high",
    "world", "feel", "real", "soul", "stay", "way", "day", "know", "now",
}
MARKDOWN_MARKS = ["**", "##", "```", "---", "\u2014"]
VI_DIACRITICS = "ăâêôơưđáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ"

def _chorus_blocks(text: str) -> list[list[str]]:
    blocks, cur, in_chorus = [], [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            if in_chorus and cur:
                blocks.append(cur)
            cur = []
            ns = norm(s)
            in_chorus = "chorus" in ns and "pre" not in ns
            continue
        if in_chorus and s:
            cur.append(s)
    if in_chorus and cur:
        blocks.append(cur)
    return blocks

def lyric_artistry_score(text: str) -> tuple[bool, list[str]]:
    t = norm(text)
    missing: list[str] = []
    cliche_hits = [c for c in AI_CLICHE_VI if norm(c) in t]
    if cliche_hits:
        missing.append("ai_cliche_phrases:" + "|".join(cliche_hits[:4]))
    body_lines = lyric_lines(text)
    eng_hits = []
    for line in body_lines:
        for w in re.findall(r"[A-Za-z]+", line):
            lw = w.lower()
            if lw in ENGLISH_COMMON and not any(ch in line for ch in VI_DIACRITICS):
                eng_hits.append(lw)
    if eng_hits:
        missing.append("english_leakage:" + "|".join(sorted(set(eng_hits))[:5]))
    md_hits = [m for m in MARKDOWN_MARKS if m in text]
    if md_hits:
        missing.append("markdown_or_emdash")
    if "ą" in text or "Ą" in text:
        missing.append("da_typo_a_ogonek")
    blocks = _chorus_blocks(text)
    if len(blocks) >= 2:
        hook = norm(blocks[0][0]) if blocks[0] else ""
        if hook:
            recur = sum(1 for b in blocks if b and norm(b[0]) == hook)
            if recur < 2:
                missing.append("hook_not_repeated")
        if blocks[0] and len(blocks[0][0].split()) > 9:
            missing.append("hook_too_long")
    elif blocks:
        missing.append("single_chorus_block")
    long_lines = [l for l in body_lines if len(l.split()) > 12]
    if len(long_lines) > 2:
        missing.append(f"unsingable_lines:{len(long_lines)}")
    stanza_sizes = []
    size = 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            if size:
                stanza_sizes.append(size)
            size = 0
        elif s:
            size += 1
    if size:
        stanza_sizes.append(size)
    if len(stanza_sizes) >= 5 and len(set(stanza_sizes)) == 1:
        missing.append("stanza_monotony")
    return (not missing, missing)

# F-01: instrumental-aware lyrics gate. Instrumental prompts carry section maps, not sung lyrics.
INSTRUMENTAL_STYLE_HINTS = [
    "instrumental", "no vocals", "no vocal", "no singer", "no singing", "no lead vocal",
    "without vocals", "wordless", "piano trio", "lounge jazz",
]

def is_instrumental_style(style: str) -> bool:
    t = norm(style or "")
    return any(norm(h) in t for h in INSTRUMENTAL_STYLE_HINTS)

def lyric_score_instrumental(text: str) -> tuple[bool, list[str]]:
    """Instrumental mode: require a section map (>=3 [Section] tags), forbid filler.
    No verse/chorus/diacritics/700-char requirements (those are vocal-song rules)."""
    t = norm(text)
    missing = []
    sections = [l.strip() for l in text.splitlines() if l.strip().startswith("[")]
    if len(sections) < 3:
        missing.append("missing_instrumental_sections_min3")
    if any(x in t for x in AI_FILLER):
        missing.append("placeholder_or_ai_filler")
    return (not missing, missing)

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

def guard_slugify(text: str) -> str:
    import unicodedata
    text = text.replace('đ', 'd').replace('Đ', 'D')
    text = ''.join(ch for ch in unicodedata.normalize('NFKD', text) if not unicodedata.combining(ch))
    text = re.sub(r'[^A-Za-z0-9]+', '-', text).strip('-')
    return re.sub(r'-+', '-', text) or 'untitled'

def cmd_route(args):
    text = args.text or Path(args.file).read_text(encoding='utf-8')
    playlist = args.playlist or resolve_playlist(text)
    missing = []
    manifest_info = None
    if not playlist:
        missing.append('playlist_album_unresolved')
    # Real validation (not decorative): when an albums root is given, the provided/resolved
    # playlist must map to an existing album manifest with a matching genre.
    if playlist and getattr(args, 'albums_root', None):
        mp = Path(args.albums_root) / guard_slugify(playlist) / 'album.json'
        if not mp.exists():
            missing.append('playlist_manifest_missing')
        else:
            try:
                man = json.loads(mp.read_text(encoding='utf-8'))
            except Exception:
                man = {}
            manifest_info = {'name': man.get('name'), 'genre': man.get('genre'), 'path': str(mp)}
            if man.get('name') and man.get('name') != playlist:
                missing.append('playlist_manifest_name_mismatch')
            if getattr(args, 'expect_genre', None) and man.get('genre') != args.expect_genre:
                missing.append('playlist_genre_mismatch')
    ok = not missing
    manifest = write_manifest(args, playlist, text) if ok and args.manifest else None
    print(json.dumps({"ok": ok, "playlist": playlist, "album": playlist, "missing": missing, "manifest": manifest, "manifest_info": manifest_info, "audit": "playlist_album_routing_before_generation"}, ensure_ascii=False, indent=2))
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
    instrumental = bool(getattr(args, 'instrumental', False)) or is_instrumental_style(getattr(args, 'style', '') or '')
    if instrumental:
        ok, missing = lyric_score_instrumental(text)
        audit = "lyrics_instrumental_section_map_no_filler"
    else:
        ok, missing = lyric_score(text, args.title)
        audit = "lyrics_structure_hook_vietnamese_integrity"
        if not getattr(args, 'no_artistry', False):
            art_ok, art_missing = lyric_artistry_score(text)
            if not art_ok:
                ok = False
                missing = missing + [f"artistry:{m}" for m in art_missing]
            audit += "+artistry_hook_viral_v070"
    print(json.dumps({"ok": ok, "instrumental": instrumental, "length": len(text), "missing": missing, "audit": audit}, ensure_ascii=False, indent=2))
    return 0 if ok else 1

def cmd_gate(args):
    """One-shot pre-generation gate: style precheck + lyrics-check(+artistry)
    [+ novelty when history given]. Single entry point so runners cannot skip
    a layer. Exit 0 only when EVERY layer passes."""
    style = Path(args.style_file).read_text(encoding='utf-8')
    lyrics = Path(args.lyrics_file).read_text(encoding='utf-8')
    report = {"title": args.title, "layers": {}, "audit": "gen_gate_all_layers_v070"}
    s_ok, s_missing = score(style)
    d_ok, d_missing = production_density(style)
    style_missing = s_missing + [f"low_production_detail:{m}" for m in d_missing]
    if len(style) > args.max_chars:
        style_missing.append(f"too_long:{len(style)}>{args.max_chars}")
    report["layers"]["style"] = {"ok": not style_missing, "missing": style_missing, "length": len(style)}
    instrumental = bool(getattr(args, 'instrumental', False)) or is_instrumental_style(style)
    if instrumental:
        l_ok, l_missing = lyric_score_instrumental(lyrics)
    else:
        l_ok, l_missing = lyric_score(lyrics, args.title)
        a_ok, a_missing = lyric_artistry_score(lyrics)
        if not a_ok:
            l_ok = False
            l_missing = l_missing + [f"artistry:{m}" for m in a_missing]
    report["layers"]["lyrics"] = {"ok": l_ok, "missing": l_missing, "instrumental": instrumental}
    if args.history:
        history = load_history(args.history)
        candidate = '\n'.join([args.title or '', style, lyrics])
        n_hits = []
        for r in history:
            prev = '\n'.join(str(r.get(k, '')) for k in ['title', 'style', 'style_prompt', 'tags', 'lyrics', 'prompt'])
            sc = novelty_sim(candidate, prev)
            if sc >= args.threshold:
                n_hits.append({'similarity': round(sc, 3), 'title': r.get('title')})
        line_ok, line_hits = line_pattern_check(lyrics, history, args.recent)
        motif_ok, motif_hits = motif_window_check(candidate, history, args.recent)
        n_ok = (not n_hits) and line_ok and motif_ok
        report["layers"]["novelty"] = {"ok": n_ok, "whole_song_hits": n_hits[:3], "line_pattern_hits": line_hits[:3], "motif_window_hits": motif_hits[:3]}
    ok = all(layer["ok"] for layer in report["layers"].values())
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1

def tokens_for_novelty(s: str) -> set[str]:
    stop=set('và của trong một những cho người không này kia với trên dưới giữa bài hát ta tôi anh em là có còn như đã the and with for into'.split())
    return {w for w in re.findall(r'[A-Za-zÀ-ỹĐđ]{3,}', norm(s)) if w not in stop}

def novelty_sim(a: str, b: str) -> float:
    A=tokens_for_novelty(a); B=tokens_for_novelty(b)
    return len(A & B) / max(1, len(A | B))

def load_history(path: str | None) -> list[dict]:
    if not path: return []
    p=Path(path)
    if not p.exists(): return []
    try:
        data=json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data,list) else data.get('runs',[])
    except Exception:
        return []


# Broad keyword/pattern-class guard for lyric formula leakage.
# Do not overfit to one exact example; flag rhetorical frames that repeat across recent songs.
LINE_PATTERN_CLASSES = {
    "conditional_surpass": ["nếu", "cao", "hơn"],
    "darkness_light_reversal": ["đêm", "tối", "sáng"],
    "ash_dawn_transformation": ["tro", "tàn", "bình", "minh"],
    "hand_sky_lift": ["tay", "trời", "nâng"],
    "call_return_light": ["gọi", "về", "sáng"],
    "wound_to_song": ["vết", "thương", "hát"],
    "death_to_immortality": ["chết", "sống", "mãi"],
}

RECENT_MOTIF_KEYWORDS = {
    "sun_light_cluster": ["mặt trời", "bình minh", "ánh sáng", "màu sáng", "trời mở"],
    "child_hand_cluster": ["đứa trẻ", "tay bé", "bàn tay", "nâng"],
    "night_height_cluster": ["đêm", "bóng tối", "cao", "hát cao"],
    "ash_rebirth_cluster": ["tro", "tàn", "hóa", "bình minh"],
    "sky_star_cluster": ["trời", "sao", "thiên hà", "vũ trụ"],
}

def line_pattern_signature(line: str) -> set[str]:
    t = norm(line)
    sig = set()
    for name, kws in LINE_PATTERN_CLASSES.items():
        hit = sum(1 for k in kws if norm(k) in t)
        # conditional_surpass needs all core words; other classes need >=2 to stay broad but not hypersensitive.
        if (name == "conditional_surpass" and hit >= 3) or (name != "conditional_surpass" and hit >= 2):
            sig.add(name)
    return sig

def lyric_lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith('[')]

def line_pattern_check(candidate: str, history_rows: list[dict], threshold_recent: int = 10) -> tuple[bool, list[dict]]:
    cand_lines = lyric_lines(candidate)
    cand = [(l, line_pattern_signature(l)) for l in cand_lines]
    cand = [(l, s) for l, s in cand if s]
    hits = []
    # F-03: history is append-only; the most recent rows live at the TAIL, not the head.
    for row in reversed(history_rows[-threshold_recent:]):
        prev_text = '\n'.join(str(row.get(k,'')) for k in ['lyrics','prompt'])
        title = row.get('title') or row.get('id') or 'previous'
        for pl in lyric_lines(prev_text):
            ps = line_pattern_signature(pl)
            if not ps: continue
            for cl, cs in cand:
                shared = sorted(cs & ps)
                if shared:
                    hits.append({'class': shared, 'candidate_line': cl, 'previous_line': pl, 'previous_title': title})
                    if len(hits) >= 8:
                        return False, hits
    return (not hits), hits

def motif_window_check(candidate: str, history_rows: list[dict], threshold_recent: int = 10) -> tuple[bool, list[dict]]:
    t = norm(candidate)
    cand_clusters = set()
    for name, kws in RECENT_MOTIF_KEYWORDS.items():
        if sum(1 for k in kws if norm(k) in t) >= 2:
            cand_clusters.add(name)
    hits = []
    # F-03: slice the most recent rows from the tail of the append-only history.
    for row in reversed(history_rows[-threshold_recent:]):
        prev = norm('\n'.join(str(row.get(k,'')) for k in ['title','style','style_prompt','tags','lyrics','prompt']))
        for name in cand_clusters:
            kws = RECENT_MOTIF_KEYWORDS[name]
            if sum(1 for k in kws if norm(k) in prev) >= 2:
                hits.append({'cluster': name, 'previous_title': row.get('title') or row.get('id') or 'previous'})
    return (not hits), hits[:8]

def cmd_novelty(args):
    style = args.style or (Path(args.style_file).read_text(encoding='utf-8') if args.style_file else '')
    lyrics = args.lyrics or (Path(args.lyrics_file).read_text(encoding='utf-8') if args.lyrics_file else '')
    candidate = '\n'.join([args.title or '', style, lyrics])
    history = load_history(args.history)
    # v0.7.1: whole-song similarity compares TITLE+LYRICS only. Style prompts share a
    # fixed genre/production scaffold (same 11-block skeleton per album), which inflated
    # combined similarity above threshold and caused ~98% false-positive novelty blocks
    # while actual lyric similarity was far below threshold. Style repetition is still
    # covered by motif_window_check (style included there).
    creative_candidate = '\n'.join([args.title or '', lyrics]) if lyrics else candidate
    hits=[]
    for r in history:
        prev_lyrics = '\n'.join(str(r.get(k,'')) for k in ['title','lyrics','prompt'])
        prev_full = '\n'.join(str(r.get(k,'')) for k in ['title','style','style_prompt','tags','lyrics','prompt'])
        prev = prev_lyrics if lyrics and (r.get('lyrics') or r.get('prompt')) else prev_full
        sc=novelty_sim(creative_candidate, prev)
        if sc >= args.threshold:
            hits.append({'similarity': round(sc,3), 'title': r.get('title'), 'id': r.get('id'), 'created_at': r.get('created_at')})
    line_ok, line_hits = line_pattern_check(lyrics, history, args.recent)
    motif_ok, motif_hits = motif_window_check(candidate, history, args.recent)
    ok = (not hits) and line_ok and motif_ok
    print(json.dumps({
        'ok':ok,
        'threshold':args.threshold,
        'whole_song_hits':hits[:5],
        'line_pattern_hits':line_hits,
        'motif_window_hits':motif_hits,
        'recent_window':args.recent,
        'audit':'novelty_subject_lyric_prompt_motif_line_pattern_keyword_class'
    },ensure_ascii=False,indent=2))
    return 0 if ok else 2

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='cmd', required=True)
    a=sub.add_parser('build-compact'); a.add_argument('--max-chars', type=int, default=1000)
    a=sub.add_parser('precheck'); a.add_argument('--text'); a.add_argument('--file'); a.add_argument('--max-chars', type=int, default=1000)
    a=sub.add_parser('postcheck'); a.add_argument('clip_ids', nargs='+'); a.add_argument('--suno', default='/root/.openclaw/workspace/bin/suno')
    a=sub.add_parser('lyrics-check'); a.add_argument('--text'); a.add_argument('--file'); a.add_argument('--title'); a.add_argument('--instrumental', action='store_true', help='Instrumental mode: validate section map instead of vocal-song rules'); a.add_argument('--style', help='Style/tags text used to auto-detect instrumental mode'); a.add_argument('--no-artistry', action='store_true', help='Skip v0.7.0 artistry layer (legacy behavior)')
    a=sub.add_parser('gen-gate'); a.add_argument('--title', required=True); a.add_argument('--style-file', required=True); a.add_argument('--lyrics-file', required=True); a.add_argument('--max-chars', type=int, default=950); a.add_argument('--instrumental', action='store_true'); a.add_argument('--history'); a.add_argument('--threshold', type=float, default=0.34); a.add_argument('--recent', type=int, default=10)
    a=sub.add_parser('playlist-route'); a.add_argument('--text'); a.add_argument('--file'); a.add_argument('--title', required=True); a.add_argument('--playlist'); a.add_argument('--manifest'); a.add_argument('--albums-root', help='Albums manifest root (suno-library/_albums); enables real manifest validation'); a.add_argument('--expect-genre', help='Require the resolved playlist manifest genre to equal this value')
    a=sub.add_parser('novelty-check'); a.add_argument('--title', required=True); a.add_argument('--style'); a.add_argument('--style-file'); a.add_argument('--lyrics'); a.add_argument('--lyrics-file'); a.add_argument('--history'); a.add_argument('--threshold', type=float, default=0.34); a.add_argument('--recent', type=int, default=10)
    args=p.parse_args()
    return {'build-compact':cmd_build,'precheck':cmd_pre,'postcheck':cmd_post,'lyrics-check':cmd_lyrics,'playlist-route':cmd_route,'novelty-check':cmd_novelty,'gen-gate':cmd_gate}[args.cmd](args)
if __name__ == '__main__': sys.exit(main())
