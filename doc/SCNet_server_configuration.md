# SCNet 西北一区服务器配置记录

## 1. 文档用途

本文记录 Zr96-H 扩散任务在 SCNet 西北一区实际使用的服务器环境。内容来自
2026-07-27 至 2026-07-28 的终端检查和首次生产作业，不是通用 SCNet 模板。

动态作业进度记录在 `07_h_diffusion_quickstart/README.md`；本文只保存相对稳定、后续复现
时容易遗漏的环境配置。

## 2. 账号与目录

```text
区域：SCNet 西北一区（西安）
用户：liuzhixiao
Shell：/bin/bash
家目录：/work/home/liuzhixiao
当前项目目录：/work/home/liuzhixiao/Zr-ckj
```

项目目录最初命名为 `Zr-smoke`，正式上传后在服务器改名为 `Zr-ckj`。07 工作流中的工具
从脚本自身位置推导目录，因此改名不影响运行；手工命令应使用当前绝对路径。

登录节点曾显示 `login06`，首次正式作业提交时显示 `login01`。登录节点由平台分配，节点名
可能变化，不能写入计算输入或提交脚本。

## 3. Slurm 调度环境

已确认命令：

```text
sbatch: /opt/gridview/slurm/bin/sbatch
squeue: /opt/gridview/slurm/bin/squeue
```

正式 CPU 作业使用：

```text
partition: xahcnormal
nodes: 1
tasks per node: 32
cpus per task: 1
```

观察到的 `xahcnormal` 节点通常提供 32 个 CPU 核和约 126536 MB 内存，但分区内存在不同
节点类型，因此资源要求以 Slurm 脚本为准，不硬编码具体节点名。

Slurm 账户在首次 H2 作业的 `sacct` 输出中显示为：

```text
acv7gl42b0
```

当前作业无需显式填写 `#SBATCH --account`；若平台策略变化，再根据 `sbatch` 报错或项目
配额页面补充。

常用查询命令：

```bash
squeue -u liuzhixiao
sacct -u liuzhixiao -S today
```

## 4. 编译器、MPI 与启动方式

登录后曾默认加载：

```text
compiler/dtk/22.04.2
compiler/devtoolset/7.3.1
mpi/hpcx/gcc-7.3.1
```

默认 `mpirun` 为：

```text
/opt/hpc/software/mpi/hpcx/v2.11.0/gcc-7.3.1/bin/mpirun
```

这些默认模块不用于当前 CPU 版 VASP 生产任务。每个 `job.slurm` 都先执行 `module purge`，
再固定加载：

```bash
module load compiler/intel/2017.5.239
module load mpi/intelmpi/2017.4.239
```

正式启动命令为：

```bash
srun --mpi=pmi2 /work/home/liuzhixiao/software/vasp.6.3.2/bin/vasp_std
```

不直接使用登录环境中的 HPC-X `mpirun`。

线程和 Intel MPI/MKL 环境固定为：

```bash
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export MKL_DEBUG_CPU_TYPE=5
export MKL_CBWR=AVX2
export I_MPI_PIN_DOMAIN=numa
```

## 5. VASP 可执行文件

当前生产可执行文件：

```text
/work/home/liuzhixiao/software/vasp.6.3.2/bin/vasp_std
```

同一安装目录还包含 `vasp_gam` 和 `vasp_ncl`，本项目使用 `vasp_std`。

`vasp_std` 默认不在登录 Shell 的 `PATH` 中。服务器还提供用户模块
`vasp-632-dtk22.10`，它指向：

```text
/work/home/liuzhixiao/apprepo/vasp/632-dtk22.10/app/bin
```

该模块对应另一套 DTK 环境，不是当前 CPU 生产任务采用的 Intel/Intel MPI 组合。不要因
`module avail vasp` 能看到它就替换正式可执行文件。

当前 module 系统不支持 `module spider` 子命令。查询模块应使用：

```bash
module avail
module show <module-name>
```

## 6. 赝势配置

### 6.1 正式使用的 Zr_sv

纯 Zr 基线要求：

```text
TITEL: PAW_PBE Zr_sv 04Jan2005
ZVAL: 12.000
ENMAX: 229.898 eV
SHA-256:
25aed69cb10325f9d37c5c68912b61a17387d1f8e4f1d804860ffa10c8a4bf76
```

服务器公共势目录中的文件：

```text
/work/home/liuzhixiao/psudopotential/PAW-GGA-PBE/Zr_sv/POTCAR.Z
```

实际是 `PAW_PBE Zr_sv 07Sep2000`，`ENMAX=229.839 eV`，解压后 SHA-256 为：

```text
ae88ffe8f400de1b63843ffaa7ed759dc66fa50e2939df1816907013c4aef6eb
```

该文件与项目基线不一致，未用于正式任务。

正确的 2005 版从本地受控输入上传至服务器私有目录：

```text
/work/home/liuzhixiao/Zr-ckj/private_potentials/Zr_sv_04Jan2005/POTCAR
/work/home/liuzhixiao/Zr-ckj/private_potentials/Zr_sv_04Jan2005/POTCAR.gz
```

两个文件仅允许保存在账号私有空间，不进入分发包或公开仓库。

