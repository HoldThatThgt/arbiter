package playbook

import "strings"

// AppendGotcha 在棋谱全文 content 的 stepID 步骤追加一条 gotcha 注记行,返回新全文。
// 纯文本手术:该步骤已有 [Gotcha] 节则插在其最后一项之后,否则在步骤末尾新建一节;
// 其余行原样保留(仅统一 CRLF 与补尾随换行)。content 须为解析无 issue 的棋谱
// (调用方保证,写盘前还应整体重解析复核);步骤不存在返回 false。
func AppendGotcha(content []byte, stepID, note string) ([]byte, bool) {
	lines := splitLines(content)
	start, end := stepSpan(lines, stepID)
	if start < 0 {
		return nil, false
	}

	insertAfter := -1 // 既有 [Gotcha] 节的节头或最后一项所在行
	section := sectionNone
	for i := start + 1; i < end; i++ {
		token, _, has := firstToken(lines[i])
		if !has {
			continue
		}
		switch token {
		case tokenStepJob, tokenCheckList, tokenBranch:
			section = sectionNone
		case tokenGotcha:
			section = sectionGotcha
			insertAfter = i
		case tokenListItem:
			if section == sectionGotcha {
				insertAfter = i
			}
		}
	}

	insert := []string{tokenListItem + " " + note}
	if insertAfter < 0 {
		insertAfter = start
		for i := end - 1; i > start; i-- { // 新节挂在步骤最后一行内容之后,保留其后的空行分隔
			if strings.TrimSpace(lines[i]) != "" {
				insertAfter = i
				break
			}
		}
		insert = []string{"", tokenGotcha, insert[0]}
	}

	out := make([]string, 0, len(lines)+len(insert))
	out = append(out, lines[:insertAfter+1]...)
	out = append(out, insert...)
	out = append(out, lines[insertAfter+1:]...)
	return []byte(strings.Join(out, "\n") + "\n"), true
}

// stepSpan 返回 stepID 步骤的行区间 [start, end):start 为其 [STEP] 行,
// end 为下一个 [STEP] 行或文件末尾;未找到返回 (-1, -1)。
func stepSpan(lines []string, stepID string) (int, int) {
	start := -1
	for i, line := range lines {
		token, rest, has := firstToken(line)
		if !has || token != tokenStep {
			continue
		}
		if start >= 0 {
			return start, i
		}
		if strings.TrimSpace(rest) == stepID {
			start = i
		}
	}
	if start < 0 {
		return -1, -1
	}
	return start, len(lines)
}
