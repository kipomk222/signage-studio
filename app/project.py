"""プロジェクト（動画1本分）の保存と読み込み。

1動画＝1フォルダ projects/<日付>_<型番>/ に、project.json・素材/・work/・output/ を置く。
値を確定するたびに save_project() を呼ぶ。保存は一時ファイルに書いてから置き換えるので、
途中で落ちても元の project.json は壊れない。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.errors import UserError
from app.pipeline.colors import normalize_color

SCHEMA_VERSION = 1
PROJECT_FILE = "project.json"
SUBDIRS = ("素材", "work", "output")
DEFAULT_TARGET_SECONDS = 150  # 標準は2〜3分


class ProjectError(UserError):
    """プロジェクトの保存・読み込みで起きた問題。"""


# --- 段階 ---------------------------------------------------------------

class Stage(str, Enum):
    PLAN = "plan"
    MATERIAL = "material"
    REVIEW = "review"
    EXPORT = "export"
    DONE = "done"

    @property
    def label(self) -> str:
        return STAGE_LABELS[self]

    @property
    def index(self) -> int:
        return STAGE_ORDER.index(self)


STAGE_ORDER = [Stage.PLAN, Stage.MATERIAL, Stage.REVIEW, Stage.EXPORT, Stage.DONE]
STAGE_LABELS = {
    Stage.PLAN: "企画",
    Stage.MATERIAL: "素材",
    Stage.REVIEW: "確認",
    Stage.EXPORT: "書き出し",
    Stage.DONE: "完了",
}


# --- project.json の中身 -------------------------------------------------

class Product(BaseModel):
    model_number: str
    name: str = ""
    maker: str = ""
    category: str = ""
    specs: dict[str, str] = Field(default_factory=dict)
    features: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    confirmed: bool = False


class Price(BaseModel):
    mode: Literal["hidden", "open", "amount"] = "hidden"
    amount: int | None = None


class Scene(BaseModel):
    id: str
    title: str
    script: str = ""
    shots: list[str] = Field(default_factory=list)
    seconds: float = 0


class Plan(BaseModel):
    target_seconds: int = DEFAULT_TARGET_SECONDS
    scenes: list[Scene] = Field(default_factory=list)
    price: Price = Field(default_factory=Price)
    extra_text: str = ""
    extra_files: list[str] = Field(default_factory=list)
    confirmed: bool = False


class Clip(BaseModel):
    file: str  # 素材/ からの相対パス
    scene_id: str | None = None
    scene_manual: bool = False   # 手で場面を選んだ（自動で変えない）
    scene_guessed: bool = False  # 話していない等で、直前のクリップから推測した
    excluded: bool = False
    exclude_reason: str = ""
    duration: float = 0
    vertical: bool = False
    silent: bool = False  # 話し声なし（差し込み用の映像）
    transcript: str = ""
    cut_ranges: list[tuple[float, float]] = Field(default_factory=list)
    kept_seconds: float = 0


class EditPiece(BaseModel):
    """完成動画に使う部分（クリップのどこからどこまで）。上から順につなぐ。"""

    clip: str
    start: float
    end: float
    scene_id: str | None = None
    reason: str = ""
    enabled: bool = True


class EditInsert(BaseModel):
    """差し込み: 説明中の映像に、別のクリップの映像を重ねる（声は説明のまま）。"""

    clip: str
    start: float          # 差し込むクリップの、どこから
    end: float            # どこまで
    piece: int            # どの部分（pieces の何番目）に重ねるか
    offset: float = 0.0   # その部分の始まりから何秒後に重ねるか（元の時間で）
    reason: str = ""
    enabled: bool = True


class EditDrop(BaseModel):
    """切った部分（言い直し・失敗など）。確認画面で「戻す」ことができる。"""

    clip: str
    start: float
    end: float
    reason: str = ""


class EditPlan(BaseModel):
    source: Literal["ai", "auto"] = "auto"   # Claude Code が決めた／機械的に決めた
    summary: str = ""
    pieces: list[EditPiece] = Field(default_factory=list)
    inserts: list[EditInsert] = Field(default_factory=list)
    dropped: list[EditDrop] = Field(default_factory=list)


class Telop(BaseModel):
    start: float
    end: float
    text: str  # **ことば** で囲んだ部分は強調色になる
    font: str = ""
    manual: bool = False  # 手で直したテロップ。再作成しても消さない
    # このテロップだけ色を変えるとき（空なら全体の設定を使う）
    text_color: str = ""
    outline_color: str = ""
    emphasis_color: str = ""

    _colors = field_validator("text_color", "outline_color", "emphasis_color")(
        classmethod(lambda cls, v: normalize_color(v)))


class TelopStyle(BaseModel):
    """テロップ全体の色。カラーコード（#RRGGBB）で指定する。"""

    text_color: str = "#FFFFFF"       # 文字
    outline_color: str = "#000000"    # 縁取り
    emphasis_color: str = "#FFD400"   # **強調** した言葉
    band_color: str = ""              # 文字の後ろの帯（空なら帯なし）
    band_opacity: float = Field(0.6, ge=0, le=1)

    _colors = field_validator("text_color", "outline_color", "emphasis_color", "band_color")(
        classmethod(lambda cls, v: normalize_color(v)))

    @field_validator("text_color", "outline_color", "emphasis_color")
    @classmethod
    def _required(cls, v: str) -> str:
        if not v:
            raise UserError("文字・縁取り・強調の色は空にできません。", "「#FFFFFF」のようにカラーコードを入れてください。")
        return v


