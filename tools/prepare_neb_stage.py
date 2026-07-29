#!/usr/bin/env python3
"""Prepare a POTCAR-free NEB continuation or climbing-image stage.

The source is never modified.  Fixed endpoints are copied byte-for-byte and
intermediate POSCAR files are copied from the latest source CONTCAR files.
No VASP calculation is launched.
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from neb_common import (
    atomic_write_text,
    atomic_write_tsv,
    known_vasp_images_hdf5_shutdown,
    minimum_image,
    minimum_pair_distance,
    neb_projected_force_history,
    parse_key_values,
    parse_outcar,
    parse_poscar,
    require_file,
    require_same_topology,
    sha256,
)
from prepare_neb_paths import JOB_TEMPLATE


IMAGES = tuple(f"{index:02d}" for index in range(6))
INTERMEDIATES = IMAGES[1:-1]
STAGE_SETTINGS = {
    "pre": {"status": "pre_neb", "force": 0.10, "lclimb": False},
    "ci": {"status": "ci_neb", "force": 0.03, "lclimb": True},
}
MIN_PAIR_DISTANCE_A = 1.0
MAX_H_STEP_A = 1.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Completed or interrupted NEB stage")
    parser.add_argument("--target", type=Path, required=True, help="New stage directory")
    parser.add_argument("--target-stage", choices=tuple(STAGE_SETTINGS), required=True)
    return parser.parse_args()


def parse_incar(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in require_file(path).read_text(encoding="utf-8").splitlines():
        content = raw.split("#", 1)[0].split("!", 1)[0].strip()
        if "=" in content:
            key, value = content.split("=", 1)
            values[key.strip().upper()] = value.strip()
    return values


def bool_tag(value: str, key: str) -> bool:
    normalized = value.strip().strip(".").upper()
    if normalized in {"TRUE", "T"}:
        return True
    if normalized in {"FALSE", "F"}:
        return False
    raise ValueError(f"cannot parse {key} boolean value: {value!r}")


def source_stage(source: Path) -> str:
    status_path = source / ".run_status"
    if status_path.is_file() and status_path.stat().st_size:
        value = parse_key_values(status_path).get("stage")
        if value in {"pre_neb", "ci_neb"}:
            return "pre" if value == "pre_neb" else "ci"
    incar = parse_incar(source / "INCAR")
    return "ci" if bool_tag(incar.get("LCLIMB", ""), "LCLIMB") else "pre"


def projected_force(stage_directory: Path) -> float:
    return neb_projected_force_history(stage_directory, INTERMEDIATES)[-1][1]


def validate_common_inputs(source: Path) -> None:
    for filename in ("INCAR", "KPOINTS", "path_manifest.tsv"):
        require_file(source / filename)
    incar = parse_incar(source / "INCAR")
    expected = {
        "IMAGES": "4",
        "SPRING": "-5",
        "IBRION": "3",
        "SMASS": "2",
        "POTIM": "0.10",
        "ISIF": "2",
        "ISYM": "0",
    }
    failed = [f"{key}={incar.get(key)!r}" for key, value in expected.items() if incar.get(key) != value]
    if failed:
        raise ValueError(f"source NEB settings changed ({', '.join(failed)}): {source}")


def validate_ci_source(source: Path) -> float:
    status = parse_key_values(source / ".run_status")
    required = {
        "stage": "pre_neb",
        "normal_termination": "yes",
        "electronic_convergence": "yes",
        "lclimb": "false",
        "force_limit_ev_a": "0.10",
        "vasp_sha256": "a1b25c7ebf384a3147aa3ad8f77ba5fa020d8eacb8755f81e56d04cafabb1b6f",
        "vtst_version": "4.2",
    }
    failed = [f"{key}={status.get(key)!r}" for key, value in required.items() if status.get(key) != value]
    if status.get("vasp_exit") != "0" and not known_vasp_images_hdf5_shutdown(source):
        failed.append(f"vasp_exit={status.get('vasp_exit')!r}")
    if failed:
        raise ValueError(f"pre-NEB status is not eligible for CI ({', '.join(failed)}): {source}")
    force = projected_force(source)
    if force > STAGE_SETTINGS["pre"]["force"] + 1.0e-6:
        raise ValueError(f"pre-NEB force exceeds 0.10 eV/A: {force}")
    recorded_force = status.get("neb_force_ev_a", "").strip()
    if recorded_force:
        if abs(float(recorded_force) - force) > 5.0e-6:
            raise ValueError(f"pre-NEB force differs between OUTCAR and .run_status: {source}")
        if status.get("neb_force_limit_pass") != "yes":
            raise ValueError(f"pre-NEB .run_status contradicts its recorded force: {source}")
    return force


def validate_source_outputs(source: Path, require_complete: bool) -> list[Path]:
    contcars: list[Path] = []
    for image in INTERMEDIATES:
        directory = source / image
        contcar = require_file(directory / "CONTCAR")
        outcar = require_file(directory / "OUTCAR")
        text = outcar.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\b(?:NaN|nan|Inf)\b", text):
            raise ValueError(f"non-finite marker found in {outcar}")
        summary = parse_outcar(outcar)
        if summary.nions != 97 or not summary.electronic:
            raise ValueError(f"incomplete electronic result: {outcar}")
        if require_complete and (not summary.normal or not summary.ionic):
            raise ValueError(f"pre-NEB image did not converge normally: {outcar}")
        structure = parse_poscar(contcar)
        if structure.elements != ("Zr", "H") or structure.counts != (96, 1):
            raise ValueError(f"unexpected topology in {contcar}")
        if minimum_pair_distance(structure) < MIN_PAIR_DISTANCE_A:
            raise ValueError(f"abnormal short bond in {contcar}")
        contcars.append(contcar)
    return contcars


def update_incar(source: Path, target_stage: str) -> str:
    settings = STAGE_SETTINGS[target_stage]
    text = require_file(source).read_text(encoding="utf-8")
    replacements = {
        "EDIFFG": f"-{settings['force']:.2f}",
        "LCLIMB": ".TRUE." if settings["lclimb"] else ".FALSE.",
        "ISTART": "0",
        "ICHARG": "2",
    }
    for key, value in replacements.items():
        pattern = re.compile(rf"(?m)^(\s*{key}\s*=\s*)[^#!\r\n]+")
        text, count = pattern.subn(rf"\g<1>{value}", text)
        if count != 1:
            raise ValueError(f"expected exactly one {key} entry in {source}, found {count}")
    first, *rest = text.splitlines()
    label = "CI-NEB" if target_stage == "ci" else "pre-NEB restart"
    if first.startswith("SYSTEM"):
        first = re.sub(r"ordinary NEB|pre-NEB|CI-NEB", label, first)
    return "\n".join((first, *rest)) + "\n"


def stage_job(path_name: str, target_stage: str) -> str:
    settings = STAGE_SETTINGS[target_stage]
    text = JOB_TEMPLATE.replace("__JOB_NAME__", f"zrh_{path_name}_{target_stage}")
    if target_stage == "ci":
        text = text.replace('stage="pre_neb"', 'stage="ci_neb"')
        text = text.replace('force_limit="0.10"', 'force_limit="0.03"')
        text = text.replace("echo \"lclimb=false\"", "echo \"lclimb=true\"")
    return text


def manifest_rows(
    source: Path,
    target: Path,
    source_stage_name: str,
    target_stage: str,
    source_force: float | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"key": "source_directory", "value": os.path.relpath(source, target), "unit": ""},
        {"key": "source_stage", "value": STAGE_SETTINGS[source_stage_name]["status"], "unit": ""},
        {"key": "target_stage", "value": STAGE_SETTINGS[target_stage]["status"], "unit": ""},
        {"key": "target_force_limit", "value": f"{STAGE_SETTINGS[target_stage]['force']:.8f}", "unit": "eV/A"},
        {"key": "lclimb", "value": str(STAGE_SETTINGS[target_stage]["lclimb"]).lower(), "unit": ""},
        {"key": "source_incar_sha256", "value": sha256(source / "INCAR"), "unit": ""},
        {"key": "source_kpoints_sha256", "value": sha256(source / "KPOINTS"), "unit": ""},
        {"key": "source_endpoint_00_sha256", "value": sha256(source / "00/POSCAR"), "unit": ""},
        {"key": "source_endpoint_05_sha256", "value": sha256(source / "05/POSCAR"), "unit": ""},
    ]
    if (source / ".run_status").is_file():
        source_status = parse_key_values(source / ".run_status")
        rows.extend(
            (
                {"key": "source_vasp_exit", "value": source_status.get("vasp_exit", ""), "unit": ""},
                {
                    "key": "source_hdf5_postrun_error",
                    "value": (
                        "known_vasp_images_error_29"
                        if known_vasp_images_hdf5_shutdown(source)
                        else "none"
                    ),
                    "unit": "",
                },
            )
        )
    if source_force is not None:
        rows.append({"key": "source_final_neb_force", "value": f"{source_force:.8f}", "unit": "eV/A"})
    for image in INTERMEDIATES:
        rows.append(
            {
                "key": f"source_{image}_contcar_sha256",
                "value": sha256(source / image / "CONTCAR"),
                "unit": "",
            }
        )
    return rows


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    target = args.target.resolve()
    if not source.is_dir():
        raise ValueError(f"source stage directory is missing: {source}")
    if target.exists():
        raise ValueError(f"refusing to overwrite existing target: {target}")
    validate_common_inputs(source)
    source_stage_name = source_stage(source)
    if args.target_stage == "ci" and source_stage_name != "pre":
        if source_stage_name != "ci":
            raise ValueError("CI target requires a pre-NEB or CI source")
    source_force: float | None = None
    if source_stage_name == "pre" and args.target_stage == "ci":
        source_force = validate_ci_source(source)
    elif (source / ".run_status").is_file():
        status = parse_key_values(source / ".run_status")
        if (
            status.get("neb_force_limit_pass") == "yes"
            and status.get("ionic_convergence") == "yes"
            and source_stage_name == args.target_stage
        ):
            raise ValueError(f"source stage is already converged; no continuation is needed: {source}")
    contcars = validate_source_outputs(
        source,
        require_complete=source_stage_name == "pre" and args.target_stage == "ci",
    )

    endpoint_00 = parse_poscar(source / "00/POSCAR")
    endpoint_05 = parse_poscar(source / "05/POSCAR")
    require_same_topology(endpoint_00, endpoint_05, "fixed endpoints")
    structures = [endpoint_00, *(parse_poscar(path) for path in contcars), endpoint_05]
    for index, structure in enumerate(structures):
        require_same_topology(endpoint_00, structure, f"stage image {index:02d}")
        if minimum_pair_distance(structure) < MIN_PAIR_DISTANCE_A:
            raise ValueError(f"abnormal short bond in stage image {index:02d}")
        if index:
            step, _, _ = minimum_image(
                structure.frac[-1] - structures[index - 1].frac[-1],
                structure.lattice,
            )
            if step > MAX_H_STEP_A:
                raise ValueError(f"discontinuous H path at image {index:02d}: {step:.8f} A")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare_neb_stage.", dir=target.parent) as temporary_name:
        stage = Path(temporary_name)
        (stage / "00").mkdir()
        (stage / "05").mkdir()
        shutil.copyfile(source / "00/POSCAR", stage / "00/POSCAR")
        shutil.copyfile(source / "05/POSCAR", stage / "05/POSCAR")
        for image, contcar in zip(INTERMEDIATES, contcars):
            destination = stage / image / "POSCAR"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(contcar, destination)
        shutil.copyfile(source / "KPOINTS", stage / "KPOINTS")
        shutil.copyfile(source / "path_manifest.tsv", stage / "path_manifest.tsv")
        atomic_write_text(stage / "INCAR", update_incar(source / "INCAR", args.target_stage))
        path_name = source
        while path_name.name.startswith(("ci_", "pre_restart_")):
            path_name = path_name.parent
        atomic_write_text(stage / "job.slurm", stage_job(path_name.name, args.target_stage))
        atomic_write_tsv(
            stage / "stage_manifest.tsv",
            ("key", "value", "unit"),
            manifest_rows(source, target, source_stage_name, args.target_stage, source_force),
        )
        shutil.move(str(stage), str(target))

    print("NEB stage preparation PASS")
    print(f"Source:       {source}")
    print(f"Target:       {target}")
    print(f"Target stage: {STAGE_SETTINGS[args.target_stage]['status']}")
    print("No POTCAR was read or copied; no VASP calculation was launched.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
