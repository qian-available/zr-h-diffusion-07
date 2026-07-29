# SCNet Zr96-H 项目维护说明

## 1. 用途与当前边界

本文说明 `Zr-ckj/07_h_diffusion_quickstart/` 在 SCNet 上的日常查看、结果验收、备份和失败
恢复方式。服务器环境的固定信息见 `SCNet_server_configuration.md`，当前作业编号与阶段
进度见 `07_h_diffusion_quickstart/README.md`。

当前维护旧 CPU 记录和三个新 DCU 算例：

```text
旧 Zr96 static：已完成，高精度参考
旧 H2 relax：已完成，继续使用
旧 T/O relax：因性能问题由用户停止，保留现场
新 Zr96/T/O retry_dcu_01：4x4x3 玩具任务输入
```

`02_endpoints/` 和 `03_neb/` 尚未进入生产阶段。第一批结果通过前，不提前建立或提交后续
任务。

## 2. 项目位置与两端职责

服务器正式目录：

```text
/work/home/liuzhixiao/Zr-ckj
```

本地工作区：

```text
D:\MeowMeowFolder\MEOWVERSE\Zr-smoke
```

两端职责不同：

| 位置 | 主要职责 |
| --- | --- |
| SCNet | 私有 POTCAR、Slurm 作业、VASP 原始输出、失败现场 |
| 本地工作区 | 输入模板、方法文档、进度记录、结果分析、绘图和交付包 |

服务器原始输出是计算证据；本地文档和分析脚本是整理入口。任何结果在下载并核对前，不只
凭聊天记录或截图保存。

## 3. 每次登录后的最小操作

进入项目：

```bash
cd /work/home/liuzhixiao/Zr-ckj
```

确认位置：

```bash
pwd
```

查看当前排队和运行任务：

```bash
squeue -u liuzhixiao
```

这三条命令不会改变任务。正常状态：

```text
PD  排队
R   运行
CG  正在结束和清理
```

任务从 `squeue` 消失不代表一定成功，只表示它已离开活动队列，需要用 `sacct` 查看最终
状态。

## 4. 忘记 Job ID 时怎么办

查看当前任务：

```bash
squeue -u liuzhixiao
```

查看当天历史：

```bash
sacct -u liuzhixiao -S today
```

任务名固定为：

```text
zr96_static
h2_relax
zr96h_t
zr96h_o
```

运行后，计算目录中还会出现：

```text
slurm-<jobid>.out
slurm-<jobid>.err
```

因此可以通过用户名、任务名或日志文件找回 Job ID，不需要依靠记忆。

## 5. 命令的风险分级

### 5.1 可随时执行的只读命令

```bash
pwd
ls -lh
squeue -u liuzhixiao
sacct -j <jobid>
cat .run_status
tail -n 20 vasp.stdout
tail -n 20 vasp.stderr
tail -n 20 OSZICAR
tail -n 20 OUTCAR
grep "General timing and accounting" OUTCAR
grep "reached required accuracy" OUTCAR
sha256sum -c inputs.sha256
```

`tail`、`cat`、`grep`、`squeue` 和 `sacct` 只读取状态，不会改变计算。

### 5.2 会改变服务器状态的命令

```text
sbatch     提交并可能消耗机时
scancel    停止作业
tar -xzf   解压并可能覆盖同名输入
cp / mv    复制或移动文件
chmod      改变访问权限
文本编辑器  修改输入或脚本
```

这些命令只在目标和目的明确时执行。运行中的计算目录不得修改 `INCAR`、`POSCAR`、
`KPOINTS`、`POTCAR` 或 `inputs.sha256`。

### 5.3 禁止随手执行

- 不在登录节点直接运行 `vasp_std` 或 `mpirun vasp_std`；
- 不删除 `OUTCAR`、`CONTCAR`、`vasprun.xml`、`OSZICAR`、WAVECAR 或 CHGCAR；
- 不在已有输出的目录中重复执行 `sbatch job.slurm`；
- 不用 `tar -xzf` 解压 `.sha256` 文本文件；
- 不把 POTCAR 放入下载包、公开仓库、聊天或共享目录；
- 不因 VASP 打印性能建议就在运行中修改 `NCORE`、`LREAL` 或其他参数。

## 6. 如何判断任务进度

### 6.1 Slurm 层

```bash
squeue -u liuzhixiao
```

`TIME` 是已运行时间，不是完成百分比。VASP 结构弛豫没有可靠的固定总步数，不能仅凭
`TIME` 计算完成比例。

### 6.2 VASP 电子迭代

进入对应计算目录后：

```bash
tail -n 20 OSZICAR
```

出现连续的 `DAV:` 或 `RMM:` 行表示电子自洽正在推进。冷启动的第一轮通常最慢。

### 6.3 VASP 离子步

对 relax 任务：

```bash
grep -c "F=" OSZICAR
```

输出数字是已经完成的离子步数。`0` 可能仅表示第一个离子步尚未完成，不等于失败。

### 6.4 性能提示与真正错误

以下大框通常只是性能建议：

```text
recommend to set NCORE
try LREAL=Auto
```

若随后出现：

```text
POSCAR, INCAR and KPOINTS ok
entering main loop
DAV: 1
```

说明 VASP 已进入计算。真正异常通常伴随 `vasp.stderr` 内容、Slurm `FAILED`、非零
`ExitCode` 或程序停止更新。

## 7. 任务结束后的检查顺序

### 7.1 先看 Slurm

```bash
sacct -j <jobid>
```

目标状态：

```text
State = COMPLETED
ExitCode = 0:0
```

这只证明程序正常退出，不等于物理结果已经收敛。

### 7.2 再看作业脚本记录

