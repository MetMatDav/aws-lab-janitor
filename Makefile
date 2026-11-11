VENV := .venv
BIN := $(VENV)/bin
# Put the project venv first so python3 (used by cdk.json), pytest, ruff and
# cfn-lint resolve to it without activating the venv.
export PATH := $(CURDIR)/$(BIN):$(PATH)

.PHONY: install lint fmt test synth nag simulate

install:
	test -d $(VENV) || uv venv --python 3.13 $(VENV)
	uv pip install --python $(BIN)/python -r requirements.txt -r requirements-dev.txt
	npm ci

lint:
	ruff check . && ruff format --check .

fmt:
	ruff check --fix . && ruff format .

test:
	pytest -q

synth:
	npx cdk synth --quiet -c reportEmail=$${JANITOR_REPORT_EMAIL:-ci@example.com}

nag:
	npx cdk synth --strict --quiet -c reportEmail=$${JANITOR_REPORT_EMAIL:-ci@example.com}

# Needs AWS credentials (iam:SimulateCustomPolicy, read-only). Synthesizes the
# stack itself for a placeholder account; pass a profile with
# AWS_PROFILE=<profile> make simulate.
simulate:
	python -m tests.iam.simulate_policies
