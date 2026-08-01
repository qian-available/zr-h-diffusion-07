#!/usr/bin/env python3
"""Build auditable TSV and PNG assets for the final Zr96H CI-NEB report.

The downloaded result archives remain immutable and are only extracted into a
temporary staging tree.  The existing ``analyze_neb.py`` remains the authority
for the primary barrier and stage-lineage checks; this script adds report-level
geometry, convergence, provenance, and visualization detail.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import types
from typing import Iterable, Sequence

import numpy as np

from neb_common import (
    NEB_FORCE_PATTERN,
    Structure,
    atomic_write_tsv,
    h_neighbors,
    minimum_image,
    minimum_pair_distance,
    parse_key_values,
    parse_poscar,
    require_file,
    require_same_topology,
)


IMAGE_NAMES = tuple(f"{index:02d}" for index in range(6))
INTERMEDIATE_NAMES = IMAGE_NAMES[1:-1]
FORCE_LIMIT_EV_A = 0.03
PRE_FORCE_LIMIT_EV_A = 0.10
COORDINATION_CUTOFF_A = 2.5
MIN_EXPECTED_PAIR_A = 1.932
EXPECTED_ENDPOINT_DELTA_EV = 0.062904

TT_FINAL_ROOT = PurePosixPath("03_neb/01_tt_c/ci_01")
TO_FINAL_ROOT = PurePosixPath("03_neb/02_to/ci_03")
TO_CI01_ROOT = PurePosixPath("03_neb/02_to/ci_01")
TO_CI02_ROOT = PurePosixPath("03_neb/02_to/ci_02")

SENSITIVE_BASENAME = re.compile(
    r"^(?:POTCAR|WAVECAR|CHGCAR)(?:$|[._-].*)", re.IGNORECASE
)
HDF5_SUFFIXES = {".h5", ".hdf5"}
FATAL_LOG_PATTERN = re.compile(
    r"segmentation|segfault|out[ -]of[ -]memory|\boom\b|killed|fatal|mpi_abort",
    re.IGNORECASE,
)
EDIFF_MARKER = "aborting loop because EDIFF is reached"

EXPECTED_RESULTS = {
    "TT_c": {
        "barrier_forward": 0.12919004,
        "barrier_reverse": 0.12919004,
        "final_force": 0.029753,
        "saddle_force": 0.029753,
        "saddle_image": "03",
        "pre_iterations": 29,
        "ci_iterations": 126,
        "path_length": 1.24145558,
    },
    "TO": {
        "barrier_forward": 0.42502420,
        "barrier_reverse": 0.36212020,
        "final_force": 0.029600,
        "saddle_force": 0.023928,
        "saddle_image": "02",
        "pre_iterations": 37,
        "ci_iterations": 136,
        "path_length": 1.98999879,
    },
}

LITERATURE_PATHS = ("TT", "TO", "OT", "OO")


@dataclass(frozen=True)
class LiteratureDataset:
    source: str
    year: int
    method_or_provenance: str
    doi: str
    direct_or_secondary: str
    barriers: tuple[tuple[str, float], ...]


LITERATURE_DATASETS = (
    LiteratureDataset(
        source="Andolina",
        year=2022,
        method_or_provenance="DFT-NEB; direct local Andolina 2022 Table 1",
        doi="10.1016/j.commatsci.2022.111384",
        direct_or_secondary="direct",
        barriers=(("TT", 0.091), ("TO", 0.410), ("OT", 0.389), ("OO", 0.465)),
    ),
    LiteratureDataset(
        source="Zhang",
        year=2017,
        method_or_provenance="DFT-NEB; direct local Zhang 2017 Table 1",
        doi="10.1038/srep41033",
        direct_or_secondary="direct",
        barriers=(("TT", 0.129), ("TO", 0.406), ("OT", 0.346), ("OO", 0.398)),
    ),
    LiteratureDataset(
        source="Domain",
        year=2002,
        method_or_provenance="DFT values reproduced in Andolina 2022 Table 1",
        doi="10.1016/S1359-6454(02)00173-8",
        direct_or_secondary="secondary",
        barriers=(("TT", 0.120), ("TO", 0.410), ("OT", 0.350), ("OO", 0.410)),
    ),
    LiteratureDataset(
        source="Wimmer",
        year=2020,
        method_or_provenance="Values attributed to Wimmer in Andolina 2022 Table 1",
        doi="10.1016/j.jnucmat.2020.152055",
        direct_or_secondary="secondary",
        barriers=(("TT", 0.130), ("TO", 0.420), ("OT", 0.350), ("OO", 0.440)),
    ),
)


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    sha256: str
    members: tuple[str, ...]
    byte_size: int


@dataclass
class StageSeries:
    path_name: str
    stage_name: str
    stage_kind: str
    job_id: str
    image_forces: dict[str, list[tuple[float, float]]]
    image_energies: dict[str, list[float]]

    @property
    def iterations(self) -> int:
        lengths = {len(values) for values in self.image_forces.values()}
        if len(lengths) != 1:
            raise ValueError(
                f"force histories differ in {self.path_name}/{self.stage_name}: {sorted(lengths)}"
            )
        return lengths.pop()

    def aggregate_force(self) -> list[tuple[float, float, str]]:
        rows: list[tuple[float, float, str]] = []
        for values in zip(*(self.image_forces[name] for name in INTERMEDIATE_NAMES)):
            maxima = [pair[0] for pair in values]
            maximum = max(maxima)
            controller = INTERMEDIATE_NAMES[maxima.index(maximum)]
            rms = math.sqrt(sum(pair[1] ** 2 for pair in values) / len(values))
            rows.append((maximum, rms, controller))
        return rows

    def barrier_history(
        self, start_e0: float, end_e0: float
    ) -> list[tuple[float, float, str]]:
        lengths = {len(values) for values in self.image_energies.values()}
        if len(lengths) != 1 or lengths != {self.iterations}:
            raise ValueError(
                f"energy histories differ in {self.path_name}/{self.stage_name}: {sorted(lengths)}"
            )
        rows: list[tuple[float, float, str]] = []
        for values in zip(*(self.image_energies[name] for name in INTERMEDIATE_NAMES)):
            energies = [start_e0, *values, end_e0]
            peak_index = int(np.argmax(energies))
            maximum = energies[peak_index]
            rows.append((maximum - start_e0, maximum - end_e0, f"{peak_index:02d}"))
        return rows


@dataclass
class PathResult:
    name: str
    final_directory: Path
    start_e0: float
    end_e0: float
    structures: list[Structure]
    energies: list[float]
    reaction_coordinates: list[float]
    final_image_forces: dict[str, tuple[float, float]]
    stages: list[StageSeries]
    geometry_rows: list[dict[str, object]]
    neighbor_rows: list[dict[str, object]]
    direct_h_distance: float
    h_path_length: float
    path_tortuosity: float
    max_lateral_deviation: float

    @property
    def saddle_index(self) -> int:
        return int(np.argmax(self.energies))

    @property
    def saddle_image(self) -> str:
        return f"{self.saddle_index:02d}"

    @property
    def barrier_forward(self) -> float:
        return max(self.energies) - self.start_e0

    @property
    def barrier_reverse(self) -> float:
        return max(self.energies) - self.end_e0

    @property
    def final_stage(self) -> StageSeries:
        return self.stages[-1]

    @property
    def final_force(self) -> float:
        return self.final_stage.aggregate_force()[-1][0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tt-archive", type=Path, required=True)
    parser.add_argument("--to-archive", type=Path, required=True)
    parser.add_argument("--to-history-archive", type=Path, required=True)
    parser.add_argument("--tt-pre-result", type=Path, required=True)
    parser.add_argument("--to-pre-result", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with require_file(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def companion_checksum_path(archive: Path) -> Path:
    return Path(f"{archive}.sha256")


def verify_companion_checksum(archive: Path) -> str:
    archive = require_file(archive.resolve())
    sidecar = require_file(companion_checksum_path(archive))
    lines = [line.strip() for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(f"checksum sidecar must contain exactly one non-empty line: {sidecar}")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+[*]?(.+)", lines[0])
    if match is None:
        raise ValueError(f"malformed SHA-256 sidecar: {sidecar}")
    expected, recorded_name = match.groups()
    if Path(recorded_name).name != archive.name:
        raise ValueError(
            f"checksum sidecar names {recorded_name!r}, expected {archive.name!r}: {sidecar}"
        )
    actual = sha256_file(archive)
    if actual.lower() != expected.lower():
        raise ValueError(f"archive SHA-256 mismatch: {archive}")
    return actual.lower()


def validate_member_name(name: str) -> PurePosixPath:
    if "\\" in name:
        raise ValueError(f"archive member contains a backslash: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive member path: {name!r}")
    basename = path.name
    if SENSITIVE_BASENAME.match(basename) or Path(basename).suffix.lower() in HDF5_SUFFIXES:
        raise ValueError(f"sensitive file is forbidden in result archives: {name!r}")
    return path


def validate_archive(
    archive: Path,
    *,
    expected_prefixes: Sequence[PurePosixPath],
    required_members: Iterable[PurePosixPath],
) -> ArchiveInfo:
    archive = archive.resolve()
    digest = verify_companion_checksum(archive)
    names: list[str] = []
    seen: set[PurePosixPath] = set()
    with tarfile.open(archive, mode="r:gz") as handle:
        for member in handle.getmembers():
            path = validate_member_name(member.name.rstrip("/"))
            if not any(path == prefix or prefix in path.parents for prefix in expected_prefixes):
                raise ValueError(f"archive member is outside expected result roots: {member.name!r}")
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise ValueError(f"non-regular archive member is forbidden: {member.name!r}")
            if not member.isfile() and not member.isdir():
                raise ValueError(f"unsupported archive member type: {member.name!r}")
            if path in seen and member.isfile():
                raise ValueError(f"duplicate archive member: {member.name!r}")
            seen.add(path)
            names.append(member.name)
    missing = sorted(str(path) for path in required_members if path not in seen)
    if missing:
        raise ValueError(f"archive lacks required members: {', '.join(missing)}")
    return ArchiveInfo(
        path=archive,
        sha256=digest,
        members=tuple(names),
        byte_size=archive.stat().st_size,
    )


def final_required_members(root: PurePosixPath) -> set[PurePosixPath]:
    required = {
        root / ".run_status",
        root / "INCAR",
        root / "KPOINTS",
        root / "inputs.sha256",
        root / "job.slurm",
        root / "path_manifest.tsv",
        root / "stage_manifest.tsv",
        root / "vasp.stderr",
        root / "vasp.stdout",
        root / "00/POSCAR",
        root / "05/POSCAR",
    }
    for image in INTERMEDIATE_NAMES:
        required.update(
            {
                root / image / "POSCAR",
                root / image / "CONTCAR",
                root / image / "OUTCAR",
                root / image / "OSZICAR",
                root / image / "vasprun.xml",
            }
        )
    return required


def history_required_members() -> set[PurePosixPath]:
    required = {
        TO_CI01_ROOT / "INCAR",
        TO_CI01_ROOT / "KPOINTS",
        TO_CI01_ROOT / "job.slurm",
        TO_CI01_ROOT / "path_manifest.tsv",
        TO_CI01_ROOT / "stage_manifest.tsv",
        TO_CI01_ROOT / "00/POSCAR",
        TO_CI01_ROOT / "05/POSCAR",
        TO_CI02_ROOT / "stage_manifest.tsv",
        TO_CI02_ROOT / "job.slurm",
        TO_CI02_ROOT / "vasp.stderr",
        TO_CI02_ROOT / "slurm-62474917.err",
    }
    for image in INTERMEDIATE_NAMES:
        required.update(
            {
                TO_CI01_ROOT / image / "POSCAR",
                TO_CI01_ROOT / image / "CONTCAR",
                TO_CI01_ROOT / image / "OUTCAR",
                TO_CI01_ROOT / image / "OSZICAR",
            }
        )
    return required


def secure_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:gz") as handle:
        for member in handle.getmembers():
            path = validate_member_name(member.name.rstrip("/"))
            target = destination.joinpath(*path.parts)
            target_resolved = target.resolve()
            if destination != target_resolved and destination not in target_resolved.parents:
                raise ValueError(f"archive member escapes staging directory: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"non-regular archive member is forbidden: {member.name!r}")
            if target.exists():
                raise ValueError(f"archive extraction would overwrite an existing file: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read archive member: {member.name!r}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def copy_pre_stage(source: Path, target: Path) -> None:
    source = source.resolve()
    if not source.is_dir():
        raise ValueError(f"pre-NEB result directory is missing: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for filename in (".run_status", "path_manifest.tsv"):
        shutil.copy2(require_file(source / filename), target / filename)
    for image in INTERMEDIATE_NAMES:
        (target / image).mkdir(parents=True, exist_ok=True)
        for filename in ("OUTCAR", "CONTCAR", "OSZICAR"):
            shutil.copy2(require_file(source / image / filename), target / image / filename)


def parse_manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with require_file(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != ["key", "value", "unit"]:
            raise ValueError(f"unexpected manifest schema: {path}")
        for row in reader:
            values[row["key"]] = row["value"]
    return values


@contextlib.contextmanager
def report_temporary_directory(prefix: str, parent: Path):
    """Create a private staging directory without Windows' 0700 ACL mismatch."""
    parent = parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as temporary:
            yield Path(temporary)
        return

    candidate: Path | None = None
    for _ in range(64):
        proposed = parent / f"{prefix}{secrets.token_hex(8)}"
        try:
            proposed.mkdir()
        except FileExistsError:
            continue
        candidate = proposed.resolve()
        break
    if candidate is None:
        raise OSError(f"cannot allocate a unique report staging directory under {parent}")
    if candidate.parent != parent:
        shutil.rmtree(candidate)
        raise ValueError(f"temporary staging directory escaped its parent: {candidate}")
    try:
        yield candidate
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


