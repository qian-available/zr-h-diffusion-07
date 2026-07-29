#!/bin/bash
#SBATCH -N 1
#SBATCH -n 32
#SBATCH -J zrh_tt_c
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
    echo "images=4"
    echo "accelerator=dcu:4"
    echo "vasp_exe=${vasp_exe}"
    echo "vasp_sha256=${vasp_sha}"
    echo "vtst_version=4.2"
    echo "finished_at=$(date --iso-8601=seconds)"
} >.run_status.tmp
mv .run_status.tmp .run_status
((vasp_exit == 0)) && [[ "${normal}" = yes && "${electronic}" = yes && "${ionic}" = yes && "${force_ok}" = yes ]]
