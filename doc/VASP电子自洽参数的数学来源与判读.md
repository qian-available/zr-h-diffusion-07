# VASP 电子自洽参数的数学来源与判读

## 1. 问题的起点：固定原子位置，求电子基态

结构弛豫包含两层循环。外层改变原子位置，内层在每一个固定的离子构型下重新求电子基态。
`OSZICAR` 中以 `DAV:` 或 `RMM:` 开头的表格描述的是内层电子自洽循环；只有电子问题先
收敛，当前离子构型的总能和原子力才有意义。

在某一个固定离子构型 $\mathbf{R} = \{\mathbf{R}_I\}$ 下，Kohn–Sham 密度泛函理论要求
在轨道正交约束和电子数约束下，使电子自由能泛函达到驻值。对采用有限展宽的金属体系，
可把这个目标写成

$$
\begin{aligned}
F[\rho] &= T_s[\rho] + E_H[\rho] + E_{xc}[\rho] \\
&\quad + \int V_{\mathrm{ext}}(\mathbf{r}; \mathbf{R})\,\rho(\mathbf{r})\,\mathrm{d}\mathbf{r} \\
&\quad + E_{II}(\mathbf{R}) - T S_{\mathrm{el}}.
\end{aligned}
$$

这里 $T_s$ 是无相互作用电子动能，$E_H$ 是 Hartree 能，
$E_{xc}$ 是交换关联能，$V_{\mathrm{ext}}$ 是固定离子产生的外势，
$E_{II}$ 是离子间相互作用能，$-T S_{\mathrm{el}}$ 是电子展宽对应的熵项。

对轨道作变分，并用拉格朗日乘子维持正交约束，得到广义 Kohn–Sham 方程

$$
\begin{aligned}
\hat{H}[\rho]\,|\psi_{n\mathbf{k}}\rangle &= \varepsilon_{n\mathbf{k}}\,\hat{S}\,|\psi_{n\mathbf{k}}\rangle, \\
\langle\psi_{m\mathbf{k}}|\,\hat{S}\,|\psi_{n\mathbf{k}}\rangle &= \delta_{mn}.
\end{aligned}
$$

$\hat{S}$ 是 PAW 方法中的重叠算符。由求得的轨道、占据数和 k 点权重构造新电荷密度：

$$
\rho_{\mathrm{out}}(\mathbf{r}) = \sum_{\mathbf{k},n} w_{\mathbf{k}} f_{n\mathbf{k}}\,|\psi_{n\mathbf{k}}(\mathbf{r})|^2,
$$

其中 PAW 的芯区修正由 VASP 内部补全。关键困难在于哈密顿量依赖电荷密度，而电荷密度又
由哈密顿量的本征态决定。因此真正要求解的是一个非线性不动点问题：

$$
\rho_* = G[\rho_*].
$$

这里 $G$ 表示“由输入密度建立哈密顿量、求解本征态、再生成输出密度”的完整
映射。电子自洽循环就是寻找这个不动点。

## 2. 一次电子自洽迭代做了什么

设第 $m$ 次电子迭代的输入密度为 $\rho_{\mathrm{in}}^{(m)}$。VASP 依次执行：

1. 用 $\rho_{\mathrm{in}}^{(m)}$ 构造 $\hat{H}^{(m)}$；
2. 迭代求解 $\hat{H}^{(m)}\psi = \varepsilon\,\hat{S}\,\psi$；
3. 用轨道和占据数生成 $\rho_{\mathrm{out}}^{(m)}$ 并计算自由能；
4. 混合输入、输出及历史密度，得到下一次输入密度：

$$
\rho_{\mathrm{in}}^{(m+1)} = M\bigl(\rho_{\mathrm{in}}^{(m)},\,\rho_{\mathrm{out}}^{(m)},\,\ldots\bigr).
$$

这一步同时包含两个不同的数值问题：在固定哈密顿量下把本征方程解准，以及让输入、输出
电荷密度彼此一致。`OSZICAR` 的各列正是对这两个问题及其计算代价的压缩记录。

