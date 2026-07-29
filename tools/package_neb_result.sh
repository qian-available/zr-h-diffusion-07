#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
root="$(cd "${script_dir}/.." && pwd -P)"

usage() {
    cat <<'EOF'
Usage:
  bash tools/package_neb_result.sh 03_neb/01_tt_c [OUTPUT_DIR]
  bash tools/package_neb_result.sh 03_neb/01_tt_c/ci_01 [OUTPUT_DIR]

The stage must have passed tools/check_neb.sh. The archive contains the
stage's inputs and VASP evidence but never POTCAR, WAVECAR, or CHGCAR.
OUTPUT_DIR defaults to the parent of 07_h_diffusion_quickstart.
EOF
}

[[ $# -ge 1 && $# -le 2 ]] || { usage >&2; exit 2; }
relative="${1#./}"
[[ "${relative}" == 03_neb/* && "${relative}" != *".."* && "${relative}" != /* ]] || {
    echo "ERROR: stage must be a relative path below 03_neb: ${relative}" >&2
    exit 2
}
stage_dir="${root}/${relative}"
[[ -d "${stage_dir}" ]] || { echo "ERROR: missing stage directory: ${stage_dir}" >&2; exit 2; }
[[ -s "${stage_dir}/.run_status" ]] || {
    echo "ERROR: missing completed stage status: ${stage_dir}/.run_status" >&2
    exit 2
}

status_value() {
    local key="$1"
    awk -F= -v key="${key}" '$1 == key {print substr($0, index($0, "=") + 1)}' \
        "${stage_dir}/.run_status"
}

stage="$(status_value stage)"
job_id="$(status_value slurm_job_id)"
case "${stage}" in
    pre_neb)
        check_stage="pre"
        ;;
    ci_neb)
        check_stage="ci"
        ;;
    *)
        echo "ERROR: unsupported or missing stage in .run_status: ${stage:-<empty>}" >&2
        exit 2
        ;;
esac
[[ "${job_id}" =~ ^[0-9]+$ ]] || {
    echo "ERROR: invalid slurm_job_id in .run_status: ${job_id:-<empty>}" >&2
    exit 2
}

check_relative="${relative#03_neb/}"
bash "${script_dir}/check_neb.sh" --stage "${check_stage}" "${check_relative}"

output_dir="${2:-$(cd "${root}/.." && pwd -P)}"
mkdir -p "${output_dir}"
output_dir="$(cd "${output_dir}" && pwd -P)"
case "${output_dir}" in
    "${stage_dir}"|"${stage_dir}/"*)
        echo "ERROR: output directory cannot be inside the stage being packaged" >&2
        exit 2
        ;;
esac

slug="${relative//\//_}"
date_stamp="$(date +%Y%m%d)"
archive_name="ZrH07_${slug}_${stage}_job${job_id}_${date_stamp}.tar.gz"
archive="${output_dir}/${archive_name}"
checksum="${archive}.sha256"
[[ ! -e "${archive}" && ! -e "${checksum}" ]] || {
    echo "ERROR: refusing to overwrite existing package: ${archive}" >&2
    exit 3
}

files=()
while IFS= read -r -d '' path; do
    files+=("${path#${root}/}")
done < <(
    find "${stage_dir}" -maxdepth 1 -type f \
        ! -name POTCAR ! -name WAVECAR ! -name CHGCAR ! -name '*.tmp' \
        -print0 | sort -z
)
for image in 00 01 02 03 04 05; do
    image_dir="${stage_dir}/${image}"
    [[ -d "${image_dir}" ]] || {
        echo "ERROR: missing image directory: ${image_dir}" >&2
        exit 2
    }
    while IFS= read -r -d '' path; do
        files+=("${path#${root}/}")
    done < <(
        find "${image_dir}" -maxdepth 1 -type f \
            ! -name POTCAR ! -name WAVECAR ! -name CHGCAR ! -name '*.tmp' \
            -print0 | sort -z
    )
done
((${#files[@]} > 0)) || { echo "ERROR: no files selected" >&2; exit 2; }

tmp_archive="${archive}.tmp.$$"
tmp_checksum="${checksum}.tmp.$$"
trap 'rm -f -- "${tmp_archive}" "${tmp_checksum}"' EXIT
tar -czf "${tmp_archive}" -C "${root}" -- "${files[@]}"

for forbidden in POTCAR WAVECAR CHGCAR; do
    if tar -tzf "${tmp_archive}" |
        awk -F/ -v name="${forbidden}" '$NF == name {found=1} END {exit !found}'; then
        echo "ERROR: forbidden file entered package: ${forbidden}" >&2
        exit 2
    fi
done
mv "${tmp_archive}" "${archive}"
(
    cd "${output_dir}"
    sha256sum "${archive_name}" >"${tmp_checksum}"
)
mv "${tmp_checksum}" "${checksum}"
chmod 600 "${archive}" "${checksum}"
trap - EXIT

echo "RESULT_PACKAGE=${archive}"
echo "RESULT_SHA256=${checksum}"
echo "FILES_PACKAGED=${#files[@]}"
echo "POTCAR_WAVECAR_CHGCAR_EXCLUDED=yes"
echo "NEB result package PASS"