class Settings(BaseModel):
    bgm_file: str = ""
    bgm_level: Literal["small", "medium", "large"] = "medium"
    silence_strength: Literal["weak", "normal", "strong"] = "normal"
    fit: Literal["pad", "crop"] = "pad"  # 縦撮りを 黒帯／切り抜き
    ai_edit: bool = True                  # 編集の判断を Claude Code にまかせる
    outputs: list[Literal["store", "youtube"]] = Field(default_factory=lambda: ["store"])
    telop_style: TelopStyle = Field(default_factory=TelopStyle)


class HistoryEntry(BaseModel):
    stage: Stage
    completed_at: datetime


class Project(BaseModel):
    schema_version: int = SCHEMA_VERSION
    id: str  # フォルダ名
    created_at: datetime
    updated_at: datetime
    stage: Stage = Stage.PLAN
    stale: list[Stage] = Field(default_factory=list)  # 「再作成が必要」な段階
    product: Product
    title: str = ""  # 冒頭タイトル
    youtube_title: str = ""
    youtube_description: str = ""
    plan: Plan = Field(default_factory=Plan)
    clips: list[Clip] = Field(default_factory=list)
    edit: EditPlan | None = None
    telops: list[Telop] = Field(default_factory=list)
    settings: Settings = Field(default_factory=Settings)
    history: list[HistoryEntry] = Field(default_factory=list)

    @property
    def model_number(self) -> str:
        return self.product.model_number

    def complete_stage(self, stage: Stage, now: datetime | None = None) -> None:
        """段階を終えたことを記録し、次の段階へ進める。"""
        self.history.append(HistoryEntry(stage=stage, completed_at=now or datetime.now()))
        if stage in self.stale:
            self.stale.remove(stage)
        next_index = min(stage.index + 1, len(STAGE_ORDER) - 1)
        if STAGE_ORDER[next_index].index > self.stage.index:
            self.stage = STAGE_ORDER[next_index]

    def mark_changed(self, stage: Stage) -> list[Stage]:
        """前の段階を直したとき、それより後で既に進んでいた段階を「再作成が必要」にする。

        手で直したテロップ（manual=True）はここでは消さない。戻り値は新たに印を付けた段階。
        """
        marked: list[Stage] = []
        for later in STAGE_ORDER[stage.index + 1:]:
            if later is Stage.DONE:
                continue
            if later.index <= self.stage.index and later not in self.stale:
                self.stale.append(later)
                marked.append(later)
        self.stale.sort(key=lambda s: s.index)
        return marked


