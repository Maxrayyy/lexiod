# Lexoid LaTeX 可定位优化规格

## 1. 目标

建立一条可验证的 `lexoid → texopt → XeLaTeX → PDF` 链路，使每个可编辑字段同时具备：

1. 稳定、可重复生成的字段 ID；
2. 源码行到 PDF 的 SyncTeX 正向定位；
3. PDF 坐标到源码行的 SyncTeX 反向定位；
4. 优化前后的文本、页数和表格几何布局回归验证。

本工具不使用 OCR 重新识别原 PDF，不修改字段值，不尝试“美化”版式。

## 2. 已确认的 Lexoid 接口

### 2.1 分页标记

Lexoid 当前的分页标记是：

```latex
% LEXOID_PAGE_COMPLETED: <page>/<total>
```

例如：

```latex
% LEXOID_PAGE_COMPLETED: 76/134
```

`texopt` 必须以此标记作为物理 PDF 页的首选来源。`--start-page` 只用于没有分页标记的历史文件，不得覆盖文件中已有的合法标记。

### 2.2 字段标记

Lexoid 当前使用：

```latex
% #VALUE_ID: LEX-P0076-V0001
% #FIELD_VALUE: 字段名
\fieldvalue{值}
```

手写字段使用同一个 `VALUE_ID`：

```latex
% #VALUE_ID: LEX-P0076-V0002
% #FIELD_VALUE: 复核人
% #HANDWRITTEN: 张三
\fieldvalue{\handwritten{张三}}
```

存疑手写值使用：

```latex
% #TODO #HANDWRITTEN: 张?; handwritten name is unclear
```

约束：

- 注释标记中必须写 `#VALUE_ID` / `#FIELD_VALUE`，不得写成 `#VALUE\_ID` / `#FIELD\_VALUE`。
- `VALUE_ID` 是下游主键，`texopt` 必须原样保留，不得用 LLM 生成的名称替换。
- 如需语义名，只能生成 `semantic_alias`，不得改变主键。
- 字段可见内容行是 `\fieldvalue{...}` 所在行，注释行本身没有 PDF box。SyncTeX 验证应使用前者。

## 3. 根因与术语

### 3.1 根因

`tabularx` 会先把整个环境体读取为宏参数，并为求解 `X` 列宽而多次排版。因此：

- `\the\inputlineno` 往往只能看到环境结束附近的处理行；
- SyncTeX 对 cell 内部原始行的映射可能折叠到环境重放点；
- 含计数、写文件、锚点或标签的宏可在试排阶段产生副作用。

`\inputlineno` 索引和 SyncTeX 是两套机制，报告中必须分开记录，不得将二者的成功或失败互相替代。

### 3.2 opaque 表格

对本工具而言，“opaque 表格”指环境体被整体捕获、重放，导致 cell 源码行不能稳定映射的表格。

| 环境 | 默认分类 | 策略 |
|---|---:|---|
| `tabular`, `tabular*`, `array` | 非 opaque | 仅拆分 cell 源码行 |
| `longtable`, `supertabular` | 候选非 opaque | 必须经实测通过后才加入 allowlist |
| `tabularx`, `tabulary` | opaque | 先转换再拆 cell |
| `tabu`, `longtabu`, `xltabular` | opaque | 先转换；不能精确转换则失败 |
| 用户自定义包装环境 | 未知 | 实测或显式配置 |

`\verb` 只可作为辅助线索，不能作为 opaque 的唯一判据。新版 `tabularx` 对 `\verb` 有限度特殊支持，但仍会捕获并重排环境体。

## 4. 优先从 Lexoid 源头解决

Lexoid 生成新 LaTeX 时应默认输出 SyncTeX-safe 表格：

1. 优先使用 `tabular` + `p{...}`；
2. 跨页表格优先使用已通过实测的 `longtable` / `supertabular`；
3. 不得在新生成的模板中优先推荐 `tabularx`；
4. 必须使用 `tabularx` 时，应视为待 `texopt` 规范化的中间产物，不得直接作为最终可定位 TeX。

