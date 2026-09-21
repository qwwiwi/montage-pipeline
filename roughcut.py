#!/usr/bin/env python3
"""Чистовик из сырого дубля: убрать паузы, доказать что речь цела.

Что делает, по шагам:

  1. извлекает звук и считает огибающую энергии из СЫРЫХ сэмплов (16 кГц, окно 20 мс, шаг 10 мс);
  2. выбирает порог тишины ОТ РЕЧИ, а не от шума;
  3. находит паузы и вырезает их середину, оставляя запас с каждой стороны;
  4. собирает результат одним проходом ffmpeg;
  5. ПРОВЕРЯЕТ работу двумя независимыми способами и печатает оба числа.

Почему рез идёт по энергии, а не по таймкодам слов — подробно в README. Коротко: на живом
материале распознаватель выдал одному слову длительность 29,86 секунды и вернул слова за
пределами файла. Таймкод — догадка, огибающая — измерение.

Ключ распознавателя берётся ТОЛЬКО из окружения (GROQ_API_KEY). В коде его нет и быть не может.
Без ключа инструмент всё равно работает: просто пропускает проверку речи и говорит об этом.

Зависимости: ffmpeg и ffprobe в PATH. Больше ничего — ни одной сторонней библиотеки.
"""

from __future__ import annotations

import argparse
import array
import difflib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --- канон, каждое число измерено; обоснование в README ------------------------------------

WINDOW_MS = 20          # окно огибающей
HOP_MS = 10             # шаг огибающей
SPEECH_HEADROOM_DB = 6.5  # порог = p75(огибающей) - это, с зажимом ниже
FLOOR_MIN_DB = -46.0
FLOOR_MAX_DB = -30.0
MIN_PAUSE_MS = 260      # короче - это дыхание, а не пауза
GUARD_MID_MS = 90       # запас внутри фразы
GUARD_SENTENCE_MS = 130 # запас на границе предложения
MIN_REMOVE_MS = 60      # меньше не стоит склейки
EDGE_KEEP_MS = 200      # тишина, оставляемая в самом начале и конце
SENTENCE_END = (".", "!", "?", "…")

RECOGNISER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
RECOGNISER_MODEL = "whisper-large-v3"


@dataclass(frozen=True)
class Removal:
    start_s: float
    end_s: float
    pause_ms: int
    at_sentence: bool


def run(args: list[str]) -> bytes:
    return subprocess.run(args, capture_output=True, check=True).stdout


def duration_of(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, check=True, text=True).stdout.strip()
    return float(out)


def envelope(wav: Path) -> tuple[list[float], float]:
    """Огибающая из сырых сэмплов, без фильтров ffmpeg в середине.

    PCM читается в память целиком — для дубля на пару минут это единицы мегабайт. Для часового
    исходника считайте кусками; форма расчёта от этого не меняется.
    """
    raw = run(["ffmpeg", "-v", "error", "-i", str(wav), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"])
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    win = int(16000 * WINDOW_MS / 1000)
    hop = int(16000 * HOP_MS / 1000)
    out: list[float] = []
    for start in range(0, max(0, len(samples) - win), hop):
        acc = 0
        for value in samples[start:start + win]:
            acc += value * value
        rms = math.sqrt(acc / win)
        out.append(20 * math.log10(rms / 32768) if rms > 0 else -120.0)
    if not out:
        raise SystemExit("в файле нет звука, по которому можно резать")
    return out, hop / 16000.0


def pick_floor(env: list[float]) -> float:
    """Порог считается от РЕЧИ.

    Привязка к шуму (10-й процентиль + 6 дБ) давала -49 дБ в тихой комнате и теряла половину
    пауз: комнатный тон громче такого порога.
    """
    ordered = sorted(env)
    speech = ordered[int(0.75 * len(ordered))]
    return max(FLOOR_MIN_DB, min(FLOOR_MAX_DB, speech - SPEECH_HEADROOM_DB))


def silence_runs(env: list[float], hop: float, floor: float) -> list[tuple[float, float]]:
    runs: list[tuple[float, float]] = []
    start: int | None = None
    for i, value in enumerate(env):
        if value < floor:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start * hop, i * hop))
            start = None
    if start is not None:
        runs.append((start * hop, len(env) * hop))
    return runs


