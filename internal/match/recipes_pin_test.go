package match

import (
	"testing"

	"github.com/HoldThatThgt/arbiter/internal/playbook"
)

// A recipes-capability match (recipe-derivation) registers recipes mid-match,
// so recipes.yaml's whole-book SHA necessarily drifts from the load-time pin.
// checkRecipePin must NOT reject its run predicates on that drift (that block
// is what forced the opening into shell `test -f` gaming) — but it must still
// require the referenced recipe to exist. A non-recipes match stays strictly
// pinned: any drift is a mismatch.
func TestCheckRecipePinRelaxedForRecipesCapability(t *testing.T) {
	root := t.TempDir()
	s := New(root, "test")
	// Current book on disk: src_compile registered (post-`register` state).
	writeRecipes(t, root, "src_compile", "harness:\n  kind: gtest\n")

	runSpec := func(recipe string) playbook.ResultSpec {
		return playbook.ResultSpec{Kind: "run", Recipe: recipe}
	}
	// A deliberately-stale pin: load-time book (before register) ≠ current.
	stalePin := RecipePin{BookSHA256: "stale-load-time-sha", Targets: map[string]string{}}

	recipesMatch := &Match{
		Playbook:   playbook.Playbook{Capabilities: []string{"recipes"}},
		RecipesPin: stalePin,
	}
	// Drift tolerated for an existing recipe.
	if err := s.checkRecipePin(recipesMatch, runSpec("src_compile")); err != nil {
		t.Fatalf("recipes match, existing recipe, book drifted: err = %v, want nil", err)
	}
	// Recipe-exists is still enforced even under the recipes capability.
	if err := s.checkRecipePin(recipesMatch, runSpec("does_not_exist")); toolCode(err) != playbook.CodeRecipePinMismatch {
		t.Fatalf("recipes match, missing recipe: code = %q, want %q", toolCode(err), playbook.CodeRecipePinMismatch)
	}

	// A non-recipes match must still reject any book drift.
	plainMatch := &Match{
		Playbook:   playbook.Playbook{Capabilities: nil},
		RecipesPin: stalePin,
	}
	if err := s.checkRecipePin(plainMatch, runSpec("src_compile")); toolCode(err) != playbook.CodeRecipePinMismatch {
		t.Fatalf("non-recipes match, book drifted: code = %q, want %q", toolCode(err), playbook.CodeRecipePinMismatch)
	}
}
