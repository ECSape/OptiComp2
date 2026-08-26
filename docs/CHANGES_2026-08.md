# OptiComp → OptiComp2 修改总结（2026‑08‑25 至 2026‑08‑26）

本文档汇总两天内对角分辨分光光度计（ARS/VAR 系统，Tyson 论文第 4 章）控制软件所做的全部工作：
对原 OptiComp v1.3.3 的审查结论、OptiComp2 重写的各层设计、两次硬件事故及其对策、工具与测试、
以及部署状态。原 `OptiComp` 仓库**未作任何修改**；所有代码在独立仓库 `OptiComp2`（部署于实验室电脑
`C:\OptiComp2`）。

---

## 1. 背景与决定

| 项目 | 内容 |
|---|---|
| 原软件 | OptiComp v1.3.3，Python 3.9 + Tkinter，11 个模块约 1 800 行 |
| 审查 | 2026‑08‑25 全函数级审查，35 项发现（均经对抗性核验） |
| 决定 | 不修补原代码；按“协议层 → 光谱仪 → 序列 → 分析 → GUI”的顺序做洁净重写，每一步在仪器旁验证 |
| 新软件 | OptiComp2，5 个目录 / 38 个受版本控制文件（不含 data/）约 9 800 行（含 9 个测试文件、129 个单元测试） |

### 1.1 原软件审查中最重要的发现

1. **运动命令无校验**：ELL18 样品台约 4 % 的 `ma` 命令被静默吞掉（无回复、不动、随后 GS00），原软件不核对最终位置，因此旧数据中可能存在角度错误的谱。
2. **饱和未检测**：57 个存档数据集中 33 个含 65535 截顶的 VAR 谱（固定 `HARD_IT`，无饱和检查）。
3. **暗底积分时间不匹配**：`Dark.csv` 固定 1000 ms 采集，而 VAR 谱用 `HARD_IT`，扣暗底不严格。
4. **DLL 多线程调用**：GUI 线程与扫描线程并发调用 BWTEKUSB.dll（未声明线程安全）。
5. **有效像素切片差一**：论文规定 254–2030（含），代码切 1776 个像素，丢掉 2030。
6. **守门规则未落实**：论文要求 step ≥ 1，代码未检查。
7. **参考自动积分时间定标未启用**：论文 4.2.3.3 描述的 80°/S 定标只剩注释掉的代码（`spectrometerframework.py:99-122`）和从不被调用的 `stageframework.setintegrationtime()`，实际使用固定 `HARD_IT`。
8. **S/P 文件标签实为正确**（TEM 模式）：14 个玻璃/ARC 数据集中 `*_P_*` 在 56° 出现 Brewster 极小值，`*_S_*` 单调上升；只有单偏振模式的 UI 标签与文件相反。
9. **样品台偏置常量不一致**：`stageframework.py` 103°，`hardwaremanager.py` 105°（VAR 扫描实际用 105°）。
10. 探测臂速度原程序发送 `2sv32` = 0x32 = 50 %（模块默认 64 %）。

---

## 2. OptiComp2 架构

```
hw/          elliptec.py   Elliptec ELLx 协议层（解析每条回复、重发、位置校验、保护回零）
             bwtek.py      BWTEKUSB.dll 封装（返回码检查、单线程、自动 IT、reopen/recover）
             stagestate.py 电机状态记录/比对（stage_state.json）
             config.py     几何常量、软限位、速度、保护列表
tools/       sequence.py   Step 构建器 + Runner（校验移动、清单、快门安全）
             manual_gui.py / *_panel.py / ui_theme.py   Tk GUI（v1.0，侧栏布局）
             monitor.py, cycle_test.py, restore_stages.py, usb_reset.py, shutter_close.py,
             ell_probe.py, spec_probe.py, demo_hw.py, ui_render.py
analysis/    var.py（式 4.10 反射率）, standards.py（Si Fresnel 标准、BK7、平板反演）
tests/       9 个文件，129 个测试（全部用 fake 串口 / fake DLL，无需硬件）
```

**线程模型**：DLL 只在 `SpectroWorker` 线程调用；手动命令走 `HardwareWorker`，序列在 `SpectroWorker` 上运行（Runner 从该线程驱动电机），
序列运行期间拒绝手动提交，二者不会交错；总线每次收发另有锁串行化。Tk 线程只通过队列提交任务、用 `after()` 取结果。

---

## 3. 各层的关键改动

### 3.1 Elliptec 协议层（`hw/elliptec.py`）

