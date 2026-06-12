package match

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"

	"github.com/HoldThatThgt/arbiter/internal/playbook"

	"gopkg.in/yaml.v3"
)

func (s *Store) recipesPath() string {
	return filepath.Join(s.Root, ".arbiter", "recipes.yaml")
}

func (s *Store) currentRecipesPin() (RecipePin, error) {
	data, err := os.ReadFile(s.recipesPath())
	if errors.Is(err, os.ErrNotExist) {
		return RecipePin{Targets: map[string]string{}}, nil
	}
	if err != nil {
		return RecipePin{}, err
	}
	pin := RecipePin{
		BookSHA256: sha256Hex(data),
		Targets:    map[string]string{},
	}
	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return RecipePin{}, err
	}
	// 引擎 RecipeBook v2(engine/arbiter_engine/runs/recipes.py _parse_targets):
	// targets 是一个 sequence,每项是带 `id` 标量的 mapping。这里只摘取 id 集合;
	// 逐目标内容无需哈希 —— BookSHA256 已唯一确定整本书(见 checkRecipePin)。
	targets := mappingValue(documentRoot(&doc), "targets")
	if targets == nil || targets.Kind != yaml.SequenceNode {
		return pin, nil
	}
	for _, entry := range targets.Content {
		id := mappingValue(entry, "id")
		if id == nil || id.Kind != yaml.ScalarNode || id.Value == "" {
			continue
		}
		pin.Targets[id.Value] = ""
	}
	return pin, nil
}

func (s *Store) checkRecipePin(m *Match, spec playbook.ResultSpec) error {
	current, err := s.currentRecipesPin()
	if err != nil {
		return &ToolError{Code: playbook.CodeRecipePinMismatch, Message: "recipe pin mismatch", Data: map[string]any{"error": err.Error()}}
	}
	pinned := m.RecipesPin
	if pinned.Targets == nil {
		pinned.Targets = map[string]string{}
	}
	if current.BookSHA256 != pinned.BookSHA256 {
		s.journalRecipePinMismatch(m, spec, pinned, current)
		return &ToolError{Code: playbook.CodeRecipePinMismatch, Message: "recipe pin mismatch"}
	}
	if spec.Recipe == "" {
		return nil
	}
	// 走到这里 BookSHA256 已相等 ⇒ 当前书与封盘时逐字节一致,逐目标哈希比较是死代码;
	// 唯一还需要回答的问题是该 recipe 是否存在于书中。
	if _, ok := current.Targets[spec.Recipe]; !ok {
		s.journalRecipePinMismatch(m, spec, pinned, current)
		return &ToolError{Code: playbook.CodeRecipePinMismatch, Message: "recipe pin mismatch"}
	}
	return nil
}

func (s *Store) journalRecipePinMismatch(m *Match, spec playbook.ResultSpec, pinned, current RecipePin) {
	fields := map[string]any{
		"match_id":      m.ID,
		"recipe":        spec.Recipe,
		"expected_book": pinned.BookSHA256,
		"found_book":    current.BookSHA256,
	}
	if spec.Recipe != "" {
		_, expectedOK := pinned.Targets[spec.Recipe]
		_, foundOK := current.Targets[spec.Recipe]
		fields["expected_target"] = expectedOK
		fields["found_target"] = foundOK
	}
	s.append("recipe_pin_mismatch", fields)
}

func documentRoot(node *yaml.Node) *yaml.Node {
	if node.Kind == yaml.DocumentNode && len(node.Content) > 0 {
		return node.Content[0]
	}
	return node
}

func mappingValue(node *yaml.Node, key string) *yaml.Node {
	if node == nil || node.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(node.Content); i += 2 {
		if node.Content[i].Kind == yaml.ScalarNode && node.Content[i].Value == key {
			return node.Content[i+1]
		}
	}
	return nil
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
