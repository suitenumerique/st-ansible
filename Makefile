.SILENT:

.PHONY: lint
lint:
	ansible-lint -v

.PHONY: test.sanity
test.sanity: clean
	ansible-test sanity -v --exclude LICENSE

.PHONY: testfull
testfull: lint test.sanity

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