### 6.2 正式使用的 H

```text
路径：
/work/home/liuzhixiao/psudopotential/PAW-GGA-PBE/H/POTCAR

TITEL: PAW_PBE H 15Jun2001
ZVAL: 1.000
ENMAX: 250.000 eV
SHA-256:
b9ed9e0fd4e660c858a39f59be6bb91671733b1136a5cd56b772198ffb3ec7fb
```

Zr96H 的 POTCAR 顺序固定为 `Zr_sv + H`。H2、T 位和 O 位任务使用同一个 H POTCAR。

## 7. 07 工作流的实际资源配置

旧 CPU 作业使用 `xahcnormal`。因 `5x5x4`、`LREAL=.FALSE.`、`ISYM=0` 和默认 CPU
并行布局导致 T/O 首个电子步过慢，T/O 作业由用户停止；Zr96 与 H2 旧结果保留。

新的玩具任务资源为：

| 任务类型 | 节点 | CPU task | DCU | 墙时上限 | 分区 |
| --- | ---: | ---: | ---: | ---: | --- |
| Zr96 `4x4x3` static | 1 | 32 | 4 | 24 h | `xahdnormal` |
| Zr96H T/O `4x4x3` relax | 1 | 32 | 4 | 24 h | `xahdnormal` |
| H2 relax | 1 | 4 | 0 | 已完成 | `xahcnormal` |
| TT_c/TO 预NEB或CI-NEB单阶段 | 1 | 32 | 4 | 5 d | `xahdnormal` |

服务器现有 DCU 模板使用：

```text
compiler/intel/2020.1.217
mpi/intelmpi/2020.1.217
compiler/dtk/22.10
/work/home/liuzhixiao/software/dcu-port-2Feb2023-all/env.sh
```

模板申请 `--gres=dcu:4`，生成4个 Intel MPI 进程，分别通过 `HIP_VISIBLE_DEVICES=0..3`
绑定一块 DCU，并用 `numactl` 绑定相应 NUMA 节点。每个进程设置 `OMP_NUM_THREADS=6`。
该 DCU 组合已经由本项目的新 Zr96、T、O 作业验证。NEB按
`TT预NEB→TT CI→TO预NEB→TO CI` 顺序提交，每次只推进一个阶段。

NEB 资源是后续阶段预设，尚未提交生产作业。

每个初始任务从自己的计算目录执行：

```bash
sbatch job.slurm
```

脚本将 VASP 标准输出和错误输出分别写入：

```text
vasp.stdout
vasp.stderr
```

Slurm 日志写入：

```text
slurm-<jobid>.out
slurm-<jobid>.err
```

任务结束后还会生成 `.run_status`。Slurm 的 `COMPLETED` 只表示程序正常退出；离子弛豫
仍需检查 `reached required accuracy`、最终最大力和结构。

## 8. VTST 与 CI-NEB 边界

家目录存在：

```text
/work/home/liuzhixiao/vtstscripts-1033
```

2026-07-29 已对实际DCU二进制进行只读检查：

```text
/work/home/liuzhixiao/software/dcu-port-2Feb2023-all/bin/vasp_std
SHA-256: a1b25c7ebf384a3147aa3ad8f77ba5fa020d8eacb8755f81e56d04cafabb1b6f
BuildID: 7404e801faa201a3bea0072f063552e65dcdc2d5
ELF: not stripped
VTST: version 4.2, 08/11/21
```

二进制字符串明确包含 `LCLIMB`、`climbing_image`、`ICHAIN`、`IOPT`、`LDNEB` 等读取和
执行符号，因此确认该 `vasp_std` 已链接VTST并支持CI-NEB。作业脚本在启动前复核二进制
SHA-256、VTST 4.2和LCLIMB标记，变化时停止。当前图像仍由
`tools/prepare_neb_paths.py` 做最小镜像插值，不使用 `nebmake.pl`。

正式路线先以 `LCLIMB=.FALSE.`、`EDIFFG=-0.10 eV/A` 做普通NEB预弛豫，再由
`tools/prepare_neb_stage.py` 从4个 `CONTCAR` 建立
`LCLIMB=.TRUE.`、`EDIFFG=-0.03 eV/A` 的CI阶段。

## 9. 已验证与未确认事项

已验证：

- `xahcnormal` 可以接收并运行 32-rank CPU VASP 任务；
- Intel 2017、Intel MPI 2017 与 `srun --mpi=pmi2` 组合可运行 VASP 6.3.2；
- 正式 Zr_sv/H 势的 SHA-256 均通过；
- H2 首次生产任务正常结束，电子和离子弛豫均收敛。
- 新 `4x4x3` DCU Zr96、T、O 均已正常结束，T/O 达到离子收敛门槛。
- 实际DCU `vasp_std` 的SHA-256已记录，并确认包含VTST 4.2与CI-NEB支持。

尚未确认：

- 分区长期排队策略和机时计费细则；
- 未来是否需要显式 `--account`；
- NEB 的实际最佳墙时和并行效率。

遇到平台迁移、模块升级或可执行文件更换时，应重新核对本文件，而不是静默沿用旧配置。
