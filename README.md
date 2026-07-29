# Zr96-H 扩散：DCU 玩具任务版

本目录采用显式 VASP 算例：每个计算目录直接保存 `POSCAR`、`INCAR`、`KPOINTS` 和
`job.slurm`，不使用 Python 总控脚本。

版本管理仅覆盖本目录，采用私有GitHub仓库、单一 `main` 分支和阶段标签；规则见
[`VERSION_CONTROL.md`](VERSION_CONTROL.md)。

当前简化路线为：

```text
已完成的高精度 Zr96 与 H2 保留
  -> 已完成 4x4x3 DCU 版 Zr96、T、O
  -> 已确认 T/O 不同配位末态并计算玩具版溶解能
  -> 已由 T 末态经精确对称操作生成 c 向等价 T2（不做端点 VASP）
  -> 已生成 TT_c 与 TO 两条 4 图像普通NEB预弛豫输入
  -> 待按 TT预NEB→TT CI→TO预NEB→TO CI 顺序计算
```

本路线以快速跑通流程为目的，不宣称完成 Zr-H k 点、有限尺寸或精确鞍点收敛。

## 当前状态（2026-07-29）

服务器项目目录：

```text
/work/home/liuzhixiao/Zr-ckj/
```

已确认势：

```text
Zr_sv: PAW_PBE Zr_sv 04Jan2005
SHA-256: 25aed69cb10325f9d37c5c68912b61a17387d1f8e4f1d804860ffa10c8a4bf76

H: PAW_PBE H 15Jun2001
SHA-256: b9ed9e0fd4e660c858a39f59be6bb91671733b1136a5cd56b772198ffb3ec7fb
```

旧 CPU 作业记录：

| Job ID | 任务 | 当前记录 |
| --- | --- | --- |
| `62040127` | 原 Zr96 `5x5x4` static | 用户确认已算完，保留为高精度参考 |
| `62040677` | H2 relax | 已正常结束，电子与离子均收敛，继续使用 |
| `62040731` | 原 T `5x5x4` relax | 因运行过慢由用户停止，原输出保留 |
| `62040755` | 原 O `5x5x4` relax | 因运行过慢由用户停止，原输出保留 |

新 DCU 初始批次已经完成并下载归档：

| Job ID | 任务 | 验收结果 |
| --- | --- | --- |
| `62149576` | 新 Zr96 `4x4x3` static | 正常结束，电子收敛 |
| `62134983` | T 初态 Zr96H relax | 13 步收敛，最大力 `0.00874278 eV/A` |
| `62148446` | O 初态 Zr96H relax | 12 步收敛，最大力 `0.00420013 eV/A` |

本地最小镜像与配位分析确认 T/O 分别保持四配位与六配位的不同末态。当前溶解能为
`Esol(T)=-0.44980706 eV/H`、`Esol(O)=-0.38690306 eV/H`，T 比 O 低 `62.904 meV`。
O 从高对称中心起算，本次 relax 本身只证明其为驻点；已有 α-Zr-H 振动/声子研究报告 O 位
无虚频，支持将其定性为亚稳局域极小。本项目未自行计算声子。T2 已由整个 T 末态的精确晶体
对称映射生成，数学上与 T1 等能，因此不再浪费一次端点 SCF/static/relax。TT_c 和 TO 输入
均已生成；TT普通NEB预弛豫已在SCNet运行，TO尚未提交。在TT CI完成前没有可报告的最终
NEB势垒。

详细结果见 [`doc/Zr96-H初始数据分析与归纳-20260728.md`](doc/Zr96-H初始数据分析与归纳-20260728.md)，
精简分享版见 [`doc/Zr96-H初始结果精简分享-20260728.md`](doc/Zr96-H初始结果精简分享-20260728.md)。

## 1. 新旧参数

旧 CPU 参数保留不动。新的 Zr96/T/O DCU 算例统一使用：

```text
ENCUT = 450 eV
EDIFF = 1E-6
LREAL = Auto
KPOINTS = Gamma-centered 4x4x3
```

T/O relax 另外使用：

```text
IBRION=2, NSW=200, ISIF=2
EDIFFG=-0.01 eV/A, POTIM=0.30
ISYM=0
```

新的 Zr96 参考使用 `IBRION=-1、NSW=0、ISYM=2`。H2 不修改、不重算。

选择 `4x4x3` 的依据：原 Zr2 在 `ENCUT=450 eV` 下，`KSPACING=0.15 A^-1` 相对
`0.12 A^-1` 的能量变化约为 `0.817 meV/atom`；`4x4x3` 约等效于这一档原胞密度。

## 2. 目录职责

