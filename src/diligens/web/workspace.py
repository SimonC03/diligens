"""The local workspace: analyses and screenings saved as files on this computer only.

One folder per analysis (`analyser/<id>/`) holds the uploaded SIE file, the computed result,
the advisor's choices and the exports; one folder per screening (`screeningar/<id>/`) holds
its result and long list. Ids are validated, so no request can reach outside the workspace.
"""

import datetime as dt
import json
import re
import secrets
import shutil
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from diligens.analysis.bridge import with_selection
from diligens.models import AnalysisResult, ScreeningResult

ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
CODE_TABLE = re.compile(r"^[a-z]+$")


class AnalysisMeta(BaseModel):
    id: str
    created: dt.datetime
    source_name: str = Field(description="file name as uploaded")
    company_name: str
    org_nr: str
    period: str = Field(description="first and last month of the bridge period")
    basis: str = Field(description="LTM or YTD")
    use_llm: bool
    model: str | None = None
    related_parties: list[str] = Field(default_factory=list)
    reconciled: bool
    selection: list[bool] | None = Field(
        default=None, description="the advisor's Inkludera choices; None means as proposed"
    )
    memo_current: bool = Field(
        default=True, description="False after the selection changed, until the memo is rewritten"
    )


class ScreeningMeta(BaseModel):
    id: str
    created: dt.datetime
    title: str = Field(description="Swedish summary of the criteria")
    candidates: int
    reports: int


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._analyses = root / "analyser"
        self._screenings = root / "screeningar"
        self._tables = root / "kodtabeller"
        for folder in (self._analyses, self._screenings, self._tables):
            folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, AnalysisResult] = {}

    @staticmethod
    def new_id(now: dt.datetime | None = None) -> str:
        now = now or dt.datetime.now()
        return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"

    @staticmethod
    def valid(item_id: str) -> bool:
        return bool(ID_PATTERN.fullmatch(item_id))

    def _folder(self, base: Path, item_id: str) -> Path:
        if not self.valid(item_id):
            raise KeyError(item_id)
        return base / item_id

    # ------------------------------------------------------------------ analyses

    def analysis_dir(self, item_id: str) -> Path:
        return self._folder(self._analyses, item_id)

    def upload_path(self, item_id: str) -> Path:
        folder = self.analysis_dir(item_id)
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "underlag.se"

    def save_analysis(self, meta: AnalysisMeta, result: AnalysisResult) -> None:
        folder = self.analysis_dir(meta.id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "resultat.json").write_text(result.model_dump_json(), encoding="utf-8")
        (folder / "memo.md").write_text(result.memo, encoding="utf-8")
        self.save_meta(meta)
        with self._lock:
            self._cache[meta.id] = result

    def save_meta(self, meta: AnalysisMeta) -> None:
        path = self.analysis_dir(meta.id) / "meta.json"
        path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")

    def analysis_meta(self, item_id: str) -> AnalysisMeta:
        path = self.analysis_dir(item_id) / "meta.json"
        if not path.is_file():
            raise KeyError(item_id)
        return AnalysisMeta.model_validate_json(path.read_text(encoding="utf-8"))

    def proposed(self, item_id: str) -> AnalysisResult:
        """The result as the analysis proposed it, before the advisor's choices."""
        with self._lock:
            result = self._cache.get(item_id)
        if result is None:
            path = self.analysis_dir(item_id) / "resultat.json"
            if not path.is_file():
                raise KeyError(item_id)
            result = AnalysisResult.model_validate_json(path.read_text(encoding="utf-8"))
            with self._lock:
                self._cache[item_id] = result
        return result

    def analysis(self, item_id: str) -> tuple[AnalysisMeta, AnalysisResult]:
        """The saved result with the advisor's bridge selection and current memo applied."""
        meta = self.analysis_meta(item_id)
        result = self.proposed(item_id)
        memo = (self.analysis_dir(item_id) / "memo.md").read_text(encoding="utf-8")
        update: dict[str, object] = {"memo": memo}
        if meta.selection is not None:
            update["bridge"] = with_selection(result.bridge, meta.selection)
        return meta, result.model_copy(update=update)

    def save_memo(self, item_id: str, memo: str) -> None:
        (self.analysis_dir(item_id) / "memo.md").write_text(memo, encoding="utf-8")

    def analyses(self) -> list[AnalysisMeta]:
        metas = []
        for path in self._analyses.glob("*/meta.json"):
            try:
                metas.append(AnalysisMeta.model_validate_json(path.read_text(encoding="utf-8")))
            except ValueError:
                continue  # a half written folder from an interrupted run
        return sorted(metas, key=lambda m: m.created, reverse=True)

    def delete_analysis(self, item_id: str) -> None:
        folder = self.analysis_dir(item_id)
        with self._lock:
            self._cache.pop(item_id, None)
        if folder.is_dir():
            shutil.rmtree(folder)

    # ---------------------------------------------------------------- screenings

    def screening_dir(self, item_id: str) -> Path:
        return self._folder(self._screenings, item_id)

    def save_screening(self, meta: ScreeningMeta, result: ScreeningResult) -> None:
        folder = self.screening_dir(meta.id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "resultat.json").write_text(result.model_dump_json(), encoding="utf-8")
        (folder / "meta.json").write_text(meta.model_dump_json(indent=2), encoding="utf-8")

    def screening(self, item_id: str) -> tuple[ScreeningMeta, ScreeningResult]:
        folder = self.screening_dir(item_id)
        if not (folder / "meta.json").is_file():
            raise KeyError(item_id)
        meta = ScreeningMeta.model_validate_json((folder / "meta.json").read_text(encoding="utf-8"))
        result = ScreeningResult.model_validate_json(
            (folder / "resultat.json").read_text(encoding="utf-8")
        )
        return meta, result

    def screenings(self) -> list[ScreeningMeta]:
        metas = []
        for path in self._screenings.glob("*/meta.json"):
            try:
                metas.append(ScreeningMeta.model_validate_json(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return sorted(metas, key=lambda m: m.created, reverse=True)

    def delete_screening(self, item_id: str) -> None:
        folder = self.screening_dir(item_id)
        if folder.is_dir():
            shutil.rmtree(folder)

    # --------------------------------------------------------------- code tables

    def code_table(self, name: str) -> dict[str, str] | None:
        """A cached SCB code table, so forms work offline after the first fetch."""
        if not CODE_TABLE.fullmatch(name):
            raise KeyError(name)
        path = self._tables / f"{name}.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}

    def save_code_table(self, name: str, table: dict[str, str]) -> None:
        if not CODE_TABLE.fullmatch(name):
            raise KeyError(name)
        path = self._tables / f"{name}.json"
        path.write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