在计算目录执行：

```bash
cat .run_status
```

目标字段：

```text
vasp_exit=0
normal_termination=yes
electronic_convergence=yes
```

### 7.3 再看 VASP 物理收敛

Static 任务至少检查：

```bash
grep "General timing and accounting" OUTCAR
grep "energy(sigma->0)" OUTCAR | tail -n 1
```

Relax 任务还必须检查：

```bash
grep "reached required accuracy" OUTCAR
```

四个初始任务全部结束后，从服务器项目根目录统一执行：

```bash
bash 07_h_diffusion_quickstart/tools/check_initial.sh
```

该工具会生成：

```text
07_h_diffusion_quickstart/01_initial/initial_summary.tsv
```

检查结果应包含正常结束、原子数、最终 `energy(sigma->0)` 和最终最大力。T/O 是否仍属于
不同稳定间隙位不能由汇总脚本决定，必须继续分析两份 `CONTCAR`。

## 8. 关键文件怎么看

| 文件 | 内容 | 是否必须保留 |
| --- | --- | --- |
| `INCAR` | 计算方法与收敛参数 | 是 |
| `POSCAR` | 初始结构 | 是 |
| `KPOINTS` | 显式 k 点网格 | 是 |
| `POTCAR` | 私有赝势 | 服务器私有保留，不下载分发 |
| `inputs.sha256` | 输入完整性记录 | 是 |
| `job.slurm` | 资源与运行环境 | 是 |
| `OUTCAR` | 最完整的文本原始结果 | 是 |
| `vasprun.xml` | 结构化原始结果 | 是 |
| `OSZICAR` | 电子和离子迭代摘要 | 是 |
| `CONTCAR` | 最终结构 | 是 |
| `vasp.stdout` | VASP 屏幕输出与警告 | 是 |
| `vasp.stderr` | 错误输出 | 是 |
| `.run_status` | 作业脚本的结束状态 | 是 |
| `slurm-*.out/.err` | 调度器日志 | 是 |

WAVECAR 和 CHGCAR 体积可能较大。当前脚本关闭了常规写出；若后续任务生成，是否下载由
续算和分析需求决定，服务器原件不随意删除。

## 9. 失败时的恢复原则

任务出现 `FAILED`、`TIMEOUT` 或异常结束时：

1. 不删除失败目录；
2. 不直接在原目录再次 `sbatch`；
3. 保存 `sacct -j <jobid>` 输出；
4. 查看 `.run_status`、`vasp.stderr` 和 `OUTCAR` 尾部；
5. 检查 `CONTCAR` 是否完整；
6. 在确认失败原因后建立独立 `retry_01`；
7. 适用时复制完整 `CONTCAR` 为 retry 的 `POSCAR`；
8. 当前方案的 retry 仍使用 `ISTART=0`、`ICHARG=2` 冷启动。

恢复目录与原目录并存，保证失败现场可追溯。不得通过删除原始输出来“强制重跑”。

## 10. 结果如何带回本地

第一批任务全部验收后再打包，不在任务运行时制作结果包。结果包至少保留：

```text
INCAR
POSCAR
KPOINTS
job.slurm
inputs.sha256
OUTCAR
OSZICAR
CONTCAR
vasprun.xml
vasp.stdout
vasp.stderr
.run_status
slurm 日志
initial_summary.tsv
```

结果包必须排除：

```text
POTCAR
WAVECAR
CHGCAR
```

推荐流程：

1. 在 SCNet 上确认所有任务通过；
2. 按实际文件大小生成不含 POTCAR 的压缩包；
3. 用 `sha256sum` 生成校验文件；
4. 通过 SCNet 网页文件管理器下载压缩包和校验文件；
5. 本地再次校验 SHA-256；
6. 在本地提取 TSV、结构和绘图数据；
7. 保留服务器原始目录，不因下载成功而立即清理。

具体打包命令在第一批结果完成后根据实际文件生成，避免现在提前遗漏文件或打包运行中的
输出。

## 11. 文档如何维护

| 文档 | 更新内容 |
| --- | --- |
| `07_h_diffusion_quickstart/README.md` | 当前阶段、Job ID、最近一次检查状态和下一步 |
| `07_h_diffusion_quickstart/doc/SCNet_server_configuration.md` | 分区、模块、VASP、MPI、赝势等稳定环境 |
| `07_h_diffusion_quickstart/doc/SCNet_project_maintenance.md` | 日常查看、验收、恢复和备份规则 |

更新状态时使用有证据的措辞：

```text
已准备
已提交
最近一次检查为 RUNNING
Slurm COMPLETED
电子收敛
离子收敛
结构身份已确认
```

不要仅凭“作业从队列消失”写成“计算成功”，也不要把准备好的端点或 NEB 输入写成已经得到
物理结果。

## 12. 当前阶段的一页操作卡

查看任务：

```bash
squeue -u liuzhixiao
```

查看已结束任务：

```bash
sacct -u liuzhixiao -S today
```

查看某个任务输出：

```bash
tail -n 20 vasp.stdout
tail -n 20 OSZICAR
tail -n 20 vasp.stderr
```

当前 NEB 准备/提交阶段：

```text
T2 已由对称性生成，不提交端点 VASP
实际DCU vasp_std 已确认包含 VTST 4.2/LCLIMB
先组装并提交 03_neb/01_tt_c 的普通NEB预弛豫（0.10 eV/A）
预NEB通过后用 tools/prepare_neb_stage.py 建立 ci_01
TT的CI-NEB达到0.03 eV/A并验收后，才按相同步骤进行TO
每个阶段都手工提交，不自动串联
不删除或覆盖初始批次及被停止的旧 CPU 输出
```
