.PHONY: install test run-orchestrator run-logs run-metrics run-events clean

# Default target
all: test

install:
	.venv/bin/pip install -r requirements.txt

test:
	PYTHONPATH=. .venv/bin/python -m unittest tests/test_agents.py

run-orchestrator:
	PYTHONPATH=. .venv/bin/python main.py --role orchestrator

run-logs:
	PYTHONPATH=. .venv/bin/python main.py --role logs

run-metrics:
	PYTHONPATH=. .venv/bin/python main.py --role metrics

run-events:
	PYTHONPATH=. .venv/bin/python main.py --role events

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
