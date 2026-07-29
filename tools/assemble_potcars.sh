#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
root="$(cd "${script_dir}/.." && pwd -P)"
zr_source="${ZR_SV_POTCAR:-/work/home/liuzhixiao/Zr-ckj/private_potentials/Zr_sv_04Jan2005/POTCAR}"
h_source="${H_POTCAR:-/work/home/liuzhixiao/psudopotential/PAW-GGA-PBE/H/POTCAR}"
zr_expected="25aed69cb10325f9d37c5c68912b61a17387d1f8e4f1d804860ffa10c8a4bf76"
h_expected="b9ed9e0fd4e660c858a39f59be6bb91671733b1136a5cd56b772198ffb3ec7fb"

[[ -f "${zr_source}" ]] || { echo "ERROR: missing ${zr_source}" >&2; exit 1; }
[[ -f "${h_source}" ]] || { echo "ERROR: missing ${h_source}" >&2; exit 1; }

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/zr_h_potcar.XXXXXX")"
trap 'rm -rf -- "${tmpdir}"' EXIT
if gzip -t -- "${zr_source}" 2>/dev/null; then
    gzip -cd -- "${zr_source}" >"${tmpdir}/Zr_sv"
else
    cat -- "${zr_source}" >"${tmpdir}/Zr_sv"
fi
zr_actual="$(sha256sum "${tmpdir}/Zr_sv" | awk '{print $1}')"
h_actual="$(sha256sum "${h_source}" | awk '{print $1}')"
[[ "${zr_actual}" = "${zr_expected}" ]] || {
    echo "ERROR: Zr_sv SHA-256 mismatch" >&2
    echo "  source:   ${zr_source}" >&2
    echo "  expected: ${zr_expected}" >&2
    echo "  actual:   ${zr_actual}" >&2
    exit 1
}
[[ "${h_actual}" = "${h_expected}" ]] || {
    echo "ERROR: H SHA-256 mismatch" >&2
    echo "  source:   ${h_source}" >&2
    echo "  expected: ${h_expected}" >&2
    echo "  actual:   ${h_actual}" >&2
    exit 1
}
cat "${tmpdir}/Zr_sv" "${h_source}" >"${tmpdir}/Zr_H"

install_case() {
    local relative="$1"
    local source="$2"
    local directory="${root}/${relative}"
    local target="${directory}/POTCAR"
    local evidence
    for evidence in OUTCAR CONTCAR OSZICAR vasprun.xml .run_status; do
        if [[ -e "${directory}/${evidence}" ]]; then
            [[ -s "${target}" ]] || {
                echo "ERROR: completed/started directory lacks POTCAR: ${directory}" >&2
                exit 1
            }
            cmp -s "${source}" "${target}" || {
                echo "ERROR: existing POTCAR differs in locked directory: ${target}" >&2
                exit 1
            }
            [[ -s "${directory}/inputs.sha256" ]] || {
                echo "ERROR: locked directory lacks inputs.sha256: ${directory}" >&2
                exit 1
            }
            (cd "${directory}" && sha256sum --check --strict --quiet inputs.sha256) || {
                echo "ERROR: locked input checksum failed: ${directory}" >&2
                exit 1
            }
            echo "LOCKED: ${relative} (existing output preserved)"
            return
        fi
    done
    if [[ -e "${target}" ]]; then
        cmp -s "${source}" "${target}" || {
            echo "ERROR: existing POTCAR differs: ${target}" >&2
            exit 1
        }
    else
        install -m 600 "${source}" "${target}"
    fi
    (
        cd "${directory}"
        sha256sum INCAR KPOINTS POSCAR POTCAR >inputs.sha256.tmp
        mv inputs.sha256.tmp inputs.sha256
    )
    echo "READY: ${relative}"
}

install_case "01_initial/01_zr96_static" "${tmpdir}/Zr_sv"
install_case "01_initial/02_h2_relax" "${h_source}"
install_case "01_initial/03_t_relax" "${tmpdir}/Zr_H"
install_case "01_initial/04_o_relax" "${tmpdir}/Zr_H"
install_case "01_initial/01_zr96_static/retry_dcu_01" "${tmpdir}/Zr_sv"
install_case "01_initial/03_t_relax/retry_dcu_01" "${tmpdir}/Zr_H"
install_case "01_initial/04_o_relax/retry_dcu_01" "${tmpdir}/Zr_H"

echo "Zr_sv SHA-256: ${zr_actual}"
echo "H SHA-256:     ${h_actual}"
echo "POTCAR assembly PASS"
