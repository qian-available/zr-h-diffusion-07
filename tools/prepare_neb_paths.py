#!/usr/bin/env python3
"""Generate symmetry-equivalent TT_c and relaxed-endpoint TO pre-NEB inputs.

This is a geometry-only tool.  It requires an explicit downloaded result root,
never reads POTCAR, never evaluates a wavefunction, and never launches VASP.
The generated ordinary NEB is a 0.10 eV/A pre-relaxation stage for a later
climbing-image NEB refinement.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile

import numpy as np

from neb_common import (
    Structure,
    atomic_write_text,
    atomic_write_tsv,
    h_neighbors,
    minimum_image,
    minimum_image_distances,
    minimum_pair_distance,
    parse_key_values,
    parse_outcar,
    parse_poscar,
    poscar_text,
    require_file,
    require_same_topology,
    sha256,
)


T_RELATIVE = Path("01_initial/03_t_relax/retry_dcu_01")
O_RELATIVE = Path("01_initial/04_o_relax/retry_dcu_01")
EXPECTED_E0 = {"T": -821.95359478, "O": -821.89069078}
EXPECTED_FORCE = {"T": 0.00874278, "O": 0.00420013}
EXPECTED_T_IDEAL = np.asarray([1.0 / 3.0, 5.0 / 12.0, 13.0 / 24.0])
EXPECTED_T2_IDEAL = np.asarray([1.0 / 3.0, 5.0 / 12.0, 15.0 / 24.0])
EXPECTED_T_CENTER_DISTANCE_A = 1.29150537
EXPECTED_TO_DISTANCE_A = 1.98514991
EXPECTED_TO_BASAL_A = 1.86838910
EXPECTED_TO_C_A = -0.67077742
FORCE_LIMIT_EV_A = 0.01
MAPPING_TOLERANCE_A = 1.0e-8
ZR_ENDPOINT_LIMIT_A = 0.25
MIN_PAIR_DISTANCE_A = 1.0
IMAGE_COUNT = 4


INCAR_TEXT = """SYSTEM = Zr96H pre-NEB, four intermediate images

ISTART = 0
ICHARG = 2
ISPIN  = 1
PREC   = Accurate
ENCUT  = 450
EDIFF  = 1E-6
NELM   = 160
ALGO   = Normal

ISMEAR = 1
SIGMA  = 0.20
LREAL  = Auto
LASPH  = .TRUE.
ADDGRID = .TRUE.

IBRION = 3
SMASS  = 2
POTIM  = 0.10
NSW    = 300
EDIFFG = -0.10
ISIF   = 2
ISYM   = 0

IMAGES = 4
SPRING = -5
LCLIMB = .FALSE.

LWAVE  = .FALSE.
LCHARG = .FALSE.
"""


KPOINTS_TEXT = """Zr96H Gamma-centered 4x4x3 mesh
0
Gamma
4 4 3
0 0 0
"""


JOB_TEMPLATE = r"""#!/bin/bash
#SBATCH -N 1
#SBATCH -n 32
#SBATCH -J __JOB_NAME__
#SBATCH --gres=dcu:4
#SBATCH -p xahdnormal
#SBATCH --time=5-00:00:00
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err

set -euo pipefail

cd "${SLURM_SUBMIT_DIR:?submit this file with sbatch}"
expected_vasp_sha="a1b25c7ebf384a3147aa3ad8f77ba5fa020d8eacb8755f81e56d04cafabb1b6f"
stage="pre_neb"
force_limit="0.10"
for file in INCAR KPOINTS POTCAR inputs.sha256; do
    [[ -s "${file}" ]] || { echo "ERROR: missing ${file}" >&2; exit 2; }
done
for image in 00 01 02 03 04 05; do
    [[ -s "${image}/POSCAR" ]] || { echo "ERROR: missing ${image}/POSCAR" >&2; exit 2; }
done
sha256sum --check --strict --quiet inputs.sha256
for output in vasp.stdout vasp.stderr .run_status; do
    [[ ! -e "${output}" ]] || { echo "ERROR: prior output exists: ${output}" >&2; exit 3; }
done
for image in 01 02 03 04; do
    for output in OUTCAR CONTCAR OSZICAR vasprun.xml; do
        [[ ! -e "${image}/${output}" ]] || {
            echo "ERROR: prior image output exists: ${image}/${output}" >&2
            exit 3
        }
    done
