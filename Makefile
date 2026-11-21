.PHONY: deploy smoke rollback test test-postgres

FILES ?=
TARGET ?=
SCHEMA ?=
ARGS ?=
RELEASE ?=

deploy:
	@test -n "$(TARGET)" -a -n "$(SCHEMA)" -a -n "$(FILES)" || { echo 'usage: make deploy TARGET=staging SCHEMA=002 FILES="path ... | --all-code" [ARGS=--dry-run]'; exit 2; }
	scripts/deploy_prod.sh --target "$(TARGET)" --schema "$(SCHEMA)" $(ARGS) $(FILES)

smoke:
	@test -n "$(TARGET)" -a -n "$(SCHEMA)" || { echo 'usage: make smoke TARGET=staging SCHEMA=002'; exit 2; }
	scripts/smoke_prod.sh --target "$(TARGET)" --schema "$(SCHEMA)"

rollback:
	@test -n "$(TARGET)" -a -n "$(SCHEMA)" -a -n "$(RELEASE)" || { echo 'usage: make rollback TARGET=staging SCHEMA=002 RELEASE=release-ID'; exit 2; }
	scripts/deploy_prod.sh --target "$(TARGET)" --schema "$(SCHEMA)" $(ARGS) --rollback "$(RELEASE)"

test:
	.venv/bin/python -m pytest tests -q

test-postgres:
	scripts/test_postgres.sh
