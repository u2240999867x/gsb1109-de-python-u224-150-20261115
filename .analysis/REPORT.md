# 两份供应商备份比对报告

数据：`test/files/` 中 55 个 RAR 物理文件（含 RAR1.5/2/3/5、分卷、SFX、solid、加密、硬链接/符号链接/版本/重复项）。
脚本：`.analysis/` 下 `catalog.py`（结构化目录）→ `verify_rarfile.py`（实读+校验）→ `compare2.py`（多重项配对）→ `final.py`（三桶汇总）。

## 方法（为何不能只看大小和 CRC）

- 配对依据**条目名多重集**：同名重复条目逐项保留、按位配对，不用大小/CRC 当身份。
- 内容身份只接受**真实读取并通过校验的强哈希**：用 `rarfile.RarFile.read()` 解压，其内部对每个成员校验 CRC32（BLAKE2sp 存在时一并校验），失败即抛 `BadRarFile`；对返回字节再算 SHA-256。53/55 档案完成实读。
- 已实证的两个反例：
  - `bsdtar`(libarchive 3.7.4) 解 `rar5-psw.rar` 输出的字节 CRC 与头部不符（错误解密且静默返回），证明未经校验的读取器不能采信。
  - `rar15-*` 与 `rar202-*` 中 `FILE1.TXT` 同为 7 字节但 CRC 与 SHA-256 都不同——同名同大小不等于同内容。
- 头部里的 CRC/大小仅作线索；未实读者一律不晋升为“相同”。

## 桶一：内容相同（已实读核验，SHA-256 一致）——150 个条目、80 个实质配对

（另：零条目族的 10 个“空配对”由 2 个头部加密档案与独立分卷片段组成，无可比条目，不计入。）

| 备份族（条目多重集） | 档案 | 核验结论 |
|---|---|---|
| stest1/stest2（10 个档案，45 对） | rar3-solid, rar3-seektest.sfx, seektest, rar5-crc(.sfx), rar5-blake, rar5-solid, rar5-quick-open, rar5-psw, rar5-psw-blake | 两文件 SHA-256 全一致；覆盖 RAR3↔RAR5、solid、CRC↔BLAKE、口令加密、SFX 壳等差异 |
| sub/ 子目录（10 条目） | rar3-subdirs ↔ rar5-subdirs | 全部一致（含非 ASCII 名、空目录） |
| 只读属性 | rar3/rar5 × unix/win（4 个，6 对） | 一致 |
| Unicode 名 | unicode ↔ unicode2 | 一致（条目顺序相反） |
| owner | rar3-owner ↔ rar5-owner | 一致 |
| 符号链接 | rar3-symlink-unix ↔ rar5-symlink-unix | 链接目标一致（data_link→data.txt 等） |
| 版本 | rar3-versions ↔ rar5-versions | 一致 |
| 注释/口令 | rar3-comment-plain ↔ rar3-comment-psw | 一致（口令仅为加密包装；此处为空文件） |
| ctime 精度（6 个，15 对） | ctime0..4, rar5-times2 | 内容一致（均为空 afile.txt） |
| 分卷 | rar3-old/rar3-vols ↔ rar5-vols 的 part1、part3 | 跨卷、跨格式内容一致 |

多重项处理：`rar5-dups`（FILE_COPY×8）、`rar5-hlink`（HARD_LINK×2）、`rar5-solid-qo`（同名跨目录）均解析到同一实体内容，别名项不重复计为独立证据。

## 桶二：目录/属性变化（内容相同，仅元数据或容器表示不同）

- 压缩方式（37 对）：store↔RAR 方法、RAR3↔RAR5 算法、字典不同；`dec`（解压大小）不变。
- 时间戳（mtime 43 / ctime 39 处）：72 处为恒定 ±10800 秒（3 小时）时区表示差（RAR3 本地墙钟 vs RAR5 UTC，同一时刻）；另 3 处为 ctime 亚秒截断（±1 秒，空文件精度测试）。非真实时间改动。
- 权限/宿主（mode 14、host_os 5 处）：Unix 权限位（如 0o100444）与 DOS 属性位（0x41 只读）的同义表示；host_os UNIX↔Windows。
- 链接表示（1 处）：RAR3 用 S_IFLNK 普通文件存目标字节，RAR5 用 redir 记录；目标串相同。
- 条目顺序：unicode 族两档案顺序相反（多重集仍相同）。
- 容器级：solid 分块、SFX 自解压壳、分卷物理切分、口令/头部加密包装、quick-open、注释、锁标志——均不改变已核验内容。

## 桶三：证据不足（未能实读，不得判同）

- `rar5-hpsw.rar`、`rar3-comment-hpsw.rar`：头部加密，本机无 AES 后端（`NoCrypto`），条目字节无法读取。
  - 线索（来自仓库 `.exp` 转储，非实读）：rar5-hpsw 含 stest1/stest2，头部 CRC `c5b7e6a2`、大小 2048，与 stest 族吻合；rar3-comment-hpsw 为 file1/file2（dec=0）。**仅头部吻合，证据不足，不能判为相同。**
- 独立分卷片段（part2、r00 等）单独打开无自身条目，须从 part1 读取；已并入对应卷集核验，不单独下结论。

## 附：已实读确认“内容不同”（同名同大小陷阱，8 个条目 / 4 对）

旧格式族 `rar15-comment{,-lock}` 与 `rar202-comment-{nopsw,psw}`：
- 组内各自相同（lock 仅头标志；psw 仅加密包装）。
- 跨组 4 对、8 条目实测不同，例如 `FILE1.TXT`（同为 7 B）：
  - rar15：`d7bca5c3…45b8`（头部 CRC `e27f07a9`）
  - rar202：`2b69a7f7…14a`（头部 CRC `7a197dba`）
- 这些不属于“相同”也不属于“证据不足”，是经读取+哈希确认的真实差异。
