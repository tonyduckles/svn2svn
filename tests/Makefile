# Run tests

SHELL_PATH ?= $(SHELL)
RM ?= rm -f
PROVE ?= prove
DEFAULT_TEST_TARGET ?= test

#SVN2SVN_TEST_OPTS=--verbose --debug
SVN2SVN_PROVE_OPTS ?= --timer --jobs 10

# Shell quote
SHELL_PATH_SQ = $(subst ','\'',$(SHELL_PATH))

T = $(sort $(wildcard t[0-9][0-9][0-9][0-9]-*.sh))

all: $(DEFAULT_TEST_TARGET)

test: pre-clean $(TEST_LINT)
	$(MAKE) aggregate-results-and-cleanup

prove: pre-clean _repo_ref $(TEST_LINT)
	@echo "*** prove ***"; $(PROVE) --exec '$(SHELL_PATH_SQ)' $(SVN2SVN_PROVE_OPTS) $(T) :: $(SVN2SVN_TEST_OPTS)
	$(MAKE) clean

$(T): _repo_ref
	@echo "*** $@ ***"; '$(SHELL_PATH_SQ)' $@ $(SVN2SVN_TEST_OPTS)

_repo_ref:
	./make-ref-repo.sh

pre-clean:
	$(RM) -r test-results

clean:
	$(RM) -r test-results
	$(RM) -r _wc_*
	$(RM) -r _repo_t*

test-lint: test-lint-duplicates test-lint-executable

test-lint-duplicates:
	@dups=`echo $(T) | tr ' ' '\n' | sed 's/-.*//' | sort | uniq -d` && \
		test -z "$$dups" || { \
		echo >&2 "duplicate test numbers:" $$dups; exit 1; }

test-lint-executable:
	@bad=`for i in $(T); do test -x "$$i" || echo $$i; done` && \
		test -z "$$bad" || { \
		echo >&2 "non-executable tests:" $$bad; exit 1; }

aggregate-results-and-cleanup: $(T)
	$(MAKE) aggregate-results
	$(MAKE) clean

aggregate-results:
	for f in test-results/t*-*.counts; do \
		echo "$$f"; \
	done | '$(SHELL_PATH_SQ)' ./aggregate-results.sh

.PHONY: pre-clean $(T) aggregate-results clean
