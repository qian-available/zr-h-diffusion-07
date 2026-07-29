#!/usr/bin/env python3
"""Validate and analyze completed staged TT_c and TO CI-NEB results."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from neb_common import (
    Structure,
    atomic_write_tsv,
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


IMAGE_NAMES = tuple(f"{index:02d}" for index in range(6))
INTERMEDIATE_NAMES = IMAGE_NAMES[1:-1]
FORCE_LIMIT_EV_A = 0.03
MIN_PAIR_DISTANCE_A = 1.0
ENDPOINT_DELTA_EV = 0.062904
SELF_CHECK_TOLERANCE_EV = 0.005


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tt-result", type=Path, required=True, help="Final TT_c CI-NEB stage")
    parser.add_argument("--to-result", type=Path, required=True, help="Final TO CI-NEB stage")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def read_manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with require_file(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != ["key", "value", "unit"]:
            raise ValueError(f"unexpected path manifest schema: {path}")
        for row in reader:
            values[row["key"]] = row["value"]
    return values


def status_pass(path: Path, final_force: float) -> None:
    status = parse_key_values(path)
    required = {
        "vasp_exit": "0",
        "normal_termination": "yes",
        "electronic_convergence": "yes",
        "images": "4",
        "stage": "ci_neb",
        "force_limit_ev_a": "0.03",
        "lclimb": "true",
        "vasp_sha256": "a1b25c7ebf384a3147aa3ad8f77ba5fa020d8eacb8755f81e56d04cafabb1b6f",
        "vtst_version": "4.2",
    }
    failed = [f"{key}={status.get(key)!r}" for key, expected in required.items() if status.get(key) != expected]
    if failed:
        raise ValueError(f"NEB .run_status failed ({', '.join(failed)}): {path}")
    recorded_text = status.get("neb_force_ev_a", "").strip()
    if recorded_text:
        recorded = float(recorded_text)
        if not math.isfinite(recorded) or abs(recorded - final_force) > 5.0e-6:
            raise ValueError(f"NEB force differs between OUTCAR and .run_status: {path}")
        if status.get("neb_force_limit_pass") != "yes":
            raise ValueError(f"NEB .run_status contradicts its recorded force: {path}")


def final_structure(directory: Path, image: str) -> Structure:
    filename = "POSCAR" if image in ("00", "05") else "CONTCAR"
    return parse_poscar(directory / image / filename)


def stage_chain(final_directory: Path) -> list[Path]:
    chain = [final_directory]
    seen = {final_directory}
    current = final_directory
    while (current / "stage_manifest.tsv").is_file():
        manifest = read_manifest(current / "stage_manifest.tsv")
        source_value = manifest.get("source_directory")
        if not source_value:
            raise ValueError(f"stage manifest lacks source_directory: {current}")
        source = (current / source_value).resolve()
        if source in seen or not source.is_dir():
            raise ValueError(f"invalid stage-chain source {source_value!r}: {current}")
        for image in INTERMEDIATE_NAMES:
            expected = manifest.get(f"source_{image}_contcar_sha256")
            if expected != sha256(source / image / "CONTCAR"):
                raise ValueError(f"stage source hash changed: {source}/{image}/CONTCAR")
        chain.append(source)
        seen.add(source)
        current = source
    chain.reverse()
    if len(chain) < 2:
        raise ValueError(f"final CI result has no pre-NEB source chain: {final_directory}")
    first_status = parse_key_values(chain[0] / ".run_status")
    if first_status.get("stage") != "pre_neb":
        raise ValueError(f"stage chain does not begin with a passed pre-NEB: {chain[0]}")
    first_force = neb_projected_force_history(chain[0], INTERMEDIATE_NAMES)[-1][1]
    if first_force > 0.10 + 1.0e-6:
        raise ValueError(f"stage chain begins with an unconverged pre-NEB: {chain[0]}")
    for image in INTERMEDIATE_NAMES:
        summary = parse_outcar(chain[0] / image / "OUTCAR")
        if not summary.normal or not summary.electronic or not summary.ionic:
            raise ValueError(f"stage chain begins with an incomplete pre-NEB image: {chain[0] / image}")
    return chain


def combined_force_history(chain: list[Path], path_name: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    global_iteration = 0
    for stage_index, directory in enumerate(chain, start=1):
        history = neb_projected_force_history(directory, INTERMEDIATE_NAMES)
        status_path = directory / ".run_status"
        status = parse_key_values(status_path) if status_path.is_file() else {}
        stage_kind = status.get("stage")
        if stage_kind not in {"pre_neb", "ci_neb"}:
            stage_kind = "pre_neb" if stage_index == 1 else "ci_neb"
        limit = 0.10 if stage_kind == "pre_neb" else 0.03
        for stage_iteration, maximum, rms in history:
            global_iteration += 1
            rows.append(
                {
                    "path": path_name,
                    "stage": directory.name,
                    "stage_kind": stage_kind,
                    "stage_iteration": stage_iteration,
                    "global_iteration": global_iteration,
                    "max_projected_force_ev_a": f"{maximum:.8f}",
                    "rms_projected_force_ev_a": f"{rms:.8f}",
                    "force_limit_ev_a": f"{limit:.8f}",
                }
            )
    return rows


def analyze_path(directory: Path, path_name: str) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict[str, float]]:
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"final CI stage directory is missing: {directory}")
    stage_manifest = read_manifest(directory / "stage_manifest.tsv")
    if stage_manifest.get("target_stage") != "ci_neb" or stage_manifest.get("lclimb") != "true":
        raise ValueError(f"final result is not a CI-NEB stage: {directory}")
    chain = stage_chain(directory)
    manifest = read_manifest(directory / "path_manifest.tsv")
    if manifest.get("path") != path_name or manifest.get("images") != "4":
        raise ValueError(f"path manifest identity failed: {directory}")
    start_e0 = float(manifest["start_e0_ev"])
    end_e0 = float(manifest["end_e0_ev"])
    for image in ("00", "05"):
        expected_hash = manifest[f"endpoint_{image}_poscar_sha256"]
        actual_hash = sha256(directory / image / "POSCAR")
        if actual_hash != expected_hash:
            raise ValueError(f"fixed endpoint {path_name}/{image} differs from its manifest")
    expected_delta = 0.0 if path_name == "TT_c" else ENDPOINT_DELTA_EV
    if abs((end_e0 - start_e0) - expected_delta) > 5.0e-8:
        raise ValueError(f"endpoint-energy regression failed for {path_name}")

    history = neb_projected_force_history(directory, INTERMEDIATE_NAMES)
    final_force = history[-1][1]
    if final_force > FORCE_LIMIT_EV_A + 1.0e-6:
        raise ValueError(f"final NEB projected force failed for {path_name}: {final_force}")
    status_pass(directory / ".run_status", final_force)

    structures: list[Structure] = []
    energies = [start_e0]
    for image in IMAGE_NAMES:
        structure = final_structure(directory, image)
        if structure.elements != ("Zr", "H") or structure.counts != (96, 1):
            raise ValueError(f"{path_name}/{image} is not Zr H / 96 1")
        if structures:
            require_same_topology(structures[0], structure, f"{path_name}/{image}")
        if minimum_pair_distance(structure) < MIN_PAIR_DISTANCE_A:
            raise ValueError(f"abnormal short bond in {path_name}/{image}")
        structures.append(structure)
        if image in INTERMEDIATE_NAMES:
            for filename in ("OUTCAR", "CONTCAR", "OSZICAR", "vasprun.xml"):
                require_file(directory / image / filename)
            summary = parse_outcar(directory / image / "OUTCAR")
            if summary.nions != 97 or not summary.normal or not summary.electronic or not summary.ionic:
                raise ValueError(f"incomplete image calculation: {path_name}/{image}")
            energies.append(summary.e0_ev)
    energies.append(end_e0)
    if len(energies) != 6:
        raise AssertionError("internal image-energy count error")
    if any(not math.isfinite(value) for value in energies):
        raise ValueError(f"non-finite energy in {path_name}")
    if any(abs(second - first) > 2.0 for first, second in zip(energies, energies[1:])):
        raise ValueError(f"discontinuous energy profile in {path_name}")

    profile_rows: list[dict[str, object]] = []
    geometry_rows: list[dict[str, object]] = []
    cumulative = 0.0
    for index, (image, structure, energy) in enumerate(zip(IMAGE_NAMES, structures, energies)):
        if index == 0:
            step = basal = c_component = 0.0
        else:
            step, vector, _ = minimum_image(
                structure.frac[-1] - structures[index - 1].frac[-1], structure.lattice
            )
            basal = float(np.linalg.norm(vector[:2]))
            c_component = float(vector[2])
            cumulative += step
            if step > 1.0:
                raise ValueError(f"discontinuous H path in {path_name}: step {index} is {step:.6f} A")
        profile_rows.append(
            {
                "path": path_name,
                "image": image,
                "reaction_coordinate_a": f"{cumulative:.8f}",
                "e0_ev": f"{energy:.10f}",
                "relative_to_t_ev": f"{energy - start_e0:.10f}",
            }
        )
        geometry_rows.append(
            {
                "path": path_name,
                "image": image,
                "h_step_a": f"{step:.8f}",
                "h_cumulative_a": f"{cumulative:.8f}",
                "h_step_basal_a": f"{basal:.8f}",
                "h_step_c_a": f"{c_component:.8f}",
                "minimum_pair_a": f"{minimum_pair_distance(structure):.8f}",
            }
        )

    convergence_rows = combined_force_history(chain, path_name)
    maximum_energy = max(energies)
    metrics = {
        "start_e0": start_e0,
        "end_e0": end_e0,
        "barrier_forward": maximum_energy - start_e0,
        "barrier_reverse": maximum_energy - end_e0,
        "final_force": final_force,
    }
    return profile_rows, convergence_rows, geometry_rows, metrics


def font(size: int) -> ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def draw_plot(
    series: list[dict[str, object]],
    output_png: Path,
    output_pdf: Path,
    *,
    title: str,
    xlabel: str,
    ylabel: str,
    threshold: float | None = None,
    secondary_threshold: float | None = None,
    vertical_markers: list[dict[str, object]] | None = None,
) -> None:
    width, height = 1800, 1100
    left, right, top, bottom = 190, 80, 100, 160
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font, tick_font = font(48), font(36), font(28)
    colors = {"TT_c": "#2166ac", "TO": "#b2182b"}
    marker = {"TT_c": "circle", "TO": "square"}
    all_x = [float(point["x"]) for item in series for point in item["points"]]
    all_y = [float(point["y"]) for item in series for point in item["points"]]
    if threshold is not None:
        all_y.append(threshold)
    if secondary_threshold is not None:
        all_y.append(secondary_threshold)
    xmin, xmax = min(all_x), max(all_x)
    ymin = min(0.0, min(all_y))
    ymax = max(all_y)
    if xmax <= xmin or ymax <= ymin:
        raise ValueError("plot data have zero range")
    ypad = 0.08 * (ymax - ymin)
    ymax += ypad
    ymin -= 0.02 * (ymax - ymin)

    def px(value: float) -> float:
        return left + (value - xmin) / (xmax - xmin) * (width - left - right)

    def py(value: float) -> float:
        return height - bottom - (value - ymin) / (ymax - ymin) * (height - top - bottom)

    for index in range(6):
        value = xmin + index * (xmax - xmin) / 5
        x = px(value)
        draw.line((x, top, x, height - bottom), fill="#dddddd", width=2)
        label = f"{value:.2f}" if xmax < 10 else f"{value:.0f}"
        box = draw.textbbox((0, 0), label, font=tick_font)
        draw.text((x - (box[2] - box[0]) / 2, height - bottom + 22), label, fill="black", font=tick_font)
    for index in range(6):
        value = ymin + index * (ymax - ymin) / 5
        y = py(value)
        draw.line((left, y, width - right, y), fill="#dddddd", width=2)
        label = f"{value:.2f}"
        box = draw.textbbox((0, 0), label, font=tick_font)
        draw.text((left - 25 - (box[2] - box[0]), y - (box[3] - box[1]) / 2), label, fill="black", font=tick_font)
    draw.line((left, top, left, height - bottom), fill="black", width=4)
    draw.line((left, height - bottom, width - right, height - bottom), fill="black", width=4)
    if threshold is not None:
        y = py(threshold)
        for x in range(left, width - right, 24):
            draw.line((x, y, min(x + 13, width - right), y), fill="#555555", width=3)
        draw.text((width - right - 270, y - 42), f"Limit = {threshold:.2f}", fill="#333333", font=tick_font)
    if secondary_threshold is not None:
        y = py(secondary_threshold)
        for x in range(left, width - right, 30):
            draw.line((x, y, min(x + 10, width - right), y), fill="#999999", width=2)
        draw.text(
            (width - right - 390, y - 40),
            f"Pre-NEB limit = {secondary_threshold:.2f}",
            fill="#777777",
            font=tick_font,
        )
    for marker_index, item in enumerate(vertical_markers or []):
        x = px(float(item["x"]))
        color = colors[str(item["name"])]
        for y in range(top, height - bottom, 26):
            draw.line((x, y, x, min(y + 13, height - bottom)), fill=color, width=3)
        draw.text(
            (x + 8, top + 115 + marker_index * 42),
            f"{item['name']} CI starts",
            fill=color,
            font=tick_font,
        )
    for item in series:
        name = str(item["name"])
        points = [(px(float(point["x"])), py(float(point["y"]))) for point in item["points"]]
        draw.line(points, fill=colors[name], width=7, joint="curve")
        for x, y in points:
            if marker[name] == "circle":
                draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=colors[name], outline="white", width=2)
            else:
                draw.rectangle((x - 10, y - 10, x + 10, y + 10), fill=colors[name], outline="white", width=2)
    draw.text((left, 24), title, fill="black", font=title_font)
    x_box = draw.textbbox((0, 0), xlabel, font=label_font)
    draw.text(((width - (x_box[2] - x_box[0])) / 2, height - 65), xlabel, fill="black", font=label_font)
    y_layer = Image.new("RGBA", (height, 90), (255, 255, 255, 0))
    y_draw = ImageDraw.Draw(y_layer)
    y_draw.text((10, 15), ylabel, fill="black", font=label_font)
    y_layer = y_layer.rotate(90, expand=True)
    image.paste(y_layer, (25, int((height - y_layer.height) / 2)), y_layer)
    legend_x = width - right - 280
    for offset, name in enumerate(("TT_c", "TO")):
        y = top + 20 + offset * 55
        draw.line((legend_x, y, legend_x + 70, y), fill=colors[name], width=7)
        draw.text((legend_x + 90, y - 20), name, fill="black", font=tick_font)
    image.save(output_png, format="PNG", dpi=(300, 300))
    image.save(output_pdf, format="PDF", resolution=300.0)


def main() -> int:
    args = parse_args()

    profiles: list[dict[str, object]] = []
    convergence: list[dict[str, object]] = []
    geometry: list[dict[str, object]] = []
    metrics: dict[str, dict[str, float]] = {}
    requested = (("TT_c", args.tt_result), ("TO", args.to_result))
    for path_name, directory in requested:
        path_profiles, path_convergence, path_geometry, path_metrics = analyze_path(directory, path_name)
        profiles.extend(path_profiles)
        convergence.extend(path_convergence)
        geometry.extend(path_geometry)
        metrics[path_name] = path_metrics

    self_check = metrics["TO"]["barrier_forward"] - metrics["TO"]["barrier_reverse"]
    if abs(self_check - ENDPOINT_DELTA_EV) > SELF_CHECK_TOLERANCE_EV:
        raise ValueError(f"TO/OT barrier self-check failed: {self_check:.8f} eV")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".analyze_neb.", dir=output.parent) as temporary_name:
        stage = Path(temporary_name)
        atomic_write_tsv(
            stage / "neb_profile.tsv",
            ("path", "image", "reaction_coordinate_a", "e0_ev", "relative_to_t_ev"),
            profiles,
        )
        atomic_write_tsv(
            stage / "neb_convergence.tsv",
            (
                "path", "stage", "stage_kind", "stage_iteration", "global_iteration",
                "max_projected_force_ev_a", "rms_projected_force_ev_a", "force_limit_ev_a",
            ),
            convergence,
        )
        atomic_write_tsv(
            stage / "path_geometry.tsv",
            ("path", "image", "h_step_a", "h_cumulative_a", "h_step_basal_a", "h_step_c_a", "minimum_pair_a"),
            geometry,
        )
        energy_series = []
        for path_name, _ in requested:
            rows = [row for row in profiles if row["path"] == path_name]
            energy_series.append(
                {"name": path_name, "points": [{"x": row["reaction_coordinate_a"], "y": row["relative_to_t_ev"]} for row in rows]}
            )
        force_series = []
        force_markers: list[dict[str, object]] = []
        for path_name, _ in requested:
            rows = [row for row in convergence if row["path"] == path_name]
            force_series.append(
                {"name": path_name, "points": [{"x": row["global_iteration"], "y": row["max_projected_force_ev_a"]} for row in rows]}
            )
            first_ci = next(row for row in rows if row["stage_kind"] == "ci_neb")
            force_markers.append(
                {"name": path_name, "x": float(first_ci["global_iteration"]) - 0.5}
            )
        draw_plot(
            energy_series,
            stage / "neb_energy_profiles.png",
            stage / "neb_energy_profiles.pdf",
            title="Zr96H CI-NEB energy profiles",
            xlabel="H cumulative path length (A)",
            ylabel="Energy relative to T (eV)",
        )
        draw_plot(
            force_series,
            stage / "neb_force_convergence.png",
            stage / "neb_force_convergence.pdf",
            title="NEB projected-force convergence",
            xlabel="NEB ionic iteration",
            ylabel="Maximum projected force (eV/A)",
            threshold=FORCE_LIMIT_EV_A,
            secondary_threshold=0.10,
            vertical_markers=force_markers,
        )
        for source in stage.iterdir():
            target = output / source.name
            if target.exists():
                if target.is_dir():
                    raise ValueError(f"refusing to replace directory with analysis file: {target}")
                target.unlink()
            shutil.move(str(source), str(target))

    print("NEB analysis PASS")
    print(f"TT barrier: {metrics['TT_c']['barrier_forward']:.8f} eV")
    print(f"TO barrier: {metrics['TO']['barrier_forward']:.8f} eV")
    print(f"OT barrier: {metrics['TO']['barrier_reverse']:.8f} eV")
    print(f"TO-OT:      {self_check:.8f} eV")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
