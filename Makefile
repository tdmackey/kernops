# Fast static checks — run before committing pipeline changes.
#   make check        everything below
#   make shellcheck   needs shellcheck installed (brew install shellcheck)
SHELL := /bin/bash
SCRIPTS := $(wildcard scripts/*.sh) $(wildcard scripts/lib/*.sh) \
	$(wildcard modules/*/build.sh)
PYTOOLS := tools/dashboard/generate.py $(wildcard tools/kpm/kpm/*.py) \
	$(wildcard scripts/*.py) $(wildcard tests/*.py)
WORKFLOWS := $(wildcard .github/workflows/*.yml)

.PHONY: check syntax yaml workflow-check dashboard-check tests pins shellcheck

check: syntax yaml workflow-check dashboard-check tests pins
	@echo "── make check: OK"

syntax:
	@for s in $(SCRIPTS); do bash -n "$$s" || exit 1; done
	@python3 -m py_compile $(PYTOOLS)
	@echo "syntax: ok ($(words $(SCRIPTS)) scripts, $(words $(PYTOOLS)) python files)"

yaml:
	@for w in $(WORKFLOWS); do \
	  ruby -ryaml -e 'YAML.load_file(ARGV[0])' "$$w" 2>/dev/null \
	    || python3 -c "import yaml,sys; yaml.safe_load(open('$$w'))" 2>/dev/null \
	    || { echo "!! $$w failed to parse (need ruby or python3+pyyaml)"; exit 1; }; \
	done
	@echo "yaml: ok ($(words $(WORKFLOWS)) workflows)"

workflow-check:
	@python3 scripts/check-workflows.py $(WORKFLOWS)

dashboard-check:
	@python3 scripts/check-dashboard-config.py
	@python3 tools/dashboard/generate.py --offline -o /tmp/kernops-dashboard-check.html >/dev/null
	@echo "dashboard: ok"

tests:
	@python3 -m unittest discover -s tests -p 'test_*.py'
	@bash tests/test_scripts.sh
	@echo "tests: ok"

# every line's ledger must be complete and its series must apply to its base
# (needs the base tag in the unified repo: scripts/kpm --line <line> fetch --base-only)
pins:
	@for l in $$(./scripts/kpm lines | cut -f1); do \
	  ./scripts/kpm --line "$$l" verify --strict || exit 1; \
	done

shellcheck:
	@command -v shellcheck >/dev/null || { echo "brew install shellcheck"; exit 1; }
	@shellcheck -S warning $(SCRIPTS)