```text
07_h_diffusion_quickstart/
├── README.md
├── 01_initial/
│   ├── 01_zr96_static/
│   │   └── retry_dcu_01/       # 新 4x4x3 DCU static
│   ├── 02_h2_relax/            # 已完成，保持不动
│   ├── 03_t_relax/
│   │   └── retry_dcu_01/       # 新 4x4x3 DCU relax
│   ├── 04_o_relax/
│   │   └── retry_dcu_01/       # 新 4x4x3 DCU relax
│   └── analysis/               # 本地 TSV、PNG 和 PDF 派生结果
├── 02_endpoints/
│   └── 01_tt_c_symmetry/       # 数学生成的 T2 与映射 manifest；不是计算目录
├── 03_neb/
│   ├── 01_tt_c/                # 00–05，T→c 向等价 T
│   └── 02_to/                  # 00–05，已弛豫 T→已弛豫 O
├── doc/
└── tools/
```

原目录的 CPU 输出不得删除、移动或覆盖。新任务只在 `retry_dcu_01` 中写入。

## 3. DCU 环境

新 `job.slurm` 继承服务器 `~/vasp-dcu.slurm`：

```text
partition: xahdnormal
nodes: 1
CPU tasks: 32
GRES: dcu:4
compiler: Intel 2020.1.217
MPI: IntelMPI 2020.1.217
DTK: 22.10
VASP environment: /work/home/liuzhixiao/software/dcu-port-2Feb2023-all/env.sh
```

脚本启动4个 MPI 进程，分别绑定 DCU `0`、`1`、`2`、`3`，每个进程使用6个 CPU 线程。
INCAR 不设置 CPU 版的 `NCORE/NPAR/KPAR`。

## 4. 在服务器组装新 POTCAR

以下第 4–7 节保留为本批任务的历史准备与提交记录；对应作业已经完成，不应重复提交。

上传以下两个新文件到 `/work/home/liuzhixiao/Zr-ckj/`：

```text
Zr_H_quickstart_07_DCU_20260728.tar.gz
Zr_H_quickstart_07_DCU_20260728.tar.gz.sha256
```

在服务器项目根目录校验并解压：

```bash
cd /work/home/liuzhixiao/Zr-ckj
sha256sum -c Zr_H_quickstart_07_DCU_20260728.tar.gz.sha256
tar -xzf Zr_H_quickstart_07_DCU_20260728.tar.gz
```

校验必须显示 `OK`。压缩包会新增 `retry_dcu_01` 并更新 README/tools，不含 POTCAR，也不会
删除已有 CPU 输出。

随后组装新 POTCAR：

```bash
cd /work/home/liuzhixiao/Zr-ckj
ZR_SV_POTCAR=/work/home/liuzhixiao/Zr-ckj/private_potentials/Zr_sv_04Jan2005/POTCAR.gz \
  bash 07_h_diffusion_quickstart/tools/assemble_potcars.sh
```

脚本不会改动已有输出的旧目录；它只验证旧输入并为三个新 `retry_dcu_01` 组装 POTCAR、
生成 `inputs.sha256`。成功时应看到三个新目录的 `READY` 和最终 `POTCAR assembly PASS`。

## 5. 提交前检查 T

```bash
cd /work/home/liuzhixiao/Zr-ckj/07_h_diffusion_quickstart/01_initial/03_t_relax/retry_dcu_01
sha256sum -c inputs.sha256
grep -E 'ENCUT|EDIFF|LREAL|IBRION|NSW|EDIFFG|ISYM' INCAR
cat KPOINTS
grep '^#SBATCH' job.slurm
```

应确认：

```text
Zr H / 96 1
ENCUT=450
EDIFF=1E-6
LREAL=Auto
4 4 3
xahdnormal
dcu:4
```

## 6. 第一次只提交 T

本次任务配置：

```text
任务数：1
资源：1 节点、32 CPU task、4 块 DCU
墙时上限：24 h
写入目录：03_t_relax/retry_dcu_01
失败恢复：保留目录，另建 retry_dcu_02
```

确认输入后，在 T 的新目录执行：

```bash
sbatch job.slurm
```

查看：

```bash
squeue -u liuzhixiao
tail -n 20 vasp.stdout
tail -n 20 vasp.stderr
```

若出现 HIP、动态库、DTK 或 DCU 绑定错误，停止后先检查现有 DCU 二进制；只有确认为版本
不兼容时才去 SCNet 商城安装新的西北一区 VASP-DCU。

## 7. T 正常后提交 O 与 Zr96

T 能正常进入电子迭代且速度明显改善后，分别进入：

```text
01_initial/04_o_relax/retry_dcu_01
01_initial/01_zr96_static/retry_dcu_01
```

