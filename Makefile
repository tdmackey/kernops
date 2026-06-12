# Fast static checks — run before committing pipeline changes.
#   make check        everything below
#   make shellcheck   needs shellcheck installed (brew install shellcheck)
SHELL := /bin/bash
SCRIPTS := $(wildcard scripts/*.sh) $(wildcard modules/*/build.sh)
WORKFLOWS := $(wildcard .github/workflows/*.yml)

.PHONY: check syntax yaml pins shellcheck

check: syntax yaml pins
	@echo "── make check: OK"

syntax:
	@for s in $(SCRIPTS); do bash -n "$$s" || exit 1; done
	@python3 -m py_compile tools/dashboard/generate.py tools/treadmill/detect.py
	@echo "syntax: ok ($(words $(SCRIPTS)) scripts, 2 python tools)"

yaml:
	@for w in $(WORKFLOWS); do \
	  ruby -ryaml -e 'YAML.load_file(ARGV[0])' "$$w" 2>/dev/null \
	    || python3 -c "import yaml,sys; yaml.safe_load(open('$$w'))" 2>/dev/null \
	    || { echo "!! $$w failed to parse (need ruby or python3+pyyaml)"; exit 1; }; \
	done
	@echo "yaml: ok ($(words $(WORKFLOWS)) workflows)"

# every pinned base's patch series must apply to its pin (uses local clone)
pins:
	@for b in $$(awk -F'\t' '!/^#/ && NF>=2 {print $$1}' kernel/upstream-base.txt); do \
	  ./scripts/apply-series.sh --check "$$b" || exit 1; \
	done

shellcheck:
	@command -v shellcheck >/dev/null || { echo "brew install shellcheck"; exit 1; }
	@shellcheck -S warning $(SCRIPTS)
