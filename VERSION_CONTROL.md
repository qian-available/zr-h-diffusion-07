# 07项目版本管理

## 范围

本Git仓库只管理 `07_h_diffusion_quickstart/`。使用私有GitHub仓库
`zr-h-diffusion-07`、单一 `main` 分支和阶段标签，不把整个 `Zr-smoke/` 纳入。

普通Git跟踪脚本、输入、结构、小型输出、派生表格、图和报告。Git LFS跟踪
`OUTCAR`、`vasprun.xml`、`XDATCAR` 和 `vasp.stdout`。POTCAR永不进入Git；
其版本和内容身份由 `inputs.sha256`、报告中的TITEL及SHA-256记录。当前NEB设置
`LWAVE=.FALSE.`、`LCHARG=.FALSE.`，因此不应产生WAVECAR或CHGCAR；二者仍作为安全
例外排除。

## 简单版本规则

只在以下时刻提交：

1. 一组输入经过本地检查、准备提交SCNet时；
2. 一个计算阶段结束且通过检查脚本时；
3. 分析表格、图和报告完成时。

不要在VASP运行过程中提交持续增长的输出。墙时续算目录若成为下一段输入，则在该段完成
后连同数据血缘一起提交。一次性启动失败且未被后续使用的目录只保留小型状态说明。

从当前基线开始，阶段标签固定为：

```text
v0.1-neb-inputs
v0.2-tt-pre-neb
v0.3-tt-cineb
v0.4-to-pre-neb
v0.5-to-cineb
v1.0-toy-diffusion-report
```

`v0.1-neb-inputs`同时作为现有初始分析和待提交NEB输入的首个可追溯基线；不为Git建立前的
历史状态补造提交。

## SCNet结果回传

SCNet当前环境为Git 1.8.3.1、无Git LFS、无GitHub SSH认证，因此不把服务器改造成GitHub
工作端。计算通过 `tools/check_neb.sh` 后，在服务器生成排除POTCAR、WAVECAR和CHGCAR的
阶段结果包：

```bash
bash tools/package_neb_result.sh 03_neb/01_tt_c
```

CI阶段把参数改为 `03_neb/01_tt_c/ci_01`。脚本先调用阶段检查，通过后才在07目录的上一级
生成 `.tar.gz` 和 `.sha256`；不覆盖同名包，也不递归打包下一阶段子目录。使用SCP/WinSCP
下载两份文件到本地，校验并解压进本仓库后，由本地Git LFS提交、推送和打标签。

任何计算提交前，`.run_status` 应记录实际输入所属的Git提交：

```text
git_commit=<40位提交SHA>
```

Git提交号用于连接输入版本，`inputs.sha256`继续用于锁定包括私有POTCAR在内的实际文件
内容，两者不能互相替代。
