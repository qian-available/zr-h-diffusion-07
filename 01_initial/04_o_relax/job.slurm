#!/bin/bash
#SBATCH --partition=xahcnormal
#SBATCH --nodes=1
#SBATCH --ntasks=32
#SBATCH --ntasks-per-node=32
#SBATCH --cpus-per-task=1
#SBATCH --time=3-00:00:00
#SBATCH --job-name=zr96h_o
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
module load compiler/intel/2017.5.239
module load mpi/intelmpi/2017.4.239
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export MKL_DEBUG_CPU_TYPE=5 MKL_CBWR=AVX2 I_MPI_PIN_DOMAIN=numa
vasp_bin="${VASP_BIN:-/work/home/liuzhixiao/software/vasp.6.3.2/bin/vasp_std}"
[[ -x "${vasp_bin}" ]] || { echo "ERROR: unavailable VASP: ${vasp_bin}" >&2; exit 2; }

set +e
srun --mpi=pmi2 "${vasp_bin}" >vasp.stdout 2>vasp.stderr
vasp_exit=$?
set -e
normal=no
electronic=no
grep -q "General timing and accounting" OUTCAR 2>/dev/null && normal=yes
grep -q "aborting loop because EDIFF is reached" OUTCAR 2>/dev/null && electronic=yes
{
    echo "slurm_job_id=${SLURM_JOB_ID}"
    echo "vasp_exit=${vasp_exit}"
    echo "normal_termination=${normal}"
    echo "electronic_convergence=${electronic}"
    echo "finished_at=$(date --iso-8601=seconds)"
} >.run_status.tmp
mv .run_status.tmp .run_status
((vasp_exit == 0)) && [[ "${normal}" = yes && "${electronic}" = yes ]]