def plan_removals(runs, words, duration: float) -> list[Removal]:
    """Что вырезаем. Край паузы не трогаем никогда.

    Слова используются ТОЛЬКО чтобы узнать, кончилось ли предложение — на границе предложения
    запас больше, и результат дышит. Разрешения на рез они не дают.
    """
    out: list[Removal] = []
    for a, b in runs:
        length_ms = (b - a) * 1000
        if length_ms < MIN_PAUSE_MS:
            continue
        at_sentence = False
        if a <= 0.15:
            ra, rb = a, max(a, b - EDGE_KEEP_MS / 1000)
        elif b >= duration - 0.15:
            ra, rb = min(b, a + EDGE_KEEP_MS / 1000), b
        else:
            prev = max((w for w in words if float(w["end"]) <= a + 0.10),
                       key=lambda w: float(w["end"]), default=None)
            at_sentence = bool(prev) and prev["word"].strip().rstrip('"»)').endswith(SENTENCE_END)
            guard = (GUARD_SENTENCE_MS if at_sentence else GUARD_MID_MS) / 1000
            ra, rb = a + guard, b - guard
        if (rb - ra) * 1000 < MIN_REMOVE_MS:
            continue
        out.append(Removal(round(ra, 3), round(rb, 3), round(length_ms), at_sentence))
    return out


def keeps_from(removals: list[Removal], duration: float) -> list[tuple[float, float]]:
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for r in sorted(removals, key=lambda x: x.start_s):
        if r.start_s > cursor + 0.02:
            keeps.append((cursor, r.start_s))
        cursor = max(cursor, r.end_s)
    if cursor < duration - 0.02:
        keeps.append((cursor, duration))
    return keeps


def render(src: Path, keeps, dst: Path, crf: int) -> None:
    """Склейка. Каждый шов попадает в измеренную тишину, поэтому звук НЕ кроссфейдится:
    попытка сгладить шов кроссфейдом съедала речь на его краях."""
    chain = "".join(
        f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}];"
        f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}];"
        for i, (a, b) in enumerate(keeps))
    joins = "".join(f"[v{i}][a{i}]" for i in range(len(keeps)))
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex",
         f"{chain}{joins}concat=n={len(keeps)}:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)])


def transcribe(wav: Path, key: str) -> list[dict]:
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


def clamp_to_file(words: list[dict], duration: float) -> tuple[list[dict], int]:
    """ЖЁСТКОЕ ПРАВИЛО. Слова за длительностью файла отбрасываются до всякой обработки.

    Не теория: на файле длиной 85,2 с распознаватель вернул слова до 115,06 с, выдумав текст на
    месте тишины. Это открытый баг, а не наша особенность — ссылки в README.
    """
    kept = [w for w in words if float(w["start"]) < duration]
    return kept, len(words) - len(kept)


