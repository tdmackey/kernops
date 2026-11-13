"""Repository layout and the source registry (kernel/sources.toml)."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MONO = Path(__file__).resolve().parents[3]


@dataclass
class Source:
    name: str
    url: str = ""
    mirror: str = ""          # directory name under $KPM_MIRRORS, if cloned locally
    role: str = "source"      # "base" or "source"
    band: int | None = None
    parent: str = ""
    tags: str = ""            # tag glob to track, e.g. "Ubuntu-7.3.0-*"
    branch: str = ""          # or a branch to track
    delta_base: str = ""      # "ref:<name>" or "grep:<regex on commit subject>"
    noise: list[str] = field(default_factory=list)   # subject regexes
    packaging_dir: str = ""   # e.g. "debian.aws": where this source keeps its config

    def remote_location(self) -> str:
        mirrors = os.environ.get("KPM_MIRRORS", "")
        if mirrors and self.mirror:
            p = Path(mirrors) / self.mirror
            if p.exists():
                return str(p)
        return self.url


@dataclass
class Config:
    sources: dict[str, Source]
    line: str
    line_dir: Path
    repo: Path                # unified bare repo
    cache: Path

    @property
    def base_source(self) -> Source:
        return next(s for s in self.sources.values() if s.role == "base")

    @property
    def base_tag(self) -> str:
        """Base as written in lines/<line>/base, e.g. 'v7.3.4'."""
        return (self.line_dir / "base").read_text().strip()

    @property
    def base_ref(self) -> str:
        return f"{self.base_source.name}/{self.base_tag}"

    def ledger_path(self, source: str) -> Path:
        return self.line_dir / "ledger" / f"{source}.jsonl"


def load(mono: Path = MONO, line: str | None = None) -> Config:
    raw = tomllib.loads((mono / "kernel" / "sources.toml").read_text(encoding="utf-8"))
    sources = {n: Source(name=n, **v) for n, v in raw.items()}
    lines = mono / "kernel" / "lines"
    line = line or os.environ.get("KPM_LINE") or sorted(p.name for p in lines.iterdir() if p.is_dir())[0]
    home = Path.home()
    return Config(
        sources=sources,
        line=line,
        line_dir=lines / line,
        repo=Path(os.environ.get("KPM_REPO", home / "kernops" / "kernel.git")),
        cache=Path(os.environ.get("KPM_CACHE", home / ".cache" / "kpm")),
    )
