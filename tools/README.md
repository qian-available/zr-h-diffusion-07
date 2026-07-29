# `tools/`：NEB 工作流辅助工具

本目录的脚本用于准备、锁定、核验、分析和打包本项目的 Zr96H NEB
计算。它们不替代 VASP：真正的普通 NEB 与 CI-NEB 力、切线和爬坡像
算法由已链接 VTST 的 VASP 可执行文件在计算节点上执行。

VTST 附带的 Perl 脚本（例如 `dist.pl`、`nebbarrier.pl`）是辅助的
后处理/诊断工具，不会执行 CI-NEB 算法，也不会启动 VASP。

## 工具一览及写入边界

| 工具 | 典型调用 | 职责 | 文件影响 |
| --- | --- | --- | --- |
| `analyze_initial.py` | `python tools/analyze_initial.py --results-root RESULT_ROOT` | 核验下载的初始计算，提取能量、力、近邻并画图。 | **读取结果；在 `--output-dir` 写入 TSV、PNG、PDF。** 不读 POTCAR、不准备或运行 VASP。 |
| `check_initial.sh` | `bash tools/check_initial.sh` | 检查四组初始计算的 OUTCAR、收敛、离子数和力。 | **读取计算结果；更新** `01_initial/initial_summary.tsv`。 |
| `assemble_potcars.sh` | `bash tools/assemble_potcars.sh` | 校验 Zr_sv/H 赝势哈希，并装配初始计算的 POTCAR 与输入校验和。 | **写入**未锁定目录的 `POTCAR`、`inputs.sha256`；已存在计算证据的目录只校验，拒绝改写。POTCAR 是受许可文件，不应提交或公开分发。 |
| `prepare_neb_paths.py` | `python tools/prepare_neb_paths.py --results-root RESULT_ROOT [--output-root ROOT]` | 由完成的 T/O 端点构建 TT_c、TO 的端点和四个中间像预 NEB 输入。 | **新建** `02_endpoints/01_tt_c_symmetry`、`03_neb/01_tt_c`、`03_neb/02_to`；目标已存在即拒绝覆盖。不读/复制 POTCAR，不启动 VASP。 |
| `assemble_neb_potcars.sh` | `bash tools/assemble_neb_potcars.sh [03_neb/PATH ...]` | 校验赝势并为 NEB 路径装配 `Zr_H` POTCAR、写输入哈希。 | **写入**未运行路径的 `POTCAR`、`inputs.sha256`；已有输出的路径只校验并保持锁定。 |
| `prepare_neb_stage.py` | `python tools/prepare_neb_stage.py --source SRC --target DST --target-stage pre\|ci` | 从源路径的中间像 CONTCAR 生成续跑或 CI-NEB 阶段；固定端点原样复制。 | **新建** `DST` 及其 POSCAR、INCAR、KPOINTS、`job.slurm`、清单；目标存在即拒绝覆盖。不读/复制 POTCAR，不启动 VASP。 |
| `check_neb.sh` | `bash tools/check_neb.sh --stage pre\|ci PATH [PATH ...]` | 核验 VASP/VTST 标记、各像输出、力阈值、端点哈希和阶段清单。 | **读取计算结果；更新**每个阶段的 `check_summary.tsv`。路径必须相对 `03_neb`，拒绝绝对路径与 `..`。 |
| `analyze_neb.py` | `python tools/analyze_neb.py --tt-result TT_CI --to-result TO_CI --output-dir DIR` | 核验两条最终 CI-NEB 路径，输出能垒、力收敛、几何表及图。 | **读取结果；在 `DIR` 更新** `neb_profile.tsv`、`neb_convergence.tsv`、`path_geometry.tsv`、PNG/PDF 图。不会运行 VASP。 |
| `package_neb_result.sh` | `bash tools/package_neb_result.sh 03_neb/PATH [OUTPUT_DIR]` | 仅在 `check_neb.sh` 通过后，生成可交付的结果包和 SHA-256。 | **新建** `.tar.gz` 与 `.sha256`，若同名存在则拒绝覆盖；排除 POTCAR、WAVECAR、CHGCAR、HDF5 及临时文件，并设为仅所有者可读写。 |
| `test_neb_tools.py` | `python tools/test_neb_tools.py` | 基于临时合成夹具回归测试 NEB 工具。 | 仅在临时目录写测试数据；不修改项目计算目录。 |
| `neb_common.py` | 被 Python 工具导入 | POSCAR/OUTCAR 解析、最小镜像、原子写入、哈希和 NEB 共用校验。 | 本身无 CLI；写入仅发生在调用它的生成/分析工具指定的目标中。 |

所有“原子写入”会先在同目录临时文件/目录中完成，再移动到目标；这减少了中途失败留下半成品的风险，但分析输出目录中的同名常规文件可被分析脚本更新。因此应将 `--output-dir` 指向专用的分析目录。

## 距离核验：本地实现与 VTST `dist.pl`

`neb_common.py` 的 `minimum_image`、`minimum_image_distances` 和
`prepare_neb_paths.py` 中的几何检查使用周期性边界条件下的最小镜像位移。
对于两张具有相同晶格、原子顺序和元素计数的结构，VTST 官方 `dist.pl`
的结构距离可表为

`sqrt(sum_i |dr_i(minimum image)|^2)`。

也就是说，它是**所有原子**最小镜像位移的 RSS（root-sum-square）距离。
本地距离核验采用相同的最小镜像原则；当使用同一对结构、晶格和原子顺序时，
可用 `dist.pl` 作为独立的官方对照。`dist.pl` 不在本仓库内，需从已安装的
VTST 脚本目录调用，例如在含 POSCAR 的目录中按本机 VTST 安装说明运行。

`dist.pl` 的全体系 RSS 距离适合判断相邻图像在整个构型空间中的间距是否均匀，
但它不是本项目能量图横轴的定义。

## 不要混用两种“距离”

`analyze_neb.py` 的 `reaction_coordinate_a`（以及
`path_geometry.tsv` 的 `h_cumulative_a`）只追踪最后一个原子 H：相邻图像间
H 的最小镜像位移逐段累加。它用于描述 H 扩散路径长度，并同时输出其基面与
`c` 分量。

这与 `dist.pl` 的全体系 RSS 结构距离不同，数值没有可直接互换的物理含义：

* H-only 累计坐标：一条路径上的累加弧长，可随图像数增加而累加；
* `dist.pl`：任意两构型的一次性全体系直线式 RSS 距离，包含 Zr 的弛豫。

因此，不能用 `dist.pl` 距离替换 `neb_profile.tsv` 的反应坐标，也不能拿
H-only 累计长度判断全体系图像间隔是否均匀。报告图像间隔时，请注明使用的是
H 位移还是全体系 RSS，并保持前后一致。

## 运行与安全提示

准备脚本生成的 `job.slurm` 会在提交前检查输入哈希、目标 VASP 的 SHA-256、
VTST 4.2 标记和 `LCLIMB` 支持；实际计算由 Slurm 中的 VASP 进程完成。准备脚本
本身只产生输入，绝不提交作业。

在服务器上推荐按以下顺序执行：准备路径 → 装配并锁定 POTCAR → `sbatch` 运行
预 NEB → `check_neb.sh` → `prepare_neb_stage.py --target-stage ci` → 装配/锁定
新阶段 POTCAR → `sbatch` CI-NEB → `check_neb.sh` → `analyze_neb.py` → 可选打包。
在任何阶段，先保留原始 OUTCAR/CONTCAR 和 `.run_status`；不要手工覆盖已产生
输出的计算目录。
