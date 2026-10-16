# Isolated development backend image using the generated backend copy.
# Build from the full checkout root after building the supported base image:
#   docker build -t hermes-agent:local .
#   docker build -f hermes-backend/Dockerfile -t hermes-backend-discovery-dev .
FROM hermes-agent:local

# Overlay only the generated Python backend copy; the default checkout is not
# modified. This copy omits the frontend source and its command runs headless.
COPY hermes-backend/ /opt/hermes/

ENV PYTHONPATH=/opt/hermes
ENV HERMES_DISCOVERY_ES_URL=http://hermes-discovery-es-dev:9200

# `serve` is the headless JSON-RPC/WebSocket backend; do not start dashboard UI.
CMD ["serve", "--host", "127.0.0.1", "--port", "9119", "--isolated"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD curl --fail --silent http://127.0.0.1:9119/api/health || exit 1
