# formkit —— 表单抽取 → 人工校对 → 入库

建在 Lexoid **之外**的一层流水线。

## 前端在哪？

**没有独立的前端工程。** 前端就是 `formkit review` 生成的 `review.html` ——
一个自包含的单文件网页：所有裁图以 data URI 内嵌在里面，
**双击用浏览器打开就行，不用 npm、不用 React、不用起任何服务器**。

## 怎么跑（在你的 Mac 上直接跑，不用 Docker）

formkit 只依赖 5 个包，全部在 Intel Mac 上有预编译 wheel —— 和 Lexoid 本体不同
（后者被 torch / paddlepaddle 卡死），所以它可以脱离 Docker 直接跑。
这也更方便：生成的 `review.html` 本来就要用 Mac 上的浏览器打开。

```bash
cd ~/Desktop/lexiod/Lexoid

# 一次性：建一个独立的小 venv（别用 Lexoid 那个 .venv，它装不起来）
python3 -m venv .venv-formkit
.venv-formkit/bin/pip install -r requirements-formkit.txt

# 国内网络：调 Gemini / OpenAI 需要代理，端口换成你自己的
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890

# 1) 抽取（API key 自动从仓库根目录的 .env 读，不用手动 export）
.venv-formkit/bin/python -m formkit extract work/paper.pdf -o work/fields.json

# 2) 生成校对页面
.venv-formkit/bin/python -m formkit review work/fields.json work/paper.pdf -o work/review.html
open work/review.html          # 浏览器打开，改完点右上角「导出 values.json」
                               # 导出的文件在 ~/Downloads，挪到 work/ 下

# 3) 合并成入库 JSON
.venv-formkit/bin/python -m formkit merge work/fields.json work/values.json -o work/final.json
```

> 注意第 2 步的代理：`review` 和 `merge` **不联网**，只有 `extract` 需要。
>
> `pypdfium2` 若报 macOS 版本不兼容，说明你的系统低于 11 —— 
> `requirements-formkit.txt` 已经把版本钉在 `<5.12`（要求 macOS 11+），
> 5.12 及以上的 mac x86_64 wheel 要求 macOS 13+。

### 也可以在 Docker 里跑

镜像里已经有 pillow / pypdfium2 / openai，但 formkit 没被装进 venv，需要指定路径：

```bash
docker compose run --rm --entrypoint python \
  -e PYTHONPATH=/app lexoid -m formkit extract paper.pdf -o fields.json
```

不推荐 —— 生成的 HTML 还是要回到 Mac 上打开，多一层转手没有收益。

## 三个命令

```bash
python -m formkit extract  paper.pdf -o fields.json               # PDF  → 字段 + bbox
python -m formkit review   fields.json paper.pdf -o review.html   # → 人工校对页面
python -m formkit merge    fields.json values.json -o final.json  # → 入库用 JSON
```

---

## 为什么不从 LaTeX 里提取值

你原本的设想是 `PDF → LaTeX → 提取值 → 人工改 → JSON`。这条路走不通，原因是
LaTeX 那一步已经把信息销毁了。看你实际的 `paper.tex`：

```latex
样品名称 & \multicolumn{1}{l|}{\textit{（手写内容，部分难以辨认）}} &
```

「样品名称」的值，**字面上就是"（手写内容，部分难以辨认）"这个字符串**。它不是一个
待填的空位，是模型放弃之后写下的一句散文。从这里再怎么解析，拿到的也只能是这句话。

更关键的是 **LaTeX 不携带 bbox**。而「手写部分重新识别」本质上要求把某个字段对应的
原图区域裁出来重新喂给模型 —— 没有坐标就无法定位该裁哪里，只能整页重问，
而整页重问的条件和第一次失败时完全一样，不会有更好的结果。

所以 formkit 让排版和取值**分成两条独立的路**：

```
                  ┌─→ lexoid latex ────────────→ paper.tex   （给人读的排版还原）
   paper.pdf ─────┤
                  └─→ formkit extract ─────────→ fields.json （给机器用的结构化值）
                                                      │
                                     formkit review ──┤
                                                      ↓
                                              review.html  ←── 人工校对
                                                      │ 导出
                                                      ↓
                                                values.json
                                                      │
                                     formkit merge ───┤
                                                      ↓
                                                 final.json → 数据库
```

## extract 的两遍策略

| | 做什么 | 为什么 |
|---|---|---|
| **Pass A** discover | 整页图 → 发现所有 `label / value / bbox / type / confidence` | 模板不固定，字段只能靠发现，不能预定义 |
| **Pass B** reread | 对待复核字段按 bbox 裁图、放大 3 倍、单独重问 | Pass A 在整页缩略图上看一个手写小格子，有效像素只有几十个；裁出来放大单独问，识别率差一个数量级 |

Pass B 只在**新置信度更高**时才覆盖原值 —— 重读失败不应该让结果变得更差。
重读整体失败（网络、限流）时保留 Pass A 的值并继续标记为待复核，不中断流程。

## 待复核判定

字段命中任一条即进入人工队列，并在界面上显示具体理由：

- 模型自述无法辨认（`难以辨认` / `[handwritten]` / `illegible` …）
- `is_handwritten = true`
- 置信度 < 阈值（默认 0.75，`--threshold` 可调）
- 无 bbox，或 bbox 覆盖 > 55% 页面（等于没定位）
- 非复选框类型但值为空

