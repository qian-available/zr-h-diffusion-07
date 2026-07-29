#!/usr/bin/env python3
"""Shared, POTCAR-free helpers for the 07 Zr96H NEB workflow."""

from __future__ import annotations

from dataclasses import dataclass
import csv
import hashlib
import itertools
import math
from pathlib import Path
import re
from typing import Iterable, Sequence

import numpy as np


SHIFTS = np.asarray(list(itertools.product((-1, 0, 1), repeat=3)), dtype=float)
NEB_FORCE_PATTERN = re.compile(
    r"FORCES:\s*max atom, RMS\s*=*\s*([+\-.0-9Ee]+)\s+([+\-.0-9Ee]+)"
)


@dataclass(frozen=True)
class Structure:
    comment: str
    lattice: np.ndarray
    elements: tuple[str, ...]
    counts: tuple[int, ...]
    frac: np.ndarray

    @property
    def labels(self) -> list[str]:
        return [element for element, count in zip(self.elements, self.counts) for _ in range(count)]

    @property
    def atom_count(self) -> int:
        return int(sum(self.counts))


@dataclass(frozen=True)
class OutcarSummary:
    nions: int
    e0_ev: float
    final_max_force_ev_a: float
    normal: bool
    electronic: bool
    ionic: bool


def neb_projected_force_history(
    stage_directory: Path,
    image_names: Sequence[str] = ("01", "02", "03", "04"),
) -> list[tuple[int, float, float]]:
    """Return the image-global VTST projected-force history.

    VTST writes ``FORCES: max atom, RMS`` to each intermediate image OUTCAR,
    not necessarily to the root stdout stream.  The convergence metric is the
    largest per-image maximum at each synchronized ionic iteration.
    """
    per_image: list[list[tuple[float, float]]] = []
    for image in image_names:
        outcar = require_file(stage_directory / image / "OUTCAR")
        matches = [
            (float(maximum), float(rms))
            for maximum, rms in NEB_FORCE_PATTERN.findall(
                outcar.read_text(encoding="utf-8", errors="replace")
            )
        ]
        if not matches:
            raise ValueError(f"no VTST projected-force history found: {outcar}")
        if any(not math.isfinite(value) for pair in matches for value in pair):
            raise ValueError(f"non-finite VTST projected force found: {outcar}")
        per_image.append(matches)
    lengths = {len(history) for history in per_image}
    if len(lengths) != 1:
        raise ValueError(
            f"intermediate-image force histories have different lengths in {stage_directory}: "
            f"{sorted(lengths)}"
        )
    rows: list[tuple[int, float, float]] = []
    for index, image_rows in enumerate(zip(*per_image), start=1):
        maximum = max(pair[0] for pair in image_rows)
        combined_rms = math.sqrt(sum(pair[1] ** 2 for pair in image_rows) / len(image_rows))
        rows.append((index, maximum, combined_rms))
    return rows


