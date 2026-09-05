# RTX 5090 与 PaddleOCR GPU 流程兼容性核查

核查日期：2026-09-04

范围：Lexoid 当前使用的 `paddlepaddle 3.2.2`、`paddleocr 3.7.0`，以及
`PaddleOCR`、`PPStructureV3`、`TableRecognitionPipelineV2`、
`PaddleOCR-VL`。只采用 NVIDIA、PaddlePaddle 和 PaddleOCR 官方资料或官方
发布物。

## 结论

**可以租 RTX 5090，但应租 1 张，不要先租多张。** 条件是远端为 Linux，
使用 NVIDIA 驱动 **575.51.03 或更高**，并把当前 CPU 镜像中的
`paddlepaddle==3.2.2` 换成 Paddle 官方 `cu129` 渠道的
`paddlepaddle-gpu==3.2.2`。

决定性证据如下：

1. NVIDIA 官方把 GeForce RTX 5090 列为 Compute Capability **12.0**；官方规格
   为 **32 GB GDDR7**。[NVIDIA CUDA GPU 表][nvidia-cuda-gpus]、
   [RTX 5090 规格][nvidia-5090]
2. Paddle 官方 `cu129` 索引提供
   `paddlepaddle_gpu-3.2.2-cp310-cp310-linux_x86_64.whl`。
   [官方 wheel 索引][paddle-cu129-index]
3. 对该官方 wheel 做 ZIP 中央目录和 `libpaddle.so` 的只读检查，二进制中明确
   存在 `-arch sm_120`；可见目标架构字符串为 `sm_90`、`sm_100`、`sm_120`。
   因而这不是“CUDA 12.9 理论兼容”，而是该发布物确实包含 RTX 5090 的原生
   `sm_120` 构建。[3.2.2 cu129 Linux wheel][paddle-322-cu129-wheel]
4. CUDA 12.9 GA 官方发布说明列出的 Linux 驱动版本为 `>=575.51.03`
   （Windows 为 `>=576.02`）。cuDNN 9.9 官方矩阵明确支持 CUDA 12.9、
   CC 12.0 和 Blackwell。[CUDA 12.9 发布说明][cuda-129-notes]、
   [cuDNN 9.9 支持矩阵][cudnn-99-matrix]

## 一个容易误判的差异

