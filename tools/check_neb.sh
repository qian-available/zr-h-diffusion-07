#!/bin/bash
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
root="$(cd "${script_dir}/.." && pwd -P)"
expected_vasp_sha="a1b25c7ebf384a3147aa3ad8f77ba5fa020d8eacb8755f81e56d04cafabb1b6f"

usage() {
    echo "Usage: bash tools/check_neb.sh --stage pre|ci PATH [PATH ...]" >&2
}

[[ "${1:-}" = "--stage" && -n "${2:-}" ]] || { usage; exit 2; }
stage_arg="$2"
shift 2
(($#)) || { usage; exit 2; }
case "${stage_arg}" in
    pre)
        expected_stage="pre_neb"
        expected_limit="0.10"
        expected_lclimb="false"
        ;;
    ci)
        expected_stage="ci_neb"
        expected_limit="0.03"
        expected_lclimb="true"
        ;;
    *)
        usage
        exit 2
        ;;
esac

failed=0
printf "path\tstage\tstatus\tcomplete_images\tfinal_neb_force_ev_a\n"
for relative in "$@"; do
    [[ "${relative}" != /* && "${relative}" != *".."* ]] || {
        echo "ERROR: path must be relative to 03_neb: ${relative}" >&2
        exit 2
    }
    directory="${root}/03_neb/${relative#./}"
    status="PASS"
    complete=0
    final_force=""
    force_values=""
    [[ -d "${directory}" ]] || status="FAIL_missing_directory"
    [[ -s "${directory}/INCAR" ]] || status="FAIL_missing_incar"
    [[ -s "${directory}/path_manifest.tsv" ]] || status="FAIL_missing_manifest"
    [[ -s "${directory}/.run_status" ]] || status="FAIL_missing_run_status"
    [[ -s "${directory}/vasp.stdout" ]] || status="FAIL_missing_stdout"

    if [[ -s "${directory}/INCAR" ]]; then
        incar_lclimb="$(awk -F= 'toupper($1) ~ /^[[:space:]]*LCLIMB[[:space:]]*$/ {
            gsub(/[[:space:].]/, "", $2); print tolower($2)
        }' "${directory}/INCAR")"
        incar_ediffg="$(awk -F= 'toupper($1) ~ /^[[:space:]]*EDIFFG[[:space:]]*$/ {
            gsub(/[[:space:]]/, "", $2); print $2
        }' "${directory}/INCAR")"
        [[ "${incar_lclimb}" = "${expected_lclimb}" ]] || status="FAIL_lclimb"
        awk -v value="${incar_ediffg}" -v limit="${expected_limit}" \
            'BEGIN {exit !(value + 0.0 == -limit)}' || status="FAIL_ediffg"
    fi

    for endpoint in 00 05; do
        expected_hash="$(awk -F '\t' -v key="endpoint_${endpoint}_poscar_sha256" \
            '$1 == key {print $2}' "${directory}/path_manifest.tsv" 2>/dev/null)"
        actual_hash="$(sha256sum "${directory}/${endpoint}/POSCAR" 2>/dev/null | awk '{print $1}')"
        [[ -n "${expected_hash}" && "${actual_hash}" = "${expected_hash}" ]] || status="FAIL_endpoint_hash"
    done

    for image in 01 02 03 04; do
        outcar="${directory}/${image}/OUTCAR"
        image_ok=yes
        for filename in OUTCAR CONTCAR OSZICAR vasprun.xml; do
            [[ -s "${directory}/${image}/${filename}" ]] || image_ok=no
        done
        if [[ "${image_ok}" != yes ]]; then
            status="FAIL_missing_image"
            continue
        fi
        grep -q "General timing and accounting" "${outcar}" || status="FAIL_abnormal_end"
        grep -q "aborting loop because EDIFF is reached" "${outcar}" || status="FAIL_electronic"
        nions="$(sed -n 's/.*NIONS *= *\([0-9][0-9]*\).*/\1/p' "${outcar}" | head -n 1)"
        [[ "${nions}" = 97 ]] || status="FAIL_nions"
        energy="$(awk '/energy\(sigma->0\)/ {value=$NF} END {print value}' "${outcar}")"
        [[ -n "${energy}" ]] || status="FAIL_energy"
        image_force="$(awk '/FORCES: max atom, RMS/ {value=$(NF-1)}
            END {if (value != "") print value}' "${outcar}")"
        if [[ -n "${image_force}" ]]; then
            force_values="${force_values}"$'\n'"${image_force}"
        else
            status="FAIL_neb_force"
        fi
        grep -q "reached required accuracy" "${outcar}" || status="FAIL_ionic"
        complete=$((complete + 1))
    done

    final_force="$(printf "%s\n" "${force_values}" | awk 'NF && !seen {maximum=$1; seen=1}
        NF && $1 > maximum {maximum=$1} END {if (seen) printf "%.8f", maximum}')"
    [[ -n "${final_force}" ]] || status="FAIL_neb_force"
    if [[ -n "${final_force}" ]]; then
        awk -v value="${final_force}" -v limit="${expected_limit}" \
            'BEGIN {exit !(value <= limit + 0.000001)}' || status="FAIL_force_limit"
    fi

    if [[ -s "${directory}/.run_status" ]]; then
        for required in \
            "vasp_exit=0" \
            "normal_termination=yes" \
            "electronic_convergence=yes" \
            "stage=${expected_stage}" \
            "force_limit_ev_a=${expected_limit}" \
            "lclimb=${expected_lclimb}" \
            "vasp_sha256=${expected_vasp_sha}" \
            "vtst_version=4.2"; do
            grep -Fqx "${required}" "${directory}/.run_status" || status="FAIL_run_status"
        done
        recorded_force="$(awk -F= '$1 == "neb_force_ev_a" {print $2}' \
            "${directory}/.run_status")"
        if [[ -n "${recorded_force}" ]]; then
            awk -v recorded="${recorded_force}" -v actual="${final_force}" \
                'BEGIN {delta=recorded-actual; if (delta < 0) delta=-delta;
                    exit !(delta <= 0.000005)}' || status="FAIL_run_status_force"
            grep -Fqx "neb_force_limit_pass=yes" "${directory}/.run_status" ||
                status="FAIL_run_status"
        else
            echo "WARNING: legacy .run_status lacks NEB force; verified image OUTCAR files instead: ${relative}" >&2
        fi
    fi
    if [[ "${stage_arg}" = ci ]]; then
        [[ -s "${directory}/stage_manifest.tsv" ]] || status="FAIL_missing_stage_manifest"
        if [[ -s "${directory}/stage_manifest.tsv" ]]; then
            grep -q $'^target_stage\tci_neb\t' "${directory}/stage_manifest.tsv" ||
                status="FAIL_stage_manifest"
        fi
    fi

    [[ "${status}" = PASS ]] || failed=1
    tmp="${directory}/check_summary.tsv.tmp.$$"
    {
        printf "path\tstage\tstatus\tcomplete_images\tfinal_neb_force_ev_a\n"
        printf "%s\t%s\t%s\t%s\t%s\n" "${relative}" "${expected_stage}" \
            "${status}" "${complete}" "${final_force}"
    } >"${tmp}"
    mv "${tmp}" "${directory}/check_summary.tsv"
    printf "%s\t%s\t%s\t%s\t%s\n" "${relative}" "${expected_stage}" \
        "${status}" "${complete}" "${final_force}"
done

if ((failed)); then
    echo "NEB stage numerical check FAIL" >&2
    exit 1
fi
echo "NEB stage numerical check PASS"
