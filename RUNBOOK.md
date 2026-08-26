# 开机 / 事故后操作手册（2026-08-27 起）

前提：原 OptiComp 程序未运行（COM4 与光谱仪独占）；`C:\OptiComp2` 已是最新部署。

## A. 光纤与探测臂（先做，不开灯也能做）

1. 看一眼光纤：走线松弛、没有缠在探测臂 (模块 2) 上。
2. 只读报告，不动任何电机：

       py tools\restore_stages.py

   预期：模块 2 报 GS02（上次自动回零失败，零点丢失），偏振片约 0°，样品台约 103°，快门关。
3. 人在旁边盯着探测臂、光纤留足余量，再恢复参考：

       py tools\restore_stages.py --safe --arm

   输入 `YES` 确认。脚本会：偏振片/样品台回零并停到 S (236°) / 185°；探测臂回零 (ho0) 后停到 44°；
   写入基线 `data\stage_state.json`。之后所有工具连接时都会与这份基线比对，异常则拒绝运动。
   若回零时探测臂再次卡住（GS02），立刻关掉 ELLB 供电（拔 ELLB 的 USB）再检查光纤，**不要**重复回零。

## B. USB 软重启验证（有人在场时做一次，以后就不用拔线）

`run_manual_gui.bat` 现在会自动请求管理员权限（出现 UAC 对话框点“是”）——GUI 里的
「恢复 (重开/USB 重启)」按钮需要提权才能重启 USB 设备。

以管理员 PowerShell：

    py tools\usb_reset.py

它对光谱仪做 PnP 重启（等价于插拔，但只作用于光谱仪这一个设备），然后探测读数。
观察：探测臂不动、`stage_state` 无异常、probe OK。以后 DLL 挂死（读数 -99 / 设备数 0）时：
GUI 光谱仪页「恢复 (重开/USB 重启)」按钮，或 monitor / cycle_test 会自动调用同一逻辑。

关闭 USB 选择性挂起（一次性，管理员）：

    powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
    powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
    powercfg /setactive SCHEME_CURRENT

## C. 光源与 Si 运动循环测试

1. 开光源，预热 ≥ 30 min（快门关）。
2. Si 装好（20×20 片在 30×30 夹具里，确认贴紧、不晃）：

       py tools\cycle_test.py --cycles 3 --frames 20 --moves both --tag si

   日志 `logs\cycle_*.log` 每段给出三个波段相对基线的变化。若某次运动后掉 >2 %，再分别跑
   `--moves scan`、`--moves exchange`、`--moves sample`、`--moves arm` 定位是哪种运动造成的。
3. 换白板，重复：`--tag white`。白板稳定而 Si 掉幅 → 样品安装/倾角问题（镜面样品对角度敏感）；
   两者都掉 → 探测臂/积分球端口重复性问题。
4. 都稳定后再用 GUI 做完整流程：参考定标 (80°/S,P) → 白板扫描 → Si 扫描 → DB 交换 → 分析页。

## D. 结束

GUI「断开」或退出会记录电机状态并关快门；脚本结束时同样记录。之后不要再插拔该 USB 集线器上的任何线。