done
mkdir .run_lock
trap 'rmdir .run_lock 2>/dev/null || true' EXIT

module purge
module load compiler/intel/2020.1.217
module load mpi/intelmpi/2020.1.217
module load compiler/dtk/22.10

pkg="/work/home/liuzhixiao/software/dcu-port-2Feb2023-all"
[[ -s "${pkg}/env.sh" ]] || { echo "ERROR: missing ${pkg}/env.sh" >&2; exit 2; }
source "${pkg}/env.sh"
vasp_exe="${VASP_EXE:-${pkg}/bin/vasp_std}"
[[ -x "${vasp_exe}" ]] || { echo "ERROR: unavailable VASP: ${vasp_exe}" >&2; exit 2; }
vasp_sha="$(sha256sum "${vasp_exe}" | awk '{print $1}')"
[[ "${vasp_sha}" = "${expected_vasp_sha}" ]] || {
    echo "ERROR: VASP SHA-256 changed: ${vasp_sha}" >&2
    exit 2
}
LC_ALL=C strings "${vasp_exe}" | grep -F "VTST: version 4.2" >/dev/null || {
    echo "ERROR: expected VTST 4.2 marker is missing" >&2
    exit 2
}
LC_ALL=C strings "${vasp_exe}" | grep -F "LCLIMB" >/dev/null || {
    echo "ERROR: expected LCLIMB support is missing" >&2
    exit 2
}

config="config.${SLURM_JOB_ID}"
: >"${config}"
for host in $(scontrol show hostnames "${SLURM_NODELIST}"); do
    for ((device=0; device<4; device++)); do
        echo "-host ${host} -env HIP_VISIBLE_DEVICES ${device} -env OMP_NUM_THREADS 6 -n 1 numactl --cpunodebind=${device} --membind=${device} ${vasp_exe}" >>"${config}"
    done
done

ulimit -s unlimited
export OMP_NUM_THREADS=6
export NCCL_IB_HCA="mlx5_0"
export HSA_FORCE_FINE_GRAIN_PCIE=1

set +e
mpirun -configfile "${config}" >vasp.stdout 2>vasp.stderr
vasp_exit=$?
set -e

normal=yes
electronic=yes
ionic=yes
for image in 01 02 03 04; do
    grep -q "General timing and accounting" "${image}/OUTCAR" 2>/dev/null || normal=no
    grep -q "aborting loop because EDIFF is reached" "${image}/OUTCAR" 2>/dev/null || electronic=no
    grep -q "reached required accuracy" "${image}/OUTCAR" 2>/dev/null || ionic=no
done
neb_force="$(
    for image in 01 02 03 04; do
        awk '/FORCES: max atom, RMS/ {value=$(NF-1)} END {if (value != "") print value}' \
            "${image}/OUTCAR" 2>/dev/null
    done | awk 'NR == 1 {maximum=$1} $1 > maximum {maximum=$1}
        END {if (NR) printf "%.8f", maximum}'
)"
force_ok=no
if [[ -n "${neb_force}" ]]; then
    awk -v value="${neb_force}" -v limit="${force_limit}" \
        'BEGIN {exit !(value <= limit + 0.000001)}' && force_ok=yes
fi
hdf5_postrun_error=none
if ((vasp_exit != 0)) &&
    ((vasp_exit == 1)) &&
    grep -Eqi 'internal error in:[[:space:]]*vhdf5\.F' vasp.stderr &&
    grep -Eqi 'HDF5 call .* produced error:[[:space:]]*29([^0-9]|$)' vasp.stderr; then
    hdf5_postrun_error=known_vasp_images_error_29
fi
scientific_result_ok=no
if [[ "${normal}" = yes && "${electronic}" = yes && "${ionic}" = yes &&
    "${force_ok}" = yes ]] &&
    { ((vasp_exit == 0)) || [[ "${hdf5_postrun_error}" = known_vasp_images_error_29 ]]; }; then
    scientific_result_ok=yes
