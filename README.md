# beep-locator

Locate fixed-frequency beep markers in PCM/WAV audio and write time-point result files beside the audio. Use when asked to find蜂鸣特征, beep marker positions, fixed 1kHz two-beep sync tones, or to analyze 16000Hz mono 16-bit PCM/WAV recordings for actual beep locations while distinguishing detected audio from sync-inferred windows.

## Skill layout

- `agents/openai.yaml`
- `references/beep-detection.md`
- `scripts/locate_beep.py`
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

## 目录约定

- `scripts/`：放可直接执行的检测脚本。当前主脚本是 `scripts/locate_beep.py`。
- `references/`：放算法说明、阈值含义、输出规范等参考资料。需要解释或调整检测逻辑时读取。
- `assets/`：仅在以后需要固定模板、示例音频或可视化资源时使用；不要把用户录音或运行结果放进来。
- 生成结果：默认不要写入 skill 目录。结果文件必须写到待测音频所在目录，例如 `{音频名}_beep_result.txt`。若用户要求集中输出，在音频所在目录下新建 `beep_results/`，不要污染 skill 本体。

## 标准流程

1. 确认音频格式。默认按 `16000Hz / 单通道 / 16bit signed little-endian PCM` 解析 `.pcm`；`.wav` 从文件头读取采样率和通道数。
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

常用参数：

- `--frequency 1000`：目标蜂鸣频率，默认 `1000Hz`。
- `--sample-rate 16000`：PCM 采样率；WAV 会读取文件头。
- `--channels 1 --channel-index 0`：通道数和分析通道。
- `--edge-only`：只保留录音开头和结尾附近的蜂鸣，适合本批同步蜂鸣场景。
- `--no-write-result`：只打印结果，不写文件。
- `--out-name "{stem}_beep_result.txt"`：自定义同目录结果文件名。

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
