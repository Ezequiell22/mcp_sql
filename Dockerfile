# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
ARG ACCEPT_EULA=N
RUN test "$ACCEPT_EULA" = "Y" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgssapi-krb5-2 \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb -o /tmp/microsoft.deb \
    && dpkg -i /tmp/microsoft.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/* /tmp/microsoft.deb \
    && useradd --uid 10001 --create-home atlas
WORKDIR /app
COPY MCP/requirements.txt /app/MCP/requirements.txt
RUN pip install --no-cache-dir -r MCP/requirements.txt
COPY MCP /app/MCP
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "MCP.server"]