## 3. 从本征方程导出 `rms` 与 `ncg`

对于当前近似轨道，定义 Kohn–Sham 本征方程残差

$$
|r_{n\mathbf{k}}^{(m)}\rangle = \bigl(\hat{H}^{(m)} - \varepsilon_{n\mathbf{k}}^{(m)}\hat{S}\bigr)\,|\psi_{n\mathbf{k}}^{(m)}\rangle.
$$

精确本征态满足 $r_{n\mathbf{k}} = 0$。Davidson 算法利用残差产生新的搜索方向，不断扩大
子空间并改进轨道。把所有相关能带和 k 点的残差范数组合成一个均方根量，可概念性地写成

$$
\mathrm{rms} \sim \sqrt{
  \frac{
    \displaystyle\sum_{n,\mathbf{k}} w_{\mathbf{k}} f_{n\mathbf{k}}\,\langle r_{n\mathbf{k}}|r_{n\mathbf{k}}\rangle
  }{
    \displaystyle\sum_{n,\mathbf{k}} w_{\mathbf{k}} f_{n\mathbf{k}}
  }
}.
$$

VASP 的具体归一化属于程序实现细节，但物理含义明确：`rms` 越小，当前轨道越接近当前
哈密顿量的本征态。它是本征求解质量的诊断量，不是当前 `EDIFF` 直接比较的对象。

平面波本征求解中最主要的重复运算之一是把哈密顿量作用到试探波函数上，即
$\hat{H}|\psi\rangle$。`ncg` 记录本次电子迭代中这类操作的大致次数。因此 `ncg` 是工作量
计数器：数值大表示该步代价高，不表示结果更不准确，也不是收敛判据。

## 4. 从密度不动点导出 `rms(c)`

定义当前电子迭代的电荷密度残差

$$
\Delta\rho^{(m)}(\mathbf{r}) = \rho_{\mathrm{out}}^{(m)}(\mathbf{r}) - \rho_{\mathrm{in}}^{(m)}(\mathbf{r}).
$$

自洽解必须满足 $\Delta\rho = 0$。`rms(c)` 是这个密度差的均方根度量，可概念性地写成

$$
\mathrm{rms}(c) \sim \sqrt{
  \frac{1}{\Omega}\int_{\Omega}\bigl|\Delta\rho^{(m)}(\mathbf{r})\bigr|^2\,\mathrm{d}\mathbf{r}
}.
$$

因此 `rms(c)` 反映电荷混合是否接近不动点。持续下降通常说明混合稳定；长时间振荡或增大
常见于电荷振荡或不合适的混合参数。它同样是诊断量，不直接与 `EDIFF` 比较。某些初始
Davidson 步尚未开始电荷混合，所以对应行可能没有 `rms(c)`。

## 5. 从自由能与能带能量导出 `E`、`dE` 和 `d eps`

第 $m$ 次电子迭代得到一个当前电子自由能估计 $E^{(m)}$。`OSZICAR` 中的
`E` 就是这个量，单位为 eV，且对应同一个固定离子构型。

相邻电子迭代的自由能变化定义为

$$
\mathrm{d}E^{(m)} = E^{(m)} - E^{(m-1)}.
$$

仅检查 `dE` 还不够，因为自由能由多个项组成，不同项的误差可能偶然抵消。为独立监视
固定势下本征值是否稳定，考虑能带能量

$$
E_{\mathrm{band}} = \sum_{n,\mathbf{k}} w_{\mathbf{k}} f_{n\mathbf{k}}\,\varepsilon_{n\mathbf{k}}.
$$

`d eps` 是 VASP 给出的固定当前势时能带能量（本征值贡献）的变化，可概念性地记作

$$
\mathrm{d}\varepsilon^{(m)} = \Delta E_{\mathrm{band}}^{(m)}\Big|_{V_{\mathrm{eff}}\,\mathrm{fixed}}.
$$

它不是某一条能带的单个本征值差，而是一个汇总的、单位为 eV 的能带能量变化量。
`dE` 小表示总自由能趋于稳定，`d eps` 小表示固定势下的轨道和本征值也已稳定。二者同时小，
才能排除“总能量误差偶然抵消，但电子态仍未解准”的情况。

