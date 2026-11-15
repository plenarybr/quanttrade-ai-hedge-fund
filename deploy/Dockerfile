# The fund plus the Claude CLI it reasons with.
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t aihf .
#
# Authentication is a CLAUDE_CODE_OAUTH_TOKEN in the environment (from
# `claude setup-token` on a machine with a browser) — never baked into a layer.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

# A non-root user whose home holds both the CLI and ~/.hedge-fund.
RUN useradd -m -u 10001 aihf
USER aihf
ENV HOME=/home/aihf PATH=/home/aihf/.local/bin:$PATH PYTHONUNBUFFERED=1

# The Claude CLI. Alternative, if you prefer npm in your base image:
#   npm install -g @anthropic-ai/claude-code
RUN curl -fsSL https://claude.ai/install.sh | bash

WORKDIR /app
COPY --chown=aihf:aihf . /app
RUN pip install --no-cache-dir --user '.[discord]'

# `paper run <fund>` by default; the bot overrides it in compose.
ENTRYPOINT ["aihf"]
CMD ["--help"]
