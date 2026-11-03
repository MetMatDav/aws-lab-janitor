VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint fmt test synth nag

install:
	uv pip install --python $(BIN)/python -r requirements.txt -r requirements-dev.txt
	npm ci

lint:
	$(BIN)/ruff check . && $(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff check --fix . && $(BIN)/ruff format .

test:
	$(BIN)/pytest -q

synth:
	npx cdk synth --quiet

nag:
	npx cdk synth --strict --quiet -c reportEmail=$${JANITOR_REPORT_EMAIL:-ci@example.com}
