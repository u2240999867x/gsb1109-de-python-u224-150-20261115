# 采集包展开正文（可索引）

## 元信息与规则

- 授权根（canonical）：`/Users/guo/Documents/GSB/1109/batches/de-python-u224-150-20261115/control/questions/GSB1109-20261115-U224-DE-PY-086/workspace`
- 主文文件：`test/test_2_latex2text.py`
- 引用指令：`\input{...}` / `\include{...}`（含无花括号的原始形式 `\input file.tex`）
- 相对路径按引用者所在目录解释；精确名不存在时依次补 `.tex`、`.latex`
- 解析路径经 `realpath` 规范化后必须位于授权根内，越界不读取
- 同一展开链上重复进入同一文件即判定循环引用并中止；不同链上的共用子文件允许重复展开
- 行号约定：每个片段标注源文件行区间，片段内第 k 行对应源文件第（起始行 + k - 1）行

## 片段索引

| 片段 | 角色 | 源文件 | 行区间 | 父片段 | 引用指令 |
| --- | --- | --- | --- | --- | --- |
| f1 | main | `test/test_2_latex2text.py` | 458–462 | — | — |
| f2 | included | `test/test_input_1.tex` | 1–7 | f1 | `\input{test_input_1.tex}` |
| f3 | main | `test/test_2_latex2text.py` | 485–489 | — | — |
| f4 | included | `test/test_input_1.tex` | 1–7 | f3 | `\input{test_input_1}` |
| f5 | main | `test/test_2_latex2text.py` | 496–500 | — | — |
| f6 | main | `test/test_2_latex2text.py` | 2114–2114 | — | — |
| f7 | main | `test/test_2_latex2text.py` | 2121–2121 | — | — |

## 展开正文

### 片段 f1 · `test/test_2_latex2text.py`（第 458–462 行）

- 角色：main
- 展开链：`test/test_2_latex2text.py`

源行 458–460：

```latex
ABCDEF fdksanfkld safnkd anfklsa
```

#### 片段 f2 · `test/test_input_1.tex`（第 1–7 行）

- 角色：included
- 展开链：`test/test_2_latex2text.py` > `test/test_input_1.tex`
- 经由指令：`\input{test_input_1.tex}`（目标 `test_input_1.tex`）

源行 1–7：

```latex
\textit{hi there!} This is {\em an equation}:
\begin{equation}
    x + y i = 0
\end{equation}

where $i$ is the imaginary unit.
```


源行 460–462：

```latex


MORENKFDNSN
```


### 片段 f3 · `test/test_2_latex2text.py`（第 485–489 行）

- 角色：main
- 展开链：`test/test_2_latex2text.py`

源行 485–487：

```latex
ABCDEF fdksanfkld safnkd anfklsa
```

#### 片段 f4 · `test/test_input_1.tex`（第 1–7 行）

- 角色：included
- 展开链：`test/test_2_latex2text.py` > `test/test_input_1.tex`
- 经由指令：`\input{test_input_1}`（目标 `test_input_1`）

源行 1–7：

```latex
\textit{hi there!} This is {\em an equation}:
\begin{equation}
    x + y i = 0
\end{equation}

where $i$ is the imaginary unit.
```


源行 487–489：

```latex


MORENKFDNSN
```


### 片段 f5 · `test/test_2_latex2text.py`（第 496–500 行）

- 角色：main
- 展开链：`test/test_2_latex2text.py`

源行 496–498：

```latex
ABCDEF fdksanfkld safnkd anfklsa
```

> **未展开占位（missing-file）**：`\input{../test_input_1}` → 解析为 `test_input_1`，状态 `missing-file`。

源行 498–500：

```latex


MORENKFDNSN
```


### 片段 f6 · `test/test_2_latex2text.py`（第 2114–2114 行）

- 角色：main
- 展开链：`test/test_2_latex2text.py`

> **未展开占位（missing-file）**：`\input{../dir-evil/secret}` → 解析为 `dir-evil/secret`，状态 `missing-file`。


### 片段 f7 · `test/test_2_latex2text.py`（第 2121–2121 行）

- 角色：main
- 展开链：`test/test_2_latex2text.py`

> **未展开占位（missing-file）**：`\input{ok}` → 解析为 `test/ok`，状态 `missing-file`。


## 引用边（解析结果）

| 引用者 | 行 | 指令 | 原始目标 | 状态 | 解析结果 |
| --- | --- | --- | --- | --- | --- |
| `test/test_2_latex2text.py` | 460 | `\input{test_input_1.tex}` | `test_input_1.tex` | ok | `test/test_input_1.tex` |
| `test/test_2_latex2text.py` | 487 | `\input{test_input_1}` | `test_input_1` | ok | `test/test_input_1.tex` |
| `test/test_2_latex2text.py` | 498 | `\input{../test_input_1}` | `../test_input_1` | missing | `test_input_1` |
| `test/test_2_latex2text.py` | 2114 | `\input{../dir-evil/secret}` | `../dir-evil/secret` | missing | `dir-evil/secret` |
| `test/test_2_latex2text.py` | 2121 | `\input{ok}` | `ok` | missing | `test/ok` |

## 告警（越界 / 缺失 / 循环）

- **missing-file**：reference '../test_input_1' does not resolve to a readable file（`test/test_2_latex2text.py:498`）
- **missing-file**：reference '../dir-evil/secret' does not resolve to a readable file（`test/test_2_latex2text.py:2114`）
- **missing-file**：reference 'ok' does not resolve to a readable file（`test/test_2_latex2text.py:2121`）
