.SILENT:

.PHONY: lint
lint: lint.cli
	ansible-lint -v --exclude cli/ --exclude changelogs/

# Lint + format-check the st-cli (ruff), matching CI (cli-tests.yml).
.PHONY: lint.cli
lint.cli:
	cd cli && ruff check . && ruff format --check .

.PHONY: test.sanity
test.sanity: clean
	ansible-test sanity -v --exclude LICENSE

.PHONY: molecule
molecule:
ifdef role
	molecule test -s $(role)
else
	molecule create --all
	molecule test --all --workers 4
	molecule destroy --all
endif

.PHONY: test
test: lint test.sanity molecule

.PHONY: clean
clean:
	rm -rf build/ tests/
	find . -name ".ansible" -type d -exec rm -rf {} +

# Document a single role with `make docs role=bla`
# Document all roles with `make docs`
# All roles should contain meta/main.yml and meta/argument_specs.yml for aar-doc to work
.PHONY: docs
docs: clean
ifdef role
	aar-doc roles/$(role) defaults; \
  aar-doc --output-file REFERENCE.md roles/$(role) markdown;
else
	@for r in $(shell ls roles/); do \
		aar-doc roles/$$r defaults; \
		aar-doc --output-file REFERENCE.md roles/$$r markdown; \
	done
endif

.PHONY: build
build: clean
	ansible-galaxy collection build --output-path build --force

# Bump the collection + CLI version everywhere: make version version=0.0.23
.PHONY: version
version:
ifndef version
	$(error version is required, e.g. make version version=0.0.23)
endif
	@echo "$(version)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "ERROR: version must be semver X.Y.Z (got '$(version)')"; exit 1; }
	sed -i -E 's/^version: .*/version: $(version)/' galaxy.yml
	sed -i -E 's/^version = ".*"/version = "$(version)"/' cli/pyproject.toml
	sed -i -E 's/^__version__ = ".*"/__version__ = "$(version)"/' cli/st_cli/__init__.py
	$(MAKE) docs

# Scaffold a changelog fragment: make changelog.fragment name=fix-rspamd-port
# Fill in the relevant section(s), then run: make changelog.lint
.PHONY: changelog.fragment
changelog.fragment:
ifndef name
	$(error name is required, e.g. make changelog.fragment name=fix-rspamd-port)
endif
	mkdir -p changelogs/fragments
	f=changelogs/fragments/$(name).yml; \
	if [ -e "$$f" ]; then echo "ERROR: $$f already exists"; exit 1; fi; \
	if [ "$(name)" = release_summary ]; then \
	  printf '%s\n' \
	    '# Summary of the whole release, shown at the top of the changelog.' \
	    'release_summary: |' \
	    '  Describe this release here.' \
	    > "$$f"; \
	else \
	  printf '%s\n' \
	    '# Sections: major_changes, minor_changes, breaking_changes,' \
	    '# deprecated_features, removed_features, security_fixes, bugfixes,' \
	    '# known_issues, release_summary, trivial. Entries are Markdown strings.' \
	    '# Use `trivial:` for changes that should NOT appear in the changelog.' \
	    'minor_changes:' \
	    '  - Describe your change here.' \
	    > "$$f"; \
	fi; \
	echo "Created $$f"

.PHONY: changelog.lint
changelog.lint:
	antsibull-changelog lint

.PHONY: changelog.release
changelog.release:
	antsibull-changelog release