| 问题（实测） | 对策 |
|---|---|
| ELL18 偶尔吞掉命令（无回复、不动） | 发前读位置；无回复 → 轮询 GS；若位置未变则**重发**，最多 3 次 |
| 长移动后停在目标前几百分之一度 | 目标 ±20 脉冲（0.05°）为到位；≤120 脉冲（0.3°）时发**纠正移动**；仍偏则带警告接受，超出 0.3° 报错 |
| 运动中只在结束时回 PO；ELL18 50 % 速度移动超过 5 s | 首等 2 s 后轮询 GS，运动超时 60 s |
| 读超时截断的回复行 | 无 `\n` 的行、长度不符的 payload 一律丢弃，不解码 |
| 串扰/延迟回复（如轮询时才到的 PO） | `query(expect=...)` 丢弃非预期类型 |
| 长移动结束时报 GS0A 但已到位 | 位置在容差内则记录并接受 |
| 机械超时 GS02 | `ma`/`fw`/`bw` 重试一次；`mr` 改为到原目标的 `ma`（不能从停住处再走一次相对量）；**`ho` 从不重试** |
| 快门被吞导致暗帧 | `fw`/`bw` 核验端点位置（容差 0） |
| 光纤臂（模块 2）回零会缠光纤 | `protected_home`：`home("2")` 必须 `force=True`，且原始命令通道拒绝向受保护地址发 `ho` |
| 模块类型/脉冲数硬编码 | 从 `IN` 回复读取型号、序列号、行程、脉冲/圈 |

### 3.2 光谱仪（`hw/bwtek.py`）

- 除 `bwtekSetTimingsUSB`（仅记录返回值）与关闭调用外，DLL 调用均设 `argtypes/restype` 并检查返回码；`read()` 用时异常（< 0.5×IT 或 > IT+5 s）记 WARNING。
- **自动积分时间**：目标峰值 85 % 满量程，接受带 78–92 %，线性外推 `IT' = IT·(0.85·65535 − base)/(peak − base)`，饱和则减半，最多 8 步；
  ≥ 4000 ms 仍无光则**中止**（不再“沿用”未收敛的 IT）。
- **参考定标在 S、P 两个偏振各做一次，取 min(IT_S, IT_P)**：漫反射白板上 P 通道在所有角度比 S 亮约 12 %（系统偏振通量），
  只按论文用 S 定标会使 P 谱饱和。
- **恢复阶梯** `recover()`：① close + reopen；② `pnputil /restart-device` 仅重启光谱仪 USB 设备（VID_16A3，等价于插拔但不碰 ELLB 所在集线器）
  → 等待重新枚举 → reopen。需要管理员权限；`run_manual_gui.bat` 自动提权。
- 饱和像素在分析中记为 NaN；暗底按积分时间分别存储（`dark_997ms.csv`），分析时匹配同 IT 的暗底。

### 3.3 电机状态记录（`hw/stagestate.py`，新）

- 序列每次移动后写入该模块的位置与状态码；断开/退出时写入四个模块的完整快照（位置、状态码、速度）到 `data/stage_state.json`；
- 每次连接/脚本启动时与基线比对：位置差 > 0.5°、状态码非 0、速度改变 → 报异常。
  无人值守脚本：任何异常都中止（`--force` 可绕过），但探测臂报 GS02（回零失败）时即使 `--force` 也拒绝运行；
  序列 Runner 每次移动前再核对一次：普通模块警告、探测臂中止；GUI 连接时只弹横幅与对话框提示，不阻止手动操作；
- 连接时把探测臂速度设回 50 %（模块断电后恢复默认 64 %）。

### 3.4 测量序列（`tools/sequence.py`）

- 步骤原语：`stage / shutter / set_it / auto_it / acquire / pause / restore_it / apply_min_it`；
  构建器：参考定标（80°，S+P，自动 IT）、暗底、单角、角度扫描、双光束 DB（含换端口盖暂停，固定 1000 ms 并恢复）。
- Runner：软限位（0–200°）；移动前检测“未经我们的运动”；`try/except BaseException` → 关快门 + 恢复 IT；
  `finally` 原子写 `manifest.json`（tmp + `os.replace`），损坏的清单移到 `manifest.json.corrupt_<时间>`。
- 守门：0 ≤ start < stop ≤ 80，step ≥ 1；无人值守时遇到 `pause` 直接中止而不是无限等待。
- 重复采集时替换旧清单记录；相同步骤块拒绝重复入队。

### 3.5 分析（`analysis/`）

- 式 4.10：`Rx = (Sx−Sd)/(Sy−Sd) · (Scy−Sd)/(Scx−Sd) · Ry`，暗底按 IT 匹配，IT 不同则按计数/ms 归一（线性探测器假设，记入 `notes`）。
- 有效像素 254–2030（含）= 1777 个。
- 标准：常数（白板）、Si Fresnel 表（`standards/silicon_TE/TM.csv`，仅实部 n，380 nm 以下标记无效）、BK7 Sellmeier、平板背面反射反演。

### 3.6 GUI（`tools/manual_gui.py` v1.0）

