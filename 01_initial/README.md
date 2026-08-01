# Zr96-H 初始批次

本目录保存 Zr96、H2 以及 Zr96H 的 T/O 间隙位初始计算输入、同步后的原始输出和派生分析。
本页聚焦 2026-07-28 初始批次；后续 CI-NEB 正式结果见仓库根目录 README。

## 采用的数据

当前热力学组合固定为：

```text
Zr96：     01_zr96_static/retry_dcu_01
H2：       02_h2_relax
Zr96H(T)： 03_t_relax/retry_dcu_01
Zr96H(O)： 04_o_relax/retry_dcu_01
```

## 新旧参数与能量血缘

旧 CPU 批次采用更严格但显著更慢的 `5×5×4`、`EDIFF=1E-7`、`LREAL=.FALSE.`。其中
Zr96 static（Job `62040127`）完成并得到 `E0=-818.00059399 eV`，可作为高精度历史参考；
T/O relax（Jobs `62040731/62040755`）因运行过慢停止，原输出保留但不视为完成结果。

新 DCU 批次将 Zr96、T 和 O 统一为 `ENCUT=450 eV`、`EDIFF=1E-6`、`LREAL=Auto` 和
Gamma-centered `4×4×3`：

- T/O 执行固定晶胞 relax，正式端点能量取各自最后一个 `energy(sigma->0)`；
- Zr96 使用 `IBRION=-1、NSW=0、ISYM=2` 只做同设置 static，为新 T/O 提供一致的宿主能量；
- 没有再做 T/O 的 `5×5×4` final static；
- 旧 `5×5×4` Zr96 与新 `4×4×3` T/O 不能混合计算当前溶解能。

新旧 Zr96 的 E0 相差约 `0.11917 eV`。这不是旧 CPU 结果错误，而是 k 网格、电子阈值和
实空间投影设置不同造成的绝对能量差；因此必须用新 Zr96 static 与新 T/O 配套。

H2 是独立分子化学势参考，采用 Gamma-only、`EDIFF=1E-8`、`LREAL=.FALSE.` 的既有已收敛
结果，Job `62040677` 在 2 步内完成，`E0=-6.76804846 eV`。它不因周期性 Zr96/T/O 改用
DCU 和 `4×4×3` 网格而重复计算。

## 验收结果

| 任务 | Job ID | 离子步 | 最终 E0 (eV) | 最大力 (eV/Å) | 状态 |
| --- | ---: | ---: | ---: | ---: | --- |
| Zr96 static | `62149576` | — | -818.11976349 | 0.00000000 | 电子收敛 |
| H2 relax | `62040677` | 2 | -6.76804846 | 0.00328400 | 电子/离子收敛 |
| T relax | `62134983` | 13 | -821.95359478 | 0.00874278 | 电子/离子收敛 |
| O relax | `62148446` | 12 | -821.89069078 | 0.00420013 | 电子/离子收敛 |

服务器生成的原始摘要已同步为 [`initial_summary.tsv`](initial_summary.tsv)。各计算目录保留
输入、`.run_status`、Slurm/VASP 日志和科学输出；POTCAR、重启文件及 HDF5 输出不进入 Git。

## 能量与结构

采用最终 `energy(sigma->0)`，溶解能定义为：

```text
Esol(site) = E0[Zr96H(site)] - E0[Zr96] - 1/2 E0[H2]
```

| 位点 | Esol (eV/H) | 相对 T 能量 (eV) | 最近邻配位 |
| --- | ---: | ---: | ---: |
| T | -0.44980706 | 0.00000000 | 4 |
| O | -0.38690306 | +0.06290400 | 6 |

T 末态最近四个 Zr-H 距离为 `2.01772–2.04224 Å`；O 末态最近六个约为 `2.29655 Å`。
两者 H 位点相距 `1.98514991 Å`。这些结果确认 T/O 是不同配位驻点，且当前设置下 T 比 O
低 `62.904 meV`。本项目未独立完成声子验证，因此 O 位稳定性仍依赖文献支持。

完整分析见 [Zr96-H 初始数据分析与归纳](Zr96-H初始数据分析与归纳-20260728.md)。机器可读表格
和图片位于 [`analysis/`](analysis/)，可由 [`../tools/analyze_initial.py`](../tools/analyze_initial.py)
重新生成。

![Initial Zr96-H summary](analysis/05_share_summary.png)

## 从仓库内结果复算

从仓库根目录执行：

```bash
python tools/analyze_initial.py \
  --results-root 01_initial \
  --output-dir 01_initial/analysis
```

脚本检查必需文件、正常结束与收敛标志、原子数和顺序、轨迹步数及关键数值回归；它不读取
POTCAR，也不准备或启动 VASP。
