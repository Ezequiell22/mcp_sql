# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS build
WORKDIR /build
COPY requirements.lock requirements-sqlserver.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements-sqlserver.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

FROM python:3.12-slim-bookworm AS runtime
# Installing Microsoft's driver requires explicit acceptance by the image builder.
ARG ACCEPT_EULA=N
RUN test "$ACCEPT_EULA" = "Y" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgssapi-krb5-2 \
    && curl --fail --silent --show-error --location https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb -o /tmp/microsoft.deb \
    && dpkg -i /tmp/microsoft.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/* /tmp/microsoft.deb \
    && groupadd --gid 10001 atlas \
    && useradd --uid 10001 --gid atlas --create-home atlas
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /home/atlas
USER 10001:10001
EXPOSE 8091
ENTRYPOINT ["atlas-sql-mcp"]
CMD ["--transport", "stdio"]
