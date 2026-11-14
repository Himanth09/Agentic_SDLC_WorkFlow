PY ?= python3

test:            ## tests for the agentic system
	$(PY) -m pytest -q

demo:            ## run every example scenario into runs/
	./scripts/run_demo.sh

lint:
	ruff check --select E,F,W,B sdlc_agent tests && mypy sdlc_agent --ignore-missing-imports

greenfield:
	$(PY) -m sdlc_agent run -f examples/requirements/greenfield_url_shortener.md

interactive:     ## you answer the clarifying questions and approve each gate
	$(PY) -m sdlc_agent run -f examples/requirements/ambiguous_campaign_links.md --approval interactive

.PHONY: test demo lint greenfield interactive