class ProjectSummary(BaseModel):
    """ホーム画面の一覧の1行。"""

    id: str
    path: Path
    model_number: str = ""
    product_name: str = ""
    updated_at: datetime | None = None
    stage_label: str = ""
    broken: bool = False  # 読み込めなかったプロジェクト


# --- フォルダ名 ----------------------------------------------------------

_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def safe_folder_part(text: str) -> str:
    """Mac / Windows の両方でフォルダ名に使える形にする。"""
    cleaned = _UNSAFE.sub("-", text.strip()).strip("-.")
    return cleaned or "noname"


# --- 作成・保存・読み込み -------------------------------------------------

def create_project(
    projects_root: Path,
    model_number: str,
    today: date | None = None,
    now: datetime | None = None,
) -> tuple[Project, Path]:
    """新しいプロジェクトフォルダを作って保存し、(プロジェクト, フォルダ) を返す。"""
    model_number = model_number.strip()
    if not model_number:
        raise ProjectError("型番が入力されていません。", "型番を入れてから、もう一度押してください。")

    projects_root = Path(projects_root)
    projects_root.mkdir(parents=True, exist_ok=True)
    base = f"{(today or date.today()).isoformat()}_{safe_folder_part(model_number)}"
    folder = projects_root / base
    n = 2
    while folder.exists():
        folder = projects_root / f"{base}_{n}"
        n += 1
    folder.mkdir()
    for sub in SUBDIRS:
        (folder / sub).mkdir()

    stamp = now or datetime.now()
    project = Project(
        id=folder.name,
        created_at=stamp,
        updated_at=stamp,
        product=Product(model_number=model_number),
    )
    save_project(folder, project, now=stamp)
    return project, folder


def save_project(folder: Path, project: Project, now: datetime | None = None) -> None:
    """project.json を安全に保存する（一時ファイルに書いてから置き換え）。"""
    folder = Path(folder)
    project.updated_at = now or datetime.now()
    data = project.model_dump_json(indent=2)

    fd, tmp_name = tempfile.mkstemp(prefix=".project-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, folder / PROJECT_FILE)
    except BaseException as exc:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        if isinstance(exc, OSError):
            raise ProjectError(
                "作業内容を保存できませんでした。",
                "保存先の空き容量を確認してから、もう一度お試しください。",
            ) from exc
        raise


def load_project(folder: Path) -> Project:
    path = Path(folder) / PROJECT_FILE
    if not path.exists():
        raise ProjectError(
            "このフォルダには作業内容が見つかりません。",
            "ホーム画面の一覧から選び直してください。",
        )
    try:
        return Project.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProjectError(
            "作業内容のファイルが読み込めませんでした。",
            "新しく企画し直すか、ファイルを元に戻してください。",
        ) from exc


def list_projects(projects_root: Path) -> list[ProjectSummary]:
    """ホーム画面用の一覧。更新日の新しい順。読み込めないものは broken=True で末尾に並べる。"""
    root = Path(projects_root)
    if not root.exists():
        return []
    ok: list[ProjectSummary] = []
    broken: list[ProjectSummary] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (folder / PROJECT_FILE).exists():
            continue
        try:
            project = load_project(folder)
        except ProjectError:
            broken.append(ProjectSummary(id=folder.name, path=folder, broken=True))
            continue
        ok.append(ProjectSummary(
            id=project.id,
            path=folder,
            model_number=project.model_number,
            product_name=project.product.name,
            updated_at=project.updated_at,
            stage_label=project.stage.label,
        ))
    ok.sort(key=lambda s: s.updated_at, reverse=True)
    return ok + broken