各执行一次：

```bash
sbatch job.slurm
```

不要在旧 CPU 目录重复提交。

## 8. 第一批结果检查

新 Zr96、T、O 全部结束后，从服务器项目根目录执行：

```bash
bash 07_h_diffusion_quickstart/tools/check_initial.sh
```

工具读取：

```text
新 4x4x3 Zr96 static
旧的已完成 H2 relax
新 4x4x3 T relax
新 4x4x3 O relax
```

它检查正常结束、电子/离子收敛、原子数、最终 `energy(sigma->0)` 和最大力。本批四项均已
通过。后续本地分析又检查了两份 `CONTCAR` 的最小镜像位移、H-Zr 配位和位点间距，确认
T/O 为两个不同配位驻点；结合已有无虚频的文献结果，O 归类为亚稳局域极小，但这不是本项目
自行完成的声子验证。

玩具版溶解能定义：

```text
Esol = E_4x4x3,Auto(Zr96H)
     - E_4x4x3,Auto(Zr96)
     - 1/2 E_existing(H2)
```

## 9. 已生成的端点与 NEB 输入

无 VASP 生成命令：

```bash
python 07_h_diffusion_quickstart/tools/prepare_neb_paths.py \
  --results-root 07_H_diffussion_result/ZrH07_initial_results_20260728/07_h_diffusion_quickstart \
  --output-root 07_h_diffusion_quickstart
```

脚本读取下载结果中的 T/O `POSCAR、CONTCAR、OUTCAR、.run_status`，不读取 POTCAR。它用
`S(x,y,z)=(x,y,7/6-z) mod 1` 映射全部 97 个原子，并用理想 T-POSCAR 确定 Zr 行置换。
理想 T 中心间距为 `1.29150537 A`；将 T 的实际松弛偏移一并镜像后，两实际 H 端点相距
`1.24144250 A`。两者含义不同，不能混用。

生成器还建立 `TT_c` 和 `TO` 的 `00–05`，中间4图像采用逐原子最小镜像插值。TO 实际
H 端点位移为 `1.98514991 A`，其中基面分量 `1.86838910 A`、c 分量
`-0.67077742 A`。本项目不使用 `nebmake.pl`。

上传到 SCNet 后，在项目根目录组装私有 POTCAR 并锁定输入：

```bash
bash 07_h_diffusion_quickstart/tools/assemble_neb_potcars.sh 03_neb/01_tt_c
cd 07_h_diffusion_quickstart/03_neb/01_tt_c
sha256sum -c inputs.sha256
sbatch job.slurm
```

脚本默认读取本项目私有的
`/work/home/liuzhixiao/Zr-ckj/private_potentials/Zr_sv_04Jan2005/POTCAR`。
不得改用 SCNet 公共目录中的 `Zr_sv/POTCAR.Z`；后者是已知的 `07Sep2000` 版本，与初始
Zr96/T/O 计算所用 `04Jan2005` 版本不一致。必要时可用 `ZR_SV_POTCAR` 显式指定同一私有
势文件；压缩和未压缩格式均可识别，脚本按解压后的内容计算 SHA-256。

先只提交 `TT_c` 的普通NEB预弛豫。其达到 `0.10 eV/A` 后，用
`tools/prepare_neb_stage.py` 从4个 `CONTCAR` 建立 `ci_01`，再以 `0.03 eV/A` 完成
CI-NEB。TT两个阶段均验收后才开始TO。详细命令见
[`03_neb/README.md`](03_neb/README.md)。本地路径生成不等于NEB已计算，当前不存在可报告势垒。

## 10. 后续与边界

端点和NEB延续 `4x4x3 + EDIFF=1E-6 + LREAL=Auto + DCU`，保持4个中间图像。
普通NEB只预弛豫到 `0.10 eV/A`；最终CI-NEB以 `LCLIMB=.TRUE.` 收敛到
`0.03 eV/A`。已确认SCNet DCU二进制包含VTST 4.2并锁定其SHA-256。
NEB投影力从各中间图像 `OUTCAR` 的 `FORCES: max atom, RMS` 读取，并以四幅图像末次
最大值作为阶段判据；根目录 `vasp.stdout` 不作为该数值来源。

本路线不做：

- T/O 的 `5x5x4` 最终 static；
- CPU/DCU 能量复现；
- `3x3x2` 对照；
- 6图像或有限尺寸检查。

结果应标为玩具任务/流程演示，不用于宣称高精度扩散参数。

服务器固定环境见
[`doc/SCNet_server_configuration.md`](doc/SCNet_server_configuration.md)，维护与下载规则见
[`VERSION_CONTROL.md`](VERSION_CONTROL.md)。
