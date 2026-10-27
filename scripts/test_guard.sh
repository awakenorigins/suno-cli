#!/usr/bin/env bash
# test_guard.sh — regression suite for suno_prompt_guard.py (v0.7.0)
# Run: bash scripts/test_guard.sh   (exit 0 = all pass)
set -u
cd "$(dirname "$0")/.."
G=scripts/suno_prompt_guard.py
PASS=0; FAIL=0
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

check() { # name expected_exit actual_exit
  if [ "$2" -eq "$3" ]; then PASS=$((PASS+1)); echo "PASS $1";
  else FAIL=$((FAIL+1)); echo "FAIL $1 (expected exit $2, got $3)"; fi
}

# --- fixtures ---------------------------------------------------------------
cat > "$T/good-style.txt" <<'EOF'
Leading Genre DNA: sacred choral hymn, warm Vietnamese devotional anthem, cathedral space, slow radiant build; Singer Identity: clear Vietnamese female lead with luminous calm; Vocal Register: soprano-alto blend, breath-soft verse, opening high refrain; Rhythm Section: near-still pulse, long swells, gentle timpani, no square beat; Signature Motif: lamp, one light passing to many, dawn, open palm; Hook Design: short Vietnamese hook, one natural dạ, ascending release; Arrangement Map: intro, verse, pre, chorus, verse, bridge, final chorus, coda; Instrument Palette: choir, strings, pipe organ soft, piano, temple bell, warm pad, đàn tranh; Mood Logic: trao ánh sáng về muôn lối, sacred and hopeful, grateful and vast, deeply human, never cheesy; Mix Target: wide sacred hall, close intimate vocal, deep warm low end, -1db; Strictly Avoid: English, slogans, melodrama, autotune, crude words.
EOF

cat > "$T/good-lyrics.txt" <<'EOF'
[Intro]

Một ngọn đèn thắp trong tay lặng

[Verse]

Con nhận chút sáng hôm nay
xin trao lại
cho lối chưa ai đi qua
cho người còn đứng trong mưa
Dạ, ánh này không giữ
chỉ mượn rồi chuyền

[Pre-Chorus]

Đèn châm đèn
sáng thêm sáng
tay mở tay
đường bớt tối

[Chorus]

Trao anh về muôn lối
ngọn con thắp đêm nay
Dạ, xin về muôn lối
không chừa một ai

[Verse]

Nếu có phần yên trong lòng
xin chia đều
cho cỏ cho sương cho gió
cho tên chưa kịp gọi thành
Dạ, con không cất riêng
sáng là để trao

[Bridge]

Không giữ cho riêng
gió mang đi khắp
biển nhận về sông
sông trả về nguồn
một tay buông ra
trăm tay cùng sáng

[Chorus]

Trao anh về muôn lối
ngọn con thắp đêm nay
Dạ, xin về muôn lối
sáng cho ngày mai

[Outro]

Một đèn thành trăm đèn
lặng mà không tắt
Dạ, trao rồi lại đầy
EOF

cat > "$T/bad-lyrics.txt" <<'EOF'
[Intro]
hello world tonight
[Verse]
yeah baby love
[Chorus]
hành trình tỏa sáng cháy hết mình vươn xa mãi mãi không ngừng nghỉ nhé em ơi
EOF

cat > "$T/inst.txt" <<'EOF'
[Intro]
soft piano
[Build]
strings rise
[Outro]
fade
EOF

cat > "$T/short-style.txt" <<'EOF'
pop, upbeat, fun
EOF

# --- tests --------------------------------------------------------------------
python3 $G precheck --file "$T/good-style.txt" --max-chars 950 >/dev/null 2>&1
check "precheck good style" 0 $?

python3 $G precheck --file "$T/short-style.txt" --max-chars 950 >/dev/null 2>&1
check "precheck rejects tag-list" 1 $?

python3 $G lyrics-check --file "$T/good-lyrics.txt" --title "Dạ Trao Anh Về Muôn Lối" >/dev/null 2>&1
check "lyrics-check good (with artistry)" 0 $?

python3 $G lyrics-check --file "$T/bad-lyrics.txt" --title "Bad Song" >/dev/null 2>&1
check "lyrics-check rejects english+cliche" 1 $?

python3 $G lyrics-check --file "$T/inst.txt" --instrumental >/dev/null 2>&1
check "instrumental mode" 0 $?

python3 $G lyrics-check --file "$T/good-lyrics.txt" --title "Dạ Trao Anh Về Muôn Lối" --no-artistry >/dev/null 2>&1
check "legacy no-artistry flag" 0 $?

python3 $G gen-gate --title "Dạ Trao Anh Về Muôn Lối" --style-file "$T/good-style.txt" --lyrics-file "$T/good-lyrics.txt" >/dev/null 2>&1
check "gen-gate all layers pass" 0 $?

python3 $G gen-gate --title "Bad" --style-file "$T/short-style.txt" --lyrics-file "$T/bad-lyrics.txt" >/dev/null 2>&1
check "gen-gate rejects bad prompt" 1 $?

# hook repetition: mutate second chorus first line
sed '0,/Trao anh về muôn lối/!{s/Trao anh về muôn lối/Câu hook khác hẳn rồi/}' "$T/good-lyrics.txt" > "$T/no-hook.txt" 2>/dev/null || cp "$T/good-lyrics.txt" "$T/no-hook.txt"
python3 - "$T/good-lyrics.txt" "$T/no-hook.txt" <<'PYEOF'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
first = text.find("Trao anh về muôn lối")
second = text.find("Trao anh về muôn lối", first + 1)
if second > -1:
    text = text[:second] + "Câu hook khác hẳn rồi" + text[second + len("Trao anh về muôn lối"):]
open(dst, "w", encoding="utf-8").write(text)
PYEOF
python3 $G lyrics-check --file "$T/no-hook.txt" --title "Dạ Trao Anh Về Muôn Lối" >/dev/null 2>&1
check "artistry catches non-repeating hook" 1 $?

# da typo (ą)
sed 's/Dạ, ánh này không giữ/Dą, ánh này không giữ/' "$T/good-lyrics.txt" > "$T/da-typo.txt"
python3 $G lyrics-check --file "$T/da-typo.txt" --title "Dạ Trao Anh Về Muôn Lối" >/dev/null 2>&1
check "artistry catches da->dą typo" 1 $?

bash -n scripts/suno_batch_runner.sh
check "batch runner syntax" 0 $?

echo "----------------------------------------"
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
