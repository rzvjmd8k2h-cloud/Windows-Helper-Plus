# 本地 Qwen 模型

本目录用于放置本地上下文模型。模型文件体积较大，**不纳入 Git**，需要自行下载。

## 必需文件

```text
models/
└── Qwen3-1.7B-Q8_0.gguf
```

- 大小：1,834,426,016 bytes（约 1.83 GB）
- SHA-256：`061B54DAADE076B5D3362DAC252678D17DA8C68F07560BE70818CACE6590CB1A`
- 量化：Q8_0
- 上游：Qwen3-1.7B GGUF
- 许可证：Apache-2.0（以 Qwen 上游仓库为准）

## 下载

从 Qwen 官方仓库下载 Q8_0 量化文件，并重命名为 `Qwen3-1.7B-Q8_0.gguf`：

- Hugging Face：<https://huggingface.co/Qwen/Qwen3-1.7B-GGUF>
- ModelScope：<https://modelscope.cn/models/Qwen/Qwen3-1.7B-GGUF>

下载后可校验：

```powershell
Get-FileHash .\models\Qwen3-1.7B-Q8_0.gguf -Algorithm SHA256
```

## 运行时

模型由 `runtime/` 下的 llama.cpp `llama-server.exe` 加载，见 [runtime/README.md](../runtime/README.md)。

## 不放置模型时

- 主诊断（DeepSeek Planner）仍可正常使用，只需在设置中填写 API Key；
- 本地上下文整理、完成度分析、病历翻译与归档不可用；
- 应用会在本地模型状态中提示不可用，不会伪造结果。

## 资源需求

- CPU 推理，无独立显卡也可运行；
- 首次加载约 15–20 秒，内存占用约 2 GB；
- 上下文整理约 40–70 秒，日志/病历翻译约 1–3 分钟（取决于 CPU）。
