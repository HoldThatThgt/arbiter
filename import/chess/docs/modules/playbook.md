# 模块设计:internal/playbook

棋谱的文法模型、行式分词解析器、校验与目录扫描。同时承载全项目共享的
**错误码与常量定义**(`model.go`)——它是依赖图的最底层,任何包都可安全引用。

## 职责与边界

- 把棋谱文本一次性反序列化为结构化模型(`Playbook/Step/Branch`),并做全量校验。
- 扫描棋谱目录形成 `Catalog`(含每个文件的解析结果与问题清单),支持按名查找。
- 注记文本手术 `AppendGotcha`(note.go):纯函数,输入全文输出全文,不碰文件。
- **不做**:任何运行期决策(分支/裁决在 match)、任何文件写入、任何宿主概念。

## 核心类型

```go
Playbook { Name, Description, Entry, MaxSteps, Goal *ResultSpec, Steps; order []string }
Step     { ID, Job, Checklist []string, Gotchas []string, Branch{Success, Failure} }
ResultSpec { Kind, Command | Server/Tool/Arguments, TimeoutS, OutputLines } // 谓词共享模型
Issue    { File, Line, Code, Detail }       // 结构化校验问题
Catalog  { Entries []CatalogEntry, Invalid []Issue }
CatalogEntry { File, Book, Problems []Issue }
```

`order` 记录步骤出现顺序(未导出,不参与 JSON 序列化);`Entry` = 首个 `[STEP]`;
`MaxSteps` 是回合预算(`StepBudget()` 给默认 256),`Goal` 是 checkmate 谓词。
**ResultSpec 定义在本包**(任务 result 与棋谱 goal 共用,verify 经类型别名复用)——
本包是依赖图最底层,保留字 `END` 与全部 `Code*`/`Issue*`/数值常量都集中于此。

## 解析器(parse.go)

行式分词,无正则、无子串扫描:

1. `splitLines`:统一 CRLF → LF,按行切分。
2. `parseHeader`:首行须为 `---`,找到闭合 `---`,中间交给 yaml.v3 结构化解析
   frontmatter(name/description 必填)。
3. 主循环:`firstToken(line)` 取行首空白分隔的第一个 token,与封闭标记集
   `{[STEP], [SetGoal], [StepJob], [CheckList], [Branch], [Gotcha], -}` 做**整词相等比较**:
   - `[SetGoal]`(至多一处、须在首个 `[STEP]` 前)→ 进入 goal 节:逐行 `key: value`
     切分,键集 `{shell, mcp, arguments, timeout_s, output_lines}` 整词相等;
     arguments 经 `json.Unmarshal` 结构化解析,数值经 `strconv` 转换并查界;
   - `[STEP]` → 收束上一步骤(`finish`),开启新 stepBuilder;
   - 四个节头 → 切换当前小节(节头行不得有尾随内容;[Gotcha] 是可选节,
     不参与 missing_section 校验,空节合法);
   - CheckList / Gotcha 节内:`-` 开头的行取剩余为一条项;
   - Branch 节内:按首个 `:` 切分(`strings.Cut`),键与 `{success, failure}` 整词相等;
   - StepJob 节内:任意行原样累积(含空行);
   - 空行在 StepJob 外作为分隔跳过;其余不合文法的非空行报 `stray_content`。
4. `validate`:步骤非空集、Job/Checklist 非空、Branch 双键齐全、target 为现存步骤或
   `END`、`max_steps` ∈ 0..1024、goal 谓词完整(kind 二选一、arguments 仅限 mcp)、
   文件 ≤ 1 MiB。

错误不短路:解析尽量收集**全部** issue 一次返回,作者一轮修完。

## 注记手术(note.go)

`AppendGotcha(content, stepID, note) (newContent, ok)`:在 stepID 步骤追加一条
`- note` 行——已有 `[Gotcha]` 节插在其最后一项之后,否则在步骤末尾新建一节;
其余行**原样保留**(仅统一 CRLF、补尾随换行),不做任何重排版。定位走与解析器
同一套 `firstToken` 整词比较(同一文法的两个消费者,永不分叉)。前置条件是
content 解析无 issue(调用方 match 保证,且写盘前整体重解析复核);注记以 `- `
前缀落行、入参单行,故任何文本都不可能改变棋谱结构。

## 目录扫描(ScanDir / Find / LoadableNames)

- 只取 `*.md`,按文件名排序保证目录序确定。
- 同名棋谱(frontmatter name 重复)给冲突各方都标 `name_conflict`,均不可装载。
- `Find(name)` 返回条目与错误码三态:not_found / name_conflict / playbook_invalid;
  `LoadableNames` 返回可装载名单(空时为 `[]` 而非 nil,保证 JSON 不出 null)。

## 不变式

- 解析成功(无 issue)的 Playbook 必然:Entry 非空且存在、每步三节齐全、所有 Branch
  target 可达(指向现存步骤或 END)。match 包据此可以不做防御性检查。
- 解析-序列化往返稳定(`order` 丢失仅影响展示序,不影响任何运行逻辑——按 ID 寻址)。

## 测试要点(parse_test.go / note_test.go)

合法棋谱 golden(多步骤/分支重入/CRLF);每个 Issue 码至少一例(含 duplicate_step、
oversize);[Gotcha] 解析(含空节合法、节内 stray);目录扫描的同名冲突;往返稳定性;
AppendGotcha 建节/追加序/步骤缺失/EOF 无换行/连续累积,结果一律重解析断言。