`texopt` 的转换功能主要用于历史文件、外部 TeX 和模型未遵守生成约束的情况。

## 5. 语法处理硬约束

### 5.1 不得用正则拆表格

必须使用 TeX-aware 词法扫描器，至少跟踪：

- `{...}` 嵌套深度；
- `\begin` / `\end` 环境嵌套；
- 注释与转义字符；
- 数学模式；
- 顶层 `&` 和顶层行结束 `\\`；
- `\multicolumn`, `\multirow`, `\makecell` 以及嵌套表格。

只能在当前表格的顶层将 `&` 视为 cell 分隔符，将 `\\` 视为 row 结束符。

### 5.2 cell 拆行

每个包含 `\fieldvalue` 或 `\handwritten` 的逻辑值必须占有独立源码行。相应的 `VALUE_ID` / `FIELD_VALUE` / `HANDWRITTEN` 注释紧邻其上，且注释行不得吞掉 `&` 或 `\\`。

格式化不得改变：

- cell 的可见内容；
- row / column 数量；
- `\cline`, `\hline`, `\multicolumn` 和嵌套表格边界；
- 原有注释的语义与归属。

## 6. opaque 表格转换

### 6.1 不变量

`tabularx → tabular` 及其他转换必须保证：

1. `X` 列转换为等价 `p{<measured width>}`；
2. 探针宽度保留 TeX 输出的原始字面量，不先转为浮点数；
3. `>{...}`, `<{...}`, `|`, `\|`, `@{...}`, `!{...}` 原样保留；
4. 只剔除已识别且已被实测宽度替代的 `\hsize` 权重赋值，其他列前/列后声明必须保留；
5. 不改变列对齐语义；
6. 不改变表格的目标总宽；
7. 不得为“看起来更好”而改变字号、行高、列间距或文本。

### 6.2 列宽来源

列宽按以下顺序求解：

#### A. 静态闭式解

仅当下列条件全部成立时使用：

- 所有可变列均为已支持的等权 `X`；
- 无 `@{...}` / `!{...}` / `\extracolsep` 等改变列间开销的构造；
- 规则线数量和 `\tabcolsep` 开销可静态确定。

全 `X` 表格的基本形式为：

```latex
\dimexpr(目标宽度 - 列间开销 - 规则线开销) / X列数\relax
```

不得在 Python 中用浮点数代替 TeX 尺寸运算。如整除产生 sp 余数，只在能证明原环境会填满目标宽时将余数补到最后一个 flex 列。

#### B. 编译探针

下列情况必须使用探针：

- `X` 与 `l/c/r/p/m/b` 混排；
- 加权 `\hsize` X 列；
- `tabulary` 的 `L/C/R/J`；
- `tabu` 权重列；
- 列修饰导致静态开销不可证明。

探针必须：

- 使用原文档引擎、类、宏包、字体和相同上下文；
- 记录每个最终列的 `\hsize` 或等价宽度；
- 隔离临时产物，不覆盖原文档辅助文件；
- 有超时限制并在报告中保留编译诊断。

#### C. 不可转换

静态解和探针都不能证明等价时，抛出 `UnconvertibleTable`。strict 模式下不得猜测列宽。

### 6.3 副作用审计

转换前扫描环境体中的：

- `\footnote`；
- `\label`；
- `\refstepcounter` / `\stepcounter` / `\addtocounter`；
- `\caption`；
- `\write` / `\immediate`；
- `\hypertarget` 及其他可配置副作用宏。

命中时不一定禁止转换，但必须写入 `report.format_risks`，并由 `verify` 证明最终输出可接受。

### 6.4 审计注释

每个被转换的表格上方增加一行不参与排版的注释：

```latex
% texopt: table=<stable-table-id> from=tabularx method=static spec_sha256=<hash>
```

完整原始 `\begin{...}` 、列规格和转换结果写入 `report.json`。不将可能跨行或含 `%` 的原始内容直接塞入单行 TeX 注释。

