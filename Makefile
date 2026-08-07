install:
	pip install -e ".[dev]"

quickstart:
	datahub docker quickstart

seed:
	python scripts/seed_datahub.py

baseline:
	undertow baseline --model "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"

break:
	python scripts/break_schema.py

reset:
	python scripts/reset_demo.py

check:
	undertow check --model "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"

check-write:
	undertow check --model "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)" --write-back

demo: reset baseline break check-write

test:
	pytest tests/ -v

lint:
	ruff check src/ tests/
	mypy src/

.PHONY: install quickstart seed baseline break reset check check-write demo test lint
