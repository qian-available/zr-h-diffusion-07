#!/usr/bin/env python3
"""Validate and analyze the downloaded Zr96-H initial VASP calculations.

The script is analysis-only: it reads an explicitly supplied downloaded result
tree and writes small TSV tables plus raster PNG/PDF figures.  It never reads
POTCAR and never prepares or launches a VASP calculation.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import itertools
import math
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont


COORDINATION_CUTOFF_A = 2.5
FORCE_LIMIT_EV_A = 0.01
EXPECTED_ARCHIVE_SHA256 = (
    "03876d4375003ceabe67ea7df84903e0f43a6ad1d15b179c302ecc1e447e6bc9"
)

CASE_SPECS = {
    "Zr96": {
        "path": Path("01_zr96_static/retry_dcu_01"),
        "nions": 96,
        "steps": 1,
        "kind": "static",
        "expected_e0": -818.11976349,
        "expected_force": 0.0,
    },
    "H2": {
        "path": Path("02_h2_relax"),
        "nions": 2,
        "steps": 2,
        "kind": "relax",
        "expected_e0": -6.76804846,
        "expected_force": 0.00328400,
    },
    "T": {
        "path": Path("03_t_relax/retry_dcu_01"),
        "nions": 97,
        "steps": 13,
        "kind": "relax",
        "expected_e0": -821.95359478,
        "expected_force": 0.00874278,
    },
    "O": {
        "path": Path("04_o_relax/retry_dcu_01"),
        "nions": 97,
        "steps": 12,
        "kind": "relax",
        "expected_e0": -821.89069078,
        "expected_force": 0.00420013,
    },
}

COLORS = {
    "T": (28, 113, 216),
    "O": (230, 126, 34),
    "Zr": (78, 139, 164),
    "H": (205, 43, 49),
    "H_initial": (242, 177, 181),
    "gray": (92, 101, 112),
    "grid": (218, 223, 230),
    "dark": (32, 38, 46),
    "green": (45, 150, 85),
    "axis_a": (139, 73, 156),
    "axis_b": (45, 150, 85),
    "axis_c": (28, 113, 216),
}


@dataclass
class Structure:
    lattice: np.ndarray
    frac: np.ndarray
    labels: list[str]


@dataclass
class CaseData:
    name: str
    directory: Path
    kind: str
    poscar: Structure
    contcar: Structure
    nions: int
    e0_ev: float
    nkpts: int
    elapsed_s: float
    ionic_steps: int
    max_forces: list[float]
    trajectory_e0: list[float]
    frames: list[np.ndarray]
    job_id: str
    status: str


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-root",
        type=Path,
        required=True,
        help="Downloaded 07_h_diffusion_quickstart directory or its 01_initial directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_root / "01_initial" / "analysis",
        help="Directory for generated TSV and figure files",
    )
    parser.add_argument("--archive", type=Path, help="Optional downloaded tar.gz for SHA-256 verification")
    parser.add_argument("--checksum", type=Path, help="Optional sha256 text file paired with --archive")
    return parser.parse_args()


def require_file(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"required non-empty file is missing: {path}")
    return path


def normalize_initial_root(path: Path) -> Path:
    root = path.resolve()
    if (root / "01_initial").is_dir():
        root = root / "01_initial"
    if root.name != "01_initial" or not root.is_dir():
        raise ValueError(
            "--results-root must be the downloaded 07_h_diffusion_quickstart "
            "directory or its 01_initial directory"
        )
    return root


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_archive(archive: Path | None, checksum: Path | None) -> str:
    if (archive is None) != (checksum is None):
        raise ValueError("--archive and --checksum must be provided together")
    if archive is None or checksum is None:
        return "not_checked"
    require_file(archive)
    require_file(checksum)
    recorded = checksum.read_text(encoding="utf-8").split()[0].lower()
    actual = sha256(archive)
    if actual != recorded or actual != EXPECTED_ARCHIVE_SHA256:
        raise ValueError(
            f"archive SHA-256 mismatch: actual={actual}, recorded={recorded}, "
            f"expected={EXPECTED_ARCHIVE_SHA256}"
        )
    return actual


def parse_poscar(path: Path) -> Structure:
    lines = require_file(path).read_text(encoding="utf-8").splitlines()
    if len(lines) < 8:
        raise ValueError(f"incomplete POSCAR/CONTCAR: {path}")
    scale = float(lines[1].split()[0])
    if scale <= 0:
        raise ValueError(f"only positive POSCAR scale is supported: {path}")
    lattice = np.array(
        [[float(value) for value in lines[index].split()[:3]] for index in range(2, 5)],
        dtype=float,
    ) * scale
    elements = lines[5].split()
    counts = [int(value) for value in lines[6].split()]
    if len(elements) != len(counts):
        raise ValueError(f"element/count mismatch in {path}")
    line_index = 7
    if lines[line_index].strip().lower().startswith("s"):
        line_index += 1
    mode = lines[line_index].strip().lower()
    line_index += 1
    nions = sum(counts)
    coordinates = np.array(
        [
            [float(value) for value in lines[line_index + atom].split()[:3]]
            for atom in range(nions)
        ],
        dtype=float,
    )
    if mode.startswith(("c", "k")):
        coordinates = coordinates * scale @ np.linalg.inv(lattice)
    elif not mode.startswith("d"):
        raise ValueError(f"unknown coordinate mode in {path}: {mode}")
    labels = [element for element, count in zip(elements, counts) for _ in range(count)]
    return Structure(lattice=lattice, frac=coordinates, labels=labels)


def minimum_image(delta_frac: np.ndarray, lattice: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    centered = np.asarray(delta_frac, dtype=float) - np.rint(delta_frac)
    best: tuple[float, np.ndarray, np.ndarray] | None = None
    for shift in itertools.product((-1, 0, 1), repeat=3):
        fractional = centered + np.array(shift, dtype=float)
        vector = fractional @ lattice
        distance = float(np.linalg.norm(vector))
        if best is None or distance < best[0]:
            best = distance, vector, fractional
    if best is None:
        raise RuntimeError("minimum-image search produced no candidates")
    return best


def parse_key_value(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in require_file(path).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def last_regex(text: str, pattern: str, description: str, path: Path) -> re.Match[str]:
    matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
    if not matches:
        raise ValueError(f"missing {description} in {path}")
    return matches[-1]


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


def parse_oszicar(path: Path) -> list[float]:
    values: list[float] = []
    pattern = re.compile(r"^\s*\d+\s+F=.*?E0=\s*([+\-.0-9Ee]+)")
    for line in require_file(path).read_text(encoding="utf-8").splitlines():
        match = pattern.search(line)
        if match:
            values.append(float(match.group(1)))
    if not values:
        raise ValueError(f"no ionic E0 records found in {path}")
    return values


def parse_xdatcar(path: Path, nions: int) -> tuple[np.ndarray, list[np.ndarray]]:
    lines = require_file(path).read_text(encoding="utf-8").splitlines()
    scale = float(lines[1].split()[0])
    lattice = np.array(
        [[float(value) for value in lines[index].split()[:3]] for index in range(2, 5)],
        dtype=float,
    ) * scale
    frames: list[np.ndarray] = []
    index = 7
    while index < len(lines):
        if lines[index].strip().lower().startswith("direct configuration"):
            frame = np.array(
                [
                    [float(value) for value in lines[index + 1 + atom].split()[:3]]
                    for atom in range(nions)
                ],
                dtype=float,
            )
            frames.append(frame)
            index += nions + 1
        else:
            index += 1
    if not frames:
        raise ValueError(f"no configurations found in {path}")
    return lattice, frames


def load_case(initial_root: Path, name: str, spec: dict[str, object]) -> CaseData:
    directory = initial_root / Path(spec["path"])
    required_names = ["POSCAR", "CONTCAR", "OUTCAR", "OSZICAR", "XDATCAR", ".run_status"]
    for filename in required_names:
        require_file(directory / filename)

    poscar = parse_poscar(directory / "POSCAR")
    contcar = parse_poscar(directory / "CONTCAR")
    if poscar.labels != contcar.labels:
        raise ValueError(f"atom labels/order changed between POSCAR and CONTCAR for {name}")
    if not np.allclose(poscar.lattice, contcar.lattice, atol=1e-8):
        raise ValueError(f"lattice changed in fixed-cell calculation for {name}")

    outcar_path = directory / "OUTCAR"
    outcar_text = outcar_path.read_text(encoding="utf-8", errors="replace")
    outcar_lines = outcar_text.splitlines()
    if "General timing and accounting" not in outcar_text:
        raise ValueError(f"VASP did not terminate normally for {name}")
    if "aborting loop because EDIFF is reached" not in outcar_text:
        raise ValueError(f"electronic convergence marker missing for {name}")
    if spec["kind"] == "relax" and "reached required accuracy" not in outcar_text:
        raise ValueError(f"ionic convergence marker missing for {name}")

    nions = int(last_regex(outcar_text, r"NIONS\s*=\s*(\d+)", "NIONS", outcar_path).group(1))
    e0_ev = float(
        last_regex(
            outcar_text,
            r"energy\(sigma->0\)\s*=\s*([+\-.0-9Ee]+)",
            "energy(sigma->0)",
            outcar_path,
        ).group(1)
    )
    nkpts = int(
        last_regex(outcar_text, r"k-points\s+NKPTS\s*=\s*(\d+)", "NKPTS", outcar_path).group(1)
    )
    elapsed_s = float(
        last_regex(
            outcar_text,
            r"Elapsed time \(sec\):\s*([+\-.0-9Ee]+)",
            "elapsed time",
            outcar_path,
        ).group(1)
    )
    max_forces = parse_force_blocks(outcar_lines, nions)
    trajectory_e0 = parse_oszicar(directory / "OSZICAR")
    xdat_lattice, frames = parse_xdatcar(directory / "XDATCAR", nions)
    if not np.allclose(xdat_lattice, poscar.lattice, atol=1e-5):
        raise ValueError(f"XDATCAR lattice differs from POSCAR for {name}")

    run_status = parse_key_value(directory / ".run_status")
    if run_status.get("vasp_exit") != "0" or run_status.get("normal_termination") != "yes":
        raise ValueError(f".run_status does not confirm a successful VASP exit for {name}")
    if run_status.get("electronic_convergence") != "yes":
        raise ValueError(f".run_status does not confirm electronic convergence for {name}")
    if spec["kind"] == "relax" and name != "H2" and run_status.get("ionic_convergence") != "yes":
        raise ValueError(f".run_status does not confirm ionic convergence for {name}")

    expected_nions = int(spec["nions"])
    expected_steps = int(spec["steps"])
    if nions != expected_nions or len(poscar.labels) != expected_nions:
        raise ValueError(f"unexpected atom count for {name}: {nions}, expected {expected_nions}")
    if len(trajectory_e0) != expected_steps or len(max_forces) != expected_steps or len(frames) != expected_steps:
        raise ValueError(
            f"unexpected trajectory length for {name}: E0={len(trajectory_e0)}, "
            f"forces={len(max_forces)}, frames={len(frames)}, expected={expected_steps}"
        )
    if abs(e0_ev - float(spec["expected_e0"])) > 5e-8:
        raise ValueError(f"E0 regression failed for {name}: {e0_ev}")
    if abs(max_forces[-1] - float(spec["expected_force"])) > 5e-7:
        raise ValueError(f"final-force regression failed for {name}: {max_forces[-1]}")
    if spec["kind"] == "relax" and max_forces[-1] > FORCE_LIMIT_EV_A + 1e-6:
        raise ValueError(f"force limit failed for {name}: {max_forces[-1]}")

    return CaseData(
        name=name,
        directory=directory,
        kind=str(spec["kind"]),
        poscar=poscar,
        contcar=contcar,
        nions=nions,
        e0_ev=e0_ev,
        nkpts=nkpts,
        elapsed_s=elapsed_s,
        ionic_steps=len(trajectory_e0),
        max_forces=max_forces,
        trajectory_e0=trajectory_e0,
        frames=frames,
        job_id=run_status.get("slurm_job_id", ""),
        status="PASS",
    )


def atomic_write_tsv(path: Path, fields: Sequence[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def format_float(value: float | None, digits: int = 8) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def atom_displacements(case: CaseData) -> list[float]:
    return [
        minimum_image(final - initial, case.poscar.lattice)[0]
        for initial, final in zip(case.poscar.frac, case.contcar.frac)
    ]


def h_neighbors(case: CaseData) -> list[dict[str, object]]:
    if case.contcar.labels[-1] != "H" or case.contcar.labels.count("H") != 1:
        raise ValueError(f"{case.name} must have exactly one H atom in the final position")
    h_frac = case.contcar.frac[-1]
    neighbors: list[dict[str, object]] = []
    for index, (label, frac) in enumerate(zip(case.contcar.labels[:-1], case.contcar.frac[:-1]), start=1):
        if label != "Zr":
            raise ValueError(f"unexpected non-Zr host atom in {case.name}")
        distance, vector, _ = minimum_image(frac - h_frac, case.contcar.lattice)
        neighbors.append({"zr_atom_index": index, "distance_a": distance, "vector": vector})
    neighbors.sort(key=lambda item: float(item["distance_a"]))
    return neighbors


def h_displacement(case: CaseData, frac: np.ndarray | None = None) -> float:
    target = case.contcar.frac[-1] if frac is None else frac
    return minimum_image(target - case.poscar.frac[-1], case.poscar.lattice)[0]


def h2_bond(structure: Structure) -> float:
    if structure.labels != ["H", "H"]:
        raise ValueError("H2 structure does not contain exactly H H")
    return minimum_image(structure.frac[1] - structure.frac[0], structure.lattice)[0]


def prepare_analysis(cases: dict[str, CaseData]) -> dict[str, object]:
    if cases["T"].contcar.labels != cases["O"].contcar.labels:
        raise ValueError("T/O element order differs")
    if not np.allclose(cases["T"].contcar.lattice, cases["O"].contcar.lattice, atol=1e-8):
        raise ValueError("T/O lattices differ")

    neighbors = {site: h_neighbors(cases[site]) for site in ("T", "O")}
    coordination = {
        site: sum(float(item["distance_a"]) <= COORDINATION_CUTOFF_A for item in neighbors[site])
        for site in ("T", "O")
    }
    if coordination != {"T": 4, "O": 6}:
        raise ValueError(f"unexpected T/O coordination counts at 2.5 A: {coordination}")

    t_disp = h_displacement(cases["T"])
    o_disp = h_displacement(cases["O"])
    site_separation = minimum_image(
        cases["O"].contcar.frac[-1] - cases["T"].contcar.frac[-1],
        cases["T"].contcar.lattice,
    )[0]
    h2_initial = h2_bond(cases["H2"].poscar)
    h2_final = h2_bond(cases["H2"].contcar)

    if abs(t_disp - 0.02505775) > 2e-6 or abs(o_disp - 0.00000669) > 2e-6:
        raise ValueError(f"H-displacement regression failed: T={t_disp}, O={o_disp}")
    if abs(site_separation - 1.98514991) > 2e-6:
        raise ValueError(f"T/O site-separation regression failed: {site_separation}")
    if abs(h2_final - 0.75039343) > 2e-6:
        raise ValueError(f"H2 bond-length regression failed: {h2_final}")

    esol_t = cases["T"].e0_ev - cases["Zr96"].e0_ev - 0.5 * cases["H2"].e0_ev
    esol_o = cases["O"].e0_ev - cases["Zr96"].e0_ev - 0.5 * cases["H2"].e0_ev
    delta_o_t = cases["O"].e0_ev - cases["T"].e0_ev
    if abs(esol_t + 0.44980706) > 5e-8 or abs(esol_o + 0.38690306) > 5e-8:
        raise ValueError(f"solution-energy regression failed: T={esol_t}, O={esol_o}")

    return {
        "neighbors": neighbors,
        "coordination": coordination,
        "h_displacements": {"T": t_disp, "O": o_disp},
        "site_separation_a": site_separation,
        "h2_initial_bond_a": h2_initial,
        "h2_final_bond_a": h2_final,
        "esol": {"T": esol_t, "O": esol_o},
        "delta_o_t_ev": delta_o_t,
    }


def write_tables(output_dir: Path, cases: dict[str, CaseData], analysis: dict[str, object]) -> None:
    metric_fields = [
        "case", "status", "job_id", "nions", "ionic_steps", "e0_ev", "max_force_ev_a",
        "elapsed_s", "nkpts", "h_initial_frac_x", "h_initial_frac_y", "h_initial_frac_z",
        "h_final_frac_x", "h_final_frac_y", "h_final_frac_z", "h_displacement_a",
        "coordination_within_2p5_a", "zr_rms_displacement_a", "zr_max_displacement_a",
    ]
    metric_rows: list[dict[str, object]] = []
    for name in ("Zr96", "H2", "T", "O"):
        case = cases[name]
        row: dict[str, object] = {
            "case": name,
            "status": case.status,
            "job_id": case.job_id,
            "nions": case.nions,
            "ionic_steps": case.ionic_steps,
            "e0_ev": format_float(case.e0_ev),
            "max_force_ev_a": format_float(case.max_forces[-1]),
            "elapsed_s": f"{case.elapsed_s:.3f}",
            "nkpts": case.nkpts,
            "h_initial_frac_x": "",
            "h_initial_frac_y": "",
            "h_initial_frac_z": "",
            "h_final_frac_x": "",
            "h_final_frac_y": "",
            "h_final_frac_z": "",
            "h_displacement_a": "",
            "coordination_within_2p5_a": "",
            "zr_rms_displacement_a": "",
            "zr_max_displacement_a": "",
        }
        if name in ("T", "O"):
            host_displacements = atom_displacements(case)[:-1]
            initial_h = case.poscar.frac[-1]
            final_h = case.contcar.frac[-1]
            row.update(
                {
                    "h_initial_frac_x": format_float(float(initial_h[0]), 10),
                    "h_initial_frac_y": format_float(float(initial_h[1]), 10),
                    "h_initial_frac_z": format_float(float(initial_h[2]), 10),
                    "h_final_frac_x": format_float(float(final_h[0]), 10),
                    "h_final_frac_y": format_float(float(final_h[1]), 10),
                    "h_final_frac_z": format_float(float(final_h[2]), 10),
                    "h_displacement_a": format_float(float(analysis["h_displacements"][name])),
                    "coordination_within_2p5_a": int(analysis["coordination"][name]),
                    "zr_rms_displacement_a": format_float(
                        float(np.sqrt(np.mean(np.square(host_displacements))))
                    ),
                    "zr_max_displacement_a": format_float(max(host_displacements)),
                }
            )
        metric_rows.append(row)
    atomic_write_tsv(output_dir / "initial_metrics.tsv", metric_fields, metric_rows)

    trajectory_fields = [
        "case", "ionic_step", "e0_ev", "delta_e_final_mev", "max_force_ev_a", "h_displacement_a"
    ]
    trajectory_rows: list[dict[str, object]] = []
    for name in ("T", "O"):
        case = cases[name]
        final_energy = case.trajectory_e0[-1]
        for step, (energy, force, frame) in enumerate(
            zip(case.trajectory_e0, case.max_forces, case.frames), start=1
        ):
            trajectory_rows.append(
                {
                    "case": name,
                    "ionic_step": step,
                    "e0_ev": format_float(energy),
                    "delta_e_final_mev": f"{1000.0 * (energy - final_energy):.6f}",
                    "max_force_ev_a": format_float(force),
                    "h_displacement_a": format_float(h_displacement(case, frame[-1])),
                }
            )
    atomic_write_tsv(output_dir / "relax_trajectory.tsv", trajectory_fields, trajectory_rows)

    neighbor_fields = ["site", "rank", "zr_atom_index", "distance_a", "within_2p5_a"]
    neighbor_rows: list[dict[str, object]] = []
    for site in ("T", "O"):
        for rank, item in enumerate(analysis["neighbors"][site], start=1):
            distance = float(item["distance_a"])
            neighbor_rows.append(
                {
                    "site": site,
                    "rank": rank,
                    "zr_atom_index": int(item["zr_atom_index"]),
                    "distance_a": format_float(distance),
                    "within_2p5_a": "yes" if distance <= COORDINATION_CUTOFF_A else "no",
                }
            )
    atomic_write_tsv(output_dir / "neighbor_distances.tsv", neighbor_fields, neighbor_rows)


def find_font(bold: bool = False) -> Path | None:
    names = ["arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"]
    roots = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")]
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    path = find_font(bold)
    if path is not None:
        return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def text_center(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, fnt: ImageFont.ImageFont, fill: tuple[int, int, int]) -> None:
    box = draw.textbbox((0, 0), text, font=fnt)
    draw.text((xy[0] - (box[2] - box[0]) / 2, xy[1] - (box[3] - box[1]) / 2), text, font=fnt, fill=fill)


def text_right(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, fnt: ImageFont.ImageFont, fill: tuple[int, int, int]) -> None:
    box = draw.textbbox((0, 0), text, font=fnt)
    draw.text((xy[0] - (box[2] - box[0]), xy[1] - (box[3] - box[1]) / 2), text, font=fnt, fill=fill)


def vertical_text(
    image: Image.Image,
    center: tuple[float, float],
    text: str,
    fnt: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    probe = ImageDraw.Draw(image)
    box = probe.textbbox((0, 0), text, font=fnt)
    width = box[2] - box[0] + 20
    height = box[3] - box[1] + 20
    label = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    label_draw = ImageDraw.Draw(label)
    label_draw.text((10 - box[0], 10 - box[1]), text, font=fnt, fill=fill + (255,))
    rotated = label.rotate(90, expand=True, resample=Image.Resampling.BICUBIC)
    image.paste(
        rotated,
        (int(center[0] - rotated.width / 2), int(center[1] - rotated.height / 2)),
        rotated,
    )


def new_canvas(width: int = 2400, height: int = 1500) -> Image.Image:
    return Image.new("RGB", (width, height), "white")


def save_figure(image: Image.Image, output_dir: Path, stem: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    png = output_dir / f"{stem}.png"
    pdf = output_dir / f"{stem}.pdf"
    png_tmp = output_dir / f".{stem}.tmp.png"
    pdf_tmp = output_dir / f".{stem}.tmp.pdf"
    image.save(png_tmp, format="PNG", dpi=(300, 300), optimize=True)
    image.save(pdf_tmp, format="PDF", resolution=300.0)
    png_tmp.replace(png)
    pdf_tmp.replace(pdf)


def panel_title(draw: ImageDraw.ImageDraw, rect: tuple[int, int, int, int], title: str, size: int = 36) -> None:
    text_center(draw, ((rect[0] + rect[2]) / 2, rect[1] + 28), title, font(size, True), COLORS["dark"])


def line_plot(
    image: Image.Image,
    rect: tuple[int, int, int, int],
    series: dict[str, Sequence[tuple[float, float]]],
    title: str,
    xlabel: str,
    ylabel: str,
    y_ticks: Sequence[float],
    log_y: bool = False,
    horizontal: float | None = None,
) -> None:
    draw = ImageDraw.Draw(image)
    panel_title(draw, rect, title)
    left, top, right, bottom = rect[0] + 145, rect[1] + 90, rect[2] - 35, rect[3] - 115
    all_x = [x for values in series.values() for x, _ in values]
    x_min, x_max = min(all_x), max(all_x)
    if x_min == x_max:
        x_max += 1.0
    y_values = list(y_ticks)
    y_min, y_max = min(y_values), max(y_values)
    if log_y:
        y_min, y_max = math.log10(y_min), math.log10(y_max)

    def px(x: float) -> float:
        return left + (x - x_min) / (x_max - x_min) * (right - left)

    def py(y: float) -> float:
        value = math.log10(y) if log_y else y
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    tick_font = font(25)
    for tick in y_ticks:
        y = py(tick)
        draw.line((left, y, right, y), fill=COLORS["grid"], width=2)
        label = f"{tick:g}"
        text_right(draw, (left - 18, y), label, tick_font, COLORS["gray"])
    x_ticks = list(range(int(math.ceil(x_min)), int(math.floor(x_max)) + 1))
    stride = max(1, math.ceil(len(x_ticks) / 8))
    for tick in x_ticks[::stride]:
        x = px(float(tick))
        draw.line((x, bottom, x, bottom + 10), fill=COLORS["dark"], width=2)
        text_center(draw, (x, bottom + 35), str(tick), tick_font, COLORS["gray"])
    draw.line((left, top, left, bottom), fill=COLORS["dark"], width=3)
    draw.line((left, bottom, right, bottom), fill=COLORS["dark"], width=3)
    if horizontal is not None:
        y = py(horizontal)
        for x in range(int(left), int(right), 24):
            draw.line((x, y, min(x + 13, right), y), fill=COLORS["green"], width=4)
    for name, values in series.items():
        color = COLORS[name]
        points = [(px(x), py(y)) for x, y in values]
        draw.line(points, fill=color, width=6, joint="curve")
        for x, y in points:
            draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=color, outline="white", width=2)
    text_center(draw, ((left + right) / 2, rect[3] - 38), xlabel, font(29), COLORS["dark"])
    vertical_text(image, (rect[0] + 42, (top + bottom) / 2), ylabel, font(27), COLORS["dark"])
    legend_x = right - 165
    legend_y = top + 22
    for index, name in enumerate(series):
        y = legend_y + index * 42
        draw.line((legend_x, y, legend_x + 50, y), fill=COLORS[name], width=6)
        draw.text((legend_x + 65, y - 16), name, font=font(25, True), fill=COLORS["dark"])


def bar_plot(
    image: Image.Image,
    rect: tuple[int, int, int, int],
    values: dict[str, float],
    title: str,
    ylabel: str,
    y_min: float,
    y_max: float,
    annotations: bool = True,
    tick_digits: int = 2,
) -> None:
    draw = ImageDraw.Draw(image)
    panel_title(draw, rect, title)
    left, top, right, bottom = rect[0] + 145, rect[1] + 90, rect[2] - 45, rect[3] - 105

    def py(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    ticks = np.linspace(y_min, y_max, 6)
    for tick in ticks:
        y = py(float(tick))
        draw.line((left, y, right, y), fill=COLORS["grid"], width=2)
        text_right(draw, (left - 18, y), f"{tick:.{tick_digits}f}", font(24), COLORS["gray"])
    zero_y = py(0.0) if y_min <= 0 <= y_max else bottom
    draw.line((left, top, left, bottom), fill=COLORS["dark"], width=3)
    draw.line((left, zero_y, right, zero_y), fill=COLORS["dark"], width=3)
    names = list(values)
    usable = right - left
    bar_width = min(170, usable / (len(names) * 2.2))
    for index, name in enumerate(names):
        x = left + usable * (index + 0.5) / len(names)
        value = values[name]
        y = py(value)
        draw.rounded_rectangle(
            (x - bar_width / 2, min(y, zero_y), x + bar_width / 2, max(y, zero_y)),
            radius=14,
            fill=COLORS.get(name, COLORS["gray"]),
        )
        text_center(draw, (x, bottom + 34), name, font(28, True), COLORS["dark"])
        if annotations:
            text_center(draw, (x, y - 28 if value < 0 else y - 28), f"{value:.4f}", font(26, True), COLORS["dark"])
    vertical_text(image, (rect[0] + 42, (top + bottom) / 2), ylabel, font(27), COLORS["dark"])


def plot_relaxation(output_dir: Path, cases: dict[str, CaseData]) -> None:
    image = new_canvas(2400, 2100)
    rows = [(70, 60, 2330, 690), (70, 735, 2330, 1365), (70, 1410, 2330, 2040)]
    energy_series = {
        name: [
            (step, 1000.0 * (energy - case.trajectory_e0[-1]))
            for step, energy in enumerate(case.trajectory_e0, start=1)
        ]
        for name, case in ((name, cases[name]) for name in ("T", "O"))
    }
    force_series = {
        name: [(step, force) for step, force in enumerate(cases[name].max_forces, start=1)]
        for name in ("T", "O")
    }
    displacement_series = {
        name: [
            (step, h_displacement(cases[name], frame[-1]))
            for step, frame in enumerate(cases[name].frames, start=1)
        ]
        for name in ("T", "O")
    }
    line_plot(image, rows[0], energy_series, "Relaxation energy", "Ionic step", "Delta E0 (meV)", [0, 20, 40, 60, 80, 100])
    line_plot(image, rows[1], force_series, "Maximum atomic force", "Ionic step", "Max force (eV/Angstrom)", [0.003, 0.01, 0.03, 0.1, 0.3, 1.2], log_y=True, horizontal=FORCE_LIMIT_EV_A)
    line_plot(image, rows[2], displacement_series, "Hydrogen displacement from initial site", "Ionic step", "H displacement (Angstrom)", [0, 0.005, 0.010, 0.015, 0.020, 0.025, 0.030])
    save_figure(image, output_dir, "01_relax_convergence")


def plot_energetics(output_dir: Path, analysis: dict[str, object]) -> None:
    image = new_canvas(1800, 1250)
    bar_plot(image, (80, 60, 1720, 1170), analysis["esol"], "Hydrogen solution energies", "Esol (eV/H)", -0.55, 0.0)
    draw = ImageDraw.Draw(image)
    text_center(
        draw,
        (900, 185),
        f"O - T = {1000.0 * float(analysis['delta_o_t_ev']):.3f} meV",
        font(31, True),
        COLORS["dark"],
    )
    save_figure(image, output_dir, "02_solution_energies")


def project_vector(vector: np.ndarray) -> tuple[float, float, float]:
    x, y, z = vector
    return 0.78 * x - 0.62 * y, 0.34 * x + 0.43 * y - 0.82 * z, 0.35 * x + 0.30 * y + 0.65 * z


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    fill: tuple[int, int, int],
    width: int = 7,
    head: float = 22.0,
) -> None:
    draw.line((start[0], start[1], end[0], end[1]), fill=fill, width=width)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        return
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    base_x, base_y = end[0] - head * ux, end[1] - head * uy
    draw.polygon(
        (
            (end[0], end[1]),
            (base_x + 0.48 * head * px, base_y + 0.48 * head * py),
            (base_x - 0.48 * head * px, base_y - 0.48 * head * py),
        ),
        fill=fill,
    )


def project_crystal_vector(vector: np.ndarray) -> tuple[float, float, float]:
    """Oblique projection chosen so hcp a, b, and c directions stay distinct."""
    x, y, z = np.asarray(vector, dtype=float)
    return y, -0.50 * x + 0.8660254038 * z, 0.8660254038 * x + 0.50 * z


def blend_color(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    fraction: float,
) -> tuple[int, int, int]:
    value = min(1.0, max(0.0, fraction))
    return tuple(int(round(a + value * (b - a))) for a, b in zip(first, second))


def draw_sphere(
    image: Image.Image,
    center: tuple[float, float],
    radius: int,
    base: tuple[int, int, int],
    outline: tuple[int, int, int] = (255, 255, 255),
) -> None:
    """Draw a small shaded sphere using concentric offset circles."""
    draw = ImageDraw.Draw(image)
    dark = blend_color(base, (20, 25, 32), 0.35)
    draw.ellipse(
        (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
        fill=dark,
        outline=outline,
        width=4,
    )
    for step in range(16):
        fraction = step / 15.0
        inner_radius = radius * (0.88 - 0.52 * fraction)
        offset = radius * 0.22 * fraction
        color = blend_color(base, (255, 255, 255), 0.50 * fraction)
        draw.ellipse(
            (
                center[0] - offset - inner_radius,
                center[1] - offset - inner_radius,
                center[0] - offset + inner_radius,
                center[1] - offset + inner_radius,
            ),
            fill=color,
        )


def draw_axis_triad(
    draw: ImageDraw.ImageDraw,
    origin: tuple[float, float],
    lattice: np.ndarray,
    length: float,
    label_size: int = 27,
) -> None:
    for label, vector, color in (
        ("a", lattice[0], COLORS["axis_a"]),
        ("b", lattice[1], COLORS["axis_b"]),
        ("c", lattice[2], COLORS["axis_c"]),
    ):
        unit = np.asarray(vector, dtype=float) / np.linalg.norm(vector)
        projected_x, projected_y, _ = project_crystal_vector(unit)
        end = (origin[0] + projected_x * length, origin[1] - projected_y * length)
        draw_arrow(draw, origin, end, color, width=7, head=20)
        text_center(
            draw,
            (end[0] + 22 * np.sign(projected_x), end[1] - 18 * np.sign(projected_y)),
            label,
            font(label_size, True),
            color,
        )
    draw.ellipse((origin[0] - 5, origin[1] - 5, origin[0] + 5, origin[1] + 5), fill=COLORS["dark"])


def plot_local_coordination(output_dir: Path, analysis: dict[str, object]) -> None:
    image = new_canvas(2400, 1250)
    draw = ImageDraw.Draw(image)
    for panel_index, site in enumerate(("T", "O")):
        rect = (70 + panel_index * 1180, 55, 1150 + panel_index * 1180, 1180)
        panel_title(draw, rect, f"{site} final site: {analysis['coordination'][site]}-fold coordination", 38)
        center = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2 + 35)
        scale = 175.0
        selected = [item for item in analysis["neighbors"][site] if float(item["distance_a"]) <= COORDINATION_CUTOFF_A]
        projected = []
        for item in selected:
            px, py, depth = project_vector(np.asarray(item["vector"], dtype=float))
            projected.append((depth, center[0] + px * scale, center[1] - py * scale, item))
        for _, x, y, _ in projected:
            draw.line((center[0], center[1], x, y), fill=(135, 146, 158), width=8)
        for _, x, y, item in sorted(projected):
            radius = 40
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=COLORS["Zr"], outline="white", width=5)
            text_center(draw, (x, y + 64), f"{float(item['distance_a']):.3f} A", font(23), COLORS["dark"])
        radius = 34
        draw.ellipse((center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius), fill=COLORS["H"], outline="white", width=5)
        text_center(draw, (center[0], center[1]), "H", font(25, True), (255, 255, 255))
        draw.text((rect[0] + 28, rect[3] - 60), "Zr", font=font(27, True), fill=COLORS["Zr"])
        draw.text((rect[0] + 105, rect[3] - 60), "H", font=font(27, True), fill=COLORS["H"])
    save_figure(image, output_dir, "03_local_coordination")


def plot_t_initial_final_h(
    output_dir: Path,
    cases: dict[str, CaseData],
    analysis: dict[str, object],
) -> None:
    """Compare initial/final T cages and show the H shift in a separate 3D zoom."""
    image = new_canvas(2800, 1500)
    draw = ImageDraw.Draw(image)
    case = cases["T"]
    delta_distance, delta_vector, _ = minimum_image(
        case.contcar.frac[-1] - case.poscar.frac[-1],
        case.contcar.lattice,
    )
    c_hat = case.contcar.lattice[2] / np.linalg.norm(case.contcar.lattice[2])
    delta_c = float(np.dot(delta_vector, c_hat))
    delta_ab_vector = delta_vector - delta_c * c_hat
    delta_ab = float(np.linalg.norm(delta_ab_vector))

    def nearest_four(structure: Structure) -> list[dict[str, object]]:
        h_frac = structure.frac[-1]
        values: list[dict[str, object]] = []
        for index, frac in enumerate(structure.frac[:-1], start=1):
            distance, vector, _ = minimum_image(frac - h_frac, structure.lattice)
            values.append({"zr_atom_index": index, "distance_a": distance, "vector": vector})
        values.sort(key=lambda item: float(item["distance_a"]))
        return values[:4]

    def draw_snapshot(
        rect: tuple[int, int, int, int],
        title: str,
        structure: Structure,
        h_color: tuple[int, int, int],
        relaxation_vector: np.ndarray | None = None,
    ) -> None:
        panel_title(draw, rect, title, 37)
        center = ((rect[0] + rect[2]) / 2, 665.0)
        scale = 172.0
        neighbors = nearest_four(structure)
        projected: list[tuple[float, float, float, dict[str, object]]] = []
        for item in neighbors:
            projected_x, projected_y, depth = project_crystal_vector(np.asarray(item["vector"], dtype=float))
            projected.append(
                (
                    depth,
                    center[0] + projected_x * scale,
                    center[1] - projected_y * scale,
                    item,
                )
            )
        for first_index in range(len(projected)):
            for second_index in range(first_index + 1, len(projected)):
                first = projected[first_index]
                second = projected[second_index]
                draw.line((first[1], first[2], second[1], second[2]), fill=(214, 220, 227), width=4)
        for _, x, y, _ in projected:
            draw.line((center[0], center[1], x, y), fill=(104, 115, 128), width=12)
            draw.line((center[0], center[1], x, y), fill=(170, 181, 191), width=6)
        spheres: list[tuple[float, tuple[float, float], int, tuple[int, int, int], str]] = [
            (depth, (x, y), 42, COLORS["Zr"], "Zr") for depth, x, y, _ in projected
        ]
        spheres.append((0.0, center, 32, h_color, "H"))
        for _, sphere_center, radius, color, label in sorted(spheres, key=lambda entry: entry[0]):
            draw_sphere(image, sphere_center, radius, color)
            if label == "H":
                text_center(draw, sphere_center, "H", font(23, True), (255, 255, 255))
        if relaxation_vector is not None:
            direction_x, direction_y, _ = project_crystal_vector(relaxation_vector)
            projected_length = max(1e-12, math.hypot(direction_x, direction_y))
            unit_x = direction_x / projected_length
            unit_y = -direction_y / projected_length
            arrow_start = (center[0] + 43 * unit_x, center[1] + 43 * unit_y)
            arrow_end = (center[0] + 148 * unit_x, center[1] + 148 * unit_y)
            draw_arrow(draw, arrow_start, arrow_end, COLORS["axis_c"], width=5, head=16)
            draw.text(
                (arrow_end[0] + 14, arrow_end[1] - 14),
                "H shift: positive c-axis",
                font=font(22, True),
                fill=COLORS["dark"],
            )
        distances = ", ".join(f"{float(item['distance_a']):.3f}" for item in neighbors)
        text_center(
            draw,
            ((rect[0] + rect[2]) / 2, 1110),
            f"Four nearest H-Zr: {distances} A",
            font(23),
            COLORS["dark"],
        )
        text_center(
            draw,
            ((rect[0] + rect[2]) / 2, 1152),
            "View recentered on H",
            font(22),
            COLORS["gray"],
        )
        if relaxation_vector is not None:
            text_center(
                draw,
                ((rect[0] + rect[2]) / 2, 1192),
                "Arrow shows direction only (not to scale)",
                font(21),
                COLORS["gray"],
            )
        draw_axis_triad(draw, (rect[0] + 210.0, 1310.0), structure.lattice, 135.0, 27)

    initial_rect = (40, 55, 880, 1435)
    final_rect = (910, 55, 1750, 1435)
    draw_snapshot(initial_rect, "Initial T structure", case.poscar, COLORS["H_initial"])
    draw_snapshot(
        final_rect,
        "Relaxed T structure",
        case.contcar,
        COLORS["H"],
        relaxation_vector=delta_vector,
    )
    text_center(
        draw,
        (895.0, 155.0),
        "Initial and final H markers have identical size",
        font(24, True),
        COLORS["dark"],
    )

    # Right: the same 3D projection, with only the H displacement enlarged 40 times.
    right_rect = (1780, 55, 2755, 1435)
    panel_title(draw, right_rect, "H shift direction (3D zoom)", 39)
    zoom_factor = 40.0
    zoom_scale = 450.0
    initial_zoom = (2290.0, 955.0)
    zoom_vector = delta_vector * zoom_factor
    zoom_px, zoom_py, _ = project_crystal_vector(zoom_vector)
    final_zoom = (initial_zoom[0] + zoom_px * zoom_scale, initial_zoom[1] - zoom_py * zoom_scale)

    # A separate triad keeps the c-axis visible even though the shift is almost parallel to it.
    draw_axis_triad(draw, (1975.0, 1045.0), case.contcar.lattice, 190.0, 30)
    text_center(draw, (1975.0, 1142.0), "a, b, c directions", font(23, True), COLORS["dark"])
    draw_arrow(draw, initial_zoom, final_zoom, COLORS["dark"], width=10, head=30)
    draw_sphere(image, initial_zoom, 32, COLORS["H_initial"])
    draw_sphere(image, final_zoom, 32, COLORS["H"])
    draw.text((initial_zoom[0] + 42, initial_zoom[1] + 16), "Initial H", font=font(27, True), fill=COLORS["dark"])
    draw.text((final_zoom[0] + 42, final_zoom[1] - 28), "Final H", font=font(27, True), fill=COLORS["dark"])
    text_center(
        draw,
        ((right_rect[0] + right_rect[2]) / 2, 270),
        "Displacement vector enlarged 40x",
        font(28, True),
        COLORS["dark"],
    )
    text_center(
        draw,
        ((right_rect[0] + right_rect[2]) / 2, right_rect[3] - 142),
        f"Actual |dr| = {delta_distance:.5f} A",
        font(27, True),
        COLORS["dark"],
    )
    text_center(
        draw,
        ((right_rect[0] + right_rect[2]) / 2, right_rect[3] - 96),
        f"c component = {delta_c:+.5f} A",
        font(24),
        COLORS["dark"],
    )
    text_center(
        draw,
        ((right_rect[0] + right_rect[2]) / 2, right_rect[3] - 56),
        f"basal component = {delta_ab:.5f} A",
        font(24),
        COLORS["gray"],
    )
    save_figure(image, output_dir, "03b_t_h_initial_final")


def plot_neighbor_distances(output_dir: Path, analysis: dict[str, object], stem: str = "04_neighbor_distances") -> None:
    image = new_canvas(2100, 1350)
    series = {
        site: [(rank, float(item["distance_a"])) for rank, item in enumerate(analysis["neighbors"][site][:12], start=1)]
        for site in ("T", "O")
    }
    line_plot(
        image,
        (65, 45, 2035, 1290),
        series,
        "Sorted H-Zr neighbor distances",
        "Neighbor rank",
        "H-Zr distance (Angstrom)",
        [0, 1, 2, 2.5, 3, 4, 4.5],
        horizontal=COORDINATION_CUTOFF_A,
    )
    save_figure(image, output_dir, stem)


def plot_share_summary(output_dir: Path, cases: dict[str, CaseData], analysis: dict[str, object]) -> None:
    image = new_canvas(3000, 1250)
    bar_plot(image, (40, 45, 980, 1190), analysis["esol"], "Site energetics", "Esol (eV/H)", -0.55, 0.0)
    neighbor_series = {
        site: [(rank, float(item["distance_a"])) for rank, item in enumerate(analysis["neighbors"][site][:8], start=1)]
        for site in ("T", "O")
    }
    line_plot(
        image,
        (1010, 45, 1980, 1190),
        neighbor_series,
        "Local coordination",
        "Neighbor rank",
        "H-Zr distance (Angstrom)",
        [0, 1, 2, 2.5, 3, 4, 4.5],
        horizontal=COORDINATION_CUTOFF_A,
    )
    final_forces = {"T": cases["T"].max_forces[-1], "O": cases["O"].max_forces[-1]}
    bar_plot(
        image,
        (2010, 45, 2960, 1190),
        final_forces,
        "Final relaxation quality",
        "Max force (eV/Angstrom)",
        0.0,
        0.012,
        tick_digits=3,
    )
    draw = ImageDraw.Draw(image)
    limit_y = 1190 - 105 - FORCE_LIMIT_EV_A / 0.012 * ((1190 - 105) - (45 + 90))
    for x in range(2155, 2915, 25):
        draw.line((x, limit_y, min(x + 14, 2915), limit_y), fill=COLORS["green"], width=4)
    text_center(
        draw,
        (510, 118),
        f"O - T = {1000.0 * float(analysis['delta_o_t_ev']):.3f} meV",
        font(27, True),
        COLORS["dark"],
    )
    save_figure(image, output_dir, "05_share_summary")


def generate_figures(output_dir: Path, cases: dict[str, CaseData], analysis: dict[str, object]) -> None:
    plot_relaxation(output_dir, cases)
    plot_energetics(output_dir, analysis)
    plot_local_coordination(output_dir, analysis)
    plot_t_initial_final_h(output_dir, cases, analysis)
    plot_neighbor_distances(output_dir, analysis)
    plot_share_summary(output_dir, cases, analysis)


def main() -> int:
    args = parse_args()
    try:
        initial_root = normalize_initial_root(args.results_root)
        archive_hash = verify_archive(args.archive, args.checksum)
        cases = {name: load_case(initial_root, name, spec) for name, spec in CASE_SPECS.items()}
        analysis = prepare_analysis(cases)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_tables(args.output_dir, cases, analysis)
        generate_figures(args.output_dir, cases, analysis)
    except (OSError, ValueError, IndexError, np.linalg.LinAlgError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"archive_sha256={archive_hash}")
    print(f"results_root={initial_root}")
    print(f"output_dir={args.output_dir.resolve()}")
    print(f"Esol_T_eV={analysis['esol']['T']:.8f}")
    print(f"Esol_O_eV={analysis['esol']['O']:.8f}")
    print(f"DeltaE_O_minus_T_eV={analysis['delta_o_t_ev']:.8f}")
    print(f"coordination_T={analysis['coordination']['T']}")
    print(f"coordination_O={analysis['coordination']['O']}")
    print(f"site_separation_A={analysis['site_separation_a']:.8f}")
    print("Initial Zr96-H analysis PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
