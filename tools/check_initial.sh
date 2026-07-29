#!/bin/bash
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
root="$(cd "${script_dir}/.." && pwd -P)"
summary="${root}/01_initial/initial_summary.tsv"
tmp="${summary}.tmp.$$"
failed=0

max_force() {
    awk '
        /TOTAL-FORCE \(eV\/Angst\)/ {inside=1; count=0; maximum=0; next}
        inside && /^[[:space:]]*-+/ {
            if (count > 0) {last=maximum; inside=0}
            next
        }
        inside && NF >= 6 && $1 ~ /^[-+0-9.]/ {
            fx=$(NF-2)+0; fy=$(NF-1)+0; fz=$NF+0
            value=sqrt(fx*fx+fy*fy+fz*fz)
            if (value > maximum) maximum=value
            count++
        }
        END {
            if (last == "") exit 1
            printf "%.8f", last
        }
    ' "$1"
}

printf "case\tstatus\tnions\te0_ev\tmax_force_ev_a\n" >"${tmp}"
cases=(
    "01_zr96_static/retry_dcu_01:96:static"
    "02_h2_relax:2:relax"
    "03_t_relax/retry_dcu_01:97:relax"
    "04_o_relax/retry_dcu_01:97:relax"
)

for record in "${cases[@]}"; do
    IFS=: read -r name expected_nions kind <<<"${record}"
    directory="${root}/01_initial/${name}"
    outcar="${directory}/OUTCAR"
    status=PASS
    nions=""
    energy=""
    force=""
    if [[ ! -s "${outcar}" ]]; then
        status="FAIL_missing_OUTCAR"
    else
        grep -q "General timing and accounting" "${outcar}" || status="FAIL_abnormal_end"
        grep -q "aborting loop because EDIFF is reached" "${outcar}" || status="FAIL_electronic"
        if [[ "${kind}" = relax ]]; then
            grep -q "reached required accuracy" "${outcar}" || status="FAIL_ionic"
        fi
        nions="$(sed -n 's/.*NIONS *= *\([0-9][0-9]*\).*/\1/p' "${outcar}" | head -n 1)"
        [[ "${nions}" = "${expected_nions}" ]] || status="FAIL_nions"
        energy="$(awk '/energy\(sigma->0\)/ {value=$NF} END {print value}' "${outcar}")"
        force="$(max_force "${outcar}" 2>/dev/null || true)"
        [[ -n "${energy}" ]] || status="FAIL_energy"
        [[ -n "${force}" ]] || status="FAIL_force"
        if [[ "${kind}" = relax && -n "${force}" ]]; then
            awk -v value="${force}" 'BEGIN {exit !(value <= 0.010001)}' ||
                status="FAIL_force_limit"
        fi
    fi
    [[ "${status}" = PASS ]] || failed=1
    printf "%s\t%s\t%s\t%s\t%s\n" \
        "${name}" "${status}" "${nions}" "${energy}" "${force}" >>"${tmp}"
done

mv "${tmp}" "${summary}"
cat "${summary}"
if ((failed)); then
    echo "Initial numerical check FAIL" >&2
    exit 1
fi
echo "Initial numerical check PASS"
echo "T/O site identity is not decided here; inspect both CONTCAR files before endpoints."