class WindowsTemporaryDirectoryAdapter:
    """TemporaryDirectory API backed by report_temporary_directory on Windows."""

    def __init__(self, suffix=None, prefix=None, dir=None, **_kwargs) -> None:
        if suffix not in {None, ""}:
            raise ValueError("temporary directory suffixes are unsupported")
        parent = Path(dir) if dir is not None else Path(tempfile.gettempdir())
        self._context = report_temporary_directory(prefix or "tmp", parent)

    def __enter__(self) -> str:
        return str(self._context.__enter__())

    def __exit__(self, exc_type, exc_value, traceback):
        return self._context.__exit__(exc_type, exc_value, traceback)


def run_official_analysis_in_process(
    workflow_root: Path,
    output: Path,
    analyzer: Path,
) -> str:
    analyzer = require_file(analyzer.resolve())
    spec = importlib.util.spec_from_file_location("_neb_official_analyzer", analyzer)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load official analyzer: {analyzer}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.tempfile = types.SimpleNamespace(
        TemporaryDirectory=WindowsTemporaryDirectoryAdapter
    )

    original_argv = sys.argv[:]
    stdout = io.StringIO()
    stderr = io.StringIO()
    sys.argv = [
        str(analyzer),
        "--tt-result",
        str(workflow_root / TT_FINAL_ROOT),
        "--to-result",
        str(workflow_root / TO_FINAL_ROOT),
        "--output-dir",
        str(output),
    ]
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return_code = module.main()
    except (OSError, ValueError, KeyError) as error:
        raise ValueError(
            "official analyze_neb.py failed:\n"
            f"stdout:\n{stdout.getvalue()}\n"
            f"stderr:\n{stderr.getvalue()}\n"
            f"error:\n{error}"
        ) from error
    finally:
        sys.argv = original_argv
    if return_code != 0 or stderr.getvalue():
        raise ValueError(
            "official analyze_neb.py failed:\n"
            f"stdout:\n{stdout.getvalue()}\n"
            f"stderr:\n{stderr.getvalue()}"
        )
    return stdout.getvalue()


