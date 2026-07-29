# TT_c 与 TO：预NEB → CI-NEB

本目录保存两条待计算路径：

```text
01_tt_c/  已收敛 T1 → 对称生成的 c 向 T2
02_to/    已收敛 T  → 已收敛 O
```

每条根目录含 `00–05` 六个图像；`01–04` 是逐原子最小镜像插值，`00/05` 是固定端点。
根目录是普通NEB预弛豫阶段：`EDIFFG=-0.10 eV/A`、`LCLIMB=.FALSE.`。预弛豫通过后，
`tools/prepare_neb_stage.py` 才能从4个 `CONTCAR` 建立子目录 `ci_01/`，其中使用
`EDIFFG=-0.03 eV/A`、`LCLIMB=.TRUE.`。

## SCNet执行顺序

服务器二进制已经只读确认包含 `VTST 4.2` 与 `LCLIMB`，并锁定：

```text
/work/home/liuzhixiao/software/dcu-port-2Feb2023-all/bin/vasp_std
SHA-256: a1b25c7ebf384a3147aa3ad8f77ba5fa020d8eacb8755f81e56d04cafabb1b6f
```

先只运行 TT 预弛豫：

```bash
cd /work/home/liuzhixiao/Zr-ckj
bash 07_h_diffusion_quickstart/tools/assemble_neb_potcars.sh 03_neb/01_tt_c
cd 07_h_diffusion_quickstart/03_neb/01_tt_c
sha256sum -c inputs.sha256
sbatch job.slurm
```

组装脚本默认读取私有的 `private_potentials/Zr_sv_04Jan2005/POTCAR`，压缩和未压缩格式
均可识别，并按解压后的内容校验 SHA-256。不要改用公共
`psudopotential/.../Zr_sv/POTCAR.Z`，该文件是不同的2000版势。

结束后回到工作流根目录检查并生成CI阶段：

```bash
bash tools/check_neb.sh --stage pre 01_tt_c
python tools/prepare_neb_stage.py \
  --source 03_neb/01_tt_c \
  --target 03_neb/01_tt_c/ci_01 \
  --target-stage ci
bash tools/assemble_neb_potcars.sh 03_neb/01_tt_c/ci_01
cd 03_neb/01_tt_c/ci_01
sha256sum -c inputs.sha256
sbatch job.slurm
```

CI结束后检查：

```bash
bash tools/check_neb.sh --stage ci 01_tt_c/ci_01
```

VTST 4.2 将 `FORCES: max atom, RMS` 写入各中间图像的 `01–04/OUTCAR`；阶段收敛值取
四幅图像末次第一列的最大值。SCNet这一本DCU二进制的根目录 `vasp.stdout` 可能不含该行，
因此不能用根目录标准输出代替图像OUTCAR。检查、阶段生成和最终分析脚本均按图像OUTCAR
审计；每幅图像还必须包含 `reached required accuracy`。

只有TT两个阶段均通过并下载验收后，才按相同步骤执行 `02_to`。脚本不自动提交下一阶段。

## 墙时续算

若预NEB或CI因墙时停止，保留原目录并建立新目录。例如CI续算：

```bash
python tools/prepare_neb_stage.py \
  --source 03_neb/01_tt_c/ci_01 \
  --target 03_neb/01_tt_c/ci_02 \
  --target-stage ci
```

预NEB墙时续算使用 `--target-stage pre` 和新目录 `pre_restart_01`。阶段生成器使用中间图像
最新 `CONTCAR`，端点保持不变；它不读取或复制POTCAR。新目录仍须单独组装POTCAR和手工提交。

## 下载后最终分析

两条最终CI阶段下载后运行：

```bash
python tools/analyze_neb.py \
  --tt-result 03_neb/01_tt_c/ci_01 \
  --to-result 03_neb/02_to/ci_01 \
  --output-dir 03_neb/analysis
```

若有 `ci_02`，对应参数改为最后通过的CI目录。势垒只读取最终CI能量；收敛表和力图同时连接
普通NEB与CI两段。当前尚未提交VASP，因此没有任何扩散势垒。

本路线仍是 `450 eV + Gamma 4x4x3 + 4图像` 玩具任务，不包含6图像、ZPE、有限尺寸或
Zr-H专门参数收敛。预计不含排队：TT两阶段约30–60小时，TO约45–90小时。