def require_file(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"required non-empty file is missing: {path}")
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with require_file(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_poscar(path: Path) -> Structure:
    lines = require_file(path).read_text(encoding="utf-8", errors="strict").splitlines()
    if len(lines) < 8:
        raise ValueError(f"incomplete POSCAR/CONTCAR: {path}")
    scale = float(lines[1].split()[0])
    if scale <= 0:
        raise ValueError(f"only positive POSCAR scale is supported: {path}")
    lattice = np.asarray(
        [[float(value) for value in lines[index].split()[:3]] for index in range(2, 5)],
        dtype=float,
    ) * scale
    elements = tuple(lines[5].split())
    counts = tuple(int(value) for value in lines[6].split())
    if len(elements) != len(counts):
        raise ValueError(f"element/count mismatch in {path}")
    index = 7
    if lines[index].strip().lower().startswith("s"):
        index += 1
    mode = lines[index].strip().lower()
    index += 1
    nions = sum(counts)
    if len(lines) < index + nions:
        raise ValueError(f"coordinate block is incomplete in {path}")
    coordinates = np.asarray(
        [[float(value) for value in lines[index + atom].split()[:3]] for atom in range(nions)],
        dtype=float,
    )
    if mode.startswith(("c", "k")):
        coordinates = coordinates * scale @ np.linalg.inv(lattice)
    elif not mode.startswith("d"):
        raise ValueError(f"unknown coordinate mode in {path}: {mode}")
    return Structure(
        comment=lines[0].strip(),
        lattice=lattice,
        elements=elements,
        counts=counts,
        frac=coordinates % 1.0,
    )


def poscar_text(structure: Structure, comment: str | None = None) -> str:
    lines = [comment or structure.comment, "1.0"]
    lines.extend("  " + "  ".join(f"{value:20.14f}" for value in row) for row in structure.lattice)
    lines.append("  " + "  ".join(structure.elements))
    lines.append("  " + "  ".join(str(value) for value in structure.counts))
    lines.append("Direct")
    lines.extend("  " + "  ".join(f"{value % 1.0:20.14f}" for value in row) for row in structure.frac)
    return "\n".join(lines) + "\n"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_write_tsv(path: Path, fields: Sequence[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def minimum_image(delta_frac: np.ndarray, lattice: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    centered = np.asarray(delta_frac, dtype=float) - np.rint(delta_frac)
    candidates = centered[None, :] + SHIFTS
    vectors = candidates @ lattice
    distances = np.linalg.norm(vectors, axis=1)
    index = int(np.argmin(distances))
    return float(distances[index]), vectors[index], candidates[index]


def minimum_image_distances(deltas_frac: np.ndarray, lattice: np.ndarray) -> np.ndarray:
    centered = np.asarray(deltas_frac, dtype=float) - np.rint(deltas_frac)
    candidates = centered[:, None, :] + SHIFTS[None, :, :]
    vectors = candidates @ lattice
    return np.linalg.norm(vectors, axis=2).min(axis=1)


def minimum_pair_distance(structure: Structure) -> float:
    first, second = np.triu_indices(structure.atom_count, k=1)
    return float(minimum_image_distances(structure.frac[second] - structure.frac[first], structure.lattice).min())


def parse_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in require_file(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def known_vasp_images_hdf5_shutdown(stage_directory: Path) -> bool:
    """Recognize the VASP <6.4.1 IMAGES/HDF5 failure after valid outputs.

    VASP may return exit code 1 after an otherwise complete NEB run when it
    tries to finalize ``vaspout.h5``.  Acceptance still requires callers to
    validate every OUTCAR and the final projected force independently.
    """
    status_path = stage_directory / ".run_status"
    stderr_path = stage_directory / "vasp.stderr"
    if not status_path.is_file() or not stderr_path.is_file():
        return False
    status = parse_key_values(status_path)
    if status.get("vasp_exit") != "1":
        return False
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    return (
        re.search(r"internal error in:\s*vhdf5\.F", stderr, re.IGNORECASE) is not None
        and re.search(r"HDF5 call .* produced error:\s*29\b", stderr, re.IGNORECASE) is not None
    )


def parse_force_blocks(lines: Sequence[str], nions: int) -> list[float]:
    maxima: list[float] = []
    index = 0
    while index < len(lines):
        if "TOTAL-FORCE (eV/Angst)" not in lines[index]:
            index += 1
            continue
        index += 1
        forces: list[list[float]] = []
        while index < len(lines) and len(forces) < nions:
            fields = lines[index].split()
            if len(fields) >= 6:
                try:
                    forces.append([float(fields[3]), float(fields[4]), float(fields[5])])
                except ValueError:
                    pass
            index += 1
        if len(forces) == nions:
            maxima.append(float(np.linalg.norm(np.asarray(forces), axis=1).max()))
    return maxima


def parse_outcar(path: Path) -> OutcarSummary:
    text = require_file(path).read_text(encoding="utf-8", errors="replace")
    nions_matches = re.findall(r"NIONS\s*=\s*(\d+)", text)
    energy_matches = re.findall(r"energy\(sigma->0\)\s*=\s*([+\-.0-9Ee]+)", text)
    if not nions_matches or not energy_matches:
        raise ValueError(f"OUTCAR lacks NIONS or energy(sigma->0): {path}")
    nions = int(nions_matches[-1])
    forces = parse_force_blocks(text.splitlines(), nions)
    if not forces:
        raise ValueError(f"OUTCAR lacks a complete force block: {path}")
    return OutcarSummary(
        nions=nions,
        e0_ev=float(energy_matches[-1]),
        final_max_force_ev_a=forces[-1],
        normal="General timing and accounting" in text,
        electronic="aborting loop because EDIFF is reached" in text,
        ionic="reached required accuracy" in text,
    )


def require_same_topology(first: Structure, second: Structure, label: str) -> None:
    if first.elements != second.elements or first.counts != second.counts:
        raise ValueError(f"element order/count mismatch for {label}")
    if first.atom_count != second.atom_count:
        raise ValueError(f"atom-count mismatch for {label}")
    if not np.allclose(first.lattice, second.lattice, rtol=0.0, atol=1e-8):
        raise ValueError(f"lattice mismatch for {label}")


def h_neighbors(structure: Structure) -> list[float]:
    if structure.elements != ("Zr", "H") or structure.counts != (96, 1):
        raise ValueError("expected Zr H / 96 1 structure")
    h_frac = structure.frac[-1]
    return sorted(minimum_image(frac - h_frac, structure.lattice)[0] for frac in structure.frac[:-1])
