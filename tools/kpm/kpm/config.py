"""Repository layout, the source registry (kernel/sources.toml) and per-line
settings (kernel/lines/<line>/line.toml)."""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def mono_root() -> Path:
    return Path(os.environ.get("KPM_MONO") or Path(__file__).resolve().parents[3])


MONO = mono_root()


@dataclass
class Source:
    name: str
    url: str = ""
    mirror: str = ""          # directory name under $KPM_MIRRORS, if cloned locally
    role: str = "source"      # "base": lines build on its tags; "reference": lookups only
    kind: str = "vendor"      # "vendor": harvested delta; "local": commits we chose with `kpm add`
    band: int | None = None
    parent: str = ""
    tags: str = ""            # tag glob to track, e.g. "Ubuntu-7.3.0-*"
    branch: str = ""          # or a branch to track
    delta_base: str = ""      # "ref:<name>" or "grep:<regex on commit subject>"
    noise: list[str] = field(default_factory=list)   # subject regexes
    packaging_dir: str = ""   # e.g. "debian.aws": where this source keeps its config
    upstream: str = ""        # mainline release this tree is built on, if not the line's (e.g. "v7.0")

    def remote_location(self) -> str:
        mirrors = os.environ.get("KPM_MIRRORS", "")
        if mirrors and self.mirror:
            for p in (Path(mirrors) / self.mirror, Path(mirrors) / f"{self.mirror}.git"):
                if p.exists():
                    return str(p)
        return self.url


@dataclass
class Config:
    mono: Path
    sources: dict[str, Source]
    line: str
    line_dir: Path
    repo: Path                # unified bare repo
    cache: Path
    kind: str                 # "stable": base is an upstream stable tag; "ubuntu": an Ubuntu release tag
    base_source_name: str
    base_tag: str
    series_root: str          # first tag of the base's series, e.g. "v7.3"; "" if unknown
    line_sources: list[str]   # sources whose ledgers belong to this line, any kind
    upstream: str = ""        # mainline release the base is built on, e.g. "v7.0" (Ubuntu lines)
    base_tags: str = ""       # tag glob to fetch from the base source for this line only

    @property
    def base_source(self) -> Source:
        return self.sources[self.base_source_name]

    @property
    def base_ref(self) -> str:
        return f"{self.base_source_name}/{self.base_tag}"

    def ref(self, tag: str) -> str:
        return f"{self.base_source_name}/{tag}"

    def ledger_path(self, source: str) -> Path:
        return self.line_dir / "ledger" / f"{source}.jsonl"

    def vendor_sources(self) -> list[str]:
        return [n for n in self.line_sources if self.sources[n].kind == "vendor"]

    def config_sources(self) -> list[str]:
        """Vendor sources whose config and modprobe rules this line merges.

        A source has them in its packaging_dir, relative to the Ubuntu kernel
        it is built on. They only mean something to a line on that same
        kernel: a source built on 7.0 (`upstream = "v7.0"`) is merged by a
        line on Ubuntu's 7.0, and not by a line on stable 7.3."""
        mine = self.upstream if self.kind != "stable" else ""
        return [n for n in self.vendor_sources()
                if self.sources[n].packaging_dir and (self.sources[n].upstream or "") == mine]

    def local_source(self) -> str | None:
        return next((n for n in self.line_sources if self.sources[n].kind == "local"), None)

    def set_base(self, tag: str) -> None:
        """Move the line to a new base tag, keeping the file's comments."""
        path = self.line_dir / "line.toml"
        text = path.read_text(encoding="utf-8")
        new, n = re.subn(r'(?m)^base\s*=\s*"[^"]*"', f'base = "{tag}"', text, count=1)
        if n != 1:
            raise SystemExit(f"kpm: no base = \"...\" line in {path}")
        from .ledger import atomic_write
        atomic_write(path, new)
        self.base_tag = tag


def lines(mono: Path | None = None) -> list[str]:
    d = (mono or mono_root()) / "kernel" / "lines"
    return sorted(p.name for p in d.iterdir() if (p / "line.toml").exists()) if d.exists() else []


def work_root() -> Path:
    return Path(os.environ.get("WORK_ROOT") or os.environ.get("KERNOPS_WORK") or Path.home() / "kernops")


def load(mono: Path | None = None, line: str | None = None) -> Config:
    mono = mono or mono_root()
    raw = tomllib.loads((mono / "kernel" / "sources.toml").read_text(encoding="utf-8"))
    sources = {n: Source(name=n, **v) for n, v in raw.items()}
    known = lines(mono)
    line = line or os.environ.get("KPM_LINE") or (known[0] if len(known) == 1 else None)
    if line is None:
        raise SystemExit(f"kpm: several lines exist ({', '.join(known)}); pass --line or set KPM_LINE")
    if line not in known:
        raise SystemExit(f"kpm: unknown line '{line}' (known: {', '.join(known)})")
    line_dir = mono / "kernel" / "lines" / line
    lt = tomllib.loads((line_dir / "line.toml").read_text(encoding="utf-8"))
    for n in [lt["base_source"], *lt.get("sources", [])]:
        if n not in sources:
            raise SystemExit(f"kpm: {line}/line.toml names unknown source '{n}'")
    return Config(
        mono=mono, sources=sources, line=line, line_dir=line_dir,
        repo=Path(os.environ.get("KPM_REPO", work_root() / "kernel.git")),
        cache=Path(os.environ.get("KPM_CACHE", Path.home() / ".cache" / "kpm")),
        kind=lt.get("kind", "stable"), base_source_name=lt["base_source"], base_tag=lt["base"],
        series_root=lt.get("series_root", ""), line_sources=list(lt.get("sources", [])),
        upstream=lt.get("upstream", ""), base_tags=lt.get("base_tags", ""),
    )
