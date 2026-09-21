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
HEAD_KEEP_MS = 200      # тишина, оставляемая в самом начале
# А в конце нужен ВОЗДУХ, и это не симметрично началу. С запасом 200 мс ролик обрывается сразу
# после последнего слова и ощущается оборванным — владелец сказал «в конце сильно прерывается»,
# и замер подтвердил: клип кончался ровно через 200 мс после речи. Начало можно резать плотно,
# конец нельзя: зритель домысливает паузу после мысли, а её нет.
TAIL_KEEP_MS = 600
SENTENCE_END = (".", "!", "?", "…")

RECOGNISER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
RECOGNISER_MODEL = "whisper-large-v3"


@dataclass(frozen=True)
class Removal:
    start_s: float
    end_s: float
    pause_ms: int
    at_sentence: bool
    # Рез тишины или рез по решению модели. Разница не косметическая: от неё зависит, КАКОЙ
    # ВОПРОС задаёт аудит. См. `audit` в main().
    by_model: bool = False


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
            ra, rb = a, max(a, b - HEAD_KEEP_MS / 1000)
        elif b >= duration - 0.15:
            ra, rb = min(b, a + TAIL_KEEP_MS / 1000), b
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


def render_speed_silence(src: Path, removals: list["Removal"], duration: float, dst: Path,
                         crf: int, speed: float, silence_speed: float) -> None:
    """Не вырезать паузу, а ПРОЛЕТЕТЬ её.

    Идея подсмотрена у OpenMontage (AGPL) — но только идея: код здесь свой, три строки фильтра.
    Склейки не видно вовсе, потому что её нет: пауза остаётся, просто идёт в несколько раз
    быстрее. Для кадра, где человек в паузе меняет позу, это честнее джамп-ката.

    Видео каждого куска получает свой `setpts`, звук — `atempo`. Ускорение тишины бывает больше
    2,0, которые `atempo` принимает за раз, поэтому оно раскладывается на множители.
    """
    def atempo_chain(factor: float) -> str:
        parts: list[str] = []
        left = factor
        while left > 2.0:
            parts.append("atempo=2.0")
            left /= 2.0
        while left < 0.5:
            parts.append("atempo=0.5")
            left /= 0.5
        parts.append(f"atempo={left:.6f}")
        return ",".join(parts)

    pieces: list[tuple[float, float, float]] = []
    cursor = 0.0
    for r in sorted(removals, key=lambda x: x.start_s):
        if r.start_s > cursor + 0.02:
            pieces.append((cursor, r.start_s, 1.0))
        pieces.append((r.start_s, r.end_s, silence_speed))
        cursor = max(cursor, r.end_s)
    if cursor < duration - 0.02:
        pieces.append((cursor, duration, 1.0))

    chain = ""
    for i, (a, b, f) in enumerate(pieces):
        # СКОБКИ ОБЯЗАТЕЛЬНЫ. `setpts=PTS-STARTPTS/6` разбирается как `PTS - (STARTPTS/6)`,
        # то есть таймлайн не сжимается, а разъезжается: первый прогон дал 1967 секунд вместо 71.
        vf = f"setpts=(PTS-STARTPTS)/{f}" if f != 1.0 else "setpts=PTS-STARTPTS"
        af = f"asetpts=PTS-STARTPTS,{atempo_chain(f)}" if f != 1.0 else "asetpts=PTS-STARTPTS"
        chain += f"[0:v]trim=start={a}:end={b},{vf}[v{i}];[0:a]atrim=start={a}:end={b},{af}[a{i}];"
    joins = "".join(f"[v{i}][a{i}]" for i in range(len(pieces)))
    graph = f"{chain}{joins}concat=n={len(pieces)}:v=1:a=1[vc][ac]"
    if abs(speed - 1.0) < 1e-6:
        vout, aout = "[vc]", "[ac]"
    else:
        graph += f";[vc]setpts=PTS/{speed}[v];[ac]{atempo_chain(speed)}[a]"
        vout, aout = "[v]", "[a]"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", graph,
         "-map", vout, "-map", aout,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)])