fi
{
    echo "slurm_job_id=${SLURM_JOB_ID}"
    echo "vasp_exit=${vasp_exit}"
    echo "normal_termination=${normal}"
    echo "electronic_convergence=${electronic}"
    echo "ionic_convergence=${ionic}"
    echo "neb_force_ev_a=${neb_force}"
    echo "stage=${stage}"
    echo "force_limit_ev_a=${force_limit}"
    echo "lclimb=false"
    echo "neb_force_limit_pass=${force_ok}"
    echo "hdf5_postrun_error=${hdf5_postrun_error}"
    echo "scientific_result_ok=${scientific_result_ok}"
    echo "images=4"
    echo "accelerator=dcu:4"
    echo "vasp_exe=${vasp_exe}"
    echo "vasp_sha256=${vasp_sha}"
    echo "vtst_version=4.2"
    echo "finished_at=$(date --iso-8601=seconds)"
} >.run_status.tmp
mv .run_status.tmp .run_status
[[ "${scientific_result_ok}" = yes ]]
"""


def parse_args() -> argparse.Namespace:
    workflow_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-root",
        type=Path,
        required=True,
        help="Downloaded 07_h_diffusion_quickstart directory containing 01_initial",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=workflow_root,
        help="07_h_diffusion_quickstart directory receiving endpoint and NEB inputs",
    )
    return parser.parse_args()


def normalize_results_root(path: Path) -> Path:
    root = path.resolve()
    if root.name == "01_initial":
        root = root.parent
    if not (root / "01_initial").is_dir():
        raise ValueError("--results-root must contain 01_initial")
    return root


def symmetry_transform(frac: np.ndarray) -> np.ndarray:
    transformed = np.asarray(frac, dtype=float).copy()
    transformed[..., 2] = 7.0 / 6.0 - transformed[..., 2]
    return transformed % 1.0


def load_case(root: Path, name: str, relative: Path) -> tuple[Structure, Structure, float, float]:
    directory = root / relative
    for filename in ("POSCAR", "CONTCAR", "OUTCAR", ".run_status"):
        require_file(directory / filename)
    poscar = parse_poscar(directory / "POSCAR")
    contcar = parse_poscar(directory / "CONTCAR")
    require_same_topology(poscar, contcar, f"{name} POSCAR/CONTCAR")
    if poscar.elements != ("Zr", "H") or poscar.counts != (96, 1):
        raise ValueError(f"{name} must use Zr H / 96 1")
    summary = parse_outcar(directory / "OUTCAR")
    status = parse_key_values(directory / ".run_status")
    if summary.nions != 97:
        raise ValueError(f"unexpected NIONS for {name}: {summary.nions}")
    if not summary.normal or not summary.electronic or not summary.ionic:
        raise ValueError(f"OUTCAR convergence markers failed for {name}")
    if status.get("vasp_exit") != "0" or status.get("normal_termination") != "yes":
        raise ValueError(f".run_status does not confirm successful exit for {name}")
    if status.get("electronic_convergence") != "yes" or status.get("ionic_convergence") != "yes":
        raise ValueError(f".run_status does not confirm convergence for {name}")
    if abs(summary.e0_ev - EXPECTED_E0[name]) > 5.0e-8:
        raise ValueError(f"E0 regression failed for {name}: {summary.e0_ev}")
    if abs(summary.final_max_force_ev_a - EXPECTED_FORCE[name]) > 5.0e-7:
        raise ValueError(f"force regression failed for {name}: {summary.final_max_force_ev_a}")
    if summary.final_max_force_ev_a > FORCE_LIMIT_EV_A + 1.0e-6:
        raise ValueError(f"force limit failed for {name}: {summary.final_max_force_ev_a}")
    return poscar, contcar, summary.e0_ev, summary.final_max_force_ev_a


def build_symmetry_endpoint(t_initial: Structure, t_final: Structure) -> tuple[Structure, list[tuple[int, int, float]], dict[str, float]]:
    if not np.allclose(t_initial.frac[-1], EXPECTED_T_IDEAL, rtol=0.0, atol=2.0e-8):
        raise ValueError(f"unexpected ideal T1 coordinate: {t_initial.frac[-1]}")
    transformed_ideal = symmetry_transform(t_initial.frac[:-1])
    host = t_initial.frac[:-1]
    assignments: list[tuple[int, int, float]] = []
    used_targets: set[int] = set()
    endpoint_frac = np.empty_like(t_final.frac)
    for source_index, transformed in enumerate(transformed_ideal):
        deltas = host - transformed
        distances = minimum_image_distances(deltas, t_initial.lattice)
        target_index = int(np.argmin(distances))
        distance = float(distances[target_index])
        if distance >= MAPPING_TOLERANCE_A:
            raise ValueError(
                f"symmetry mapping residual is too large for Zr {source_index + 1}: {distance} A"
            )
        if target_index in used_targets:
            raise ValueError(f"symmetry mapping is not one-to-one at Zr target {target_index + 1}")
        used_targets.add(target_index)
        endpoint_frac[target_index] = symmetry_transform(t_final.frac[source_index])
        assignments.append((source_index, target_index, distance))
    if len(used_targets) != 96:
        raise ValueError("symmetry mapping omitted one or more Zr atoms")
    endpoint_frac[-1] = symmetry_transform(t_final.frac[-1])
    endpoint = Structure(
        comment="Zr96H symmetry-generated c-axis T2 endpoint; no VASP endpoint calculation",
        lattice=t_final.lattice.copy(),
        elements=t_final.elements,
        counts=t_final.counts,
        frac=endpoint_frac,
    )

    ideal_target = symmetry_transform(t_initial.frac[-1])
    if not np.allclose(ideal_target, EXPECTED_T2_IDEAL, rtol=0.0, atol=2.0e-8):
        raise ValueError(f"symmetry operation did not produce expected T2: {ideal_target}")
    ideal_distance, ideal_vector, _ = minimum_image(
        ideal_target - t_initial.frac[-1], t_initial.lattice
    )
    if abs(ideal_distance - EXPECTED_T_CENTER_DISTANCE_A) > 2.0e-8:
        raise ValueError(f"ideal TT distance regression failed: {ideal_distance}")
    if float(np.linalg.norm(ideal_vector[:2])) > 1.0e-10:
        raise ValueError("ideal TT vector has an unexpected basal component")

    zr_displacements = minimum_image_distances(
        endpoint.frac[:-1] - t_final.frac[:-1], t_final.lattice
    )
    if float(zr_displacements.max()) > ZR_ENDPOINT_LIMIT_A:
        raise ValueError(
            f"symmetry endpoint Zr displacement exceeds {ZR_ENDPOINT_LIMIT_A} A: "
            f"{float(zr_displacements.max())}"
        )
    neighbors = h_neighbors(endpoint)
    if sum(distance <= 2.5 for distance in neighbors) != 4 or neighbors[4] <= 2.5:
        raise ValueError(f"symmetry T2 is not a clear four-coordinate site: {neighbors[:6]}")
    actual_distance, actual_vector, _ = minimum_image(
        endpoint.frac[-1] - t_final.frac[-1], t_final.lattice
    )
    metrics = {
        "max_mapping_residual_a": max(item[2] for item in assignments),
        "ideal_h_distance_a": ideal_distance,
        "ideal_h_basal_a": float(np.linalg.norm(ideal_vector[:2])),
        "ideal_h_c_a": float(ideal_vector[2]),
        "actual_h_distance_a": actual_distance,
        "actual_h_basal_a": float(np.linalg.norm(actual_vector[:2])),
        "actual_h_c_a": float(actual_vector[2]),
        "zr_endpoint_rms_a": float(np.sqrt(np.mean(zr_displacements**2))),
        "zr_endpoint_max_a": float(zr_displacements.max()),
        "t2_neighbor_1_a": neighbors[0],
        "t2_neighbor_4_a": neighbors[3],
        "t2_neighbor_5_a": neighbors[4],
    }
    return endpoint, assignments, metrics


def interpolate(start: Structure, end: Structure, fraction: float, comment: str) -> Structure:
    require_same_topology(start, end, comment)
    fractional_deltas = np.empty_like(start.frac)
    for index, delta in enumerate(end.frac - start.frac):
        _, _, fractional_deltas[index] = minimum_image(delta, start.lattice)
    return Structure(
        comment=comment,
        lattice=start.lattice.copy(),
        elements=start.elements,
        counts=start.counts,
        frac=(start.frac + fraction * fractional_deltas) % 1.0,
    )


def prepare_path(
    path_name: str,
    start: Structure,
    end: Structure,
    start_e0: float,
    end_e0: float,
    source_t_hash: str,
    source_end_hash: str,
) -> tuple[list[Structure], list[dict[str, object]], dict[str, float | str]]:
    require_same_topology(start, end, path_name)
    images = [interpolate(start, end, index / (IMAGE_COUNT + 1), f"{path_name} image {index:02d}") for index in range(IMAGE_COUNT + 2)]
    images[0] = Structure(
        comment=f"{path_name} endpoint 00 from converged source",
        lattice=start.lattice.copy(), elements=start.elements, counts=start.counts, frac=start.frac.copy()
    )
    images[-1] = Structure(
        comment=f"{path_name} endpoint 05",
        lattice=end.lattice.copy(), elements=end.elements, counts=end.counts, frac=end.frac.copy()
    )
    geometry_rows: list[dict[str, object]] = []
    cumulative = 0.0
    for index, image in enumerate(images):
        min_pair = minimum_pair_distance(image)
        if min_pair < MIN_PAIR_DISTANCE_A:
            raise ValueError(f"{path_name} image {index:02d} has {min_pair:.8f} A atom pair")
        if index == 0:
            step_distance = basal = c_component = 0.0
        else:
            step_distance, vector, _ = minimum_image(
                image.frac[-1] - images[index - 1].frac[-1], image.lattice
            )
            basal = float(np.linalg.norm(vector[:2]))
            c_component = float(vector[2])
            cumulative += step_distance
        geometry_rows.append(
            {
                "path": path_name,
                "image": f"{index:02d}",
                "h_step_a": f"{step_distance:.8f}",
                "h_cumulative_a": f"{cumulative:.8f}",
                "h_step_basal_a": f"{basal:.8f}",
                "h_step_c_a": f"{c_component:.8f}",
                "minimum_pair_a": f"{min_pair:.8f}",
            }
        )
    total_distance, total_vector, _ = minimum_image(end.frac[-1] - start.frac[-1], start.lattice)
    metrics: dict[str, float | str] = {
        "path": path_name,
        "images": IMAGE_COUNT,
        "start_e0_ev": start_e0,
        "end_e0_ev": end_e0,
        "endpoint_delta_e_ev": end_e0 - start_e0,
        "h_endpoint_distance_a": total_distance,
        "h_endpoint_basal_a": float(np.linalg.norm(total_vector[:2])),
        "h_endpoint_c_a": float(total_vector[2]),
        "source_t_contcar_sha256": source_t_hash,
        "source_end_contcar_sha256": source_end_hash,
        "endpoint_00_poscar_sha256": hashlib.sha256(poscar_text(images[0]).encode("utf-8")).hexdigest(),
        "endpoint_05_poscar_sha256": hashlib.sha256(poscar_text(images[-1]).encode("utf-8")).hexdigest(),
    }
    if path_name == "TO":
        if abs(total_distance - EXPECTED_TO_DISTANCE_A) > 2.0e-6:
            raise ValueError(f"TO distance regression failed: {total_distance}")
        if abs(float(np.linalg.norm(total_vector[:2])) - EXPECTED_TO_BASAL_A) > 2.0e-6:
            raise ValueError("TO basal-component regression failed")
        if abs(float(total_vector[2]) - EXPECTED_TO_C_A) > 2.0e-6:
            raise ValueError("TO c-component regression failed")
    return images, geometry_rows, metrics


def manifest_rows(metrics: dict[str, float | str]) -> list[dict[str, object]]:
    units = {
        "start_e0_ev": "eV", "end_e0_ev": "eV", "endpoint_delta_e_ev": "eV",
        "h_endpoint_distance_a": "A", "h_endpoint_basal_a": "A", "h_endpoint_c_a": "A",
    }
    return [
        {"key": key, "value": f"{value:.12f}" if isinstance(value, float) else value, "unit": units.get(key, "")}
        for key, value in metrics.items()
    ]


def write_path(directory: Path, path_name: str, images: list[Structure], geometry_rows: list[dict[str, object]], metrics: dict[str, float | str]) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    for index, image in enumerate(images):
        atomic_write_text(directory / f"{index:02d}" / "POSCAR", poscar_text(image))
    atomic_write_text(directory / "INCAR", INCAR_TEXT.replace("pre-NEB", f"{path_name} pre-NEB"))
    atomic_write_text(directory / "KPOINTS", KPOINTS_TEXT)
    atomic_write_text(directory / "job.slurm", JOB_TEMPLATE.replace("__JOB_NAME__", f"zrh_{path_name.lower()}"))
    atomic_write_tsv(directory / "path_manifest.tsv", ("key", "value", "unit"), manifest_rows(metrics))
    atomic_write_tsv(
        directory / "initial_path_geometry.tsv",
        ("path", "image", "h_step_a", "h_cumulative_a", "h_step_basal_a", "h_step_c_a", "minimum_pair_a"),
        geometry_rows,
    )


def main() -> int:
    args = parse_args()
    results_root = normalize_results_root(args.results_root)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    t_initial, t_final, t_e0, _ = load_case(results_root, "T", T_RELATIVE)
    o_initial, o_final, o_e0, _ = load_case(results_root, "O", O_RELATIVE)
    require_same_topology(t_initial, o_initial, "T/O initial structures")
    require_same_topology(t_final, o_final, "T/O final structures")

    endpoint, assignments, symmetry_metrics = build_symmetry_endpoint(t_initial, t_final)
    t_contcar_path = results_root / T_RELATIVE / "CONTCAR"
    o_contcar_path = results_root / O_RELATIVE / "CONTCAR"
    t_hash = sha256(t_contcar_path)
    o_hash = sha256(o_contcar_path)
    tt_images, tt_geometry, tt_metrics = prepare_path(
        "TT_c", t_final, endpoint, t_e0, t_e0, t_hash, "symmetry:" + t_hash
    )
    to_images, to_geometry, to_metrics = prepare_path(
        "TO", t_final, o_final, t_e0, o_e0, t_hash, o_hash
    )

    targets = (
        output_root / "02_endpoints" / "01_tt_c_symmetry",
        output_root / "03_neb" / "01_tt_c",
        output_root / "03_neb" / "02_to",
    )
    for target in targets:
        if target.exists():
            raise ValueError(f"refusing to overwrite existing generated target: {target}")

    with tempfile.TemporaryDirectory(prefix=".prepare_neb_paths.", dir=output_root) as temporary_name:
        stage = Path(temporary_name)
        endpoint_stage = stage / "02_endpoints" / "01_tt_c_symmetry"
        endpoint_stage.mkdir(parents=True)
        atomic_write_text(endpoint_stage / "POSCAR", poscar_text(endpoint))
        symmetry_rows = [
            {"record": "metric", "source_zr_index": "", "target_zr_index": "", "value": key, "detail": f"{value:.12f}"}
            for key, value in symmetry_metrics.items()
        ]
        symmetry_rows.extend(
            {
                "record": "zr_mapping",
                "source_zr_index": source + 1,
                "target_zr_index": target + 1,
                "value": "mapping_residual_a",
                "detail": f"{residual:.12e}",
            }
            for source, target, residual in assignments
        )
        symmetry_rows.extend(
            [
                {"record": "source", "source_zr_index": "", "target_zr_index": "", "value": "t_poscar_sha256", "detail": sha256(results_root / T_RELATIVE / "POSCAR")},
                {"record": "source", "source_zr_index": "", "target_zr_index": "", "value": "t_contcar_sha256", "detail": t_hash},
                {"record": "operation", "source_zr_index": "", "target_zr_index": "", "value": "fractional_transform", "detail": "x'=x; y'=y; z'=(7/6-z) mod 1"},
            ]
        )
        atomic_write_tsv(
            endpoint_stage / "symmetry_manifest.tsv",
            ("record", "source_zr_index", "target_zr_index", "value", "detail"),
            symmetry_rows,
        )
        write_path(stage / "03_neb" / "01_tt_c", "TT_c", tt_images, tt_geometry, tt_metrics)
        write_path(stage / "03_neb" / "02_to", "TO", to_images, to_geometry, to_metrics)

        for relative in (
            Path("02_endpoints/01_tt_c_symmetry"),
            Path("03_neb/01_tt_c"),
            Path("03_neb/02_to"),
        ):
            destination = output_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stage / relative), str(destination))

    print("Symmetry endpoint and NEB input generation PASS")
    print(f"T2 endpoint: {targets[0]}")
    print(f"TT_c path:   {targets[1]}")
    print(f"TO path:     {targets[2]}")
    print("No POTCAR was read or copied; no VASP calculation was launched.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
