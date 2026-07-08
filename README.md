# beep-locator

Locate fixed-frequency beep markers in PCM/WAV audio and write time-point result files beside the audio. Use when asked to find蜂鸣特征, beep marker positions, fixed two-beep sync tones, or analyze mono/multichannel PCM/WAV files with header-first format parsing, channel filtering, and reliable actual-beep positioning.

## Skill layout

- `agents/openai.yaml`
- `references/beep-detection.md`
- `scripts/batch_beep_locator.py`
- `scripts/locate_beep.py`
- `scripts/postprocess_8ch_edges.py`
- `scripts/scan_header_aware_edges.py`
- `SKILL.md`

## Install the skill

Copy this folder into:

```text
~/.codex/skills/beep-locator
```

Then restart Codex.

## Usage and workflow

## 目标

定位音频中固定频率蜂鸣特征，并把结果写到对应音频所在目录。默认蜂鸣模板来自已确认样本：约 `1000Hz`，结构为 `约1秒蜂鸣 + 约1秒间隔/静音 + 约1秒蜂鸣`，总长约 `3秒`。

后续处理同类音频时，通常不需要再依赖 `你好小奥.wav`；只要用户提供待测音频和音频格式即可。只有当蜂鸣频率、节奏结构、采样格式不确定或已变化时，才要求用户提供新的参考音频来重新标定。

## 适用边界

- 本 skill 的核心任务是：在没有 `yc.txt`、人工标注或真值文件的情况下，基于音频本身找出实际蜂鸣位置。
- 用户通常只会提供音频文件、目录和格式信息；必须优先完成格式解析、逐通道检测、可靠通道共识和起止点输出。
- `yc.txt`、人工标注、准确率统计或历史误差对比只属于外部验证材料，不作为默认输入，也不要写入常规检测流程。
- 不要把某次项目的对比结论、准确率表、异常清单或固定数据集文件名固化进 skill；这些内容应留在项目目录的临时脚本/报告中。

## 目录约定

- `scripts/`：放可直接执行的检测脚本。当前主脚本是 `scripts/locate_beep.py`。
- `references/`：放算法说明、阈值含义、输出规范等参考资料。需要解释或调整检测逻辑时读取。
- `assets/`：仅在以后需要固定模板、示例音频或可视化资源时使用；不要把用户录音或运行结果放进来。
- 生成结果：默认不要写入 skill 目录。结果文件必须写到待测音频所在目录，例如 `{音频名}_beep_result.txt`。若用户要求集中输出，在音频所在目录下新建 `beep_results/`，不要污染 skill 本体。

## 标准流程

1. 确认音频格式。所有音频无论扩展名和通道数，都必须先检查文件头；只要存在 `RIFF/WAVE` 等可识别头信息，就以头信息里的采样率、通道数、位深、data 偏移为准。只有没有头信息时，才使用目录名、文件名或用户参数作为兜底。
2. 只把音频自身实际存在的蜂鸣写成“实际蜂鸣”。不要把同组通道同步推断窗口混写成实际检测结果。
3. 用固定频率检测定位候选：围绕 `1000Hz` 做窄带/傅里叶分量分析，按小帧计算 1kHz 能量。
4. 合并连续高能量帧，寻找 `约1秒 + 间隔约1秒 + 约1秒` 的双蜂鸣结构。
5. 回到原始 PCM 对候选段复核：计算 1kHz 分量占总能量比例、RMS、峰值、零样本比例。
6. 对重叠候选去重，保留 1kHz 占比和结构评分更好的结果。
7. 把秒数转换成 `X分YY秒ZZZ毫秒`，写入音频同目录的结果文件。

## 推荐脚本

优先运行 bundled script，不要每次重写检测代码：

```powershell
python C:\Users\Administrator\.codex\skills\beep-locator\scripts\locate_beep.py "E:\path\audio.pcm" --sample-rate 16000 --channels 1 --sample-format s16le --edge-only
```

批量目录、多通道或大音频优先使用头信息优先的批处理脚本：

```powershell
python C:\Users\Administrator\.codex\skills\beep-locator\scripts\batch_beep_locator.py `
  --root E:\path\audio_dir `
  --frequency 1000 `
  --edge-window-sec 3600
```

如果任务要求只输出首尾证据完整的位置，并把弱候选/缺失项列为未找到，使用边界扫描脚本：

```powershell
python C:\Users\Administrator\.codex\skills\beep-locator\scripts\scan_header_aware_edges.py `
  --root E:\path\audio_dir `
  --output-prefix beep_edges_reliable `
  --official-min-support-ratio 0.50 `
  --official-min-ratio 0.50