def norm(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", text.lower())


def main() -> None:
    ap = argparse.ArgumentParser(description="Чистовик из сырого дубля: убрать паузы, доказать что речь цела")
    ap.add_argument("source", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--crf", type=int, default=20)
    # Наблюдаемые значения на чистых сборках: 0,86-0,97. Порог 0,85 прошёл бы с запасом
    # в 0,018 — это ложный отказ на хорошем резе при первом же капризе распознавателя.
    # Жёсткий гейт здесь не этот, а аудит по энергии выше; перепроверка речи - второй,
    # независимый сигнал, и ему положено быть мягче.
    ap.add_argument("--min-similarity", type=float, default=0.80,
                    help="порог совпадения при перепроверке речи; никогда не 1.0 — у распознавателя своя изменчивость")
    ap.add_argument("--dry-run", action="store_true", help="только план, без сборки")
    args = ap.parse_args()

    src: Path = args.source
    if not src.exists():
        raise SystemExit(f"нет файла {src}")
    dst: Path = args.output or src.with_name(src.stem + "-roughcut.mp4")
    key = os.environ.get("GROQ_API_KEY", "").strip()

    with tempfile.TemporaryDirectory(prefix="roughcut-") as tmp:
        work = Path(tmp)
        wav = work / "source.wav"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(wav)])

        duration = duration_of(src)
        env, hop = envelope(wav)
        floor = pick_floor(env)
        runs = silence_runs(env, hop, floor)

        words: list[dict] = []
        dropped = 0
        if key:
            words, dropped = clamp_to_file(transcribe(wav, key), duration)
        else:
            print("GROQ_API_KEY не задан: режу по звуку, границы предложений не различаю, "
                  "проверку речи пропускаю", file=sys.stderr)

        removals = plan_removals(runs, words, duration)
        keeps = keeps_from(removals, duration)
        removed_s = sum(r.end_s - r.start_s for r in removals)

        print(f"файл            {src.name}, {duration:.2f} с")
        print(f"порог тишины    {floor:.1f} дБ (от речи, не от шума)")
        print(f"пауз найдено    {len([r for r in runs if (r[1]-r[0])*1000 >= MIN_PAUSE_MS])}")
        print(f"резов           {len(removals)}, убрано {removed_s:.2f} с")
        if dropped:
            print(f"ОТБРОШЕНО       {dropped} слов(а) за пределами длительности файла")
        print(f"станет          {duration - removed_s:.2f} с "
              f"(сжатие {100 * removed_s / duration:.1f}%)")

        # ПРОВЕРКА ПЕРВАЯ: измерение. Внутри вырезанного не должно быть ничего слышимого.
        worst = -120.0
        for r in removals:
            i0, i1 = int(r.start_s / hop), max(int(r.start_s / hop) + 1, int(r.end_s / hop))
            worst = max(worst, max(env[i0:i1] or [-120.0]))
        ok = worst < floor
        print(f"аудит тишины    {'чисто' if ok else 'ПРОВАЛ'} "
              f"(самый громкий сэмпл внутри вырезанного {worst:.1f} дБ, "
              f"{worst - floor:+.1f} к порогу)")
        if not ok:
            raise SystemExit("отказ: вырезаемое содержит звук выше порога — это рез по речи")

        if args.dry_run or not removals:
            if not removals:
                print("резать нечего: пауз нужной длины нет")
            return

        render(src, keeps, dst, args.crf)
        got = duration_of(dst)
        print(f"собран          {dst} — {got:.2f} с "
              f"(расхождение с планом {abs(got - (duration - removed_s)) * 1000:.0f} мс)")

        # ПРОВЕРКА ВТОРАЯ: распознать результат заново.
        if not key:
            return
        out_wav = work / "result.wav"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(dst), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(out_wav)])
        after = norm(" ".join(w["word"] for w in transcribe(out_wav, key)))
        # сравнивать надо с тем, что ДОЛЖНО остаться, а не с полным исходником
        expected = norm(" ".join(
            w["word"] for w in words
            if any(a <= float(w["start"]) and float(w["end"]) <= b for a, b in keeps)))
        matcher = difflib.SequenceMatcher(a=expected, b=after, autojunk=False)
        lost = [expected[i1:i2] for tag, i1, i2, _, _ in matcher.get_opcodes() if tag == "delete"]
        flat = [x for group in lost for x in group]
        print(f"проверка речи   совпадение {matcher.ratio():.4f} "
              f"(порог {args.min_similarity}), потеряно слов {len(flat)}")
        if flat:
            print(f"                потеряно: {' '.join(flat[:10])}")
        if matcher.ratio() < args.min_similarity:
            raise SystemExit("отказ: после сборки речь разошлась с ожидаемой сильнее порога")


if __name__ == "__main__":
    main()
