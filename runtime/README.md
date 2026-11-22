# llama.cpp 运行时

本目录用于放置本地 Qwen 使用的 llama.cpp Windows 构建。二进制体积较大，**不纳入 Git**。

## 推荐布局

```text
runtime/
└── llama-b9200/
    ├── llama-server.exe
    ├── ggml.dll
    ├── llama.dll
    └── ...（同目录其余 DLL）
```

应用默认使用：

```text
runtime\llama-b9200\llama-server.exe
```

## 下载

1. 打开 llama.cpp Releases：<https://github.com/ggml-org/llama.cpp/releases>
2. 下载 Windows CPU x64 构建，例如 `llama-b9200-bin-win-cpu-x64.zip`；
3. 解压后把 `llama-server.exe` 及同目录 DLL 放入 `runtime\llama-b9200\`。

如果使用其他版本或目录，可在应用的本地模型设置中修改服务路径，或设置配置项 `local_model_server_exe`。

## 许可证

llama.cpp 使用 MIT License；随包 DLL 遵循各自上游许可证。本仓库不分发这些二进制文件。