```

该脚本默认只把证据完整的音频写入 `*_positions.csv`；未找到、只找到头/尾、tone ratio 偏低、主通道支持不足的音频写入 `*_not_found.csv`，弱候选保留在 `*_audit_candidates.json`。同时会生成 `*_review.csv` 供人工校验，包含头尾候选位置和备注，例如“稳定”“特征偏弱”“mic缺陷/通道不足”“仅尾部”“未检出可靠特征”。

常用参数：

- `--frequency 1000`：目标蜂鸣频率，默认 `1000Hz`。
- `--sample-rate 16000`：PCM 采样率；WAV 会读取文件头。
- `--channels 1 --channel-index 0`：通道数和分析通道。
- `--edge-only`：只保留录音开头和结尾附近的蜂鸣，适合本批同步蜂鸣场景。
- `--no-write-result`：只打印结果，不写文件。
- `--out-name "{stem}_beep_result.txt"`：自定义同目录结果文件名。

## 当前使用方式

后续默认场景是用户没有 `yc.txt`，只给音频文件/目录和音频格式信息，要求找出 bee 位置。按以下方式执行：

1. 单个音频：先确认路径存在，再按文件头优先解析格式；如果没有头信息，才使用用户给出的采样率、位深、通道数。
2. 目录批量：使用 `batch_beep_locator.py` 或 `scan_header_aware_edges.py` 扫描目录；大音频必须按首尾窗口或流式方式处理，避免整文件一次性载入内存。
3. 单通道或拆分单通道：直接在该音频自身波形上检测，输出实际蜂鸣开始/结束位置；不要用其他通道推断成实际蜂鸣。
4. 多通道未拆分：先逐通道看 RMS、峰值、零样本、削顶、是否形成蜂鸣结构；剔除空通道、常量通道、削顶异常和明显不是蜂鸣的通道。
5. 4/6/8 通道：优先使用前置 mic 通道形成共识，后置回采/降噪通道只作参考确认；如果多个可靠 mic 在同一位置检出，合并为完整音频的最可能 bee 位置。
6. 首尾蜂鸣任务：通常只输出头部和尾部两个蜂鸣窗口；如果前后 1 小时均无可靠特征，记录“未检测到可靠蜂鸣特征”，不要硬填数值。
7. 输出结果：简洁列出音频名、bee 开始位置、bee 结束位置；必要时附通道序号、备注、未检出原因。不要输出与定位无关的统计噪声。

常用命令模板：

```powershell
# 单文件，用户已给裸 PCM 格式
python C:\Users\Administrator\.codex\skills\beep-locator\scripts\locate_beep.py `
  "E:\path\audio.pcm" `
  --sample-rate 16000 --channels 1 --sample-format s16le --edge-only

# 目录批量，优先文件头，首尾窗口扫描
python C:\Users\Administrator\.codex\skills\beep-locator\scripts\batch_beep_locator.py `
  --root E:\path\audio_dir `
  --frequency 1000 `
  --edge-window-sec 3600

# 证据完整才输出正式结果，弱候选进 review/not_found
python C:\Users\Administrator\.codex\skills\beep-locator\scripts\scan_header_aware_edges.py `
  --root E:\path\audio_dir `
  --output-prefix beep_edges_reliable `
  --official-min-support-ratio 0.50 `
  --official-min-ratio 0.50

# 已有批量明细时，对 4/6/8 通道做边界后处理
python C:\Users\Administrator\.codex\skills\beep-locator\scripts\postprocess_8ch_edges.py `
  --root E:\path\audio_dir `
  --summary-json E:\path\audio_dir\beep_results\beep_summary.json `
  --out-csv E:\path\audio_dir\beep_results\bee_positions_profile.csv `
  --simple-csv E:\path\audio_dir\beep_results\bee_positions_profile_simple.csv `
  --profile auto
