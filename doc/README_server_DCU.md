# Zr96-H DCU calculation inputs

This package contains explicit VASP input directories for a simplified Zr96-H interstitial diffusion
workflow on the SCNet Northwest DCU partition. It contains no POTCAR and no VASP output.

## Directory layout

```text
07_h_diffusion_quickstart/
├── README.md
├── 01_initial/
│   ├── 01_zr96_static/
│   │   └── retry_dcu_01/
│   ├── 02_h2_relax/
│   ├── 03_t_relax/
│   │   └── retry_dcu_01/
│   └── 04_o_relax/
│       └── retry_dcu_01/
├── 02_endpoints/
├── 03_neb/
└── tools/
```

The original four directories retain the first CPU input set. The three `retry_dcu_01` directories are the
active DCU input set. `02_h2_relax` has no retry because the completed H2 result is retained.

## Active tasks

| Directory | System | Task | Main settings |
| --- | --- | --- | --- |
| `01_zr96_static/retry_dcu_01` | Zr96 | fixed-structure static | `450 eV`, `4x4x3`, `EDIFF=1E-6`, `LREAL=Auto` |
| `03_t_relax/retry_dcu_01` | Zr96H, initial T site | fixed-cell relaxation | `450 eV`, `4x4x3`, `EDIFFG=-0.01 eV/A` |
| `04_o_relax/retry_dcu_01` | Zr96H, initial O site | fixed-cell relaxation | `450 eV`, `4x4x3`, `EDIFFG=-0.01 eV/A` |
| `03_neb/01_tt_c` | Zr96H, T→T | ordinary NEB pre-relaxation | `4 images`, `EDIFFG=-0.10 eV/A` |
| `03_neb/02_to` | Zr96H, T→O | ordinary NEB pre-relaxation | `4 images`, `EDIFFG=-0.10 eV/A` |

Each active directory contains `POSCAR`, `INCAR`, `KPOINTS`, and `job.slurm`. POTCAR and
`inputs.sha256` are generated in private SCNet storage by `tools/assemble_potcars.sh`.

## Potential metadata

```text
Zr_sv: PAW_PBE Zr_sv 04Jan2005
SHA-256: 25aed69cb10325f9d37c5c68912b61a17387d1f8e4f1d804860ffa10c8a4bf76

H: PAW_PBE H 15Jun2001
SHA-256: b9ed9e0fd4e660c858a39f59be6bb91671733b1136a5cd56b772198ffb3ec7fb
```

The combined potential order for Zr96H is `Zr_sv + H`, matching `Zr H / 96 1` in POSCAR.

## DCU resources

The active Slurm scripts request one `xahdnormal` node, 32 CPU tasks, and four DCUs. They load Intel
2020, IntelMPI 2020, DTK 22.10, and source
`/work/home/liuzhixiao/software/dcu-port-2Feb2023-all/env.sh`. Four MPI processes are bound to
`HIP_VISIBLE_DEVICES=0..3`.

The first production submission is the T-site retry. O-site and Zr96 jobs are submitted only after the T job
demonstrates a working DCU environment. Existing CPU outputs are not overwritten.

After all three initial DCU jobs and the retained H2 job passed, `tools/check_initial.sh` generated
`01_initial/initial_summary.tsv`; subsequent structural analysis confirmed distinct four-coordinate T and
six-coordinate O final states.

The SCNet DCU executable was subsequently confirmed to contain VTST 4.2 and `LCLIMB`. Each path is now
run as an ordinary-NEB pre-relaxation to `0.10 eV/A`, followed by an independently prepared CI-NEB
stage to `0.03 eV/A`. See `03_neb/README.md`; no stage is submitted automatically.

This reduced setting is intended for workflow demonstration and does not establish converged Zr-H defect or
diffusion energetics.