def run_official_analysis(
    workflow_root: Path,
    output: Path,
    analyzer: Path,
) -> str:
    if os.name == "nt":
        completed_stdout = run_official_analysis_in_process(
            workflow_root,
            output,
            analyzer,
        )
        if "NEB analysis PASS" not in completed_stdout:
            raise ValueError("official analyzer did not report NEB analysis PASS")
        return completed_stdout

    completed = subprocess.run(
        [
            sys.executable,
            str(require_file(analyzer.resolve())),
            "--tt-result",
            str(workflow_root / TT_FINAL_ROOT),
            "--to-result",
            str(workflow_root / TO_FINAL_ROOT),
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        raise ValueError(
            "official analyze_neb.py failed:\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    if "NEB analysis PASS" not in completed.stdout:
        raise ValueError("official analyzer did not report NEB analysis PASS")
    return completed.stdout


def parse_force_history(path: Path) -> list[tuple[float, float]]:
    text = require_file(path).read_text(encoding="utf-8", errors="replace")
    rows = [(float(maximum), float(rms)) for maximum, rms in NEB_FORCE_PATTERN.findall(text)]
    if not rows:
        raise ValueError(f"OUTCAR has no VTST projected-force history: {path}")
    return rows


OSZICAR_IONIC_PATTERN = re.compile(
    r"^\s*\d+\s+F=\s*[+\-.0-9Ee]+\s+E0=\s*([+\-.0-9Ee]+)", re.MULTILINE
)
OSZICAR_ELECTRONIC_PATTERN = re.compile(r"^\s*(?:DAV|RMM|CG):\s*(\d+)")


def parse_oszicar_energy_history(path: Path) -> list[float]:
    text = require_file(path).read_text(encoding="utf-8", errors="replace")
    values = [float(value) for value in OSZICAR_IONIC_PATTERN.findall(text)]
    if not values:
        raise ValueError(f"OSZICAR has no ionic E0 history: {path}")
    return values


def parse_oszicar_electronic_maxima(path: Path) -> list[int]:
    maxima: list[int] = []
    current = 0
    for line in require_file(path).read_text(encoding="utf-8", errors="replace").splitlines():
        electronic = OSZICAR_ELECTRONIC_PATTERN.match(line)
        if electronic:
            current = max(current, int(electronic.group(1)))
            continue
        if OSZICAR_IONIC_PATTERN.match(line):
            if current == 0:
                raise ValueError(f"ionic OSZICAR row lacks preceding electronic iterations: {path}")
            maxima.append(current)
            current = 0
    if not maxima:
        raise ValueError(f"OSZICAR has no electronic iteration history: {path}")
    return maxima


def infer_job_id(directory: Path) -> str:
    status_path = directory / ".run_status"
    if status_path.is_file():
        value = parse_key_values(status_path).get("slurm_job_id", "")
        if value:
            return value
    candidates = sorted(directory.glob("slurm-*.out")) + sorted(directory.glob("config.*"))
    for candidate in candidates:
        match = re.search(r"(\d{8})", candidate.name)
        if match:
            return match.group(1)
    return "unknown"


def load_stage(path_name: str, directory: Path, stage_kind: str) -> StageSeries:
    image_forces = {
        image: parse_force_history(directory / image / "OUTCAR") for image in INTERMEDIATE_NAMES
    }
    image_energies = {
        image: parse_oszicar_energy_history(directory / image / "OSZICAR")
        for image in INTERMEDIATE_NAMES
    }
    stage = StageSeries(
        path_name=path_name,
        stage_name=directory.name if stage_kind == "ci_neb" else "pre",
        stage_kind=stage_kind,
        job_id=infer_job_id(directory),
        image_forces=image_forces,
        image_energies=image_energies,
    )
    _ = stage.iterations
    return stage


def validate_final_electronic_convergence(directory: Path, expected_iterations: int) -> None:
    incar = require_file(directory / "INCAR").read_text(encoding="utf-8", errors="replace")
    nelm_match = re.search(r"^\s*NELM\s*=\s*(\d+)", incar, re.MULTILINE | re.IGNORECASE)
    if nelm_match is None:
        raise ValueError(f"final INCAR lacks NELM: {directory / 'INCAR'}")
    nelm = int(nelm_match.group(1))
    for image in INTERMEDIATE_NAMES:
        outcar_path = directory / image / "OUTCAR"
        text = require_file(outcar_path).read_text(encoding="utf-8", errors="replace")
        force_positions = [match.start() for match in NEB_FORCE_PATTERN.finditer(text)]
        ediff_positions = [
            match.start() for match in re.finditer(re.escape(EDIFF_MARKER), text, re.IGNORECASE)
        ]
        if len(force_positions) != expected_iterations or len(ediff_positions) != expected_iterations:
            raise ValueError(
                f"final electronic/ionic count mismatch for {outcar_path}: "
                f"forces={len(force_positions)}, EDIFF={len(ediff_positions)}, expected={expected_iterations}"
            )
        if not ediff_positions or ediff_positions[-1] > force_positions[-1]:
            raise ValueError(f"final ionic step lacks a preceding EDIFF marker: {outcar_path}")
        maxima = parse_oszicar_electronic_maxima(directory / image / "OSZICAR")
        if len(maxima) != expected_iterations:
            raise ValueError(f"OSZICAR electronic history length mismatch: {directory / image}")
        if max(maxima) >= nelm:
            raise ValueError(f"electronic loop reached NELM={nelm}: {directory / image}")


def validate_final_logs(directory: Path) -> None:
    stderr = require_file(directory / "vasp.stderr").read_text(
        encoding="utf-8", errors="replace"
    )
    if re.search(r"internal error in:\s*vhdf5\.F", stderr, re.IGNORECASE) is None:
        raise ValueError(f"expected vhdf5.F post-run error is absent: {directory}")
    if re.search(r"HDF5 call .* produced error:\s*29\b", stderr, re.IGNORECASE) is None:
        raise ValueError(f"expected HDF5 error 29 is absent: {directory}")
    if FATAL_LOG_PATTERN.search(stderr):
        raise ValueError(f"unexpected fatal error accompanies HDF5 error 29: {directory}")
    for path in directory.glob("slurm-*.err"):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            raise ValueError(f"final Slurm stderr is not empty: {path}")


def read_official_profile(path: Path, path_name: str) -> list[dict[str, str]]:
    with require_file(path).open("r", encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle, delimiter="\t") if row["path"] == path_name]
    if [row["image"] for row in rows] != list(IMAGE_NAMES):
        raise ValueError(f"official profile lacks the six ordered images for {path_name}")
    return rows


def final_structure(directory: Path, image: str) -> Structure:
    filename = "POSCAR" if image in {"00", "05"} else "CONTCAR"
    return parse_poscar(directory / image / filename)


def minimum_image_vector(delta_frac: np.ndarray, lattice: np.ndarray) -> np.ndarray:
    _, vector, _ = minimum_image(delta_frac, lattice)
    return vector


def build_path_result(
    name: str,
    final_directory: Path,
    stages: list[StageSeries],
    official_profile: Path,
) -> PathResult:
    manifest = parse_manifest(final_directory / "path_manifest.tsv")
    start_e0 = float(manifest["start_e0_ev"])
    end_e0 = float(manifest["end_e0_ev"])
    profile_rows = read_official_profile(official_profile, name)
    energies = [float(row["e0_ev"]) for row in profile_rows]
    reaction_coordinates = [float(row["reaction_coordinate_a"]) for row in profile_rows]
    structures = [final_structure(final_directory, image) for image in IMAGE_NAMES]
    for image, structure in zip(IMAGE_NAMES, structures):
        if structure.elements != ("Zr", "H") or structure.counts != (96, 1):
            raise ValueError(f"{name}/{image} is not Zr H / 96 1")
        require_same_topology(structures[0], structure, f"{name}/{image}")

    final_image_forces = {
        image: stages[-1].image_forces[image][-1] for image in INTERMEDIATE_NAMES
    }
    geometry_rows: list[dict[str, object]] = []
    neighbor_rows: list[dict[str, object]] = []
    cumulative_vectors = [np.zeros(3, dtype=float)]
    cumulative_length = 0.0
    direct_distance, direct_vector, _ = minimum_image(
        structures[-1].frac[-1] - structures[0].frac[-1], structures[0].lattice
    )
    if direct_distance <= 0:
        raise ValueError(f"zero endpoint H distance for {name}")
    direction = direct_vector / direct_distance
    previous_projection = -1.0e-12

    for index, (image, structure) in enumerate(zip(IMAGE_NAMES, structures)):
        neighbors = h_neighbors(structure)
        minimum_pair = minimum_pair_distance(structure)
        if minimum_pair < MIN_EXPECTED_PAIR_A:
            raise ValueError(
                f"minimum pair distance regressed below {MIN_EXPECTED_PAIR_A:.3f} A: "
                f"{name}/{image}={minimum_pair:.8f}"
            )
        if index == 0:
            h_step = basal = c_component = full_rss = host_rss = 0.0
            host_rms = host_max = 0.0
            step_vector = np.zeros(3, dtype=float)
        else:
            previous = structures[index - 1]
            h_step, step_vector, _ = minimum_image(
                structure.frac[-1] - previous.frac[-1], structure.lattice
            )
            basal = float(np.linalg.norm(step_vector[:2]))
            c_component = float(step_vector[2])
            per_atom = np.asarray(
                [
                    minimum_image(
                        structure.frac[atom] - previous.frac[atom], structure.lattice
                    )[0]
                    for atom in range(structure.atom_count)
                ],
                dtype=float,
            )
            full_rss = float(np.linalg.norm(per_atom))
            host_rss = float(np.linalg.norm(per_atom[:-1]))
            host_rms = float(np.sqrt(np.mean(per_atom[:-1] ** 2)))
            host_max = float(np.max(per_atom[:-1]))
            if h_step <= 1.0e-6 or full_rss <= 1.0e-6:
                raise ValueError(f"collapsed adjacent images in {name}: {index - 1:02d}->{image}")
            cumulative_length += h_step
            cumulative_vectors.append(cumulative_vectors[-1] + step_vector)
        projection = float(np.dot(cumulative_vectors[-1], direction))
        if index and projection <= previous_projection:
            raise ValueError(f"H path backtracks along its endpoint direction: {name}/{image}")
        previous_projection = projection
        coordination = sum(distance <= COORDINATION_CUTOFF_A for distance in neighbors)
        geometry_rows.append(
            {
                "path": name,
                "image": image,
                "reaction_coordinate_a": reaction_coordinates[index],
                "h_step_a": h_step,
                "h_step_basal_a": basal,
                "h_step_c_a": c_component,
                "full_rss_step_a": full_rss,
                "host_rss_step_a": host_rss,
                "host_rms_step_a": host_rms,
                "host_max_step_a": host_max,
                "minimum_pair_a": minimum_pair,
                "coordination_2p5a": coordination,
                "zr_h_distances": neighbors[:6],
            }
        )
        for rank, distance in enumerate(neighbors[:6], start=1):
            neighbor_rows.append(
                {
                    "path": name,
                    "image": image,
                    "reaction_coordinate_a": f"{reaction_coordinates[index]:.8f}",
                    "neighbor_rank": rank,
                    "zr_h_distance_a": f"{distance:.8f}",
                    "is_within_2p5a": "yes" if distance <= COORDINATION_CUTOFF_A else "no",
                }
            )

    if abs(cumulative_length - reaction_coordinates[-1]) > 5.0e-7:
        raise ValueError(f"H path length differs from official analyzer for {name}")
    expected_direct = float(manifest["h_endpoint_distance_a"])
    if abs(direct_distance - expected_direct) > 5.0e-7:
        raise ValueError(f"endpoint H distance differs from manifest for {name}")
    lateral = []
    for vector in cumulative_vectors:
        lateral.append(float(np.linalg.norm(vector - np.dot(vector, direction) * direction)))

    return PathResult(
        name=name,
        final_directory=final_directory,
        start_e0=start_e0,
        end_e0=end_e0,
        structures=structures,
        energies=energies,
        reaction_coordinates=reaction_coordinates,
        final_image_forces=final_image_forces,
        stages=stages,
        geometry_rows=geometry_rows,
        neighbor_rows=neighbor_rows,
        direct_h_distance=direct_distance,
        h_path_length=cumulative_length,
        path_tortuosity=cumulative_length / direct_distance,
        max_lateral_deviation=max(lateral),
    )


def assert_close(label: str, actual: float, expected: float, tolerance: float) -> None:
    if not math.isfinite(actual) or abs(actual - expected) > tolerance:
        raise ValueError(f"numeric regression failed for {label}: {actual} != {expected}")


def validate_regressions(result: PathResult) -> None:
    expected = EXPECTED_RESULTS[result.name]
    assert_close(
        f"{result.name} forward barrier",
        result.barrier_forward,
        float(expected["barrier_forward"]),
        5.0e-8,
    )
    assert_close(
        f"{result.name} reverse barrier",
        result.barrier_reverse,
        float(expected["barrier_reverse"]),
        5.0e-8,
    )
    assert_close(
        f"{result.name} final force",
        result.final_force,
        float(expected["final_force"]),
        5.0e-7,
    )
    assert_close(
        f"{result.name} saddle force",
        result.final_image_forces[result.saddle_image][0],
        float(expected["saddle_force"]),
        5.0e-7,
    )
    assert_close(
        f"{result.name} H path length",
        result.h_path_length,
        float(expected["path_length"]),
        5.0e-8,
    )
    if result.saddle_image != expected["saddle_image"]:
        raise ValueError(
            f"saddle-image regression failed for {result.name}: {result.saddle_image}"
        )
    pre_iterations = sum(stage.iterations for stage in result.stages if stage.stage_kind == "pre_neb")
    ci_iterations = sum(stage.iterations for stage in result.stages if stage.stage_kind == "ci_neb")
    if pre_iterations != expected["pre_iterations"] or ci_iterations != expected["ci_iterations"]:
        raise ValueError(
            f"stage-iteration regression failed for {result.name}: "
            f"pre={pre_iterations}, ci={ci_iterations}"
        )
    aggregate = result.final_stage.aggregate_force()
    maxima = [row[0] for row in aggregate]
    if any(value <= FORCE_LIMIT_EV_A for value in maxima[:-1]):
        raise ValueError(f"{result.name} crossed the CI force limit before its final iteration")
    if maxima[-1] > FORCE_LIMIT_EV_A:
        raise ValueError(f"{result.name} final force exceeds the CI limit")
    tail = maxima[-12:]
    if len(tail) != 12 or not all(first > second for first, second in zip(tail, tail[1:])):
        raise ValueError(f"{result.name} final 12 forces are not strictly decreasing")
    saddle_coordination = int(result.geometry_rows[result.saddle_index]["coordination_2p5a"])
    if saddle_coordination != 3:
        raise ValueError(f"{result.name} saddle is not the expected three-coordinate bottleneck")


def build_output_rows(
    results: Sequence[PathResult],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    summaries: list[dict[str, object]] = []
    images: list[dict[str, object]] = []
    neighbors: list[dict[str, object]] = []
    barriers: list[dict[str, object]] = []
    for result in results:
        pre_iterations = sum(
            stage.iterations for stage in result.stages if stage.stage_kind == "pre_neb"
        )
        ci_iterations = sum(
            stage.iterations for stage in result.stages if stage.stage_kind == "ci_neb"
        )
        minimum_row = min(result.geometry_rows, key=lambda row: float(row["minimum_pair_a"]))
        aggregate = result.final_stage.aggregate_force()
        summaries.append(
            {
                "path": result.name,
                "saddle_image": result.saddle_image,
                "start_e0_ev": f"{result.start_e0:.10f}",
                "end_e0_ev": f"{result.end_e0:.10f}",
                "barrier_forward_ev": f"{result.barrier_forward:.8f}",
                "barrier_reverse_ev": f"{result.barrier_reverse:.8f}",
                "final_max_projected_force_ev_a": f"{result.final_force:.8f}",
                "force_margin_ev_a": f"{FORCE_LIMIT_EV_A - result.final_force:.8f}",
                "saddle_final_projected_force_ev_a": (
                    f"{result.final_image_forces[result.saddle_image][0]:.8f}"
                ),
                "pre_iterations": pre_iterations,
                "ci_iterations": ci_iterations,
                "final_ci_iterations": result.final_stage.iterations,
                "total_iterations": pre_iterations + ci_iterations,
                "h_direct_distance_a": f"{result.direct_h_distance:.8f}",
                "h_path_length_a": f"{result.h_path_length:.8f}",
                "path_tortuosity": f"{result.path_tortuosity:.8f}",
                "max_lateral_deviation_a": f"{result.max_lateral_deviation:.8f}",
                "minimum_pair_a": f"{float(minimum_row['minimum_pair_a']):.8f}",
                "minimum_pair_image": minimum_row["image"],
                "last12_force_strictly_decreasing": "yes",
                "acceptance_status": "PASS_HDF5_POSTRUN",
            }
        )
        for index, (image, energy, geometry) in enumerate(
            zip(IMAGE_NAMES, result.energies, result.geometry_rows)
        ):
            force = result.final_image_forces.get(image)
            distances = list(geometry["zr_h_distances"])
            images.append(
                {
                    "path": result.name,
                    "image": image,
                    "is_saddle": "yes" if image == result.saddle_image else "no",
                    "reaction_coordinate_a": f"{result.reaction_coordinates[index]:.8f}",
                    "e0_ev": f"{energy:.10f}",
                    "relative_to_t_ev": f"{energy - result.start_e0:.10f}",
                    "final_projected_force_ev_a": "" if force is None else f"{force[0]:.8f}",
                    "final_projected_rms_ev_a": "" if force is None else f"{force[1]:.8f}",
                    "h_step_a": f"{float(geometry['h_step_a']):.8f}",
                    "h_step_basal_a": f"{float(geometry['h_step_basal_a']):.8f}",
                    "h_step_c_a": f"{float(geometry['h_step_c_a']):.8f}",
                    "full_rss_step_a": f"{float(geometry['full_rss_step_a']):.8f}",
                    "host_rss_step_a": f"{float(geometry['host_rss_step_a']):.8f}",
                    "host_rms_step_a": f"{float(geometry['host_rms_step_a']):.8f}",
                    "host_max_step_a": f"{float(geometry['host_max_step_a']):.8f}",
                    "minimum_pair_a": f"{float(geometry['minimum_pair_a']):.8f}",
                    "coordination_2p5a": geometry["coordination_2p5a"],
                    **{
                        f"zr_h_{rank}_a": f"{distance:.8f}"
                        for rank, distance in enumerate(distances, start=1)
                    },
                }
            )
        neighbors.extend(
            {
                **row,
                "is_saddle": "yes" if row["image"] == result.saddle_image else "no",
            }
            for row in result.neighbor_rows
        )
        global_iteration = 0
        for stage in result.stages:
            for stage_iteration, (forward, reverse, peak) in enumerate(
                stage.barrier_history(result.start_e0, result.end_e0), start=1
            ):
                global_iteration += 1
                barriers.append(
                    {
                        "path": result.name,
                        "stage": stage.stage_name,
                        "stage_kind": stage.stage_kind,
                        "job_id": stage.job_id,
                        "stage_iteration": stage_iteration,
                        "global_iteration": global_iteration,
                        "peak_image": peak,
                        "barrier_forward_ev": f"{forward:.8f}",
                        "barrier_reverse_ev": f"{reverse:.8f}",
                    }
                )
    return summaries, images, neighbors, barriers


def build_literature_rows(results: Sequence[PathResult]) -> list[dict[str, object]]:
    """Return the comparison dataset in a stable, explicitly audited order."""

    rows: list[dict[str, object]] = []
    for dataset in LITERATURE_DATASETS:
        for path, barrier in dataset.barriers:
            rows.append(
                {
                    "source": dataset.source,
                    "year": dataset.year,
                    "method_or_provenance": dataset.method_or_provenance,
                    "path": path,
                    "barrier_eV": f"{barrier:.8f}",
                    "doi": dataset.doi,
                    "direct_or_secondary": dataset.direct_or_secondary,
                }
            )

    by_name = {result.name: result for result in results}
    if set(by_name) != {"TT_c", "TO"}:
        raise ValueError(
            "literature comparison requires exactly TT_c and TO results: "
            f"{sorted(by_name)}"
        )
    current = (
        ("TT", by_name["TT_c"].barrier_forward),
        ("TO", by_name["TO"].barrier_forward),
        ("OT", by_name["TO"].barrier_reverse),
    )
    for path, barrier in current:
        rows.append(
            {
                "source": "This work",
                "year": 2026,
                "method_or_provenance": "Internal 0 K PBE/PAW CI-NEB result",
                "path": path,
                "barrier_eV": f"{barrier:.8f}",
                "doi": "",
                "direct_or_secondary": "internal",
            }
        )
    return rows


def write_tsv_outputs(
    output: Path,
    official_output: Path,
    summaries: list[dict[str, object]],
    images: list[dict[str, object]],
    neighbors: list[dict[str, object]],
    barriers: list[dict[str, object]],
    literature: list[dict[str, object]],
) -> None:
    for filename in ("neb_profile.tsv", "neb_convergence.tsv", "path_geometry.tsv"):
        shutil.copy2(require_file(official_output / filename), output / filename)
    atomic_write_tsv(
        output / "neb_summary.tsv",
        tuple(summaries[0].keys()),
        summaries,
    )
    atomic_write_tsv(
        output / "neb_image_details.tsv",
        tuple(images[0].keys()),
        images,
    )
    atomic_write_tsv(
        output / "neb_coordination.tsv",
        tuple(neighbors[0].keys()),
        neighbors,
    )
    atomic_write_tsv(
        output / "neb_barrier_convergence.tsv",
        tuple(barriers[0].keys()),
        barriers,
    )
    atomic_write_tsv(
        output / "neb_literature_comparison.tsv",
        tuple(literature[0].keys()),
        literature,
    )


def configure_matplotlib(cache_directory: Path):
    os.environ["MPLCONFIGDIR"] = str(cache_directory)
    cache_directory.mkdir(parents=True, exist_ok=True)
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager
    from matplotlib import pyplot as plt

    candidates = (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            font_manager.fontManager.addfont(str(candidate))
            family = font_manager.FontProperties(fname=str(candidate)).get_name()
            matplotlib.rcParams["font.family"] = family
            break
    matplotlib.rcParams.update(
        {
            "axes.unicode_minus": False,
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.labelsize": 11,
            "legend.fontsize": 9,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "axes.linewidth": 0.8,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )
    return plt


TT_COLOR = "#2A6FBB"
TO_COLOR = "#D1495B"
NEUTRAL = "#5B6470"
LIGHT_NEUTRAL = "#C8CDD3"
GRID = "#D9DDE2"
LIMIT_COLOR = "#333333"
REPORT_DPI = 300
MIN_PNG_WIDTH = 1800
MIN_PNG_HEIGHT = 1200
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CJK_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")
LEGACY_PNG_NAMES = (
    "neb_literature_comparison.png",
)
STANDALONE_PNG_NAMES = (
    "01_energy_profile_tt.png",
    "02_energy_profile_to_ot.png",
    "03_force_history_tt.png",
    "04_force_final15_tt.png",
    "05_force_history_to.png",
    "06_force_final15_to.png",
    "07_coordination_tt.png",
    "08_coordination_to.png",
    "09_local_structure_tt_00.png",
    "10_local_structure_tt_ts03.png",
    "11_local_structure_tt_05.png",
    "12_local_structure_to_00.png",
    "13_local_structure_to_ts02.png",
    "14_local_structure_to_05.png",
    "15_image_spacing_tt.png",
    "16_image_spacing_to.png",
    "17_h_displacement_components_tt.png",
    "18_h_displacement_components_to.png",
    "19_minimum_distance_tt.png",
    "20_minimum_distance_to.png",
    "21_literature_barriers.png",
)
COMPOSITE_PNG_NAMES = (
    "neb_energy_profiles.png",
    "neb_force_convergence.png",
    "neb_coordination_evolution.png",
    "neb_transition_structures.png",
    "neb_path_geometry.png",
)
REPORT_PNG_NAMES = STANDALONE_PNG_NAMES + COMPOSITE_PNG_NAMES
REPORT_TSV_NAMES = (
    "neb_summary.tsv",
    "neb_profile.tsv",
    "neb_convergence.tsv",
    "path_geometry.tsv",
    "neb_image_details.tsv",
    "neb_coordination.tsv",
    "neb_barrier_convergence.tsv",
    "neb_literature_comparison.tsv",
)
COMPOSITE_MINIMUM_PIXELS = {
    "neb_energy_profiles.png": (3600, 1600),
    "neb_force_convergence.png": (3600, 2800),
    "neb_coordination_evolution.png": (3600, 1600),
    "neb_transition_structures.png": (3600, 2500),
    "neb_path_geometry.png": (3600, 2500),
}


def path_display_name(result: PathResult) -> str:
    return "TT" if result.name == "TT_c" else "TO"


def path_color(result: PathResult) -> str:
    return TT_COLOR if result.name == "TT_c" else TO_COLOR


def finish_axis(axis, grid_axis: str = "y") -> None:
    axis.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    axis.spines[["top", "right"]].set_visible(False)


def require_english_figure_text(figure) -> None:
    for artist in figure.findobj():
        get_text = getattr(artist, "get_text", None)
        if not callable(get_text):
            continue
        value = get_text()
        if isinstance(value, str) and CJK_TEXT_PATTERN.search(value):
            raise ValueError(f"figure contains CJK text: {value!r}")


def png_metadata(path: Path) -> tuple[int, int, float, float]:
    width: int | None = None
    height: int | None = None
    pixels_per_meter: tuple[int, int] | None = None
    with require_file(path).open("rb") as handle:
        if handle.read(8) != PNG_SIGNATURE:
            raise ValueError(f"report image is not a PNG: {path}")
        while True:
            header = handle.read(8)
            if len(header) != 8:
                raise ValueError(f"truncated PNG chunk header: {path}")
            length, chunk_type = struct.unpack(">I4s", header)
            payload = handle.read(length)
            checksum = handle.read(4)
            if len(payload) != length or len(checksum) != 4:
                raise ValueError(f"truncated PNG chunk: {path}")
            if chunk_type == b"IHDR":
                width, height = struct.unpack(">II", payload[:8])
            elif chunk_type == b"pHYs":
                x_ppm, y_ppm, unit = struct.unpack(">IIB", payload)
                if unit == 1:
                    pixels_per_meter = (x_ppm, y_ppm)
            elif chunk_type == b"IEND":
                break
    if width is None or height is None or pixels_per_meter is None:
        raise ValueError(f"PNG lacks dimensions or physical resolution metadata: {path}")
    x_dpi = pixels_per_meter[0] * 0.0254
    y_dpi = pixels_per_meter[1] * 0.0254
    return width, height, x_dpi, y_dpi


def validate_png_asset(path: Path) -> None:
    width, height, x_dpi, y_dpi = png_metadata(path)
    minimum_width, minimum_height = COMPOSITE_MINIMUM_PIXELS.get(
        path.name,
        (MIN_PNG_WIDTH, MIN_PNG_HEIGHT),
    )
    if width < minimum_width or height < minimum_height:
        raise ValueError(
            f"report image is below {minimum_width}x{minimum_height} pixels: "
            f"{path} is {width}x{height}"
        )
    if x_dpi < REPORT_DPI - 1.0 or y_dpi < REPORT_DPI - 1.0:
        raise ValueError(
            f"report image resolution is below {REPORT_DPI} dpi: "
            f"{path} is {x_dpi:.2f}x{y_dpi:.2f} dpi"
        )


def save_figure(plt, figure, output: Path) -> None:
    require_english_figure_text(figure)
    try:
        figure.savefig(
            output,
            dpi=REPORT_DPI,
            bbox_inches="tight",
            pad_inches=0.08,
        )
    finally:
        plt.close(figure)
    validate_png_asset(output)


def draw_energy_profile(axis, result: PathResult) -> None:
    color = path_color(result)
    relative = np.asarray(result.energies) - result.start_e0
    axis.plot(
        result.reaction_coordinates,
        relative,
        color=color,
        linewidth=2.2,
        marker="o" if result.name == "TT_c" else "s",
        markersize=7,
        markeredgecolor="white",
        markeredgewidth=0.8,
    )
    for x, y, image in zip(result.reaction_coordinates, relative, IMAGE_NAMES):
        axis.annotate(
            image,
            (x, y),
            xytext=(0, 11 if image != result.saddle_image else 15),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontweight="bold" if image == result.saddle_image else "normal",
        )
    saddle_x = result.reaction_coordinates[result.saddle_index]
    saddle_y = relative[result.saddle_index]
    axis.scatter(
        [saddle_x],
        [saddle_y],
        s=155,
        facecolors="none",
        edgecolors=LIMIT_COLOR,
        linewidths=1.5,
        zorder=5,
    )
    barrier_label = f"TS {result.saddle_image}\n{path_display_name(result)} = {result.barrier_forward:.3f} eV"
    if result.name == "TO":
        barrier_label += f"\nOT = {result.barrier_reverse:.3f} eV"
    axis.annotate(
        barrier_label,
        (saddle_x, saddle_y),
        xytext=(36, -5),
        textcoords="offset points",
        arrowprops={"arrowstyle": "->", "color": LIMIT_COLOR, "lw": 1.0},
        va="center",
        bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": GRID, "alpha": 0.95},
    )
    if result.name == "TO":
        endpoint_delta = result.end_e0 - result.start_e0
        axis.axhline(endpoint_delta, color=NEUTRAL, ls="--", lw=1.0)
        axis.text(
            result.reaction_coordinates[-1] * 0.56,
            endpoint_delta + 0.014,
            f"O - T = {endpoint_delta:.6f} eV",
            color=NEUTRAL,
        )
    title = "TT CI-NEB energy profile" if result.name == "TT_c" else "TO/OT CI-NEB energy profile"
    axis.set_title(title)
    axis.set_ylabel("Energy relative to T (eV)")
    axis.set_xlabel("Cumulative H path length (A)")
    axis.margins(x=0.04, y=0.20)
    finish_axis(axis)


def plot_energy_profile(plt, result: PathResult, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(10.0, 6.0), constrained_layout=True)
    draw_energy_profile(axis, result)
    save_figure(plt, figure, output)


def stage_offsets(result: PathResult) -> list[tuple[StageSeries, int, int]]:
    rows: list[tuple[StageSeries, int, int]] = []
    start = 1
    for stage in result.stages:
        end = start + stage.iterations - 1
        rows.append((stage, start, end))
        start = end + 1
    return rows


def draw_force_convergence(axis, result: PathResult) -> None:
    color = path_color(result)
    for stage, start, end in stage_offsets(result):
        aggregate = stage.aggregate_force()
        xs = np.arange(start, end + 1)
        ys = np.asarray([item[0] for item in aggregate])
        stage_color = color if stage.stage_kind == "ci_neb" else LIGHT_NEUTRAL
        axis.plot(
            xs,
            ys,
            color=stage_color,
            lw=2.0,
            marker="o" if len(xs) == 1 else None,
            ms=5,
            label=f"{stage.stage_name} | Job {stage.job_id}",
        )
        if start > 1:
            axis.axvline(start - 0.5, color=NEUTRAL, ls=":", lw=1.0)
    axis.axhline(PRE_FORCE_LIMIT_EV_A, color=NEUTRAL, ls="--", lw=1.0, label="pre-NEB limit")
    axis.axhline(FORCE_LIMIT_EV_A, color=LIMIT_COLOR, ls="--", lw=1.2, label="CI-NEB limit")
    axis.set_yscale("log")
    axis.set_ylim(0.024, 0.75)
    axis.set_ylabel("Maximum projected force (eV/A)")
    axis.set_xlabel("Concatenated NEB ionic step")
    counts = "29 + 126 steps" if result.name == "TT_c" else "37 + 1 + 135 steps"
    axis.set_title(f"{path_display_name(result)} staged force convergence | {counts}")
    finish_axis(axis)
    axis.legend(loc="upper right", frameon=False, ncol=2)


def plot_force_convergence(plt, result: PathResult, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(10.5, 6.2), constrained_layout=True)
    draw_force_convergence(axis, result)
    save_figure(plt, figure, output)


def draw_force_tail(axis, result: PathResult) -> None:
    final = result.final_stage
    tail_count = 15
    tail_start = final.iterations - tail_count + 1
    xs = np.arange(tail_start, final.iterations + 1)
    line_styles = ("-", "--", "-.", ":")
    markers = ("o", "s", "^", "D")
    all_values: list[float] = []
    for index, image in enumerate(INTERMEDIATE_NAMES):
        values = [pair[0] for pair in final.image_forces[image]][-tail_count:]
        all_values.extend(values)
        axis.plot(
            xs,
            values,
            color=LIGHT_NEUTRAL if index > 0 else NEUTRAL,
            ls=line_styles[index],
            marker=markers[index],
            markevery=(len(xs) - 1, len(xs)),
            ms=4,
            lw=1.2,
            label=f"Image {image}",
        )
    envelope = [row[0] for row in final.aggregate_force()][-tail_count:]
    axis.plot(
        xs,
        envelope,
        color=path_color(result),
        lw=2.4,
        marker="o",
        ms=4,
        label="Global maximum",
        zorder=4,
    )
    axis.axhline(FORCE_LIMIT_EV_A, color=LIMIT_COLOR, ls="--", lw=1.2, label="CI-NEB limit")
    axis.text(
        0.02,
        0.06,
        f"Margin = {FORCE_LIMIT_EV_A - result.final_force:.6f} eV/A",
        transform=axis.transAxes,
        color=path_color(result),
    )
    axis.set_ylim(max(0.0, min(all_values) - 0.002), 0.038)
    axis.set_xlim(xs[0], xs[-1])
    axis.set_xlabel("Final CI stage step")
    axis.set_ylabel("Projected force (eV/A)")
    axis.set_title(f"{path_display_name(result)} final CI force | last 15 steps")
    finish_axis(axis)
    axis.legend(loc="upper right", frameon=False, ncol=2)


def plot_force_tail(plt, result: PathResult, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(8.6, 5.6), constrained_layout=True)
    draw_force_tail(axis, result)
    save_figure(plt, figure, output)


def segment_axis_data(result: PathResult) -> tuple[np.ndarray, list[str], list[dict[str, object]]]:
    geometry = result.geometry_rows[1:]
    labels = [f"{first}-{second}" for first, second in zip(IMAGE_NAMES, IMAGE_NAMES[1:])]
    return np.arange(len(geometry)), labels, geometry


def draw_path_steps(axis, result: PathResult) -> None:
    x, labels, geometry = segment_axis_data(result)
    axis.plot(x, [row["h_step_a"] for row in geometry], "o-", color=path_color(result), label="H step")
    axis.plot(x, [row["full_rss_step_a"] for row in geometry], "s--", color=NEUTRAL, label="Full-system RSS")
    axis.plot(x, [row["host_rss_step_a"] for row in geometry], "^:", color=LIGHT_NEUTRAL, label="Zr-host RSS")
    axis.set_xticks(x, labels)
    axis.set_xlabel("Image segment")
    axis.set_ylabel("Adjacent-image displacement (A)")
    axis.set_title(f"{path_display_name(result)} adjacent-image displacement")
    finish_axis(axis)
    axis.legend(frameon=False)


def plot_path_steps(plt, result: PathResult, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(8.8, 5.6), constrained_layout=True)
    draw_path_steps(axis, result)
    save_figure(plt, figure, output)


def draw_path_components(axis, result: PathResult) -> None:
    x, labels, geometry = segment_axis_data(result)
    axis.plot(x, [row["h_step_basal_a"] for row in geometry], "o-", color=path_color(result), label="Basal magnitude")
    axis.plot(x, [row["h_step_c_a"] for row in geometry], "s--", color=NEUTRAL, label="Signed c component")
    axis.axhline(0.0, color=GRID, lw=0.8)
    axis.set_xticks(x, labels)
    axis.set_xlabel("Image segment")
    axis.set_ylabel("H displacement component (A)")
    axis.set_title(f"{path_display_name(result)} H-path direction components")
    finish_axis(axis)
    axis.legend(frameon=False)


def plot_path_components(plt, result: PathResult, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(8.8, 5.6), constrained_layout=True)
    draw_path_components(axis, result)
    save_figure(plt, figure, output)


def draw_minimum_distance(axis, result: PathResult) -> None:
    x = np.arange(6)
    values = [row["minimum_pair_a"] for row in result.geometry_rows]
    axis.plot(x, values, "o-", color=path_color(result), lw=2.0, label="Minimum pair distance")
    axis.axhline(MIN_EXPECTED_PAIR_A, color=NEUTRAL, ls="--", lw=1.0, label="Regression floor")
    axis.axvline(result.saddle_index, color=LIMIT_COLOR, ls=":", lw=1.0)
    axis.scatter(
        [result.saddle_index],
        [values[result.saddle_index]],
        s=115,
        facecolors="none",
        edgecolors=LIMIT_COLOR,
        linewidths=1.4,
        zorder=4,
    )
    saddle_value = values[result.saddle_index]
    annotation_offset = (22, 24) if saddle_value < 1.95 else (22, -24)
    axis.annotate(
        f"TS {result.saddle_image} | {saddle_value:.6f} A",
        (result.saddle_index, saddle_value),
        xytext=annotation_offset,
        textcoords="offset points",
        arrowprops={"arrowstyle": "->", "color": LIMIT_COLOR, "lw": 0.9},
    )
    axis.set_xticks(x, IMAGE_NAMES)
    axis.set_ylim(1.90, 2.36)
    axis.set_xlabel("Image")
    axis.set_ylabel("Minimum pair distance (A)")
    axis.set_title(f"{path_display_name(result)} minimum pair distance")
    finish_axis(axis)
    axis.legend(frameon=False, loc="upper left")


def plot_minimum_distance(plt, result: PathResult, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(8.8, 5.6), constrained_layout=True)
    draw_minimum_distance(axis, result)
    save_figure(plt, figure, output)


def draw_coordination(axis, result: PathResult, show_legend: bool = True) -> None:
    markers = ("o", "s", "^", "D", "v", "P")
    rank_colors = ("#1D4E89", "#2A6FBB", "#5A8FD0", "#8A96A3", "#AAB2BC", "#C8CDD3")
    ordinals = ("1st", "2nd", "3rd", "4th", "5th", "6th")
    x = np.arange(6)
    for rank in range(6):
        values = [float(row["zr_h_distances"][rank]) for row in result.geometry_rows]
        axis.plot(
            x,
            values,
            marker=markers[rank],
            color=rank_colors[rank],
            lw=1.5,
            ms=5,
            label=f"{ordinals[rank]} neighbor",
        )
    axis.axhline(COORDINATION_CUTOFF_A, color=LIMIT_COLOR, ls="--", lw=1.0, label="2.5 A cutoff")
    axis.axvline(result.saddle_index, color=path_color(result), ls=":", lw=1.2)
    for image_index, row in enumerate(result.geometry_rows):
        axis.text(
            image_index,
            4.03,
            f"CN={row['coordination_2p5a']}",
            ha="center",
            fontsize=9,
            color=NEUTRAL,
        )
    axis.set_xticks(x, IMAGE_NAMES)
    axis.set_ylim(1.82, 4.15)
    axis.set_xlabel("Image")
    axis.set_ylabel("H-Zr minimum-image distance (A)")
    axis.set_title(f"{path_display_name(result)} H-Zr nearest-neighbor distances | TS {result.saddle_image}")
    finish_axis(axis)
    if show_legend:
        axis.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), frameon=False)


def plot_coordination(plt, result: PathResult, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(9.6, 6.0), constrained_layout=True)
    draw_coordination(axis, result)
    save_figure(plt, figure, output)


def camera_project(vectors: np.ndarray) -> np.ndarray:
    azimuth = math.radians(-35.0)
    elevation = math.radians(24.0)
    rz = np.asarray(
        [
            [math.cos(azimuth), -math.sin(azimuth), 0.0],
            [math.sin(azimuth), math.cos(azimuth), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    rx = np.asarray(
        [
            [1.0, 0.0, 0.0],
            [0.0, math.cos(elevation), -math.sin(elevation)],
            [0.0, math.sin(elevation), math.cos(elevation)],
        ]
    )
    rotated = vectors @ rz.T @ rx.T
    return rotated[:, :2]


def local_neighbor_vectors(structure: Structure, count: int = 6) -> list[tuple[np.ndarray, float]]:
    rows: list[tuple[np.ndarray, float]] = []
    h_frac = structure.frac[-1]
    for zr_frac in structure.frac[:-1]:
        distance, vector, _ = minimum_image(zr_frac - h_frac, structure.lattice)
        rows.append((vector, distance))
    rows.sort(key=lambda item: item[1])
    return rows[:count]


def draw_transition_structure(
    axis,
    result: PathResult,
    image_index: int,
) -> None:
    neighbors = local_neighbor_vectors(result.structures[image_index], count=6)
    vectors = np.asarray([row[0] for row in neighbors])
    projected = camera_project(vectors)
    distances = [row[1] for row in neighbors]
    bonded = [rank for rank, value in enumerate(distances) if value <= COORDINATION_CUTOFF_A]
    if image_index == result.saddle_index:
        bonded = [0, 1, 2]
    for rank, point in enumerate(projected):
        if rank not in bonded:
            continue
        axis.plot([0, point[0]], [0, point[1]], color=LIGHT_NEUTRAL, lw=1.5, zorder=1)
        midpoint = point * 0.54
        axis.text(
            midpoint[0],
            midpoint[1],
            f"{distances[rank]:.3f}",
            fontsize=9,
            ha="center",
            va="center",
            bbox={"boxstyle": "round,pad=0.12", "fc": "white", "ec": "none", "alpha": 0.92},
        )
    axis.scatter(
        projected[:, 0],
        projected[:, 1],
        s=190,
        color="#8D99A6",
        edgecolors="white",
        linewidths=0.9,
        zorder=2,
    )
    axis.scatter([0], [0], s=115, color="#D1495B", edgecolors="white", linewidths=0.9, zorder=3)
    direction_index = min(image_index + 1, 5) if image_index < 5 else image_index - 1
    direction = minimum_image_vector(
        result.structures[direction_index].frac[-1] - result.structures[image_index].frac[-1],
        result.structures[image_index].lattice,
    )
    if image_index == 5:
        direction = -direction
    arrow = camera_project(np.asarray([direction]))[0]
    arrow_norm = np.linalg.norm(arrow)
    if arrow_norm > 0:
        arrow = arrow / arrow_norm * 0.78
        axis.annotate(
            "",
            xy=(arrow[0], arrow[1]),
            xytext=(0, 0),
            arrowprops={"arrowstyle": "->", "color": path_color(result), "lw": 2.0},
            zorder=4,
        )
    if image_index == 0:
        state = "initial"
    elif image_index == result.saddle_index:
        state = "transition state"
    else:
        state = "final"
    coordination = result.geometry_rows[image_index]["coordination_2p5a"]
    axis.set_title(
        f"{path_display_name(result)} | image {image_index:02d} | {state} | CN = {coordination}"
    )
    axis.set_xlim(-4.05, 4.05)
    axis.set_ylim(-4.05, 4.05)
    axis.set_aspect("equal")
    axis.axis("off")


def plot_transition_structure(
    plt,
    result: PathResult,
    image_index: int,
    output: Path,
) -> None:
    figure, axis = plt.subplots(figsize=(7.2, 7.2), constrained_layout=True)
    draw_transition_structure(axis, result, image_index)
    save_figure(plt, figure, output)


def plot_literature_comparison(plt, results: Sequence[PathResult], output: Path) -> None:
    rows = build_literature_rows(results)
    figure, axis = plt.subplots(figsize=(10.4, 6.3), constrained_layout=True)
    y_centers = {path: float(len(LITERATURE_PATHS) - index - 1) for index, path in enumerate(LITERATURE_PATHS)}
    literature_by_path = {
        path: [
            float(row["barrier_eV"])
            for row in rows
            if row["path"] == path and row["source"] != "This work"
        ]
        for path in LITERATURE_PATHS
    }
    for index, path in enumerate(LITERATURE_PATHS):
        center = y_centers[path]
        low = min(literature_by_path[path])
        high = max(literature_by_path[path])
        axis.plot(
            [low, high],
            [center, center],
            color=LIGHT_NEUTRAL,
            lw=5,
            solid_capstyle="round",
            label="Literature min-max" if index == 0 else None,
            zorder=1,
        )
        axis.scatter(
            [low, high],
            [center, center],
            marker="|",
            s=150,
            color=NEUTRAL,
            linewidths=1.2,
            zorder=2,
        )

    source_styles = {
        "Andolina": (0.18, "o", "#4E79A7", True, "Andolina 2022 (direct)"),
        "Zhang": (0.06, "s", "#F28E2B", True, "Zhang 2017 (direct)"),
        "Domain": (-0.06, "^", "#59A14F", False, "Domain 2002 (secondary)"),
        "Wimmer": (-0.18, "D", "#B07AA1", False, "Wimmer 2020 (secondary)"),
    }
    for source, (offset, marker, color, filled, label) in source_styles.items():
        source_rows = [row for row in rows if row["source"] == source]
        axis.scatter(
            [float(row["barrier_eV"]) for row in source_rows],
            [y_centers[str(row["path"])] + offset for row in source_rows],
            s=78,
            marker=marker,
            facecolors=color if filled else "white",
            edgecolors=color,
            linewidths=1.5,
            label=label,
            zorder=3,
        )

    current_rows = [row for row in rows if row["source"] == "This work"]
    axis.scatter(
        [float(row["barrier_eV"]) for row in current_rows],
        [y_centers[str(row["path"])] for row in current_rows],
        s=165,
        marker="*",
        facecolors=TO_COLOR,
        edgecolors=LIMIT_COLOR,
        linewidths=0.8,
        label="This work (internal)",
        zorder=5,
    )
    for row in current_rows:
        value = float(row["barrier_eV"])
        axis.annotate(
            f"{value:.3f}",
            (value, y_centers[str(row["path"])]),
            xytext=(8, 8),
            textcoords="offset points",
            color=TO_COLOR,
            fontweight="bold",
        )

    y_ticks = [y_centers[path] for path in LITERATURE_PATHS]
    axis.set_yticks(y_ticks, LITERATURE_PATHS)
    axis.set_ylim(-0.42, 3.42)
    axis.set_xlim(0.075, 0.49)
    axis.set_xlabel("Electronic barrier (eV)")
    axis.set_title("Zr-H migration barriers | current CI-NEB and literature")
    axis.grid(axis="x", color=GRID, linewidth=0.8)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, 1.20), ncol=3)
    save_figure(plt, figure, output)


def plot_energy_profiles_composite(
    plt,
    results: Sequence[PathResult],
    output: Path,
) -> None:
    figure, axes = plt.subplots(
        1,
        2,
        figsize=(15.6, 6.4),
        constrained_layout=True,
    )
    for axis, result in zip(axes, results):
        draw_energy_profile(axis, result)
    save_figure(plt, figure, output)


def plot_force_convergence_composite(
    plt,
    results: Sequence[PathResult],
    output: Path,
) -> None:
    figure, axes = plt.subplots(
        2,
        2,
        figsize=(15.6, 11.2),
        constrained_layout=True,
    )
    for column, result in enumerate(results):
        draw_force_convergence(axes[0, column], result)
        draw_force_tail(axes[1, column], result)
    save_figure(plt, figure, output)


def plot_coordination_composite(
    plt,
    results: Sequence[PathResult],
    output: Path,
) -> None:
    figure, axes = plt.subplots(
        1,
        2,
        figsize=(16.5, 6.8),
        sharey=True,
        constrained_layout=True,
    )
    for axis, result in zip(axes, results):
        draw_coordination(axis, result, show_legend=False)
    handles, labels = axes[0].get_legend_handles_labels()
    figure.legend(
        handles,
        labels,
        loc="lower center",
        bbox_to_anchor=(0.5, -0.03),
        ncol=7,
        frameon=False,
    )
    save_figure(plt, figure, output)


def plot_transition_structures_composite(
    plt,
    results: Sequence[PathResult],
    output: Path,
) -> None:
    figure, axes = plt.subplots(
        2,
        3,
        figsize=(14.4, 9.6),
        constrained_layout=True,
    )
    for row, result in enumerate(results):
        image_indices = (0, result.saddle_index, 5)
        for column, image_index in enumerate(image_indices):
            draw_transition_structure(axes[row, column], result, image_index)
    save_figure(plt, figure, output)


def plot_path_geometry_composite(
    plt,
    results: Sequence[PathResult],
    output: Path,
) -> None:
    figure, axes = plt.subplots(
        2,
        3,
        figsize=(15.6, 10.2),
        constrained_layout=True,
    )
    for row, result in enumerate(results):
        draw_path_steps(axes[row, 0], result)
        draw_path_components(axes[row, 1], result)
        draw_minimum_distance(axes[row, 2], result)
    save_figure(plt, figure, output)


def render_report_pngs(plt, results: Sequence[PathResult], output: Path) -> None:
    if len(results) != 2:
        raise ValueError(f"expected TT and TO results, got {len(results)}")
    tt_result, to_result = results
    plot_energy_profile(plt, tt_result, output / STANDALONE_PNG_NAMES[0])
    plot_energy_profile(plt, to_result, output / STANDALONE_PNG_NAMES[1])
    plot_force_convergence(plt, tt_result, output / STANDALONE_PNG_NAMES[2])
    plot_force_tail(plt, tt_result, output / STANDALONE_PNG_NAMES[3])
    plot_force_convergence(plt, to_result, output / STANDALONE_PNG_NAMES[4])
    plot_force_tail(plt, to_result, output / STANDALONE_PNG_NAMES[5])
    plot_coordination(plt, tt_result, output / STANDALONE_PNG_NAMES[6])
    plot_coordination(plt, to_result, output / STANDALONE_PNG_NAMES[7])
    plot_transition_structure(plt, tt_result, 0, output / STANDALONE_PNG_NAMES[8])
    plot_transition_structure(
        plt, tt_result, tt_result.saddle_index, output / STANDALONE_PNG_NAMES[9]
    )
    plot_transition_structure(plt, tt_result, 5, output / STANDALONE_PNG_NAMES[10])
    plot_transition_structure(plt, to_result, 0, output / STANDALONE_PNG_NAMES[11])
    plot_transition_structure(
        plt, to_result, to_result.saddle_index, output / STANDALONE_PNG_NAMES[12]
    )
    plot_transition_structure(plt, to_result, 5, output / STANDALONE_PNG_NAMES[13])
    plot_path_steps(plt, tt_result, output / STANDALONE_PNG_NAMES[14])
    plot_path_steps(plt, to_result, output / STANDALONE_PNG_NAMES[15])
    plot_path_components(plt, tt_result, output / STANDALONE_PNG_NAMES[16])
    plot_path_components(plt, to_result, output / STANDALONE_PNG_NAMES[17])
    plot_minimum_distance(plt, tt_result, output / STANDALONE_PNG_NAMES[18])
    plot_minimum_distance(plt, to_result, output / STANDALONE_PNG_NAMES[19])
    plot_literature_comparison(plt, results, output / STANDALONE_PNG_NAMES[20])

    plot_energy_profiles_composite(plt, results, output / COMPOSITE_PNG_NAMES[0])
    plot_force_convergence_composite(plt, results, output / COMPOSITE_PNG_NAMES[1])
    plot_coordination_composite(plt, results, output / COMPOSITE_PNG_NAMES[2])
    plot_transition_structures_composite(plt, results, output / COMPOSITE_PNG_NAMES[3])
    plot_path_geometry_composite(plt, results, output / COMPOSITE_PNG_NAMES[4])


def publish_stage(stage: Path, output: Path) -> None:
    staged_files = {source.name for source in stage.iterdir() if source.is_file()}
    missing_images = sorted(set(REPORT_PNG_NAMES) - staged_files)
    if missing_images:
        raise ValueError(f"report asset stage lacks PNG files: {', '.join(missing_images)}")
    missing_tables = sorted(set(REPORT_TSV_NAMES) - staged_files)
    if missing_tables:
        raise ValueError(f"report asset stage lacks TSV files: {', '.join(missing_tables)}")
    for filename in REPORT_PNG_NAMES:
        validate_png_asset(stage / filename)
    for filename in REPORT_TSV_NAMES:
        require_file(stage / filename)
    output.mkdir(parents=True, exist_ok=True)
    for source in stage.iterdir():
        if source.is_dir():
            continue
        target = output / source.name
        if target.exists() and target.is_dir():
            raise ValueError(f"refusing to replace a directory with a report asset: {target}")
        temporary = output / f".{source.name}.tmp"
        shutil.copy2(source, temporary)
        temporary.replace(target)
    for filename in LEGACY_PNG_NAMES:
        target = output / filename
        if target.exists() and target.is_dir():
            raise ValueError(f"refusing to remove a legacy asset directory: {target}")
        if target.is_file():
            target.unlink()


def main() -> int:
    args = parse_args()
    output = args.output_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    tt_info = validate_archive(
        args.tt_archive,
        expected_prefixes=(TT_FINAL_ROOT,),
        required_members=final_required_members(TT_FINAL_ROOT),
    )
    to_info = validate_archive(
        args.to_archive,
        expected_prefixes=(TO_FINAL_ROOT,),
        required_members=final_required_members(TO_FINAL_ROOT),
    )
    history_info = validate_archive(
        args.to_history_archive,
        expected_prefixes=(TO_CI01_ROOT, TO_CI02_ROOT),
        required_members=history_required_members(),
    )

    temporary_parent = args.tt_archive.resolve().parent
    with report_temporary_directory("build_neb_report_", temporary_parent) as temporary_root:
        workflow_root = temporary_root / "workflow"
        copy_pre_stage(args.tt_pre_result, workflow_root / "03_neb/01_tt_c")
        copy_pre_stage(args.to_pre_result, workflow_root / "03_neb/02_to")
        secure_extract(tt_info.path, workflow_root)
        secure_extract(history_info.path, workflow_root)
        secure_extract(to_info.path, workflow_root)

        official_output = temporary_root / "official-analysis"
        analyzer = Path(__file__).with_name("analyze_neb.py")
        official_stdout = run_official_analysis(workflow_root, official_output, analyzer)

        tt_final = workflow_root / TT_FINAL_ROOT
        to_final = workflow_root / TO_FINAL_ROOT
        tt_stages = [
            load_stage("TT_c", workflow_root / "03_neb/01_tt_c", "pre_neb"),
            load_stage("TT_c", tt_final, "ci_neb"),
        ]
        to_stages = [
            load_stage("TO", workflow_root / "03_neb/02_to", "pre_neb"),
            load_stage("TO", workflow_root / TO_CI01_ROOT, "ci_neb"),
            load_stage("TO", to_final, "ci_neb"),
        ]
        validate_final_electronic_convergence(tt_final, tt_stages[-1].iterations)
        validate_final_electronic_convergence(to_final, to_stages[-1].iterations)
        validate_final_logs(tt_final)
        validate_final_logs(to_final)

        results = [
            build_path_result(
                "TT_c", tt_final, tt_stages, official_output / "neb_profile.tsv"
            ),
            build_path_result(
                "TO", to_final, to_stages, official_output / "neb_profile.tsv"
            ),
        ]
        for result in results:
            validate_regressions(result)
        assert_close(
            "TO-OT endpoint self-check",
            results[1].barrier_forward - results[1].barrier_reverse,
            EXPECTED_ENDPOINT_DELTA_EV,
            5.0e-8,
        )

        summaries, images, neighbors, barriers = build_output_rows(results)
        literature = build_literature_rows(results)
        asset_stage = temporary_root / "assets"
        asset_stage.mkdir()
        write_tsv_outputs(
            asset_stage,
            official_output,
            summaries,
            images,
            neighbors,
            barriers,
            literature,
        )

        plt = configure_matplotlib(temporary_root / "matplotlib-cache")
        render_report_pngs(plt, results, asset_stage)

        publish_stage(asset_stage, output)

    print("NEB report asset build PASS")
    print(official_stdout.strip())
    print(f"TT archive SHA-256: {tt_info.sha256}")
    print(f"TO archive SHA-256: {to_info.sha256}")
    print(f"TO history SHA-256: {history_info.sha256}")
    print(f"OUTPUT_DIR={output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
