# RIMKit：N2 安装与使用指南

本文只说明如何将 SOMA 人体动作重定向到 Noetix N2（机器人 ID：`n2`）。所有命令均在项目根目录执行。

## 1. 安装

### 前置条件

- Ubuntu Linux 或 macOS
- Conda
- Git
- 支持 C++17 的编译器（Ubuntu 通常安装 `build-essential` 即可）

从零开始安装：

```bash
git clone https://github.com/tmjeong1103/RIMKit.git
cd RIMKit

conda create -n rimkit python=3.10
conda activate rimkit
python -m pip install --upgrade pip

# 安装 RIMKit、GEM-X .pt 输入支持和 MP4/PNG 渲染支持。
python -m pip install -e ".[gemx,video]"
```

若已经位于项目目录，只需激活已有环境并安装：

```bash
cd /home/zsp/project/RIMKit
conda activate rimkit
python -m pip install -e ".[gemx,video]"
```

安装后检查 N2 模型和原生 C++ 计算后端：

```bash
rimkit backend --require-native
rimkit robots verify n2
```

两条命令都成功后才能开始运行。`native` 后端使用编译后的 C++ IK 和碰撞计算；N2 的完整流程比 `python` 后端明显更快。

## 2. 输入动作

RIMKit 根据文件扩展名自动识别输入格式。完整重定向流程至少需要 2 帧动作。

| 格式 | 典型来源 | 项目内置动作位置 | 额外依赖 |
| --- | --- | --- | --- |
| `.npz` | Kimodo SOMA77 | `examples/motions/kimodo/soma_rp_v11/` | 无 |
| `.pt` | GEM-X SOMA | `examples/motions/gem-x/` | PyTorch；按上面的 `.[gemx,video]` 安装后已具备 |

### 2.1 Kimodo `.npz`

内置的 N2 快速示例动作：

```text
examples/motions/kimodo/soma_rp_v11/stand_walk_run_stop.npz
```

该目录还包含慢走、后退走、侧步、跳跃落地等动作。自定义 Kimodo 文件可以放在任意位置，例如：

```text
motions/my_walk.npz
```

`.npz` 至少需要以下两个数值数组：

| 键名 | 形状 | 含义 |
| --- | --- | --- |
| `posed_joints` | `(T, 77, 3)` | SOMA77 全局关节位置 |
| `global_rot_mats` | `(T, 77, 3, 3)` | SOMA77 全局关节旋转矩阵 |

可选键为 `fps`（标量帧率）和 `foot_contacts`（`(T, 4)` 或 `(T, 6)` 的脚接触标签）。未提供 `fps` 时默认按 30 Hz 处理。输入不能包含 pickle/object 数组、NaN 或 Inf。

### 2.2 GEM-X `.pt`

内置示例动作：

```text
examples/motions/gem-x/rapid_stepping.pt
```

自定义 GEM-X 文件可以放在任意位置，例如：

```text
motions/my_motion.pt
```

`.pt` 必须是 GEM-X 的 SOMA 输出，至少包含：

```text
body_params_global/
  body_pose
  global_orient
  transl
net_outputs/
  static_conf_logits
```

其中 `body_pose` 为 `(T, 228)` 或 `(T, 76, 3)`，`global_orient` 和 `transl` 为 `(T, 3)`，`static_conf_logits` 为 `(T, 6)`（允许带一个批次维度 `(1, T, 6)`）。GEM-X 文件本身不保存帧率；默认按 30 Hz 处理，也可以在命令中通过 `--fps` 显式指定。

### 2.3 运行前验证输入

先验证文件格式，格式不正确时不会开始 N2 重定向：

```bash
# 验证 Kimodo 输入
rimkit validate examples/motions/kimodo/soma_rp_v11/stand_walk_run_stop.npz

# 验证 GEM-X 输入；--fps 仅在原始动作确实不是 30 Hz 时修改。
rimkit validate examples/motions/gem-x/rapid_stepping.pt --fps 30
```

## 3. 运行 N2 重定向

### 3.1 推荐：Kimodo 输入、生成 MP4 和缩略图

```bash
conda activate rimkit

rimkit run \
  examples/motions/kimodo/soma_rp_v11/stand_walk_run_stop.npz \
  --method core \
  --robot n2 \
  --output runs/n2-stand-walk-run \
  --backend native \
  --video \
  --thumbnail
```

`--backend native` 是推荐设置：它使用已安装的 C++ 后端。不要写 `--backend python`，除非是在排查原生后端问题。

上述命令的结果在：

```text
runs/n2-stand-walk-run/stand_walk_run_stop/n2/
├── final/robot_motion.npz     # 最终 N2 关节动作
├── preview/final.mp4          # N2 预览视频
├── preview/final.png          # N2 缩略图
├── stages/                    # DMR、碰撞、FPA 等中间结果
└── manifest.json              # 本次运行的配置和输入记录
```

`--output` 指定的是输出根目录；实际目录总是：

```text
<输出根目录>/<输入文件名（不含扩展名）>/n2/
```

每次重新运行同一个输入时，请使用新的 `--output` 目录，例如 `runs/n2-stand-walk-run-v2`，避免覆盖已有候选结果。

### 3.2 GEM-X 输入示例

```bash
rimkit run \
  examples/motions/gem-x/rapid_stepping.pt \
  --method core \
  --robot n2 \
  --output runs/n2-rapid-stepping \
  --fps 30 \
  --backend native \
  --video \
  --thumbnail
```

若 GEM-X 输入的实际帧率不是 30 Hz，将 `--fps 30` 改为真实帧率。不要用 `--fps` 来减少计算时间；它会改变动作的时间尺度，而不会减少帧数。

### 3.3 使用自己的动作文件

Kimodo `.npz`：

```bash
rimkit run \
  motions/my_walk.npz \
  --method core \
  --robot n2 \
  --output runs/n2-my-walk \
  --backend native \
  --video \
  --thumbnail
```

GEM-X `.pt`：

```bash
rimkit run \
  motions/my_motion.pt \
  --method core \
  --robot n2 \
  --output runs/n2-my-motion \
  --fps 30 \
  --backend native \
  --video \
  --thumbnail
```

### 3.4 更快地只生成最终动作

不需要 MP4、PNG 或中间阶段文件时，使用下面的命令。它仍会完成 DMR、碰撞处理和 FPA，只是不渲染且不保存各阶段 `.npz` 文件：

```bash
rimkit run \
  examples/motions/kimodo/soma_rp_v11/stand_walk_run_stop.npz \
  --method core \
  --robot n2 \
  --output runs/n2-stand-walk-run-fast \
  --backend native \
  --no-stages
```

结果仍会写入：

```text
runs/n2-stand-walk-run-fast/stand_walk_run_stop/n2/final/robot_motion.npz
```

## 4. 读取最终 N2 动作

最终结果是安全的 NumPy `.npz` 文件，可按如下方式读取：

```bash
python - <<'PY'
import numpy as np

path = "runs/n2-stand-walk-run/stand_walk_run_stop/n2/final/robot_motion.npz"
motion = np.load(path, allow_pickle=False)

print(motion.files)
print("qpos shape:", motion["qpos"].shape)
PY
```

`qpos` 的形状为 `(帧数, 25)`。N2 在本项目中的状态维度为 `nq=25`、速度维度为 `nv=24`、执行器维度为 `nu=18`。