> 占位符匹配刻意**只在括号内**匹配 `handwritten` / `signature` 这类英文词。
> 裸词匹配会把 "handwritten value" 这种正文误判成占位符 —— 开发时的 fixture
> 已经踩到过，`tests/test_formkit.py::test_bare_english_words_are_not_placeholders` 守着这条。

## field_id：入库正确性的地基

模板不固定 + 要入库，这两条组合起来最大的风险是 **同一份 PDF 重跑两次产生两套主键**。
所以 `field_id` 的生成刻意**只依赖三样东西**：

```
field_id = p{页码:02d}.{归一化label}[.{页内重名序号}]
例：p01.样品名称    p03.检验项目.2
```

- **不依赖 value** —— 人工改完值之后 id 必须不变
- **不依赖 bbox** —— 坐标每次都会抖动
- **不依赖模型返回顺序** —— 只用「页内出现顺序」解重名

`doc_id = sha256(PDF字节)[:16]`，同一份 PDF 恒定，所以「重跑覆盖入库」是幂等的。

`merge` 会**硬性拒绝** `doc_id` 不匹配的合并 —— 把 A 文档的校对结果并到 B 文档上
是最危险且事后最难发现的一类错误，宁可失败也不要警告。

## 建议的表结构

模板不固定，所以不要一模板一张表。用 EAV + JSONB：

```sql
CREATE TABLE doc (
  doc_id      TEXT PRIMARY KEY,          -- = final.json 的 doc_id
  source_pdf  TEXT NOT NULL,
  page_count  INT,
  model       TEXT,
  extracted_at TIMESTAMPTZ,
  reviewed_at  TIMESTAMPTZ,
  reviewer     TEXT,
  raw          JSONB                     -- 整份 final.json 存档，便于回溯
);

CREATE TABLE doc_field (
  doc_id     TEXT REFERENCES doc(doc_id) ON DELETE CASCADE,
  field_id   TEXT,
  page       INT,
  seq        INT,
  label      TEXT,                       -- OCR 原文
  label_norm TEXT,                       -- 归一化，用于跨文档聚合
  type       TEXT,
  value      TEXT,
  value_json JSONB,                      -- checkbox_group 的结构化值
  bbox       REAL[4],
  confidence REAL,
  is_handwritten BOOL,
  source     TEXT,                       -- vlm | vlm_retry | human
  edited     BOOL,
  confirmed  BOOL,
  note       TEXT,
  PRIMARY KEY (doc_id, field_id)
);
CREATE INDEX ON doc_field (label_norm);
CREATE INDEX ON doc_field (doc_id) WHERE NOT confirmed;   -- 查未校对完的文档
```

`final.json` 的 `records` 数组就是 `doc_field` 的行，字段名一一对应，可直接批量插入。
另外 `final.json.flat` 提供 `{页 → {字段名: 值}}` 的扁平视图，方便临时消费，
但**不建议用它入库** —— label 是 OCR 出来的，会变，不适合当列名。

## 校对页面

单文件 HTML，所有裁图以 data URI 内嵌，浏览器直接打开，**不需要起服务、不需要网络**。

- 左侧显示该字段在原图上裁出来的区域（点击放大），右侧是输入框 / 真复选框
- 橙色左边框 = 待复核，绿色 = 已确认；顶部实时显示进度
- 「只看待复核」过滤 + 按字段名/值搜索
- 值与模型原值不同时自动标「已修改」，`merge` 据此把 `source` 置为 `human`
- 草稿自动存 localStorage（`file://` 下可能被浏览器禁用，已 try/catch 兜住，
  以导出为准）
- `Cmd/Ctrl+S` 导出

## 已知限制与风险

1. **bbox 质量取决于模型。** Gemini 原生支持 `box_2d` 检测，是目前 extract 的默认
   （`gemini-2.5-flash`）。GPT 系列的坐标输出明显不如 Gemini 稳。
   如果你更想用 `gpt-5.6-luna` 做 LaTeX，那就让两条路各用各的模型 ——
   这正是把它们拆开的好处之一。
2. **bbox 不准时裁图会偏。** 已经做了三层缓解：裁图向外扩 12% 边距、
   bbox 缺失时回退整页缩略图、bbox 过大时标记「定位不可信」。但不准就是不准，
   最终靠人工在界面上发现。
3. **Pass B 会显著增加 API 调用次数**（每个待复核字段一次）。10 页表单若有 60 个
   待复核字段，就是 10 + 60 = 70 次调用。`--no-reread` 可关掉。
4. **同页 label 顺序变化会导致重名字段的 id 漂移。** 只影响页内出现多次的同名字段
   （如多个「检验项目」）。若这类字段很多，需要换成基于 bbox 纵坐标排序的稳定序号 ——
   目前没做，因为它反过来会让 id 依赖 bbox。
5. **未做表单切分。** 你那份 PDF 里有 4 种不同表单编码混在 10 页里。目前
   `form_code` 只是逐页记录在 `pages[]`，没有把 10 页拆成 4 份独立文档。
   如果入库粒度是「一份表单一条记录」而不是「一个 PDF 一条记录」，这一步需要补。

## 测试

```bash
pip install pytest pillow pypdfium2
python -m pytest tests/ -q        # 39 passed
```

覆盖：field_id 稳定性与重名、Gemini 坐标转换、bbox 夹紧与退化框拒绝、
占位符误报/漏报、merge 的 doc_id 校验与孤儿 id、部分校对回落、
HTML 转义（含 `</script>` 逃逸）与自包含性。

不含真实 API 调用 —— `extract` 的模型交互部分需要 key，未纳入自动化测试。