```

## 格式解析优先级

为避免目录名或用户参数错误导致错位解析，所有单通道、4 通道、6 通道、8 通道以及拆分通道音频都按以下优先级确认格式：

1. 先读取文件头：`.wav` 或扩展名为 `.pcm` 但内容带 `RIFF/WAVE` 头时，必须按头信息解析，不能按目录名覆盖。
2. 从头信息读取 `sample_rate`、`channels`、`bits_per_sample`、`data` chunk 偏移和帧数；例如 `.pcm` 文件也可能实际是 `16000Hz/6ch/32bit WAV`。
3. 只有没有可识别头信息时，才根据路径中的 `16k16bit8通道`、`48k16bit8通道`、`单通道` 等描述推断裸 PCM 格式。
4. 如果文件大小与推断帧大小不对齐，不能继续硬解析；应记录为格式疑似异常，或先尝试常见头偏移/位深复核，再输出需要人工确认。
5. 后续蜂鸣检测、通道筛选、边界合并必须基于正确解码后的物理通道数据进行。

## 8 通道边界后处理逻辑

处理 8 通道 PCM/WAV 时，不能简单把所有检出通道按 tone ratio 加权合并边界。已验证的通用策略是：先用多通道确认蜂鸣事件，再用主 mic/完整蜂鸣通道确定起止边界，回采/降噪/短 burst 通道只做确认，避免把起点或终点拉偏。

### 通道角色

1. 先逐通道统计 RMS、峰值、零样本比例、是否常量/DC、是否削顶、是否能形成 1kHz 双蜂鸣。
2. 剔除全零、近似全零、常量/DC、削顶异常、以及有能量但没有 1kHz 双蜂鸣结构的通道。
3. 如果设备 profile 已知，优先使用 profile：
   - OBU 48k/16bit/8通道：`ch0,ch1` 作为 mic 主边界通道，`ch5,ch6` 作为回采/降噪参考通道，`ch2,ch3,ch4,ch7` 通常不参与边界。
4. 如果 profile 未知，自动把 `continuous_pair` 且有多通道共识的通道作为主边界候选；`burst_pair`、短时高 tone ratio、明显延迟的通道只作为参考确认。

### 起止点修正

- 开始点：如果主边界通道起点比初始合并结果更早并超过阈值，修正到主边界起点；回采/降噪通道晚到超过约 `100ms` 时只确认，不参与开始点加权。
- 结束点：只有主边界通道终点与初始合并结果差异超过阈值时才修正；否则保持初始合并结果，避免边界抖动。
- 过度修正保护：单个主通道候选如果比初始结果更晚，不要把结果后移；孤立的几十毫秒 1kHz 小片段不能作为边界。

可用后处理脚本处理已有批量检测汇总：

```powershell
python C:\Users\Administrator\.codex\skills\beep-locator\scripts\postprocess_8ch_edges.py `
  --root E:\path\audio_dir `
  --summary-json E:\path\audio_dir\beep_results\beep_summary.json `
  --previous-csv E:\path\audio_dir\beep_results\bee_positions_reliable_edges.csv `
  --target-top-escape "target_dir_name" `
  --out-csv E:\path\audio_dir\beep_results\bee_positions_profile.csv `
  --simple-csv E:\path\audio_dir\beep_results\bee_positions_profile_simple.csv `
  --profile auto
```

若设备通道角色已知，可改用：

```powershell
--profile known --mic-channels 0,1 --reference-channels 5,6 --ignore-channels 2,3,4,7
```

## 4/6 通道边界处理补充

4 通道和 6 通道同样按“先判通道特征，再用可靠 mic 共识定边界”的逻辑处理，不能把后级回采/降噪通道和主 mic 直接平均。

- 4 通道常见 profile：`ch0,ch1` 优先作为 mic 主边界通道；`ch2,ch3` 如果有数据，多数情况下只作为回采/降噪参考；若为空通道则剔除。
- 6 通道常见 profile：`ch0,ch1,ch2,ch3` 优先作为 mic 候选池；`ch4,ch5` 可作为回采/降噪参考。前四路也要逐通道筛选，削顶严重、常量/DC、没有 1kHz 双蜂鸣结构的通道不能参与边界。
- 6 通道的最佳点优先来自多条 mic 的同位置共识；如果只有部分 mic 检出，则使用可靠 mic 候选，并用参考通道确认事件是否存在，不允许单个参考通道把起止点拉偏。
- 对同一物理蜂鸣，若多个 mic 候选相差很小，取最早稳定起点和最晚稳定终点；孤立的小片段或明显晚到的回采/降噪片段只作辅助证据。
- 在强音乐/人声背景下，如果只得到低 `tone_ratio` 的 `burst_pair` 弱候选，且没有 `continuous_pair` 或清晰多 mic 结构证据，不要把候选时间写成正式蜂鸣位置；正式结果应写“未检测到可靠蜂鸣特征”，弱候选可单独保留为人工复核表。

处理已有批量检测汇总时，仍可使用 `postprocess_8ch_edges.py`，该脚本已参数化支持 4/6/8 通道：

