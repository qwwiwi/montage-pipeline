#!/usr/bin/env python3
"""Субтитры поверх готового чистовика. Второй заход, и он не двигает ни одной склейки.

Формат — ASS через libass. Не drawtext: drawtext рисует статичный текст и ломается на `:` и `,`
внутри строки, а ASS умеет подсветку по словам, обводку, положение и кириллицу, и стоит копейки
по процессору.

ГЛАВНОЕ ПРАВИЛО ШВА: этот шаг работает поверх ЗАМОРОЖЕННОГО чистовика. Он не может сдвинуть рез,
потому что не знает о резах вообще — он получает готовый файл и его собственную расшифровку.
Поэтому все доказательства первого захода остаются в силе.

Текст субтитров НЕ придумывается. Он берётся из расшифровки готового файла. Правка отдельного
слова допустима, замена фразы — нет: иначе видео начинает врать.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RECOGNISER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
RECOGNISER_MODEL = "whisper-large-v3"

# Безопасная зона Reels: нижние 672 пикселя занимает интерфейс Instagram, последняя пригодная
# строка — 1248. Но при крупной съёмке подпись там ложится на лицо, поэтому канон (вариант B,
# выбран владельцем) ставит её НИЖЕ ЛИЦА, на 1400.
#
# Плата за это названа прямо: ниже 1248 начинается полоса, которую Instagram может перекрыть.
# Нижние 300-400 пикселей занимает интерфейс всегда, промежуток между 1248 и ~1500 обычно
# свободен, но его высота зависит от ДЛИНЫ ПОДПИСИ к ролику. Отсюда правило продукта: когда
# субтитры стоят ниже безопасной линии, подпись к ролику держат короткой.
CANON_Y = 1400
SAFE_LINE = 1248

PRESETS: dict[str, dict[str, object]] = {
    # Канон. Тонкая обводка, без заливки и плашки — для кадра, где главное лицо.
    "quiet": {"size": 52, "outline": 4, "shadow": 0, "bold": 0,
              "primary": "&H00FFFFFF", "outline_colour": "&HBF000000", "back": "&H00000000",
              "border_style": 1},
    # Строка на полупрозрачной подложке: читается на любом фоне.
    "plate": {"size": 54, "outline": 0, "shadow": 0, "bold": 1,
              "primary": "&H00FFFFFF", "outline_colour": "&H00000000", "back": "&HA6000000",
              "border_style": 3},
    # Толстый контур без подложки, стиль ленты новостей.
    "outline": {"size": 58, "outline": 7, "shadow": 0, "bold": 1,
                "primary": "&H00FFFFFF", "outline_colour": "&HFF000000", "back": "&H00000000",
                "border_style": 1},
}


def run(args: list[str]) -> bytes:
    return subprocess.run(args, capture_output=True, check=True).stdout


def transcribe(video: Path, key: str) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="captions-") as tmp:
        wav = Path(tmp) / "a.wav"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(wav)])
        out = subprocess.run(
            ["curl", "-sS", "--max-time", "600", RECOGNISER_URL,
             "-H", f"Authorization: Bearer {key}",
             "-F", f"model={RECOGNISER_MODEL}", "-F", "response_format=verbose_json",
             "-F", "timestamp_granularities[]=word", "-F", f"file=@{wav}"],
            capture_output=True, check=True).stdout
    doc = json.loads(out)
    if "words" not in doc:
        raise SystemExit(f"распознаватель не вернул слова: {str(doc)[:200]}")
    return [w for w in doc["words"] if w.get("word", "").strip()]


def duration_of(p: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(p)],
                                capture_output=True, check=True, text=True).stdout.strip())


def clamp_to_file(words: list[dict], duration: float) -> tuple[list[dict], int]:
    """То же жёсткое правило, что и в резе: слово за пределами файла не существует."""
    kept = [w for w in words if float(w["start"]) < duration]
    return kept, len(words) - len(kept)


def group_lines(words: list[dict], max_chars: int, max_gap: float) -> list[dict]:
    """Слова в строки.

    Строка рвётся по трём причинам, и все три нужны: длина, пауза между словами, и знак
    завершения. Без паузы строка склеивает две мысли через вдох; без длины уезжает за кадр.
    """
    lines: list[dict] = []
    cur: list[dict] = []
    for w in words:
        text = w["word"].strip()
        if cur:
            gap = float(w["start"]) - float(cur[-1]["end"])
            too_long = len(" ".join(x["word"].strip() for x in cur)) + 1 + len(text) > max_chars
            ended = cur[-1]["word"].strip().rstrip('"»)').endswith((".", "!", "?", "…"))
            if gap > max_gap or too_long or ended:
                lines.append({"start": float(cur[0]["start"]), "end": float(cur[-1]["end"]),
                              "words": cur})
                cur = []
        cur.append(w)
    if cur:
        lines.append({"start": float(cur[0]["start"]), "end": float(cur[-1]["end"]), "words": cur})
    return lines


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def build_ass(lines: list[dict], preset: dict, font: str, y: int, width: int, height: int,
              highlight: str | None) -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{preset['size']},{preset['primary']},{preset['primary']},{preset['outline_colour']},{preset['back']},{preset['bold']},0,0,0,100,100,0,0,{preset['border_style']},{preset['outline']},{preset['shadow']},2,60,60,{y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows: list[str] = []
    for line in lines:
        if highlight is None:
            body = escape(" ".join(w["word"].strip() for w in line["words"]))
            rows.append(f"Dialogue: 0,{ass_time(line['start'])},{ass_time(line['end'])},"
                        f"Main,,0,0,0,,{body}")
            continue
        # Подсветка звучащего слова: строка перерисовывается на каждое слово, активное красится.
        for i, w in enumerate(line["words"]):
            parts = []
            for j, x in enumerate(line["words"]):
                token = escape(x["word"].strip())
                parts.append(f"{{\\c{highlight}}}{token}{{\\c{preset['primary']}}}"
                             if j == i else token)
            rows.append(f"Dialogue: 0,{ass_time(float(w['start']))},{ass_time(float(w['end']))},"
                        f"Main,,0,0,0,,{' '.join(parts)}")
    return head + "\n".join(rows) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Субтитры поверх готового чистовика")
    ap.add_argument("video", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--preset", choices=sorted(PRESETS), default="quiet")
    ap.add_argument("--font", default="Golos Text")
    # libass ищет шрифты через fontconfig. Наш шрифт лежит в образе сервиса и системе неизвестен,
    # поэтому каталог передаётся явно — иначе libass молча подставит первый попавшийся и
    # кириллица приедет чужой гарнитурой.
    ap.add_argument("--fontsdir", type=Path, default=Path("."))
    ap.add_argument("--y", type=int, default=CANON_Y,
                    help=f"отступ снизу до строки; канон {CANON_Y}, безопасная линия Reels {SAFE_LINE}")
    ap.add_argument("--max-chars", type=int, default=32)
    ap.add_argument("--max-gap", type=float, default=0.45)
    ap.add_argument("--highlight", default=None,
                    help="цвет подсветки звучащего слова в формате ASS, например &H004DD3FF")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--ass-only", action="store_true", help="только собрать .ass, не жечь")
    args = ap.parse_args()

    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise SystemExit("нужен GROQ_API_KEY: текст субтитров берётся из расшифровки, не выдумывается")

    duration = duration_of(args.video)
    words, dropped = clamp_to_file(transcribe(args.video, key), duration)
    if dropped:
        print(f"ОТБРОШЕНО       {dropped} слов(а) за пределами длительности файла")

    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0",
                            str(args.video)], capture_output=True, check=True, text=True).stdout
    width, height = (int(x) for x in probe.strip().split(",")[:2])

    lines = group_lines(words, args.max_chars, args.max_gap)
    # ВЫРАВНИВАНИЕ РЕШАЕТ, ОТКУДА СЧИТАЕТСЯ ОТСТУП. При выравнивании 8 (верх) MarginV меряется
    # от ВЕРХА, и первый прогон положил строку на 540 вместо 1400 — ровно на лицо. Нужно
    # выравнивание 2 (низ), и тогда MarginV — это высота строки над нижним краем.
    margin_v = height - args.y
    ass = build_ass(lines, PRESETS[args.preset], args.font, margin_v, width, height,
                    args.highlight)

    dst = args.output or args.video.with_name(args.video.stem + "-subs.mp4")
    ass_path = dst.with_suffix(".ass")
    ass_path.write_text(ass, encoding="utf-8")

    print(f"слов            {len(words)}, строк {len(lines)}")
    print(f"стиль           {args.preset}, шрифт {args.font}, строка на {args.y} "
          f"({'ниже' if args.y > SAFE_LINE else 'выше'} безопасной линии {SAFE_LINE})")
    print(f"разметка        {ass_path}")
    if args.ass_only:
        return

    run(["ffmpeg", "-y", "-v", "error", "-i", str(args.video),
         "-vf", f"subtitles={ass_path}:fontsdir={args.fontsdir}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", str(dst)])
    got = duration_of(dst)
    same = abs(got - duration) < 0.05
    print(f"собран          {dst} — {got:.2f} с")
    print(f"длительность    {'не изменилась' if same else 'ИЗМЕНИЛАСЬ, это дефект'} "
          f"(было {duration:.2f})")
    if not same:
        raise SystemExit("отказ: второй заход не имеет права менять длину чистовика")


if __name__ == "__main__":
    main()
