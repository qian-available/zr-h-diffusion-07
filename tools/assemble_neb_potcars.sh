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

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/zrh_neb_potcar.XXXXXX")"
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

has_output() {
    local directory="$1"
    local candidate
    for candidate in vasp.stdout vasp.stderr .run_status slurm-*.out slurm-*.err; do
        compgen -G "${directory}/${candidate}" >/dev/null && return 0
    done
    for image in 01 02 03 04; do
        for candidate in OUTCAR CONTCAR OSZICAR vasprun.xml; do
            [[ -e "${directory}/${image}/${candidate}" ]] && return 0
        done
    done
    return 1
}

install_path() {
    local relative="$1"
    [[ "${relative}" != /* && "${relative}" != *".."* ]] || {
        echo "ERROR: path must be relative to 07_h_diffusion_quickstart: ${relative}" >&2
        exit 1
    }
    local directory="${root}/${relative}"
    local target="${directory}/POTCAR"
    local image
    [[ -d "${directory}" ]] || { echo "ERROR: missing path directory ${directory}" >&2; exit 1; }
    for image in 00 01 02 03 04 05; do
        [[ -s "${directory}/${image}/POSCAR" ]] || {
            echo "ERROR: missing ${directory}/${image}/POSCAR" >&2
            exit 1
        }
    done
    if has_output "${directory}"; then
        [[ -s "${target}" && -s "${directory}/inputs.sha256" ]] || {
            echo "ERROR: locked path lacks POTCAR or inputs.sha256: ${directory}" >&2
            exit 1
        }
        cmp -s "${tmpdir}/Zr_H" "${target}" || {
            echo "ERROR: existing POTCAR differs in locked path: ${target}" >&2
            exit 1
        }
        (cd "${directory}" && sha256sum --check --strict --quiet inputs.sha256) || {
            echo "ERROR: locked input checksum failed: ${directory}" >&2
            exit 1
        }
        echo "LOCKED: ${relative} (existing output preserved)"
        return
    fi
    if [[ -e "${target}" ]]; then
        cmp -s "${tmpdir}/Zr_H" "${target}" || {
            echo "ERROR: existing POTCAR differs: ${target}" >&2
            exit 1
        }
        chmod 600 "${target}"
    else
        install -m 600 "${tmpdir}/Zr_H" "${target}"
    fi
    (
        cd "${directory}"
        sha256sum INCAR KPOINTS POTCAR \
            00/POSCAR 01/POSCAR 02/POSCAR 03/POSCAR 04/POSCAR 05/POSCAR \
            >inputs.sha256.tmp
        mv inputs.sha256.tmp inputs.sha256
        chmod 600 POTCAR
    )
    echo "READY: ${relative}"
}

if (($#)); then
    requested=("$@")
else
    requested=("03_neb/01_tt_c" "03_neb/02_to")
fi
for relative in "${requested[@]}"; do
    install_path "${relative#./}"
done
echo "Zr_sv SHA-256: ${zr_actual}"
echo "H SHA-256:     ${h_actual}"
echo "NEB POTCAR assembly and input lock PASS"
