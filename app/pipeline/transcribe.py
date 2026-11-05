"""ステップ3: 文字起こし。単語ごとの時刻を残す。

つないだ後の動画（タイトル・エンドカードを付ける前）を文字起こしするので、
時刻はそのまま編集後の動画の時間になる。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from app.errors import UserError
from app.platform import PlatformProfile


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Phrase:
    start: float
    end: float
    text: str


# (動画, 型番などのヒント) -> 単語のリスト。テストでは差し替える
Transcriber = Callable[[Path, str], list[Word]]

MODEL_MISSING = ("文字起こしの準備ができていません。",
                 "フォルダで「uv sync」を実行してから、もう一度お試しください。")
DOWNLOAD_FAILED = ("文字起こし用のデータをダウンロードできませんでした。",
                   "インターネットにつながっているか確認して、もう一度お試しください（初回のみ数分かかります）。")


def _faster_whisper(model_name: str) -> Transcriber:
    def run(path: Path, hint: str) -> list[Word]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise UserError(*MODEL_MISSING) from exc
        try:
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
        except Exception as exc:
            raise UserError(*DOWNLOAD_FAILED) from exc
        segments, _info = model.transcribe(
            str(path), language="ja", word_timestamps=True, vad_filter=True,
            initial_prompt=hint or None, condition_on_previous_text=False,
        )
        return [Word(float(w.start), float(w.end), w.word) for s in segments for w in (s.words or [])]

    return run


def _mlx_whisper(model_name: str) -> Transcriber:
    def run(path: Path, hint: str) -> list[Word]:
        try:
            import mlx_whisper
        except ImportError as exc:
            raise UserError(*MODEL_MISSING) from exc
        try:
            result = mlx_whisper.transcribe(
                str(path), path_or_hf_repo=model_name, language="ja", word_timestamps=True,
                initial_prompt=hint or None, condition_on_previous_text=False,
            )
        except (OSError, ConnectionError) as exc:
            raise UserError(*DOWNLOAD_FAILED) from exc
        return [Word(float(w["start"]), float(w["end"]), w["word"])
                for s in result.get("segments", []) for w in s.get("words", [])]

    return run


def default_transcriber(profile: PlatformProfile) -> Transcriber:
    if profile.whisper_backend == "mlx-whisper":
        return _mlx_whisper(profile.whisper_model)
    return _faster_whisper(profile.whisper_model)


def hint_text(terms: list[str]) -> str:
    """型番や商品名を先に伝えておくと、聞き取りの精度が上がる。"""
    return "、".join(dict.fromkeys(t for t in terms if t)) + "。" if terms else ""


BREAK_CHARS = "。！？!?、"
MAX_PHRASE_CHARS = 28
GAP = 0.5


def to_phrases(words: list[Word]) -> list[Phrase]:
    """単語を、Claude に渡しやすい短いまとまりにする（間が空いたら・句読点・長さで区切る）。"""
    phrases: list[Phrase] = []
    cur: list[Word] = []

    def flush() -> None:
        if cur:
            text = "".join(w.text for w in cur).strip()
            if text:
                phrases.append(Phrase(round(cur[0].start, 2), round(cur[-1].end, 2), text))
            cur.clear()

    for w in words:
        if cur and (w.start - cur[-1].end > GAP or len("".join(x.text for x in cur)) >= MAX_PHRASE_CHARS):
            flush()
        cur.append(w)
        if w.text.strip()[-1:] in BREAK_CHARS:
            flush()
    flush()
    return phrases


def save_words(words: list[Word], path: Path) -> None:
    path.write_text(json.dumps([asdict(w) for w in words], ensure_ascii=False, indent=1), encoding="utf-8")


def load_words(path: Path) -> list[Word]:
    return [Word(**w) for w in json.loads(path.read_text(encoding="utf-8"))]


def transcribe(profile: PlatformProfile, video: Path, hint: str = "",
               transcriber: Transcriber | None = None) -> list[Word]:
    words = (transcriber or default_transcriber(profile))(Path(video), hint)
    return [w for w in words if w.text.strip()]