PaddlePaddle v3.2.2 源码的 `cmake/cuda.cmake` 默认 known-architecture 列表只到
`sm_90`，没有 `sm_120`；截至核查日，补充通用 Blackwell CMake preset 的
PR #79331 仍为 open，且 PR 自己声明只是 build-configuration readiness，
不是完整运行时支持声明。[v3.2.2 `cuda.cmake`][paddle-322-cuda-cmake]、
[PR #79331][paddle-pr-79331]

这与上面的 wheel 检查并不矛盾：

- 源码文件描述默认的 `All`/preset 选择；
- 发布流水线可以使用 `CUDA_ARCH_NAME=Manual`、`CUDA_ARCH_BIN=120` 构建专用
  wheel；
- 官方 `cu129` 的 3.2.2 发布 wheel 正是这种结果，二进制已包含 `sm_120`。

因此准确结论是：**不要拿任意 Paddle 3.2.2 GPU wheel 或自行按默认参数编译；
应明确使用官方 `cu129` Linux wheel。** 旧的 cu118/cu126 包以及当前项目的 CPU
包不满足此条件。

官方仓库中早期 RTX 50 系列问题记录也说明“CUDA 版本正确”不等于 wheel 含有
目标 SM：旧 wheel 曾只编译 75/80/86/89，导致 5090 的 120 架构报
`Unsupported GPU architecture`。[Issue #73687][paddle-issue-73687]
这条记录适合作为安装后的验收理由，但不能用来否定后来实际包含 `sm_120` 的
3.2.2 `cu129` wheel。

## 当前镜像能否直接用

不能直接用 GPU。项目当前 `Dockerfile`：

- 先从 PyTorch CPU 索引安装 `torch`；
- 安装的是 `paddlepaddle==3.2.2` CPU 包；
- 导出 requirements 后主动删除 `nvidia-*`、CUDA、Torch 和 Triton 依赖；
- 基础镜像是 `python:3.10-slim`，未配置 NVIDIA GPU runtime。

GPU 镜像至少要执行等价安装：

```bash
python3 -m pip uninstall -y paddlepaddle
python3 -m pip install paddlepaddle-gpu==3.2.2 \
  -i https://www.paddlepaddle.org.cn/packages/stable/cu129/
```

宿主机安装驱动和 NVIDIA Container Toolkit，并以 `--gpus all`（或 Compose 的
GPU device reservation）启动。NVIDIA 官方容器工具文档要求先安装 GPU 驱动，
再安装和配置 NVIDIA Container Toolkit/Container Runtime。
[NVIDIA Container Toolkit 安装指南][nvidia-container-toolkit]

建议租机后、开始批量处理前先运行：

```bash
nvidia-smi
python -c "import paddle; print(paddle.__version__); print(paddle.device.cuda.get_device_name()); paddle.utils.run_check()"
python -c "from paddleocr import PaddleOCR; PaddleOCR(device='gpu')"
```

再用 1 页真实 U2 PDF 跑 OCR、表格和一次复杂表格 fallback；只有这组 smoke test
通过，才开始 375 页批处理。此测试也能发现云厂商镜像驱动、Python ABI 或模型
下载问题。

## 显存判断

PaddleOCR 官方流水线文档给出了模型存储大小和 GPU 推理时间，但没有给这些整条
流水线统一、可保证的“最低显存”数字。因此下面把官方事实和工程预算分开。

### 官方事实

- 通用 OCR 是方向、去畸变、文字行方向、检测、识别等小模型的组合；官方页面
  列出的典型模型从不足 1 MB 到几十 MB，PP-OCRv5 server detector 为 84.3 MB。
  [通用 OCR 流水线][paddleocr-ocr]
- `PPStructureV3` 是多个布局、OCR、表格/公式/图表模块的组合；可选
  `PP-Chart2Table` 为 0.58B 参数、模型存储 1.4 GB。
  [PPStructureV3 文档][paddleocr-ppstructure]
- `TableRecognitionPipelineV2` 可直接指定 `device="gpu"`，由检测、OCR、表格分类、
  单元格和结构识别模块组合。[表格识别 V2 文档][paddleocr-table-v2]
- PaddleOCR-VL 的核心 VLM 是 **0.9B**；官方支持矩阵列出 NVIDIA GPU +
  PaddlePaddle，可用专用 VLM 服务提升速度、内存和稳定性。
  [PaddleOCR-VL 文档][paddleocr-vl]

### 工程预算（不是上游保证值）

- 通用 PaddleOCR：通常按 **2--4 GB** 可用显存预算。
- PPStructureV3 / TableRecognitionPipelineV2：按 **6--12 GB** 预算，取决于启用
  模块、输入分辨率和并发批量。
- PaddleOCR-VL 0.9B fallback：FP16 权重理论下限约 1.8 GB，但 KV cache、视觉
  特征、框架 workspace 和并发会明显增加占用；按 **8--16 GB** 预算更稳妥。
- 同进程同时常驻多个 pipeline、240 DPI 页面批量为 4、偶发 480 DPI crop 时，
  32 GB 仍有充分余量，但应实测峰值并避免无界并发。

由此，RTX 5090 的 32 GB 不是刚好够，而是对本任务有较大余量。

## 为什么只租 1 张

375 页任务中，GPU 只加速本地 Paddle 检测、OCR、表格和 PaddleOCR-VL fallback。
最终 TeX 生成/优化调用的是远端大模型 API，GPU 数量不会缩短 API 排队、网络和
生成时间。

而且 PaddleOCR 的单 pipeline 指定一个 `gpu:0`；增加第二张卡不会自动把一个
pipeline 加速。要利用 2 张卡，代码必须启动两套进程/模型，显式把页面分片到
`gpu:0` 和 `gpu:1`。这会重复模型显存，并且在远端视觉 API 成为瓶颈后收益很小。

建议配置：

- **GPU：1 x RTX 5090 32 GB**
- Paddle batch：先从 4 开始
- 本地 GPU worker：先 1 个 pipeline 进程；确认显存和吞吐后再提高页级并发
- 远端 vision concurrency：按 API 限流独立设置，不与 GPU 数量绑定

只有在单卡基准显示 GPU 连续 90% 以上利用率、Paddle 阶段显著长于远端 API，且
代码已实现显式双卡分片时，第二张卡才值得租。对当前一次性 375 页工作负载，先租
2 张通常只是增加成本。

## 租机验收清单

1. Linux x86_64，Python 3.10（与目标 wheel 一致）。
2. `nvidia-smi` 显示 RTX 5090，驱动 `>=575.51.03`。
3. NVIDIA Container Toolkit 已配置，容器内 `nvidia-smi` 正常。
4. 安装来源明确为官方 `stable/cu129`，包名是 `paddlepaddle-gpu==3.2.2`，不是
   `paddlepaddle==3.2.2`。
5. `paddle.utils.run_check()` 通过，且不出现 architecture mismatch。
6. 一页 U2 样本在通用 OCR、表格和 PaddleOCR-VL fallback 三条路径输出正常。

## 不确定性

- 官方没有为这四条完整 pipeline 发布统一的最低/峰值显存表，上述显存范围是依据
  官方模型规模和该项目并发配置给出的工程预算，必须以租机实测峰值校准。
- `sm_120` 存在于官方 3.2.2 cu129 wheel 是二进制发布物检查结论；它证明架构
  编译支持，但不能替代项目真实样本的算子级正确性测试。
- 云厂商可能给出较新或定制驱动；只要不低于上述版本且容器 smoke test 通过即可。

## Sources

所有链接于 2026-09-04 访问。

[nvidia-cuda-gpus]: https://developer.nvidia.com/cuda/gpus
[nvidia-5090]: https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/
[cuda-129-notes]: https://docs.nvidia.com/cuda/archive/12.9.0/cuda-toolkit-release-notes/index.html
[cudnn-99-matrix]: https://docs.nvidia.com/deeplearning/cudnn/backend/v9.9.0/reference/support-matrix.html
[nvidia-container-toolkit]: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
[paddle-cu129-index]: https://www.paddlepaddle.org.cn/packages/stable/cu129/paddlepaddle-gpu/
[paddle-322-cu129-wheel]: https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu/paddlepaddle_gpu-3.2.2-cp310-cp310-linux_x86_64.whl
[paddle-322-cuda-cmake]: https://github.com/PaddlePaddle/Paddle/blob/v3.2.2/cmake/cuda.cmake
[paddle-pr-79331]: https://github.com/PaddlePaddle/Paddle/pull/79331
[paddle-issue-73687]: https://github.com/PaddlePaddle/Paddle/issues/73687
[paddleocr-ocr]: https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/OCR.html
[paddleocr-ppstructure]: https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html
[paddleocr-table-v2]: https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/table_recognition_v2.html
[paddleocr-vl]: https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html
