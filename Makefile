.PHONY: deploy deploy-smoke smoke test

FILES ?=

# make deploy FILES="routers/admin_forms.py main.py"
deploy:
	@if [ -z "$(FILES)" ]; then \
		echo 'usage: make deploy FILES="path/to/file.py [more files]"'; \
		echo '       make deploy FILES=--all-changed'; \
		exit 2; \
	fi
	scripts/deploy_prod.sh $(FILES)

# same as deploy, then run smoke_prod.sh against the live site
deploy-smoke:
	@if [ -z "$(FILES)" ]; then \
		echo 'usage: make deploy-smoke FILES="path/to/file.py [more files]"'; \
		exit 2; \
	fi
	scripts/deploy_prod.sh --smoke $(FILES)

smoke:
	scripts/smoke_prod.sh

test:
	.venv/bin/python -m pytest tests -q
