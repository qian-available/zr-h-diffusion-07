#!/bin/bash
#SBATCH -N 1
#SBATCH -n 32
#SBATCH -J zr96h_o_dcu
#SBATCH --gres=dcu:4
#SBATCH -p xahdnormal
#SBATCH --time=1-00:00:00
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err

set -euo pipefail

cd "${SLURM_SUBMIT_DIR:?submit this file with sbatch}"
for file in POSCAR INCAR KPOINTS POTCAR inputs.sha256; do
    [[ -s "${file}" ]] || { echo "ERROR: missing ${file}" >&2; exit 2; }
done
sha256sum --check --strict --quiet inputs.sha256
for output in OUTCAR CONTCAR OSZICAR vasprun.xml vasp.stdout vasp.stderr .run_status; do
    [[ ! -e "${output}" ]] || { echo "ERROR: prior output exists: ${output}" >&2; exit 3; }
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
normal=no
electronic=no
ionic=no
grep -q "General timing and accounting" OUTCAR 2>/dev/null && normal=yes
grep -q "aborting loop because EDIFF is reached" OUTCAR 2>/dev/null && electronic=yes
grep -q "reached required accuracy" OUTCAR 2>/dev/null && ionic=yes
{
    echo "slurm_job_id=${SLURM_JOB_ID}"
    echo "vasp_exit=${vasp_exit}"
    echo "normal_termination=${normal}"
    echo "electronic_convergence=${electronic}"
    echo "ionic_convergence=${ionic}"
    echo "accelerator=dcu:4"
    echo "vasp_exe=${vasp_exe}"
    echo "finished_at=$(date --iso-8601=seconds)"
} >.run_status.tmp
mv .run_status.tmp .run_status
((vasp_exit == 0)) && [[ "${normal}" = yes && "${electronic}" = yes && "${ionic}" = yes ]]