## 7. 字段 ID 与语义命名

### 7.1 主键

已有 `% #VALUE_ID` 时，直接使用 `LEX-P####-V####`。这是 registry、验证结果和下游回填的唯一主键。

仅历史文件缺少 `VALUE_ID` 时，`texopt` 才按物理页和视觉顺序生成兼容 ID：

```text
LEX-P0076-V0001
```

生成后必须回写到 TeX，以保证下次运行字节级稳定。

### 7.2 semantic alias

可选别名格式：

```text
p076-dimension_inspection_record-outer_diameter_measured
```

别名不参与 SyncTeX 定位，不能作为回填主键。

表名来源按优先级：

1. 紧邻表格上方、独占一行的 `\textbf{...}` / `\textsc{...}`；
2. 同一空隙内的其他粗体串；
3. `\caption{...}`；
4. 最近的 `\section` / `\subsection` / `\paragraph`；
5. 稳定位置名 `table_p076_03`。

向上扫描在前一张表的 `\end{...}` 或任意 sectioning 命令处停止。

字段语义优先使用 `% #FIELD_VALUE` 的标签。只在标签缺失或过于模糊时才可按表批量调用 LLM。LLM 输出必须经过：

- JSON schema 校验；
- slug 重新生成；
- 表内去重与稳定后缀；
- 缺失 key 的位置名回退；
- 按请求指纹缓存到 `.texopt-names.json`。

`--no-llm` 必须完全离线可用，并保证主键与定位功能不受影响。

## 8. CLI 合同

### 8.1 audit

```bash
python -m texopt.cli audit input.tex --report audit.json
```

输出：

- 表格环境类型与数量；
- opaque / 未知环境；
- 可用的列宽求解方法；
- 缺失或重复的 `VALUE_ID`；
- 分页标记完整性；
- 格式副作用风险。

`audit` 只读，不修改输入。

### 8.2 optimise

```bash
python -m texopt.cli optimise input.tex -o input.opt.tex \
  --registry fields.json \
  --report report.json \
  --diff opt.diff
```

行为：

1. 读取分页标记和现有 `VALUE_ID`；
2. 审计所有表格；
3. 按需自动运行列宽探针；
4. 转换 opaque 表格；
5. 拆分 field cell 的源码行；
6. 以临时文件写出，全部 strict 检查通过后再原子替换目标文件。

选项：

- `--no-probe`：禁用编译探针，只允许静态可证明的转换；
- `--allow-opaque`：唯一降级逃生阀，允许保留未转换表格；
- `--start-page N`：仅在没有 Lexoid 分页标记时生效；
- `--semantic-aliases`：生成可选语义别名；
- `--no-llm`：语义别名只使用标签和位置回退。

strict 默认行为：

- 任何 opaque/未知表格未转换：退出 3；
- 不产生或覆盖 `.opt.tex`；
- 仍产生 `report.json` 和诊断信息，便于修复。

`--allow-opaque` 模式下：

- 允许产生 `.opt.tex`；
- stderr 必须显示 WARNING；
- `report.opaque_remaining` 必须逐表列出原因；
- 成功退出码为 2，表示有明确降级，不得返回 0。

### 8.3 verify

```bash
xelatex -synctex=1 -interaction=nonstopmode input.opt.tex

python -m texopt.cli verify input.opt.tex input.opt.pdf \
  --registry fields.json \
  --baseline-pdf input.orig.pdf \
  --check-geometry
```

`verify` 必须通过 `synctex` CLI 查询定位，不直接依赖 `.synctex.gz` 内部文本格式。

## 9. 验证项

### 9.1 compile

- XeLaTeX 退出码为 0；
- 存在 PDF 和 `.synctex.gz`；
- 中文文档使用 XeLaTeX + `ctex` / `fontspec`；
- 未定义引用、重复 destination 等会破坏定位的警告视为失败或显式风险。