VASP 对常规电子自洽循环采用的全局停止条件是

$$
|\mathrm{d}E| < \mathrm{EDIFF} \quad\text{且}\quad |\mathrm{d}\varepsilon| < \mathrm{EDIFF}.
$$

本项目的 DCU T/O 输入使用 `EDIFF=1E-6`，所以两项都必须小于 $10^{-6}$ eV。判断时取
绝对值，正负号只表示本次迭代中能量上升或下降。

## 6. 七个字段的自然含义与作用

```text
       N       E                     dE             d eps       ncg     rms          rms(c)
DAV:   1    ...
DAV:   2    ...
```

| 参数 | 数学来源与含义 | 实际作用 |
| --- | --- | --- |
| `N` | 当前固定离子构型中的电子迭代编号 $m$ | 显示本轮电子自洽用了多少步；每进入新的离子步通常重新从 1 计数 |
| `E` | 当前密度和轨道给出的电子自由能 $E^{(m)}$ | 观察当前电子解的能量位置，并用于构造 `dE` |
| `dE` | $E^{(m)} - E^{(m-1)}$ | 与 `EDIFF` 比较，检查总自由能是否稳定 |
| `d eps` | 固定当前势时能带能量/本征值贡献的变化 | 与 `EDIFF` 比较，防止总能误差抵消掩盖未收敛的电子态 |
| `ncg` | 本次电子迭代中 $\hat{H}\|\psi\rangle$ 等核心操作的大致次数 | 衡量本步计算工作量；不用于判断收敛 |
| `rms` | $(\hat{H} - \varepsilon\hat{S})\psi$ 的综合残差范数 | 诊断当前轨道解本征方程的质量；不直接与 `EDIFF` 比较 |
| `rms(c)` | $\rho_{\mathrm{out}} - \rho_{\mathrm{in}}$ 的电荷密度残差 | 诊断电荷混合和自洽不动点的稳定性；不直接与 `EDIFF` 比较 |

## 7. 与离子步输出的边界

电子表中的 `dE` 与离子步汇总中的 `d E` 名称相似，但不是同一个比较：

```text
DAV:  7 ... dE ... d eps ...   # 同一离子构型内，相邻电子迭代的变化
11 F= ... E0= ... d E=...      # 相邻离子构型之间的总能量变化
```

电子自洽完成后，VASP 才用收敛电子密度计算当前构型的能量和原子力。`F=` 是电子自由能，
不是原子力；本项目正式能量比较使用 `E0`，即 `energy(sigma->0)`。原子力应从 `OUTCAR` 的
`TOTAL-FORCE` 或以下汇总读取：

```text
FORCES: max atom, RMS
```

当前 `EDIFFG=-0.01` 是外层离子弛豫的力判据，与电子表中的 `rms`、`rms(c)` 无关。

## 8. 最小判读规则

对于当前 `ALGO=Normal`、`EDIFF=1E-6` 的任务，一组电子迭代是否通过，只需检查最后一条
`DAV:` 记录：

$$
|\mathrm{d}E| < 10^{-6}\,\mathrm{eV},\qquad |\mathrm{d}\varepsilon| < 10^{-6}\,\mathrm{eV}.
$$

`rms` 和 `rms(c)` 用于解释为什么收敛快、慢或振荡，`ncg` 用于认识计算代价；三者都不
替代 `EDIFF`。当电子循环通过后，再检查外层结构优化的最大原子力是否小于
$|\mathrm{EDIFFG}| = 0.01$ eV/Å。这就把电子收敛和离子收敛严格分开。

## 参考依据

- [VASP Wiki: Electronic minimization](https://vasp.at/wiki/Electronic_minimization)
- [VASP Wiki: ALGO](https://vasp.at/wiki/ALGO)
- [VASP Wiki: EDIFF](https://vasp.at/wiki/EDIFF)
- [VASP Wiki: OSZICAR](https://vasp.at/wiki/OSZICAR)
