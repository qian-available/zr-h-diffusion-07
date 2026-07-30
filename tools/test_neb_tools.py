#!/usr/bin/env python3
"""No-VASP regression tests for the 07 symmetry/NEB utilities."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from neb_common import Structure, h_neighbors, require_file, require_same_topology
from prepare_neb_paths import JOB_TEMPLATE, build_symmetry_endpoint, prepare_path


TOOLS = Path(__file__).resolve().parent
WORKFLOW = TOOLS.parent
DOWNLOADED = WORKFLOW.parent / "07_H_diffussion_result" / "ZrH07_initial_results_20260728" / "07_h_diffusion_quickstart"


def fake_outcar(energy: float, forces: tuple[float, ...]) -> str:
    force_rows = "\n".join(
        f" {index + 1:4d}.0 0.0 0.0 0.000000 0.000000 0.000000" for index in range(97)
    )
    force_summary = "".join(
        f" FORCES: max atom, RMS     {force:.6f}    {force / 2:.6f}\n"
        for force in forces
    )
    return (
        " NIONS = 97\n"
        f" energy(sigma->0) = {energy:.10f}\n"
        " TOTAL-FORCE (eV/Angst)\n"
        " ------------------------------------------------------------\n"
        f"{force_rows}\n"
        " ------------------------------------------------------------\n"
        f"{force_summary}"
        " aborting loop because EDIFF is reached\n"
        " reached required accuracy - stopping structural energy minimisation\n"
        " General timing and accounting informations for this job:\n"
    )


def fake_status(stage: str, force: float, limit: float, lclimb: bool) -> str:
    return (
        "slurm_job_id=12345\n"
        "vasp_exit=0\n"
        "normal_termination=yes\n"
        "electronic_convergence=yes\n"
        "ionic_convergence=yes\n"
        f"neb_force_ev_a={force:.6f}\n"
        f"stage={stage}\n"
        f"force_limit_ev_a={limit:.2f}\n"
        f"lclimb={str(lclimb).lower()}\n"
        "neb_force_limit_pass=yes\n"
        "images=4\n"
        "accelerator=dcu:4\n"
        "vasp_exe=/work/home/liuzhixiao/software/dcu-port-2Feb2023-all/bin/vasp_std\n"
        "vasp_sha256=a1b25c7ebf384a3147aa3ad8f77ba5fa020d8eacb8755f81e56d04cafabb1b6f\n"
        "vtst_version=4.2\n"
    )


def populate_stage(directory: Path, start: float, relative: tuple[float, ...], *, stage: str, forces: tuple[float, ...]) -> None:
    for image, delta in zip(("01", "02", "03", "04"), relative):
        shutil.copy2(directory / image / "POSCAR", directory / image / "CONTCAR")
        (directory / image / "OUTCAR").write_text(
            fake_outcar(start + delta, forces),
            encoding="utf-8",
        )
        (directory / image / "OSZICAR").write_text("synthetic regression fixture\n", encoding="utf-8")
        (directory / image / "vasprun.xml").write_text("<synthetic/>\n", encoding="utf-8")
    (directory / "vasp.stdout").write_text(
        "reached required accuracy\n",
        encoding="utf-8",
    )
    limit = 0.10 if stage == "pre_neb" else 0.03
    (directory / ".run_status").write_text(
        fake_status(stage, forces[-1], limit, stage == "ci_neb"),
        encoding="utf-8",
    )


class GeometryFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        if not DOWNLOADED.is_dir():
            self.skipTest("downloaded T/O regression data are unavailable")
        from neb_common import parse_poscar

        t_dir = DOWNLOADED / "01_initial/03_t_relax/retry_dcu_01"
        self.t_initial = parse_poscar(t_dir / "POSCAR")
        self.t_final = parse_poscar(t_dir / "CONTCAR")

    def test_missing_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                require_file(Path(directory) / "missing")

    def test_wrong_atom_count_fails(self) -> None:
        bad = Structure("bad", self.t_final.lattice, ("Zr", "H"), (95, 1), self.t_final.frac[:-1])
        with self.assertRaises(ValueError):
            h_neighbors(bad)

    def test_element_order_and_lattice_mismatch_fail(self) -> None:
        wrong_order = Structure("bad", self.t_final.lattice, ("H", "Zr"), (1, 96), self.t_final.frac)
        with self.assertRaises(ValueError):
            require_same_topology(self.t_final, wrong_order, "wrong order")
        lattice = self.t_final.lattice.copy()
        lattice[2, 2] += 0.01
        wrong_lattice = Structure("bad", lattice, self.t_final.elements, self.t_final.counts, self.t_final.frac)
        with self.assertRaises(ValueError):
            require_same_topology(self.t_final, wrong_lattice, "wrong lattice")

    def test_non_bijective_mapping_fails(self) -> None:
        bad_frac = self.t_initial.frac.copy()
        bad_frac[1] = bad_frac[0]
        bad = Structure("bad", self.t_initial.lattice, self.t_initial.elements, self.t_initial.counts, bad_frac)
        with self.assertRaises(ValueError):
            build_symmetry_endpoint(bad, self.t_final)

    def test_abnormal_short_bond_fails(self) -> None:
        endpoint, _, _ = build_symmetry_endpoint(self.t_initial, self.t_final)
        bad_frac = endpoint.frac.copy()
        bad_frac[-1] = bad_frac[0]
        bad = Structure("bad", endpoint.lattice, endpoint.elements, endpoint.counts, bad_frac)
        with self.assertRaises(ValueError):
            prepare_path("TT_c", self.t_final, bad, -1.0, -1.0, "a", "b")


class AnalyzerEndToEndTest(unittest.TestCase):
    def test_generated_pre_neb_settings(self) -> None:
        for relative in ("01_tt_c", "02_to"):
            incar = (WORKFLOW / "03_neb" / relative / "INCAR").read_text(encoding="utf-8")
            self.assertIn("EDIFFG = -0.10", incar)
            self.assertIn("LCLIMB = .FALSE.", incar)
        self.assertIn('stage="pre_neb"', JOB_TEMPLATE)
        self.assertIn('force_limit="0.10"', JOB_TEMPLATE)
        self.assertIn("#SBATCH -N 1", JOB_TEMPLATE)
        self.assertIn("expected_nodes=1", JOB_TEMPLATE)
        self.assertIn('"${image}/OUTCAR"', JOB_TEMPLATE)
        self.assertNotIn("END {print value}' vasp.stdout", JOB_TEMPLATE)
        self.assertIn("known_vasp_images_error_29", JOB_TEMPLATE)

    def test_staged_cineb_generation_and_analysis(self) -> None:
        source = WORKFLOW / "03_neb"
        if not (source / "01_tt_c").is_dir():
            self.skipTest("generated NEB inputs are unavailable")
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            result_root = directory / "03_neb"
            result_root.mkdir()
            profiles = {
                "01_tt_c": (-821.95359478, (0.060, 0.130, 0.100, 0.045)),
                "02_to": (-821.95359478, (0.120, 0.410, 0.330, 0.150)),
            }
            final_stages: dict[str, Path] = {}
            for path_name, (start, relative) in profiles.items():
                target = result_root / path_name
                shutil.copytree(
                    source / path_name,
                    target,
                    ignore=shutil.ignore_patterns("ci_*", "pre_restart_*"),
                )
                populate_stage(
                    target,
                    start,
                    relative,
                    stage="pre_neb",
                    forces=(0.180, 0.125, 0.090),
                )
                ci_target = target / "ci_01"
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(TOOLS / "prepare_neb_stage.py"),
                        "--source",
                        str(target),
                        "--target",
                        str(ci_target),
                        "--target-stage",
                        "ci",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("EDIFFG = -0.03", (ci_target / "INCAR").read_text(encoding="utf-8"))
                self.assertIn("LCLIMB = .TRUE.", (ci_target / "INCAR").read_text(encoding="utf-8"))
                ci_job = (ci_target / "job.slurm").read_text(encoding="utf-8")
                self.assertIn("#SBATCH -N 4", ci_job)
                self.assertIn("#SBATCH -n 128", ci_job)
                self.assertNotIn("#SBATCH --ntasks-per-node=24", ci_job)
                self.assertIn("#SBATCH --gres=dcu:4", ci_job)
                self.assertIn("expected_nodes=4", ci_job)
                self.assertIn("ranks_per_image=$((total_ranks / image_count))", ci_job)
                self.assertNotIn("NCORE =", (ci_target / "INCAR").read_text(encoding="utf-8"))
                self.assertFalse((ci_target / "POTCAR").exists())
                self.assertEqual(
                    (target / "00/POSCAR").read_bytes(),
                    (ci_target / "00/POSCAR").read_bytes(),
                )
                self.assertEqual(
                    (target / "03/CONTCAR").read_bytes(),
                    (ci_target / "03/POSCAR").read_bytes(),
                )
                repeated = subprocess.run(
                    [
                        sys.executable,
                        str(TOOLS / "prepare_neb_stage.py"),
                        "--source",
                        str(target),
                        "--target",
                        str(ci_target),
                        "--target-stage",
                        "ci",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                )
                self.assertNotEqual(repeated.returncode, 0)
                self.assertIn("refusing to overwrite", repeated.stderr)
                populate_stage(
                    ci_target,
                    start,
                    relative,
                    stage="ci_neb",
                    forces=(0.070, 0.045, 0.020),
                )
                final_stages[path_name] = ci_target
            output = directory / "analysis"
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "analyze_neb.py"),
                    "--tt-result",
                    str(final_stages["01_tt_c"]),
                    "--to-result",
                    str(final_stages["02_to"]),
                    "--output-dir",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            expected = (
                "neb_profile.tsv",
                "neb_convergence.tsv",
                "path_geometry.tsv",
                "neb_energy_profiles.png",
                "neb_energy_profiles.pdf",
                "neb_force_convergence.png",
                "neb_force_convergence.pdf",
            )
            for filename in expected:
                self.assertGreater((output / filename).stat().st_size, 100)
            self.assertIn("TO-OT:      0.06290400 eV", completed.stdout)
            convergence = (output / "neb_convergence.tsv").read_text(encoding="utf-8")
            self.assertIn("pre_neb", convergence)
            self.assertIn("ci_neb", convergence)
            preview = os.environ.get("NEB_TEST_PREVIEW_DIR")
            if preview:
                preview_dir = Path(preview)
                preview_dir.mkdir(parents=True, exist_ok=True)
                for filename in ("neb_energy_profiles.png", "neb_force_convergence.png"):
                    shutil.copy2(output / filename, preview_dir / filename)

    def test_ci_generation_rejects_pre_neb_above_limit(self) -> None:
        source = WORKFLOW / "03_neb/01_tt_c"
        if not source.is_dir():
            self.skipTest("generated NEB inputs are unavailable")
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            pre = directory / "pre"
            shutil.copytree(source, pre)
            populate_stage(
                pre,
                -821.95359478,
                (0.060, 0.130, 0.100, 0.045),
                stage="pre_neb",
                forces=(0.180, 0.1001),
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "prepare_neb_stage.py"),
                    "--source",
                    str(pre),
                    "--target",
                    str(directory / "ci"),
                    "--target-stage",
                    "ci",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("exceeds 0.10", completed.stderr)

    def test_ci_generation_accepts_legacy_empty_stdout_force_status(self) -> None:
        source = WORKFLOW / "03_neb/01_tt_c"
        if not source.is_dir():
            self.skipTest("generated NEB inputs are unavailable")
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            pre = directory / "pre"
            shutil.copytree(source, pre)
            populate_stage(
                pre,
                -821.95359478,
                (0.060, 0.130, 0.100, 0.045),
                stage="pre_neb",
                forces=(0.180, 0.090),
            )
            status_path = pre / ".run_status"
            status = status_path.read_text(encoding="utf-8")
            status = status.replace("ionic_convergence=yes", "ionic_convergence=no")
            status = status.replace("neb_force_ev_a=0.090000", "neb_force_ev_a=")
            status = status.replace("neb_force_limit_pass=yes", "neb_force_limit_pass=no")
            status_path.write_text(status, encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "prepare_neb_stage.py"),
                    "--source",
                    str(pre),
                    "--target",
                    str(directory / "ci"),
                    "--target-stage",
                    "ci",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_ci_generation_accepts_known_hdf5_postrun_error(self) -> None:
        source = WORKFLOW / "03_neb/01_tt_c"
        if not source.is_dir():
            self.skipTest("generated NEB inputs are unavailable")
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            pre = directory / "pre"
            shutil.copytree(source, pre)
            populate_stage(
                pre,
                -821.95359478,
                (0.060, 0.130, 0.100, 0.045),
                stage="pre_neb",
                forces=(0.180, 0.090),
            )
            status_path = pre / ".run_status"
            status = status_path.read_text(encoding="utf-8")
            status = status.replace("vasp_exit=0", "vasp_exit=1")
            status = status.replace("neb_force_ev_a=0.090000", "neb_force_ev_a=")
            status = status.replace("neb_force_limit_pass=yes", "neb_force_limit_pass=no")
            status_path.write_text(status, encoding="utf-8")
            (pre / "vasp.stderr").write_text(
                "internal error in: vhdf5.F at line: 110\n"
                "HDF5 call in vhdf5.F:110 produced error: 29\n",
                encoding="utf-8",
            )
            target = directory / "ci"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "prepare_neb_stage.py"),
                    "--source",
                    str(pre),
                    "--target",
                    str(target),
                    "--target-stage",
                    "ci",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = (target / "stage_manifest.tsv").read_text(encoding="utf-8")
            self.assertIn("source_vasp_exit\t1\t", manifest)
            self.assertIn(
                "source_hdf5_postrun_error\tknown_vasp_images_error_29\t",
                manifest,
            )

    def test_ci_generation_rejects_unknown_nonzero_exit(self) -> None:
        source = WORKFLOW / "03_neb/01_tt_c"
        if not source.is_dir():
            self.skipTest("generated NEB inputs are unavailable")
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            pre = directory / "pre"
            shutil.copytree(source, pre)
            populate_stage(
                pre,
                -821.95359478,
                (0.060, 0.130, 0.100, 0.045),
                stage="pre_neb",
                forces=(0.180, 0.090),
            )
            status_path = pre / ".run_status"
            status = status_path.read_text(encoding="utf-8").replace(
                "vasp_exit=0",
                "vasp_exit=1",
            )
            status_path.write_text(status, encoding="utf-8")
            (pre / "vasp.stderr").write_text("unrelated MPI failure\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "prepare_neb_stage.py"),
                    "--source",
                    str(pre),
                    "--target",
                    str(directory / "ci"),
                    "--target-stage",
                    "ci",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("vasp_exit='1'", completed.stderr)

    def test_interrupted_pre_neb_can_continue_without_run_status(self) -> None:
        source = WORKFLOW / "03_neb/01_tt_c"
        if not source.is_dir():
            self.skipTest("generated NEB inputs are unavailable")
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            pre = directory / "pre"
            shutil.copytree(source, pre)
            populate_stage(
                pre,
                -821.95359478,
                (0.060, 0.130, 0.100, 0.045),
                stage="pre_neb",
                forces=(0.250, 0.180),
            )
            (pre / ".run_status").unlink()
            restart = directory / "pre_restart_01"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "prepare_neb_stage.py"),
                    "--source",
                    str(pre),
                    "--target",
                    str(restart),
                    "--target-stage",
                    "pre",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            incar = (restart / "INCAR").read_text(encoding="utf-8")
            self.assertIn("EDIFFG = -0.10", incar)
            self.assertIn("LCLIMB = .FALSE.", incar)
            self.assertEqual(
                (pre / "04/CONTCAR").read_bytes(),
                (restart / "04/POSCAR").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
