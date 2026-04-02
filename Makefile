.PHONY: install compile package clean all

all: install compile package

install:
	cd "$(CURDIR)" && npm install

compile:
	cd "$(CURDIR)" && npm run compile

package: compile
	cd "$(CURDIR)" && npx @vscode/vsce package --no-dependencies

clean:
	rm -rf out/ node_modules/ *.vsix

# Install Python deps into a venv for the MCP server
python-deps:
	python3 -m venv .venv
	.venv/bin/pip install -e mcp_server/
