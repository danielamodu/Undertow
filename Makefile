FRAUD  := urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)
CHURN  := urn:li:mlModel:(urn:li:dataPlatform:sagemaker,churn_predictor_v1,PROD)

install:
	pip install -e ".[dev]"

quickstart:
	datahub docker quickstart

seed:
	python scripts/seed_datahub.py

# Both models need a baseline: they share an upstream table, so a change to it
# has to be diffable from each one's own last-approved state.
baseline:
	undertow baseline --model "$(FRAUD)"
	undertow baseline --model "$(CHURN)"

break:
	python scripts/break_schema.py

reset:
	python scripts/reset_demo.py

check:
	undertow check --model "$(FRAUD)"

check-churn:
	undertow check --model "$(CHURN)"

check-write:
	undertow check --model "$(FRAUD)" --write-back

# One dropped column, two models, two teams. This is the blast radius: the
# fraud team's change breaks a model the churn team owns, and neither of them
# knows the other exists.
blast-radius:
	-undertow check --model "$(FRAUD)"
	-undertow check --model "$(CHURN)"

# Uses the DataHub MCP server for reads instead of the Python SDK.
check-mcp:
	undertow check --model "$(FRAUD)" --mcp

# Adds the agent investigation loop. Needs --mcp and ANTHROPIC_API_KEY.
# Enriches the report; it cannot change the verdict.
check-investigate:
	undertow check --model "$(FRAUD)" --mcp --investigate

demo: reset baseline break check-write

test:
	pytest tests/ -q
	pytest contrib/datahub-mlmodel-patch-builder/ -q

lint:
	ruff check src/ tests/
	mypy src/

.PHONY: install quickstart seed baseline break reset check check-churn check-write \
        blast-radius check-mcp check-investigate demo test lint