def seam_contact_sheet(src: Path, removals: list["Removal"], out_dir: Path, limit: int = 8) -> list[Path]:
    """ВИЗУАЛЬНАЯ проверка: кадр ДО и кадр ПОСЛЕ каждого шва, рядом.

    Две машинные проверки отвечают на вопрос «не срезали ли речь». Они ничего не говорят о том,
    ВИДНО ли склейку: человек в паузе успевает сменить позу, и звук при этом идеально чист.
    Поэтому третья проверка — глазами, но по подготовленному материалу, а не отсматриванием
    всего ролика.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for n, r in enumerate(sorted(removals, key=lambda x: x.start_s)[:limit]):
        before = max(0.0, r.start_s - 0.04)
        after = r.end_s + 0.04
        left = out_dir / f"seam{n:02d}-a.png"
        right = out_dir / f"seam{n:02d}-b.png"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{before}", "-i", str(src), "-frames:v", "1",
             "-vf", "scale=360:-2", str(left)])
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{after}", "-i", str(src), "-frames:v", "1",
             "-vf", "scale=360:-2", str(right)])
        sheet = out_dir / f"seam{n:02d}.png"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(left), "-i", str(right),
             "-filter_complex", "[0:v][1:v]hstack=inputs=2", str(sheet)])
        left.unlink(missing_ok=True)
        right.unlink(missing_ok=True)
        made.append(sheet)
    return made


def render(src: Path, keeps, dst: Path, crf: int, speed: float) -> None:
    """Склейка, и только потом ускорение.

    Каждый шов попадает в измеренную тишину, поэтому звук НЕ кроссфейдится: попытка сгладить шов
    кроссфейдом съедала речь на его краях.

    ПОРЯДОК ВАЖЕН. Резать надо на ИСХОДНОЙ скорости, а ускорять уже собранное. Если ускорить
    сначала, все измеренные таймкоды разъедутся и аудит будет проверять не то, что вырезано.
    Поэтому ускорение — последний фильтр в цепочке, после concat.

    `atempo` растягивает время, не трогая высоту тона: голос не станет писклявым. Он принимает
    0,5-2,0 за один проход, так что обычные 1,0-1,5 проходят одним фильтром.
    """
    chain = "".join(
        f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}];"
        f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}];"
        for i, (a, b) in enumerate(keeps))
    joins = "".join(f"[v{i}][a{i}]" for i in range(len(keeps)))
    graph = f"{chain}{joins}concat=n={len(keeps)}:v=1:a=1[vc][ac]"
    if abs(speed - 1.0) < 1e-6:
        vout, aout = "[vc]", "[ac]"
    else:
        graph += f";[vc]setpts=PTS/{speed}[v];[ac]atempo={speed}[a]"
        vout, aout = "[v]", "[a]"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", graph,
         "-map", vout, "-map", aout,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)])


def render_audio_only(src: Path, keeps, dst: Path) -> None:
    """Собрать ТОЛЬКО звук чистовика, на исходной скорости.

    Зачем отдельный проход: проверка речи должна идти по тому, что мы ВЫРЕЗАЛИ, а не по тому,
    что потом растянули. Измерено на живом файле: при ускорении 1,3 совпадение падает с 0,8677
    до 0,8478 и распознаватель «теряет» три слова, которых рез не касался — план реза в обоих
    прогонах был буквально одинаковый. Быстрая речь просто хуже распознаётся.

    Звуковая склейка стоит доли секунды против полного перекодирования видео, поэтому проверка
    получается и честнее, и дешевле.
    """
    chain = "".join(f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}];"
                    for i, (a, b) in enumerate(keeps))
    joins = "".join(f"[a{i}]" for i in range(len(keeps)))
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex",
         f"{chain}{joins}concat=n={len(keeps)}:v=0:a=1[a]", "-map", "[a]",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)])


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
    ap.add_argument("--speed", type=float, default=1.3,
                    help="ускорение готового чистовика; применяется ПОСЛЕ реза, тон не меняется. "
                         "1.0 — не ускорять")
    ap.add_argument("--drop", action="append", default=[], metavar="ОТ-ДО",
                    help="убрать отрезок целиком, в секундах: --drop 60.8-62.8. "
                         "Это ВХОД ДЛЯ РЕШЕНИЯ МОДЕЛИ: дубли и неудачные заходы выбирает она, "
                         "код только исполняет и проверяет. Можно повторять.")
    ap.add_argument("--silence", choices=["remove", "speed", "mark"], default="remove",
                    help="что делать с паузой: вырезать, ускорить, или только разметить")
    ap.add_argument("--silence-speed", type=float, default=6.0,
                    help="во сколько раз ускорять паузу в режиме speed")
    ap.add_argument("--seams", type=Path, default=None,
                    help="куда положить кадры швов для проверки глазами (до и после каждого реза)")
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

        # Решение модели: отрезки, которые она велела выбросить целиком. Границы, названные по
        # тексту, СНАПЯТСЯ В ИЗМЕРЕННУЮ ТИШИНУ — модель не режет по живому даже когда просит.
        for spec in args.drop:
            try:
                a_s, b_s = spec.split("-", 1)
                a, b = float(a_s), float(b_s)
            except ValueError:
                raise SystemExit(f"не разобрал отрезок {spec!r}, нужен вид 60.8-62.8")
            snapped = []
            for t, prefer_late in ((a, True), (b, False)):
                inside = [(x, y) for x, y in runs if x <= t <= y]
                if inside:
                    x, y = inside[0]
                    snapped.append(min(y - 0.08, max(x + 0.08, t)))
                    continue
                near = sorted(runs, key=lambda r: min(abs(r[0] - t), abs(r[1] - t)))
                placed = None
                for x, y in near[:3]:
                    if min(abs(x - t), abs(y - t)) > 0.5 or (y - x) < 0.18:
                        continue
                    placed = (y - 0.08) if prefer_late else (x + 0.08)
                    break
                if placed is None:
                    raise SystemExit(
                        f"ОТКАЗ: у границы {t:.2f} нет тишины в пределах 500 мс — "
                        f"резать там значит резать по слову")
                snapped.append(placed)
            removals.append(Removal(round(snapped[0], 3), round(snapped[1], 3),
                                    round((snapped[1] - snapped[0]) * 1000), False,
                                    by_model=True))
            print(f"решение модели  {a:.2f}-{b:.2f} -> снаплено в тишину "
                  f"{snapped[0]:.3f}-{snapped[1]:.3f}")
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

        # ПРОВЕРКА ПЕРВАЯ, и она задаёт РАЗНЫЕ вопросы двум видам реза.
        #
        # Эта разница стоила отказа на ровном месте: аудит проверял нутро КАЖДОГО выреза и
        # завалил рез по решению модели с превышением +12,3 дБ. Дефекта не было — вопрос был не
        # тот. В вырезанном дубле речь находится НАМЕРЕННО.
        #
        #   рез тишины  — внутри не должно быть ничего слышимого. Это всё утверждение.
        #   рез модели  — внутри речь по замыслу; проверять надо ОБА СТЫКА, они обязаны
        #                 попадать в тишину, иначе склейка рубит слово.
        seam = max(1, int(0.06 / hop))

        def loudest(i0: int, i1: int) -> float:
            return max(env[max(0, i0):max(1, i1)] or [-120.0])

        worst_silence, worst_seam = -120.0, -120.0
        for r in removals:
            i0, i1 = int(r.start_s / hop), max(int(r.start_s / hop) + 1, int(r.end_s / hop))
            if r.by_model:
                worst_seam = max(worst_seam, loudest(i0 - seam, i0 + seam),
                                 loudest(i1 - seam, i1 + seam))
            else:
                worst_silence = max(worst_silence, loudest(i0, i1))
        ok_silence, ok_seam = worst_silence < floor, worst_seam < floor
        print(f"аудит тишины    {'чисто' if ok_silence else 'ПРОВАЛ'} "
              f"(громчайший сэмпл внутри вырезанного {worst_silence:.1f} дБ, "
              f"{worst_silence - floor:+.1f} к порогу)")
        if any(r.by_model for r in removals):
            print(f"стыки модели    {'чисто' if ok_seam else 'ПРОВАЛ'} "
                  f"(громчайший сэмпл на стыке {worst_seam:.1f} дБ, "
                  f"{worst_seam - floor:+.1f} к порогу)")
        if not ok_silence:
            raise SystemExit("отказ: вырезаемая ТИШИНА содержит звук выше порога — это рез по речи")
        if not ok_seam:
            raise SystemExit("отказ: стык реза модели попадает не в тишину — склейка рубит слово")

        if args.seams and removals:
            sheets = seam_contact_sheet(src, removals, args.seams)
            print(f"швы            {len(sheets)} кадров «до и после» в {args.seams}")

        if args.silence == "mark":
            print("режим mark: только разметка, ничего не собираю")
            for r in sorted(removals, key=lambda x: x.start_s):
                print(f"  {r.start_s:8.3f} - {r.end_s:8.3f}  пауза {r.pause_ms} мс"
                      f"{' (граница предложения)' if r.at_sentence else ''}")
            return

        if args.dry_run or not removals:
            if not removals:
                print("резать нечего: пауз нужной длины нет")
            return

        # ПРОВЕРКА ВТОРАЯ: распознать результат заново — но ПО ЗВУКУ НА ИСХОДНОЙ СКОРОСТИ.
        if key:
            cut_wav = work / "cut.wav"
            render_audio_only(src, keeps, cut_wav)
            after = norm(" ".join(w["word"] for w in transcribe(cut_wav, key)))
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
                raise SystemExit("отказ: после реза речь разошлась с ожидаемой сильнее порога")

        if not (0.5 <= args.speed <= 2.0):
            raise SystemExit("ускорение вне диапазона 0.5-2.0: atempo не примет его одним проходом")
        if args.silence == "speed":
            render_speed_silence(src, removals, duration, dst, args.crf, args.speed,
                                 args.silence_speed)
        else:
            render(src, keeps, dst, args.crf, args.speed)
        got = duration_of(dst)
        if args.silence == "speed":
            kept_silence = sum((r.end_s - r.start_s) / args.silence_speed for r in removals)
            planned = (duration - removed_s + kept_silence) / args.speed
        else:
            planned = (duration - removed_s) / args.speed
        if abs(args.speed - 1.0) > 1e-6:
            print(f"ускорение       {args.speed}x (после реза, высота тона не меняется)")
        print(f"собран          {dst} — {got:.2f} с "
              f"(расхождение с планом {abs(got - planned) * 1000:.0f} мс)")
        print(f"итого           {duration:.2f} с -> {got:.2f} с "
              f"(короче на {100 * (1 - got / duration):.1f}%)")


if __name__ == "__main__":
    main()