### 9.2 synctex_roundtrip

对每个 registry field：

1. 使用 `\fieldvalue{...}` 的 `value_source_line` 做正向查询；
2. 获取 PDF 页和 box；
3. 取 box 中心做反向查询；
4. 反向结果必须落在该值的 `source_span`，而不是要求落在无 box 的注释行。

默认通过率必须为 100%。任何豁免都必须是配置中的显式 field ID，且写入报告。

### 9.3 content_regression

使用 `pdftotext -layout`比较：

- 页数；
- 逐页文本；
- 可配置忽略纯空白差异，但不得忽略可见字符差异。

不使用像素级截图回归。

### 9.4 geometry_regression

只要发生过 opaque 表格转换，必须自动启用，不依赖用户遗忘传入 `--check-geometry`。

使用 `pdftotext -bbox`对齐词框，报告：

- 最大位移；
- p95 位移；
- 超容差词框数；
- 无法对齐的文本及所在页。

默认容差统一为 `0.05pt`，可用 `--geometry-tolerance` 显式放宽。规格和 CLI 帮助中不得同时出现 `0.05pt` 与 `0.5pt` 两个默认值。

## 10. report.json 最低字段

```json
{
  "schema_version": "1.0",
  "input": {},
  "page_markers": {},
  "tables": [],
  "converted_tables": [],
  "opaque_remaining": [],
  "format_risks": [],
  "fields": [],
  "naming": {
    "existing_label": 0,
    "llm": 0,
    "cache": 0,
    "heuristic": 0
  },
  "naming_fallbacks": [],
  "verification": {
    "compile": {},
    "synctex_roundtrip": {},
    "content_regression": {},
    "geometry_regression": {}
  }
}
```

每张表至少记录：

- 稳定 table ID；
- 原环境和新环境；
- 源码起止行；
- 原列规格和新列规格；
- 静态/探针/未转换的决策与原因；
- 实测列宽原始字面量；
- 格式风险；
- 几何回归结果。

## 11. 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 成功，无降级 |
| 1 | 一般输入、解析、编译或验证失败 |
| 2 | `--allow-opaque` 下成功产出，但仍有 opaque 表格 |
| 3 | strict 模式下存在不可转换表格，未产出优化 TeX |
| 4 | 字段 ID / 分页标记不一致，无法保证下游稳定性 |

## 12. 验收标准

一个文档只有在下列条件全部成立时才算优化成功：

- strict 模式下 `opaque_remaining` 为空；
- 所有 `VALUE_ID` 唯一且稳定；
- 所有 field 都有独立的 `value_source_line` / `source_span`；
- XeLaTeX 编译成功并产生 SyncTeX；
- SyncTeX 双向回路 100% 通过；
- 页数和可见文本回归通过；
- 发生表格转换时，几何回归通过；
- 报告中没有未解释的静默回退。

## 13. 实现顺序

1. 先修改 Lexoid LaTeX prompt，使新文档默认使用 `tabular` + `p{}`；
2. 实现分页标记、`VALUE_ID` 和 cell 词法解析；
3. 实现 `audit` 与非 opaque 表格拆行；
4. 实现静态闭式 `tabularx → tabular`；
5. 实现自动探针与复杂列规格；
6. 实现 SyncTeX / content / geometry 验证；
7. 最后增加可选 semantic alias 与 LLM 命名。

LLM 命名不得阻塞基础定位与格式等价转换的交付。

## 14. 参考

- `tabularx` 官方文档：环境体是宏参数，且为求解列宽会多次排版：<https://mirrors.ctan.org/macros/latex/required/tools/tabularx.pdf>
- `tabulary` 官方文档：`L/C/R/J` 列依据内容自然宽度按比例分配：<https://mirrors.ctan.org/macros/latex/contrib/tabulary/tabulary.pdf>
- SyncTeX CLI 是正向/反向查询的对外接口，实现不应自行解析内部文件格式：<https://tug.org/texlive/doc/synctex/synctex.html>