- 侧栏 5 页：仪器（连接 + 快速开始清单 + 电机状态 + 注意事项）、电机与快门、光谱仪、测量、分析；
- 常驻状态栏（串口/光谱仪/IT/快门/探测臂/序列）+ 任何页面可按的红色「关闭快门」；日志抽屉 Ctrl+L；
- 快捷键 Ctrl+1..5 切页、Ctrl+L 日志、Ctrl+R 运行、Esc 中止、F5 查询全部信息、Ctrl+Shift+S 保存日志；关快门是状态栏按钮，无快捷键；
- 序列运行时锁定手动操作；退出流程：中止 → 工作线程关快门 + 记录状态（30 s 上限）→ 光谱仪关闭 → 关串口；
- 演示模式 `--demo`（`DemoBus`/`DemoSpec`，不碰 COM4/DLL），`--screenshot DIR` 自动巡游截图。

### 3.7 工具

| 工具 | 用途 |
|---|---|
| `restore_stages.py` | 只读报告；`--safe --arm` 在有人监督下恢复参考（偏振片/样品台回零并停 S/185°，探测臂 50 % 速度、回零、停 44°），写基线；`--arm` 需交互终端输入 `YES` 确认（仅 `--yes` 可跳过） |
| `usb_reset.py` | 探测或 PnP 重启光谱仪 USB 设备 |
| `shutter_close.py` | 硬杀脚本后：`0bw` 并核验位置 0 |
| `monitor.py` | 无人值守稳定性监视（固定几何连续读谱 → CSV，三波段相对首帧变化） |
| `cycle_test.py` | 运动循环测试（sample/arm/scan/exchange/both），每循环取暗底，自动 `recover()` |
| `ell_probe.py`, `spec_probe.py` | 只读探测 |
| 停止方式 | 建 `logs/STOP` 文件 → 脚本关快门、存数据、记录状态后退出；禁止 taskkill |

---

## 4. 两次硬件事故与对策

### 4.1 2026‑08‑25：样品台吞命令
首次完整序列（`data/test_ref`）中 ELL18 连续两次忽略 `ma`。→ 协议层重发 + 位置校验（§3.1）。

### 4.2 2026‑08‑26 01:14 / 01:21：USB 供电事故
USB 拓扑（Get‑PnpDevice 核实）：根集线器 → 通用集线器 → {B&W Tek 光谱仪 VID_16A3&PID_2EC8；子集线器 → FTDI ELLB 总线 COM4；键鼠}。
ELLB 无外接电源，**任何**该集线器上的插拔都会给全部模块断电，而 ELL14/ELL18 上电自动回零：
偏振片 → 0°，样品台 185° → 102.9°，带光纤的探测臂回零被卡（GS02，位置读数 11° 无意义），速度复位到 64 %。
另：光谱仪连续 590 次 997 ms 读取后一次读阻塞 25 s 返回 −99，之后每个新进程 `GetDeviceCount()==0`，直到物理插拔重新枚举。

对策：状态记录与门禁（§3.3）、保护回零、GS02 不重试、`recover()` 软插拔（§3.2）、`RUNBOOK.md` 开机流程、
建议给 ELLB 外接 5 V 并关闭 USB 选择性挂起（`powercfg` 命令在 RUNBOOK B 节）。

---

## 5. 测试与部署

- `tests/`：129 个测试，本地（pytest）与实验室电脑（`py -m unittest discover -s tests`，无 pytest）均通过；
  覆盖协议编解码/截断回复/重发/纠正移动/保护回零、DLL 返回码与 pnputil 安全、Runner 安全（中止关快门、无操作员暂停、软限位）、
  monitor/cycle 的故障注入恢复（`--dry-fail-at`）、状态比对、GUI 功能对照表与布局审计（演示硬件）。
- 部署：`scp -r tools tests hw analysis ... Admin@100.68.49.11:C:/OptiComp2/`，SHA‑256 逐文件核对一致；每次部署后远程跑全量测试。
- 提交记录（OptiComp2 `main`，GitHub `ECSape/OptiComp2`）：`9ec747e` … `6194c65`，共 31 次提交，见 `git log`。

---

## 6. 已知问题 / 待办

- 截图为合成渲染（Mac 未授予屏幕录制权限），真机外观需在实验室确认。
- 布局审计覆盖 1280×820 与 1100×720 下的全部页面，但未覆盖高度 < 820 px 时拉开日志抽屉的情形（会压缩分析图）。
- 探测臂零点在 2026‑08‑26 事故中丢失，需按 RUNBOOK A 节由人监督恢复后再做任何测量。
- Si 运动循环测试（RUNBOOK C 节）尚未执行；白板与 Si 的对比用于区分“样品安装”与“探测臂/端口重复性”问题。
- ELLB 外接供电与光谱仪改接主机原生 USB 口，为硬件层面的根治建议。
