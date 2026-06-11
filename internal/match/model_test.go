package match

import (
	"go/ast"
	"go/parser"
	"go/token"
	"testing"
)

func TestDeadAbortConstantsRemoved(t *testing.T) {
	dead := map[string]bool{
		"AbortReplaced":      true,
		"AbortInternalError": true,
	}

	file, err := parser.ParseFile(token.NewFileSet(), "model.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, decl := range file.Decls {
		gen, ok := decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.CONST {
			continue
		}
		for _, spec := range gen.Specs {
			value, ok := spec.(*ast.ValueSpec)
			if !ok {
				continue
			}
			for _, name := range value.Names {
				if dead[name.Name] {
					t.Fatalf("dead abort constant still declared: %s", name.Name)
				}
			}
		}
	}
}