```powershell
# 4 通道：前两路 mic 优先，后两路参考
--profile known --mic-channels 0,1 --reference-channels 2,3

# 6 通道：前四路 mic 优先，后两路参考
--profile known --mic-channels 0-3 --reference-channels 4,5

# 强音乐背景/弱候选保守模式：拒绝只靠 burst_pair 输出正式位置
--reject-burst-primary --skip-unreliable-primary
```

## 判断规则

实际蜂鸣必须同时满足：

- 候选频率集中在目标频率附近，默认约 `1000Hz`。
- 两段蜂鸣的开始时间差约 `2秒`，中间间隔约 `1秒`。
- 每段蜂鸣通常接近 `1秒`；弱通道可允许较短 burst，但必须通过原始 PCM 频率占比复核。
- 原始 PCM 中 `1kHz` tone ratio 不能过低；低于阈值时不要写成实际蜂鸣。
- 如果窗口内全零、RMS 为 0、峰值为 0，只能说明没有实际蜂鸣，不能报实际位置。

置信度参考：

- `tone_ratio >= 0.35`：高可信。
- `0.20 <= tone_ratio < 0.35`：中等可信。
- `0.12 <= tone_ratio < 0.20`：弱可信，需要结合结构和上下文谨慎表述。
- `tone_ratio < 0.12`：不要作为实际蜂鸣输出。

## 可靠输出策略

批量处理时，正式结果只输出“证据完整”的蜂鸣位置；证据不足的候选不要写入位置表，避免后续 skill 误用错误时间点。

证据完整至少满足：

- 格式解析已确认：优先来自文件头；无头信息时，路径/用户参数推断的帧大小必须与文件大小对齐。
- 主通道共识足够：4 通道默认前 2 路 mic，6 通道默认前 4 路 mic，8 通道按设备 profile 或可靠 mic 池；默认至少达到主 mic 通道数的 `50%` 支持即可输出，单个回采/降噪/弱通道不能单独输出正式位置。
- 频率与结构稳定：目标频率 tone ratio 达到可靠阈值，且能形成清晰双蜂鸣结构。
- 首尾边界完整：若当前任务要求长音频首尾蜂鸣，只有头部和尾部都找到可靠证据时，才把该音频写入正式位置表。
- 通道边界一致：多条可靠 mic 的起止点应在允许误差内；明显延迟的回采/降噪通道只作确认，不参与拉动边界。

证据不足的情况必须从正式位置表剔除，并写入未找到/待复核清单：

- 未检测到蜂鸣结构。
- 只找到头部或只找到尾部。
- tone ratio 偏低、只出现 `burst_pair`、或被强音乐/人声背景干扰。
- 主 mic 支持通道数不足，即未达到主 mic 通道数的 `50%`；或只有回采/降噪/单个通道给出候选。
- 文件头/位深/通道数/帧对齐异常，不能确认正确解析。

正式汇总建议分两类文件：

- `*_positions*.csv`：只包含证据完整、可直接使用的音频和位置。
- `*_not_found*.csv`：只列未找到或证据不足的音频及原因；不要在这里填入弱候选位置。
- `*_review*.csv`：人工校验表，可显示稳定候选、偏弱候选、mic 缺陷、仅头/仅尾、未找到等备注；该表用于人工判断，不作为后续自动处理的正式位置输入。

## 输出要求

结果文件只写时间点和必要说明，格式保持简洁：

```text
xxx.pcm 实际蜂鸣特征位置（16000Hz / 16bit PCM/WAV / 目标频率 1000Hz）

1. 0分21秒250毫秒 → 0分24秒300毫秒（持续 0分03秒050毫秒）
2. 28分09秒050毫秒 → 28分12秒050毫秒（持续 0分03秒000毫秒）

说明：以上只表示该音频自身实际检测到的蜂鸣特征；不要把同组通道同步推断窗口写成实际蜂鸣。
```

如果未检测到实际蜂鸣，必须明确写：

```text
未检测到实际蜂鸣特征
```

## 同步推断边界

当多通道或同组录音中某个文件自身无蜂鸣波形，但其他同步通道存在蜂鸣时，可以另外给“同步推断对应窗口”。这类结果必须单独标注为同步推断，并写明该文件窗口内的证据，例如 `100%` 全零、RMS 为 0、峰值为 0。不要把同步推断窗口写入实际蜂鸣结果文件，除非用户明确要求输出推断窗口，并且文件名或说明中必须包含“同步推断/非实际蜂鸣”。

## 需要读取参考资料的情况

- 需要解释算法细节、阈值或调参时，读取 `references/beep-detection.md`。
- 需要确认目录和输出归档规范时，先看本文件的“目录约定”。
