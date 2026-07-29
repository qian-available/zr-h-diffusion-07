# TT_c 与 TO：预NEB → CI-NEB

本目录保存两条计划路径：

```text
01_tt_c/  已收敛 T1 → 对称生成的 c 向 T2
02_to/    已收敛 T  → 已收敛 O
```

每条根目录含 `00–05` 六个图像；`01–04` 是逐原子最小镜像插值，`00/05` 是固定端点。
根目录是普通NEB预弛豫阶段：`EDIFFG=-0.10 eV/A`、`LCLIMB=.FALSE.`。预弛豫通过后，
`tools/prepare_neb_stage.py` 才能从4个 `CONTCAR` 建立子目录 `ci_01/`，其中使用
`EDIFFG=-0.03 eV/A`、`LCLIMB=.TRUE.`。

## 为什么选择 TT_c 和 TO

[Zhang、Jiang与Bai对α-Zr-H扩散网络的DFT+KMC研究](https://doi.org/10.1038/srep41033)
将最近邻TT、TO、OT、OO列为三维扩散的基本跳跃，并报告电子势垒约为
TT `0.129 eV`、TO `0.406 eV`、OT `0.346 eV`。该文还发现较长的次近邻TT和OO路径
并不稳定，会分别松弛成TO+OT和OT+TO。由此，本轮先计算：

1. `TT_c`：当前T1到最近的c向等价T2；
2. `TO`：当前低能T末态到已弛豫O末态，同一路径反向给出OT势垒。

本项目的几何来自自身96-Zr超胞：TT理想中心距离为 `1.29150537 A`，包含对称松弛偏移后
实际H端点距离为 `1.24144250 A`；TO实际H位移总长、基面分量和有符号c分量分别为
`1.98514991/1.86838910/-0.67077742 A`。因此不另算 `3.2342 A` 的纯基面T→T直连，
因为它对应由T→O→T组成的较长复合路径。OO留作后续补充。

文献势垒只用于数量级审计，不是本项目通过门槛。文献的宏观扩散各向异性来自完整TT/TO/
OT/OO网络，不能把本项目的TT直接叫作“c轴扩散势垒”，也不能把TO叫作“纯基面势垒”。

## 为什么先普通 NEB、再 CI-NEB

Zhang等使用CI-NEB和3个中间图像；直接从初始插值启动CI在方法上是允许的。本项目改成两段，
是针对4图像、97个全可动原子和线性初始路径的工程稳健性选择：先让整条带平滑并确定稳定的
最高能区域，再让climbing image收敛到鞍点。普通NEB达到 `0.10 eV/A` 后，其4个
`CONTCAR`全部继承到CI阶段，因此预弛豫不是丢弃重算。

NEB改进切线和CI算法的原始方法依据为：

- [Henkelman and Jónsson, improved tangent NEB, *J. Chem. Phys.* 113 (2000) 9978](https://doi.org/10.1063/1.1323224)
- [Henkelman, Uberuaga and Jónsson, climbing-image NEB, *J. Chem. Phys.* 113 (2000) 9901](https://doi.org/10.1063/1.1329672)

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

SCNet不直接连接GitHub。每个阶段检查通过后，在07工作流根目录生成下载包，例如：

```bash
bash tools/package_neb_result.sh 03_neb/01_tt_c
bash tools/package_neb_result.sh 03_neb/01_tt_c/ci_01
```

结果包和对应 `.sha256` 写到07目录的上一级，不含POTCAR、WAVECAR或CHGCAR。下载到本地并
校验后，再由本地Git/Git LFS纳入版本。

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
普通NEB与CI两段。截至2026-07-29，TT普通NEB预弛豫已在SCNet运行；CI尚未完成，因此当前
仍没有可报告的最终扩散势垒。

本路线仍是 `450 eV + Gamma 4x4x3 + 4图像` 玩具任务，不包含6图像、ZPE、有限尺寸或
Zr-H专门参数收敛。预计不含排队：TT两阶段约30–60小时，TO约45–90小时。
