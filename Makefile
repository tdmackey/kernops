# Fast static checks — run before committing pipeline changes.
#   make check        everything below
#   make shellcheck   needs shellcheck installed (brew install shellcheck)
SHELL := /bin/bash
SCRIPTS := $(wildcard scripts/*.sh) $(wildcard scripts/lib/*.sh) \
	$(wildcard modules/*/build.sh)
PYTOOLS := tools/dashboard/generate.py tools/treadmill/detect.py \
	tools/patchscan/patchscan scripts/write-provenance.py \
	scripts/check-dashboard-config.py scripts/check-workflows.py \
	scripts/module-abi-metadata.py scripts/write-release-summary.py \
	scripts/write-treadmill-report.py \
	tests/test_dashboard.py
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
	@python3 tools/dashboard/generate.py --offline -o /tmp/gb200-dashboard-check.html >/dev/null
	@echo "dashboard: ok"

tests:
	@python3 -m unittest discover -s tests -p 'test_*.py'
	@bash tests/test_scripts.sh
	@echo "tests: ok"

# every pinned base's patch series must apply to its pin (uses local clone)
pins:
	@for b in $$(awk -F'\t' '!/^#/ && NF>=2 {print $$1}' kernel/upstream-base.txt); do \
	  ./scripts/apply-series.sh --check "$$b" || exit 1; \
	done

shellcheck:
	@command -v shellcheck >/dev/null || { echo "brew install shellcheck"; exit 1; }
	@shellcheck -S warning $(SCRIPTS)
